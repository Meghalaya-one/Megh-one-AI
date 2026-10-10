# Current State

**Last updated:** 2026-10-10 (night), by the **measure-gap session** (KI-231: "person days under CM Elevate" now says the measure is MGNREGA's and offers it; `_measure_gap_answer` in `app/pipeline.py`, new `tests/test_measure_gap_direct.py`; code changed, uncommitted). Before that, the same evening: the **cross-scheme FIX session** — the 20 officer cross-scheme cases now **20/20 live (36/36 with typed variants and the UI screenshot question)**; D-034 deterministic cross-scheme comparison + KI-213/214/218..228 fixed; **code changed, uncommitted** (`app/pipeline.py`, new `tests/test_cross_scheme_compare.py`). Before that, the same day: the **cross-scheme LIVE retest session** (VPN up: the DB and bot columns finally run on the post-D-033 code; **4 PASS / 16 FAIL**; KI-212/215 verified fixed, KI-216 closed as not-a-defect, KI-213 magnitude corrected, KI-218..224 opened; **no code changed**; report `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md`). Before that: 2026-10-10, by the bare-Focus = Focus Legacy session (D-033; code changed, uncommitted). Before that: by the **cross-scheme use-case audit session** (the 20 cases in `Cross Scheme Test Cases.csv`; raw baseline for all six schemes computed, static pipeline audit done, **DB and bot columns NOT run — VPN down**; KI-212 to KI-216 opened; **no code changed**; report `docs/Cross_Scheme_UseCase_Test_Report_2026-10-10.md`). Before that: 2026-10-09, by the **use-cases-modal session** (KI-210 rows not clickable + KI-211 only 3 of 7 schemes listed; `web/ai_query.html` and the new `tests/test_ui_use_cases.py`; code changed, uncommitted). Before that: 2026-10-07, by the **NRLM use-case QA + fix session** (43 NRLM use cases: 16/43 → **43/43**; KI-188 to KI-195 opened AND fixed, plus two verifier false positives; code changed, uncommitted). Before that: 2026-10-06, by the Focus Legacy duplicate-groups session (KI-187, D-032 confirmed 2026-10-07 = paid more than once; code changed, uncommitted). Earlier the same day: by the CM Elevate OFF-009 all-pairs re-test and KI-182 fix session (code changed, uncommitted). Before that: 2026-10-03, by the Focus Legacy FY-comparison report session (KI-183; code changed, uncommitted). Before that: 2026-09-29 (early morning), by the conversational scheme-swap session (KI-180; code changed, uncommitted). Before that: 2026-09-29 (late night), by the CM Elevate Legacy use-case re-test session (no code changed; KI-166..168 opened). Before that: 2026-09-29 (night), by the Focus Legacy all-levels fix session (KI-020, KI-145..165, D-031; code changed, uncommitted). Before that: 2026-09-29, by the context-relevance / semantic-contract session (code changed, uncommitted; D-030, KI-030/032/034 and KI-130 to KI-135; **offline-verified only**, VPN down). Before that: 2026-09-28 (evening), by the CM Elevate all-blocks / all-villages fix session (code changed in `app/pipeline.py`, tests; uncommitted). Before that: 2026-09-28, by the PMAY-G fix session (code changed, uncommitted; D-029, KI-089 to KI-097). Before that: 2026-09-27 night, by the Focus Plus all-blocks / all-villages session (code changed in `app/pipeline.py`, tests in `tests/test_focusplus_usecase_fixes.py` and `tests/test_mgnrega_usecase_fixes.py`; **uncommitted**). Earlier the same day: the CM Elevate use-case QA **and fix** session (code changed in `app/pipeline.py` and `app/schema_context.py`, plus a new test file; **uncommitted**). Before that, the same day: the Focus Plus use-case QA **and fix** session (code changed in `app/pipeline.py` and `app/routers/query.py`, **uncommitted**, on top of the uncommitted 2026-09-26 MGNREGA and context work).
**Branch / commit:** `main` @ `36b7427` (pushed to `origin/main`), plus the 2026-10-10 work committed on top (VERIFIED by `git status` on 2026-10-10).
`docs/HANDOFF.md` was restored on 2026-10-07 (it had been deleted in the working tree although CLAUDE.md
requires it every session) — conflict RESOLVED, VERIFIED.
Still outstanding on 2026-10-07: the QA evidence folders, 9 `.xlsx` test reports and
`docs/CM_Elevate_Legacy_DB_Issues.md` remain **deleted in the working tree and not committed**, because this
document and KNOWN_ISSUES.md still cite them as the evidence for specific KI numbers. Awaiting a decision on
whether that evidence is retired (update the citing docs) or was removed by accident (restore it).

## A measure another scheme owns, asked under a named scheme (2026-10-10, night) — KI-231
- "So what is the total person days in Meghalaya under CM Elevate?" (UI screenshot) used to end in "I understood the
  question but couldn't build a working query". Now `_measure_gap_answer` (`_answer_data`, after `classify_scheme`,
  before any model call) pauses with `measure-unavailable`: "CM Elevate doesn't record person-days — only MGNREGA
  does, so there is no person-days figure for CM Elevate for all of Meghalaya", the same question under MGNREGA as
  the first chip, then CM Elevate's own measures for the same scope. Same measure registry as the KI-180 swap check
  (MGNREGA person-days / job cards / wages / material / employment; PMAY-G houses). Only when the scheme is named,
  only for one scheme; "self-employment" under CM Elevate is not MGNREGA employment. VERIFIED live (chips answer
  27,264,054 person-days FY 2023-24 and 8,627 applications). pytest **1,879 passed, 2 skipped** (35 files, +22).
  Nothing else changed. Code changed, uncommitted.

## Bare "Focus" = Focus Legacy (2026-10-10) — D-033, KI-217 (closes KI-212)
- A question naming only "Focus" is rewritten to "Focus Legacy" before routing (`_pin_bare_focus`), on the DATA and
  KNOWLEDGE paths; the which-Focus question no longer fires. Focus Plus / Focus+ / noun use / group names untouched. VERIFIED
  live: "What is focus?" → Focus Legacy KB; "…disbursed under focus in FY 2024-25" → ₹11,49,90,000 (= DB); Focus Plus
  question unchanged; live context suite 43/43. CLAUDE.md §4 updated. Code changed, uncommitted.

## Cross-scheme use cases: FIXED — 20/20 live (2026-10-10) — D-034, KI-213..228

**All 20 officer cases (`Cross Scheme Test Cases.csv`) answer correctly live**, and so do 15 typed-scope
variants and the user's UI screenshot question — **36/36**, each figure checked against megh_db by an
independent verifier (its own SQL, not the app's). 19/20 answer in one turn; CROSS-9 asks one legitimate
block-or-village question. Report: `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md` ("AFTER FIXES").

**What changed (all `app/pipeline.py`):**
- **D-034 — deterministic cross-scheme comparison** (`_cross_scheme_compare_plan` / `_data` / `_answer`),
  run in `_answer_data` before the scheme / scope / year pauses: one parameter-bound query per scheme and
  measure, each scheme's own unit (MGNREGA households latest FY, PMAY-G houses, Focus Plus
  `COUNT(DISTINCT beneficiary_key)`, CM Elevate applicants, Focus Legacy memberships in groups, CM Elevate
  Legacy records, NRLM members), never a combined total, CM Elevate money "no money recorded", each
  scheme's data years stated. Narrow by design — MGNREGA+PMAY-G-only, a specific year, a single-scheme
  measure, a village / constituency / sub-scheme / tranche all keep the old path.
- KI-214 money-ranking vocabulary; KI-225 an all-years scope no longer moves CM-ELEVATE to Legacy; KI-219
  per-scheme years in the year pause; KI-222 DATA intent for comparisons.
- Guards for every other wording on the model path: `_cross_scheme_sql_issue` (KI-213/218/220/223/227),
  `_verifier_join_complaint_on_aggregates` (KI-221), `_range_claim_misstated` (KI-226),
  `_cross_unit_total_stated` (KI-220).

**Tests:** pytest **1,854 passed, 2 skipped** (34 files, +64 in `test_cross_scheme_compare.py`); plain
scripts **14/14** (`test_admin_level_collision.py` failed once only while the VPN was down — passes with
it up); live context suite **43/43, follow-ups 29/29, SQL 20/20**; 8 live control questions outside the
comparison keep their previous path and figures.

**Open, for the officers (not code):** CROSS-7 / 18 name no scheme, so all seven are compared and NRLM's
SHG members lead 8 districts; if they mean the five schemes of the other cases, the question should name
them. Focus Legacy has no person record, so its "beneficiaries" are stated as memberships in groups.

## Cross-scheme use cases: LIVE retest — 4 PASS / 16 FAIL (2026-10-10) — KI-218..224

> **Corrected the same day (typed-scope retest):** **5 PASS / 4 PARTIAL / 11 FAIL**, not 4/16. The first live run answered the year pause via the chip; typing the scope into the question (as officers do) skips the pause and works better — CROSS-1 passes that way. See §0 of the retest report. New: KI-225 (a typed year flips CM-ELEVATE to CM Elevate Legacy), KI-226 (composer misstates a max that was in the result), KI-227 (one UNION branch drops the district filter), KI-228 ("Focus Legacy beneficiaries" = 102,021 or 11,906 depending on phrasing). KI-221 is narrower: the verifier rejections cluster on the chip-resume path.

