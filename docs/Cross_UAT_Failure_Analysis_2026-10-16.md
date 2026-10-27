# Cross-scheme UAT — failure analysis and re-test (2026-10-16)

> **UPDATE, later on 2026-10-16: the code fixes were made (D-036).** The user's instructions were:
> - fix everything except Focus+;
> - PMAY-G includes all 1,71,107 houses;
> - an unnamed or exclusion-only question compares the four UAT schemes;
> - do not change anything else.
>
> **Live re-run of all 60 cases on the fixed code (VERIFIED):**
> - **59 / 60 pass.** All 21 earlier passes still pass, and 38 of the 39 failures now pass.
> - **1 left unchanged:** T13.3, a Focus+-only question, per the instruction.
> - **24 of the 59** still show a number that differs from the testers' source file only because of the data:
>   - Focus+ West Garo Hills / South West Garo Hills (KI-065) and Focus+ villages (KI-084), both left as is;
>   - MGNREGA West Garo Hills / South West Garo Hills ±445 (KI-240).
>
>   Every other figure matches.
>
> **Where the results are:** sheet "After fixes (60)" in `docs/Cross_UAT_Retest_2026-10-16.xlsx`.
>
> **Tests:** `tests/test_cross_uat_fixes.py` (new); pytest 1,934 passed, 2 skipped; 14 / 14 scripts.
>
> **Also fixed while verifying:**
> - T02.2: "How do … compare" now goes to the fixed comparison.
> - T13.2: "reaches the most villages" is now a coverage ask.
> - T04.3: one FY for money now goes to the fixed comparison.
> - T19.2: the 4B verifier's false "prohibited join" on a UNION of views is now discarded.
>
> **Not changed, seen only after a clarification chip (UAT already counts these cases as Pass):**
> - T05.3: MGNREGA + PMAY-G only, on the older model path, still ₹3,628.68 / ₹2,185.24 from the rounded view.
> - T08.3, T12.3, T20.3: model-path answers after the year chip use 1,70,981 or raw rupees.
>
> These were outside the 39 failures, so they were left alone ("don't change others").
>
> The analysis below is the state **before** the fixes.

**Input:** `Megh_One_AI_Cross_UAT_Test_Report.xlsx` (repo root): 60 cases, 21 passed, 39 failed. The testers ran it
on 15 Oct 2026 against the deployed app (`https://115.124.102.167:8300/ai-query`), one new chat per question.

**Re-test (VERIFIED, 2026-10-16):** all 39 failed questions were re-run on the **current local code** (working tree,
including the uncommitted 2026-10-15 MGNREGA change):
- through `pipeline.answer_question`, in-process, in a fresh session each, with the live DB and model gateway (VPN);
- clarifications were answered as a user would (the all / combined chip, else the first).

Every number the bot gave was then checked against megh_db with read-only aggregate SQL.

Row-level results are in `docs/Cross_UAT_Retest_2026-10-16.xlsx`:
- sheet 1: one row per case, with the bot's answer now, the root causes and why it fails;
- sheet 2: the root-cause index.

Harness and raw answers: session scratchpad only (`uat_run.py`, `uat_results.json`), not committed.

## 1. Result

| | Cases |
|---|---|
| Failed in UAT (deployed app) | 39 |
| **Pass on current code** | **3** — T14.2, T14.3, T19.3 (fixed by the 2026-10-15 MGNREGA all-years change; **needs a deploy**) |
| **Still fail on current code** | **36** |
| … would pass after code fixes, if the two decisions in §3 go the testers' way | 18 |
| … still differ from the source after code fixes, because the DB differs from the source files | 18 |

**The deployed app is behind the local code.** The 2026-10-15 MGNREGA change (households summed over FY 2022-23
to 2025-26, 14,94,437) has not been deployed. That is why the testers saw 3,65,556 everywhere. Locally, every
MGNREGA state and district count is now the all-years figure.

## 2. Root causes (most cases first)

