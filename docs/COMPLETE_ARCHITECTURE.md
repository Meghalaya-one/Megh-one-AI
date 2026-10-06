# Complete Architecture — Megh One AI

*Written for: an engineer joining this codebase who needs the whole system in one document.*

*Reconciled against the source on **2026-10-05** (branch `main`, working tree dirty — see §16).
Labels: **VERIFIED** (read in code, config or a test run) · **INFERRED** (reasoned from evidence)
· **UNKNOWN — NEEDS VERIFICATION**.*

This document is the single end-to-end map: every layer, every module, every stage of the
request path, every data store, and the reasoning behind the design. It complements rather than
replaces the focused docs:

| For | Read |
|---|---|
| Stage-by-stage prompt/LLM detail | [AI_PIPELINE.md](AI_PIPELINE.md) |
| Column-level DB reference | [DATA_MODEL.md](DATA_MODEL.md) |
| Per-scheme rules and registries | [SCHEMES.md](SCHEMES.md) |
| Component/API reference (shorter) | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Why a design is the way it is | [DECISIONS.md](DECISIONS.md) |

---

## 1. What the system is

One FastAPI service that lets Meghalaya government officers ask natural-language questions about
**six rural-development scheme datasets** and get back a verified number, a table, a chart and a
short written answer.

**The six schemes** (VERIFIED, `app/schema_context.py:SCHEME_CATALOG`):

| Scheme | What it is | Grain | Money unit |
|---|---|---|---|
| **MGNREGA** | Rural employment guarantee | source row (many per village-year), two facts | **LAKH ₹** |
| **PMAY-G** | Rural housing | one sanctioned house | ₹ |
| **Focus Plus** | State farmer cash-benefit / DBT | one payment (member × tranche) | unverified |
| **CM Elevate** | 15 livelihood/enterprise schemes, one `scheme_code` | one application | **none — `COUNT(*)` is the whole vocabulary** |
| **Focus Legacy** | Legacy producer-group disbursement ("FOCUS") | one payment to a **producer group** | unverified |
| **CM Elevate Legacy** | CM Elevate sanction-and-disbursement, 13 schemes | one applicant's sanction + disbursement | ₹ |

**Three question classes, three answer paths:**

1. **DATA** — "how many houses were completed in Ri Bhoi?" → NL→SQL against PostgreSQL
   `megh_db` (schema `curated`), then an LLM-composed sentence over the real rows.
2. **KNOWLEDGE** — "who is eligible for PMAY-G?" → RAG over Qdrant (SME reference docs).
3. **EDGE** — greetings, identity, thanks, off-topic, abuse → deterministic regex, **no model
   call at all**.

All inference is remote, on a self-hosted vLLM gateway at `10.48.242.4`. The UI is vanilla
HTML/JS. **No React, no WebSocket, no streaming, no graph framework.**

### 1.1 The two design commitments that explain everything else

Almost every unusual thing in this codebase follows from two commitments:

**(a) A wrong number is worse than no answer.** This is a government dashboard; an officer may
put a figure from it into a report. So the system never lets a model's word be final on a
number. Every numeric claim passes through deterministic code that re-checks it against the
actual rows. This is why `pipeline.py` is 15,000 lines: it is mostly guards.

**(b) Never trust a prose rule alone.** Prompt instructions fail under sampling. The project's
established pattern for any wrong-number bug is **three layers** (CLAUDE.md §5):

```
1. a prompt rule          — tells the model the right thing
2. a few-shot example     — shows the model the right thing
3. a deterministic guard  — makes the wrong thing impossible
```

Layer 3 is the one that actually holds. `execute_with_repair` has **31 SQL rewrite guards** and
~20 reject-and-repair assertions; `compose_response` has a numeric-faithfulness check with a
deterministic fallback sentence. (VERIFIED by count, `app/pipeline.py:11972`+.)

---

## 2. Runtime topology

```
                    browser (vanilla HTML/JS, JWT in localStorage)
                                      │
                                   HTTPS
                                      ▼
            ┌──────────────────────────────────────────────────┐
            │ nginx  (per app VM)                              │
            │   TLS · ModSecurity CRS WAF · rate zones         │
            │   deploy/nginx/nginx-nlpservice.conf             │
            └──────────────────────┬───────────────────────────┘
                   proxy_pass http://127.0.0.1:8300
                   (single upstream, NO stickiness ──┐ see §9.3)
                                      ▼
            ┌──────────────────────────────────────────────────┐
            │ uvicorn app.main:app --workers 2                 │
            │ deploy/systemd/megh-nlpservice.service           │
            └───┬──────────┬───────────┬──────────────┬────────┘
                │          │           │              │
      asyncpg pool   httpx client  AsyncQdrant   fastembed (in-process, CPU)
       10–30/worker   semaphore 24   client       bge-small-en-v1.5, 384-dim
                │          │           │
                ▼          ▼           ▼
        PostgreSQL 18.4  vLLM gateway  Qdrant
        megh_db          /openai/v1    :6333
        :5432
        ──────────────── all three on 10.48.242.4 ────────────────
                         (office Fortinet VPN from dev machines)
```

- **Target deployment:** two application VMs, each nginx + systemd. VERIFIED from the deploy
  files.
- **VM hardware** (from an earlier doc revision, not verifiable from code): ESDS VMs, 24 vCores
  / 256 GB RAM each. VM #1 has 2× H200 GPUs that **this service does not use** — inference is
  the gateway's job.
- **UNKNOWN — NEEDS VERIFICATION:** whether production is live, and whether the Docker path or
  the systemd path is in use.
- `docker-compose.yml` binds `127.0.0.1:8301:8300` and has an optional `local-infra` profile
  (Qdrant + Redis). VERIFIED.

---

## 3. Repository layout

