# Scheme Onboarding Runbook — Megh One AI

*Written 2026-10-06, re-verified and corrected 2026-10-07. **Scope:** the step-by-step procedure
for onboarding a new scheme, derived from how the six committed schemes were actually onboarded and
tested, plus the automation plan for the mechanical parts.*

> **DOCUMENTATION CONFLICT — RESOLVED HERE (2026-10-07).** CLAUDE.md §3 links
> `docs/SCHEME_ONBOARDING_HANDOFF.md`, but that file **does not exist and is in no commit** —
> `git rev-list --all --objects` finds no such blob, so it was only ever an untracked working file
> and has been lost. This runbook is therefore **self-contained**: the per-layer detail it used to
> carry is reproduced in §3 and §5. Either remove that row from CLAUDE.md §3 or point it here.
> The per-scheme SME contract in `data/<scheme>/README.md` remains authoritative for that scheme.

**Evidence.** Every registry, symbol, command and figure below was verified against the source by
importing the live objects and running the suites. Where this file disagrees with the code, the
code wins. VERIFIED / INFERRED / PLANNED labels follow CLAUDE.md §8.

---

## 0. Baseline on 2026-10-07 (VERIFIED, measured)

| Check | Result |
|---|---|
| pytest, 26 files | **1,498 passed**, 1 warning, 47 s |
| `tools/verify_wiring.py` | **7 schemes, 174 checks, 6 gaps** |
| `docs/HANDOFF.md` | **Restored** 2026-10-07 (it had been deleted in the working tree) |

**A seventh scheme, NRLM, is now partly wired** — scaffolding committed on 2026-10-07. It holds all
7 YAMLs + README in `data/NRLM/` and entries in all 13 app/web files, and `curated.v_nrlm` is its
query surface needing no joins for ordinary questions. Its two open findings are in §6.4, and it is
the live worked example of this runbook.

The 6 verifier findings break down as: 2 intentional year holes (Focus Plus, Focus Legacy), the
known dual few-shot bank (CM Elevate Legacy), **one false positive** (`_SCHEME_FIT`, §6.3), and
**two genuine NRLM findings** (§6.4). Record this baseline before you start, so a later failure is
not blamed on your scheme.

*Earlier baseline, for reference: 2026-10-06 was 1,436 passed, 6 schemes / 150 checks / 4 gaps.*

---

## 1. The starting state this runbook assumes

The external/SME team delivers into the repo, and you place these before step 1:

```
data/<folder>/                       7 YAMLs + README.md (+ prompt_assembler.py if supplied)
data/reference/<docs>.md             KB docs, tagged <!-- scheme: <Canonical Name> -->
```

**A supplied prompt assembler is an offline validator, not a runtime path (VERIFIED).**
`data/focus_plus/prompt_assembler.py` (1,313 lines) and `data/cm_elevate/prompt_assembler.py`
(1,576 lines) both exist, and `grep -rn "prompt_assembler" app/ tools/ tests/` returns **nothing**.
The live prompt path is `app/prompt_builder.py` reading the YAMLs through `app/annotations.py`.
Keep it that way: making the assembler the runtime path replaces a working layer and needs a
DECISIONS entry first. Note NRLM shipped **without** one — it is optional.

What an assembler *is* good for is the acceptance gate in step 2. Its `--self-test` validates the
whole YAML contract, including a column count against the live view.

---

## 2. Phase A — Identity and the spec (before any code)

### Step 1. Read the room
1. `CLAUDE.md`, `docs/CURRENT_STATE.md`, `docs/HANDOFF.md`.
2. This runbook's §3 (the 14 registries) and §7 (what the automation may and may not do).
   Also `docs/SCHEMES.md` §Adding a scheme, which lists the same registries independently.
3. `git status` and recent commits.
4. **Record the baseline** (§0): `.venv/Scripts/python.exe tools/verify_wiring.py`.

### Step 2. Validate the handoff — fail fast, zero writes
If an assembler was supplied, run its own gate first:

```bash
.venv/Scripts/python.exe data/<folder>/prompt_assembler.py --self-test
```

Non-zero exit → stop and send it back. For reference, Focus Plus passes today, validating
88 classification rules, 67 defaults, 87 few-shots, 11 resolver dimensions, 11 join edges
(7 prohibited) and the `v_focus_plus` column count (VERIFIED).

