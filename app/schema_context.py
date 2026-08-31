"""
Schema context for the SQL generator, condensed from data/schema/schema_for_developers.md,
split per scheme so a query only pays for the schema/rules/vocabulary it actually
needs — matching how few_shot_examples() and prohibited_joins_text() are already
scheme-scoped. This matters more as scheme count grows: at 2 schemes the waste
from including everything is mild; it does not stay mild at 6.

The BUSINESS_VOCABULARY blocks are reconciled from the two source-of-truth
business documents — data/reference/{mgnrega,pmay}_kpi_use_cases.xlsx
(Glossary + Query Logic columns) — against the real curated schema. Only the
COLUMN NAMES and TERM DEFINITIONS were carried over; the Excel's numeric
"Expected Answer" values were deliberately NOT used as ground truth (several
are stale — see the MGNREGA total-expenditure discrepancy noted in the chat,
unresolved as of 2026-08-28) and do not appear here.

Re-derive this whenever a scheme is added or SCHEMA_FOR_DEVELOPERS.md changes;
do not let it drift.
"""

SCHEME_CATALOG = {
    "MGNREGA": "Rural employment guarantee — village-year grain, two facts "
               "(employment, expenditure), money in LAKH RUPEES.",
    "PMAY-G": "Rural housing scheme — one row per sanctioned house, money in RUPEES.",
}

# The metrics each scheme actually carries in the curated data — the plain-English
# list to show a user who asks for something the data does not track ("MGNREGA
# loan defaults", "PMAY-G rent paid"). Reconciled from the *_VOCAB blocks below;
# keep the two in step when either changes.
SCHEME_METRICS = {
    "MGNREGA": [
        "person-days of work generated",
        "households employed / persons employed",
        "households that completed 100 days",
        "job cards issued (cumulative stock)",
        "wage expenditure (unskilled and semi-skilled) and material expenditure",
        "total expenditure (in lakh rupees)",
        "women employment provided (raw count only)",
        "cost per person-day",
    ],
    "PMAY-G": [
        "houses sanctioned, completed, and in progress",
        "house construction stage (Proposed Site, Existing site (Old House), "
        "House Sanctioned, Plinth, Roof Cast, Completed)",
        "sanctioned amount, amount released, amount pending (in rupees)",
        "installments / tranches paid",
        "utilisation rate and completion rate",
    ],
}


def available_metrics_text(schemes: list[str]) -> str:
    """A bullet list of the metrics the given scheme(s) carry, for telling a user
    their question asked for one that isn't in the data."""
    lines: list[str] = []
    for s in schemes or list(SCHEME_METRICS):
        for m in SCHEME_METRICS.get(s, []):
            lines.append(f"  - {s}: {m}")
    return "\n".join(lines)

_PREAMBLE = """
Query only these curated/semantic objects (schema-qualified, e.g. curated.v_pmay).
Never query raw, staging or meta — the connection cannot see them anyway.
""".strip()

_SHARED_TABLES = """
SHARED TABLES
  curated.dim_scheme(scheme_key, scheme_code, scheme_name, money_unit, time_semantics)
  curated.dim_year(year_key, financial_year, financial_year_short, start_year, end_year, data_quality_note)
  curated.dim_geography(geography_key, village_code, lgd_village_name, lgd_block, lgd_district, entity_type)

WHERE scheme_key / scheme_code EXIST — read before adding any scheme filter:
  These two columns live on EXACTLY two objects: curated.dim_scheme and
  curated.v_cross_scheme_money_district_year. They are NOT on curated.v_pmay,
  curated.v_expenditure, curated.v_employment, curated.v_district_year_summary or
  curated.v_pmay_monthly_sanctions — selecting or filtering scheme_key / scheme_code
  there fails with `column "scheme_key" does not exist`.
  Every per-scheme fact table and view is already ONE scheme's data. For a question
  about a single scheme (MGNREGA only, or PMAY-G only) do NOT add a scheme filter and
  do NOT join to curated.dim_scheme — just query that scheme's own object directly.
  A scheme filter is needed ONLY on curated.v_cross_scheme_money_district_year, i.e.
  only for a question that spans BOTH schemes at once.

SCHEME_CODE LITERALS — when you DO filter curated.v_cross_scheme_money_district_year
  (a both-schemes question), these are the ONLY two values stored in scheme_code:
      MGNREGA   -> 'MGNREGA'
      PMAY-G    -> 'PMAY'      <- the code is the bare string 'PMAY'. It is NOT 'PMAY-G',
                                  'PMAY_G', 'PMAYG' or 'PMAY-Gramin'. Filtering
                                  scheme_code = 'PMAY-G' matches ZERO rows and returns a
                                  false 0 for the PMAY side while an unfiltered SUM still
                                  looks right — always write scheme_code = 'PMAY'.
""".strip()