The 20 officer cases re-run **with the VPN up**, on the post-D-033 code. The DB and bot columns
the earlier audit could not produce are now done. **No code changed this session.**
Report: `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md`.

**4 PASS** (CROSS-3, 17, 18, 20) **/ 16 FAIL.** The distribution is the point: 9 failures are
*visible* (6 "I couldn't build a working query", 3 endless pause chains) and **7 are silent** —
4 confident wrong numbers and 3 data questions answered from the knowledge base.

**Resolved by this run:**
- **KI-212 / KI-215 FIXED** (D-033, verified live): all 8 bare-FOCUS cases pin to Focus Legacy;
  CROSS-17/20 now resolve all 5 schemes, CROSS-2 resolves 2 with the cross-scheme block loaded.
- **KI-216 CLOSED, not a defect:** `v_cross_scheme_money_district_year` holds only MGNREGA and
  PMAY, so `_CROSS_SCHEME_MONEY_SQL`'s separate Focus Plus branch is correct. The view's totals
  also reconcile with the raw figures (3,628.68 vs 3,628.67 cr; 2,185.24 vs 2,185.26 cr).
- **KI-213 corrected:** Focus Plus beneficiaries are **105,813** (`COUNT(DISTINCT
  beneficiary_key)`), not 12,527 — `member_id` exists only on the 12.5K cohort, the 93K cohort
  has none. Two wrong readings now: `COUNT(*)` +3.64x, `member_id` -8.45x.

**The worst result — KI-214 made concrete (now CRITICAL).** CROSS-12 answered *"Focus Plus
provided the highest total financial assistance with 1197392500.00"* (no unit) when **MGNREGA is
highest at 3,628.67 crore, 30x larger**. One missing vocabulary token (`assistance`) in
`_MONEY_SUPERLATIVE` meant the deterministic ranking never fired; LLM SQL then omitted the
MGNREGA and PMAY-G branches and `ORDER BY..LIMIT 1` turned a partial list into a superlative.

**New (KI-218..224):** the generator ignores the D-033 pin and tells the user data is missing
when it is present (218); the year pause offers a **union of 9 years no single scheme has**, hit
by 13 of 20 cases (219); the composer states a **cross-unit total** "across all four schemes"
when 7 were resolved and three schemes with money in that district were dropped (220); the 4B
verifier **rejects the prompt's own prescribed** aggregate-then-CROSS-JOIN as a "PROHIBITED
JOIN" — 9 of 11 repair attempts, and the sole cause of all 6 no-answer cases (221);
cross-scheme superlatives with no money word fall through to RAG and return encyclopaedia prose
(222); an un-aggregated UNION branch floods the answer with ~997 junk rows (223); three cases
dead-end in three pauses whose last one re-asks the scheme the question already named (224).

**The actionable pattern:** every PASS used the LONG (UNION ALL) shape; every no-answer case
attempted the WIDE (CROSS JOIN) shape that the prompt mandates for one-figure-per-scheme
questions. **The WIDE shape is effectively unusable today** — that is KI-221.

**Next, in priority order:** KI-214 (smallest change, fixes the worst answer) -> KI-221
(unblocks 6 cases) -> KI-220 -> KI-219 -> KI-222 -> KI-218/223/224/213. Each needs a regression
test on the real function, and `live_context_validation.py` re-run for anything touching
routing or pauses.

## Cross-scheme use cases: audited, 11 of 20 fail on code evidence (2026-10-10) — KI-212..216

The 20 officer cases in `Cross Scheme Test Cases.csv`, tested against the raw sources and the
pipeline's own routing code. **No code was changed this session.** Full report:
`docs/Cross_Scheme_UseCase_Test_Report_2026-10-10.md`.

**The run is incomplete, by design-of-circumstance.** The request was raw vs DB vs bot;
`10.48.242.4` was unreachable (ping 100% loss; 5432/6333/8000/8001 all time out) and that one
host serves Postgres, Qdrant **and** every model endpoint, so the DB and bot columns could not
be produced. What *was* possible needed no VPN: the raw baseline, and a static audit that
executes the real pipeline functions offline.

**Raw baseline (computed this session, aggregate-only for the two PII partitions):**
MGNREGA person-days **90,915,181** and expenditure **₹362,866.57 lakh**; PMAY-G **171,107
houses**, released **₹2,185.26 cr**; Focus Plus **385,671 payments** but only **12,527 unique
members**, ₹119.74 cr; Focus Legacy **14,569 payments / 11,906 groups / 102,021 memberships**,
₹51.01 cr; CM Elevate **8,627 applications, no money and no year**; CM Elevate Legacy **2,823
applicants**, ₹82.90 cr. Money ranking: MGNREGA 3,628.67 › PMAY-G 2,185.26 › Focus Plus 119.74
› CM Elevate Legacy 82.90 › Focus Legacy 51.01 › CM Elevate **not held**.

**11 of 20 fail on code evidence alone, and none would surface as a visible error:**
- **KI-212 (High)** — "Focus+ … and FOCUS" drops Focus Legacy **silently**: a Focus Plus match
  makes `_is_ambiguous_focus` early-return False, so the which-Focus pause never fires. 8 cases.
- **KI-213 (High)** — the Focus Plus beneficiary guard is disabled on every multi-scheme
  question by its own first line, leaving only the prose rule against reading `COUNT(*)` as
  beneficiaries: **385,671 vs 105,813, a 3.64x overstatement** (corrected by the live retest; 12,527
  was `member_id`, the 12.5K cohort only), which also **reorders the
  district ranking** (uneven 20x–65x fan-out). 8 cases.
- **KI-214 (Medium)** — the deterministic money ranking misses "financial **assistance**"
  (CROSS-12) and "**higher** … : A or B?" (CROSS-5), losing the verbatim "CM Elevate holds no
  money" statement on the very case that needs it.
- **KI-215 (Medium)** — CROSS-2 routes as a **one**-scheme set, so the `_CROSS_SCHEME` guidance
  block is omitted from the SQL prompt entirely.
- **KI-216 (High if confirmed)** — **UNKNOWN:** if `v_cross_scheme_money_district_year` already
  carries Focus Plus, `_CROSS_SCHEME_MONEY_SQL` **double-counts** it. One query settles it.

**The framing finding:** "beneficiary count" is not a defined quantity across these schemes.
One row is a village-year, a house, a payment, an application, a group payment or an SHG
depending on the scheme; CM Elevate has **no money column at all** (so CROSS-4/5/10/11/19/20 are
impossible as asked, not merely hard); Focus Legacy has **no person record of any kind** (so the
beneficiary cases must answer in groups or memberships); and **no two schemes share a full year
window**. A "pass" means the bot states the mismatch and gives each scheme's own correct figure
side by side — a single blended total is the wrong answer however well phrased.

**What the prompt gets right and must not be weakened:** the `_CROSS_SCHEME` block correctly
calls a CM Elevate money comparison "IMPOSSIBLE", warns that
`v_cross_scheme_village_coverage` has no Focus Plus and no CM Elevate column, and defines Focus
Plus beneficiaries as `COUNT(DISTINCT beneficiary_key)` — verified present for every
multi-scheme set.

**Next:** fix KI-212 and KI-214 first (10 cases re-test unchanged otherwise), settle KI-216 with
one query, then run the live DB and bot columns per §6 of the report.

## Use-cases modal: clickable, and all seven schemes (2026-10-09) — KI-210, KI-211

A UI-only session, no Python changed. Two defects in the **Use cases** modal of
`web/ai_query.html`, both user-reported.

**KI-211 — the modals listed 3 of the 7 schemes.** All three hand-maintained lists — `USE_CASES`
(Use cases), `GLOSSARY` (Glossary) and the sidebar `SAMPLE_CATEGORIES` — covered only MGNREGA,
PMAY-G and FOCUS+, the schemes that existed when they were written. Focus Legacy, CM Elevate,
CM Elevate Legacy and NRLM had no examples and no glossary terms at all. All three now
carry all seven — **34 rows in 8 sections** — with the questions taken from the officers' own
use-case files (`CM Elevate.csv`, `Focus +_Use_Cases.csv`, `PMAY-G.csv`, `NRLM_Use_Cases.xlsx`,
`Use_Cases_-_Focus.csv`, `Use_Cases_-_CM_Elevate_legacy.csv`) and the `[district]` / `[block]`
placeholders replaced by real places. The scheme rules are respected: no bare "Focus" or bare
"CM Elevate" (both are collisions that must never be guessed), CM Elevate gets COUNT questions
only (no money, no time dimension), and no NRLM row puts RF/CIF in a financial year (cumulative
and undated — that scheme's highest-risk wrong-number path).

**Verified by routing, not by eye:** all 34 questions go through the pipeline's own
`_shortcut_scheme`, and **34/34 resolve to the scheme of the section they are filed under**.

The **Glossary** went from 15 terms in 3 sections to **49 terms in 7**. Its definitions are taken
from `app/schema_context.py` `SCHEME_METRICS` and `docs/SCHEMES.md`, so it states what the
pipeline enforces — deliberately including the traps that produce a plausible wrong number:
Focus Legacy's three counting subjects (payments / groups / memberships are three different
numbers) and memberships-not-people; CM Elevate having no money and no time dimension at all;
CM Elevate Legacy's stored `total_disbursement` and its own 13 schemes; and NRLM's `COUNT(*)` =
groups with members as a COLUMN, plus RF/CIF being cumulative undated grants that must never sit
in a financial year or be called savings or loans. Every figure quoted was checked against its
source.