```
meghalaya/
├── CLAUDE.md                     session rules + doc map (read first, every session)
│
├── app/                          the FastAPI service — 29,350 lines of Python
│   ├── main.py            233    app object, lifespan startup, /health, /metrics, static UI
│   ├── config.py          472    every setting (pydantic-settings over .env) — 134 keys
│   │
│   │   ── the orchestrator ──
│   ├── pipeline.py     15,214    the entire routing + NL→SQL chain. 365 functions, 2 classes
│   │
│   │   ── routing inputs ──
│   ├── edge.py          1,017    regex edge layer: greetings, identity, off-topic, harmful
│   ├── entity_resolver.py 1,522  district/block/village/year/AC/PG resolution (YAML + live DB)
│   ├── premise_check.py   380    checks numbers the QUESTION asserts against the result
│   │
│   │   ── prompt assembly ──
│   ├── prompt_builder.py   663   SQL / repair / verifier prompt assembly
│   ├── schema_context.py 1,610   hand-written per-scheme TABLES / RULES / VOCAB blocks
│   ├── schema_introspect.py 309  live information_schema + semantic.* catalogue → prompt
│   ├── annotations.py      405   few-shot + FK YAML loader; IDF few-shot ranking
│   ├── context_budget.py   184   per-prompt token accounting, priority-aware SQL budget
│   │
│   │   ── conversation state (4 layers, see §8) ──
│   ├── session_store.py    299   L1: in-process per-worker session cache
│   ├── conversation_store.py 399 L2: Postgres app.conversations / conversation_turns
│   ├── session_sync.py     109   cross-worker state sync (D-023)
│   ├── context_manager.py  907   reference substitution, follow-up evidence tiers, summaries
│   ├── context_policy.py   640   per-field merge actions, follow-up kinds, provenance (D-024)
│   ├── conversation_memory.py 184 L4: semantic memory of older turns in Qdrant
│   ├── followups.py        616   deterministic "next step" chips (no model call)
│   │
│   │   ── RAG ──
│   ├── rag.py              328   retrieve + tiered answer from the KB
│   ├── kb_ingest.py        302   chunk, embed and upsert the SME reference docs
│   ├── vectorstore.py      134   Qdrant client wrapper
│   ├── local_embed.py       76   fastembed CPU embeddings
│   │
│   │   ── infrastructure ──
│   ├── llm.py              601   ONE httpx client; all model roles; concurrency gate
│   ├── db.py               175   asyncpg pool; run_readonly() guard for generated SQL
│   ├── auth.py             353   roles, scope narrowing, post-SQL authorize()
│   ├── security.py         119   PBKDF2 hashing, HS256 JWT
│   ├── deps.py              62   FastAPI dependencies (current_scope, require_*)
│   ├── appdb.py            345   app.* schema DDL + user/tenant CRUD
│   ├── cache.py            229   exact-match LRU (+ optional Redis L2) and metrics counters
│   ├── semantic_cache.py   139   in-process cosine cache
│   ├── asr_guard.py         82   voice-input echo guard
│   ├── net.py               55   client IP extraction behind trusted proxies
│   ├── users.yaml                one-time seed for app.users — PLAINTEXT passwords (KI-023)
│   ├── routers/                  query · auth · history · rag · admin
│   └── middleware/               security_headers · limits · rate_limit
│
├── web/                          served UI, no build step
│   ├── ai_query.html             the chat console (~4,160 lines, inline JS)
│   ├── admin.html                the admin SPA
│   ├── Meghalaya_UnifiedPortal_UI.html   portal landing page
│   └── vendor/                   Chart.js, vendored
│
├── data/                         SME inputs, read at startup
│   ├── <scheme>/                 7 YAMLs + README per scheme (the SME contract)
│   ├── reference/                10 SME markdown docs → the RAG knowledge base
│   ├── web/                      additional tagged KB markdown
│   └── schema/                   schema_for_developers.md (stale for 4 newer schemes)
│
├── docs/                         project documentation + QA evidence workbooks
├── deploy/                       nginx (+ModSecurity CRS), systemd unit, SQL role/retention
├── tests/                        42 files (26 pytest-style + 14 plain scripts + 2 harnesses)
└── Dockerfile · docker-compose.yml · requirements.txt · requirements.lock · .env.example
```

**Path coupling (VERIFIED):** `app/` resolves `data/` and `web/` as siblings via
`Path(__file__).resolve().parents[1]`. The tree shape must be kept intact.

`nlp-service/` exists in the working tree but is empty and untracked. Its purpose is UNKNOWN.

`data/cm_elevate/prompt_assembler.py` and `data/focus_plus/prompt_assembler.py` are tracked but
**imported by nothing**. They are SME-side artefacts; the live prompt is built by
`app/prompt_builder.py`. (VERIFIED by grep.)

---

## 4. The request path, end to end

### 4.1 Overview

```
POST /api/query  {question, session_id?}
  │
  ├─ 1. middleware    security headers → CORS → body limit (256 KB) → rate limit (30/60s)
  ├─ 2. auth          JWT → UserScope (role, tenant, districts, blocks, schemes, granularity)
  ├─ 3. session       session_store.ensure + session_sync.sync_in (rehydrate from Postgres)
  ├─ 4. cache         exact LRU, then semantic (cosine 0.93) — skipped for follow-up fragments
  │
  ├─ 5. pipeline.answer_question ───── under asyncio.wait_for(60s) ─────────┐
  │     │                                                                  │
  │     ├─ _run_pipeline  (§5)  routing: edge / knowledge / data           │
  │     └─ _attach_followups    deterministic next-step chips              │
  │     └─ context_manager.update_state + maybe_update_summary            │
  │                                                                        │
  ├─ 6. record L1 turn                                                     │
  ├─ 7. fire-and-forget: persist_turn · index_turn · save_context_state    │
  ├─ 8. JSONL audit mirror                                                 │
  ├─ 9. cache write-back                                                   │
  └─ 10. metrics                                                     ◄─────┘
```

### 4.2 Input validation (VERIFIED, `app/routers/query.py`)

- `question`: 1–2000 chars, C0 control characters stripped.
- `session_id`: optional, `^[A-Za-z0-9._:-]+$`, max 128. Falls back to `day-<uid>-<date>`, then
  `one-<random>`.

### 4.3 Error mapping (VERIFIED)

| Exception | HTTP | Notes |
|---|---|---|
| `ClarificationNeeded` | **200** `route:"clarification"` | Not an error — the system is asking back. Stores `pending_scope_q` |
| `ModelBusyError` | 503 + `Retry-After: 5` | Model queue wait exceeded 20 s |
| `DatabaseUnavailableError` | 503 + `Retry-After: 5` | VPN/network drop mid-question (KI-025) |
| `asyncio.TimeoutError` | 504 | The 60 s ceiling, or a re-raised DB `command_timeout` |
| `UnsafeSQLError` | 500 | **Effectively unreachable** — the repair loop catches it and falls back to the KB |
| anything else | 502 | e.g. an httpx timeout re-raised from the pipeline |

### 4.4 Response shape (VERIFIED)

Every response carries `route`, `intent`, `answer`, `session_id`, `execution_time_ms`, plus the
empty data fields `schemes`, `resolved_entities`, `sql`, `sql_query`, `row_count`, `rows`,
`data` (so the client never has to branch on presence).

| `route` | Extra fields |
|---|---|
| `data` | `confidence`, `rows` (first 20), `data` (all, ≤1000), `follow_up_options`, `rewritten_question?` |
| `knowledge` | `confidence`, `sources`, `follow_up_options` |
| `edge` | `edge_type`, `suggestions` |
| `clarification` | `needs_clarification`, `question`, `clarification:{options, rule}` |
| `denied` | `denied_by` (`scheme` / `geography` / `granularity`) |

---

## 5. The pipeline — `app/pipeline.py`

15,214 lines, 365 functions, 2 exception classes, **no graph framework**. The module docstring
states the choice plainly:

> "The whole NL → SQL → answer flow as one plain sequential function. No graph framework: each
> step is an ordinary `await`, branches are ordinary `if`. This is deliberately simpler than the
> LangGraph design in the GrantThornton proposal — appropriate at 2 schemes and 20-40 concurrent
> users; revisit if either grows a lot."

That comment is now stale on scheme count (six, not two), but the decision stands.

### 5.1 `_run_pipeline` — the router (lines 14463–15130)

Numbered stages, in execution order. The ordering is **load-bearing**: nearly every step sits
where it does because something broke when it sat elsewhere.