_SHARED_RULES = """
  * Count villages by village_code or geography_key, never by name (duplicate names exist).
  * The ENTIRE dataset is Meghalaya state — every row is already in Meghalaya. "in Meghalaya",
    "across Meghalaya", "state-wide", "overall", or naming no place at all means NO geographic
    filter whatsoever. Never emit lgd_district / lgd_block / lgd_village_name = 'Meghalaya'
    (or ILIKE '%meghalaya%'), and never add a dim_geography sub-select on entity_type = 'State'
    or lgd_village_name = 'MEGHALAYA' — no fact row's lgd_district matches that, so the filter
    silently returns 0 rows. Add a district/block filter ONLY when the question names a
    specific district or block.
  * NEVER put DISTINCT — or a nested aggregate — inside a window function
    (COUNT(DISTINCT x) OVER (...), SUM(DISTINCT x) OVER (...)). PostgreSQL rejects it
    ("DISTINCT is not implemented for window functions"). Do the distinct count in its own
    CTE first, then apply the window function to that.
  * "What share / % of <metric> is concentrated in the top N% (or top decile / quartile) of
    <districts | blocks | villages>" — a concentration / Pareto question. Aggregate per unit,
    rank, then divide — do NOT try to do it in one windowed expression:
      WITH per_unit AS (
        SELECT <unit>, SUM(<metric>) AS m FROM <curated object> [WHERE ...] GROUP BY <unit>
      ), ranked AS (
        SELECT m, NTILE(<100 / N>) OVER (ORDER BY m DESC) AS bucket FROM per_unit
      )
      SELECT ROUND(100.0 * SUM(m) FILTER (WHERE bucket = 1) / NULLIF(SUM(m), 0), 1)
             AS pct_in_top_slice
      FROM ranked
      LIMIT 1;
    top 10% -> NTILE(10); top 25% / quartile -> NTILE(4); top 5% -> NTILE(20). For "top N
    <units>" (a count, not a percentage) rank with ROW_NUMBER() OVER (ORDER BY m DESC) and
    divide the sum of rows <= N by the grand total instead.
""".strip()

_CLOSING = """
Every query MUST end with a LIMIT clause (the executor adds one if you omit it, but include it).
Generate a single read-only SELECT statement only — no comments, no explanation, SQL only.
""".strip()

_MGNREGA_TABLES = """
MGNREGA TABLES
  curated.fact_mgnrega_employment(year_key, geography_key, person_days, households_employed,
      persons_employed, households_completed_100_days, job_cards_issued_total)
  curated.fact_mgnrega_expenditure(year_key, geography_key, unskilled_wage_exp,
      semi_skilled_wage_exp, material_exp, total_exp)  -- LAKH RUPEES
  curated.v_employment / curated.v_expenditure  -- facts pre-joined to year+geography
  curated.v_district_year_summary  -- THE way to combine both MGNREGA facts (district x year)
""".strip()