Then check the keys `app/annotations.py` actually reads, because **a wrong key fails silently**:

| Key | File | If absent |
|---|---|---|
| `sql_generation_examples` | few-shot YAML | zero few-shots load; SQL quality collapses with no error |
| `foreign_key_augmentation.edges[].is_prohibited` / `use_instead` | FK YAML | prohibited joins never reach the prompt |
| `ranking_stop_words`, `refusal_score_factor`, `max_refusal_shots` | few-shot YAML | optional; ranking falls back to defaults |
| `common_mistakes`, `answer_shots` | few-shot YAML | optional; only CM Elevate Legacy's v2 bank uses them |

**`status:` values are a known trap.** Only `RETIRED_v2.0` is dropped at load and `UNANSWERABLE`
is treated as a negative example. **`REFUSED` is not understood** — that is why
`cmelevatelegacy_few_shot.yaml` (v1) is NOT the registered bank and
`cmelevatelegacy_prompt_few_shots.yaml` (v2) is.

Finally: KB docs exist and carry the exact canonical tag (`grep -ic "<label>" data/reference/<docs>`),
and the YAML `column_count` matches the live view (needs the VPN).

### Step 3. Fix the canonical identity
There is **no enum**. About 14 registries compare the display string literally, and the Qdrant
`scheme` payload tag must match it exactly. One spelling drift makes the scheme silently
unreachable on that path.

Decide and write down: canonical string, `data/` folder name, YAML prefix (these differ —
`PMAY-G` → `pmay` / `pmay_`), the query view, grain, money unit, time dimension.

### Step 4. Write `tools/scheme_spec.yaml` (PLANNED — the automation's only hand-written input)
The YAMLs carry `display_name`, `datasets` (with `object_type`, `grain`), `semantic_rules`,
`nlp_sql_rules` and named `dimensions` (VERIFIED), but **PII signalling is inconsistent across
schemes** — Focus Legacy's partitions YAML mentions `privacy` 4 times, CM Elevate Legacy's 0 times.
So a small spec file is unavoidable:

```yaml
- canonical: "New Scheme"
  folder: new_scheme
  yaml_prefix: newscheme
  view: curated.v_new_scheme
  grain: "one payment to one household"
  money_unit: rupees            # rupees | lakh | crore | none
  years: ["2024-25", "2025-26"] # [] = no time dimension
  year_holes: ["2023-24"]       # absent, NOT zero; never offered as a chip
  year_semantics: null          # e.g. NRLM: formation year, NOT a funding/reporting year
  programme_parent: null        # set -> _PROGRAMME_DATASETS + omit from _SCHEME_FIT
  village_capable: true
  village_narrow: true
  ac_capable: false
  ac_via: column               # column | dim_geography join  -> drives the prompt entity line
  privacy_tables: [curated.fact_new_scheme_raw]
  kb_docs: [reference/newscheme_reference.md, reference/newscheme_faq.md]
  kb_doc_name: null             # set if the docs call the scheme something else
  collision_strategy: null      # HUMAN-ONLY, see step 5
  intentional_omissions: []     # declared gaps; kills verifier false positives
```

`intentional_omissions` exists because of a real false positive: a membership check cannot know
about a deliberate gap (§6.3). `year_semantics` exists because of NRLM (§6.4).

### Step 5. THE HUMAN GATE — collision strategy (D-009)
If the new canonical string is a prefix or substring of an existing one (or vice versa), the
automation must **stop and refuse to guess**. You choose:

- **ask** — both patterns require a qualifier and the bare word matches neither, on purpose.
  The Focus pattern: `_BARE_FOCUS_WORD`, `_is_ambiguous_focus`, `_focus_ambiguity_clarification`,
  pause rule `focus-scheme-ambiguous`.
- **pin** — decide on explicit vocabulary, never ask. The CM Elevate pattern:
  `_prefers_cm_elevate_legacy` → `_pin_cm_elevate_dataset`, undone on the KNOWLEDGE route by
  `_unpin_cm_elevate`.

Record the choice in `docs/DECISIONS.md`. Everything downstream of the choice is mechanical and
generated (step 8). NRLM collides with nothing, so it needed neither.

