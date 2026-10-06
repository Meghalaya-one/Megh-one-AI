# Schemes — Megh One AI

*Reconciled against the code on 2026-10-06. **Seven** schemes are wired end to end (VERIFIED:
`schema_context.SCHEME_CATALOG`, `annotations._SCHEME_DIRS`, `entity_resolver._RESOLVER_FILE`,
`auth.ROLE_PERMISSIONS`; NRLM added 2026-10-06, see
[SCHEME_ONBOARD_NRLM.md](SCHEME_ONBOARD_NRLM.md) for the step-by-step record).*

Scheme identifiers are the **canonical display strings** used everywhere in code: `"MGNREGA"`,
`"PMAY-G"`, `"Focus Plus"`, `"CM Elevate"`, `"Focus Legacy"`, `"CM Elevate Legacy"`, `"NRLM"`.
No enum exists. The strings are compared literally, and the Qdrant KB `scheme` tag must match them
exactly.

The per-scheme SME contract lives in `data/<folder>/README.md` and its YAMLs. That contract is
more detailed and more authoritative than this page. The rules the SQL generator actually sees
are in `app/schema_context.py` (`_<X>_TABLES`, `_<X>_RULES`, `_<X>_VOCAB`).

## Summary table

| Scheme | Data folder | Query surface | Grain | Money | FY coverage | AC answerable | Few-shot pool | KB docs |
|---|---|---|---|---|---|---|---|---|
| MGNREGA | `data/mgnrega` | `v_employment`, `v_expenditure`, `v_district_year_summary` | source row | lakh ₹ | 2022-23…2025-26 | yes (`assembly_constituency_name`) | `few_shot.yaml` (68) | reference + FAQ + web |
| PMAY-G | `data/pmay` | `v_pmay`, `v_pmay_monthly_sanctions` | house | ₹ (monthly: crore) | 2017-18…2023-24 | no | `pmay_few_shot.yaml` (63) | reference + FAQ + web |
| Focus Plus | `data/focus_plus` | `v_focus_plus` | payment (member × tranche) | unverified | 2022-23, 2025-26 | no | `focusplus_few_shot.yaml` (87) | reference + FAQ |
| CM Elevate | `data/cm_elevate` | `v_cm_elevate` | application | **none** | **none** | no | `cmelevate_few_shot.yaml` (155) | reference + FAQ |
| Focus Legacy | `data/focus_legacy` | `v_focus_legacy` | payment to a producer group | unverified (= members × 5000) | 2021-22, 2022-23, 2024-25, 2025-26 (**no 2023-24**) | yes (via `dim_geography.ac_name`) | `focuslegacy_few_shot.yaml` (86) | reference + FAQ |
| CM Elevate Legacy | `data/cm_elevate_legacy` | `v_cm_elevate_disbursement` | applicant sanction + disbursement | ₹ | 2024-25, 2025-26 | yes (via `dim_geography.ac_name`) | `cmelevatelegacy_prompt_few_shots.yaml` (132) | **shares CM Elevate's** |
| NRLM | `data/NRLM` (upper case) | `v_nrlm` | **one Self Help Group** | ₹, **cumulative, no year** | **formation year only**, 1984-85…2022-23 | yes (`constituency_name_raw`, no join) | `nrlm_few_shot.yaml` (103) | reference + FAQ |

Notes on the table:
- Few-shot counts are the examples **actually loaded** by `annotations.load_all()` (VERIFIED on
  2026-09-26). The loader drops `status: RETIRED_v2.0` entries, which is why PMAY-G loads 63,
  not 71. A raw grep for `question:` over-counts.
- FY lists are the `_SCHEME_DATA_YEARS` defaults. At startup, `refresh_scheme_years` replaces
  them with the DB's distinct `year_key` values.
- **NRLM's "FY" is not a reporting year.** It is the year each SHG was FORMED
  (`formation_financial_year_short`), and it is valid only for SHG-count, member and formation
  questions. NRLM is the only scheme whose money column must NEVER be filtered, grouped or
  trended by it — see the NRLM section below.
- NRLM is the only **group-grained** fact in `megh_db`: one row is one SHG, so `COUNT(*)` is a
  count of groups, never of people.