_MGNREGA_RULES = """
MGNREGA RULES (breaking these produces a wrong number, not just an ugly query):
  1. NEVER join fact_mgnrega_employment to fact_mgnrega_expenditure directly (no shared grain).
     For cross-KPI questions use curated.v_district_year_summary instead.
  2. Both MGNREGA facts are at source-row grain — always SUM ... GROUP BY, never read a row raw.
  3. job_cards_issued_total is a cumulative STOCK. Report ONE financial year, never a sum
     across years. It is still at source-row grain, so the figure for an area is
     SUM(job_cards_issued_total) over that area for a single year_key. For a statewide
     "how many job cards" with no year given, use the latest year:
       SELECT SUM(job_cards_issued_total) AS job_card_stock
       FROM curated.v_employment
       WHERE year_key = (SELECT MAX(year_key) FROM curated.v_employment);
     A bare `SELECT job_cards_issued_total ... LIMIT 1` (no SUM, no GROUP BY) is ALWAYS
     wrong — it returns one village's number as if it were the whole total.
  4. Money unit: MGNREGA expenditure is LAKH RUPEES. State the unit. Never mix with PMAY's rupees.
  5. MGNREGA "dues" / "pending liabilities" / "unpaid amount" have NO column in curated —
     do not compute or estimate one from any other column. Answer that this is not available
     in the current source.
  5a. There is likewise NO "pending" / "unpaid" / "awaiting" count for beneficiaries,
      households, persons or job cards — the employment fact records what WAS provided,
      not a backlog or a waitlist. If the question asks for "pending beneficiaries",
      "pending applications", "paid vs pending", etc., return only the metric that exists
      (e.g. households_employed) and state that a pending/backlog figure is not tracked here.
      Do not emit a fabricated 0 for the pending side.
  6. `women_employment_provided` exists on fact_mgnrega_employment and a raw count may be
     reported, but never publish a ratio/percentage/share computed from it — the definition
     is unconfirmed (see SCHEMA_FOR_DEVELOPERS.md rule 9.1).
  7. cost_per_person_day_rupees: use the precomputed column on v_district_year_summary, or,
     if computing it inline, SUM(total_exp) * 100000 / NULLIF(SUM(person_days), 0) — total_exp
     is LAKH and person_days is a raw count. NEVER divide the crore figure (total_exp / 100)
     by person_days: that is ~0.00004 and rounds to 0 for every row.
""".strip()

_MGNREGA_VOCAB = """
MGNREGA BUSINESS VOCABULARY (source: data/ KPI Use Cases workbook, Glossary sheet)
    "spending" / "expenditure" -> total_exp                "wages" -> unskilled_wage_exp + semi_skilled_wage_exp
    "material cost" -> material_exp                         "people employed" / "beneficiaries" -> households_employed or persons_employed (ask which if ambiguous)
    "work days" / "man-days" -> person_days                 "job cards" -> job_cards_issued_total (STOCK, rule 3)
    "100-day completion" -> households_completed_100_days   "wage compliance" / "60:40 ratio" -> unskilled_wage_exp / total_exp (statutory formula, NOT (unskilled+semi)/total)
""".strip()

_PMAY_TABLES = """
PMAY-G TABLES
  curated.dim_pmay_house_status(status_key, status_name, is_completed, is_in_progress)
      status_name is exactly one of (stage order low->high): 'Proposed Site',
      'Existing site(Old House)', 'House Sanctioned', 'Plinth', 'Roof Cast', 'Completed'
      (12 rows are NULL). For "completed" / "in progress" use the booleans (rule 2);
      for the other four stages filter status_name on the exact string above.
  curated.fact_pmay_house(scheme_key, year_key, geography_key, village_code, status_key,
      sanction_date, sanctioned_amount, amount_released, amount_pending, is_placeholder,
      completed_underfunded)  -- RUPEES
  curated.v_pmay  -- PMAY fact pre-joined to year/geography/status; one row = one house.
      Every row is PMAY-G, so it has NO scheme_key / scheme_code column and needs NO
      scheme filter and NO join to curated.dim_scheme — query it directly.
      Money columns: sanctioned_amount, amount_released, amount_pending (RUPEES). There is
      no single "total expenditure" column like MGNREGA's total_exp — "expenditure" /
      "spending" / "spent" for PMAY-G means amount_released (money actually disbursed).
      Carries lgd_district / lgd_block / lgd_village_name and financial_year /
      financial_year_short directly (no join needed) — this is THE object for any
      statewide, district- or block-level PMAY-G number.
  curated.v_pmay_monthly_sanctions  -- statewide monthly totals ONLY. Exactly four columns:
      sanction_month, houses_sanctioned, sanctioned_cr, released_cr (already CRORE, already
      aggregated). It has NO geography column — selecting lgd_district / lgd_block from it
      errors with `column "lgd_district" does not exist`. Never use it for a per-district,
      per-block or "top N districts" question.
""".strip()

