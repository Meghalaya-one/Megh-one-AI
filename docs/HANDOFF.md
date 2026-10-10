# Handoff

*For the next Claude session or account. Keep this short and overwrite it at the end of every
significant session.*

**Commit note (2026-10-10): everything below through the KI-231 entry is now COMMITTED and pushed.**
- The 2026-10-07 work went out as `36b7427`; the 2026-10-10 work (D-033, D-034, KI-212..231, the new runbook,
  the cross-scheme reports and the 7 new test files) was committed on top and pushed to `origin/main`.
- The "Code changed, uncommitted" wording in the entries below is therefore historical — it describes the state
  at the time each session ended, not now. Entries added after this note should say where they stand.
- Tests on the committed tree: pytest **1,879 passed, 2 skipped** (35 files). Matches the CLAUDE.md §7 baseline.
- **Still NOT committed, awaiting a decision:** the working tree deletes 762 QA evidence screenshots, 9 `.xlsx`
  test reports and `docs/CM_Elevate_Legacy_DB_Issues.md`. CURRENT_STATE.md and KNOWN_ISSUES.md still cite those
  folders as the evidence for specific KI numbers, so the deletions were deliberately left out of both commits.
  Either retire that evidence (and update the citing docs in the same commit) or restore it with
  `git checkout HEAD -- docs/`.
- **KI-022 is unchanged and still Open:** `Focus Legacy to share to BLH.csv` (unmasked `account_no`, `ifsc_code`,
  `name_on_the_account`) remains tracked on `origin/main` from `7064ab6`. `.gitignore` now blocks *new* root-level
  `*.csv` / `*.xlsx`, but ignoring a tracked file does not untrack it.

**Latest (2026-10-10, night): KI-231 — a measure another scheme owns, asked under a named scheme. Code changed, uncommitted.**
- **User report (UI screenshot):** "So what is the total person days in Meghalaya under CM Elevate?" → "couldn't build a
  working query". Wanted: say the measure is MGNREGA's, not CM Elevate's, and offer it.
- **Where it lives:** `app/pipeline.py` `_measure_gap_answer` + `_measure_gap_scope_phrase` + `_MEASURE_OWNER_OFFERS`
  (next to `_swap_measure_gap`), one call in `_answer_data` right after `classify_scheme` / the Focus Legacy group-name
  pin and before `_cm_legacy_not_held`. Pause rule `measure-unavailable`; the router's generic chip-pause branch
  remembers it (KI-181), nothing in `routers/query.py` changed.
- **Scope:** fires only when exactly one scheme is NAMED in the question and the measure is in
  `_SCHEME_OWN_MEASURES` for another scheme. Two schemes named → D-034 comparison as before. Money / counts →
  generator as before. Follow-up fragments ("and person-days?") were not touched: the swap variant (KI-180) and the
  vocabulary rule in the context layer handle those as before.
- **Tests:** pytest **1,879 passed, 2 skipped** (35 files; new `tests/test_measure_gap_direct.py`, 22). Live in-process:
  the reported question, both chips, a PMAY-G/MGNREGA pair, controls unchanged. Plain scripts / live context suite
  not re-run (no routing or state code changed).
- **Trap:** an in-process harness must `await init_client()` (and `init_pool()`), or every DATA question ends in
  "couldn't build a working query" with a buried "llm client not initialized" warning.
- **Next:** commit; if more scheme-only measures should be covered (CM Elevate applications under MGNREGA, Focus
  Legacy producer groups under PMAY-G, …), add them to `_SCHEME_OWN_MEASURES` + `_MEASURE_OWNER_OFFERS` — both
  the swap and the direct check read that one registry.

**Latest (2026-10-10, evening): cross-scheme cases FIXED — 20/20 live. D-034. Code changed, uncommitted.**
- **Result:** the 20 officer cases + 15 typed variants + the UI screenshot question: **36/36 live**,
  verified against megh_db with independent SQL. Report: `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md`
  → "AFTER FIXES". KI-213, 214, 218–228 marked Fixed in KNOWN_ISSUES (KI-212/215 fixed earlier by D-033,
  KI-216 closed as not a defect).
- **Where it lives:** `app/pipeline.py` — `_cross_scheme_compare_plan` (which questions), `_xs_query` (the
  per-scheme SQL), `_cross_scheme_compare_answer` (wording from rows), wired into `_answer_data` before the
  pauses and into `classify_intent`. Guards: `_cross_scheme_sql_issue` in `execute_with_repair`,
  `_verifier_join_complaint_on_aggregates` in `_verify_sql`, `_range_claim_misstated` and
  `_cross_unit_total_stated` in `compose_response`. Pins: `_ALL_FINANCIAL_YEARS_SCOPE` in
  `_prefers_cm_elevate_legacy`. Year pause text: `_scheme_years_text`. D-034 has the reasoning.
- **Not changed, on purpose:** MGNREGA + PMAY-G-only comparisons, any question with a specific year, a
  village / constituency / sub-scheme / tranche, and the `_CROSS_SCHEME` prompt text (it still tells the
  model to probe the money view; harmless, and editing it would move other answers).
- **Tests:** pytest 1,854 passed / 2 skipped (34 files; new `tests/test_cross_scheme_compare.py`, 64);
  scripts 14/14; live context 43/43 + follow-ups 29/29; 8 live controls unchanged.
- **Traps (cost time today):**
  1. A live harness must send a clarification chip's **`question`**, not its label — the earlier retest
     sent labels and recorded false failures (KNOWLEDGE answers, triple pauses).
  2. Another Claude session (`…/45f4ba7f…/scratchpad/crossrun`) ran cross-scheme tests in parallel and
     exhausted DB connections ("too many clients"); it also made the D-033 edit. Diff `pipeline.py`
     against a backup before editing.
  3. `inspect.getsource` tests fail spuriously if `pipeline.py` is edited during a pytest run.
  4. The VPN dropped twice for ~30 min; plain scripts then crawl and `test_admin_level_collision.py` fails
     on connect errors only.
- **Follow-up user report (same evening), KI-229/230:** a seven-scheme money question with "status" stays on
  model SQL; its fallback now writes a labelled side-by-side and explains CM Elevate's missing figure, and the
  UI Sources line now lists every scheme read (`sourceSchemes()` in web/ai_query.html).
- **Next:** commit (nothing is committed); ask the officers whether CROSS-7/18 mean all seven schemes
  (NRLM leads 8 districts) or the five named elsewhere.

**Latest (2026-10-10, later): cross-scheme LIVE retest — 4 PASS / 16 FAIL. KI-218..224. TESTING ONLY, NO CODE CHANGED.**

> **Corrected the same day (typed-scope retest):** **5 PASS / 4 PARTIAL / 11 FAIL**, not 4/16. The first live run answered the year pause via the chip; typing the scope into the question (as officers do) skips the pause and works better — CROSS-1 passes that way. See §0 of the retest report. New: KI-225 (a typed year flips CM-ELEVATE to CM Elevate Legacy), KI-226 (composer misstates a max that was in the result), KI-227 (one UNION branch drops the district filter), KI-228 ("Focus Legacy beneficiaries" = 102,021 or 11,906 depending on phrasing). KI-221 is narrower: the verifier rejections cluster on the chip-resume path.
- **The VPN came back**, so the DB and bot columns that the earlier audit could not produce are
  now done, on the **post-D-033** code. Report:
  `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md`. **No `app/` or `tests/` file was
  touched** — the user asked for testing only, twice.
- **Note for whoever owns D-033:** it works at the router but **not** end to end. CROSS-2
  resolves `['Focus Plus','Focus Legacy']` correctly and still answers *"Focus Plus: 105,813
  beneficiaries. The data available doesn't cover the FOCUS scheme"* — the generator wrote
  `SELECT 'FOCUS', 0 FROM curated.v_cross_scheme_money_district_year WHERE scheme_code='FOCUS'`
  (a MONEY view, for a BENEFICIARY count, on a scheme_code that does not exist). **KI-218.**
- **Scoreboard: 4 PASS** (CROSS-3, 17, 18, 20) **/ 16 FAIL** — 9 visible (6 "couldn't build a
  working query", 3 endless pause chains), **7 silent** (4 confident wrong numbers, 3
  encyclopaedia answers to data questions). The silent ones are the dangerous half.
- **Closed / corrected — do not re-investigate:**
  - **KI-212, KI-215: FIXED**, verified live (all 8 bare-FOCUS cases pin; CROSS-17/20 get all 5).
  - **KI-216: CLOSED, not a defect.** The view holds only `MGNREGA` and `PMAY`, so the separate
    Focus Plus UNION branch is right. Raw and DB reconcile to rounding.
  - **KI-213: magnitude was WRONG in the first report.** Focus Plus beneficiaries are
    **105,813**, not 12,527 — `member_id` exists only on the 12.5K cohort (93K cohort has none).
    `COUNT(*)` overstates **3.64x**; `member_id` understates **8.45x**. Guard still off on
    multi-scheme questions, so the KI stands.