## Name collisions — must never be guessed

**Focus Plus vs Focus Legacy.**
- They share the name and **no key**:
  - Focus Plus pays individual beneficiaries.
  - Focus Legacy pays producer groups.
- `_SCHEME_NAME_PATTERN` requires a qualifier. A **bare "Focus"** matches neither pattern on
  purpose, and triggers a two-way ask (`_is_ambiguous_focus` → `_focus_ambiguity_clarification`)
  on both the DATA and the KNOWLEDGE paths.
- "Producer group" / "PG" vocabulary belongs to Focus Legacy only.
- Do not re-add a bare "focus" fuzzy alias.

**CM Elevate vs CM Elevate Legacy.**
- A bare "CM Elevate" is **pinned deterministically**, not asked (`_pin_cm_elevate_dataset`):
  - money, sanction, subsidy, loan, lender, LIFCOM, desanction or FY words → Legacy;
  - application-workflow words (on hold, verified, status, gender, withdrawn) → CM Elevate;
  - neither → CM Elevate.
  - A bare "pending" is deliberately **not** a Legacy word.
- The pin is a DATA decision only. The KNOWLEDGE route undoes it (`_unpin_cm_elevate`), because
  both share one KB.
- Every regex or table map mentioning `cm_elevate` must handle the Legacy form first: the
  `cm_elevate` pattern also matches "CM Elevate Legacy", and `cm_elevate` is a substring of the
  Legacy table names.

---

## MGNREGA
- **Purpose:** rural employment guarantee (work days, households, wages, expenditure).
- **Tables:**
  - `fact_mgnrega_employment` and `fact_mgnrega_expenditure`, via `v_employment` /
    `v_expenditure`;
  - `v_district_year_summary`, the only sanctioned place where both facts are combined.
- **Key columns:**
  - `person_days`, `households_employed`, `persons_employed`, `households_completed_100_days`,
    `job_cards_issued_total` (a **stock**), `women_employment_provided`;
  - `total_exp`, `unskilled_wage_exp`, `semi_skilled_wage_exp`, `material_exp` (in **lakh**);
  - `assembly_constituency_name` (on `v_employment`).
- **Rules:**
  - never join the two facts (a fan-out);
  - always SUM … GROUP BY;
  - job cards: one year only;
  - keep lakh (no ÷100 for small areas);
  - dues and pending figures do not exist;
  - wage compliance = unskilled ÷ total.
- **Guards:** `_mgnrega_facts_joined`, `_rowgrain_no_aggregate`,
  `_crore_conversion_for_single_village`, `_mgnrega_split_fact_hint` (repair hint).
- **Unsupported:** dues and pending liabilities, and admin expenditure (a pre-route refusal,
  `_ADMIN_EXPENDITURE_REQUESTED`).
- **Tests:** `test_mgnrega_split_facts.py`, `test_ac_full_results.py`,
  `test_admin_level_collision.py`, `test_block_*`.

## PMAY-G
- **Purpose:** rural housing (houses sanctioned, completed, released).
- **Tables:**
  - `fact_pmay_house` → `v_pmay` (house grain; `lgd_*` on the row);
  - `v_pmay_monthly_sanctions` (statewide, crore, **no geography**);
  - `dim_pmay_house_status` (6 stages).
- **Rules:**
  - `WHERE NOT is_placeholder` — except the beneficiary count, which includes the zero-sanction
    records (decided 2026-10-03, KI-127);
  - "completed" / "in progress" come from the booleans;
  - exclude `sanctioned_amount = 0` from rates;
  - "expenditure" means `amount_released`;
  - filter on `year_key`;
  - the cross-scheme `scheme_code` is `'PMAY'`.
- **Tests:** the cross-scheme and edge suites, plus `tests/test_pmay_usecase_fixes.py` (2026-09-28).
- **Deterministic facts path (D-029):** the fixed use-case shapes (counts, money, remaining,
  stages, release status, rates, summaries, A-vs-B comparisons; one FY or one date) are
  answered by `_pmay_facts_query` / `_pmay_facts_answer` in `app/pipeline.py`, not the SQL
  model. PMAY-G is a village-grained scheme for resolution (`_village_scheme`,
  `_pmay_village_names`). `amount_released` is NULL on 336 houses (nothing released) — COALESCE it.