_PMAY_RULES = """
PMAY-G RULES (breaking these produces a wrong number, not just an ugly query):
  1. curated.v_pmay: always filter `WHERE NOT is_placeholder` unless the question is explicitly
     about data completeness.
  2. "Completed" / "in progress" questions use status_key's booleans is_completed /
     is_in_progress, never `status_name = 'Completed'` as a string match.
  3. Exclude `sanctioned_amount = 0` rows (126 known rows) from any utilisation-rate or
     per-house-average calculation — they are a documented data gap, not real zeros.
  4. Money unit: PMAY amounts are RUPEES; v_pmay_monthly_sanctions is already CRORE. State
     the unit. Never mix with MGNREGA's lakhs.
  5. Financial-year filter: use `year_key` (smallint — 2023 means FY 2023-24; data spans
     year_key 2017 through 2023, plus 126 rows with year_key NULL). Do NOT filter on
     `financial_year` (a fixed-width CHAR stored as '2023-2024', NOT '2023-24') or on
     `financial_year_short` ('2023-24') unless you use that column's exact string form —
     `financial_year = '2023-24'` matches zero rows and returns a false 0.
  6. Per-district / per-block / "top N districts" / "share across districts" questions:
     SELECT from curated.v_pmay and GROUP BY lgd_district (or lgd_block) — it carries those
     columns directly. Do NOT use curated.v_pmay_monthly_sanctions for these; it has no
     geography column. For "share concentrated in the top N districts", aggregate the metric
     per district in a CTE, then divide the top-N subtotal by the grand total.
""".strip()

_PMAY_VOCAB = """
PMAY-G BUSINESS VOCABULARY (source: data/ KPI Use Cases workbook, Glossary sheet)
    "sanctioned" -> sanctioned_amount                        "released" / "paid out" / "expenditure" / "spending" / "spent" / "outlay" / "disbursed" -> amount_released
    "pending" / "utilisation gap" -> sanctioned_amount - amount_released, or amount_pending directly
    "utilisation rate" -> amount_released / sanctioned_amount * 100
    "completion rate" -> COUNT(*) FILTER (WHERE is_completed) / COUNT(*) * 100
    "tranche" / "installment" -> installments_paid           "construction stage" -> status_name / is_completed / is_in_progress
    "sanction order" -> sanction_no                          "LGD code" -> village_code (district/block have no code column in curated, name only)
""".strip()