- **Fix in this order** (reasons, not preference):
  1. **KI-214 (CRITICAL, one token)** — add `assistance|support` to `_MONEY_SUPERLATIVE` (+ a
     `higher … A or B` branch to `_CROSS_SCHEME_SUPERLATIVE`). Live-proven damage: CROSS-12 said
     *"Focus Plus … highest … 1197392500.00"* when **MGNREGA is highest at 3,628.67 cr, 30x
     larger**. Lock: `tests/test_cross_scheme_money.py`.
  2. **KI-221 (HIGH, unblocks 6 cases alone)** — the 4B verifier calls the prompt's OWN
     prescribed aggregate-then-CROSS-JOIN a "PROHIBITED JOIN" (that rule is for ROW-LEVEL fact
     joins). 9 of 11 repair attempts; every no-answer case. **Every PASS used the LONG
     (UNION ALL) shape; the WIDE (CROSS JOIN) shape is unusable today.**
  3. **KI-220 (CRITICAL)** — composer guard: never total across differing units, never say
     "all N schemes" unless N matches the resolved set, never render CM Elevate money as 0.00
     ("not held"). CROSS-11 did all three at once (737.68 cr, "four" when 7 resolved, three
     money-bearing schemes dropped).
  4. **KI-219 (HIGH, gates 13 of 20)** — the year pause claims JOINT availability across a
     9-year union no scheme has; pick FY 2019-20 and the "comparison" is PMAY-G alone.
  5. **KI-222** — "widest coverage" / "highest concentration" / "performance in X" leave DATA
     for RAG. 6. **KI-218, KI-223, KI-224, KI-213.**
- **Harness notes — these cost real time, don't rediscover them:**
  - `answer_question(q, session=…, scope=None)`. There is **no** `session_id` kwarg.
  - Startup must include `init_pool`, `init_client`, **`init_qdrant`**, `annotations.load_all`,
    `entity_resolver.load_all`, `schema_introspect.load`, `refresh_scheme_years`. Without
    Qdrant every KNOWLEDGE-path case **raises** instead of answering (CROSS-14 did).
  - **`ClarificationNeeded` is an exception.** A single-turn harness scores **18/20 as
    "paused"** and tests nothing. Resume via `router.remember_pause` and reply with the widest
    option, up to 3 hops — copy `tests/live_context_validation.py`.
  - Use the configured `session_store` singleton; `SessionStore()` needs 3 positional args.
  - **Never run two harness instances against one jsonl** — both write it and race.
  - Reusable scripts are in the session scratchpad (`db_baseline.py`, `bot_run2.py`,
    `cases.py` with the 20 questions and real place names substituted).
- **Still unanswered, for the officers not for code:** 8 cases say "FOCUS" where two Focus
  schemes exist. D-033 now reads that as Focus Legacy — but Focus Legacy has **no person record
  of any kind**, so CROSS-1/2/6/17/19/20 ask it for a beneficiary count it cannot give (only
  11,906 groups or 102,021 memberships). Confirm the intent before these become acceptance
  criteria.

**Latest (2026-10-10): cross-scheme use-case audit — KI-212..216. TESTING ONLY, NO CODE CHANGED.**
- **Scope:** the 20 officer cases in `Cross Scheme Test Cases.csv`. Report:
  `docs/Cross_Scheme_UseCase_Test_Report_2026-10-10.md`.
- **The run is INCOMPLETE and that is the first thing to know.** The ask was raw vs DB vs bot.
  `10.48.242.4` was unreachable all session (ping 100% loss; 5432/6333/8000/8001 time out), and
  that single host serves Postgres, Qdrant **and** every model endpoint, so **the DB and bot
  columns were never produced**. The raw column and a static code audit are complete.
- **The user asked for testing only, no code changes — respected.** `app/` and `tests/` were not
  touched (verified by mtime: `pipeline.py` 10-08, `edge.py` 10-07; only the three docs carry a
  10-10 stamp). The `M` flags on `app/*.py` in `git status` are pre-existing from earlier
  sessions. **The five KIs below are diagnosed but NOT fixed — that is the next session's work.**