- **Rules added 2026-09-28:** `_PMAY_RULES` 7–9 (one row per named area, ratio of totals, no
  LIMIT 1 on a comparison); stage phrases with "sanctioned" match only as a phrase.
- **Use-case QA (2026-09-28):** round 1 17 / 28 → after fixes see TESTING.md
  (`docs/PMAY_G_UseCase_Test_Report_2026-09-28*.xlsx`). `v_pmay` equals the raw source row for row.

## Focus Plus
- **Purpose:** Meghalaya state farmer cash benefit (DBT) paid in tranches. Per the reference
  FAQ, it requires producer-group membership; it is not for every farmer.
- **Surface:** `v_focus_plus`, 25 columns. This is the **privacy boundary** for person-level
  data.
- **Rules:**
  - A row is a **payment**, so `COUNT(*)` counts payments.
  - Beneficiaries = `COUNT(DISTINCT beneficiary_key)`.
  - `batch_label` splits the data into two cohorts:
    - `'93K'`: the legacy paid cohort, 4 rows per person, with **no** person columns;
    - `'12.5K'`: the registration cohort, Tranch 4 only, with person columns.
  - Gender, occupation and status breakdowns describe the 12.5K cohort only.
  - `member_id` and `pincode` are PII: count them only, never select them.
  - Blocks use **`block_name_raw`** (Title Case), not `lgd_block`, which is 15% NULL.
  - The unit is unverified: `amount_disbursed` is only ever 5,000 (Tranch 1) or 2,500.
  - A payment size stated in the question ("a loan of five thousand", "₹2,500 payments") is a
    filter: a held amount must reach the SQL (`_focusplus_stated_amount_missing`), any other
    amount pauses (`amount-not-held`), and "loan" gets a "Focus Plus is not a loan" note
    (2026-09-29, KI-132).
- **Special logic:**
  - the tranche gate (`TRANCHE_CLARIFY_ENABLED`);
  - `_person_level_tranche_conflict`;
  - `_focusplus_wants_overall_summary`;
  - `_focusplus_drop_unrequested_verification_status`;
  - `_focusplus_single_district_beneficiary_guard`;
  - `_focusplus_answer_guarantees` (2026-09-27): shares, comparison difference, complete named
    lists and ₹ formatting after the composer (AI_PIPELINE §2.9 step 3);
  - a named batch (12.5K / 93K) or tranche counts as a scope: there is no "which area?" pause.
- **Unsupported:** producer-group questions (those are Focus Legacy), and dates (the only time
  grain is FY).
- **All blocks / all villages (2026-09-27 night):** 204/204 block and 7,026/7,026 village
  questions on the final code
  (`docs/Focus_Plus_AllBlocks_AllVillages_Test_Report_2026-09-27.xlsx`). Villages use the
  village-grained guards shared with MGNREGA (D-028); see AI_PIPELINE §2.3.
- **QA (2026-09-27):** the use cases scored 19 / 30 before fixes and **30 / 30 after**
  (`docs/Focus_Plus_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`). Round 1: The report is
  `docs/Focus_Plus_UseCase_Test_Report_2026-09-27.xlsx`, and its failures (KI-060 to
  KI-063) are fixed. The DB matches raw on every total. The one exception: 175 rows in the Dalu area are
  stored under a different district (KI-065, `docs/Focus_Plus_DB_Issues.md`).
- **Tests:** `test_focusplus_block_columns.py`.

## CM Elevate
- **Purpose:** **applications** to 15 livelihood/enterprise schemes under one programme.
- **Surface:**
  - `v_cm_elevate` (22 columns);
  - `dim_cm_elevate_scheme` (15 rows; use it for "list the schemes");
  - `fact_cm_elevate_application`, only for `scheme_specific @>` queries that use the GIN
    index.