### Step 6. Decide programme vs dataset
If the new scheme is another **dataset of an existing programme**, add `_PROGRAMME_DATASETS` and
**deliberately omit it from `_SCHEME_FIT`** — eligibility advice belongs to the programme, and
every `_SCHEME_FIT` consumer folds the dataset onto its parent first. Declare that omission in
the spec.

---

## 3. Phase B — Register the 14 registries (Layers 2–15)

### Step 7. Generate and apply the entries
`tools/onboard_scheme.py` (PLANNED) edits in place, with its own rollback because the tree
routinely carries hundreds of uncommitted files (772 on 2026-10-07), so `git checkout` is not a
safe undo:

1. **Snapshot** every target file to `.onboard_backup/<timestamp>/`; `--rollback` restores.
2. **Refuse to run** if the scheme is already partly registered, unless `--force`.
3. **Anchored insertion**, not blind regex: find the last entry in each registry and insert after
   it, preserving indentation and trailing commas.
4. **Compile gate after every file**: `py_compile` + `ast.parse` for Python, brace/paren balance
   for the HTML. First failure → auto-rollback the whole run. Never leave `pipeline.py`
   half-edited: one scheme name has ~148 call sites in it.

| # | File | Entries (VERIFIED present for the committed schemes) |
|---|---|---|
| 1 | `app/annotations.py` | `_SCHEME_DIRS`, `_FEW_SHOT_FILE`, `_FK_FILE` |
| 2 | `app/schema_context.py` | `SCHEME_CATALOG`, `SCHEME_METRICS`, `_<X>_TABLES/_RULES/_VOCAB`, `_SCHEME_BLOCKS` |
| 3 | `app/schema_introspect.py` | `_SUBJECT_TO_SCHEME`, `_TABLE_NAME_TO_SCHEME` (**first substring hit wins**) |
| 4 | `app/entity_resolver.py` | `_RESOLVER_FILE`, **`_ACTIVITY_VIEWS`** (the one NRLM missed, §6.4), `AC_CONTENTS_SOURCE` if AC-capable |
| 5 | `app/pipeline.py` | `_SCHEME_NAME_PATTERN`, `_SCHEME_FUZZY_ALIASES` (RapidFuzz ≥80), `_SCHEME_CANONICAL_SPELLING`, `_SCHEME_DISPLAY_NAME`, `_<X>_ONLY_TERMS`, `_SCHEME_DATA_YEARS`, `refresh_scheme_years` sql_by_scheme, `_SCHEME_USER_SUMMARY`, `_scheme_clarification`, `_bank_clarification`, `_CROSS_SCHEME_MONEY_SQL` (if money), `_AC_CAPABLE_SCHEMES`, `_VILLAGE_FACT_SCHEMES`, `_VILLAGE_NARROW_SCHEMES`, `_VILLAGE_CATALOGUES`, `_SCHEME_OWN_MEASURES`, `_SCHEME_HEADLINE_OFFERS`, `_PROGRAMME_DATASETS`, `_SCHEMES_WITHOUT_YEAR`, `_SCHEME_FIT` (unless `programme_parent`) |
| 6 | `app/edge.py` | `_SCHEME_NAMED`, `_SCHEME_STRONG`, `_SCHEME_ALIASES` (**Legacy forms first**), `_SCHEME_CAPABILITY`, `_SCHEME_STARTERS`, `STARTERS`, `_DOMAIN_WORDS` |
| 7 | `app/followups.py` | `_SCHEME_RX`, `_primary_schemes`, a `_<x>_data()` builder + its dispatch, the knowledge ladder |
| 8 | `app/prompt_builder.py` | `_<X>_EXACT`, `_scheme_of`, the resolved-entity line |
| 9 | `app/kb_ingest.py` | `_SOURCES`, `_CANONICAL_SCHEME` |
| 10 | `app/rag.py` | `_SCHEME_DOC_NAMES` (+ `_KB_SCHEME_ALIAS` if it has no KB of its own) |
| 11 | `app/auth.py` | the `schemes` list of **all 8 roles** — else users cannot see the scheme |
| 12 | `app/config.py` | `ASR_PROMPT` |
| 13 | `web/ai_query.html` | the scheme card, i18n (`heroSub`, `composerPh`, `greeting`), `CARD_QUESTIONS`, `renderCardQuestions`, `prettyName` |
| 14 | `data/<folder>/` | already placed in §1 |