| ID | Root cause | Cases | Type | Tracked |
|---|---|---|---|---|
| RC2 | PMAY-G count and village coverage in the cross-scheme path filter `NOT is_placeholder`, which drops the 126 houses with no sanction date (1,70,981 / 5,120 instead of 1,71,107 / 5,121) | 20 | Code + **decision** | KI-233 |
| RC3 | Focus+ West Garo Hills / South West Garo Hills: the DB moves 51 beneficiaries WGH→SWGH and 31 SWGH→WGH (net 20) | 13 | **Data** (bot matches DB) | KI-065 |
| RC6 | No combined total and no combined district ranking: D-034 deliberately never adds different schemes' counts or money | 6 | **Design decision** | KI-236 |
| RC9 | The fixed comparison has no answer shape for "districts where A > B", "second after X", "top 3 districts", "rank …", "which of … / which reaches the most" | 6 | Code | KI-237 |
| RC4 | Focus+ villages: the DB has codes for rows the source left blank (KI-084), and the count includes 57 wards | 5 | Data + code | KI-084, KI-241 |
| RC7 | The question misses the fixed comparison path, and the model's SQL picks the wrong measure (FOCUS producer groups as beneficiaries, PMAY-G sanctioned amount as disbursed, raw rupees) | 4 | Code | KI-238 |
| RC5 | "Excluding X" / "leaving out X" is read as naming X | 4 | Code | KI-234 |
| RC1d | MGNREGA WGH / SWGH differ from the source by 445 households (DB 2,61,666 / 1,64,976, source 2,61,221 / 1,65,421); state total matches | 4 | **Data** (INFERRED: LGD village remap) | KI-240 |
| RC8 | "the FOCUS scheme" and "FOCUS on coverage" match `_FOCUS_AS_NOUN` (the English word), so FOCUS is not pinned to Focus Legacy and drops out | 2 | Code | KI-235 |
| RC10 | The scheme-money ranking reads `v_cross_scheme_money_district_year`, which rounds each district-year before summing (MGNREGA ₹3,628.68 vs ₹3,628.67; PMAY-G ₹2,185.24 vs ₹2,185.26) | 1 | Code | KI-239 |
| RC1 | MGNREGA households latest FY only | 3 (now pass) + part of 11 | Code — **fixed locally, not deployed** | KI-242 |

### RC2 — PMAY-G "1,70,981 vs 1,71,107" (KI-233)

**Evidence (DB, VERIFIED):**
- `curated.v_pmay` has 1,71,107 rows.
- 126 of them have `is_placeholder = true`. These are exactly the rows with no `sanction_date`, no `year_key`
  and `sanctioned_amount = 0`. Their `amount_released` is 0, so money is unaffected.
- District gaps: Ri Bhoi 40, East Khasi Hills 31, West Jaintia Hills 21, South Garo Hills 11, North Garo Hills 10,
  South West Garo Hills 9, West Garo Hills 4.
- Villages: 5,121 with the placeholder rows, 5,120 without.

**Code:** `app/pipeline.py`:
- `_XS_MEASURES[("PMAY-G","count")]` uses `COUNT(*)` with `NOT is_placeholder`;
- `_xs_query` coverage adds `NOT is_placeholder` for PMAY-G.

**Why it is a decision, not a plain bug — DOCUMENTATION CONFLICT, NEEDS VERIFICATION:**
- **KI-127 (decided 2026-10-03)** says PMAY-G *beneficiaries* = every record (state 1,71,107, Ri Bhoi 17,565). The
  cross-scheme path never followed that rule; it counts "houses sanctioned".
- **On 2026-10-15** the home-page PMAY-G card was set, at the user's request, to "Beneficiaries 1,70,981"
  (households = houses with a non-zero sanction).
- The UAT expects 1,71,107 for every PMAY-G beneficiary figure.

**Pick one rule:**
- (a) 1,71,107 everywhere (KI-127 and the testers): change the two `_XS_` entries and the card. This fixes RC2 in
  20 cases.
- (b) 1,70,981 everywhere: the testers' expected figures must change, and KI-127 is superseded.

### RC3 — Focus+ West Garo Hills 35,039 vs 35,059 (KI-065, data-side)

- `district_name_raw` vs `lgd_district` in `curated.v_focus_plus`: 51 beneficiaries are raw "West Garo Hills" but
  stored as South West Garo Hills; 31 are the reverse.
- Net result: WGH 35,039 (source 35,059) and SWGH 15,294 (source 15,274). Amounts: WGH ₹39.13 Cr (source ₹39.16 Cr),
  SWGH ₹16.68 Cr (source ₹16.64 Cr).
- The bot reports the DB faithfully. This needs the ingestion team (`docs/Focus_Plus_DB_Issues.md`, KI-065: 144 Dalu
  rows and 31 Betasing / Damalgre / Zikzak rows).
- **No code change is right here:** the app must not re-map districts.
- Four cases fail **only** because of this: T02.3, T06.3, T16.1, T17.2.

### RC4 — Focus+ villages 3,513 vs 3,220 (KI-084 + KI-241)

- **DB:** 3,523 distinct `village_code`. Of these, 10 are 'Unresolved' (excluded), 57 are wards and 3,456 are
  villages. 3,456 + 57 = 3,513 is what the bot shows.