- **11 of 20 cases fail on code evidence alone**, none as a visible error:
  - **KI-212 (High, 8 cases)** — "Focus+ … and FOCUS" (the officers' own wording) drops Focus
    Legacy **silently**. `_is_ambiguous_focus` early-returns False as soon as the Focus Plus
    pattern matches anywhere, so `\bfocus\s*\+` eats "Focus+" and the standalone "FOCUS" is
    invisible; no which-Focus pause fires. Probe: `'…in FOCUS.'`→ambiguous=True,
    `'…in Focus+ and FOCUS.'`→False. Fix with the residue technique at pipeline.py:419-420.
  - **KI-213 (High, 8 cases)** — `_focusplus_single_district_beneficiary_guard` opens
    `if schemes != ["Focus Plus"]: return sql`, so the guard is **off on every cross-scheme
    question** and only the prose rule stops `COUNT(*)`=385,671 being reported as
    beneficiaries=105,813 (**3.64x**; corrected by the later live retest — 12,527 was `member_id`,
    the 12.5K cohort only). Fan-out is uneven, so it **reorders the district
    ranking** too. The invariant needs a post-SQL check that ignores scheme count.
  - **KI-214 (Medium)** — money ranking misses "financial **assistance**" (CROSS-12; the word is
    absent from `_MONEY_SUPERLATIVE`) and "**higher** … : A or B?" (CROSS-5). Additive fix;
    `tests/test_cross_scheme_money.py` is the lock.
  - **KI-215 (Medium)** — CROSS-2 routes as a one-scheme set, so the `_CROSS_SCHEME` prompt block
    is omitted outright (measured `cross_block=False`).
  - **KI-216 (UNKNOWN, High if confirmed)** — if `v_cross_scheme_money_district_year` already
    carries Focus Plus, `_CROSS_SCHEME_MONEY_SQL` **double-counts** it. Settle with
    `SELECT DISTINCT scheme_code FROM curated.v_cross_scheme_money_district_year`.
- **Raw baseline is in the report** and is what the DB/bot columns must be checked against.
  Both PII partitions were **aggregated only, no row printed** (KI-022).
- **The framing finding, worth raising with the officers before these become acceptance
  criteria:** "beneficiary count" is not a defined quantity across these schemes. CM Elevate has
  **no money column at all**, so CROSS-4/5/10/11/19/20 are *impossible* as asked, not hard;
  Focus Legacy has **no person record of any kind**, so the beneficiary cases can only answer in
  groups (11,906) or memberships (102,021); and **no two schemes share a full year window**. A
  pass means stating the mismatch and giving each scheme's own figure side by side — one blended
  total is wrong however well phrased.
- **Next session, in this order:** (1) fix KI-212 and KI-214, or 10 cases re-test unchanged;
  (2) settle KI-216 with the one query; (3) run the live DB and bot columns per §6 of the report;
  (4) re-run `tests/live_context_validation.py`, since KI-212 touches routing.

**Latest (2026-10-09): KI-210 + KI-211 — the "Use cases" and "Glossary" modals. UI only, code changed, uncommitted.**
- **User report:** the Use cases modal listed the example questions, but clicking one did nothing.
- **Cause:** `openUseCases()` wrote `onclick="closeModal(); ask(${JSON.stringify(q)});"`.
  `JSON.stringify` emits a real `"`, which closes the `onclick="` attribute, so the browser
  parsed the invalid fragment `closeModal(); ask(`. All 14 rows were dead, not just some.
- **Fix** (`web/ai_query.html`): the question now travels in `data-ask="${escapeHTML(q)}"` and
  `bindModalAsk()` attaches the handler with `addEventListener` after the rows are in the DOM —
  the same pattern, and the same warning comment, that `bindRichActions()` already carried one
  screen up. Rows are `role="button" tabindex="0"`, answer Enter/Space, and show a `→` on hover.
- **Verified offline** (no VPN needed — this is browser-side only): all 14 questions round-trip
  byte-exactly through `data-ask`, the old form parses to invalid JS for every one, both inline
  `<script>` blocks pass `node --check`, and no `onclick="…JSON.stringify…"` is left in the file.
- **KI-211, the same file:** **all three** hand-maintained lists covered only **3 of the 7
  schemes** (MGNREGA, PMAY-G, FOCUS+) — `USE_CASES`, `GLOSSARY` and the sidebar
  `SAMPLE_CATEGORIES`. Focus Legacy, CM Elevate, CM Elevate Legacy and NRLM had no examples and
  no glossary terms.
  - **Use cases:** now **34 rows in 8 sections**, questions lifted from the officers' own
    use-case files with `[district]` / `[block]` replaced by real places.
  - **Glossary:** now **49 terms in 7 sections** (was 15 in 3), defined from
    `app/schema_context.py` `SCHEME_METRICS` + `docs/SCHEMES.md` so it states what the pipeline
    enforces, traps included (Focus Legacy payments vs groups vs memberships; CM Elevate has no
    money and no year; NRLM `COUNT(*)` = groups and RF/CIF are cumulative undated grants).
    Every figure quoted was checked against its source.
  - Scheme rules respected throughout: no bare "Focus" / bare "CM Elevate", CM Elevate
    COUNT-only, no NRLM RF/CIF in a financial year.
- **Routing checked, not eyeballed:** all 34 questions through `pipeline._shortcut_scheme` →
  **34/34 land on the scheme of the section they are filed under**.
- **Tests:** new `tests/test_ui_use_cases.py` (**52**), invariants not wording — it covers both
  modals. Proven to bite by reintroducing each defect (inline handler → 3 fail; NRLM use-case
  section renamed → 8 fail; a Glossary section renamed → 3 fail).
  pytest **1,777 passed, 2 skipped** (33 files); plain scripts **14/14**. Live context suite NOT
  re-run — no routing, rewrite or state change, and no Python touched.
- **Dead code found, left alone:** the sidebar chip list never renders — its container
  `queriesContainer` exists nowhere in the page (true in `HEAD` too), so `loadSampleQuestions()`
  returns at its guard. Updated for consistency and moved off its own fragile inline `onclick`,
  but reviving that sidebar is a separate decision for whoever owns the layout.
- **Rule for the next person:** any NEW modal row or chip must use `data-ask` / `data-chip-ask`
  plus `addEventListener`, never an inline `onclick` built by string interpolation. And adding a
  scheme means adding a section to **`USE_CASES`, `GLOSSARY` and `SAMPLE_CATEGORIES`** — the test
  suite fails if any scheme in `SCHEME_CATALOG` is missing from the first two.

**Latest (2026-10-08): KI-199 to KI-209 — the exhaustive run. All geography levels GREEN. Code changed, uncommitted.**
- **Scope:** every block, constituency, village and SHG — 45,728 questions, each expected answer
  read from `curated.v_nrlm` AND cross-checked against the raw CSV.
- **Complete and clean on the final code:** districts **12/12**, blocks **56/56**,
  constituencies **55/55**, villages **4,976/4,976**. SHG lookups still running
  (~5,000 done, all passing).
- **Ten defects found and fixed (KI-199 … KI-209)**, every one a silent wrong number or an
  endless thread — never a visible error. See KNOWN_ISSUES for each.
- **The newest and most instructive is KI-209:** after 1,645 consecutive SHG lookups passed,
  60 failures began at **exactly `shg_code = 1900`**. `_YEAR_RANGE_TOKEN_RE` matches a bare
  `19xx`/`21xx` as a financial year, so the CODE failed the year-range check and killed the
  question — about 200 of the 40,629 SHGs. Fixed with `_year_tokens_in()`, which skips a number
  the question introduces as a code/id. **This is the argument for exhaustive testing over
  sampling:** the harness walks codes in order, so the whole broken band sat together; a random
  sample would likely have missed it.
- **Four of the ten trace to one root:** the LLM mention-extractor dropping or mis-slotting the
  village when a question names BOTH a village and a block (village=None on 6 of 8 identical
  calls). Anything depending on `mentions["village"]`/`["block"]` needs a deterministic backstop.
- **Independent re-verification:** all 9,590 PASS verdicts were re-checked against the expected
  figures with a separate matcher — 0 disagreements. DB vs raw CSV across 9,717 tested cases —
  only the 4 known KI-196 rows differ, which is the source-file defect, not a pipeline fault.
- **Tests:** pytest **1,711 passed, 2 skipped** (32 files); plain scripts **14/14**.
- **Running the rest:** `bash scratchpad/nrlm_qa/run_forever.sh` — resumable, skips ids already
  in `bulk_results.jsonl`, pool and concurrency per docs/TESTING.md.
- **Three method traps, each cost time:**
  1. Workers started before a fix keep writing stale results. **Always drop every non-PASS and
     re-run before diagnosing** — several "failures" were already fixed.
  2. A single year chip returns a correct SUBTOTAL (JAIAW PDENG: 12 total = 6+6 across two
     years). Answer year pauses with "all years" when the expectation is a total.
  3. KI-206 reproduced **only at concurrency**; single-case re-runs passed every time.
- **The VPN drops roughly hourly.** Its errors are `ConnectError` / `ConnectTimeout` /
  `DatabaseUnavailableError` — connectivity, never logic. Clear them and re-run.

**Latest (2026-10-07, exhaustive): KI-199 to KI-204 — all blocks / constituencies / villages / SHGs. Code changed, uncommitted.**
- **What was asked:** test every block, constituency, village and SHG, and fix whatever fails.
- **Scope:** 12 districts + 56 blocks + 55 constituencies + 4,976 villages + **40,629 SHGs**
  = **45,728 questions**, each driven the way the UI drives it (follow a chip, keep going) and
  each expected answer read from `curated.v_nrlm` AND cross-checked against the raw CSV. The
  only DB-vs-CSV differences are the 12 known KI-196 rows.
- **Districts 12/12, blocks 56/56, constituencies 55/55 passed first time.** Villages and SHGs
  exposed **six** distinct defects, every one a silent wrong number or an endless thread:
  - **KI-199** a block literal (name OR `block_lgd_code`) beside a resolved `village_code`
    zeroed the answer — 23 SHGs reported as 0; `block_lgd_code = 656` was a DISTRICT code.
    21 of the first 30 village failures.
  - **KI-200** a village question that NAMED its block still asked which village, and looped.
  - **KI-201** the BLOCK name was resolved as the village ("Rongram" -> village "Rongram
    Bazar"), answering 9 for a village that holds 8.
  - **KI-202** the village scan backstop never ran for questions that SAY "village", which is
    exactly when the extractor drops the village name.
  - **KI-203** an SHG question was answered from **Focus Legacy** ("No producer group named
    'SHG code 7194'") although v_nrlm holds it.
  - **KI-204** a named SHG was then asked for a district and a year, though `shg_code` is unique.
- **Files:** `app/pipeline.py` (`_GEO_BESIDE_VILLAGE_RE`, `_VILLAGE_CODE_SCHEMES`,
  `_block_named_for_village`, the village-slot clear, the scan-backstop gate, the Focus Legacy
  group-name pull, `_NRLM_SHG_CODE_RE` + both clarification gates); new tests
  `tests/test_village_block_narrowing.py` (24) and `tests/test_nrlm_shg_lookup.py` (28).
- **Tests:** pytest **1,662 passed, 2 skipped** (32 files); plain scripts **14/14**.
- **The bulk run is still going** (`scratchpad/nrlm_qa/bulk_run.py`, resumable — it skips ids
  already in `bulk_results.jsonl`, so just re-run it after a VPN drop). At the last check
  **1,482 / 1,482 passing** on the final code. Re-run any non-PASS at the end: results written
  before a fix landed are stale, not real failures.
- **Watch out for:** four of the six defects were the SAME underlying thing — the LLM
  mention-extractor dropping or mis-slotting the village on a question that names a village AND
  a block. It is non-deterministic (village=None on 6 of 8 identical calls), so anything that
  depends on `mentions["village"]` or `mentions["block"]` needs a deterministic backstop.

**Latest (2026-10-07, final): KI-198 — an OFFERED year chip was refused as out of range; everything looped. Fixed.**
- **Reported:** "it is looping again and again". A loop sweep that drives the pipeline the way
  the UI does (follow a chip, keep going, flag any thread that re-asks what it already asked)
  found **8 of 58 paths looping**, all NRLM: "How many SHGs are in Chokpot", "How many SHGs are
  there?", "total CIF in Chokpot", "How many members are there in NRLM".
- **Cause:** each paused for a year, offered **FY 1984-85** (NRLM's earliest formation year),
  and then answered *"data is available only for the financial years 1984-85, …"* — refusing a
  year out of its own list. `_parse_year_key` only understood **2010-2039**, and NRLM is the
  first scheme whose data starts before 2010 (1984-85 … 2022-23).
- **Fixed:** an EXPLICIT `NNNN-NN` range parses from 1900 on (the pair is unambiguous); a BARE
  four-digit number stays 2010-2039, so "top 2000 villages" / "the 1984 census" are still not
  years. Note `refresh_scheme_years()` already self-corrects the year LIST at startup
  (NRLM 26 -> 30 live) — the parser was the only defect.
- **Also fixed, found while verifying:** those chips reach genuinely empty cells (Chokpot in
  FY 1984-85 really is 0 — one SHG statewide was formed that year, in West Garo Hills) and the
  answer read "the data available doesn't cover the count of SHGs". `_nrlm_counted_zero_answer`
  now states the measured zero; money columns keep their unit ("has received Rs 0 of CIF").
  This is KI-194's reasoning applied to the non-empty path.
- **Files:** `app/pipeline.py` (`_parse_year_key`, `_nrlm_counted_zero_answer` +
  `_NRLM_COUNTED_METRIC` / `_NRLM_ZERO_MONEY`), `tests/test_year_chip_no_loop.py` (new, 36).
- **The new test is an invariant, not a symptom check:** for every scheme in `SCHEME_CATALOG`,
  every year chip the pipeline offers must parse AND be in range. Scheme eight gets it free.
  Verified by narrowing the parser back: 11 tests fail; with the fix, all pass.
- **Tests:** pytest **1,638 passed, 2 skipped** (30 files); plain scripts **14/14**; NRLM use
  cases **43/43**; loop sweep **258 paths, 0 loops, 0 dead ends** (4 transient VPN timeouts,
  each answering on retry).
- **If you add a scheme with an unusual year range,** check `_parse_year_key` as well as
  `_SCHEME_DATA_YEARS`: the year LIST self-corrects from the live DB, the PARSER does not.

**Latest (2026-10-07, last): KI-197 — NRLM was refused as an unsupported scheme and the chip LOOPED. Fixed.**
- **Reported by the user**, with a screenshot: "List all SHGs registered under in Chokpot for
  NRLM" -> *"NRLM isn't one of the schemes I cover…"*, offered an **NRLM (Self Help Groups)**
  chip, and tapping it produced the identical refusal, for ever.
- **Cause:** `pipeline._UNSUPPORTED_SCHEME` still carried `nrlm|day-nrlm|aajeevika|ajeevika|
  livelihoods mission` from before NRLM was onboarded (2026-10-06). The comment above that
  pattern already stated the rule ("CM Elevate is now a supported scheme … must NOT appear
  here") — NRLM was simply never removed. The loop came from `_unsupported_scheme_named`
  blanking only the FIRST match, so the one-mention chip the UI offers always re-matched.
- **Fixed in three parts:** aliases removed; the refusal text and the three `edge.py` coverage
  replies (`_OUT_OF_SCOPE_REPLY`, `greeting`, `identity`, and the out-of-area reply that said
  "these six schemes") now name all seven; and a structural guard,
  `tests/test_supported_scheme_not_refused.py` (25 tests), which walks `SCHEME_CATALOG` and
  `_SCHEME_NAME_PATTERN` and fails if any alias of a loaded scheme is in `_UNSUPPORTED_SCHEME`,
  and asserts no refusal chip can resume into another refusal. **Verified by reintroducing the
  bug: 8 of the 25 fail; with the fix, all 25 pass.**
- The reported question now answers **517 SHGs in Chokpot block** — equal in `curated.v_nrlm`
  and the raw CSV.
- **For the next onboarding:** `_UNSUPPORTED_SCHEME` is a registry too, and the only one that is
  a *removal* rather than an addition — which is why it was missed. Now listed in
  `docs/SCHEMES.md` §Adding a scheme, step 5, and enforced by the test above.
- **Tests after this fix:** pytest **1,594 passed** (29 files); plain scripts **14/14**; NRLM
  use cases **43/43** live (re-run after the change, since `edge.py` and the refusal path are
  shared).

**Latest (2026-10-07, later): NRLM use-case QA AND fixes. 16/43 -> 43/43. Code changed, uncommitted. LIVE-VERIFIED.**
- **What was done.** Ran the 43 use cases in `NRLM_Use_Cases.xlsx` through the real
  `pipeline.answer_question`, checked every figure against **both** the raw
  `NRLM to share to BLH.csv` (40,629 rows) and `curated.v_nrlm` (40,629 rows), fixed everything
  they found, and re-ran them on the final code.
- **Result: round 1 16/43, final 43/43 live.** The data itself reconciles exactly (34 scalar
  metrics plus the district / block / year / constituency / village breakdowns), so every
  failure was pipeline behaviour.
- **Deliverable:** `NRLM_UseCase_Test_Report_2026-10-07.xlsx` (repo root, gitignored by the
  root `*.xlsx` rule) — Summary, Test Results, Defects, Data Reconciliation, and 43 embedded
  proof images.
- **Files changed:**
  - `app/entity_resolver.py` — `_ACRONYM_VOCABULARY` (KI-189).
  - `app/pipeline.py` — `_nrlm_count_question_listed_rows`, `_nrlm_unrequested_limit`,
    `_nrlm_list_total`, `_deterministic_answer(total=…)`, `compose_response(list_total=…)`
    (KI-188); `_scaled_money_units` (KI-190); `_nrlm_money_year_caveat` (KI-191);
    `_ac_drilldown_clarification` (KI-192); `_KB_DISCLAIMS_RE` + `_data_path_kb_fallback`
    (KI-193); `_nrlm_zero_answer` (KI-194); `_nrlm_name_search_contains` (KI-195);
    `_verifier_year_complaint_is_false` and new `_verifier_nrlm_grain_complaint_is_false`
    (two verifier false positives).
  - `tests/test_nrlm_usecase_fixes.py` — new, 71 tests.
- **Tests:** pytest **1,569 passed** (28 files); plain scripts **14/14**; NRLM use cases
  **43/43** live. `tests/live_context_validation.py` **NOT re-run** — see below.
- **Next step (the one thing left):** re-run `tests/live_context_validation.py` (43/43 is the
  baseline). KI-192 touched a clarification path and KI-193 touched the DATA->KB fallback, so
  the multi-turn suite should confirm them even though every pytest suite is green.
- **Watch out for, if you touch this area:**
  - `_nrlm_count_question_listed_rows` must stay off ranking questions. "Which SHGs have the
    highest **number of** female members" contains the count cue but the rows ARE its answer —
    forcing COUNT(*) made the question stop being answered at all.
  - `_nrlm_list_total` handles BOTH a query with its own LIMIT and one capped only by
    `db.run_readonly`'s `SQL_MAX_RESULT_ROWS`. Dropping an unrequested LIMIT (KI-188) moves a
    query from the first case to the second.
  - `_nrlm_zero_answer` parses the WHERE clause; the model writes multi-line SQL, so a
    condition can end `")
"`. That cost one live round before it was caught.

**Latest (2026-10-07): the accumulated 2026-09-29..10-07 work committed and pushed. No behaviour change in this commit.**
- This session only committed what was already in the working tree (scheme QA fixes KI-182/186/187 and D-032, the
  context/state work, the NRLM onboarding scaffolding, `docs/COMPLETE_ARCHITECTURE.md`, `tools/verify_wiring.py`).
- Tests on the committed tree: pytest **1,498 passed** (26 files), plain scripts **14/14**. Matches the documented
  2026-10-07 baseline. Live suites not re-run: no code changed in this session.
- `.gitignore` now ignores root-level `*.csv` / `*.xlsx` (keeping `Use_Cases_-_Focus.csv`, already tracked). Several
  raw extracts sitting in the repo root hold unmasked beneficiary names, mobile numbers, account numbers and IFSC
  codes, and were never meant to be committed (KI-022).
- `docs/HANDOFF.md` was restored: it was deleted in the working tree although CLAUDE.md requires it every session.
  The CURRENT_STATE conflict note about it is now resolved.
- **NOT committed, left for a decision (still deleted in the working tree):** 762 QA evidence screenshots, 9 `.xlsx`
  test reports and `docs/CM_Elevate_Legacy_DB_Issues.md`. CURRENT_STATE.md and KNOWN_ISSUES.md still cite those
  evidence folders as the record for specific KI numbers, so committing the deletions would break those references.
  Decide whether that evidence is being retired (then update the citing docs in the same commit) or was removed by
  accident (then `git checkout HEAD -- docs/` to bring it back).
- **Pre-existing leak, NOT created here:** `Focus Legacy to share to BLH.csv` (14,569 rows, unmasked `account_no`,
  `ifsc_code`, `name_on_the_account`) is tracked and was already pushed in `7064ab6`. Ignoring it now does not untrack
  it. Removing it needs a history rewrite (`git filter-repo`) plus force-push coordination, and the accounts in it
  should be treated as disclosed. Raised with the user; no action taken without instruction.

**Latest (2026-09-29, morning): typed replies resume every chip pause (KI-181). Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/routers/query.py` (`remember_pause` generic branch, uses `SCOPE_MERGE_RULES`), `app/pipeline.py` (`SCOPE_MERGE_RULES`,
  `_resume_option_pause`, `_option_tokens`, `_paused_thread_antecedent`, step 0a'' in `_run_pipeline`, `_paused_state` thread override,
  measure-gap pause remembered as its first offer), `tests/test_context_relevance_and_contract.py` (+33, now 177).
- Tests: pytest 1,371 passed, 2 failed (same parallel-session verifier tests as below); scripts 14/14; live context 43/43.
- Open: an unmatched reply with its own measure + place but no year keeps the paused scheme, not the paused year (FY pause follows).
- Next: restart :8300; commit when the user asks.

**Latest (2026-09-29, early morning): "give me for pmay" — conversational scheme swap (KI-180). Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/pipeline.py` (`_SCHEME_SWAP_FOLLOWUP` request-verb opening, `_SUBSTITUTION_CUE`, new `_SCHEME_OWN_MEASURES`,
  `_SCHEME_HEADLINE_OFFERS`, `_swap_scope_phrase`, `_swap_measure_gap`, one call after the follow-up rewrite in `_run_pipeline`),
  `tests/test_context_relevance_and_contract.py` (+26, now 144).
- Tests: pytest 1,338 passed, 2 failed — both pin `_verifier_village_code_complaint_is_false` and were broken by a PARALLEL
  CM Elevate Legacy session editing `pipeline.py` at the same time (it added `["CM Elevate Legacy"]` to that gate); not KI-180.
  Scripts 14/14; live context 43/43; live replay via scratchpad `119975e8-…/scratchpad/live_convo.py`.
- Open: a bare "for focus" -> which-Focus chip -> Focus Plus with an MGNREGA-only measure still reaches the generator (not gated).
  (The pause is now remembered — KI-181, entry above.)
- Next: restart :8300; commit when the user asks.

**Latest (2026-09-29, late night): CM Elevate Legacy KI-166..181 fixed — use cases 36/36, all levels 2,614/2,614. Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/pipeline.py` (CM Elevate Legacy SQL guards `_cm_legacy_sanctioned_count`, `_cm_legacy_unrequested_limit`,
  `_cm_legacy_unselected_group_by`; `_cm_legacy_answer_guarantees` and its `_cml_*` helpers; chip pin / twin ranking /
  urban blocks / verifier filter / Garo gate extended to CM Elevate Legacy; KI-179 level word in the explicit-village
  step, all schemes), `tests/test_cm_elevate_legacy.py` (+48 tests, 131).
- Reports: `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29_v2_after_fixes.xlsx`,
  `docs/CM_Elevate_Legacy_AllBlocks_AllVillages_Test_Report_2026-09-29.xlsx` (+ evidence folders).
- Harness: scratchpad `d746a809-…/scratchpad/bulk` — `gen_bulk.py` (truth from DB AND raw, system python),
  `bulk_run.py` (.venv; set BOTH `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=6` — a max below the default min 10 kills the
  pool), `bulk_judge.py`, `build_bulk.py`, `issues.json`; `trace_sql.py` logs failing SQL.
- Tests: pytest 1,309 (26 files), scripts 14/14, live context 41/43 + scenario G 5/5 twice (transient save-failed).
- Next: RESTART :8300; commit when the user asks; KI-003 (failing SQL not logged) made the KI-181 hunt slow.

**Earlier (2026-09-29, late night): CM Elevate Legacy use-case re-test — 33/36. No code changed.**
- The user said "Focus legacy" but named the CM Elevate Legacy files, so CM Elevate Legacy was tested (36 cases, 2 full runs + 3 repeats).
- DB = raw row for row (2,823). Failures, all AI layer and reproducible: KI-166 (TC-14 sanctioned = COUNT(*) → 2,823 vs 2,820, 5/5),
  KI-167 (TC-34 composer "20 villages with 1 record" vs 24, 5/5), KI-168 (TC-13 LIMIT 10 → "down to 33", 4/5).
- Report `docs/CM_Elevate_Legacy_UseCase_Test_Report_2026-09-29.xlsx` (+ `_Evidence_2026-09-29/`).
- Harness: scratchpad `d746a809-…/scratchpad` — `run_bot.py OUT [TC…]` (.venv), `ground_truth.py` and `recon.py` (system python; the raw
  file is aggregated only — reading its rows is blocked as PII), `judge.py`, `verdicts.py`, `fails.py`, `build.py`.
- Next: fix KI-166..168 with deterministic guards + regression tests (user has not asked yet), then re-run the 36.

**Latest (2026-09-29, night): Focus Legacy all blocks / villages / ACs / PGs — 32,560/32,560. Code changed, uncommitted. LIVE-VERIFIED.**
- Files: `app/pipeline.py`, `app/entity_resolver.py` (`constituency_contents(ac, scheme)`), `app/edge.py`
  (`_mask_group_name`), `tests/test_focus_legacy_usecase_fixes.py` (122), 3 older tests updated with reasons
  (`test_admin_level_collision.py` fake signature, `test_mgnrega_usecase_fixes.py` verifier scope,
  `test_ac_flow_focus_legacy.py` docstring).
- Tests: pytest 1,265; scripts 14/14; live context 43/43; use cases 28/28 x3 (final code).
- Reports: `docs/Focus_Legacy_AllBlocks_Villages_ACs_PGs_Test_Report_2026-09-29.xlsx`,
  `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29_v2_after_fixes.xlsx` (+ evidence folders).
- Harness: scratchpad `2ca0226e-…/scratchpad/bulk` — `gen_bulk.py` (truth from DB AND raw), `bulk_run.py`
  (concurrent, resumable, clicks chips), `bulk_judge.py`; run shards in parallel with `DB_POOL_MAX_SIZE=5`.
- Next: CM Elevate Legacy duplicate-village chips (KI-156) and PMAY-G DATE_TRUNC (KI-164) — both NOT VERIFIED;
  KI-151 under load; RESTART the :8300 server to pick the changes up; commit when the user asks.

**Latest (2026-09-29, evening): Focus Legacy use-case re-test — 28/28 on 3 of 3 runs. No code changed.**
- Checked against the live DB (`curated.v_focus_legacy`, never the privacy table) and the raw file;
  DB = raw row for row. Report `docs/Focus_Legacy_UseCase_Test_Report_2026-09-29.xlsx` (+ `_Evidence_2026-09-29/`).
- New cosmetic issues KI-145..147 (open). Harness: scratchpad `2ca0226e-…/scratchpad`
  (`bot_run.py`, `judge.py`, `expected.py`, `recon.py`, `verdicts.py`, `build.py`; Excel COM for recalc).

**Latest (2026-09-29, later): the reported "what is focus" conversation — KI-136 to KI-144. Code changed, uncommitted. LIVE-VERIFIED (VPN up).**
- Fixes: `_names_bare_focus_scheme` (no model rewrite for a bare scheme "Focus"); `_drop_inherited_time` /
  `_drop_inherited_place` (KNOWLEDGE path); `rewrite_violation` keeps the follow-up's own metric;
  `_followup_thread_state` + `_data_thread_antecedent` (which thread a follow-up continues — never a
  state wipe, see D-030 amendment); `_measure_after_knowledge` + `_typed_intent`;
  `turn_context["standalone_question"]` read by `routers.query.pause_question`;
  `ConversationState.last_dimension` + "all of them" CLEAR + `_all_years_rewrite`; Focus Plus
  amount answer guarantee; `build_followup_context(state=...)`.
- Tests: pytest 1,185; scripts 14/14; live context suite 43/43 (final code); live battery S1–S8 correct.
  Harness: scratchpad `5a4188f1-…/scratchpad/live_convo.py` (real lifespan, router pause helpers;
  "@chip:<label>" clicks a chip), `battery.sh`, `battery2.sh`.
- Open: KI-040 (MGNREGA "beneficiaries" = SUM(persons_employed) across years, person-years — needs an
  SME definition); after 2+ edge turns a bare "how many beneficiaries?" asks "which scheme?" (safe, by
  choice); bulk false-positive rate of `_resolved_scope_missing` not measured.
- Next: commit when the user asks; a Focus Plus / MGNREGA block-village bulk sample to measure the guard.

**Earlier (2026-09-29): context relevance + semantic contract (D-030). Offline at the time; live-verified by the later session.**
- The VPN was down all session (`10.48.242.4` answered neither 5432 nor 443), so **nothing was
  live-verified**. FIRST NEXT STEP when it is up: `tests/live_context_validation.py` (43 checks),
  then the reported conversation live (see KNOWN_ISSUES KI-130 to KI-135), then a Focus Plus and a
  MGNREGA block/village regression sample to measure `_resolved_scope_missing` false positives.
- Changed: `app/context_policy.py` (`continuation_signals`, `is_bare_reference`),
  `app/pipeline.py` (step 0-c gate, `reference-ambiguous`, step 1g `_village_name_search_answer`,
  `_acronym_near_miss_clarification`, Focus Plus `_focusplus_stated_amount*`,
  `_resolved_scope_missing` guard, `inject_year_scope` call, decision logs), `app/edge.py`
  (`personal_request`, `has_domain_vocabulary`), `app/entity_resolver.py`
  (`acronym_near_misses`), `app/premise_check.py` (`stated_amount_filters`, number words),
  `app/context_manager.py` (`inject_year_scope`, KI-032 clears, `year_all`),
  `app/session_store.py` (`year_all`), `app/context_budget.py` (`log_decision`); new
  `tests/test_context_relevance_and_contract.py` (96).
- Tests: pytest 1,163 (26 files); scripts 14/14. Offline harness:
  scratchpad `5a4188f1-…/scratchpad/repro_routing.py` (real `_run_pipeline`, model/RAG/DATA stubbed).
- Not fixed, still open: KI-035, KI-040 (MGNREGA "beneficiaries" is undefined, which is what
  "same for mgnrega" will answer with), KI-036/037 (Songsak-style block/village level memory; tests
  5–7 of the request rely on the existing level gates and were not live-checked).

**Latest (2026-09-29, later): PMAY-G plain year = calendar year.** "during 2017" now answers calendar 2017 (EKH 1,419 houses; table + SQL agree) with FY 2017-18 (1,548) as a one-line note; explicit FY unchanged. pytest 1,266; use cases 71/71; follow-ups 18/18. Restart :8300.

**Latest (2026-09-29): KI-129 — PMAY-G result table / Sources.** The table under an answer now holds only the place and the asked figures (`_pmay_display_rows`); "Sources: TRUE" gone (SQL + UI). Live: follow-ups 18/18, use cases 71/71; pytest 1,067. Restart :8300.

**Latest (2026-09-28, night): PMAY-G extra information removed (KI-128).** Summaries list only the use-case figures; single figures carry no side details; rewritten follow-ups answer only the figure the user typed (`_PMAY_TYPED_TURN`). Live: 2,129-question sample 2,129/2,129, use cases 71/71, follow-ups 18/18; pytest 1,065. Restart :8300.

**Latest (2026-09-28, late): PMAY-G tester sheet (Test Case Results - 21st Sep 26 - PMAY-G) re-tested.** Every input the tester named is now correct vs DB/raw (GABIL SONGGITCHAM partial 83; no KYNDONGTUBER mix-up; one row per status; LASKEIN 5,232). Live: 192/192 (21 Sep inputs, all districts x ben/summary/performance/each FY, 63 dates) + use cases 71/71. Fixed KI-125 (bare year "sanctioned in 2023" was dropped → now FY 2023-24 + calendar-2023 line). Open decisions: KI-126 (two taps for a shared village name) and KI-127 (placeholder rows in beneficiary counts: tester's 5,251 vs 5,232).

**Latest (2026-09-28, later): CM Elevate status = current_file_status (KI-123, user decision).** Prompt rule 13 + vocabulary, 2 few-shots, guard `_cm_elevate_status_is_file_status`. Pending / verification / on hold unchanged. Tester questions 36/36; pytest 1,062.

**Previous (2026-09-28, late): CM Elevate tester sheet (Test Case Results - 21st Sep 26) re-tested.** 36 tester-phrased questions all correct vs DB/raw after two fixes (KI-121 verifier grain false positive, KI-122 sector wording); pytest 1,055. Open decisions: KI-123 (testers want current_file_status for status/pending) and KI-124 (applicants = distinct request_id vs raw row count).

**Latest (2026-09-28, evening): CM Elevate — remaining issues + all districts / blocks / villages. Code changed, uncommitted.**
- Final pass **7,364 / 7,364** (7,364 questions = 12 districts, 66 blocks, 2,087 villages x 3 phrasings x the use-case types); round 1 4,132/4,208. Report `docs/CM_Elevate_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx`.
- KI-074 DECIDED by the product owner: pending = verification On Hold only (interim line removed). KI-076 fixed (`_APPLICANT_PLACE_CUE`).
- KI-106..120: CM Elevate in `_village_scheme` / `_VILLAGE_NARROW_SCHEMES` (village catalogue `_cm_elevate_village_names`); urban bodies mapped to stored `lgd_block` (`_cm_elevate_blocks_to_data`, `_cm_elevate_block_from_mention`, urban step at the top of `resolve_entities`); SQL guards `_cm_elevate_fix_literals`, `_cm_elevate_sector_all_programmes`, `_cm_elevate_unasked_programme_filter`; `_focusplus_pin_village_where` literal/FILTER-aware (`_top_level_where_span`); chip pin keeps the scheme's twin; verifier filters; number words → digits before the checks.
- Three older tests that pinned CM Elevate OUTSIDE the village gate were updated (reason in each).
- Tests: pytest 1,053; context suite 43/43. Harness: scratchpad `63ff2b45-…/scratchpad/bulk` (gen_bulk, bulk_run, bulk_judge, make_extra, build_bulk). RESTART the :8300 server.

**Latest (2026-09-28, late night): PMAY-G full scenario test — 17,331/17,331. Code changed, uncommitted.**
- 34 first-pass failures → KI-099 to KI-104 fixed; the final pass found KI-105 (model-path crore rounding), fixed and the
  571 model-reachable questions re-run 571/571. Follow-ups 18/18, use cases 71/71, context 43/43, pytest 1,005.
- Report `docs/PMAY_G_Full_Scenario_Test_Report_2026-09-28.xlsx` (+ `_Evidence_2026-09-28/`). RESTART the :8300 server.

**Previous (2026-09-28, night): KI-098 — PMAY-G village named beside its block. Code changed, uncommitted.**
- User report: "…in NONGSOHRAM across all financial years, RI MULIANG block, WEST KHASI HILLS" answered for
  the whole block. Fix: `_pmay_village_beside_block` at the end of `resolve_entities` (PMAY-G only; reads
  an explicit ", X block, DISTRICT" tail from the text; one mention written "X block" stays the block).
- Live: 5,120/5,120 villages with the tail, 224/224 blocks, 500/500 bare villages, 71/71 use cases;
  pytest 998. The running server on :8300 must be RESTARTED to pick the change up.

**Previous (2026-09-28, evening): PMAY-G fixes + all blocks / all villages. Code changed, uncommitted.**
- **Result:** use cases **28/28** (71/71 questions on 2 of 2 fresh live runs, identical answers; round 1 was 17/28); all blocks **224/224**; all villages **5,120/5,120** on the final code (round 1 4,393/5,120 → KI-096 fixed).
- **Design (D-029):** `_pmay_facts_query` / `_pmay_facts_answer` answer the fixed PMAY-G shapes
  deterministically; the model path keeps the rest with `_pmay_sql_issue`,
  `_pmay_comparison_limit`, `_pmay_rupee_format`. PMAY-G is now in `_village_scheme`
  (`_pmay_village_names`). Resolver: "sanctioned" stage phrases match in order only.
- **Tests:** `tests/test_pmay_usecase_fixes.py` (53); pytest 993; scripts 14/14; live context 43/43;
  Focus Plus regression 60/60 + 300/300; MGNREGA 60/60 + 300/300. Four older tests that pinned
  PMAY-G outside the village gate / out-of-area check were updated, with the reason in each.
- **Reports:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28_v2_after_fixes.xlsx` (+ `_Evidence_…_after_fixes/`),
  `docs/PMAY_G_AllBlocks_AllVillages_Test_Report_2026-09-28.xlsx` (+ `_Evidence_2026-09-28/`).
- **Next step:** commit when the user asks. Keep the FY pause (product decision). The harness is in
  scratchpad `9d9b9d7b-…` (gen_bulk, bulk_run, bulk_judge, build_bulk, run_bot, judge, verdicts_after,
  build_xlsx ROUND=2).

**Previous session (2026-09-28, later): PMAY-G use-case QA. No code changed.**
- **Result:** **17 / 28 test cases** (49 / 71 questions; two live runs; a question passes only if
  both pass). The user said "Focus legacy" but named PMAY-G files (`PMAY-G.csv` = use cases,
  `PMAY_FullyMapped_with_dates.csv` = raw), so PMAY-G was tested.
- **DB = raw** row for row (171,107 rows).
- **Report:** `docs/PMAY_G_UseCase_Test_Report_2026-09-28.xlsx`, evidence
  `docs/PMAY_G_UseCase_Evidence_2026-09-28/` (73 PNGs; 016a and 022b also have `_runB`).
- **Issues found:** KI-089 to KI-095 (all fixed later the same day — see the session above). The biggest was KI-089
  (per-year GROUP BY for "financial summary" and comparisons), then KI-091 (false "House
  Sanctioned stage").
- **Not a failure:** the FY pause on no-year PMAY-G questions is a 2026-09-09 product decision.
- **Next step:** fix KI-089 to KI-095 (PMAY-G only, prompt + few-shot + deterministic guard), add
  `tests/test_pmay_usecase_fixes.py`, re-run the 71 questions.
- **Harness:** session scratchpad `9d9b9d7b-…/scratchpad` holds `cases.py`, `frames.py`,
  `recon.py`, `expected.py`, `run_bot.py`, `judge.py` (money within stated rounding and 1%),
  `verdicts.py`, `render.py` and `build_xlsx.py`. The runner and renderer use the `.venv` python;
  pandas and openpyxl steps use the system python. Raw names are dropped at load.

**Previous session (2026-09-28): KI-025 fixed; data package for KI-065 / KI-084. Code changed, uncommitted.**
- **KI-025:**
  - `db.DatabaseUnavailableError` and `db.is_connection_error()` (connection-class errors; not
    TimeoutError);
  - `execute_with_repair` raises it before any repair;
  - `_run_pipeline` re-raises it, with no KB fallback;
  - the router returns 503.
  - Tests: 3 in `tests/test_focusplus_usecase_fixes.py`. Live-verified with a real unreachable
    DB port.
- **KI-065 / KI-084:** data-side, so the chatbot must not change megh_db. The ingestion team has
  the row-level package `docs/Focus_Plus_Data_Corrections_for_Ingestion_2026-09-28.xlsx`
  (README, rows, per-village summary, read-only verification SQL). Next: their decision. No
  chatbot change is needed after they fix it.
- **Tests:** pytest 939; scripts 14/14; live context 43/43.

**Previous session (2026-09-27 night): Focus Plus all blocks and all villages. Code changed, uncommitted.**
- **Result on the final code:** blocks **204/204**; villages **7,026/7,026**.
  - Confirmation pass: 204/204 and 1,000/1,000 random villages.
  - MGNREGA regression: 448/448 and 300/300.
  - Report: `docs/Focus_Plus_AllBlocks_AllVillages_Test_Report_2026-09-27.xlsx`.
- **Fixes (all in `app/pipeline.py`; KNOWN_ISSUES KI-079 to KI-088; D-028):**
  - The MGNREGA village guards (KI-049 to KI-059) are gated on `_village_scheme(schemes)`, which
    returns MGNREGA exactly as before, or Focus Plus when it is the only scheme.
  - Focus Plus adds `_focusplus_village_names`, `_focusplus_narrow_village`,
    `_focusplus_pin_village_where` and `_focusplus_alias_district_collision`.
  - The twin-village chip tag is `_village_code_tag`.
  - Also: the "Nan" block, BURMA, and the one-figure misquote rule in `compose_response`.
- **Tests:** `tests/test_focusplus_usecase_fixes.py` (53). Two MGNREGA tests that pinned
  MGNREGA-only gates were updated, with the reason in the test.
- **Another session edited the same files in parallel** (CM Elevate, calendar dates: KI-068 to
  KI-078, D-027). Numbering was coordinated by reading the docs first.
- **Next step:** commit when the user asks. Remaining data-side items for the ingestion team are
  KI-065 and KI-084. The bulk harness (`gen_fp_bulk.py`, `fp_bulk_run.py`, `fp_bulk_judge.py`,
  `build_fp_bulk.py`) is in the session scratchpad; the method is on the report's Summary sheet.

**Earlier (2026-09-27): calendar date read as a financial year (KI-078). Code changed, uncommitted.**
- **The user's screenshot:** "How many PMAY houses were sanctioned on 2017-11-28?" gave 504
  (correct, checked in megh_db), but the answer said "not the 11 or 28 figures assumed" and
  "applies to FY 2017-18".
- **Fixed (only this; nothing else changed):**
  - `app/pipeline.py`: `_EXPLICIT_FY_RANGE_RE` no longer matches the head of a date;
    `_needs_scope_clarification` accepts a full date as the time pin (`_CALENDAR_DATE_RE`).
  - `app/premise_check.py`: `extract_premises` blanks dates (`_DATE_RE`).
- **Tests:** new `tests/test_calendar_date_filter.py` (18); pytest 915 (24 files); scripts 14/14;
  live context suite **43/43** (follow-up 29/29, SQL 20/20); live probes 4/4.
- **Next:** commit when the user asks.

**Earlier session (2026-09-27): MGNREGA women-share fix (KI-077). Code changed, uncommitted.**
- **The user's screenshot:** "What percentage of employment persons were women in ekh?" came back
  as 0.00%, with no year named.
- **Fixed in `app/pipeline.py`, MGNREGA only:**
  - the year gate now covers percentage, share and women questions, and women chips offer only
    the years that carry women data;
  - a deterministic women query uses the recorded years and says which;
  - FY 2025-26 is answered "not recorded";
  - generator SQL over all years is restricted to the recorded years.
- **Details:** KNOWN_ISSUES KI-077 and AI_PIPELINE.
- **Tests:** 897 pytest, 14/14 scripts, live context 43/43, 8/8 live women checks.
- **Next:** commit when the user asks.

**Earlier session (2026-09-27, later): CM Elevate use-case QA, then fixes. Code changed, uncommitted.**
- **Result:**
  - Round 1: 23 / 30 test cases (47 / 55 questions).
  - After fixes: **30 / 30** (55 / 55) on 2 of 2 fresh live runs, with identical answers.
  - Every figure was checked against megh_db and the raw workbook, which are identical (8,627
    rows).
  - The user said "Focus legacy" but named CM Elevate files, so CM Elevate was tested.
- **Reports:**
  - round 1: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27.xlsx`;
  - after fixes: `docs/CM_Elevate_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`, with
    evidence in `..._Evidence_2026-09-27_after_fixes/`. It keeps the round-1 status as a column.
- **Files:**
  - `app/pipeline.py`:
    - `_UNRESOLVED_PLACEHOLDER_SCHEMES` now includes CM Elevate;
    - `_CMELEVATE_ONLY_TERMS` now includes PRIME SEED;
    - five `_cm_elevate_*` SQL rewrites run in `execute_with_repair`;
    - the `_cme_*` guarantees run through `_cm_elevate_answer_guarantees` after the composer;
    - new verifier filter `_verifier_scheme_specific_complaint_is_false`.
  - `app/schema_context.py`: the CM Elevate vocabulary now covers pending at a level, status by
    sector, approved/rejected on `file_status`, and the `file_status` key.
  - `tests/test_cmelevate_usecase_fixes.py` (33 tests).
  - Docs: AI_PIPELINE §2.8 and §2.9, D-027, KNOWN_ISSUES KI-068 to KI-076, TESTING, SCHEMES,
    CURRENT_STATE, and the CLAUDE.md baseline.
- **Tests:** pytest 891 passed; scripts 14/14; live context suite 43/43. 11 reworded probes gave
  10 correct and 1 intent flake (KI-076, pre-existing).
- **Scope:** every change is gated on `schemes == ["CM Elevate"]`. The only shared-code
  touches are the Unresolved-scheme tuple and the CM Elevate term regex. No YAML was edited.
- **Next step:**
  - Commit when the user asks.
  - **Ask the product owner which "pending" is wanted (KI-074)**: verification On Hold (953)
    or file status Pending (8,472). If they choose file status, change the vocabulary line and
    drop `_cme_pending_file_status`.
  - Consider KI-076.
- **Harness:**
  - the scratchpad `63ff2b45-…/scratchpad` holds cases, expected, judge, run_bot, render,
    build_xlsx (`ROUND=2`), verdicts and `verdicts_after`, plus the `probe/` folder;
  - run passes sequentially with `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=6`;
  - read every answer, because the judge misses grouped lists and top-3 answers.

**Latest session (2026-09-27, continued): Focus Plus fixes. Code changed, uncommitted.**
- **Result: 30 / 30** (39 / 39 questions) on 2 of 2 fresh live runs after fixing KI-060 to
  KI-064, plus two bugs found along the way:
  - KI-066: in all schemes, a typed reply that resumed a pause and then paused again was
    remembered as the bare reply;
  - KI-067: a Focus Legacy bank question raised `KeyError`.
- **Report:** `docs/Focus_Plus_UseCase_Test_Report_2026-09-27_v2_after_fixes.xlsx`, with
  evidence in `docs/Focus_Plus_UseCase_Evidence_2026-09-27_after_fixes/`. It keeps the round-1
  status as a column.
- **Files:**
  - `app/pipeline.py`: `_focusplus_answer_guarantees` and its `_fp_*` helpers,
    `_fix_digit_grouping`, `_sql_literal_numbers`, `_bank_clarification` (chips and a Focus
    Legacy entry), `SCHEME_PAUSE_RULES`, `_needs_scope_clarification`, and turn_context
    `resumed_question`;
  - `app/routers/query.py`: `pause_question`;
  - `tests/test_focusplus_usecase_fixes.py` (32 tests);
  - docs: AI_PIPELINE §2.9 / §5.2, D-026, KNOWN_ISSUES, TESTING, SCHEMES, CURRENT_STATE and
    CLAUDE.md baselines.
- **Tests:** pytest 858 passed; scripts 14/14; live context suite 43/43.
- **Scope:** the answer guarantees are gated on Focus Plus. The digit-grouping fix, the
  SQL-literal numbers and `pause_question` apply to every scheme (D-026 explains why).
- **Next step:** commit when the user asks. Consider extending `_fp_comparison` and the
  shares to other schemes if their QA shows the same gaps. KI-065 is waiting on the data team.

**Earlier the same day: Focus Plus use-case QA, round 1. No code changed.**
- **Result: 19 / 30 test cases pass** (27 / 39 questions). Two live runs agreed on every verdict.
  Every figure was checked against megh_db and the raw `Focus Plus Master.csv`.
  - The user said "Focus legacy" but named the Focus Plus files, so Focus Plus was tested.
- **Report:** `docs/Focus_Plus_UseCase_Test_Report_2026-09-27.xlsx`:
  - sheets Summary, Test Results, Question Detail, DB vs Raw and Evidence;
  - the Evidence sheet embeds `docs/Focus_Plus_UseCase_Evidence_2026-09-27/*.png`.
- **New open issues:**
  - KI-060: no percentages;
  - KI-061: no difference or "which is higher";
  - KI-062: the composer printed "1,0263";
  - KI-063: the no-chip "which bank" clarification loses the question;
  - KI-064: wording;
  - KI-065: a data-side district difference. The data-team note is `docs/Focus_Plus_DB_Issues.md`.
- **Next step (done later the same day):** fix KI-060 to KI-064 and re-run the 39 questions. The harness scripts (cases, judge, run_bot, render, build_xlsx) are
  in the session scratchpad, not the repo, and the method is on the report's Summary sheet.
- **Harness lessons:**
  - A question that pauses for "which area?" needs the "All of Meghalaya" chip, not the first
    district.
  - Set `PYTHONIOENCODING=utf-8`, or the "₹" in an answer crashes the runner's print.
  - Fetching the whole `v_focus_plus` view needs `timeout=500`.

**Previous session (2026-09-26, into the night): MGNREGA all-blocks and all-villages test, then fixes.
Code changed, uncommitted.**
- **Result on the final code: 13,298 / 13,298.**
  - All 56 blocks × 8 questions and all 6,425 villages × 2 questions (FY 2024-25);
  - each figure matched against megh_db and the raw CSVs.
  - Report: `docs/MGNREGA_AllBlocks_AllVillages_Test_Report_2026-09-26.xlsx`, with before/after
    evidence images.
- **Fixed:** KI-049 to KI-059 (the table and causes are in KNOWN_ISSUES), all in `app/pipeline.py`
  and all gated on MGNREGA. The ones to know:
  - the village-chip resume is pinned from the chip text (`_mgnrega_village_chip_pin`);
  - `_mgnrega_longest_village_in` does a whole-name lookup over every MGNREGA village;
  - SQL rewrites around a resolved `village_code`: `_mgnrega_pin_village_code` and
    `_mgnrega_drop_geo_beside_village`;
  - `_mgnrega_empty_answer` handles no-record and zero-base answers.
- **Tests:** `tests/test_mgnrega_usecase_fixes.py` now has 78 tests. Final suite numbers are in
  TESTING.md.
- **Bulk-run lesson (read before running one):** cap the DB pool (`DB_POOL_MAX_SIZE=10`).
  Tonight's VPN drops plus the default pool of 30 exhausted `megh_db` ("too many clients").
  The harness pauses on outages and retries. The method is in TESTING.md, "Bulk live runs".
- **Open:**
  - 16 village-to-block mapping differences between the DB and raw (the data team's; in
    `MGNREGA_DB_Issues.md`);
  - the KI-041 chip bug still exists in the other five schemes (awaiting the user's go-ahead);
  - generic "total expenditure" questions still ask "which scheme?", by design.
- **Next step:** commit when the user asks. Consider running the same all-villages harness for
  other years, and for the other schemes if the user wants the gated fixes extended.

**Earlier the same day: MGNREGA use-case QA, then fixes (30 / 30).**
- **Round 1:** 16 / 30. Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26.xlsx`.
- **Fixes:** KI-041 to KI-048, plus wording and unit fixes, all gated on MGNREGA (the user asked
  that other schemes not change). Rationale is in D-025, and the fix map is in KNOWN_ISSUES under
  "KI-041 to KI-048".
- **Round 2:** 30 / 30. Evidence: 43 of 43 queries on two fresh full live runs, checked against
  megh_db and the raw CSVs. Report: `docs/MGNREGA_UseCase_Test_Report_2026-09-26_v2_after_fixes.xlsx`.
- **Files changed:**
  - `app/pipeline.py`;
  - `app/schema_context.py` (MGNREGA rules 8–10);
  - new `tests/test_mgnrega_usecase_fixes.py` (41 tests);
  - docs: AI_PIPELINE, KNOWN_ISSUES, DECISIONS (D-025), CURRENT_STATE, TESTING and
    `MGNREGA_DB_Issues.md`.
- **Tests:** pytest 789 passed (baseline 748). Scripts 14/14. Live context suite 43/43.
- **Open items:**
  - GENAPARA's geography differs between the DB and raw (the data team's, in
    `docs/MGNREGA_DB_Issues.md`);
  - the KI-041 chip bug in the other schemes (awaiting the user's go-ahead);
  - generic "total expenditure" questions still ask "which scheme?". That is by design, since
    PMAY-G and Focus Plus also hold money.
- **Next step:** commit when the user asks. If other schemes are to get the KI-041 fix, remove
  the `"MGNREGA" in schemes` condition in `_explicit_level_in`, then re-run their QA sets.
- The harness scripts lived in the session scratchpad and are **not** in the repo. The method is
  on each report's Summary sheet.

**Previous session:** 2026-09-26. The context-hardening session, then a live validation with the
VPN up. Code changed, **uncommitted** (the user asked for no commit). Also uncommitted from
earlier the same day: context pass 1 (D-022) and the "All of Meghalaya" scope chip.

**LIVE-VERIFIED 2026-09-26** (`tests/live_context_validation.py`, real gateway and DB, turns
rotated across 3 worker stores through real Postgres):
- **43/43 checks**, follow-up resolution 29/29, SQL 20/20, repeated twice.
- Legacy A/B (`--legacy`): 39/43. Figures are in TESTING.md, "Live A/B".
- DB sync cost: about 50 ms p50 each way; `sync_in` p95 359 ms.

**The first live run found, and this session fixed:**
1. **Scenario design:** Focus Plus holds only FY 2022-23 and FY 2025-26, so the scenarios now use
   FY 2025-26. Scenario B2 (MGNREGA) checks the SQL-level year change. Scenario F step 5 no
   longer carries an FY, which D-009 pins to CM Elevate Legacy.
2. **Real bug:** "Show it by district." was rewritten "…in West Garo Hills". The fix is a
   `REMOVED by the follow-up` prompt line plus the `cleared:<field>` check
   (`_cleared_filters`, `context_policy.rewrite_violation`).
3. **Real bug:** "How many beneficiaries were there?" was not treated as a follow-up. The fix is
   `pipeline.is_scopeless_followup`, used in `_run_pipeline` and in the router's `is_cacheable`.

**New open issues (Low):**
- KI-030: an "all years" choice is not carried into the next follow-up.
- KI-031: the scheme swap keeps the old metric words.

**TESTS:** pytest, 19 files: **657 passed**. Scripts: **14/14**. Live: 43/43.

**LATEST: scheme substitution fixed (KI-039), uncommitted.** "give me same for <scheme>"
used to be answered from the reference docs. It is now a follow-up that swaps only the scheme:
`pipeline._scheme_substitution` / `is_scheme_substitution`, plan kind `SCHEME_SUBSTITUTION`,
AI_PIPELINE.md §5.6.
- Tests: `tests/test_scheme_substitution.py` (91).
- Live results: `logs/live_scheme_substitution_2026-09-26.json`.
- New Medium issue KI-040: MGNREGA "beneficiaries" is undefined.

**Before that (same day): live end-to-end validation, no code changed.** The report is
`docs/Context_Validation_Report_2026-09-26.md`: 53 turns, 47 PASS, 1 caveat, 5 FAIL. New issues KI-032 to KI-038.

**NEXT EXACT STEP (supersedes the list below):**
1. KI-032: apply the plan's CLEAR to `session.state` in `update_state`.
2. KI-034: a deterministic "resolved entity missing from SQL" guard.
3. KI-033: use the last DATA turn as the antecedent after a digression.
4. KI-035: never sum per-year distinct counts as a total.
5. Then re-run `tests/live_context_validation.py` and the validation groups.

**Previous next steps:**
1. KI-030: add a `year_all_combined` flag, mirroring `tranche_all_combined`.
2. KI-031.
3. Then the KI-022 / KI-023 user decision, which is still pending.

Re-run `tests/live_context_validation.py` (VPN) after any routing, rewrite or state change.

**DO NOT CHANGE (without reading the incident comment and the tests):**
- the `execute_with_repair` guards and `_verify_sql` filters;
- the `compose_response` faithfulness checks;
- `db._assert_safe` / `run_readonly`;
- the scheme-collision handling;
- the pending-pause resume (steps 0a / 0a');
- `data/<scheme>/*.yaml`;
- `megh_db.curated`.

Do not reintroduce raw previous-answer text into the rewrite prompt (D-022). Do not move
conversation state back into process memory (D-023).

**Never print or copy rows from `Focus Legacy to share to BLH.csv`.**
