# megh_db — Data Model Map (MGNREGA + PMAY-G)

Condensed from `DataPart/SCHEMA_FOR_DEVELOPERS (2).md` (live `information_schema`
extract, 2026-08-24 — 16 curated objects, 204 columns). That document is the
authority; this is the working map. `backend/schema_context.py` is the *in-prompt*
condensation the SQL generator sees — **keep all three in sync** when the schema
changes.

Since 2026-08-29 the SQL prompt also carries a **LIVE SCHEMA** block built at
startup by `backend/schema_introspect._load_live_schema()` — a real
`information_schema.columns` + `pg_constraint` read of `curated`/`semantic`,
assembled into the prompt by `backend/prompt_builder.py`. The hand-written files
above stay the source of the *hazard rules* (which joins are invalid, which
columns are stocks — not introspectable), but a column list that drifts from the
database is now visible to the generator as a contradiction, and in `GET
/admin/schema` (`live_columns` / `live_fks`). If the startup read fails (offline
dev, catalog perms) the prompt silently falls back to `schema_context.py` alone.

| | |
|---|---|
| Database | `megh_db` on PostgreSQL 18.4 @ `10.48.242.4:5432` |
| App role | `megh_readonly` — `SELECT` on `curated` + `semantic` only |
| Schemas | `raw` / `staging` / `curated` / `semantic` / `meta` — query **`curated`** only |
| Schemes | MGNREGA (employment + expenditure), PMAY-G (housing) |

---

## The six rules that produce a *wrong number* if broken

1. **Never join the two MGNREGA fact tables to each other.** Both allow multiple
   rows per (year, village); a row-level join multiplies totals. Use
   `curated.v_district_year_summary`, or aggregate each side to the same grain first.
2. **Both MGNREGA facts are at SOURCE-ROW grain** — one row = one source-workbook
   row, not one village-year. Always `SUM … GROUP BY`; never read a row raw.
3. **Money units differ by scheme — three are live at once.** MGNREGA expenditure
   = **LAKH** rupees; PMAY `sanctioned_amount` / `amount_released` / `amount_pending`
   = **RUPEES**; the monthly + cross-scheme views = **CRORE**. State the unit; never
   mix units in one `SUM`. `curated.dim_scheme.money_unit` is the authority.
4. **`job_cards_issued_total` is a cumulative STOCK** — never `SUM` it across years.
   Report one financial year (2025-26 stock = 520,999).
5. **Count villages by `village_code` / `geography_key`, never by name** — 345
   village names are shared by more than one village.
6. **PMAY and MGNREGA facts are at different grains** (house vs village-year) —
   never join them directly. Use `v_cross_scheme_money_district_year` (district ×
   year) or `v_cross_scheme_village_coverage` (village).

Extra guards carried in `schema_context.py`:

7. `curated.v_pmay` — always `WHERE NOT is_placeholder` unless the question is about
   data completeness.
8. **MGNREGA "dues" / "pending liabilities" / "unpaid amount" have no column** —
   answer "not available in the current source", never estimate one.
9. PMAY "completed" / "in progress" → the `is_completed` / `is_in_progress`
   booleans on `dim_pmay_house_status`, never a `status_name` string match.
10. PMAY — exclude `sanctioned_amount = 0` rows (126 known) from any
    utilisation-rate or per-house-average.
11. MGNREGA `women_employment_provided` — a raw count may be reported, but **never**
    a ratio/percentage from it (definition unconfirmed; exceeds `persons_employed`
    on 582 rows).
12. **The whole dataset is Meghalaya.** "in Meghalaya" / "state-wide" / no place named
    ⇒ **no** geographic filter. Never `lgd_district/lgd_block/lgd_village_name = 'Meghalaya'`
    and never a `dim_geography` sub-select on `entity_type = 'State'` /
    `lgd_village_name = 'MEGHALAYA'` — no fact row matches, so it silently returns 0.

---

## Star schema

```
        dim_scheme            dim_year            dim_pmay_house_status
      (scheme_key)          (year_key)              (status_key)
            │                 │  │  │                    │
            └── fact_pmay_house ─┘  │  └── fact_mgnrega_expenditure
                   │  │            │              │
        status_key │  │ geo_key    │ year_key     │ geo_key
                   │  └── fact_mgnrega_employment │
                   │         │  geo_key           │
                   ▼         ▼                    ▼
                 ───────── dim_geography ──────────
                              ▲
                 dim_geography_alias   bridge_geography_source
                 (MGNREGA facts only)

   ✗ NO join edge between any two fact tables — by design (rules 1, 6)
   ✗ fact_pmay_house has NO alias_key (PMAY resolves geography at ingest)
```

### Dimensions