**KI-210 — clicking a row did nothing.**

A UI-only fix, no Python touched. The **Use cases** modal rendered its 14 example questions
with an inline `onclick="closeModal(); ask(${JSON.stringify(q)});"`. `JSON.stringify` emits a
real double quote, which terminates the `onclick="` attribute, so every row's handler was the
truncated, invalid `closeModal(); ask(` and **no row did anything when clicked**.

`web/ai_query.html` now puts the question in `data-ask="${escapeHTML(q)}"` and binds the click
in `bindModalAsk()` with `addEventListener`, after `openModal()` has inserted the rows — the
pattern `bindRichActions()` already used for rich-message actions, whose comment names this
exact failure. Rows are `role="button" tabindex="0"`, respond to Enter and Space, and reveal a
`→` on hover or focus so they read as clickable.

Verified in a real DOM against the **served** page (jsdom): the modal opens, **all 34 rows click
through to `ask()` with the exact question text and close the modal**, Enter works, and the same
DOM parses the old markup to `onclick = "closeModal(); ask("` with no handler bound. Both inline
`<script>` blocks pass `node --check`.

**Tests.** New `tests/test_ui_use_cases.py` (**52 tests**) pins the invariants, not the wording:
no inline `onclick`, the question in an escaped `data-ask`, rows focusable and keyboard-operable,
every scheme in `SCHEME_CATALOG` has its own section, every question routes to its section, no
bare "Focus", no CM Elevate money/year, no NRLM money-in-a-year, no unfilled placeholder. Proven
to bite by reintroducing each defect (inline handler → 3 fail; NRLM use-case section renamed
→ 8 fail; a Glossary section renamed → 3 fail).
Full run: pytest **1,777 passed, 2 skipped** (33 files; 1,725 before this suite) and plain
scripts **14/14**. The live context suite was not re-run — no routing, rewrite or state change.

**One thing found and deliberately left alone:** the sidebar chip list is **dead code**. Its
container `queriesContainer` exists nowhere in the page (also true in `HEAD`), so
`loadSampleQuestions()` returns at its guard and no chip renders. It was updated for consistency
and moved off its own fragile inline `onclick`, but reviving that sidebar is a separate decision.

## NRLM use-case QA and fixes (2026-10-07) — 16/43 → 43/43

NRLM was wired as the **seventh** scheme on 2026-10-06 (`docs/SCHEME_ONBOARD_NRLM.md`) but had
never been run live. This session ran the 43 use cases, fixed everything they found, and re-ran
them.

- **Round 1: 16 of 43 passed.** After the fixes: **43 / 43**, live, on the final code.
  Report with per-case proof images: `NRLM_UseCase_Test_Report_2026-10-07.xlsx` (repo root).
- **The data reconciles exactly.** 34 scalar metrics plus the district, block, year,
  constituency and village breakdowns agree between the raw `NRLM to share to BLH.csv` and
  `curated.v_nrlm` (40,629 rows in both). Every failure was pipeline behaviour. VERIFIED.
- **Eight defects, KI-188 to KI-195, all fixed the same day** — each as a deterministic guard,
  because the prose rules for the two money defects already existed and did not hold under
  sampling (CLAUDE.md §5):
  - **KI-188** (11 cases) a capped page of rows reported as the whole answer — Umling block
    answered "40" against a true **1,167**; a district comparison said "five districts /
    twenty-seven financial years" against **12 and 30**; the all-blocks CIF was understated by
    92%. Fixed in four places: repair a count question that lists rows, drop an unrequested
    LIMIT from an every-group question, re-count a list that is still cut short, and lead the
    deterministic answer with that total.
  - **KI-189** (6 cases) the token **"SHG" resolved to the district South Garo Hills** (alias
    `SGH`) and paused every NRLM question. Fixed with a scheme-vocabulary exclusion.
  - **KI-190** (3 cases) a rupee total stated with **no unit** ("…is 98.48" for ₹98.48 crore).
  - **KI-191** (2 cases) cumulative CIF/RF attributed to a financial year and presented as a
    trend — the scheme's documented highest-risk wrong-number path.
  - **KI-192** a one-block constituency paused for a narrowing that selects the same rows;
    **KI-193** a data question answered with a KB "not in the reference material" refusal;
    **KI-194** a true zero (NR-15) reported as missing data; **KI-195** a name search matching
    exactly instead of containing (found 2 of 20).
  - **KI-196** 12 raw rows contradict their own district LGD code; the DB is right. For the
    ingestion team — no code change.
- **Two SQL-verifier false positives** found while re-testing and fixed: it demanded
  `year_key` when NRLM's only year column is `formation_financial_year_short`, and it demanded
  an aggregate on a per-SHG money column although one `v_nrlm` row **is** one SHG. Each had
  killed a use case outright (UC33, UC30) by exhausting the repair budget.
- **KI-197 (user report, same day): NRLM was refused as "a scheme I don't cover", and the
  chip it offered looped for ever.** `_UNSUPPORTED_SCHEME` still listed NRLM's aliases from
  before onboarding, so "…for NRLM" was refused, the NRLM chip re-asked the same question,
  and it was refused again. Fixed: aliases removed; the refusal text and the three
  `edge.py` coverage replies now name all seven schemes; and
  `tests/test_supported_scheme_not_refused.py` (25 tests) fails if any alias of a loaded
  scheme is ever in that pattern again, or if any refusal chip can loop. The reported
  question now answers **517 SHGs in Chokpot block** (= DB = raw CSV).
- **KI-198 (user report, same day): a year chip the pipeline OFFERED was refused as out of
  range, looping for ever.** A loop sweep that follows chips like the UI found **8 of 58
  paths looping**, all NRLM. Each paused for a year, offered **FY 1984-85**, then said
  "data is available only for … 1984-85 …" and showed the list again. `_parse_year_key`
  only understood 2010-2039; NRLM is the first scheme with data before 2010. Fixed: an
  explicit `NNNN-NN` range now parses from 1900 on, while a bare four-digit number stays
  2010-2039 so "top 2000 villages" is still not a year. Also fixed in the same report:
  following those chips reached a genuine zero that was worded "the data doesn't cover"
  — `_nrlm_counted_zero_answer` now states the measured zero (KI-194's reasoning on the
  non-empty path). **Sweep after the fix: 258 paths, 0 loops, 0 dead ends.**
- **KI-199 to KI-208 (exhaustive all-blocks / all-constituencies / all-villages /
  all-SHGs run, 2026-10-07..08): nine more defects, all fixed.** 45,728 questions, each
  expected answer read from `curated.v_nrlm` AND cross-checked against the raw CSV.
  **Districts 12/12, blocks 56/56, constituencies 55/55 and villages 4,936/4,936 all
  pass on the final code**; the 40,629 SHG lookups are still running. Every defect was a
  silent wrong number or an endless thread, never a visible error:
  KI-199 a block literal (name or `*_lgd_code`) beside a resolved village_code zeroed the
  answer (23 SHGs -> 0; 21 of the first 30 village failures); KI-200 a village question
  naming its own block still asked which village, and looped; KI-201 the BLOCK name was
  resolved as the village (9 for a village holding 8); KI-202 the village scan backstop
  never ran for questions that SAY "village"; KI-203 an SHG question was answered from
  Focus Legacy; KI-204 a named SHG was then asked for a district and a year; KI-205 an
  EXACT village name was offered as ambiguous against a look-alike; KI-206 a block NAME in
  an integer code column killed the query (5 Ranikor villages, reproducible only under
  concurrency); KI-207 "&" in a village name truncated the scan; KI-208 a one-figure
  answer stated the ROW COUNT, not the figure ("There is 1 SHG" for a COUNT of 12,
  intermittent 3-of-5).
  **Four of the nine trace to one thing:** the LLM mention-extractor dropping or
  mis-slotting the village when a question names both a village and a block
  (village=None on 6 of 8 identical calls). Every fix is a deterministic backstop.
- **Tests:** pytest **1,709 passed, 2 skipped** over 32 files (1,498 baseline + 71 in `tests/test_nrlm_usecase_fixes.py`
  + 25 in `tests/test_supported_scheme_not_refused.py` + 36 in `tests/test_year_chip_no_loop.py`
  + 45 in `tests/test_village_block_narrowing.py` + 28 in `tests/test_nrlm_shg_lookup.py`); plain scripts **14/14**; NRLM use cases **43/43** live.

