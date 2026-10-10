# Testing

*Reconciled on 2026-09-26 by running the full suite on the Windows dev box, with the
`10.48.242.4` DB and gateway **unreachable** (VPN off).*

## 1. What the tests are, and what they are not

- Every test is a **regression lock for a past incident**. The tests cover:
  - regex routing;
  - prompt text;
  - YAML catalogues;
  - deterministic helpers;
  - flows with the LLM stubbed out.
- **No test measures NL→SQL accuracy** against the real models and DB. A green suite says nothing
  about whether answers are correct (KNOWN_ISSUES KI-017).
- Correctness is checked by **live QA passes**. Their reports are in `docs/*.xlsx` and
  `docs/*_DB_Issues.md`:
  - Focus Legacy: 28/28, plus the bulk run of 168 + 580; re-tested 2026-09-29: 28/28 on 3 of 3 runs vs DB and raw (`docs/Focus_Legacy_UseCase_Test_Report_2026-09-29.xlsx`, v2 after fixes `…_v2_after_fixes.xlsx`); all blocks / villages / ACs / PGs 32,560/32,560 (`docs/Focus_Legacy_AllBlocks_Villages_ACs_PGs_Test_Report_2026-09-29.xlsx`);
  - Cross-scheme (the officers' 20 cases, `Cross Scheme Test Cases.csv`): 11/20 static, 4/20 then
    5 PASS + 4 PARTIAL live on 2026-10-10 before fixes; **after the D-034 fixes 36/36 live** (20 cases +
    15 typed-scope variants + the UI screenshot question) against independent DB queries
    (`docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md`, KI-213..228). Regression lock:
    `tests/test_cross_scheme_compare.py` (64). **Harness rule:** a clarification reply must be the
    chip's `question` (what web/ai_query.html sends), never its label — sending labels produced
    false failures in that retest.
  - CM Elevate Legacy: 36/36 on 2026-09-25; re-tested 2026-09-29: **33/36** vs DB and raw (2 full runs + 3 repeats of the failures;
    `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29.xlsx` + `_Evidence_2026-09-29/`). TC-13/14/34 failed in the AI layer (KI-166..168);
    **after fixes 36/36** on 2 of 2 fresh runs (`…_2026-09-29_v2_after_fixes.xlsx` + `_Evidence_2026-09-29_after_fixes/`);
    all districts / blocks / villages / constituencies **2,614/2,614** vs DB and raw (KI-169..181;
    `docs/CM_Elevate_Legacy_AllBlocks_AllVillages_Test_Report_2026-09-29.xlsx` + `_Evidence_2026-09-29/`).

## 2. How to run

Always use the repo venv, and run from the repo root (`.env` is read relative to the working
directory):

```bash
# 1) pytest-style suites (35 files as of 2026-10-10 (night); the glob picks up new ones). Do NOT run `pytest tests` — see KI-018.
.venv/Scripts/python.exe -m pytest -q $(grep -lE "^\s*(async )?def test_" tests/test_*.py)

# 2) plain-script suites (14 files) — each prints "ALL … PASSED" and exits 0/1
for f in tests/test_ac_full_results.py tests/test_admin_level_collision.py \
         tests/test_block_backstop.py tests/test_block_parent_district.py \
         tests/test_context_manager.py tests/test_cross_scheme_collision.py \
         tests/test_cross_scheme_money.py tests/test_edge_conversational.py \
         tests/test_focusplus_block_columns.py tests/test_history_charts.py \
         tests/test_mgnrega_split_facts.py tests/test_no_data_answer.py \
         tests/test_security.py tests/test_verifier_missing_geo.py; do
  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe "$f" || echo "FAIL $f"; done
```

- Set `PYTHONIOENCODING=utf-8` on Windows, because the scripts print non-ASCII characters.
- The script suites are slow when the DB is unreachable. Some entity-resolution paths wait for
  connect timeouts.
- `tests/smoke_restructure.py` is an end-to-end smoke test against a **running server** at
  `http://127.0.0.1:8502` (hardcoded `BASE`, not the default 8300). It was not run.

### Live context validation (needs the VPN)

```bash
.venv/Scripts/python.exe tests/live_context_validation.py            # all scenarios, 3 workers via Postgres
.venv/Scripts/python.exe tests/live_context_validation.py --legacy   # the pre-2026-09-26 rewrite, for A/B
.venv/Scripts/python.exe tests/live_context_validation.py --only A,E --no-db-sync
```

- Exit codes: 0 means all checks passed; 1 means a check failed; 3 means not run (unreachable).
- The report is written to `logs/live_context_validation.json`.
- With DB sync on, it writes temporary `app.conversations` rows under `live-ctx-*` session ids
  and deletes them at the end.

### Bulk live runs: every MGNREGA block and village (2026-09-26)

These runs are the only accuracy checks at scale; see §1 on why green unit tests do not prove
NL→SQL accuracy. The harness lived in the session scratchpad; its method is recorded here and on
the report's Summary sheet.

- **Cases:** FY 2024-25.
  - All 56 blocks × 8 questions: households, person-days, expenditure, women, 100-day %, spend
    + person-days, and two bare-name forms.
  - All 6,425 villages × 2 questions: person-days and total expenditure.
  - Village names are typed exactly as stored.
- **Expected values:** SUM by `village_code` / block from megh_db, read-only. Independently, the
  same sums from the raw CSVs in the repo root.
- **Execution:** `pipeline.answer_question` in-process with the `.venv` Python. When the bot
  pauses, the harness clicks the chip an officer would pick:
  - the option carrying the target village's exact name and block;
  - "village" or "block";
  - MGNREGA.
- **Pass rule:** the answer states the expected figure (lakh/crore display rounding allowed), or
  says there are no records without inventing a number.
- **DB connections: limit the pool.** The default pool is 10–30 connections per process, and
  `megh_db` allows `max_connections` = 100 in total and is shared. During a VPN drop, the server
  keeps the dead connections open until its TCP timeout. On 2026-09-26 that exhausted the server
  ("too many clients already"), and the pipeline turned it into "couldn't build a query" / KB
  fallbacks (KI-025).
  - Run bulk jobs with `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=10` and concurrency ≤ 10.
  - Pause on outages: check TCP to 10.48.242.4:5432 before each case, and retry a case that
    failed while the host was down.
- **Expect wall-clock time:** about 55 cases/min at concurrency 10.

## 3. Latest results

| Date | Suite | Result | Notes |
|---|---|---|---|
| 2026-10-10 (night; **KI-231, user-reported: "person days under CM Elevate" got "couldn't build a working query"**) | pytest 35 files + live | **1,879 passed, 2 skipped** (+22 in the new `tests/test_measure_gap_direct.py`, which calls the real `_measure_gap_answer`, the real `_answer_data` with only the DB-backed village masking stubbed and `resolve_entities` set to fail if reached, and the router's `remember_pause` / `_resume_option_pause`). Live, in-process with `init_pool` + `init_client`: the reported question pauses `measure-unavailable` with the MGNREGA chip first; that chip reaches the normal year pause and FY 2023-24 answers 27,264,054 person-days; the Applications chip answers 8,627; "houses completed under MGNREGA in West Garo Hills" names PMAY-G as the owner; the PMAY-G control answers 13,964 completed houses in West Garo Hills FY 2023-24. Plain scripts and the live context suite were NOT re-run (no routing, rewrite or state code changed; the new check sits inside `_answer_data`) | VPN up. In-process harness needs `await init_client()` or every DATA question ends "couldn't build a working query" |
| 2026-10-09 (UI: KI-210 rows not clickable, KI-211 Use cases + Glossary listed only 3 of 7 schemes) | pytest 33 files + DOM | **1,777 passed, 2 skipped** (1,725 + the new `tests/test_ui_use_cases.py`, 52); plain scripts **14/14**; routing **34/34** (every use-case question through `pipeline._shortcut_scheme` lands on the scheme of its section); jsdom against the served page: Use cases 8 sections / **34/34 rows click through to `ask()` with the exact text and close the modal** / Enter works / the OLD markup parses to `onclick = "closeModal(); ask("` with no handler bound; Glossary **7 sections, 49 terms**, all escapes resolved | No Python changed, so the live context suite was NOT re-run. The new suite was proven to bite by reintroducing each defect: inline handler → 3 fail, NRLM use-case section renamed → 8 fail, a Glossary section renamed → 3 fail |
| 2026-10-05 (CM Elevate KI-186: pending at level = file_status Pending, unique = distinct) | pytest 26 files + live | **1,433 passed** (+28 in `test_cmelevate_usecase_fixes.py`, now 142); scripts **14/14**; live pending set **871/871** (scratchpad `pend2/`: level 0/1/2 + On Hold x Meghalaya / 12 districts / 15 programmes / 180 pairs, + 39 "unique" variants), 4 hardest cases 12/12 repeats, "highest pending programmes" 6/6, OFF-009 sample 150/150, plain regression 6/6 | A/B against the previous prompt found and fixed a wording regression (plain "pending" -> current_file_status 4/6). Live context suite not re-run (no routing / rewrite / state change) |
| 2026-10-05 (CM Elevate OFF-009 all ordered programme pairs, KI-182 fix + empty-result zero answer + clear one-zero wording) | pytest 26 files + live | **1,405 passed** (+28 in `test_cmelevate_usecase_fixes.py`, now 114); scripts **14/14**; live OFF-009 bulk **2,520/2,520** questions answered (12 districts x 15P2 ordered pairs): before the fix 1,107 PASS / 282 'no matching records' / 1,131 FAIL, after the KI-182 fix 2,238 PASS / 282 'no matching records' / 0 FAIL, and after the empty-result zero answer **2,520 / 2,520 PASS** (282 both-zero re-run live 282/282; the user's screenshot questions answered); after the clear one-zero wording all 1,426 zero cases re-run live **1,426/1,426** (wording, order and figures checked line by line, `grade_zero.py`), SQL data correct 2,520/2,520 both times, raw = DB 2,520/2,520 | Answer-text change only (no routing / rewrite / state change), so the live context suite was not re-run. Harness: scratchpad `pairs2/` (gen_pairs2.py, bulk_run.py, judge_pairs2.py). Expected figures from `CM_Elevate_AllSchemes_20260930_full.xlsx` and a fresh read-only `curated.v_cm_elevate` extract |
| 2026-09-29, morning (typed reply to any chip pause, KI-181) | pytest 26 files + live | **1,371 passed, 2 failed** (+33 in `test_context_relevance_and_contract.py`, now 177; the same 2 verifier tests owned by the parallel CM Elevate Legacy session, see the row below). Scripts **14/14**; live context suite **43/43** (follow-up 29/29); live replay: measure gap -> "houses sanctioned" (106,527) / "the second one" (410,200 households) / "houses completed in west garo hills" (stays PMAY-G), Focus Plus year-out-of-range -> "2022-23", Sericulture -> "weaving", Garo Hills region -> "west garo hills" (5,489,616) | VPN up |
| 2026-09-29, early morning ("give me for pmay" scheme swap + measure gap, KI-180) | pytest 26 files + live | **1,338 passed, 2 failed** (+26 in `test_context_relevance_and_contract.py`, now 144). The 2 failures (`test_verifier_district_complaint_beside_village_code_is_discarded`, `test_verifier_complaint_on_correct_village_sql_is_discarded`) pin `_verifier_village_code_complaint_is_false` OUTSIDE CM Elevate Legacy; a parallel CM Elevate Legacy session widened that gate while this session ran — not caused by KI-180, left to that session. Scripts **14/14**; live context suite **43/43** (follow-up 29/29); live replay (scratchpad `119975e8-…/scratchpad/live_convo.py`): reported pair, "give me for pmay please", PMAY-G houses -> "give me for mgnrega", money swap (EKH ₹221.26 cr, unchanged), "tell me about pmay" still KNOWLEDGE, chip 106,527 houses | VPN up |
| 2026-10-10 (D-033 bare "Focus" = Focus Legacy) | pytest 26 files + live | **1,790 passed, 2 skipped**; scripts **14/14**; live: "What is focus?" → Focus Legacy KB, "…disbursed under focus in FY 2024-25" → ₹11,49,90,000, Focus Plus unchanged; live context suite **43/43** (follow-up 29/29); 4 older tests updated to D-033 (`test_context_relevance_and_contract.py`, `test_followup_scheme_scope.py`, `test_scheme_recommendation.py` x2) | VPN up |
| 2026-10-07 (KI-187 confirmed: duplicates = paid more than once, 2,655, D-032) | pytest 26 files + live | **1,498 passed**; scripts **14/14**; live check: see CURRENT_STATE | VPN intermittent |
| 2026-10-06 (KI-187 refined: same-year duplicates, D-032) | pytest 26 files + live | **1,498 passed**; scripts **14/14**; live (= DB = raw): statewide 7 (FY 2022-23: 1, FY 2025-26: 6), FY 2025-26 alone 6, FY 2024-25 "No", West Khasi Hills 2 | VPN up |
| 2026-10-05 (KI-187, Focus Legacy duplicate producer groups, D-032) | pytest 26 files + live | **1,436 passed**; scripts **14/14**; live: statewide 2,655 / 11,906 (2,647 twice, 8 three times; 5,318 of 14,569 records) and West Garo Hills 791 / 2,701 — both equal to the raw file; "duplicate payments" question unchanged | VPN up |
| 2026-10-07..08 (**exhaustive: all blocks / constituencies / villages / SHGs, KI-199 to KI-208**) | live bulk + pytest 32 files | **45,728 questions** (12 districts, 56 blocks, 55 constituencies, 4,976 villages, 40,629 SHGs), each expected answer read from `curated.v_nrlm` AND cross-checked against the raw CSV (only the 12 known KI-196 rows differ). **Districts 12/12, blocks 56/56, constituencies 55/55, villages 4,936/4,936 pass on the final code**; SHG lookups still running. Nine defects found and fixed, every one a silent wrong number or an endless thread. pytest **1,709 passed, 2 skipped**; plain scripts **14/14** | VPN dropped twice (runner is resumable — it skips ids already in bulk_results.jsonl). **Three method traps, all cost time:** (1) workers started before a fix keep writing stale results — always re-run a non-PASS with `--ids` before diagnosing; (2) a single year chip returns a correct SUBTOTAL (JAIAW PDENG: 12 total, 6+6 across two years) — answer year pauses with "all years"; (3) KI-206 reproduced ONLY at concurrency, never on a single re-run. Harness `bulk_gen.py` / `bulk_run.py` in the session scratchpad |
| 2026-10-07 (**KI-198, user-reported: "it is looping again and again"**) | pytest 30 files + live sweep | **1,638 passed, 2 skipped** (+36 in the new `tests/test_year_chip_no_loop.py`, +8 NRLM zero-wording); plain scripts **14/14**; NRLM use cases **43/43**. **Loop sweep: 8 of 58 chip paths looped before the fix, 258 paths / 0 loops / 0 dead ends after** (4 transient VPN timeouts, each answering on retry). The sweep drives the pipeline the way the UI does — follow a chip, keep going — and flags any thread that re-asks what it already asked, or whose chip resumes into its own text. The new suite asserts the INVARIANT (every year chip any scheme offers must parse and be in range), not the symptom. **Verified by narrowing `_parse_year_key` back: 11 tests fail; with the fix all pass** | VPN intermittent. Harness `loop_hunt.py` in the session scratchpad |
| 2026-10-07 (**KI-197, user-reported: NRLM refused as unsupported, chip looped**) | pytest 29 files + live | **1,594 passed** (+25 in the new `tests/test_supported_scheme_not_refused.py`); plain scripts **14/14**; NRLM use cases re-run **43/43** (the refusal path and `edge.py` are shared). The new suite is structural: it walks `SCHEME_CATALOG` and `_SCHEME_NAME_PATTERN` and fails if any alias of a loaded scheme sits in `_UNSUPPORTED_SCHEME`, and asserts that no refusal chip can resume into another refusal. **Verified by reintroducing the bug — 8 of the 25 fail — then removing it again.** The reported question now answers 517 SHGs in Chokpot block (= DB = raw) | VPN up |
| 2026-10-07 (**NRLM use-case QA + fixes, KI-188 to KI-195**) | pytest 28 files + live | **Round 1 16/43 use cases; after the fixes 43/43 live on the final code.** Every figure checked against **both** the raw `NRLM to share to BLH.csv` (40,629 rows) and `curated.v_nrlm` (40,629 rows): 34 scalar metrics plus the district / block / year / constituency / village breakdowns agree exactly, so all 27 failures were pipeline behaviour. pytest **1,569 passed** (1,498 baseline + 71 new in `tests/test_nrlm_usecase_fixes.py`); plain scripts **14/14**. Two SQL-verifier false positives found while re-testing and fixed (NRLM's `formation_financial_year_short` satisfying a resolved `year_key`; an aggregate demanded on a per-SHG column although one `v_nrlm` row IS one SHG) — each had killed a use case by exhausting the repair budget. Report `NRLM_UseCase_Test_Report_2026-10-07.xlsx` with 43 embedded proof images | VPN up (dropped twice mid-run; affected cases re-run). `live_context_validation.py` NOT re-run — the one remaining check, see HANDOFF. Harness in the session scratchpad |
| 2026-10-03 (KI-183, reported Focus Legacy FY comparison) | pytest 26 files + live | **1,377 passed**; scripts **14/14**; the reported question 20/20 in-process and the exact 4-turn conversation through the real `/api/query` endpoint (scratchpad `http_replay.py`: lifespan + JWT minted with `security.issue_token`, persistence stubbed out) — all answered; Focus Legacy use cases **28/28** (TC-01..12 first hit a VPN drop and were re-run) | VPN up |
| 2026-09-29, night (Focus Legacy all blocks / villages / ACs / PGs, KI-020 + KI-145..165) | pytest 26 files + live | **1,265 passed**; scripts **14/14**; live context suite **43/43** (follow-up 29/29); use cases **28/28 on 3 of 3 runs** (final code, month names now checked); bulk **32,560/32,560** (56 blocks x3 + 12 district breakdowns, 55 ACs x3, 3,384 villages x2, 11,906 PGs x2, 1,635 older raw spellings) on the latest run of each question, with a final-code regression pass of 5,215 (all block/AC questions, every question that ever failed, 4,300 random) 5,214/5,215 then 5/5 after KI-165. Harness: scratchpad `2ca0226e-…/scratchpad/bulk` (`gen_bulk.py`, `bulk_run.py`, `bulk_judge.py`), `build_bulk_report.py` | VPN up |
| 2026-09-29, later (reported "what is focus" conversation, KI-136 to KI-144; VPN up) | pytest 26 files + live | **1,185 passed** (+22 in `test_context_relevance_and_contract.py`, now 118); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20; turn p50 1.70 s / p95 3.12 s; rewrite p50 162 ms); live battery (scratchpad `live_convo.py`, `battery.sh`, `battery2.sh`): S1 substitution / unrelated / personal, S2 geography-metric-reference, S3 WHK, S4 five thousand (₹46.64 cr + not-a-loan line), S4b not-held, S5 Songsak search + block/village, S6 the reported conversation, S7 knowledge-then-place, S8 digression keeps MGNREGA — all correct, each figure read against its SQL. S8 failed once (KI-144), was fixed and re-run | VPN up |
| 2026-09-29 (context relevance + semantic contract, KI-030/032/034, KI-130 to KI-135) | pytest, 26 files | **1,163 passed** (1,067 + 96 in `test_context_relevance_and_contract.py`); scripts **14/14**. **OFFLINE ONLY**: `10.48.242.4` unreachable (5432 and 443), so `tests/live_context_validation.py` and every live check were NOT run. Reproduced before the fix with the real `_run_pipeline` (model/RAG/DATA stubbed): 4 of the reported turns misrouted | DB and gateway unreachable |
| 2026-09-28 (CM Elevate all districts / blocks / villages, KI-106 to KI-120) | live bulk | **7,364 / 7,364** on the final code (round 1 4,132/4,208); pytest **1,053**; context suite 43/43 | 7,364 questions; DB = raw; run passes one at a time, DB_POOL_MAX_SIZE ≤ 14; VPN dropped once (runner waits) |
| 2026-09-27 (calendar date read as FY, KI-078) | pytest, 24 files | **915 passed** (+18 in `test_calendar_date_filter.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); live probes 4/4 (2017-11-28 and 28/11/2017 → 504, 2017-18 → 15,513, undated still asks scope) | VPN up |
| 2026-09-27 (MGNREGA women share, KI-077) | pytest, 23 files | **897 passed** (+6 in `test_mgnrega_usecase_fixes.py`, now 84); scripts **14/14**; live context suite **43/43**; live women checks 8/8 (EKH 77.70% FY 2024-25, 74.90% FY 2022-23..2024-25, FY 2025-26 "not recorded") | VPN up |
| 2026-09-27 (CM Elevate use-case fixes, KI-068 to KI-075) | pytest, 23 files | **891 passed** (858 + 33 in `test_cmelevate_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); CM Elevate use cases: **55/55 questions on 2 of 2 fresh live runs (30/30 use cases)**, each figure checked against megh_db and the raw workbook; 11 reworded probe questions: 10 correct, 1 pre-existing intent flake (KI-076) | VPN up for the final runs |
| 2026-09-27 (CM Elevate use-case QA round 1, before the fixes) | live use cases | **23/30 use cases, 47/55 questions**, identical on 2 fresh live runs; each figure checked against megh_db `v_cm_elevate` and the raw `CM_Elevate_AllSchemes_20260927_full.xlsx` (DB = raw exactly). Failures KI-068 to KI-073; KI-074 needs a product decision. pytest and scripts not re-run (no code changed) | VPN flapped; runs were restarted after a drop. Harness in the session scratchpad; the method is on the report's Summary sheet |
| 2026-10-03 (PMAY-G beneficiaries = every record, KI-127) | pytest, 26 files | **1,373 passed, 2 failed** — the 2 failures are not from this change (CM Elevate Legacy KI-173 widened `_verifier_village_code_complaint_is_false`; tests in test_mgnrega / test_focus_legacy still assert the old scope); +2 PMAY tests. Live: Laskein 5,251, Ri Bhoi 17,565, state 171,107, Rtiang Sanphew 1, completed unchanged; use cases: only the 5 beneficiary answers changed, each equal to the every-record count in raw and DB; follow-ups likewise (MT1-1/2) | VPN up |
| 2026-09-28 (PMAY-G extra information removed, KI-128) | pytest, 25 files | **1,065 passed** (+6 in `test_pmay_usecase_fixes.py`, 2 older ones updated to the trimmed lists); live: 2,129-question random sample of the full scenario set + 21 Sep inputs **2,129/2,129**, use cases **71/71**, follow-ups **18/18** | VPN up |
| 2026-09-28 (PMAY-G 21 Sep tester sheet recheck, KI-125) | pytest, 25 files | **1,055 passed** (incl. 2 new bare-year tests); live: 192/192 (21 Sep inputs: GABIL SONGGITCHAM breakdown + partial, KYNDONGTUBER, LASKEIN count + breakdowns, 12 districts x beneficiaries / financial summary / performance / 7 FYs, 63 dates) and use cases 71/71 (a first attempt lost 38 to a VPN drop and was re-run clean) | VPN flapping |
| 2026-09-28 (PMAY-G full scenario test, KI-099 to KI-105) | pytest, 25 files | **1,005 passed** (`test_pmay_usecase_fixes.py` now 65); scripts **14/14**; live context suite **43/43**; PMAY-G full scenario matrix **17,331/17,331** (first pass 17,297 — 34 failures diagnosed, fixed, re-run 34/34, then the whole matrix re-run; the 571 model-reachable questions re-run again after KI-105): every village × 3 phrasings, every block / district / state × 16 question types, every block / district × every FY, 63 dates, 99 comparisons, 110 model-path questions; follow-up conversations **18/18**; use cases **71/71**; Focus Plus 300/300 and MGNREGA 300/300 villages. Report `docs/PMAY_G_Full_Scenario_Test_Report_2026-09-28.xlsx`. Harness: scratchpad gen_all.py, bulk_run.py, bulk_judge.py, gen_mt.py, run_mt.py, build_all.py | VPN dropped twice (runner paused / 3 re-runs) |
| 2026-09-28 (PMAY-G village named beside its block, KI-098) | pytest, 25 files | **998 passed** (+5 in `test_pmay_usecase_fixes.py`, now 58); live: all 5,120 villages asked with their ", <BLOCK> block, <DISTRICT>" tail **5,120/5,120**; all 224 block questions **224/224**; 500 random bare-name villages **500/500**; 71 use-case questions **71/71**; offline: 0 of 448 block phrasings read as a village | VPN up |
| 2026-09-28 (PMAY-G use-case + all-blocks/all-villages fixes, KI-089 to KI-097) | pytest, 25 files | **993 passed** (+53 in `test_pmay_usecase_fixes.py`; 4 older tests that pinned PMAY-G outside the village gate or the out-of-area check were updated with the reason); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); PMAY-G use cases **28/28** (71/71 questions on 2 of 2 fresh live runs, identical answers; round 1 was 17/28); all blocks **224/224**; all villages **5,120/5,120** on the final code (round 1 4,393/5,120 → KI-096 fixed); Focus Plus regression 60/60 blocks + 300/300 villages; MGNREGA regression 60/60 blocks + 300/300 villages; 16 model-path probes correct | VPN up |
| 2026-09-28 (KI-025 database-outage handling) | pytest, 24 files | **939 passed** (+3 in `test_focusplus_usecase_fixes.py`); scripts **14/14**; live context suite **43/43**; live outage simulation (megh_db port unreachable) → `DatabaseUnavailableError`, no KB answer, no repairs; live regression 204/204 blocks + 200/200 random villages | VPN up |
| 2026-09-27 night (Focus Plus all blocks / all villages, KI-079 to KI-088) | pytest, 24 files | **936 passed**; scripts **14/14**; live context suite **43/43** (follow-up 29/29); bulk live run on the final code: **204/204 block + 7,026/7,026 village questions** against megh_db and the raw CSV (8 re-run on final code, 3/3); confirmation pass 204/204 + 1,000/1,000 random villages; MGNREGA regression 448/448 blocks + 300/300 villages | VPN flapping, runner paused and resumed |
| 2026-09-27 (Focus Plus use-case fixes, KI-060 to KI-067) | pytest, 22 files | **858 passed** (826 + 32 in `test_focusplus_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29); Focus Plus use cases: **39/39 questions on 2 of 2 fresh live runs (30/30 use cases)**, each figure checked against megh_db and the raw CSV; typed bank-pause flow checked live through three pauses | VPN up (dropped once mid-session, retried) |
| 2026-09-26 night (MGNREGA all blocks / all villages, KI-049 to KI-059) | pytest, 21 files | **826 passed** (748 + 78 in `test_mgnrega_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (one earlier run was 40/43, with scenario D failing on a DB-sync outage; D alone and the full suite re-run clean); bulk live run on the final code: **448/448 block + 12,850/12,850 village queries** against megh_db and the raw CSVs | VPN flapping, retried |
| 2026-09-26 (MGNREGA use-case fixes, KI-041 to KI-048) | pytest, 21 files | **789 passed** (748 + 41 in `test_mgnrega_usecase_fixes.py`); scripts **14/14**; live context suite **43/43** (follow-up 29/29, SQL 20/20); MGNREGA use cases: **43/43 queries on 2 of 2 fresh live runs (30/30 use cases)**, each figure checked against megh_db and the raw CSVs | VPN up |
| 2026-09-26 (scheme substitution, KI-039) | pytest, 20 files | **748 passed** (657 + 91 in `test_scheme_substitution.py`); scripts **14/14**; live context suite **43/43**; live substitution run: the reported conversation plus 6 scheme pairs and 3 negatives, all correct | VPN up |
| 2026-09-26 (context validation) | live end-to-end, 16 conversations / 53 turns (brief groups A–F, plus clarification, scheme switching, contamination, long answer, 8-turn) | **47 PASS, 1 PASS with a caveat, 5 FAIL**. Findings KI-032 to KI-038; report `docs/Context_Validation_Report_2026-09-26.md` | VPN up, no code changes |
| 2026-09-26 (after live fixes) | pytest, 19 files | **657 passed**, 0 failed (`test_context_hardening.py` now 61) | VPN up |
| 2026-09-26 (after live fixes) | plain scripts, 14 files | **14/14 exit 0** | VPN up |
| 2026-09-26 (context hardening) | pytest, 19 files | **649 passed**, 0 failed (596 + 53 new in `test_context_hardening.py`) | DB unreachable |
| 2026-09-26 (context hardening) | plain scripts, 14 files | **14/14 exit 0** | DB unreachable |
| 2026-09-26 (context hardening) | `tests/live_context_validation.py` (VPN up) | **43/43 checks**, follow-up resolution 29/29, SQL 20/20, turns across 3 workers via real Postgres; repeated twice | live gateway and DB |
| 2026-09-26 (context hardening) | `tests/live_context_validation.py --legacy` | 39/43, follow-up 25/29, SQL 18/18 | live A/B baseline |
| 2026-09-26 (context layer) | pytest, 18 files | **596 passed**, 0 failed (558 + 38 new in `test_context_semantic_state.py`) | DB unreachable |
| 2026-09-26 (context layer) | plain scripts, 14 files | **14/14** | DB unreachable |
| 2026-09-26 | pytest, 17 files | **558 passed**, 0 failed, 1 warning (Pydantic class-based `Config` deprecation) in 24.6 s | DB unreachable |
| 2026-09-26 | plain scripts, 14 files | **14/14 exit 0** | DB unreachable |
| 2026-09-26 | `pytest tests` (whole dir) | **INTERNALERROR** (`SystemExit` from a script at import), 214 s | KI-018 |
| 2026-09-24 | pytest, 16 files | 423 passed (TECHNICAL_BRIEF) | before later additions |

### Live A/B (2026-09-26)

| Metric (live, 2026-09-26, same 8 scenarios, one run each) | Legacy (pre-context work) | New |
|---|---|---|
| Checks passed | 39/43 | **43/43** |
| Follow-up resolution checks | 25/29 | **29/29** |
| SQL executed without the fallback | 18/18 | 20/20 (two more turns reached SQL) |
| Rewrite prompt, approximate tokens, mean / max | 236 / 279 | 217 / 240 |
| Classifier input tokens, gateway mean | 925 | 870 |
| SQL-generation input tokens, gateway mean | 10,196 | 10,280 |
| Rewrite stage p50 / p95 | 141 / 898 ms | 154 / 638 ms |
| SQL generation p50 / p95 | 715 / 1,010 ms | 728 / 998 ms |
| Whole turn p50 / p95 (excluding the DB sync) | 2,155 / 3,297 ms | 2,335 / 3,886 ms |
| DB `sync_in` p50 / p95 | 52 / 105 ms | 52 / 359 ms |
| DB `sync_out` p50 / p95 | 55 / 128 ms | 50 / 153 ms |

- The new run was repeated: two runs gave 43/43 both times.
- Legacy fails scenario A, T3: "How many beneficiaries were there?" was not treated as a
  follow-up, so it got a year pause and lost the Dalu / FY scope. It also fails one structural
  check in D.
- Turn latency is not an apples-to-apples comparison: the new run answered 2 more data turns,
  whose full SQL chains the legacy run never reached.
- The DB sync ran in both modes. The A/B isolates the context logic, and the old per-worker
  behaviour is covered offline.
- TTFT is not observable, because generation is not streamed.
- The SQL prompt measured by the gateway is about 10.2k tokens, including LIVE SCHEMA and the
  catalogue. The offline estimate of 3.0k–6.9k excludes those blocks.

## 4. Test → component map

**The numbers in brackets below are collected pytest cases** (`pytest --collect-only`,
2026-09-26). Many tests are parametrised, so the 21 files hold 382 `def test_` functions but 826
cases. Per file:
- `ac_flow_focus_legacy` 16, `ac_focus_legacy` 18, `asr_transcribe` 54, `cm_elevate_legacy` 83;
- `context_semantic_state` 38, `context_hardening` 61, `scheme_substitution` 91;
- `fewshot_ranking` 20, `focus_legacy_routing` 44, `focus_legacy_usecase_fixes` 42, `mgnrega_usecase_fixes` 84;
- `followup_scheme_scope` 8, `lookup_intent` 19, `pg_abbreviation` 22, `pg_name_lookup` 15;
- `rag_general_scoping` 38, `scheme_pick` 46, `scheme_recommendation` 70;
- `stale_clarification` 31, `year_gap_comparison` 17, `year_gap_tolerance` 15.


| Test file | Style (cases) | Protects | Component(s) |
|---|---|---|---|
| `test_security.py` | script | headers, body/field limits, endpoint gating, login throttle, error hygiene, secret guard | middleware, routers, main |
| `test_pmay_usecase_fixes.py` | pytest (53) | PMAY-G use-case + all-villages fixes (KI-089 to KI-097): facts-path shape detection and answers (exact ₹, differences, stages), model-path guards (year GROUP BY, AVG rates, LIMIT 1), house-status 'sanctioned' phrase, written dates, typo / plural blocks, village narrowing, stated zeros | pipeline `_pmay_*`, entity_resolver `resolve_house_status`, premise_check |
| `test_asr_transcribe.py` | pytest (54) | voice input format/lang/no-speech/prompt-echo | routers/query, llm.call_asr, asr_guard, web |
| `test_context_manager.py` | script | reference substitution, state carry, summary, memory | context_manager, conversation_memory |
| `test_context_relevance_and_contract.py` | pytest (177) | the 2026-09-29 conversation: request-verb scheme swaps ("give me for pmay") and the swap measure-gap pause (KI-180); typed replies to every chip pause (`_resume_option_pause`, `_paused_thread_antecedent`, router `remember_pause`, KI-181); unrelated messages ("who is harshit") and personal money requests do not inherit the scheme; real follow-ups keep a continuation signal; substitution for every registry scheme; bare "which one?" asks; WHK near-miss chips; Focus Plus stated amount (parse, guard, not-held pause, not-a-loan note); `_resolved_scope_missing` rejects K8-shaped SQL and does not over-validate (all years, CM Elevate, village, region, LIKE, apostrophe, date range); village name search (DB stubbed); KI-032 state clears; KI-030 all-years carried and persisted; the three-turn West Garo Hills → Dalu → beneficiaries plan; `pipeline_decision` log has labels, not text | context_policy, edge, entity_resolver, premise_check, pipeline, context_manager, session_store, context_budget |
| `test_scheme_substitution.py` | pytest (91) | "give me same / do the same / now for <scheme>" keeps the previous operation for every registry scheme; the rewrite swaps only the scheme (metric, geography, year, grouped, result-reference shapes); negatives (grouping/time/geography changes, informational, comparison) are untouched; never cached; 7th-scheme agnostic | pipeline `_scheme_substitution` / `is_scheme_substitution` / `_scheme_swap_rewrite`, context_policy, router |
| `test_context_hardening.py` | pytest (61) | cross-worker state A→B→C (KI-028/KI-001) incl. stale-write refusal, DB-down/slow-DB degradation, pending pause across workers; merge actions KEEP/REPLACE/CLEAR/REQUIRE_CLARIFICATION and what they drop from `prior_resolved`; follow-up kinds → layers; provenance sources; field-specific rewrite checks (year, metric, category, village names); 7th-scheme agnosticism; priority-aware SQL budget; per-call usage record | session_sync, session_store, context_policy, context_manager, pipeline, prompt_builder, context_budget, llm, router helpers |
| `live_context_validation.py` | **live script, not pytest** | scenarios A–G against the real gateway and DB, turns rotated across 3 workers through real Postgres; per-turn rewrite/state/SQL/answer/tiers; per-role tokens and p50/p95 latency; `--legacy` A/B | whole pipeline |
| `test_context_semantic_state.py` | pytest (38) | follow-up rewrite evidence tiers (no `answer[:300]`), provenance guard, scheme isolation for all 6 schemes, failed-turn state safety, typed scheme-pause resume, pause replies never cached, `prompt_context` logging, backward compatibility | context_manager, pipeline `rewrite_followup` / `_resume_scheme_pause`, prompt_builder, context_budget, router |
| `test_history_charts.py` | script | reopened chats redraw charts (`response` JSONB) | conversation_store, web |
| `test_stale_clarification.py` | pytest (31) | a new question is not merged into a stale pause; PG names are not geography | `_reply_abandons_scope_pause`, `_drop_producer_group_names` |
| `test_edge_conversational.py` | script | meta/conversational handling | edge |
| `test_lookup_intent.py` | pytest (19) | "is there any X named Y" → DATA | classify_intent, `_DATA_HINTS` |
| `test_followup_scheme_scope.py` | pytest (8) | follow-ups that switch scheme | looks_like_followup, rewrite |
| `test_scheme_pick.py`, `test_scheme_recommendation.py` | pytest (46, 70) | pick/recommend/why/fit, harmful-first ordering | pipeline step f |
| `test_rag_general_scoping.py` | pytest (38) | KB scheme scoping; no lost clarification pause; stale collection | `_run_pipeline` KNOWLEDGE, rag, kb_ingest |
| `test_fewshot_ranking.py` | pytest (20) | breakdown questions retrieve right-shaped exemplars | annotations IDF ranking |
| `test_verifier_missing_geo.py` | script | verifier false "missing geography" suppressed | `_verify_sql` filters |
| `test_cmelevate_usecase_fixes.py` | pytest (33) | CM Elevate use-case fixes KI-068 to KI-075: the Unresolved filter off programme totals; complete programme lists; the multi-programme split and combined total; pending at a level vs a plain pending; zero-count sectors kept; comparison differences; garbled two-label answers rebuilt; the file_status reading of pending; approved/rejected on file_status; the verifier scheme_specific false complaint; PRIME SEED routing | _cm_legacy_keep_unresolved_off_village, _cm_elevate_* SQL guards, _cme_* guarantees, _verifier_scheme_specific_complaint_is_false, _infer_scheme_from_terms |
| `test_focusplus_usecase_fixes.py` | pytest (56) | Focus Plus use-case fixes KI-060 to KI-067: shares (breakdown and recount-verified single count), comparison difference / higher side, complete named lists, rupee formatting, mis-grouped digits, SQL-literal numbers, bank-pause chips + typed resume, Focus Legacy bank KeyError, batch/tranche scope, resumed question remembered on a second pause; all-villages fixes: village narrowing, WHERE pin (place literals, subquery), twin-village LGD chips, district-alias collision, Nan block, BURMA, one-figure misquote, roman numerals | compose_response, _focusplus_answer_guarantees, _bank_clarification, _resume_scheme_pause, _needs_scope_clarification, _run_pipeline, router pause_question |
| `test_mgnrega_usecase_fixes.py` | pytest (84) | MGNREGA use-case fixes KI-041 to KI-048: block chip, SOUTH TURA, scheme vocabulary, integer division, village lists, comparisons, deterministic spend + person-days and admin queries, 100-days faithfulness, top-1 wording, lakh units | resolve_entities, _infer_scheme_from_terms, execute_with_repair, _answer_data, compose_response |
| `test_calendar_date_filter.py` | pytest (18) | KI-078: a calendar date (2017-11-28, 28/11/2017) is not back-filled as an FY, yields no premise figures, and satisfies the scope gate; real FY ranges, premises and the undated scope pause unchanged | _backfill_explicit_year, premise_check.extract_premises, _needs_scope_clarification |
| `test_mgnrega_split_facts.py` | script | employment and expenditure never joined; split-fact hint | execute_with_repair guards |
| `test_no_data_answer.py` | script | empty result → one line; typos don't cause it | compose_response, `_no_data_answer` |
| `test_cross_scheme_money.py` | script | "which scheme paid most" is deterministic across schemes | `_cross_scheme_money_answer` |
| `test_cross_scheme_collision.py`, `test_admin_level_collision.py` | script | block/village/AC level-collision chips per scheme | entity_resolver, gates |
| `test_block_backstop.py`, `test_block_parent_district.py` | script | named blocks survive extraction; a block is scoped to its own district | resolve_entities |
| `test_ac_full_results.py`, `test_ac_focus_legacy.py`, `test_ac_flow_focus_legacy.py` | script / pytest (18, 16) | AC questions return all rows; AC flow end to end for Focus Legacy | entity_resolver, prompt_builder, `_AC_CAPABLE_SCHEMES` |
| `test_focusplus_block_columns.py` | script | Focus Plus blocks use `block_name_raw` | prompt_builder entities block |
| `test_focus_legacy_routing.py` | pytest (44) | Focus Legacy wiring and scoping | scheme registries |
| `test_focus_legacy_usecase_fixes.py` | pytest (122) | the 2026-09-25 QA fixes (group size, list totals, verifier guards, overview retrieval); 2026-09-29 all-levels fixes KI-020, KI-145..165 (breakdowns from rows, month / rupee format, AC scheme routing and drill-down, PG-name parsing / older spellings / edge masking, village phrase, twins, chip pin, DATE_TRUNC cast) | pipeline, edge, entity_resolver (Focus Legacy) |
| `test_pg_name_lookup.py`, `test_pg_abbreviation.py` | pytest (15, 22) | PG name matching and suffix stripping; NULL-row answers; "PG" recognised | schema_context rule 5a, compose_response, edge |
| `test_year_gap_tolerance.py`, `test_year_gap_comparison.py` | pytest (15, 17) | absent year + valid year answered; comparisons substitute | `_apply_year_gap` |
| `test_cm_elevate_legacy.py` | pytest (83) | CM Elevate Legacy wiring, pinning, not-held, exact totals | pipeline (CM Legacy), schema_context |

## 5. Coverage by requested category

| Category | Status |
|---|---|
| Unit | Yes, extensively. Most tests call single functions |
| Integration (stubbed LLM, in-process pipeline) | Partial. Several suites drive `answer_question` with stubbed model calls |
| End-to-end (live server) | Only `smoke_restructure.py`, which needs a running server; not part of the routine run |
| SQL correctness vs DB | **None automated.** Guards are tested on SQL strings only |
| Scheme tests | Focus Legacy, CM Elevate Legacy and Focus Plus blocks have suites. PMAY-G has `test_pmay_usecase_fixes.py` (2026-09-28) plus the live use-case and all-blocks / all-villages reports |
| Clarification | Yes (stale pause, collisions, year gap, follow-ups) |
| Composer | Partial. `compose_response` / `_deterministic_answer` are exercised in `test_cm_elevate_legacy.py` and `test_pg_name_lookup.py`, and no-data in `test_no_data_answer.py`. No dedicated faithfulness-guard suite |
| Security | `test_security.py`. **No test that generated SQL cannot read `app.*`** (KI-004), and no prompt-injection test |
| Regression | Every suite is regression-oriented |
| Performance / load | **None.** `docs/INFERENCE_REQUIREMENTS.md` describes a gateway load test (`hey`), run by AIOps |

## 6. Rules for new tests

- Test the **real function**. A test that re-implemented the logic under test once stayed green
  after the production branch was disabled.
- Some tests pin exact source strings (e.g. call sites in `pipeline.py`). Keep those call sites
  verbatim, or update the test deliberately.
- `_SCHEME_DATA_YEARS` is overwritten at startup by `refresh_scheme_years`. Assert against the
  values the module actually holds.
- **Live QA harness:**
  - call `pipeline.answer_question` in-process with the `.venv` Python, from the repo root;
  - click the clarification chip a user would pick;
  - the DB and gateway need the VPN.

  System Python lacks `fastembed`, so KB answers silently degrade under it.
- Test name lookups **after** a place-scoped turn. Single-turn harnesses miss follow-up rewrite
  bugs.