| # | Stage | Why here |
|---|---|---|
| — | **CM Elevate dataset pin** (`_pin_cm_elevate_dataset`) | "Total amount disbursed under CM Elevate" is only answerable by CM Elevate Legacy. Settled once, up front. A DATA-only decision — the KNOWLEDGE route undoes it |
| — | `prev` / `ctx_state` computed | Moved **ahead of** the step-0 edge check so the edge whitelist can relax for a plausible follow-up |
| **0a** | Resume a scope pause | Fold a free-text reply ("West Garo Hills 2023-24") back into the paused question |
| **0a'** | Resume a **scheme** pause | A scheme choice, not a scope fragment — the scope merge does not apply |
| **0a''** | Resume any other chip pause | An unmatched reply continues the **paused** question's scheme, not the last answered turn's |
| **0c** | **Continuation gate** (`context_policy.continuation_signals`) | A previous answer is context only for a message that shares something with it. "who is harshit" after a Focus Plus answer shares nothing — without a signal it is a **new** question |
| **0--** | Harmful-intent refusal | Runs **first** among the scheme steps, so "i want to rob a bank, give me suggestions" can't be claimed by the recommender on the word "suggestions" |
| **0-** | Geo definition ("what is EKH?") | Before the edge layer, which would bounce a lone abbreviation as off-topic |
| **0-a** | Scheme listing / comparison / pick / recommendation | Before the knowledge route narrows to one scheme — no single document covers "the difference between the schemes" |
| **0** | **Edge layer** on raw text | Before follow-up detection, which would otherwise turn "hello" into a bogus follow-up |
| **0f** | Deterministic reference substitution | "the previous year", "the former/latter", "both" → concrete values, before the follow-up detector |
| **1** | **Follow-up rewrite** | Fragment → standalone question. A bare "Focus" is **never** handed to the model rewrite (must ask) |
| **1b** | Re-check edge on the rewrite | Cheap, and the rewrite can surface one |
| **1d/1e** | Bank clarification · admin-expenditure · unsupported scheme named | "PM-KISAN" → say plainly it isn't held |
| **1g** | Village-name search | "how many villages are named X?" |
| **2** | **Intent classification** | DATA vs KNOWLEDGE |
| **3** | KNOWLEDGE → `rag.answer_from_kb` | |
| **4** | DATA → `_answer_data` | On hard failure, tries the KB once before giving up |

### 5.2 `_answer_data` — the DATA path (lines 13885–14370)

```
 1. unsupported-scheme / cross-scheme-money shortcuts
 2. _is_ambiguous_focus          → bare "Focus"? ASK (never guess)         ── CLAUDE.md §4
 3. _mask_scheme_words_in_village_name   (a village called "Focus…" isn't a scheme mention)
 4. _needs_scheme_clarification  → which scheme?
 5. _needs_topn_clarification    → "top districts" — top how many?
 6. classify_scheme              → the scheme list                    [MODEL: classifier]
 7. PG-name / Sericulture / acronym-near-miss narrowing
 8. resolve_entities             → district/block/village/year/AC/tranche/status
 9. clarification gates:  scope → year → tranche → person-level-tranche-conflict
10. per-scheme deterministic answer paths (MGNREGA women, PMAY-G facts, Focus+ summary…)
11. generate_sql                                                   [MODEL: 30B SQL coder]
12. auth.authorize(scope, schemes, resolved, sql)   ── AFTER generation, BEFORE execution
13. execute_with_repair          → 31 rewrite guards + ~20 asserts + verifier + ≤3 repairs
14. premise_check                → does the question's own asserted number match the data?
15. compose_response             → answer sentence                 [MODEL: 9B composer]
16. per-scheme answer guarantees → final deterministic corrections
```

**Why `authorize` runs between generation and execution (step 12):** the SQL text is the only
place the real geography and granularity of a query are visible. Scope can't be checked before
the query exists, and checking after execution would mean the row data had already been read.

### 5.3 `execute_with_repair` — the SQL safety net (line 11972)

The most important function in the system. Up to **4 attempts** (1 + `max_repairs=3`). Each
attempt:

**Phase 1 — 31 silent rewrites.** Deterministic fixes applied without spending a model call,
e.g.:

| Guard | Fixes |
|---|---|
| `_uppercase_geo_literals` | `lgd_district`/`lgd_block` are UPPERCASE; a Title-Case literal silently matches **0 rows** |
| `_mgnrega_numeric_division`, `_mgnrega_lakh_not_divided` | MGNREGA money is natively lakh |
| `_cm_elevate_fix_literals`, `_cm_elevate_split_scheme_in_list` | CM Elevate's 15 programme names |
| `_focus_legacy_geo_columns` | `block_name_raw`/`district_name_raw` went NULL after a DB change → map to `lgd_*` |
| `_focus_legacy_date_trunc_as_date` | `DATE_TRUNC` timezone trap |
| `_pmay_crore_to_rupees`, `_pmay_comparison_limit` | PMAY-G unit and LIMIT handling |
| `_mgnrega_pin_village_code`, `_focusplus_pin_village_where` | a resolved village must appear in the WHERE |

**Phase 2 — ~20 reject-and-repair assertions.** Each raises `ValueError` with a **long, specific,
instructive** message naming the real incident. These messages are the repair prompt. Examples
(abridged):

- `_mgnrega_facts_joined` — joining `v_employment` to `v_expenditure` fans out: *"a real 986,020
  person-days became 109,448,220"*. The message hands over the correct two-CTE rewrite.
- `_village_filtered_at_wrong_level` — a village name placed in a block/district column matches
  zero rows and *"returns a confident 0 for a village that has real records"*.
- `_village_code_as_geography_key` — `geography_key ≠ village_code`; unrelated surrogate keys.
- `_crore_conversion_for_single_village` — a real non-zero village amount displayed as
  `"0.00 crore"`.
- `_focus_legacy_group_size_summed` — a group's member count is on **each** payment; 2,655 groups
  were paid more than once, so `SUM` double-counts (*"a 20-member group paid twice reads 40"*).
  Use `MAX(no_of_pg_members)`.
- `_rowgrain_no_aggregate` — a bare row read from a row-grain view returns **one arbitrary row**,
  not a total.
- `_STATE_PSEUDO_FILTER` — there is no `'Meghalaya'` row; the whole dataset is Meghalaya.

**Phase 3 — the paid semantic verifier.** `_verify_sql` (4B model) runs **last**, deliberately:
the free regex guards catch known bug shapes first, so only SQL that clears all of them costs a
model call.

The verifier is itself distrusted. Nine `_verifier_*_is_false` predicates discard known
false-positive complaint shapes (cosmetic issues, suppressed geography, false year complaints,
apostrophe complaints, village-code complaints, join complaints, empty-entity check-2…). A
verifier that cries wolf would otherwise burn the repair budget.

**Phase 4 — execute** via `db.run_readonly`, then a post-execution check
(`_mgnrega_comparison_without_figures`: a comparison must return the figures, not just a label).

**Connection errors short-circuit.** A dropped VPN is not a SQL problem. Before this guard, a
drop spent three 30B repair calls and then answered from the reference documents (NANDICHAR II,
Focus Plus all-villages run, KI-025).

### 5.4 `compose_response` — numeric faithfulness (line 13382)