- **Rules:**
  - **no money and no time**: COUNT is the only aggregate, and any year or amount question is
    unanswerable;
  - "on hold" = `data_verified = 'On Hold'`, never the `onhold` boolean, which is always FALSE;
  - applicants = `COUNT(DISTINCT request_id)` (about 36 duplicates, all in Piggery);
  - `applicant_category`, not `type_raw`;
  - `LOWER()` levels and statuses;
  - gender is in `scheme_specific ->> 'gender_id'`;
  - exclude `'Unresolved'` only for village counts;
  - a multi-sub-scheme ask gets a breakdown.
- **Year handling:** `_SCHEME_DATA_YEARS["CM Elevate"] = []` means "no time dimension", so a
  year question gets a refusal (`no-time-dimension`).
- **Tests:** `test_cmelevate_usecase_fixes.py` (33, the 2026-09-27 fixes), plus the cross-scheme,
  collision and follow-up suites.
- **Deterministic guards (2026-09-27, D-027):**
  - SQL rewrites: the programme split, zero-count sectors, pending at a level, plain "pending"
    = On Hold, and approved/rejected on `file_status`;
  - answer guarantees after the composer: `_cm_elevate_answer_guarantees`;
  - "PRIME SEED" routes to CM Elevate.
  - "Pending" = On Hold only (KI-074 decided 2026-09-28; the interim file-status line was removed).
  - Two or more named programmes: each figure plus the combined total (KI-069). A named programme
    with no applications is stated as 0 (KI-182, fixed 2026-10-05) — with only place filters (plus On Hold /
    Valid / level 0-2) the whole answer is rebuilt as a clear breakdown (`_cme_zero_breakdown_answer`). When ALL named programmes
    are 0 (empty result) the answer says "There are no applicants under … in <place> — the data records 0"
    instead of "no matching records", once the place is confirmed in the data (`_cme_zero_programmes_answer`).
- **OFF-009 all ordered programme pairs (2026-10-05):** 2,520 questions (12 districts x 15P2). Before the KI-182 fix
  1,131 FAIL; after it and the empty-result zero answer, 2,520 / 2,520 PASS. Raw = DB and the SQL figures
  were correct in 2,520/2,520 both times.
- **Pending AT a level (KI-186, decided 2026-10-05):** `current_level = 'levelN'` AND `file_status = 'Pending'` — level 1 = 8,307,
  level 2 = 165, level 0 = 0 (all 90 level-0 files are Send Back). Plain "pending" stays On Hold. "Unique applications" =
  COUNT(DISTINCT request_id).