**Generation sources (INFERRED from the YAML shapes, VERIFIED to exist):** `_<X>_RULES` from
`semantic_rules` + `nlp_sql_rules`; `_<X>_TABLES` from `datasets`; `_<X>_VOCAB` from resolver
`dimensions`; `SCHEME_CATALOG` from `grain` + `money_unit` + the "NOT the other one" warning when
a collision exists; `_<X>_ONLY_TERMS` from resolver dimension values minus shared geography.

### Step 8. Apply the collision consequences (mechanical, from step 5)
- Negative lookbehind/lookahead in **every** pattern: `pipeline._SCHEME_NAME_PATTERN`,
  `followups._SCHEME_RX`, `edge._SCHEME_ALIASES`. CM Elevate Legacy needed
  `(?<!legacy )` and `(?![\s-]*(?:legacy|disbursements?)\b)`.
- Reorder **every** table matcher so the longer name is tested first:
  `schema_introspect._TABLE_NAME_TO_SCHEME` (`cm_elevate_disb` before `cm_elevate`) and
  `prompt_builder._scheme_of`.
- Guard `_correct_scheme_spelling`, which otherwise turns "Elevate" into "CM CM Elevate Legacy".

### Step 9. Years — and what the year *means*
`[]` if there is no time dimension (also add to `_SCHEMES_WITHOUT_YEAR`). **Leave hole years out**
of `_SCHEME_DATA_YEARS` so no chip ever offers them — Focus Legacy omits FY 2023-24 because it is
a gap, not a zero (D-018). Confirm `refresh_scheme_years()` has a `sql_by_scheme` entry for the
scheme, or the chips drift from live data at startup.

**Also state what the year column means.** NRLM's is `formation_year_key` — the year an SHG was
formed, explicitly *"NOT a funding or reporting year"* per the DB column comment and the README's
headline finding. A scheme whose year is not a reporting year needs that said in `_<X>_RULES`, or
the SQL model will read "in 2020-21" as activity in that year.

### Step 10. Village and AC capability
Decide `_VILLAGE_FACT_SCHEMES` / `_VILLAGE_NARROW_SCHEMES` / `_AC_CAPABLE_SCHEMES`, and make the
`prompt_builder` entity line emit **columns that exist in the new view**. Three different shapes
exist today (VERIFIED):
- MGNREGA: `assembly_constituency_name` on the view;
- Focus Legacy / CM Elevate Legacy: a `dim_geography` join on `geography_key`;
- NRLM: `constituency_name_raw` / `constituency_number_raw` on `v_nrlm` itself, **no join**.

Current state: AC-capable = MGNREGA, Focus Legacy, CM Elevate Legacy, NRLM; village-fact =
MGNREGA, Focus Plus, PMAY-G, CM Elevate.

---

## 4. Phase C — Verify wiring and ingest

### Step 11. Registry gap check
```bash
.venv/Scripts/python.exe tools/verify_wiring.py "New Scheme"
```
Exit 0 required. **Triage `SILENT` first** — those fail without raising anything. NRLM's open
`_ACTIVITY_VIEWS` finding is exactly this class (§6.4). Teach the verifier to read
`scheme_spec.yaml` so declared `intentional_omissions` report OK, not MISSING.

### Step 12. KB label check and re-ingest
```bash
grep -ic "<label>" data/reference/<its docs>     # the composer fails if the label is absent
```
Then re-ingest: admin `POST /api/rag/reingest`, or restart the service. `ingest_kb` rebuilds when
a scheme is **missing** from Qdrant, not only when the collection is too small — the old size-only
check once left Focus Legacy with **zero chunks in production**.

### Step 13. Start the app and eyeball it
```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8300
```
Open `/ai-query` (the card, the starters, a data question, a "how does it work" question),
`/admin-ui` and `/health`.

---

## 5. Phase D — Testing (this is where the real work is)