```
rows empty?                → _no_data_answer / CM-Elevate zero-programme answer
every numeric cell 0/NULL? → hand the composer the REAL metric list, so it can say what IS
                             available instead of a vague "not covered"
all-NULL single row?       → detected explicitly — an aggregate that matched nothing comes back
                             as ONE row of NULLs, not zero rows (reported 2026-09-22:
                             "the membership count is null", as if the DB held a null)
multi-row?                 → _result_digest over EVERY row (totals, mean, extremes, full
                             dimension coverage) so the prose matches the chart and table,
                             not just the ~40 rows the model can see
  │
  ├─ compose                                                     [MODEL: 9B composer]
  ├─ _answer_numbers_faithful  — every number stated must exist in the data
  ├─ _answer_covers_metrics    — a multi-metric row must report every metric
  ├─ one strict retry on failure
  ├─ _deterministic_answer     — final fallback built straight from the rows
  └─ _HEDGE_RE                 — the opposite failure: a real non-zero result hedged as
                                 "no data / can't be broken down" is overridden
```

The faithfulness check carries explicit allowances, each from a real false positive: premise
numbers a note asked the composer to quote; MGNREGA's "100 days", which is a measure **name**
not a figure; and year-gap notes, which legitimately mention a year the scheme lacks.

### 5.5 Clarification as a first-class outcome

`ClarificationNeeded` is an exception that produces **HTTP 200**. The system asks back rather
than guessing, with chips the UI renders as buttons.

**The two name collisions that must never be guessed** (CLAUDE.md §4):

- A bare **"Focus"** → ask which (Focus Plus or Focus Legacy). They share a name and **no key**.
- A bare **"CM Elevate"** → pinned by keywords (`_pin_cm_elevate_dataset`): money / FY / lender
  words mean CM Elevate Legacy. Pinned, not asked, because the keyword signal is reliable.

Other gates: scheme, top-N, scope (area), year, tranche, region, dimension collision, AC
drill-down, village chips, Sericulture choice, bank channel, admin expenditure.

**Resuming a pause is genuinely hard**, and steps 0a/0a'/0a'' exist because of it: the reply may
be a fragment, a typed chip label, a one-tap chip carrying the whole rewritten question, a fresh
unrelated question, or a conversational "never mind". Each needs different handling, and the
pending state is always consumed so it can't leak into a later turn (KI-181, stale-clarification
work).

---

## 6. The model layer

### 6.1 Fixed roles (VERIFIED, `app/config.py` — do not move without a DECISIONS entry)

| Role | Model | Temp | Timeout | Used for |
|---|---|---|---|---|
| SQL generation | `qwen-model` (30B coder) | 0.0 | 30 s | **SQL only** |
| Classifier | `qwen4-deploy` (4B) | 0.0 | 30 s | intent, scheme, follow-up rewrite |
| SQL verifier | `qwen4-deploy` (4B) | 0.0 | 15 s | semantic check of generated SQL |
| Composer | `qwen35-9b` | 0.0 | 20 s | the answer sentence (max 800 tokens) |
| Embeddings | **local** fastembed `bge-small-en-v1.5` | — | — | 384-dim, ONNX, CPU, ~130 MB |
| Reranker | `qwen3-reranker` | — | — | **DISABLED** (`RERANKER_ENABLED=False`) |
| ASR | `qwen3-asr` | — | 60 s | voice input |

Everything runs at **temperature 0.0**. Determinism matters more than fluency here.

### 6.2 `app/llm.py` — one client, one gate (VERIFIED)

- **One shared `httpx.AsyncClient`** for every role.
- **Concurrency:** a per-worker `asyncio.Semaphore(MODEL_MAX_CONCURRENCY=24)`. Waiting past
  `MODEL_QUEUE_TIMEOUT_SECONDS=20` raises `ModelBusyError` → 503 + `Retry-After: 5`. This is
  deliberate **load shedding**: better a fast 503 than a 60 s timeout.
- **Request shape:** `/chat/completions` with a **single `user` message** (no system prompt),
  `chat_template_kwargs.enable_thinking=False`, optional `guided_json` / `guided_regex`. A
  400/404/422 on a guided request is retried once **without** the guided fields — gateway
  versions differ.
- **TLS:** uses `AI_MODEL_CA_BUNDLE_PATH` if the file exists; otherwise **falls back to
  `verify=False`** with a warning (commit `5c5a100`). The gateway uses a self-signed "Enlight
  AIOps" CA.
- **Observability:** one `llm_call` log line per call — role, model, queue ms, latency ms, and
  the gateway's `prompt_tokens` / `completion_tokens`.

### 6.3 Model calls per request

| Path | Calls |
|---|---|
| Edge | **0** |
| Cache hit | **0** |
| KNOWLEDGE | 1 classifier + 1 composer (+1 embedding, local) |
| DATA, best case | 3 |
| DATA, typical | 4–6 |
| DATA, worst case | ~13 (follow-up rewrite + classify + resolve + generate + verify + 3 repairs + compose + strict retry) |

The regex-first ordering throughout exists to keep the typical case near the bottom of that
range.

### 6.4 Prompt assembly — `app/prompt_builder.py`

A SQL prompt is composed of:

```
_PREAMBLE               never query raw/staging/meta; always the v_* views
_SHARED_TABLES          dim_geography, dim_year, dim_scheme
_SHARED_RULES           the 11 cross-scheme wrong-number rules
<scheme>_TABLES         per-scheme query surface
<scheme>_RULES          per-scheme traps (the longest blocks: CM Elevate ~180 lines)
<scheme>_VOCAB          the metrics that actually exist
_live_schema_block      introspected real columns + FKs (schema_introspect)
_entities_block         what the resolver pinned — the semantic contract
_fewshot_block          top-5 examples, IDF-ranked for THIS question
_prohibited_block       joins that must never happen
_common_mistakes_block
```

**Few-shot ranking is per-scheme and IDF-weighted** (`annotations.py`). Because the candidate
pool is already one scheme's bank, the scheme name is **noise** — it appears in every example.
`_build_scheme_stop` discounts ubiquitous tokens so ranking keys on what makes a question
distinctive. CM Elevate Legacy's bank is `cmelevatelegacy_prompt_few_shots.yaml` (**v2**), not
the v1 file.

`context_budget.py` does per-prompt token accounting against `PROMPT_BUDGET_SQL_TOKENS=15360`
and drops sections by priority when over budget.

---

## 7. Entity resolution — `app/entity_resolver.py`

Turns the places, years and categories in free text into exact DB values. Two sources:

1. **YAML catalogues**, loaded at startup — `data/<scheme>/*_entity_resolver.yaml`. **Each
   scheme has its own copy**, because the same name can resolve differently per scheme.
2. **The live DB**, queried per request — villages only (`resolve_village`,
   `village_names_exact`, `constituency_contents`). There are ~7,364 villages with 345 shared
   names; a static catalogue would be both stale and huge.