## Project status
- **Stage (INFERRED):** internal UAT with live QA passes per scheme.
- **Production deployment status:** UNKNOWN — NEEDS VERIFICATION. Not recorded in the repo. A deployed instance
  (`115.124.102.167:8300`, writing to the shared `megh_db` `app.*` schema) answered a question on 2026-10-03 that the
  repo code answers correctly in every reproduction (KI-183), so it is INFERRED to run different code or configuration.

## Completed functionality (VERIFIED in code)
- All six schemes wired end to end: MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy
  (added 2026-09-22) and CM Elevate Legacy (added 2026-09-24).
- **NL→SQL chain:**
  - the classifier, entity resolution and clarification gates;
  - 30B SQL generation, 9 deterministic guards and the 4B verifier;
  - up to 3 repairs;
  - the composer, with faithfulness guards.
- RAG over 10 SME docs plus tagged web docs, with scheme-scoped retrieval.
- **Deterministic answers:**
  - geo abbreviations;
  - scheme listing, comparison, recommendation and pick;
  - Focus Legacy PG-name lookups;
  - cross-scheme money ranking.
- Multi-turn context: follow-up rewrite, structured state, a summary, and Qdrant memory.
  Since 2026-09-26 the rewrite gets the previous turn as structured tiers instead of
  `answer[:300]`, with a provenance check on its output (AI_PIPELINE.md §5.1, D-022).
- A typed reply to the "which scheme?" / "which Focus?" pause resumes the paused question
  (§5.2).
- A `prompt_context` log line for every model prompt: tokens per section, budget, and
  request id (§5.3).
- Voice input with no-speech and prompt-echo guards (2026-09-25).
- Auth, multi-tenancy, admin console, history (pin, archive, rename, delete), audit, caches,
  health and metrics.

## Latest: complete architecture document (2026-10-05). Documentation only — no code changed.
- New `docs/COMPLETE_ARCHITECTURE.md`: the whole system in one document — runtime topology, repo
  layout, the full `/api/query` path, `_run_pipeline`'s numbered stages, `execute_with_repair`'s
  31 rewrites + ~20 reject-and-repair asserts + verifier ordering, `compose_response`'s
  faithfulness checks, the four conversation-state layers (D-022/023/024), entity resolution,
  RAG, security, capacity, deployment and the dependency graph. Reconciled against the source,
  not the docs; every count re-verified.
- Added to the CLAUDE.md §3 documentation map.
- Stale facts fixed on the way: `pipeline.py` is **15,214** lines, not "about 8,400" (CLAUDE.md §2
  and ARCHITECTURE.md §1 both updated). Verified unchanged: `ai_query.html` 4,164 lines,
  `.env.example` 94 keys, 26 pytest-style test files.
- Remaining conflicts are recorded in the new doc's §20.1, not silently fixed: the `pipeline.py`
  docstring still justifies the no-framework choice by "2 schemes"; `data/pmay/README.md` §9 still
  says PMAY is not in `megh_db`; the newer `SCHEMA_FOR_DEVELOPERS.md` the scheme READMEs cite is
  not in this repo.
- No tests run: no code changed.