- **Source:** 3,220 distinct LGD codes. Ingestion gave a code to the 102,923 rows the source left blank (KI-084,
  awaiting the mapping-rule confirmation), so even without wards the DB has 3,456.
- **Mylliem:** 48 = 41 villages + 7 wards (source 27). **Dadenggiri:** 130, no wards (source 80).
- **Code part (KI-241):** a "villages" coverage count includes wards (`entity_type = 'Ward'`); `_xs_query`
  excludes only 'Unresolved'.
- **Data part:** report to ingestion. The figure will not match the source until the mapping rule is settled.

### RC5 — exclusions ignored (KI-234)

`_named_schemes()` returns every scheme name in the text. Nothing reads "excluding", "leaving out", "except" or
"other than".

| Case | What happens |
|---|---|
| T07.2 | "Leaving out MGNREGA and CM-ELEVATE" → compares exactly those two |
| T15.3 | "Excluding MGNREGA … across PMAY-G, Focus+ and FOCUS" → MGNREGA is included |
| T11.2 | "excluding CM-ELEVATE" → the only scheme named is CM Elevate → classified KNOWLEDGE → "I don't have that detail in the CM Elevate reference material" |
| T12.2 | "Leaving out CM-ELEVATE" → the all-scheme money ranking, CM Elevate Legacy and NRLM included |

T13.2 ("Excluding CM-ELEVATE, which scheme reaches the most villages?") passed only because the bot asked a
clarifying question, which the UAT accepts.

### RC6 — combined totals and combined district ranking (KI-236, design)

- **What D-034 does:** it prints one figure per scheme and states they are "never added together". Households,
  houses, farmers and PG memberships are different units, and so are expenditure, releases and remittances.
- **What the testers expect:**
  - a combined beneficiary total (18,73,378, T01.1);
  - a combined amount (₹2,356.01 Cr, T04.2);
  - a district ranking by the sum of all four schemes (T15.1, T15.2, T15.3).
- **Decision needed:** keep refusing, or add a sum that carries a "different units" caveat.

### RC7 — model-path answers pick the wrong measure (KI-238)

These questions skip `_cross_scheme_compare_plan`:
- a named FY (T01.3);
- no count / money / overview word: "how are … doing" (T08.2), "which is bigger" (T18.3);
- only one scheme named (T13.3).

The model's SQL then goes wrong:

| Case | What the model's SQL did |
|---|---|
| T01.3 | Focus Legacy `COUNT(DISTINCT pg_id)` = 8,806 called "beneficiaries" (members 37,018) |
| T08.2 | Focus Legacy PG count 2,701 as beneficiaries; PMAY-G `SUM(sanctioned_amount)` (₹430.86 Cr) labelled "amount disbursed" (released ₹425.01 Cr); PMAY-G 33,143 (placeholder filter); raw rupees, no crore |
| T18.3 | Compares Focus+ people with FOCUS **PG** counts, and the composer says "Focus+ bigger in 12 districts, Focus Legacy in 3" (15 districts; its own rows give 9 vs 3). The faithfulness check did not catch a wrong count of rows |
| T13.3 | `HAVING COUNT(*) < 100` on payments → only the 1 no-district row; expected the four low districts (283, 404, 419, 1,221) |

### RC8 — FOCUS dropped (KI-235)

`_pin_bare_focus` (D-033) skips any "focus" covered by `_FOCUS_AS_NOUN`:
- "the FOCUS scheme" matches `the\s+focus`;
- "FOCUS on coverage" matches `focus … on`.

Both are scheme names here, because another scheme (Focus+) is listed beside them. Only 3 schemes are compared in
T01.2 and T20.2.

### RC9 — answer shapes the fixed comparison lacks (KI-237)

`_cross_scheme_compare_answer` produces only:
- per-scheme totals;
- each scheme's highest and lowest district;
- a single leader per district.

What is missing, by case:

| Case | Missing shape |
|---|---|
| T03.3 | "in which districts does A have more than B" (a pairwise list per district) |
| T18.2 | "second after MGNREGA" |
| T15.2 | "Top 3 districts" matches neither `_XS_BY_DISTRICT_CUE` nor `_XS_TOP_DISTRICT_CUE`, so no districts are returned at all |
| T07.3, T13.1, T20.2 | The rank cue misses "which reaches the most", "Which of … widest" and "Rank …", so the answer gives figures but **no winner and no order**. The testers saw the winner on the deployed version; this is a regression in the answer text |

### RC10 — T12.2's different amounts (KI-239)

- `_cross_scheme_money_answer` uses `_CROSS_SCHEME_MONEY_SQL`, which sums `amount_crore` from
  `v_cross_scheme_money_district_year`.