Resolved dimensions: district, block, village, year, assembly constituency, region (e.g. "Garo
Hills"), house status (PMAY-G), tranche (Focus Plus), CM scheme / scheme group, producer group.

### 7.1 Why this is harder than it looks

| Trap | Handling |
|---|---|
| District and block exist **only as denormalised UPPERCASE names** — no code columns, no tables | hierarchy lives in YAML; literals upper-cased in SQL |
| 345 village names are **shared** | count by `village_code`/`geography_key`, never by name |
| `geography_key` is a **surrogate, not stable across reloads**; `village_code` (LGD) is the natural key | `_village_code_as_geography_key` guard |
| An AC **cuts across** blocks and districts, so ANDing both yields zero rows | `_ac_with_invented_geo_filter` guard |
| `pg_name` carries a **group-type suffix**, so exact match never works | token matching; empty aggregates return a NULL row, not zero rows |
| A village may be named after a scheme word | `_mask_scheme_words_in_village_name` |
| Names collide **across dimensions** (a block and a village share a name) | `collides_across_dimensions` → a clarification chip |
| Twin villages, same name, same block | `_fl_expand_twins` |
| Acronyms ("EKH", "WGH") and near-misses | `lookup_geo_term`, `acronym_near_misses` |
| `entity_type='Unresolved'` placeholder rows | excluded for village questions only; district/state totals keep them |

### 7.2 The semantic contract

What the resolver pinned **must** appear in the SQL. `_resolved_scope_missing` rejects SQL that
dropped a resolved filter — otherwise a village question silently returns a statewide total and
reports it as the village's figure. This is the single most dangerous failure shape in the
system, and it has three separate guards (`_mgnrega_village_filter_missing`,
`_village_filtered_at_wrong_level`, `_resolved_scope_missing`).

---

## 8. Conversation state — four layers

Multi-turn context is the hardest part of this system, and it carries three DECISIONS entries
(D-022, D-023, D-024).

```
L1  session_store.py        in-process, per worker, TTL 1800 s, ≤8 turns
    │                       pending_scope_q / _village_hint / _rule / _options
    │                       Turn.result_summary = deterministic row summary
    │                       ── since 2026-09-26 this is only a CACHE ──
    ▼
L2  conversation_store.py   Postgres app.conversations.context_state (JSONB, versioned)
    │                       THE SOURCE OF TRUTH. Written by the awaited,
    │                       revision-guarded save_session_state. Also holds the summary
    ▼
L3  session_sync.py         sync_in on every request · sync_out awaited (D-023)
    ▼
L4  conversation_memory.py  Qdrant megh_conversation_memory — older turns, semantic.
                            Used ONLY when the in-process session has <2 turns
```

### 8.1 Why L2 is the source of truth (D-023, KI-028/KI-001)

nginx proxies to a single upstream **without stickiness**, and uvicorn runs 2 workers. Turn 2 of
a conversation can land on a different worker than turn 1. Per-worker memory therefore cannot
hold conversation state. `sync_in` refreshes from Postgres on every request; `sync_out` is
**awaited** (not fire-and-forget) so the next turn can't race it.

**Consequence:** anything the next turn needs must be in `Session.to_snapshot()`. A field that
exists only on the in-process object will vanish. (The clarification-resume state was exactly
this bug — KI-001.)

### 8.2 Follow-up context is structured, not sliced (D-022)

The rewrite prompt does **not** receive raw previous-answer text. It receives:

- the previous turn's **filters** (`previous_filters_line`);
- its **result rows** only when the follow-up actually points into them
  (`build_rewrite_evidence` tiers);
- layers chosen by the follow-up's **kind** (`context_policy.context_layers`).

### 8.3 Output is provenance-checked field by field (D-024)

`context_policy.rewrite_violation` / `_rewrite_provenance_violation` check that every field in
the rewritten question traces to either the user's words or allowed context. A rewrite that
invents a district is rejected.

### 8.4 Per-field merge plan

`plan_state_merge` returns a `MergePlan` assigning each field one of **KEEP / REPLACE / CLEAR /
REQUIRE_CLARIFICATION** — deterministic, no model call. `REQUIRE_CLARIFICATION` on `reference`
is how "which one?" with nothing to point at becomes a question rather than a guess.

### 8.5 The continuation gate (step 0c)

The failure that motivated it (reported 2026-09-29): after a Focus Plus answer, "who is harshit"
and "he is my collik remember" were rewritten "…under Focus Plus" and answered from Focus Plus
reference docs. Two bugs compounded — the edge whitelist was disabled by the mere **existence**
of a previous answer, and `looks_like_followup` treated any short anchorless message as a
fragment. Now a previous answer is context only for a message sharing a real signal: scheme or
measure words, a place, a year, a grouping, a reference into the result, or a how-does-it-work
question. **Choose the thread per follow-up; never wipe state.**

---

## 9. Data layer

### 9.1 Schemas and ownership (VERIFIED)

| Schema | Role | Owner |
|---|---|---|
| `raw`, `staging`, `meta` | ingestion internals | ingestion team — **the app never queries them** |
| `curated` | the star schema: dims, facts, `v_*` views | ingestion team — **read-only for this app** |
| `semantic` | `table_catalog`, `column_catalog`, `glossary`, `metric_definitions`, `join_graph` | ingestion team |
| `app` | `tenants`, `users`, `login_events`, `conversations`, `conversation_turns`, `query_audit` | **this app** (`appdb.ensure_schema`, idempotent) |

**Do not modify DB data or schema.** `megh_db.curated` belongs to `megh-ingestion`. Data defects
go to them as a `docs/*_DB_Issues.md` note.

### 9.2 `app/db.py` — the SQL boundary (VERIFIED)

| Function | Use |
|---|---|
| **`run_readonly(sql)`** | **the only path for LLM SQL** |
| `fetch_rows` / `fetchrow` / `fetchval` | the app's own **parameter-bound** reads |
| `execute` / `execute_script` | the app's own writes and DDL — `app.*` only |

`_assert_safe` enforces: no `;` inside the statement · a `select`/`with` first word · none of
`insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum` anywhere. Then it
appends `LIMIT 1000` if "limit" is absent.

**Never bypass it. Never run model text through `fetch_rows` or `execute`.**

**Two timeouts, both easy to misread:**
- `DB_POOL_TIMEOUT=30` is the per-connection **connect** timeout, **not** an acquire timeout.
  `pool.acquire()` is called with no timeout, so a saturated pool waits until the 60 s request
  ceiling.
- `command_timeout` (15 s) is **client-side**: asyncpg cancels and raises `asyncio.TimeoutError`.
  It is **not** a server `statement_timeout`.

### 9.3 Join model — no fact is ever joined to another fact

- MGNREGA employment ⟂ expenditure: both source-row grain, so a join fans out (986,020 →
  109,448,220). Use `v_district_year_summary`, or one CTE per fact.
- Scheme ⟂ scheme: use the cross-scheme views.
- Focus Plus ⟂ Focus Legacy, CM Elevate ⟂ CM Elevate Legacy: **no shared key**.
- `dim_producer_group` must be queried **alone** — joining it back double-counts repeat-paid
  groups.
- CM Elevate Legacy: never INNER JOIN `dim_year` yourself; it drops the Sericulture rows.

### 9.4 Startup-only snapshots (VERIFIED, `main.lifespan`)

Three things are read **once per worker at startup and never refreshed**:

1. `refresh_scheme_years` → `_SCHEME_DATA_YEARS`
2. `schema_introspect.load` → live columns, FKs, `semantic.*` catalogue
3. the YAML entity catalogues

**After the ingestion team adds a year or column, or changes a view: restart the service.** Until
then the year chips, the out-of-range guard and the prompt's column list are stale. Villages are
the exception — resolved live per request.

### 9.5 Connection budget (INFERRED)

30 connections/worker × 2 workers × 2 VMs = up to **120** connections to a host shared with
pgAdmin and analysts. Server `max_connections` is UNKNOWN — NEEDS VERIFICATION. Bulk QA runs cap
`DB_POOL_MAX_SIZE` to ~14 for this reason.

---

## 10. RAG — the knowledge path

```
data/reference/*.md  (10 SME docs)  +  data/web/*.md (tagged)
        │
   kb_ingest._chunk          heading-aware chunking; _strip_urban_subsections
        │                    (PMAY-G docs carry urban content that isn't ours)
   local_embed.embed          fastembed bge-small-en-v1.5, 384-dim, batch 32
        │
   Qdrant collection "megh_scheme_kb", payload {scheme, source, text}
        │
   rag.retrieve              top_k=12, EXACT-MATCH scheme payload filter, min_score 0.30
        │
   rag.answer_from_kb        tiered:
        │                      score ≥ 0.84 → return the chunk verbatim (near-exact match)
        │                      score ≥ 0.55 → compose from the kept chunks   [9B]
        │                      below        → "not covered by the reference material"
        └─ answer_from_kb_multi → retrieve per scheme for a multi-scheme question
```

**The ingest is self-healing** (VERIFIED, `kb_ingest.ingest_kb`): on startup it recreates the
collection if it is missing, too small, **or missing any scheme** (`vectorstore.distinct_schemes`).
The collection is **shared** across workers and environments.

**CM Elevate Legacy reads CM Elevate's KB** (`_KB_SCHEME_ALIAS`) — it has no reference docs of
its own.

**The exact-match scheme filter is a known sharp edge.** It has silently made chunks unreachable
three different ways (scheme label mismatch between ingest and query, a missing alias, and a
scheme absent from the collection). After editing `data/reference/`, re-ingest: admin
`POST /api/rag/reingest`, or restart.

**The reranker is off.** `RAG_RERANK_TOP_N=8` still names the step; retrieval order is used
as-is.

---

## 11. Security

Full OWASP control map: [SECURITY.md](SECURITY.md).

### 11.1 Authentication (VERIFIED, `app/security.py`)

- PBKDF2-HMAC-SHA256, **200k iterations**.
- HS256 JWT: `sub`, `username`, `tenant_id`, `role`, `exp`, issuer checked. TTL
  `JWT_TTL_MINUTES=720` (a 12 h officer shift).
- The browser keeps it in `localStorage` (`megh_jwt`).

### 11.2 Authorization (VERIFIED, `app/auth.py`)

Eight roles in `ROLE_PERMISSIONS`: `super_admin`, `tenant_admin`, `admin`, `state_officer`,
`district_officer`, `block_officer`, `analyst`, `public`. Every role lists all six schemes.

`scope_from_user` narrows a role by the user's own districts, blocks and schemes.

`authorize(scope, schemes, resolved_entities, sql)` runs **after SQL generation, before
execution**, checking three things:

1. **scheme** — is this scheme in the user's list?
2. **geography** — do the districts/blocks in the SQL fall inside the user's own?
3. **granularity** — state < district < block < village, capped per user.

Admin roles bypass. A deny returns `route:"denied"` with `denied_by`.

**Known gap (KI-007):** the geography regex only recognises `lgd_district` / `lgd_block`. A query
filtering geography another way is not caught.

### 11.3 Multi-tenancy

Every conversation, turn and audit row carries `tenant_id`; admin reads are filtered by
`_tenant_filter`.

### 11.4 Middleware (VERIFIED, `app/middleware/`)

Order inbound: **rate limit → body limit → CORS → security headers** (outermost).

- `security_headers.py` — CSP (keeps `'unsafe-inline'` for the inline JS), HSTS (1 y), nosniff,
  frame-deny, referrer and permissions policies, `X-Request-ID`.
- `limits.py` — 256 KB body cap; `/api/query/transcribe` is exempt with its own 10 MB cap.
- `rate_limit.py` — per-IP sliding window: login 6/300 s, `/api/query*` 30/60 s. Backend
  `memory` or `redis`.

Plus nginx + **ModSecurity CRS** in front (staged `DetectionOnly` → `On`).

### 11.5 Privacy boundary — tables that must never be queried

| Table | Holds |
|---|---|
| `curated.fact_focus_legacy_disbursement` | unmasked `account_no`, `name_on_the_account` |
| `curated.bridge_pg_bank_history` | unmasked account numbers |
| `curated.fact_cm_elevate_disbursement` | applicant first/middle/last names |
| Focus Plus `member_id`, `pincode` | person-level identifiers |

**These are enforced only by the prompt today — KI-004.** There is no deterministic table
blocklist in `run_readonly`. Given §1's own "never trust a prose rule alone" principle, this is
the largest open inconsistency in the system.

**The same PII is committed to git (KI-022).** `Focus Legacy to share to BLH.csv` at the repo
root holds 14,569 rows with **14,491 unmasked all-digit `account_no` values and 10,353
`name_on_the_account` values**. It was committed in `7064ab6`, and `origin/main` points at that
commit. Never print, quote, copy or upload its rows — count or aggregate only.

`app/users.yaml` holds **plaintext seed passwords** for `superadmin` and `rd-admin` (KI-023).

### 11.6 Logging and audit (VERIFIED)

- Pipeline log lines carry **no request id, user or question**. The exception is the
  `prompt_context` line (`context_budget.py`): section token sizes, budget and `request_id` — no
  prompt text.
- **The SQL of a failed attempt is not logged (KI-003)**, which makes repair-loop debugging hard.
- `app.query_audit` + `logs/query_audit.jsonl`: user, tenant, role, route, schemes, granularity,
  allow/deny, row count, IP, latency. **Not the SQL.**
- `app.conversation_turns` does store the final SQL and the full response JSON (so a reopened
  chat can redraw its charts).
- `ASR_DEBUG_DIR`, when set, saves **every voice upload as WAV plus its transcript**. It is set
  in the local dev `.env` (VERIFIED 2026-09-26). Diagnostics only; gitignored under `logs/`.

---

## 12. Caching, capacity and degradation

### 12.1 Two caches (VERIFIED)

| Cache | Mechanism | Settings |
|---|---|---|
| Exact | LRU, optional Redis L2 | TTL 900 s, 2,000 entries |
| Semantic | in-process cosine over the pipeline's embedding | threshold **0.93**, TTL 900 s, 1,000 entries, min 12 chars |

Both are keyed on question + `scope.cache_fingerprint()` — so one officer's scope-narrowed answer
can never be served to another. **Neither caches clarifications or follow-up fragments**, whose
meaning depends on conversation state.

### 12.2 Capacity (design target 20–40 concurrent users, ~200 DAU)

| Control | Setting |
|---|---|
| Model concurrency | 24/worker → 96 cluster-wide (INFERRED: 2 workers × 2 VMs) |
| Load shed | 20 s queue wait → 503 + `Retry-After: 5` |
| Request ceiling | 60 s → 504 (nginx `proxy_read_timeout` 120 s) |
| DB pool | 10–30/worker; command timeout 15 s; ≤1000 rows |
| Caches | exact 15 min; semantic 15 min at cosine 0.93 |

### 12.3 Degradation is deliberate and silent

**The server always boots.** Every remote startup step is wrapped in `_try` — it logs and
continues. The DB pool rebuilds lazily on first use, and KB ingest runs as a **background** task
so a cold Qdrant never blocks traffic.

In-flight degradations, all logged as warnings only:

| Failure | Behaviour |
|---|---|
| Verifier call fails | treated as "no issue" — the SQL proceeds |
| Schema-catalogue load fails | falls back to the hand-written prompt blocks |
| Context-layer failure | ignored; the answer still goes out |
| Follow-up chip build fails | answer returned without chips |
| KB ingest fails | RAG path degraded, DATA path unaffected |

The trade-off: the service stays up, but a silent degradation is hard to notice in production.
`/health` (admin or `X-Metrics-Token`) and `/metrics` are the only signals.

---

## 13. API surface

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/query` | user JWT | `{question, session_id?}` → answer JSON (§4.4) |
| `POST /api/query/transcribe` | user JWT | multipart audio ≤10 MB → `{text}` or `{text:"", no_speech:true}` |
| `POST /api/auth/login` · `/logout` · `GET /me` · `POST /bootstrap` | – / user | HS256 JWT; bootstrap the first super_admin |
| `GET /api/history` · `GET/PATCH/DELETE /api/history/{session_id}` · `POST …/pin` · `…/archive` | user | officer-private chat history |
| `GET /api/rag/status` · `POST /api/rag/reingest` | user / admin | KB point count; rebuild the KB |
| `/admin/tenants`, `/roles`, `/users[/{id}]`, `/logins`, `/conversations[/{id}]`, `/audit`, `/stats`, `/schema` | admin JWT, tenant-scoped | admin console API |
| `GET /health` | anonymous → `{status}` only; admin/token → components | DB + Qdrant (+ Redis) |
| `GET /metrics` | admin JWT or `X-Metrics-Token` | route counts, latency p50/p95/p99, cache stats |
| `/` · `/ai-query` · `/admin-ui` · `/static/*` | – | static UI, `Cache-Control: no-cache` |

`/docs` and `/openapi.json` exist **only when `ENV=dev`**.

**There is no WebSocket, SSE or streaming endpoint** (VERIFIED: nothing in `app/` or `web/`
matches `websocket` or `StreamingResponse`).

---

## 14. Frontend — `web/`

- **`ai_query.html`** (~4,160 lines) — the chat console. Calls `/api/query`,
  `/api/query/transcribe`, `/api/history*`, `/api/auth/*`. JWT in `localStorage`. Renders
  clarification chips, follow-up chips, Chart.js charts (vendored) and tables.
- **`admin.html`** — the admin SPA.
- **`Meghalaya_UnifiedPortal_UI.html`** — portal landing page.
- **No build step, no framework, no client-side router.**
- `?api=<base-url>` sets `API_BASE`. Default same-origin; a cross-origin base is blocked by the
  enforced CSP (`connect-src 'self'`) unless CSP and CORS change.
- The `PRODUCTS` object is a leftover of the per-scheme era — now a single `unified_auto` entry,
  with a stale comment above it saying "MGNREGA / PMAY-G / both". Cosmetic.

### 14.1 Voice input

16 kHz WAV only. `asr_guard.py` plus `ASR_PROMPT` guard against two real failures: a take
stopped mid-word transcribing as a bare "Okay.", and the prompt being echoed back as the
transcript.

---

## 15. Configuration

**134** settings in `app/config.py` (pydantic-settings), overridable by `.env`. `.env.example`
documents ~94 keys.

**`.env` is read relative to the working directory — always run from the repo root.** It is
gitignored and holds real secrets; never print or commit it.

The systemd unit deliberately does **not** use `EnvironmentFile=`, because inline `#` comments
would break parsing.

### 15.1 Feature toggles (all `True` unless noted)

| Group | Keys |
|---|---|
| Clarification gates | `SCHEME_CLARIFY_ENABLED`, `TOPN_CLARIFY_ENABLED`, `SCOPE_CLARIFY_ENABLED`, `YEAR_CLARIFY_ENABLED`, `TRANCHE_CLARIFY_ENABLED` |
| Guards | `OUT_OF_SCOPE_GUARD_ENABLED`, `PREMISE_CHECK_ENABLED`, `YEAR_RANGE_GUARD_ENABLED`, `SQL_VERIFY_ENABLED` |
| Context | `CONTEXT_LAYER_ENABLED`, `CONTEXT_STATE_ENABLED`, `CONTEXT_STATE_SHARED`, `CONTEXT_SUMMARY_ENABLED`, `CONTEXT_SEMANTIC_MEMORY_ENABLED`, + budgets |
| Follow-ups | `FOLLOWUP_REWRITE_ENABLED`, `FOLLOWUP_SUGGEST_ENABLED` |
| Decoding | `GUIDED_DECODING_ENABLED`, `SQL_GUIDED_DECODING_ENABLED` |
| Other | `SCHEMA_CATALOG_ENABLED`; **`RERANKER_ENABLED=False`** |

---

## 16. Testing

**Use the repo venv: `.venv/Scripts/python.exe`.** System Python lacks `fastembed`, and RAG
answers **silently degrade** under it.

42 files in `tests/` (40 `test_*.py` plus `live_context_validation.py` and `smoke_restructure.py`): **26 pytest-style** + **14 plain scripts** (`python tests/test_X.py`). Exact
commands in [TESTING.md](TESTING.md).

**Do not run bare `pytest tests`** — it crashes with INTERNALERROR (KI-018). The working
invocation selects pytest-style files explicitly:

```bash
.venv/Scripts/python.exe -m pytest -q $(grep -lE "^\s*(async )?def test_" tests/test_*.py)
```

**Baseline (2026-10-05):** **1,436 pytest passed** (26 files), **14/14** scripts, live context
suite **43/43**.

`tests/live_context_validation.py` needs the VPN. Re-run it after **any** routing, rewrite or
state change.

### 16.1 Testing rules that matter

- Every bug fix gets a regression test calling the **real function**, never a re-implementation.
- Some tests **pin exact source strings** in `pipeline.py`. If one fails after a refactor, read
  it before "fixing" it.
- **Green tests do not prove NL→SQL accuracy.** There is no golden-set benchmark. Behaviour
  changes need a live check: call `pipeline.answer_question` in-process with the `.venv` Python
  from the repo root so `.env` loads.

### 16.2 How scheme QA was actually done

Each scheme went through exhaustive live runs with every figure checked against both `megh_db`
and the raw source file. Evidence workbooks are in `docs/`:

| Scheme | Scale |
|---|---|
| MGNREGA | use cases 30/30; all blocks + villages **13,298/13,298** |
| Focus Plus | 30/30; **7,230/7,230** |
| CM Elevate | 30/30; **7,364/7,364**; KI-182 retest **2,520/2,520** |
| PMAY-G | 17/28 → 28/28; full scenario matrix **17,331/17,331** |
| Focus Legacy | 19/28 → 28/28; blocks/villages/ACs/PGs **32,560/32,560** |
| CM Elevate Legacy | 36/36, then 33/36 on re-test (KI-166..168) |

---

## 17. Deployment

[deploy/DEPLOYMENT.md](../deploy/DEPLOYMENT.md): Python 3.11 (deployed venv 3.11.9), venv,
`.env`, systemd unit, nginx, ModSecurity CRS (staged `DetectionOnly` → `On`), DB role and
retention scripts, go-live checklist.

- `deploy/sql/01_create_megh_app_role.sql` creates `megh_app`: SELECT on `curated`/`semantic`,
  SELECT/INSERT/UPDATE/DELETE + CREATE on `app`.
- `deploy/sql/02_retention_policy.sql` adds `app.purge_expired()`. Its windows are placeholders
  and whether it is scheduled is UNKNOWN.
- The **local dev `.env` connects as `postgres`** (VERIFIED). Which role production uses is
  UNKNOWN — NEEDS VERIFICATION (KI-004).
- The Docker build uses `requirements.txt`, not the lock file — the lock pins Windows-only
  packages.
- **No PM2 and no AWS appear in the repo** (VERIFIED).

### 17.1 Local development

```bash
# from the repo root, so .env loads
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8300
# then: /ai-query (chat) · /admin-ui · /health
```

`megh_db`, Qdrant and the model gateway are all on `10.48.242.4` and need the Fortinet VPN. The
DB connection sometimes times out briefly — retry.

---

## 18. Dependency graph

```
routers/query ──► pipeline ──► edge, context_manager, context_policy, followups,
     │                         premise_check, rag, auth, annotations, entity_resolver
     │                │
     │                ├──► prompt_builder ──► schema_context, schema_introspect, annotations
     │                ├──► entity_resolver ──► db (villages live), data/*/…entity_resolver.yaml
     │                ├──► llm (every model role)
     │                ├──► context_budget (token accounting)
     │                └──► db.run_readonly  ◄── the ONLY path for generated SQL
     │
     ├──► cache, semantic_cache
     ├──► session_store ◄──► session_sync ◄──► conversation_store (Postgres)
     └──► conversation_memory ──► vectorstore (Qdrant)

rag ──► vectorstore (Qdrant), local_embed / llm.call_embedding
main ──► appdb, schema_introspect, annotations, entity_resolver, kb_ingest, db, llm, vectorstore
```

**`pipeline.py` imports almost everything and almost nothing imports it** (only
`routers/query.py`, `main.py` and the tests). It is the top of the graph — which is why it is
15,000 lines, and why the project's rule is to make small targeted changes inside it rather than
decompose it.

---

## 19. Startup sequence (VERIFIED, `app/main.py:lifespan`)

```
1. secret guard            logs CRITICAL on insecure defaults — NEVER blocks boot
2. init_pool               asyncpg
3. init_client             httpx
4. init_qdrant             Qdrant
5. load_annotations        few-shot + FK YAML from data/
   load_entity_resolver    entity catalogues from data/
6. appdb.ensure_schema     CREATE TABLE IF NOT EXISTS on app.*; seed tenant + users
7. schema_introspect.load  live columns, FKs, semantic.* catalogue   ── snapshot, see §9.4
8. refresh_scheme_years    per-scheme FY list from the DB            ── snapshot, see §9.4
9. ingest_kb               BACKGROUND task — never blocks traffic
```

Steps 2–8 are each wrapped in `_try`: the server boots **degraded** rather than not at all.

---

## 20. Known architectural risks

Full list: [KNOWN_ISSUES.md](KNOWN_ISSUES.md). The ones that are architectural rather than
behavioural:

| ID | Risk |
|---|---|
| **KI-004** | Privacy tables are protected **only by the prompt**. No deterministic blocklist in `run_readonly` — inconsistent with §1(b) |
| **KI-022** | Unmasked bank account numbers and holder names are **committed to git** and reachable from `origin/main` |
| **KI-023** | `app/users.yaml` holds plaintext seed passwords |
| **KI-007** | The authorize geography regex only recognises `lgd_district` / `lgd_block` |
| **KI-003** | Failed SQL is not logged, making repair-loop debugging hard |
| **KI-018** | Bare `pytest tests` crashes with INTERNALERROR |
| **KI-001** | Clarification-resume state is per-worker; anything the next turn needs must be in `to_snapshot()` |
| **KI-020** | The DB now has PG name-alias views; the chatbot doesn't use them yet |
| — | **Prompt size** is the main cost driver. The SQL prompt carries full per-scheme rule blocks; `context_budget` caps it at 15,360 tokens but the ceiling is close |
| — | **Startup snapshots go stale.** A new year or column needs a restart (§9.4) |
| — | **Per-IP rate limiting behind NAT** can throttle a whole office as one client |
| — | **Scheme count is enumerated by hand in ~14 registries** — adding a scheme means touching all of them ([SCHEMES.md §Adding a scheme](SCHEMES.md#adding-a-scheme)) |

### 20.1 Documentation conflicts found while writing this

| Claim | Reality |
|---|---|
| CLAUDE.md §2 and ARCHITECTURE.md §1: `pipeline.py` is "about 8,400 lines" | **15,214 lines** (VERIFIED). Grew with the four newer schemes. **Both fixed 2026-10-05** |
| CLAUDE.md §4: schemes enumerated in "about 14 registries" | Still the figure in SCHEMES.md; not independently re-counted here |
| `pipeline.py` module docstring: "appropriate at 2 schemes" | Six schemes now. The no-framework decision still stands, but the stated justification is stale |
| `data/pmay/README.md` §9: "PMAY is not in megh_db yet" | The code queries `v_pmay`. Stale (noted in DATA_MODEL.md) |
| `data/schema/schema_for_developers.md` | Stale for the four newer schemes; the newer `SCHEMA_FOR_DEVELOPERS.md` the READMEs cite is **not in this repo** |

---

## 21. Reading order for a new engineer

1. **`CLAUDE.md`** — the rules. Short, and every line is there for a reason.
2. **This document** — the whole map.
3. **`docs/CURRENT_STATE.md`** — what works, what's broken, what's next.
4. **`app/pipeline.py:14463`** (`_run_pipeline`) — read the numbered stage comments top to
   bottom. They are the real routing spec.
5. **`app/pipeline.py:11972`** (`execute_with_repair`) — read the guard messages. Each one is a
   post-mortem of a real wrong number.
6. **`docs/DECISIONS.md`** — before proposing any architectural replacement.

### 21.1 The one habit that matters most

**Almost every regex, gate and guard in `pipeline.py` exists because of a real reported
failure**, and the comment above it usually names the incident and date. Read that comment before
changing or removing it. The code looks over-defensive until you read why each defence is there.

Evidence order when sources disagree: **source code > DB schema > tests > config > docs > git
history > chat memory.** If a doc disagrees with the code, the code wins — and you fix the doc.