## Latest: CM Elevate pending at a level = file_status 'Pending'; "unique" = distinct (2026-10-05, KI-186). Code changed, uncommitted. LIVE-VERIFIED.
- User screenshot: "unique applications pending at level 1 for all of Meghalaya" -> 8,372 (every level-1 file, COUNT(*)). Now 8,307 (`current_level = 'level1' AND file_status = 'Pending'`, COUNT(DISTINCT request_id)). Level 2 = 165, level 0 = 0. Plain "pending" stays On Hold (user's choice). Decision recorded in DECISIONS.md D-027 amendment 2026-10-05.
- Also fixed on the way: case-folded district / level literals, a 4B verifier false check-2, and a prompt-wording regression on plain "pending" (see KI-186, AI_PIPELINE.md).
- Live: 871/871 pending questions; OFF-009 sample 150/150; tests 1,433 pytest, 14/14 scripts. The 2026-10-01 OFF-018 report used the old definition.

## Latest: CM Elevate OFF-017 status distribution re-test (2026-10-05). No code changed.
- 192 questions (12 districts x all programmes + each of 15): raw = DB 192/192, bot data correct 192/192, current_file_status used 192/192. PASS 130, 'no matching records' 61, FAIL 1 — same as 2026-10-01.
- New: KI-184 (Sports & Wellness Centre named in full still asks "which scheme?", 12/12). Wording: KI-185. Report: `docs/CM_Elevate_OFF017_StatusDistribution_Retest_2026-10-05.xlsx`.

## Latest: CM Elevate — a named programme with 0 applicants is stated as 0 (2026-10-05, KI-182). Code changed, uncommitted. LIVE-VERIFIED.
- Question: "How many applicants are there in [district] under [program 1] and [program 2]?", tested on every ordered
  pair: 12 districts x 15 x 14 = 2,520 questions. Raw (`CM_Elevate_AllSchemes_20260930_full.xlsx`) = DB in 2,520/2,520.
- Before the fix: 1,131 FAIL. When one programme has 0 applicants in the district, GROUP BY scheme_name returns one row and
  the answer named only the other programme ("Meghalaya Warehouse Scheme: 10 applicants.") or merged both under one figure.
- Fix (answer text only, `app/pipeline.py`): `_cme_multi_scheme_total` also runs on a one-row result, and the new
  `_cme_requested_zero_programmes` adds each programme named in the SQL's `scheme_name IN (…)` with no row as
  "<P>: 0 (none recorded)". It does this only when absence provably means 0 (AI_PIPELINE.md §CM Elevate guarantees).
- After the fix: **2,520 / 2,520 PASS** (the both-zero follow-up below included). SQL data correct 2,520/2,520. Pairs where both
  programmes are non-zero are unchanged (1,094/1,094).
- Both programmes 0 (user screenshot, same day): the result is empty, and the answer was "I couldn't find any matching
  records for South West Khasi Hills, …". Now `compose_response` answers a CM Elevate empty result with
  `_cme_zero_programmes_answer`: "There are no applicants under the Agro Tourism Villa Scheme or the Chief Minister's
  Green Taxi Scheme in South West Khasi Hills — the data records 0 for each." This applies only when the place is
  confirmed by the app's own bound lookup. Live: 282/282, and the screenshot question is answered as above.
- One programme 0 (second user screenshot, same day): the appended "Cinema Theatre: 0 (none recorded); …" line read as a stub. When the only other filters are places and at most one status the wording can name (On Hold, Valid, level 0-2), `_cme_zero_breakdown_answer` rebuilds the whole answer: "There are 2 applicants in West Garo Hills across the 2 programmes you asked about:" + one line per programme in the question's order (the 0 one "0 — no applicants recorded in West Garo Hills") + where the applicants are. Any other filter keeps the appended line, so no filter is dropped from the wording.
  Live: all 1,426 zero cases (1,144 one-zero + 282 both-zero) 1,426/1,426.
- Tests: 1,405 pytest (+28), 14/14 scripts. Reports: `docs/CM_Elevate_OFF009_AllPermutations_Retest_2026-10-05.xlsx`
  (before) and `docs/CM_Elevate_OFF009_AfterFix_KI182_2026-10-05.xlsx` (after).

## Latest: typed replies resume every chip pause (2026-09-29, morning, KI-181). Code changed, uncommitted. LIVE-VERIFIED.
- Before: only the area/year/entity/ranking pauses and the which-scheme pauses were remembered. Any other pause with chips
  (measure gap, year out of range, tranche, region, AC part, Sericulture, CM sub-scheme group, stated amount, …) forgot its question,
  so a typed "houses sanctioned" or "2022-23" was read against the last answered turn.
- Now the router remembers every pause with options; a typed reply picks ONE option by its words, an ordinal, or "yes"
  (`_resume_option_pause`), and an unmatched reply stays on the paused scheme (`_paused_thread_antecedent`). AI_PIPELINE §5.2.
- Tests: pytest 1,371 passed + the 2 parallel-session failures; scripts 14/14; live context 43/43.

## Latest: conversational scheme swap — "give me for pmay" (2026-09-29, early morning). Code changed, uncommitted. LIVE-VERIFIED.
- Reported: "Total MGNREGA person-days in 2023-24" then "give me for pmay" returned a PMAY-G scheme description.
- Now a request-verb swap continues the previous DATA question on the new scheme (`_SCHEME_SWAP_FOLLOWUP`, `_SUBSTITUTION_CUE`).
  When the carried measure does not exist in the new scheme (person-days in PMAY-G, houses in MGNREGA), the bot says so and
  offers the new scheme's own measures for the same place/year as chips (`_swap_measure_gap`, rule `swap-measure-unavailable`),
  instead of the old "couldn't build a working query". Questions about a scheme ("tell me about pmay") are unchanged.
- Tests: pytest 1,338 passed + 2 failing tests owned by a parallel CM Elevate Legacy session (see TESTING.md); scripts 14/14; live context 43/43.

## Latest: CM Elevate Legacy fixes + all districts / blocks / villages (2026-09-29, late night). Code changed, uncommitted. LIVE-VERIFIED.
- **Use cases 36/36** on 2 of 2 fresh runs (were 33/36: TC-13 LIMIT 10, TC-14 COUNT(*), TC-34 derived count).
- **All levels 2,614/2,614** vs DB AND raw (12 districts, 59 blocks, 1,051 villages, 55 ACs; 7 question shapes).
  Round 1 found KI-169..181 (twin-village chip loop / ranking, exact small amounts, spelling, ward literal,
  unnamed breakdown figures, Tura MB refused, invented subsidy split, garbled village lists, Garo village,
  "village" kept in the name, verifier false positive after a chip, unselected GROUP BY) — all fixed.
- Design: deterministic SQL guards in `execute_with_repair` + `_cm_legacy_answer_guarantees` after the composer
  (AI_PIPELINE §2.9a). KI-179 (level word dropped from an extracted village name) touches ALL schemes, guarded by
  an exact-name check.
- Tests: pytest 1,309+ (26 files; `test_cm_elevate_legacy.py` 131), scripts 14/14, live context 41/43 then the
  2 failing checks (scenario G, a transient app.conversations save-failed) 5/5 on 2 re-runs.

## Earlier the same night: CM Elevate Legacy use-case re-test — 33/36. No code changed.
- All 36 use cases (`Use_Cases_-_CM_Elevate_legacy.csv`) run live through `pipeline.answer_question`, 2 full runs + 3 repeats of the
  failures; every figure checked against `curated.v_cm_elevate_disbursement` AND the raw file `Cm Elevate legacy to share to BLH (1).csv`.
- **DB = raw** row for row (2,823 = 2,823 on source id; all money, scheme, FY, district, block, AC, lender, desanction fields identical).
- **Fails (AI layer, all reproducible):** TC-14 sanctioned 2,823 vs 2,820 (KI-166, SQL `COUNT(*)`); TC-34 "20 villages with 1 record" vs 24
  (KI-167, composer); TC-13 `LIMIT 10` → "down to 33" vs Motorcaravan 1 (KI-168). TC-01..10 knowledge answers all correct.
- Report `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29.xlsx` (+ `docs/CM_Elevate_Legacy_UseCase_Evidence_2026-09-29/`, 37 PNGs).

## Latest: reported "what is focus" conversation (2026-09-29, later), uncommitted, LIVE-VERIFIED
- **Report:** after a CM Elevate Legacy turn, "what is focus" was answered with CM Elevate material;
  "give me beneficiaries" paused generically; the user had to retype the whole question.
- **Root causes (KI-136 to KI-144):** a bare "Focus" went to the model rewrite, which read it as a noun;
  the rewrite carried a data turn's year / place into how-it-works questions; the rewrite could swap
  the user's metric; the old scheme's filters fed follow-ups after a knowledge answer on another
  scheme; intent was read from the rewrite, not the typed words; a paused follow-up was remembered as
  a fragment; "all of them combined" kept the last year; a place-only change after knowledge answers
  lost the data thread.
- **Tests:** pytest **1,185**; scripts **14/14**; live context suite **43/43**; live battery S1–S8 all
  correct (it also live-verified the first pass, KI-130 to KI-135).

## Earlier the same day: context relevance + semantic contract (2026-09-29), uncommitted (offline at the time; live-verified later that day)
- **Reported conversation** (after "beneficiaries in focus+ across all financial years"): "who is
  harshit" / "he is my collik remember" answered as Focus Plus; "now give me five thousand loan…"
  became a Focus Plus DATA pause; "…Focus Plus … for a loan of five thousand…" ran SQL without the
  amount; WHK dropped from a comparison. Reproduced offline with the real `_run_pipeline`.
- **Fixed (D-030):** continuation gate `context_policy.continuation_signals` (KI-130); edge
  `personal_request` (KI-131); Focus Plus stated-amount filter + guard + pause (KI-132); WHK
  near-miss chips (KI-133); bare "which one?" asks (KI-134); village name search (KI-135); generic
  `_resolved_scope_missing` SQL guard (KI-034); CLEAR applied to committed state (KI-032);
  all-years carried (KI-030); `pipeline_decision` log lines.
- **Tests:** pytest **1,163** (26 files; +96 in `test_context_relevance_and_contract.py`);
  scripts **14/14**. **Not run:** `tests/live_context_validation.py` and every live check — the
  DB and gateway (`10.48.242.4`) were unreachable. Run them first when the VPN is up.
- "give me same for mgnrega" already worked (KI-039); it is re-tested and unchanged.

## Latest: CM Elevate all blocks / all villages (2026-09-28)
- **CM Elevate all districts / blocks / villages (2026-09-28): 7,364 / 7,364 on the final code** (round 1: 4,132 / 4,208). Every district (12), block (66) and village (2,087 x 3 phrasings) x the use-case question types, each figure checked against megh_db and the raw workbook. Report `docs/CM_Elevate_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx` (+ `_Evidence_2026-09-28/`).
  - Fixed: KI-074 decided (pending = On Hold only), KI-076 (intent cue), KI-106 to KI-120 (CM Elevate joined the village guards; urban bodies; literal / sector / programme-filter SQL guards; verifier false positives; twin-village chip; number words). All CM Elevate-gated except the literal- and FILTER-aware village WHERE rebuild (KI-108, also Focus Plus). pytest 1,053; context suite 43/43.

## Latest: PMAY-G beneficiaries include zero-sanction records (2026-10-03, KI-127 decided), uncommitted
- Beneficiary counts = every record; all other PMAY-G figures unchanged. Live-verified against raw + DB.
- pytest 1,373 passed; 2 failing tests belong to the CM Elevate Legacy work (KI-173 widened
  `_verifier_village_code_complaint_is_false`; two older tests still assert the old scope).

## Latest: PMAY-G plain year = calendar year (2026-09-29, KI-125 revised), uncommitted
- "during 2017" → calendar 2017 in the answer, table and SQL; FY reading as a one-line note. Explicit FY unchanged.

## Latest: PMAY-G result table and Sources (2026-09-29, KI-129), uncommitted
- Result table = place + asked figures; Sources no longer shows "TRUE". pytest 1,067; follow-ups 18/18; use cases 71/71.

## Latest: PMAY-G extra information removed (2026-09-28, KI-128), uncommitted
- Summaries = exactly the use-case figures; no side details on single figures; follow-ups answer only what was typed.
- Live 2,129/2,129 sample + use cases 71/71 + follow-ups 18/18; pytest 1,065.

## Latest: PMAY-G 21 Sep tester sheet recheck (2026-09-28), uncommitted
- All tester-named inputs correct vs DB and raw; 192/192 live checks + use cases 71/71.
- Fixed KI-125: a bare year was dropped by the facts path (answered all years). Now FY reading + calendar-year line.
- Open product decisions: KI-126 (one combined village + year question), KI-127 (count zero-sanction placeholder
  records or not — the tester's 14 Sep and 21 Sep remarks conflict).

## Latest: PMAY-G full scenario test (2026-09-28): 17,331 / 17,331, uncommitted
- **Why:** the KI-098 miss showed the earlier village test used one phrasing only. This run covers every place type x 16
  question types, 3 village phrasings, every FY, dates, comparisons, model-path questions and follow-ups.
- **Found and fixed (KI-099 to KI-105):** 'Garo'/'Khasi' inside a village name triggered the hill-range pause; 'Old …'
  villages read as the Old House stage; ', X block' mid-sentence; a village named like its block; two-village comparisons;
  FY printed as a calendar year; model-path amounts in 2-decimal crore.
- **Result:** 17,331/17,331; follow-ups 18/18; use cases 71/71; context 43/43; pytest 1,005; scripts 14/14.
  Report `docs/PMAY_G_Full_Scenario_Test_Report_2026-09-28.xlsx`.

## Village named beside its block (2026-09-28, KI-098, user report), uncommitted
- **Report:** "…still to be released in NONGSOHRAM across all financial years, RI MULIANG block, WEST
  KHASI HILLS" answered with the whole block (₹33,84,000).
- **Fix (PMAY-G only):** `_pmay_village_beside_block`, called at the end of `resolve_entities`.
- **Live:** all 5,120 villages asked with their block + district tail **5,120/5,120** (before the fix, 216 of
  the first 927 such questions answered for the block); block questions unchanged (see TESTING).

## PMAY-G fixes (2026-09-28): 28 / 28 use cases, all blocks and all villages, uncommitted
- **Result:** use cases **28/28** (71/71 questions on 2 of 2 fresh live runs, identical answers; round 1 was 17/28); all blocks **224/224**; all villages **5,120/5,120** on the final code (round 1 4,393/5,120 → KI-096 fixed). Every figure checked against megh_db and the raw CSV (identical row for row).
- **Changed:** `app/pipeline.py` (PMAY-G facts path, model-path guards, village gate, date / typo /
  plural-blocks fixes), `app/entity_resolver.py`, `app/premise_check.py`, `app/schema_context.py`,
  `data/pmay/pmay_few_shot.yaml`, `data/pmay/pmay_entity_resolver.yaml`, tests. D-029; KI-089 to KI-097.
- **Regression:** pytest 993; scripts 14/14; live context 43/43; Focus Plus 60/60 + 300/300;
  MGNREGA 60/60 + 300/300.
- **Reports:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28_v2_after_fixes.xlsx`,
  `docs/PMAY_G_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx`.

## PMAY-G use-case QA, round 1 (2026-09-28): 17 / 28, before fixes
- **What:** the 28 PMAY-G use cases (`PMAY-G.csv`, PMAY-OFF-001 … 028) as 71 real questions, each
  run twice live, with every figure checked against megh_db and the raw
  `PMAY_FullyMapped_with_dates.csv`. The user said "Focus legacy", but named PMAY-G files, so PMAY-G was tested.
- **Result:** **17 / 28 test cases pass** (49 / 71 questions). The failures are 007, 009, 012,
  016, 018, 022, 023, 024, 025, 027 and 028.
- **DB vs raw:** identical row for row (171,107 rows, 13 fields, 0 mismatches).
- **Root causes (KNOWN_ISSUES KI-089 to KI-095, all fixed the same day — see above):**
  - per-year GROUP BY on summaries and comparisons (row dumps, mislabelled or invented figures);
  - required parts missing (remaining amount, difference, sanctioned amount);
  - a false "House Sanctioned stage" qualifier from the entity resolver;
  - utilisation as an average of ratios;
  - LIMIT 1 on "which has more";
  - a written-date premise misread;
  - village money rounded to 0.0x crore.
- **Report:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28.xlsx` (Summary, Test Results, Question
  Detail, DB vs Raw, Evidence); PNGs in `docs/PMAY_G_UseCase_Evidence_2026-09-28/`.
- **Fixed the same day** — see the section above.

## Latest: database-outage handling and data package (2026-09-28), uncommitted
- **KI-025 fixed (connection loss):** a megh_db outage mid-question now returns 503 "Couldn't
  reach the data service… retry". It no longer answers "not in the reference material" or spends
  SQL repairs. Changed: `app/db.py`, `app/pipeline.py`, `app/routers/query.py`. Slow-query
  timeouts are unchanged (504).
- **KI-065 / KI-084 (data):** not changeable by the chatbot (the DB belongs to the ingestion team,
  CLAUDE.md §6). A row-level package is handed over:
  `docs/Focus_Plus_Data_Corrections_for_Ingestion_2026-09-28.xlsx`. Awaiting their decision.
- **Tests:** pytest 939; scripts 14/14; live context 43/43; outage simulation passed; live
  204/204 blocks + 200/200 villages.

## Latest: Focus Plus all blocks and all villages (2026-09-27 night): 100% on the final code, uncommitted
- **Scope:** every block (51 × 4 questions) and every village or ward in `v_focus_plus`
  (3,513 × 2). Each figure was checked against megh_db and the raw CSV.
- **Result:** blocks **204/204**; villages **7,026/7,026**.
  - Before fixes: blocks 201/204, and villages 62/327 on the first sample.
  - Confirmation pass on the final code: 204/204 and 1,000/1,000.
  - MGNREGA regression: 448/448 and 300/300.
- **Report:** `docs/Focus_Plus_AllBlocks_AllVillages_Test_Report_2026-09-27.xlsx`, with evidence
  images.
- **Fixed (KI-079 to KI-088):**
  - the village guards now cover Focus Plus (`_village_scheme`, D-028);
  - `_focusplus_narrow_village`;
  - `_focusplus_pin_village_where` (place conditions, place literals, `geography_key` subquery);
  - twin-village "(LGD code)" chips;
  - the district-alias collision (BAGHMARA);
  - the "Nan" block;
  - BURMA;
  - the Focus Plus one-figure misquote rule;
  - roman numerals in names.
- **Data side:** DB village totals often exceed the raw file's code-only totals (KI-084, see
  `docs/Focus_Plus_DB_Issues.md`).
- **Tests:** pytest 936 (24 files); scripts 14/14; live context 43/43.

## Latest: CM Elevate use-case fixes (2026-09-27): 30 / 30 after fixes, uncommitted
- **Result:** 55 / 55 questions on 2 of 2 fresh live runs. Each figure was checked against
  megh_db `v_cm_elevate` and the raw `CM_Elevate_AllSchemes_20260927_full.xlsx` (DB = raw
  exactly).
  - Round 1, before the fixes: 23 / 30.
  - Report: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`.
- **Fixed, all CM Elevate-gated (D-027):**
  - KI-068: Unresolved rows dropped from programme totals;
  - KI-069: no per-programme split;
  - KI-070: pending at level 2 gave 0;
  - KI-071: no comparison difference;
  - KI-072: zero sectors vanished;
  - KI-073: garbled two-label prose;
  - KI-075: PRIME SEED paused on "which scheme?".
  - Found while re-testing: a plain "pending" became a level filter; approved/rejected went to
    the wrong column; a verifier false positive on `scheme_specific`.
- **KI-074 interim:** "pending" is still On Hold. A single-figure pending answer also states the
  file-status Pending count. **Product decision still needed.**
- **Tests:** pytest 891 passed (858 + 33 new); scripts 14/14; live context suite 43/43.
- **Open:** KI-076, an intent-classifier flake for a question with no counting word (Low,
  pre-existing).
- The user said "Focus legacy" but named the CM Elevate files, so CM Elevate was tested.

## Latest: Focus Plus use-case fixes (2026-09-27): 30 / 30 after fixes, uncommitted
- **Result:** 39 / 39 questions on 2 of 2 fresh live runs, each figure checked against megh_db
  and the raw CSV. Report: `docs/Focus_Plus_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`.
- **Fixed:**
  - KI-060: shares;
  - KI-061: comparison difference;
  - KI-062: "1,0263", fixed for all schemes;
  - KI-063: bank-pause chips and typed resume;
  - KI-064: rupee format, the "amount raw" fallback (the SQL-literal fix applies to all schemes),
    and no "which area?" for a batch or tranche;
  - KI-066: a typed reply that pauses again kept only the bare reply, all schemes;
  - KI-067: the Focus Legacy bank `KeyError`.
- **Where:** `_focusplus_answer_guarantees` and helpers, `_fix_digit_grouping`,
  `_sql_literal_numbers`, `_bank_clarification`, `_needs_scope_clarification`,
  turn_context `resumed_question`, and router `pause_question`. Rationale is D-026.
- **Tests:** pytest 858 passed (826 + 32 new); scripts 14/14; live context suite 43/43.
- **Still open:** KI-065, data-side (175 Dalu ↔ SWGH rows), with the ingestion team.

## Focus Plus use cases, round 1 (2026-09-27): 19 / 30, before fixes
- **Inputs:** `Focus +_Use_Cases.csv` (FOCUS-001..030) and the raw `Focus Plus Master.csv`.
  - The user wrote "Focus legacy", but both files are Focus Plus, so Focus Plus was tested.
- **Method:** 39 concrete questions, placeholders filled with real values. Each went through the
  live `pipeline.answer_question` twice, clicking the chip a tester would pick. Every figure was
  checked against both megh_db (`v_focus_plus`) and the raw CSV.
- **Result:** **19 PASS / 11 FAIL** test cases (27 / 39 questions). Both runs gave the same
  verdicts.
  - **Failed:** FOCUS-008, 011, 012, 013, 014, 016 (percentages missing, KI-060); 026, 027, 028
    (no difference / higher district, KI-061); 021 (malformed "1,0263", KI-062); 029 (bank
    question lost after the no-chip clarification, KI-063).
  - In 10 of the 11 failures the numbers themselves are correct. The exception is 029, which gave
    no number at all.
- **Report:** `docs/Focus_Plus_UseCase_Test_Report_2026-09-27.xlsx`, with evidence PNGs in
  `docs/Focus_Plus_UseCase_Evidence_2026-09-27/`.
- **DB vs raw:** identical except 175 rows (Dalu ↔ SWGH) in a different district. See KI-065 and
  `docs/Focus_Plus_DB_Issues.md`.
- **Fixed since the testers' 14 Sep comments:**
  - no stray verification column (018, 019);
  - no stray FY column (010);
  - "female during 2025-26" answers (021a);
  - the Focus+ overall summary (030) is complete and correct.

## Calendar date read as FY fix (2026-09-27, KI-078; uncommitted)
- **Report:** "How many PMAY houses were sanctioned on 2017-11-28?" answered 504 (correct) but
  added "not the 11 or 28 figures assumed" and "applies to FY 2017-18".
- **Now:** "504 PMAY houses were sanctioned on 2017-11-28." A date is no longer back-filled as
  an FY, never yields premise figures, and satisfies the scope gate.
- **Checks:** live 4/4 probes; pytest 915; scripts 14/14.

## MGNREGA women share fix (2026-09-27, KI-077; uncommitted)
- **Report:** "What percentage of employment persons were women in ekh?" answered "0.00% … a
  genuine zero".
- **Cause:** the question named no year and the year gate ignored percentage questions, so the
  SQL model took FY 2025-26, whose women column is unrecorded at source.
- **Now:**
  - the bot asks the FY, offering only the years that carry women data;
  - the answer states the years it covers: EKH 77.70% in FY 2024-25, or 74.90% for FY 2022-23
    to 2024-25;
  - FY 2025-26 is answered "not recorded", never 0.
- **Checks:** live 8/8. pytest 897, scripts 14/14, live context 43/43.

## Latest QA: MGNREGA all blocks and all villages (2026-09-26 night): 100% on the final code, uncommitted
- **Scope:** FY 2024-25.
  - All 56 blocks × 8 questions = 448.
  - All 6,425 villages × 2 questions (person-days, total expenditure) = 12,850.
  - Every expected figure comes from megh_db and, independently, from the raw CSVs.
- **Result on the final code: 13,298 / 13,298.** VERIFIED as follows:
  - the full village pass gave 12,846 / 12,850;
  - the 4 misses were 3 short-name villages (fixed afterwards) and one gateway 502;
  - those, plus every case that went through a "village" chip, were re-run on the final code:
    590 / 590 (2 retried after a VPN drop);
  - blocks re-run on the final code: 448 / 448.
- **Report:** `docs/MGNREGA_AllBlocks_AllVillages_Test_Report_2026-09-26.xlsx`, with before/after
  evidence in `docs/MGNREGA_AllBlocks_AllVillages_Evidence_2026-09-26/`.
- **Defects found and fixed:** KI-049 to KI-059, all MGNREGA-only. The worst was a village chosen
  from the bot's own list answered with its whole BLOCK's total (KI-049). Other defects:
  - village-chip loops;
  - bare block names;
  - names with "&", "INCL", "BLOCK", "India" or "Dairy" in them;
  - a stray district filter and an extra village code in the SQL;
  - "null" wording.
- **Data side (the data team's):** 16 villages are in a different block in the DB than in the raw
  file, which affects 68 of the 448 block figures. Village figures are identical. See
  `docs/MGNREGA_DB_Issues.md`.
- **Operational lesson:** bulk runs must cap the DB pool. On 2026-09-26 VPN drops plus a 30-connection
  pool exhausted `megh_db` (max 100, shared), and the result was "too many clients already". See
  TESTING.md, "Bulk live runs".

## MGNREGA use cases (2026-09-26): 30/30 after fixes
- **Round 1 (no code changed):** 16 PASS / 14 FAIL of the 30 use cases in
  `Test_Case_Results_21st_Sep_26_MGNREGA (1).csv`. The testers had 17 / 12 / 1 On-Hold on
  21 Sep.
  - Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26.xlsx`, with evidence in
    `docs/MGNREGA_UseCase_Evidence_2026-09-26/`.
- **Round 2 (after the fixes, same day): 30/30 PASS.** All 43 concrete queries were correct on
  two fresh full live runs, with every figure checked against megh_db and the raw CSVs. VERIFIED.
  - Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26_v2_after_fixes.xlsx`, with evidence in
    `docs/MGNREGA_UseCase_Evidence_2026-09-26_after_fixes/`.
- **Fixes:** KI-041 to KI-048, plus three answer-wording and unit fixes. All are in
  `app/pipeline.py` and `app/schema_context.py` (MGNREGA rules 8–10), and **every change is
  gated on MGNREGA**. Design choices are in D-025.
- **Tests:** `tests/test_mgnrega_usecase_fixes.py` (41 at that point; 78 after the all-villages fixes). pytest 789 passed at that point. Scripts 14/14. Live
  context suite 43/43.
- **DB vs raw:** rows and all statewide totals are identical. The one mapping difference is village
  GENAPARA (`docs/MGNREGA_DB_Issues.md`, owned by the data team). No failure was caused by the DB.
- **Known limitation, left on purpose:** the negated-level chip bug (KI-041) still affects the
  other five schemes. The user asked that they not be changed.

## Focus Legacy duplicate producer groups (2026-10-07) — KI-187, D-032
- Product owner's definition (confirmed 2026-10-07): a duplicate producer group is one PAID MORE THAN ONCE — "Yes — 2,655 of
  the 11,906 … (2,647 paid twice, 8 paid 3 times) … 5,318 of the 14,569 payment records", no year split; a year / place in
  the question narrows the rows. The 2026-10-06 same-year-only count (7) was withdrawn.
- **Not yet on the deployed server (VERIFIED 2026-10-06):** `115.124.102.167:8300` still gave the old "0 duplicate payment
  records" answer. Everything after commit `f38ea1b` (2026-09-29) is uncommitted and so not deployed. The local `:8300`
  (127.0.0.1 only, `--reload`) runs the current code and is not what that address serves.

## Focus Legacy FY comparison report (2026-10-03) — KI-183
- Reported on the deployed server: "Compare total remittances between financial years 2023-24 and 2024-25 for Focus
  Legacy" → "couldn't build a working query" (conv 51495). Not reproducible on the repo code in 20 in-process runs, the
  exact 4-turn conversation through the real `/api/query` endpoint as the same user, or the `f38ea1b` / `7064ab6` builds.
  The VM's code/config is UNKNOWN — NEEDS VERIFICATION; redeploy from this repo and re-test.
- Fixed in the same flow: the composer's correct comparison was discarded by the hedge guard (the FY 2023-24 gap sentence
  matched `_HEDGE_RE`) — now the answer reads "₹14.18 crore in FY 2022-23 and ₹11.50 crore in FY 2024-25 … no data for FY
  2023-24" (5/5 live).

## Focus Legacy all blocks / villages / ACs / PGs (2026-09-29, night) — VERIFIED, code changed, uncommitted
- **32,560/32,560** questions correct vs the live DB and the raw file (latest run of each): 56 blocks x3,
  12 district breakdowns, 55 ACs x3, 3,384 villages x2 (list + amount), 11,906 PGs x2 (exists + members),
  1,635 older raw spellings. Final-code regression pass: 5,215 questions (all block/AC, every question that
  ever failed, 4,300 random) 5,214/5,215, the last fixed (KI-165) and re-run.
- Use cases **28/28 on 3 of 3 runs** on the final code; live context suite **43/43**; pytest **1,265**;
  scripts **14/14**.
- Fixed: KI-020 (older PG spellings via `v_focus_legacy_pg_search`), KI-145..165 — breakdowns written from rows
  (D-031), month/rupee formatting, AC-alone no longer pins MGNREGA, AC drill-down per scheme, PG-name parsing
  (punctuation, place words, "_", non-ASCII, "Focus" in a name), edge refusing group/village names holding a
  state or country word, village phrase read from the text, twin villages and LGD chips, village chip pin +
  year chip, verifier false complaint, misspelt block literal, DATE_TRUNC timezone shift.
- Reports: `docs/Focus_Legacy_AllBlocks_Villages_ACs_PGs_Test_Report_2026-09-29.xlsx` (+ `_Evidence_…/`, 70 PNGs,
  before/after per fix), `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29_v2_after_fixes.xlsx` (+ `_Evidence_…_after_fixes/`).
- **Open:** KI-151 (one intermittent "couldn't build a query" under heavy load); KI-155 data team (6 mis-encoded
  names); KI-164 NOT VERIFIED for other schemes (PMAY-G `date_trunc('month', sanction_date)`); the duplicate-village
  chip loop (KI-156) NOT VERIFIED for CM Elevate Legacy; TC-10/16 "beneficiaries" definition needs an SME.

## Focus Legacy use-case re-test (2026-09-29) — VERIFIED, no code changed
- **28/28 PASS** on 3 of 3 full runs (supplementary TC-14b, TC-28blk: 2/2); data answers were
  word-for-word identical across the runs. Each figure was checked against the live DB view
  `curated.v_focus_legacy` AND the raw file `Focus Legacy to share to BLH.csv`, computed independently.
- **DB vs raw:** 14,569 rows joined 1:1 (raw `id` = `source_row_id`); pg_id, members, amount, date, FY,
  district, block, village and constituency identical on every row. PG-name text differs on 1,636
  rows (the DB keeps one current name per pg_id — by design). 88 rows have no block in both sources.
- Remarks, not failures: KI-145 (TC-26 omits 5 no-block PGs), KI-146 (MGNREGA wording in the AC
  chip), KI-147 (unformatted money, "month 4"); TC-10/16 "beneficiaries" = members summed over payments
  (102,021; 91,446 if each PG is counted once — needs an SME definition); one PG records 190 members.
- Report: `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29.xlsx`; proofs:
  `docs/Focus_Legacy_UseCase_Evidence_2026-09-29/` (one PNG per case).

## Earlier QA (from the `docs/` records, 2026-09-25)
- **Focus Legacy:**
  - 28/28 use cases in round 3, VERIFIED from the report's summary sheet;
  - bulk run of 168/168 block and 580/580 PG questions, per session notes. The report computes
    these with formulas that have no cached values, so they are not machine-verified.
- **CM Elevate Legacy:** 36/36 against both the DB and the raw file, VERIFIED from the "FINAL
  RESULT" in report v3. Earlier rounds: 22/36 and 33/36.
- **Ingestion-team fixes:**
  - block NULLs for rows with no village code, in both schemes;
  - CM Elevate Legacy row id 2392 loaded.

  8 of 9 Focus Legacy re-verification checks pass. TC-F1 has an open question (KI-019).

## Partially complete / open
- **PG alternate-spelling lookup:** done. `_focus_legacy_pg_name_answer` queries
  `curated.v_focus_legacy_pg_search` (KI-020, fixed 2026-09-29; VERIFIED in code 2026-10-02).
- **No accuracy benchmark** (KI-017).
- **Security hardening gaps:**
  - generated SQL can reach `app.*` and the privacy tables (KI-004);
  - authorization blind spots (KI-007);
  - TLS verification is off unless a CA bundle is present (KI-015).
- ~~Clarification resume is per worker (KI-001)~~ — stale line, corrected 2026-09-29: KI-001 is
  fixed and live-verified (KNOWN_ISSUES; D-023), see the next bullet.
- **KI-028 / KI-001 are fixed and live-verified** (2026-09-26): shared state goes through
  Postgres (D-023). New low-severity findings from the live run: KI-030 (an "all years" choice
  is not carried forward) and KI-031 (the scheme swap keeps old metric words).
- Full list: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Documentation audit (2026-09-26)

**Result:**
- All 11 maintained docs were re-checked against the code.
- Every code identifier they cite exists (script check).
- All relative links resolve.
- 24 documentation issues were found. 23 were corrected in the maintained docs. 1 (a stale
  claim in the SME file `data/pmay/README.md`) is recorded in DATA_MODEL.md but the file itself
  was not edited.
- The critical risk KI-022 is documented; it needs a user decision.

**New verified risks added to KNOWN_ISSUES:**
- KI-022: raw PII file pushed. **Critical.**
- KI-023: plaintext seed passwords.
- KI-024: voice recordings saved on the dev box.
- KI-025: LLM repairs spent on infrastructure errors.
- KI-026: no pool acquire timeout.
- KI-027: startup-only year and schema snapshots.
- KI-004 was extended with the prompt-injection path to `app.users`.

## Current development focus
- **INFERRED from the last commits:** per-scheme use-case QA and fixes for the two newest
  schemes.
- **This session:** follow-up context from structured state (D-022), the scheme-pause resume, and
  prompt-context observability.

## Current known failures
- No failing automated tests (see below).
- `pytest tests` over the whole directory fails with INTERNALERROR (KI-018). This is a test
  harness issue, not a product failure.

## Blockers
- None recorded in the repo.
- Live verification needs the office VPN to reach `10.48.242.4`. It was unreachable from this
  machine on 2026-09-26.

## Recent changes (git)

| Commit | Date | Summary |
|---|---|---|
| `7064ab6` | 2026-09-25 | Added Focus Legacy + CM Elevate Legacy data/tests/docs, QA reports, TECHNICAL_BRIEF, ASR fixes, UI |
| `815c37a` | 2026-09-18 | Routing, entity resolution, QA test coverage |
| `5c5a100` | 2026-09-13 | `verify=False` fallback for the gateway; app port bound to localhost |
| `036eafa` | 2026-09-13 | UAT fixes: district acronyms, Focus+ summary, live column notes |
| `7ad882f` | 2026-09-11 | SQL semantic verifier (qwen3-4b) |

Uncommitted code change (2026-09-26): the `scope-not-specified` statewide chip is now
"All of Meghalaya" (area only), not "All of Meghalaya, all years". See HANDOFF.md.

## Context-layer session (2026-09-26, code)

**Changed (uncommitted):**
- `app/context_manager.py`, `app/context_budget.py` (new), `app/pipeline.py`
- `app/prompt_builder.py`, `app/rag.py`, `app/entity_resolver.py`
- `app/session_store.py`, `app/routers/query.py`, `app/middleware/security_headers.py`,
  `app/config.py`
- `tests/test_context_semantic_state.py` (new)

**Behaviour:** see HANDOFF.md and AI_PIPELINE.md §5.1–5.5. The SQL, verifier and composer prompt
texts are byte-identical under budget. No `curated` DB, API-contract, scheme or business-rule
changes.

**Second pass, same day:** `app/session_sync.py`, `app/context_policy.py`,
`app/conversation_store.py`, `app/llm.py`, `tests/test_context_hardening.py` and
`tests/live_context_validation.py`, plus further edits to the pass-1 files. The only DB change is
the existing `app.conversations.context_state` JSONB, which now holds a versioned snapshot. No
DDL.

## Files changed by the documentation sessions (documentation only)

**Created:**
- `CLAUDE.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/AI_PIPELINE.md`
- `docs/SCHEMES.md`
- `docs/CURRENT_STATE.md`
- `docs/KNOWN_ISSUES.md`
- `docs/DECISIONS.md`
- `docs/TESTING.md`
- `docs/HANDOFF.md`

**Rewritten** (the previous versions were stale two-scheme docs):
- `docs/ARCHITECTURE.md`
- `docs/DATA_MODEL.md`

**Amended by the audit:**
- `CLAUDE.md`: PII-file and seed-password guardrails.
- `docs/{ARCHITECTURE,AI_PIPELINE,DATA_MODEL,SCHEMES,TESTING,DECISIONS,KNOWN_ISSUES,PROJECT_CONTEXT,SECURITY}.md`.
- `deploy/DEPLOYMENT.md`: the verify commands now match the auth on `/health`, `/metrics` and
  `/api/rag/status`.

**Amended (initialisation):**
- `README.md`: scheme list, model table, docs pointer.
- `docs/SECURITY.md`: corrected the DB-role claim.
- `docs/TECHNICAL_BRIEF.md`: marked as a historical snapshot.
- `docs/INFERENCE_REQUIREMENTS.md`: added a status note.

## Scheme substitution (2026-09-26)
- KI-039 is **fixed**: "give me same for <scheme>" is a context-preserving follow-up
  (AI_PIPELINE.md §5.6).
- It is live-verified on the reported conversation and 6 more scheme pairs; the negative cases
  are unchanged.
- New Medium issue KI-040: MGNREGA "beneficiaries" is undefined.

## Live context validation (2026-09-26, no code changes)
- 53 turns, 47 PASS, 1 caveat, 5 FAIL: `docs/Context_Validation_Report_2026-09-26.md`.
- **Open High issues from it:**
  - KI-032: stale block in the committed state;
  - KI-033: stale summary after a knowledge digression;
  - KI-034: SQL ignored mandatory entities and the verifier passed it;
  - KI-035: summing distinct beneficiaries across years.
- **Medium:** KI-031 (raised), KI-036, KI-037.

## Test status (2026-09-27, after the calendar-date fix KI-078, VPN up)
- pytest, 24 pytest-style files: **915 passed**, 0 failed. Plain scripts: **14/14**.

## Test status (2026-09-27, after the Focus Plus fixes, VPN up)
- pytest, 23 pytest-style files: **891 passed**, 0 failed (2026-09-27, after the CM Elevate fixes).
- Plain scripts: **14/14**. Live context suite: **43/43** (follow-up 29/29).
- Focus Plus use cases: 39/39 questions on 2 of 2 fresh live runs (30/30).

## Test status (2026-09-26 night, after the MGNREGA all-villages fixes, VPN up)
- pytest, 21 pytest-style files: **826 passed**, 0 failed.
- Bulk live: 448/448 block and 12,850/12,850 village queries on the final code.
- Plain scripts: **14/14**.
- Live context validation: **43/43 checks** (repeated twice). Legacy A/B: 39/43. Details are in TESTING.md.
- `smoke_restructure.py`: not run (needs a live server on :8502).

## Deployment status
- Deploy artefacts exist:
  - systemd;
  - nginx + ModSecurity;
  - Docker and docker-compose;
  - SQL role and retention scripts.
- Local dev `.env`: DB user `postgres`, `AUTH_ENABLED=true`, `EMBEDDING_PROVIDER=local`, CA
  bundle path set, `REDIS_URL` blank.
- Which environment is live, and with which DB role: UNKNOWN — NEEDS VERIFICATION.

## Next tasks (suggested, in priority order; none started)
0. **Decide what to do about KI-022**, the pushed raw file with unmasked bank accounts, and
   KI-023, the plaintext seed passwords. This is the user's or data owner's call: remove the
   file, possibly rewrite history, review repo access.
1. Wire the PG alias view into `_focus_legacy_pg_name_answer` (KI-020). This is the promised
   follow-up to the ingestion team.
2. ~~Persist the clarification-resume state (KI-001) and the last turn (KI-028)~~ — done
   2026-09-26 (D-023). New item: re-run the live suite on the 2026-09-29 changes (D-030).
3. Least-privilege DB role and table allowlist for generated SQL (KI-004).
4. Request-id propagation and logging of each attempt's SQL (KI-003).
5. A golden-set accuracy benchmark per scheme (KI-017).
6. Fix test collection so `pytest tests` works (KI-018).

## Components that should not be changed casually
- `pipeline.execute_with_repair` guards and `_verify_sql` filters. Each one encodes a live
  incident.
- `compose_response` faithfulness and hedge checks.
- `db.run_readonly` / `_assert_safe`.
- `_SCHEME_NAME_PATTERN`, `_is_ambiguous_focus` and `_pin_cm_elevate_dataset`: the scheme-name
  collision handling.
- `_reply_abandons_scope_pause` and the `pending_scope_q` resume logic.
- `data/<scheme>/*.yaml`: the SME contract. Change it only with an SME-backed reason, and keep
  `schema_context.py` in step.
- `app.*` DDL in `appdb.py`: additive `IF NOT EXISTS` only, because an older build must tolerate
  a newer schema.