### Step 14. Offline suites
```bash
# 1) pytest-style suites (26 files). Do NOT run `pytest tests` — KI-018 INTERNALERROR.
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
Use the repo venv — system Python lacks `fastembed` and RAG answers silently degrade. Set
`PYTHONIOENCODING=utf-8` on Windows. Expect **1,498 passed** as the 2026-10-07 starting point.

**Green tests do not prove NL→SQL accuracy** (KI-017). Every test is a regression lock for a past
incident. Correctness comes from the live passes below.

### Step 15. Live use-case QA — expect TWO rounds
This is the pattern every scheme followed (VERIFIED from TESTING.md §3):

| Scheme | Round 1 | After fixes | KIs raised |
|---|---|---|---|
| MGNREGA | — | 30/30 use cases (43/43 queries) | KI-041..048 |
| Focus Plus | — | 30/30 use cases (39/39 questions) | KI-060..067 |
| CM Elevate | **23/30** use cases, 47/55 questions | **30/30** (55/55) on 2 of 2 fresh runs | KI-068..075 |
| PMAY-G | **17/28** | **28/28** (71/71) on 2 of 2 fresh runs | KI-089..097 |
| Focus Legacy | 19/28 → 11/28 → **28/28** on 3 of 3 runs | 28/28 | KI-020, KI-145..165 |
| CM Elevate Legacy | 36/36, re-test **33/36** | **36/36** on 2 of 2 fresh runs | KI-166..168 |
| NRLM | **not yet run** | — | — |

**Method:**
- Call `pipeline.answer_question` **in-process** with the `.venv` Python, from the repo root so
  `.env` loads. DB and gateway need the office VPN.
- **Expected figures come from two independent sources:** a read-only query against `megh_db`, and
  the same aggregate from the raw source workbook/CSV. Confirm DB = raw before grading.
- When the bot pauses, the harness clicks the chip an officer would pick (the option carrying the
  target's exact name and block; "village" or "block"; the scheme).
- **Pass rule:** the answer states the expected figure (display rounding allowed), or says there
  are no records **without inventing a number**.
- Run each set **twice on fresh runs** and require identical answers — that is what caught the
  sampling flakes.
- Write the report to `docs/<Scheme>_UseCase_Test_Report_<date>.xlsx`, and the after-fix run to
  `…_v2_after_fixes.xlsx`. Put the method on the Summary sheet.

### Step 16. Bulk live run — every block and village
The only accuracy check at scale. Precedents (VERIFIED): MGNREGA 448 blocks + 12,850 villages;
Focus Plus 204 + 7,026; PMAY-G 224 + 5,120 (round 1 4,393/5,120 → KI-096); CM Elevate 7,364/7,364
(round 1 4,132/4,208); Focus Legacy **32,560/32,560** (56 blocks ×3, 12 district breakdowns,
55 ACs ×3, 3,384 villages ×2, 11,906 PGs ×2, 1,635 older spellings); CM Elevate Legacy 2,614/2,614.

**Protect the database — this is not optional.** The default pool is 10–30 connections per
process, `megh_db` allows `max_connections` = 100 **in total and is shared**, and during a VPN drop
the server holds dead connections until its TCP timeout. On 2026-09-26 that exhausted the server
("too many clients already") and the pipeline turned it into "couldn't build a query" / KB
fallbacks (KI-025).
- Run with `DB_POOL_MIN_SIZE=2 DB_POOL_MAX_SIZE=10`, concurrency ≤ 10. **Set both**: a max below
  the default min of 10 kills the pool (noted during the CM Elevate Legacy bulk run).
- Check TCP to `10.48.242.4:5432` before each case; pause on an outage and re-run any case that
  failed while the host was down.
- Budget roughly **55 cases/min at concurrency 10**.
- Harness shape that worked: `gen_bulk.py` → `bulk_run.py` → `bulk_judge.py` →
  `build_bulk_report.py`, in the session scratchpad. Truth is generated with **system python**
  (it reads the DB and raw files) and the bot is run with **`.venv`**.

### Step 17. Live context validation (after any routing, rewrite or state change)
```bash
.venv/Scripts/python.exe tests/live_context_validation.py
```
43/43 is the standing bar. Exit 0 = pass, 1 = a check failed, 3 = not run (unreachable). Report
lands in `logs/live_context_validation.json`. With DB sync on it writes temporary
`app.conversations` rows under `live-ctx-*` and deletes them at the end.
If your change was answer-text only, you may skip it — say so explicitly in the summary, as the
2026-10-05 rows do.

### Step 18. Regression on the other schemes
A new scheme's guards and regexes touch shared code. Every past onboarding re-ran a sample of the
neighbours — e.g. after PMAY-G: "Focus Plus 60/60 blocks + 300/300 villages, MGNREGA 60/60 +
300/300". Do the same.

### Step 19. Fix round — three fixes per wrong-number bug (D-007)
What comes out of rounds 15–16 is exactly what cannot be generated. For every wrong-number bug:
1. a prose rule in `schema_context`;
2. a few-shot example;
3. a **deterministic guard** in `execute_with_repair` or after `compose_response`.

Prose alone has repeatedly failed under sampling — `_cm_legacy_exact_totals` exists because
"don't sum rounded rows" failed **three times**. Add the Layer 10/11 guards your QA actually
demands; do not pre-invent them.

Every bug fix gets a regression test that calls the **real function**, never a re-implementation.
Put them in `tests/test_<scheme>_usecase_fixes.py`, matching the existing per-scheme files.

### Step 20. Data defects go to the ingestion team
**Do not modify `megh_db.curated`** — it belongs to `megh-ingestion`. Write defects up in
`docs/<Scheme>_DB_Issues.md` and do not patch them in the bot (D-016). Precedents:
`MGNREGA_DB_Issues.md`, `Focus_Plus_DB_Issues.md`, `Focus_Legacy_DB_Issues.md`, with replies in
`Chatbot_Answers_to_Ingestion_Team.md`. `CM_Elevate_Legacy_DB_Issues.md` is committed in `7064ab6`
but **deleted in the working tree** (VERIFIED 2026-10-07) and left pending a decision per
`docs/HANDOFF.md`; recover it with
`git checkout 7064ab6 -- docs/CM_Elevate_Legacy_DB_Issues.md` if you need the precedent.

### Step 21. Privacy
Privacy tables are **never queried** — today only the prompt protects them (KI-004). Raw source
files with PII are aggregated only, never printed, quoted, copied or committed
(`Focus Legacy to share to BLH.csv`, KI-022 — already tracked and pushed; treat those accounts as
disclosed). Root-level `*.csv` / `*.xlsx` are now gitignored. Never repeat the `app/users.yaml`
seed passwords (KI-023).

---

## 6. Phase E — Close out

### Step 22. Documentation is part of the task (CLAUDE.md §8)
Always update `docs/CURRENT_STATE.md` and `docs/HANDOFF.md`, plus: `SCHEMES.md` (the scheme and
the adding-a-scheme list), `AI_PIPELINE.md` (routing/prompts/guards/composer), `DATA_MODEL.md`
(objects, joins, units, years), `DECISIONS.md` (the collision choice and any new pattern),
`TESTING.md` (a new results row), `KNOWN_ISSUES.md` (every KI raised), and **this runbook**
(§0 baseline, the §5 QA table, and any new trap the scheme taught you).

Finish with the 8-part summary: WHAT CHANGED · FILES CHANGED · TESTS RUN · TEST RESULTS ·
DOCUMENTATION UPDATED · KNOWN ISSUES · REMAINING WORK · NEXT STEP.

### Step 23. Consistency sweep
Grep for stale scheme counts, model roles, file names, test counts and issue statuses. Mark what
you cannot resolve `DOCUMENTATION CONFLICT — NEEDS VERIFICATION`. Known-stale today:
`config.APP_NAME` lists four schemes; the `YEAR_RANGE_GUARD_ENABLED` comment says "both schemes …
exactly those four years"; the `annotations.py` docstring says "for both schemes"; a
`_run_pipeline` comment says "only four are loaded". **CLAUDE.md §1 and §3 still say "six scheme
datasets"** while NRLM is wired as a seventh — update when NRLM is declared live. Cosmetic.

### 6.3 The false positive you must not re-report
`tools/verify_wiring.py` reports `CM Elevate Legacy` as MISSING from `pipeline._SCHEME_FIT`.
**It is intentional and safe (VERIFIED).** `_SCHEME_FIT` holds one entry per *programme*, not per
dataset — 6 on 2026-10-07 (MGNREGA, PMAY-G, Focus Plus, Focus Legacy, CM Elevate, NRLM). CM Elevate
Legacy is absent because it is a *dataset of* the CM Elevate *programme*:

```
folded map:     {'CM Elevate Legacy': 'CM Elevate'}
_named_schemes: ['CM Elevate Legacy']  ->  after fold: ['CM Elevate']  ->  in _SCHEME_FIT: True
```

`_PROGRAMME_DATASETS` folds the dataset onto its programme **before** every `_SCHEME_FIT` lookup;
the other consumers guard with `in _SCHEME_FIT`; and the one unguarded read is fed only by
`_PROFILE_RULES`, a closed list that never emits the Legacy name. Three fit-check questions naming
"cm elevate legacy" returned `None` with no KeyError.

**The lesson for the automation:** a membership check across a flat registry cannot know about a
deliberate omission. That is what `intentional_omissions` in `scheme_spec.yaml` is for.

### 6.4 NRLM: the two open findings (VERIFIED 2026-10-07)
NRLM is the live worked example. Both findings came from `tools/verify_wiring.py`, which is the
point of step 11.

1. **SILENT — `v_nrlm` is absent from `entity_resolver._ACTIVITY_VIEWS`.** That tuple is read by
   `_activity_counts()` to break ties between duplicate `dim_geography` rows (same village name,
   same block and district, different `village_code`) that a user cannot tell apart. With NRLM
   missing, its own records never vote on which twin is meant. Nothing raises. **Fix:** add
   `"curated.v_nrlm"` to the tuple. Not applied here — it is a behaviour change on a shared
   resolver path and belongs with NRLM's QA round, with a regression test.
2. **WARN — 13 skipped years, 1985-86 to 1997-98.** `_SCHEME_DATA_YEARS["NRLM"]` holds 26 entries
   running 1984-85, then jumping to 1998-99 → 2022-23. The README states **30 years, 1984-85 to
   2022-23** with 1 SHG in 1984-85, so the sparse early tail is real data, not an error — but 26 ≠
   30, so **four years are unaccounted for. UNKNOWN — NEEDS VERIFICATION** against
   `SELECT DISTINCT formation_year_key FROM curated.v_nrlm` with the VPN up. Confirm whether the
   missing years are genuinely absent (leave them out, per D-018) or were dropped by mistake.

Remember NRLM's year is **`formation_year_key`** — the year the SHG was formed, *not* a funding or
reporting year (step 9). Its money columns are cumulative, so year filters and money do not
compose the way they do in the other schemes.

---

## 7. What is automatable, and what is not

| Step | Automated? |
|---|---|
| 2 Validate the handoff | **Full** — the assembler's `--self-test` plus the silent-key checks |
| 4 Spec | ~25 lines by hand, once |
| 5 Collision strategy | **Human. The tool refuses to guess** (D-009) |
| 6 Programme vs dataset | **Human** — drives `_PROGRAMME_DATASETS` and the `_SCHEME_FIT` omission |
| 7–10 The 14 registries | **Full**, in place, with snapshot rollback + compile gate |
| 11–13 Verify, ingest, boot | **Full** |
| 14 Offline suites | **Full** |
| 15–18 Live QA and bulk runs | **Run** automated; **grading** automated against DB = raw |
| 19 Fix round | **Human** — Layer 3 rules, Layer 10/11 guards, regression tests |
| 20–23 Defect notes, docs | **Human** |

Never automatable, because automating them is how the system breaks: the semantic rules
("count groups on `pg_id`, never `pg_name`"; "money = members × 5000"; `job_cards_issued_total`
is a stock, not a flow; NRLM's year is a formation year), the data traps in the README, the
collision strategy, and the SQL guards and answer guarantees that only a QA round can reveal.

Realistic effect: wiring drops from about **3 days to half a day**. The SME authoring and the two
QA rounds are irreducible.

---

## 8. Tooling status

| Tool | Status |
|---|---|
| `tools/verify_wiring.py` | **EXISTS**, 525 lines, read-only, 9 checks, exit 1 on a gap. Committed 2026-10-07 |
| `tools/scheme_spec.yaml` | **PLANNED** |
| `tools/onboard_scheme.py` | **PLANNED** — stages: validate, generate, edit in place, verify |
| `tools/qa_harness.py` | **PLANNED** — generalises the per-scheme scratchpad harnesses |

Suggested build order: the spec + the verifier change first (small, removes the false positive),
then the validate/generate stages, then the in-place writer with rollback.