- **Status = `current_file_status`** (Forward / Sent back / Resubmit; decided 2026-09-28, KI-123). Pending stays `data_verified = 'On Hold'`; verified = 'Valid'.
- **All districts / blocks / villages (2026-09-28): 7,364 / 7,364** on the final code (KI-106..120). CM Elevate is a village-grained scheme (`_village_scheme`); urban bodies are stored as blocks ("<TOWN>-MUNICIPAL BOARD" / "-TOWN COMMITTEE") and the block catalogue lags the data (21 of 66 missing) — `_cm_elevate_blocks_to_data` maps to the stored spelling. "Pending" = On Hold only (decided 2026-09-28).
- **QA (2026-09-27):** the use cases (`CM Elevate.csv`, CM-ELEVATE-OFF-001..030).
  - Round 1: **23 / 30** (47 / 55 questions). Report:
    `docs/CM_Elevate_UseCase_Test_Report_2026-09-27.xlsx`.
  - After the same-day fixes (KI-068 to KI-075, D-027): **30 / 30** (55 / 55) on 2 of 2 live
    runs. Report: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`.
  - KI-074, what "pending" means, was decided on 2026-09-28: On Hold only.
  - The raw workbook `CM_Elevate_AllSchemes_20260927_full.xlsx` equals `v_cm_elevate` exactly:
    8,627 rows and 8,600 distinct `request_id`.
  - VERIFIED 2026-09-27: `scheme_specific` also holds `file_status` (Pending 8,472, Send Back 90,
    Rejected 64, Approved 1) and `current_level_raw` on every row. The old "no approval field"
    vocabulary line was corrected on 2026-09-27.

## Focus Legacy
- **Purpose:** the "FOCUS" scheme (Farmers' Collectivization for Upscaling Production and
  Marketing Systems): payments to **producer groups (PGs)**. This is the only group-grained fact.
- **Surface:**
  - `v_focus_legacy` (28 columns; the account number is masked and the holder name is dropped);
  - `dim_producer_group`, queried alone, for first and last payment years;
  - `dim_pg_entity_type` (3 types).
- **Never query:** `fact_focus_legacy_disbursement` or `bridge_pg_bank_history`.
- **Rules:**
  - `amount_disbursed = no_of_pg_members × 5000`, so money and member rankings are the same.
  - Three counting subjects:
    - `COUNT(*)` = payments;
    - `COUNT(DISTINCT pg_id)` = groups;
    - `SUM(no_of_pg_members)` = memberships, not people.
  - A group's **size** is `MAX(no_of_pg_members)` per `pg_id`, never a SUM (guard
    `_focus_legacy_group_size_summed`).
  - Count groups on `pg_id`, never on `pg_name`.
  - Name lookups: strip the group-type suffix and match the core name with ILIKE. The stored
    name looks like "Sakania Pg" or "X Producer Group", so an exact `=` never matches.
  - Never parse `pg_id`: its district token is stale on 1,075 rows.
  - **FY 2023-24 is absent, not zero.** It is shown as a gap and never as a 0. A comparison
    that names it substitutes the nearest year that holds data (`_apply_year_gap`).
  - Dates are treasury batches (30 distinct dates), so no monthly trends.
  - Bank-wise analysis is allowed via `bank_name`.
  - **Duplicate producer groups = groups paid more than once** (product owner, confirmed 2026-10-07, D-032):
    2,655 of 11,906 statewide (2,647 twice, 8 three times), no year split. Answered deterministically by
    `_focus_legacy_duplicate_groups_answer` (place / year filtered). "Duplicate payments / records" (the same
    pg_id paid twice on the same date: 0) is a different question.
- **Special logic:**
  - `_focus_legacy_pg_name_answer` gives **deterministic** answers to "is there a PG named X" /
    "members in X";
  - `_drop_producer_group_names` stops PG names being resolved as places;
  - `_focus_legacy_geo_columns` maps the `*_name_raw` columns (now NULL in the DB) to `lgd_*`;
  - `_focus_legacy_list_total` gives the true count of a truncated list;
  - `_focus_legacy_answer_notes`;
  - an overview `retrieval_query` for RAG;
  - `rag._SCHEME_DOC_NAMES`: the docs call it "FOCUS", never "Focus Legacy".
- **Tests:** `test_focus_legacy_routing.py`, `test_focus_legacy_usecase_fixes.py`,
  `test_pg_name_lookup.py`, `test_pg_abbreviation.py`, `test_lookup_intent.py`,
  `test_ac_focus_legacy.py`, `test_ac_flow_focus_legacy.py`, `test_year_gap_*.py`,
  `test_stale_clarification.py`.
- **QA (2026-09-25):**
  - 28/28 use cases in round 3. VERIFIED from the summary sheet of
    `docs/Focus_Legacy_UseCase_Test_Report.xlsx`.
  - A bulk run of 56 blocks × 3 questions plus 290 PGs × 2 questions,
    `docs/Focus_Legacy_Blocks_and_PGs_Test_Report.xlsx`. The after-fix totals of 168/168 and
    580/580 come from session notes. The report computes them with Excel formulas that have no
    cached values, so they were not machine-verified.

## CM Elevate Legacy
- **Purpose:** CM Elevate **sanctions and disbursements**: 13 schemes, 2,823 rows. The DB calls
  it "CM Elevate Disbursement" (`source_system = 'cm_elevate_disbursement'`).
- **Surface:**
  - `v_cm_elevate_disbursement` (38 columns; needs zero joins for ordinary questions);
  - `dim_cm_elevate_disb_scheme` (13 rows; distinct from CM Elevate's 15).
- **Never query:** `fact_cm_elevate_disbursement` (applicant names). Never join to CM Elevate's
  objects.
- **Rules:**
  - Records = `COUNT(*)`; **sanctioned** = `COUNT(sanctioned_amount)`. Three refused rows have no
    amount.
  - Money is in ₹, shown as `ROUND(SUM/1e7, 2)` crore.
  - Use the stored totals only (`total_disbursement` = subsidy + loan).
  - Utilisation % and sanction rate have fixed formulas.
  - FY comes from the stored label. The Sericulture rows have no year.
  - Lender (`loan_entity`) is `'Bank'`, `'LIFCOM'` or NULL.
  - The refusal fields disagree with each other.
  - Zero is not NULL.
  - A "not held" list covers applicant names, monthly figures, and so on (`_CM_LEGACY_NOT_HELD`).
- **Special logic:**
  - `_cm_legacy_exact_totals` **re-queries exact totals in code**: prose "don't sum rounded
    rows" rules failed 3 times;
  - `_cm_legacy_keep_unresolved_off_village`;
  - `_cm_legacy_qualify_shared_geo_cols`;
  - `_cm_legacy_small_money_notes` ("under ₹0.01 crore");
  - answer-style shots (`_cm_legacy_style_block`);
  - since 2026-09-29: SQL guards `_cm_legacy_sanctioned_count` and `_cm_legacy_unrequested_limit`,
    and the post-composer `_cm_legacy_answer_guarantees` (AI_PIPELINE §2.9a, KI-166..179). Village
    chips pin / rank by this scheme's own records; its urban bodies (TURA MUNICIPAL BOARD,
    WILLIAM NAGAR-, RESUBELPARA-MUNICIPAL BOARD) map to its own stored blocks.
- **KB:** none of its own. It reads CM Elevate's (`rag._KB_SCHEME_ALIAS`).
- **Tests:** `test_cm_elevate_legacy.py` (126 tests, 2026-09-29).
- **QA 2026-09-29:** use cases 33/36 → **36/36** after fixes (KI-166..168); all 12 districts, 59 blocks,
  1,051 villages and 55 constituencies **2,614/2,614** vs DB and raw (KI-169..181). Reports:
  `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29_v2_after_fixes.xlsx`,
  `docs/CM_Elevate_Legacy_AllBlocks_AllVillages_Test_Report_2026-09-29.xlsx`.
- **QA (earlier):** 36/36 use cases, 2026-09-25. VERIFIED from the "FINAL RESULT" in
  `docs/CM_Elevate_Legacy_UseCase_Test_Report_v3_final.xlsx`. The history there: round 1 22/36,
  round 2 33/36, round 3 36/36.

---

## Cross-scheme behaviour (VERIFIED)
- "Which scheme paid out the most?" → `_cross_scheme_money_answer`, deterministic SQL across
  every money-bearing scheme (`_CROSS_SCHEME_MONEY_SQL`). Test: `test_cross_scheme_money.py`.
- MGNREGA + PMAY: `v_cross_scheme_money_district_year` (crore) and
  `v_cross_scheme_village_coverage`. Focus Plus's presence in the money view is unconfirmed;
  CM Elevate is confirmed absent.
- Otherwise, aggregate per scheme and combine at district level in CTEs. Never row-join.
- Scheme listing, comparison, recommendation and "pick one" are answered deterministically
  (`_SCHEME_FIT`, copied from the reference FAQs).
- Unsupported named schemes (PM-KISAN, Ujjwala, …) → `_unsupported_scheme_clarification`.

## NRLM — the Self Help Group register (added 2026-10-06)

National Rural Livelihoods Mission (DAY-NRLM, "Aajeevika"), run in Meghalaya by **MSRLS**
under the C&RD Department. Query surface `curated.v_nrlm` (29 columns, 40,629 rows), lineage
fact `curated.fact_nrlm_shg`, reconciliation `meta.v_reconciliation_nrlm`.

Three facts decide almost every NRLM query, and all three differ from the other six schemes:

**1. One row is one SHG — not a person, a house or a payment.**
`COUNT(*)` counts groups. People are a COLUMN: `SUM(total_members)` (410,847),
`SUM(female_members)` (409,249), `SUM(male_members)`. Reading `COUNT(*)` as people understates
members roughly tenfold. `shg_name` is **display only** — 27,692 names for 40,629 groups, and
'Iatreilang Shg' alone is 295 different SHGs; `shg_code` is the key.

**2. The only year is the year the SHG was FORMED.**
`formation_financial_year_short`, char(7), `'1984-85'`…`'2022-23'`. There is no reporting year,
no release date and no extract-date column. It compares correctly as text. `'Pre-Nrlm'` is a
stored **type**, not a year filter (432 typed vs 645 formed before 2011-12 — the sets differ).

**3. Money is cumulative and undated — the scheme's highest-risk rule.**
`revolving_fund_amount` (RF) and `cif_amount` (CIF) are what each SHG has received **in total**
as at the extract, in rupees. They add across SHGs, so a sum by district, block, type or status
is sound. They can never be placed in a year: *"RF released in 2021-22"*, *"growth in CIF"*,
*"fund utilisation"* and *"has RF increased since last extract"* have **no answer**, because
filtering money by the formation year returns what SHGs formed that year *hold today* — a
different figure that looks exactly like the one asked for. The one allowed form is the
**cohort view**, used only when the user asks for it or accepts the offer, with the sums
aliased `*_held_cr` / `*_held_lakh` so the result cannot be misread.

Because prose rules alone have repeatedly failed under sampling (CLAUDE.md §5), this is
enforced in three places: `_NRLM_RULES` rule 3 in the SQL prompt, the few-shot refusals in
`nrlm_schema_partitions.yaml`, and the absence of any money-plus-year offer in the follow-up
builder, the edge starters and the UI chips (pinned by `tests/test_nrlm_onboarding.py`).

### The other NRLM traps

| Trap | Rule |
|---|---|
| **2,032 SHGs have no village** (`entity_type = 'Unresolved'`, 5.0%) | they carry a correct block and district, so they stay **in** every state / district / block total; add `entity_type <> 'Unresolved'` **only** to village counts, lists, rankings and per-village averages |
| `constituency_name_raw` is NULL on **the same** 2,032 rows | a constituency breakdown adds `IS NOT NULL` and sums to **38,597**, not 40,629 — say so |
| 197 village names belong to more than one village | identity is `village_code`; `COUNT(DISTINCT village_code)`, never by name |
| `lgd_district` / `lgd_block` are **UPPERCASE** | a mixed-case equality filter returns zero rows **silently** |
| `constituency_number_raw` holds `'36 MAWKYRWAT'` | never cast to integer; match `LIKE '36 %'` (the trailing space stops `'3 %'` matching `'36'`) |
| A block and its same-name constituency | cover **different areas** (Mawkyrwat: 1,106 SHGs as a block, 1,092 as a constituency) — 26 block names are also constituency names |
| `gp_name` | free text, no code, **not** an LGD level; differs from the LGD village on ~23-28% of rows. Use only when the user says GP / panchayat / dorbar |
| Zero is a recorded value, never NULL | "received" is `> 0`, "not received" is `= 0`. 73.3% of SHGs hold no CIF, so show the average over **both** populations |
| All 1,197 inactive SHGs show 0 RF and 0 CIF (NR-15) | a fund question filtered to inactive SHGs returns zero — report it with the caveat, never as a funding finding |
| EASTERN WEST KHASI HILLS (740) | a real district, distinct from WEST KHASI HILLS (279) |
| Unconfirmed values | the rupee unit itself (NR-07), 39 SHGs with CIF above ₹5 lakh (NR-25), 5 duplicate pairs (NR-24) |

### Not held by NRLM

Savings, corpus, internal lending, bank linkage, loans, credit, repayment, NPA; federations
(Village Organisation / Cluster Level Federation); member names or any member-level detail;
social category, disability; livelihood activity; training; grading, meetings, Panchasutra;
the formation **date** or month (the source column was empty on every row); households,
population, per-capita, saturation; targets, budgets, fund utilisation; other states; urban
livelihoods (DAY-NULM).

**RF and CIF must never be presented as savings or loans** — they are Mission grants. This is
the likeliest NRLM mistake, so it is in the prompt's NOT-HELD list and in `SCHEME_METRICS`.

### Why "SHG" is not a scheme-name pattern

`_SCHEME_NAME_PATTERN["NRLM"]` matches `nrlm`, `day-nrlm`, `aajeevika`, `msrls`, `srlm` and the
spelled-out mission name — but **not** a bare "SHG". An SHG is the unit NRLM counts, and CM
Elevate also accepts SHG applicants (an `applicant_category` value), so a bare "SHG" must not
name the scheme. It is scheme-specific **vocabulary** instead (`_NRLM_ONLY_TERMS`), which only
fires when no scheme is named — so *"How many CM Elevate applications came from SHGs?"* stays
with CM Elevate.

## Adding a scheme

Schemes are hand-registered. As of 2026-09-26, missing any one registry leaves the scheme
half-wired. The symbols below were VERIFIED to exist.

1. `data/<scheme>/`: the 7 SME YAMLs + README. KB docs go in `data/reference/`, tagged with the
   canonical name. (NRLM's folder is upper-case `data/NRLM/`, as delivered.)
2. `app/annotations.py`: `_SCHEME_DIRS`, `_FEW_SHOT_FILE`, `_FK_FILE`.
3. `app/entity_resolver.py`: `_RESOLVER_FILE`, `_ACTIVITY_VIEWS`, `_AC_CONTENTS_SQL` +
   `AC_CONTENTS_SOURCE` if the scheme answers assembly-constituency questions, and
   `_DIMENSION_ALIASES` if the resolver file names a dimension differently from the six names
   `load_all()` reads (`district`, `block`, `year`, `assembly_constituency`, `tranche_label`,
   `cm_scheme`). NRLM needed the latter: its file says `constituency` and `formation_year`.
   **Miss the alias and the dimension loads as empty, silently** — for NRLM that meant 55
   constituencies resolving to nothing and AC questions falling back to the same-name block.
4. `app/schema_context.py`: `SCHEME_CATALOG`, `SCHEME_METRICS`, `_<X>_TABLES/_RULES/_VOCAB`,
   `_SCHEME_BLOCKS`.
5. `app/pipeline.py`:
   - `_SCHEME_NAME_PATTERN`, `_SCHEME_FUZZY_ALIASES`, the canonical spelling map;
   - `_<X>_ONLY_TERMS` + `_infer_scheme_from_terms`;
   - `_SCHEME_DATA_YEARS` + `refresh_scheme_years`;
   - `_scheme_clarification`, the scheme blurbs, `_SCHEME_FIT`;
   - `_CROSS_SCHEME_MONEY_SQL` if the scheme has money;
   - `_bank_clarification`;
   - `_AC_CAPABLE_SCHEMES` if AC is answerable;
   - every regex whose scheme name is a prefix of the new one;
   - `_SCHEME_DISPLAY_NAME` and `_MEASURE_PLAIN` if the scheme joins the money ranking;
   - the scheme-option lists in `_scheme_clarification` — **both** the generic ask and the
     AC-specific (KI-148) ask, and note the generic option block appears **twice** in the file.
6. `app/followups.py`: `_SCHEME_RX`, `_primary_schemes`, the knowledge ladder, a
   `_<x>_data()` builder + its dispatch.
7. `app/edge.py`: scheme regexes, capability blurbs, `STARTERS`, `_DOMAIN_WORDS`.
8. `app/prompt_builder.py`: `_scheme_of` and the per-scheme exact-match helpers.
9. `app/schema_introspect.py`: the subject→scheme and table-name→scheme maps.
10. `app/kb_ingest.py`: `_SOURCES`.
11. `app/rag.py`: `_SCHEME_DOC_NAMES` (+ `_KB_SCHEME_ALIAS` if it has no docs).
12. `app/auth.py`: every role's `schemes` list, plus the `app/users.yaml` comments.
13. `app/config.py`: `ASR_PROMPT` scheme list. Optionally, `APP_NAME` (already stale; see below).
14. `web/ai_query.html`: the scheme card, i18n strings, `CARD_QUESTIONS`, `prettyName`.

After adding a scheme: restart the service or re-ingest the KB, check
`grep -ic "<label>" data/reference/<its docs>` (the composer fails if the label is absent from
the docs), and add a routing test.

**Stale scheme-count text in code** (cosmetic; not fixed in this documentation task):
- `config.APP_NAME` lists four schemes.
- The `YEAR_RANGE_GUARD_ENABLED` comment says "both schemes … exactly those four years".
- The `annotations.py` docstring says "for both schemes".
- A `_run_pipeline` comment says "only four are loaded".