_CROSS_SCHEME = """
CROSS-SCHEME TABLES (only relevant when the question spans MGNREGA and PMAY-G together)
  curated.v_cross_scheme_money_district_year  -- MGNREGA vs PMAY spend, both normalised to CRORE
  curated.v_cross_scheme_village_coverage  -- which villages have MGNREGA / PMAY / both

CROSS-SCHEME — GENERAL PROCEDURE. Apply this to EVERY question that spans MGNREGA and
PMAY-G, however it is worded — reworded, compound, negated, "twist and turn". The worked
examples further down are only instances of this procedure; if a question does not match
one closely, follow the procedure, do NOT pattern-match a near-miss example.

  STEP 1 — sort the question into exactly ONE of three families:

    FAMILY A — MONEY: any spend / expenditure / released / sanctioned / disbursed / outlay
      amount, compared, combined, ranked or shared across the two schemes.
        -> Use curated.v_cross_scheme_money_district_year (already CRORE; one row per
           scheme x district x year; PMAY's scheme_code literal is 'PMAY', never 'PMAY-G').
           Filter / GROUP BY / pivot with FILTER (WHERE scheme_code = ...). Carry
           measure_semantics into the answer.
        -> ONLY if the grain asked is finer than district x year (block, village) does this
           view not fit — then build it with FAMILY C's method applied to the money columns
           (MGNREGA total_exp is LAKH -> /100 for crore; PMAY amount_released is RUPEES
           -> /1e7).

    FAMILY B — COVERAGE / OVERLAP: which, or how many, districts | blocks | villages are
      covered by BOTH schemes / by ONLY one / by EITHER; "common to both", "present in
      both", "where both schemes operate", convergence footprint.
        -> Use curated.v_cross_scheme_village_coverage (one row per village; columns
           mgnrega_person_days and pmay_houses, each NULL where that scheme is absent).
           COALESCE(measure, 0) > 0 means "present", = 0 means "absent".
        -> Roll the village grain UP to the grain asked first (for blocks, JOIN
           curated.dim_geography on village_code to get lgd_block), THEN apply the > 0 / = 0
           tests, THEN COUNT. Never COUNT the raw view for a district/block question.

    FAMILY C — ANY OTHER METRIC, one figure per scheme, side by side: beneficiaries /
      households / persons / houses / person-days / job cards / completion / women /
      100-day / installments / "how many benefited", "how many did each scheme reach /
      help / assist / cover / provide for", etc. There is NO cross-scheme view for these,
      and it is NOT unanswerable.
        -> Compute EACH scheme's figure with EXACTLY the SQL its own single-scheme question
           would use: same view (curated.v_employment / curated.v_expenditure for MGNREGA,
           curated.v_pmay for PMAY-G), same filters (curated.v_pmay ALWAYS
           `WHERE NOT is_placeholder`; a single latest year_key for stock-like MGNREGA
           counts), same aggregate — each inside its OWN subquery / CTE.
        -> Then combine the FINISHED per-scheme sub-results:
             * one statewide number per scheme -> CROSS JOIN the one-row subqueries.
             * a per-district / block / village table -> FULL OUTER JOIN the pre-aggregated
               CTEs on the GRAIN columns only (COALESCE the grain in SELECT), never on a
               measure column.
        -> SELECT each sub-result's column straight through, aliased by scheme.

  UNIVERSAL INVARIANTS — a wrong cross-scheme number almost always means one of these was
  broken:
    * NEVER join or CROSS JOIN a MGNREGA object (curated.v_employment, curated.v_expenditure,
      curated.fact_mgnrega_*) to a PMAY object (curated.v_pmay, curated.fact_pmay_house) on
      geography_key, year_key, village_code, district or any other data column — they share
      no grain. The ONLY valid cross-scheme joins are (a) the two curated.v_cross_scheme_*
      views themselves, and (b) a FULL OUTER JOIN between two subqueries ALREADY aggregated
      to the same grain, joined on the grain columns.
    * NEVER wrap a finished per-scheme subquery in an outer COUNT(*) / SUM() / AVG(). The
      outer aggregate then counts the OTHER table's rows and the figure collapses — this is
      how "PMAY houses benefited" came back ~7k instead of ~171k.
    * For FAMILY C use ONE of exactly two shapes and nothing else:
        - WIDE: CROSS JOIN the one-row per-scheme subqueries; the final SELECT lists
          m.<col>, p.<col> straight through (no aggregate around them). One result row.
        - LONG: a bare `SELECT '<scheme>' AS scheme, <agg> AS value FROM <that scheme's
          view> [filters]` per scheme joined by UNION ALL, and that UNION ALL IS the whole
          query — one result row per scheme.
      NEVER UNION ALL the two schemes and then SELECT ... FROM (that union): the per-scheme
      columns do not line up and any outer aggregate counts union rows, not real records.
    * Each per-scheme figure MUST equal what that scheme's standalone query returns. If
      "houses benefited for PMAY-G" alone is `COUNT(*) FROM curated.v_pmay WHERE NOT
      is_placeholder`, the PMAY side of the cross-scheme answer is that identical expression.
    * The two schemes measure DIFFERENT things (MGNREGA person-days / households given work
      vs PMAY-G houses). Do NOT add them into one "combined total" unless both sides are the
      SAME unit (money, both in crore). Otherwise report one figure per scheme and state
      each unit — and, for MGNREGA yearly counts, the financial year.
    * curated.bridge_geography_source is MGNREGA-internal (source_system 'EMPLOYMENT' /
      'EXPENDITURE' only) — it has NO PMAY rows and can never answer a cross-scheme question.

  Worked examples — each is ONE instance of the procedure above. For anything not shown,
  follow the procedure; do not force the question onto the nearest example.

  Worked example (FAMILY C template) — "How many households/beneficiaries benefited from
  each scheme?" / "households benefited across MGNREGA and PMAY-G" / "how many people did
  the two schemes reach". One figure per scheme, different units, NO combined total.
  Aggregate each scheme in its OWN one-row subquery exactly as its single-scheme query
  would, CROSS JOIN, and SELECT each column straight through:
    SELECT m.mgnrega_households_employed, p.pmay_houses_sanctioned
    FROM (SELECT SUM(households_employed) AS mgnrega_households_employed
          FROM curated.v_employment
          WHERE year_key = (SELECT MAX(year_key) FROM curated.v_employment)) m
    CROSS JOIN (SELECT COUNT(*) AS pmay_houses_sanctioned
                FROM curated.v_pmay
                WHERE NOT is_placeholder) p
    LIMIT 1;
  The MGNREGA side is a single financial year (latest if none named), like every
  households_employed / person-days figure; the PMAY side is all sanctioned houses across
  the data window. State both units and the financial year in the answer. For a
  per-district version, GROUP BY lgd_district inside each subquery and FULL OUTER JOIN on
  lgd_district (COALESCE it in SELECT).

  Worked example — "Compare MGNREGA and PMAY spending in West Garo Hills":
    SELECT scheme_code, amount_crore, measure_semantics
    FROM curated.v_cross_scheme_money_district_year
    WHERE lgd_district = 'WEST GARO HILLS'
    ORDER BY scheme_code
    LIMIT 10;

  Worked example — "Compare districts for MGNREGA and PMAY-G" / any per-district table with
  one MGNREGA column, one PMAY column and a combined column. The view is LONG (one row per
  scheme), so split the schemes with conditional aggregation — and the PMAY filter literal
  is 'PMAY', never 'PMAY-G' (see SCHEME_CODE LITERALS). A wrong PMAY literal is exactly what
  makes the PMAY column come back 0 while the combined column still looks right:
    SELECT lgd_district,
           ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'MGNREGA'), 2) AS mgnrega_total_crore,
           ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'PMAY'), 2)    AS pmay_total_crore,
           ROUND(SUM(amount_crore), 2)                                        AS combined_total_crore
    FROM curated.v_cross_scheme_money_district_year
    GROUP BY lgd_district
    ORDER BY combined_total_crore DESC
    LIMIT 50;

  Worked example — "Top 5 blocks by combined MGNREGA expenditure + PMAY-G amount released" /
  ANY block- or village-level combined-money question. There is NO block-grain cross-scheme
  view, and joining curated.v_expenditure to curated.v_pmay is prohibited. Build it the way
  the curated views build v_district_year_summary internally: aggregate EACH scheme to the
  target grain in its own CTE, convert both to CRORE (MGNREGA total_exp is LAKH -> /100;
  PMAY amount_released is RUPEES -> /1e7), then FULL OUTER JOIN the two pre-aggregated CTEs
  on the grain columns only — never on a fact column. Block names do not repeat across
  districts, but carry lgd_district through both sides and join on both:
    WITH mgnrega AS (
      SELECT lgd_district, lgd_block, SUM(total_exp) / 100 AS mgnrega_exp_cr
      FROM curated.v_expenditure
      GROUP BY lgd_district, lgd_block
    ), pmay AS (
      SELECT lgd_district, lgd_block, SUM(amount_released) / 1e7 AS pmay_released_cr
      FROM curated.v_pmay
      WHERE NOT is_placeholder
      GROUP BY lgd_district, lgd_block
    )
    SELECT COALESCE(m.lgd_district, p.lgd_district)  AS lgd_district,
           COALESCE(m.lgd_block, p.lgd_block)        AS lgd_block,
           ROUND(COALESCE(m.mgnrega_exp_cr, 0), 2)   AS mgnrega_exp_cr,
           ROUND(COALESCE(p.pmay_released_cr, 0), 2) AS pmay_released_cr,
           ROUND(COALESCE(m.mgnrega_exp_cr, 0) + COALESCE(p.pmay_released_cr, 0), 2) AS combined_cr
    FROM mgnrega m
    FULL OUTER JOIN pmay p
      ON m.lgd_district = p.lgd_district AND m.lgd_block = p.lgd_block
    ORDER BY combined_cr DESC
    LIMIT 5;
  For a per-village version, GROUP BY village_code (+ lgd_village_name) on each side and join
  on village_code. The two amounts stay different kinds of number — MGNREGA expenditure
  incurred vs PMAY money released — so say that in the answer, exactly as measure_semantics
  would for the district view.

  Worked example — "Which districts have BOTH MGNREGA activity and PMAY-G houses
  sanctioned?" (a coverage question — roll the village-grain coverage view up to
  district; "has activity" / "has houses" means the summed measure is > 0, with a
  NULL measure treated as 0):
    SELECT lgd_district
    FROM curated.v_cross_scheme_village_coverage
    GROUP BY lgd_district
    HAVING COALESCE(SUM(mgnrega_person_days), 0) > 0
       AND COALESCE(SUM(pmay_houses), 0) > 0
    ORDER BY lgd_district
    LIMIT 20;
  For "only MGNREGA" / "only PMAY-G", set the other side's HAVING term to = 0. For a
  per-village version, SELECT village_code, lgd_district and filter the two measures
  directly with no GROUP BY. NEVER answer a coverage question from a bare
  SELECT DISTINCT lgd_district — that lists every district and proves nothing about
  which have both.

  Worked example — "How many common districts are in both schemes?" / "how many
  districts have both MGNREGA and PMAY-G activity?" (the COUNT of that same
  district set — NOT a count of rows). The coverage view is one row per village,
  so counting it directly counts villages, not districts. Roll up to district
  first, then count the districts that qualify:
    SELECT COUNT(*) AS districts_with_both
    FROM (
      SELECT lgd_district
      FROM curated.v_cross_scheme_village_coverage
      GROUP BY lgd_district
      HAVING COALESCE(SUM(mgnrega_person_days), 0) > 0
         AND COALESCE(SUM(pmay_houses), 0) > 0
    ) d;
  Same shape for "how many villages are in both schemes" EXCEPT there GROUP BY /
  the subquery is not needed — the view is already one row per village, so
  SELECT COUNT(*) ... WHERE COALESCE(mgnrega_person_days,0) > 0 AND
  COALESCE(pmay_houses,0) > 0. NEVER COUNT(*) the raw view for a "how many
  districts" question — that returns the village count (a number in the
  thousands), not the ~12 districts asked for.

  Worked example — "How many BLOCKS are common to both schemes?" / "block-wise,
  which blocks are in both schemes?" / "how many blocks have both MGNREGA and
  PMAY-G activity?". curated.v_cross_scheme_village_coverage carries village_code
  and lgd_district but NOT lgd_block, so join it to curated.dim_geography on
  village_code to pick up the block, roll the village grain up to
  (lgd_district, lgd_block), keep the blocks where BOTH summed measures are > 0,
  then count them:
    SELECT COUNT(*) AS blocks_with_both
    FROM (
      SELECT g.lgd_district, g.lgd_block
      FROM curated.v_cross_scheme_village_coverage v
      JOIN curated.dim_geography g ON g.village_code = v.village_code
      GROUP BY g.lgd_district, g.lgd_block
      HAVING COALESCE(SUM(v.mgnrega_person_days), 0) > 0
         AND COALESCE(SUM(v.pmay_houses), 0) > 0
    ) b;
  To LIST the blocks instead, drop the COUNT(*) wrapper and finish with
  ORDER BY g.lgd_district, g.lgd_block LIMIT 50. For "blocks with only MGNREGA" /
  "only PMAY-G", set the other HAVING term to = 0. Always GROUP BY BOTH
  lgd_district and lgd_block (a bare block name can repeat across districts), and
  NEVER COUNT(*) the joined rows directly — that counts villages, not blocks.

  Worked example — "How many villages have MGNREGA activity but no PMAY-G house
  sanctioned?" (count of villages covered by ONE scheme and not the other). This is
  curated.v_cross_scheme_village_coverage as well — NOT curated.bridge_geography_source,
  NOT a join between curated.v_employment and curated.v_pmay. The view is already one
  row per village, so NO GROUP BY; each measure is NULL when that scheme did nothing
  there, and NULL means "no activity", so COALESCE it to 0 on BOTH sides:
    SELECT COUNT(*) AS villages
    FROM curated.v_cross_scheme_village_coverage
    WHERE COALESCE(mgnrega_person_days, 0) > 0
      AND COALESCE(pmay_houses, 0) = 0;
  Swap the two conditions for "PMAY-G house but no MGNREGA activity". Both sides > 0
  is "villages with BOTH"; OR the two is "villages with EITHER". To list the villages
  rather than count them, SELECT village_code, lgd_district and add LIMIT.

  Worked example — "Give me the full MGNREGA + PMAY-G convergence picture for Meghalaya —
  spend, coverage, completion, and where the two schemes overlap or don't" / "the whole
  convergence / overlap picture" / any SINGLE request that wants combined SPEND +
  COMPLETION + COVERAGE/OVERLAP across both schemes at once. No one view carries all of
  that, and it is NOT unanswerable — build a district-grain table by aggregating four
  independent CTEs to lgd_district and LEFT JOINing them onto the full district list:
  money (pivoted per scheme from the cross-scheme money view), village overlap (rolled
  up from the cross-scheme coverage view), MGNREGA completion and PMAY completion (each
  from that scheme's own detail view). Join ONLY on lgd_district, never on a fact column:
    WITH money AS (
      SELECT lgd_district,
             ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'MGNREGA'), 2) AS mgnrega_crore,
             ROUND(SUM(amount_crore) FILTER (WHERE scheme_code = 'PMAY'), 2)    AS pmay_crore
      FROM curated.v_cross_scheme_money_district_year
      GROUP BY lgd_district
    ), coverage AS (
      SELECT lgd_district,
             COUNT(*) FILTER (WHERE COALESCE(mgnrega_person_days,0) > 0
                                AND COALESCE(pmay_houses,0) > 0) AS villages_both,
             COUNT(*) FILTER (WHERE COALESCE(mgnrega_person_days,0) > 0
                                AND COALESCE(pmay_houses,0) = 0) AS villages_mgnrega_only,
             COUNT(*) FILTER (WHERE COALESCE(mgnrega_person_days,0) = 0
                                AND COALESCE(pmay_houses,0) > 0) AS villages_pmay_only
      FROM curated.v_cross_scheme_village_coverage
      GROUP BY lgd_district
    ), mgnrega_done AS (
      SELECT lgd_district, SUM(households_completed_100_days) AS mgnrega_100day_households
      FROM curated.v_employment
      GROUP BY lgd_district
    ), pmay_done AS (
      SELECT lgd_district,
             COUNT(*)                             AS pmay_houses_sanctioned,
             COUNT(*) FILTER (WHERE is_completed) AS pmay_houses_completed
      FROM curated.v_pmay
      WHERE NOT is_placeholder
      GROUP BY lgd_district
    ), districts AS (
      SELECT lgd_district FROM money
      UNION SELECT lgd_district FROM coverage
      UNION SELECT lgd_district FROM mgnrega_done
      UNION SELECT lgd_district FROM pmay_done
    )
    SELECT d.lgd_district,
           COALESCE(mo.mgnrega_crore, 0)             AS mgnrega_crore,
           COALESCE(mo.pmay_crore, 0)                AS pmay_crore,
           COALESCE(md.mgnrega_100day_households, 0) AS mgnrega_100day_households,
           COALESCE(pd.pmay_houses_sanctioned, 0)    AS pmay_houses_sanctioned,
           COALESCE(pd.pmay_houses_completed, 0)     AS pmay_houses_completed,
           COALESCE(co.villages_both, 0)             AS villages_both,
           COALESCE(co.villages_mgnrega_only, 0)     AS villages_mgnrega_only,
           COALESCE(co.villages_pmay_only, 0)        AS villages_pmay_only
    FROM districts d
    LEFT JOIN money mo        USING (lgd_district)
    LEFT JOIN coverage co     USING (lgd_district)
    LEFT JOIN mgnrega_done md USING (lgd_district)
    LEFT JOIN pmay_done pd    USING (lgd_district)
    ORDER BY (COALESCE(mo.mgnrega_crore,0) + COALESCE(mo.pmay_crore,0)) DESC
    LIMIT 20;
  mgnrega_crore is MGNREGA expenditure incurred and pmay_crore is PMAY money released
  against sanctions — different kinds of number, so say that in the answer (exactly as
  measure_semantics would). villages_both / villages_mgnrega_only / villages_pmay_only
  ARE the "where they overlap or don't" finding. For a single statewide row, drop
  lgd_district from every CTE's SELECT and GROUP BY, drop the districts CTE, and
  cross join the four one-row CTEs: SELECT * FROM money, coverage, mgnrega_done, pmay_done.
""".strip()

_SCHEME_BLOCKS = {
    "MGNREGA": (_MGNREGA_TABLES, _MGNREGA_RULES, _MGNREGA_VOCAB),
    "PMAY-G": (_PMAY_TABLES, _PMAY_RULES, _PMAY_VOCAB),
}


def build_schema_context(schemes: list[str]) -> str:
    """Assemble only the tables/rules/vocabulary the classified scheme(s) need.
    Shared dimensions and the LIMIT/read-only instructions are always included;
    cross-scheme views only appear when more than one scheme is in play."""
    parts = [_PREAMBLE, _SHARED_TABLES]

    for scheme in schemes:
        block = _SCHEME_BLOCKS.get(scheme)
        if block:
            parts.extend(block)

    if len(schemes) > 1:
        parts.append(_CROSS_SCHEME)

    parts.append(f"SHARED RULES:\n{_SHARED_RULES}")
    parts.append(_CLOSING)
    return "\n\n".join(parts)


# Kept for any caller that still wants the full, unscoped context (e.g. a
# fallback if scheme classification produced nothing usable).
SCHEMA_CONTEXT = build_schema_context(list(SCHEME_CATALOG))