| Object | Grain / rows | Key columns |
|---|---|---|
| `curated.dim_scheme` | one row per scheme | `scheme_key`, `scheme_code`, `money_unit`, `time_semantics`, `grain_note` |
| `curated.dim_year` | 4 rows, FY 2022-23 → 2025-26 | `year_key` (FY start year), `financial_year` `'2022-2023'`, `financial_year_short` `'2022-23'`, `data_quality_note` |
| `curated.dim_geography` | 7,364 rows | `geography_key` (surrogate, **not stable across reload**), `village_code` (LGD, natural key — use this externally), `lgd_village_name`, `lgd_block`, `lgd_district` (block/district stored UPPERCASE), `entity_type` |
| `curated.dim_geography_alias` | 18,959 rows | every source spelling → `geography_key`. Name lookup only, never a join key for aggregation. MGNREGA only. |
| `curated.dim_pmay_house_status` | construction stages | `status_key`, `status_name`, `is_completed`, `is_in_progress` (both nullable — `WHERE is_completed` excludes NULL) |
| `curated.bridge_geography_source` | 18,456 rows | which villages appear in which source, over which years — coverage questions |

`dim_year` notes: **2024-25** employment looks like a partial extract; **2025-26**
women's column is zero on every row. Surface `data_quality_note` with any answer
covering those years.

### Fact tables

| Object | Grain | Rows | Measures |
|---|---|---|---|
| `curated.fact_mgnrega_employment` | one source row | 26,375 | `person_days` (headline), `households_employed`, `persons_employed`, `households_completed_100_days`, `job_cards_issued_total` (STOCK), `women_employment_provided` (⚠ no ratios) |
| `curated.fact_mgnrega_expenditure` | one source row | 18,818 | **LAKH ₹** — `unskilled_wage_exp`, `semi_skilled_wage_exp`, `material_exp`, `tax_exp`, `total_exp` |
| `curated.fact_pmay_house` | one sanctioned house | not verified | **₹** — `sanctioned_amount` (≈ per-house constant), `amount_released` (varies), `amount_pending`; `sanction_date` / `sanction_month` (only real dates in the DB); `status_key`; `year_key` **nullable** (sanction outside FY22-26); flags `is_placeholder`, `completed_underfunded`, `mapping_category` |

### Views — query these, not the raw facts

| View | Use |
|---|---|
| `curated.v_employment`, `curated.v_expenditure` | fact + year + geography resolved; **still source-row grain — aggregate** |
| `curated.v_district_year_summary` | the **only** sanctioned way to combine the two MGNREGA facts; precomputes `unskilled_wage_share_pct` (correct `unskilled ÷ total` formula), `cost_per_person_day_rupees` |
| `curated.v_pmay` | PMAY house grain — `COUNT(*)` is a house count; `WHERE NOT is_placeholder` |
| `curated.v_pmay_monthly_sanctions` | statewide, already aggregated, already **CRORE** |
| `curated.v_cross_scheme_money_district_year` | MGNREGA vs PMAY spend, both normalised to **CRORE**; carry `measure_semantics` into the answer (the two amounts are different kinds of number) |
| `curated.v_cross_scheme_village_coverage` | which villages get MGNREGA / PMAY / both; expect NULLs — that is the finding |

---

## How MGNREGA and PMAY-G relate

- The **only** shared key is `village_code` (LGD code) via `dim_geography`. That is
  what lets the two be compared at all.
- Different grains, different money units, different time semantics (MGNREGA
  reporting year vs PMAY sanction FY). No FK between the fact tables.
- Cross-scheme questions have two base paths: the district×year money view
  (`v_cross_scheme_money_district_year`) and the village coverage view
  (`v_cross_scheme_village_coverage`).
- A compound "full convergence / overlap picture" question (spend **and** completion
  **and** coverage in one ask) is still one query: aggregate money, village overlap,
  MGNREGA 100-day completion and PMAY completion to `lgd_district` in four independent
  CTEs, then `LEFT JOIN` them on `lgd_district` only. See the worked example in
  `schema_context.py` (`_CROSS_SCHEME`). Never join any of those CTEs on a fact column.
- Anything the cross-scheme views plus that composition still don't reach — combined
  money at block/village grain aside (its own worked example) — is answered per scheme
  and combined in prose.

---

## Verified totals — MGNREGA only, as of 2026-08-20

| Measure | Value |
|---|---:|
| Total expenditure, 4 years | ₹3,628.67 Cr (362,867 lakh) |
| Person-days, 4 years | 90,915,181 |
| 100-day completions, 4 years | 171,125 |
| Job-card stock, 2025-26 | 520,999 |
| Villages with any MGNREGA activity | 6,491 |

**PMAY-G has no verified totals.** Nothing has been double-derived — house counts,
released/pending crore and completion rates must be measured before they are
quoted. `SCHEMA_FOR_DEVELOPERS` §8 / §12.