- The view does `ROUND(SUM(...), 2)` per district-year (48 MGNREGA groups, 83 PMAY groups). The sum of the rounded
  values drifts:
  - MGNREGA ₹3,628.68 Cr (true ₹3,628.6657);
  - PMAY-G ₹2,185.24 Cr (true ₹2,185.26).
- The fixed comparison (D-034) reads the base views and is exact, so the two paths disagree.
- **Fix:** read the base views in the ranking path too. The view belongs to ingestion; do not change it.

### RC1 / RC1d — MGNREGA

- **RC1, fixed locally (2026-10-15):** state 14,94,437; Ri Bhoi 1,12,602; North Garo Hills 1,36,418; West Khasi Hills
  62,655; Eastern West Khasi Hills 50,391. All match the testers.
- **Still open in RC1:** the LLM prompt's FAMILY C rule in `schema_context.py` still pins the latest year. A
  cross-scheme question that misses the fixed path would still give 3,65,556.
- **RC1d, data-side (KI-240):** DB West Garo Hills 2,61,666 and South West Garo Hills 1,64,976. The testers' source
  gives 2,61,221 / 1,65,421 (±445), the same kind of WGH / SWGH remap as Focus+. INFERRED: the exact villages need
  the source file to be compared with `curated.v_employment` by village.

## 3. Decisions needed from the product owner

1. **PMAY-G beneficiaries:** 1,71,107 (every record, KI-127, the testers) or 1,70,981 (non-zero sanction, the
   2026-10-15 card)? This one answer settles 20 of the 36 remaining failures.
2. **Combined totals:** may the bot add different schemes' counts and money (with a caveat), and rank districts by
   the sum? (6 cases.)

## 4. Recommended fixes (code — none made in this session)

| # | Fix | Where | Cases it fixes |
|---|---|---|---|
| 1 | Deploy the current tree (MGNREGA all years) | VM | T14.2, T14.3, T19.3, plus the MGNREGA part of 11 more |
| 2 | Apply decision 1 to `_XS_MEASURES` PMAY-G count and to the coverage filter | `pipeline.py` | 20 (alone: T08.1, T14.1, T16.2, T19.1) |
| 3 | Exclusion parser: drop schemes after "excluding / leaving out / except / other than / apart from"; with none left named, use the remaining catalogue | `_named_schemes` callers, `_cross_scheme_compare_plan`, `_cross_scheme_money_answer` | T07.2, T11.2, T12.2, T15.3 |
| 4 | Do not treat "the FOCUS scheme" / "FOCUS on" as the noun when another scheme is listed beside it | `_pin_bare_focus` / `_FOCUS_AS_NOUN` | T01.2, T20.2 |
| 5 | Wider plan cues: "doing", "bigger / larger / more than", "rank", "which of", "reaches the most", "top N districts"; allow a named FY when every named scheme holds it | `_XS_*_CUE`, `_cross_scheme_compare_plan` | T01.3, T08.2, T18.3, T15.2, T07.3, T13.1, T20.2 |
| 6 | New answer shapes: pairwise A > B by district, Nth place by district, top-N districts | `_cross_scheme_compare_answer` | T03.3, T18.2, T15.x |
| 7 | Combined totals and ranking, if decision 2 allows | same | T01.1, T04.2, T15.1–15.3 |
| 8 | Money ranking from the base views, not the rounded view | `_CROSS_SCHEME_MONEY_SQL` | T12.2 |
| 9 | Coverage "villages" excludes wards (or reports them separately) | `_xs_query` | part of T09.1, T13.1, T20.x |
| 10 | Deterministic guard: a Focus Legacy "beneficiaries" answer must use `SUM(no_of_pg_members)`, never `COUNT(DISTINCT pg_id)` (CLAUDE.md §5 pattern) | `execute_with_repair` | T01.3, T08.2, T18.3 |
| 11 | FAMILY C prompt rule: follow the all-years MGNREGA decision | `schema_context.py` | model-path MGNREGA counts |

## 5. Data-side items for the ingestion team (megh-ingestion)

- **KI-065:** Focus+ 82 beneficiaries placed in the other Garo Hills district (net 20).
- **KI-084:** Focus+ village codes assigned where the source had none (3,456 DB villages vs 3,220 source codes).
- **KI-240 (new):** MGNREGA WGH / SWGH households differ from the source by 445.

Even after every code fix, these cases will differ from the testers' source-file figures:
T02.3, T03.1, T03.3, T06.1–T06.3, T07.1, T08.2, T09.1, T09.2, T13.1, T16.1, T17.1–T17.3, T18.1, T20.1, T20.2.
For these, the testers should either accept the DB figure or wait for the data fix.
