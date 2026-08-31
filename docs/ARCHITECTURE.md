# nlp-service — Architecture & Infrastructure

One FastAPI service answers natural-language questions about Meghalaya's
**MGNREGA** and **PMAY-G** schemes. It generates read-only SQL against the
curated `megh_db` star schema for numbers, and retrieves from a small Qdrant
knowledge base for "how the scheme works" questions. All model inference is
remote — the service itself runs no models.

There is no longer any per-scheme service or gateway: scheme selection
(MGNREGA / PMAY-G / both) happens inside the pipeline, not in the URL.

---

## Infrastructure

### Application tier — 2× ESDS VMs

| | VM #1 | VM #2 |
|---|---|---|
| vCores | 24 | 24 |
| RAM | 256 GB | 256 GB |
| Storage | 1 TB @ 5000 IOPS | 1 TB @ 5000 IOPS |
| GPU | 2× H200 (141 GB HBM3e each) | unconfirmed |
| OS | Ubuntu | Ubuntu |

Shared: 100 Mbps internet, 2 public IPs, vFirewall (25 SSL-VPN + 2 site-to-site
tunnels), antivirus, 10 TB block storage, 5 TB backup, Enlight AIOps monitoring ×2.

> **The application-tier GPUs are unused by this workload.** `nlp-service` does
> zero local inference — every model call is a network hop to the inference box
> below. The H200s only become relevant if a model is later self-hosted on the
> ESDS side instead of using the remote gateway.

### Data + inference tier — `10.48.242.4` (pre-existing, not part of the ESDS quote)

| Component | Detail |
|---|---|
| Model gateway | OpenAI-compatible (vLLM). `qwen-model` = **qwen3-coder-30b-fp8** (classify / intent / SQL-gen), `qwen35-9b` (answer composer), `qwen3-embedding`, `qwen3-reranker`, `qwen3-asr` |
| PostgreSQL | 18.4, database `megh_db` — curated MGNREGA + PMAY-G star schema |
| Qdrant | vector DB, port `6333` — the scheme knowledge base |
| pgAdmin4 | admin UI, port `8080` |
| TLS | self-signed cert issued by "Enlight AIOps" — trust the CA bundle via `AI_MODEL_CA_BUNDLE_PATH` |

---

## Request path

```
client
  │  HTTP :80  (TLS :443 to be added — see nginx conf)
  ▼
nginx (one per app VM)                    deploy/nginx/nginx-nlpservice.conf
  │  proxy_pass 127.0.0.1:8300
  ▼
uvicorn  backend.main:app  --workers 2    deploy/systemd/megh-nlpservice.service
  │
  ├── asyncpg pool (10–30) ───────────────►  PostgreSQL  megh_db   @ 10.48.242.4:5432
  ├── httpx AsyncClient ──────────────────►  Qwen gateway          @ 10.48.242.4/openai/v1
  └── AsyncQdrantClient ──────────────────►  Qdrant                @ 10.48.242.4:6333
```

The app VMs hold no state. Restarting either is safe; the KB re-ingest on
startup is idempotent (skips when Qdrant already holds the chunks).

---

## Routing — Edge → SQL → RAG

`backend/pipeline.py::answer_question()`:

1. **Edge** (`edge.py`) — greetings, "who are you", thanks, off-topic, abuse.
   Regex only, no model call. Returns `route:"edge"`.
2. **Intent** (`classify_intent`) — keyword fast-path, else one classifier call.
   `DATA` (a number from `megh_db`) vs `KNOWLEDGE` (how the scheme works).
3. **KNOWLEDGE** → `rag.answer_from_kb()` — embed → Qdrant search → qwen3-reranker
   → confidence tiers (return top chunk / compose from chunks / "not covered").
   `route:"knowledge"`.
4. **DATA** → the existing NL→SQL path: **scheme gate** (`_needs_scheme_clarification` —
   `SCHEME_CLARIFY_ENABLED`; if the question names no scheme, asks for no cross-scheme
   view, and uses no scheme-specific vocabulary, raise `ClarificationNeeded` with
   one-tap `options` instead of guessing "both") → **top-N gate**
   (`_needs_topn_clarification` — `TOPN_CLARIFY_ENABLED`; a ranked list over a dimension
   with no count) → `classify_scheme` → `resolve_entities`
   (deterministic; raises `ClarificationNeeded` when ambiguous) → **scope gate**
   (`_needs_scope_clarification` — `SCOPE_CLARIFY_ENABLED`; an aggregate question that
   pins no place *and* no year, free-text reply merged back in) → **year gate**
   (`_needs_year_clarification` — `YEAR_CLARIFY_ENABLED`; a metric/breakdown question
   whose place is already settled but which names no financial year and asks for no
   time series — offers the scheme's FYs + "all years combined" as one-tap `options`) →
   `execute_with_repair` (one repair retry) → **premise check** (`premise_check.check_premises`
   — `PREMISE_CHECK_ENABLED`; a quantity the question asserts as fact, e.g. "the 1.71 L
   sanctioned houses", is matched against the ≤3-row result — a note is added only when *no*
   returned figure is close to it, so the composer won't restate an unsupported number) →
   `compose_response` (told to not report a fabricated 0/NULL for a metric the schema
   doesn't carry, and to correct any premise a note flags). `route:"data"`.
   A `ClarificationNeeded` is returned as `intent:"CLARIFY"` + `clarification.options`
   (plus the flat `needs_clarification`/`question` keys); the frontend renders the
   options as clickable replies that each resume the flow as a standalone question.
5. **Fallback** — a hard DATA failure tries the KB once before returning an
   honest "couldn't answer that from the current data".

Every generated statement is re-checked in `db.py` (`SELECT`/`WITH` only, single
statement, no DDL/DML keywords) regardless of the DB role.

**Follow-ups.** With an `X-Session-Id` header, `session_store.py` keeps the last
few turns per worker (TTL `SESSION_TTL_SECONDS`). When turn *n*+1 reads like a
fragment (`looks_like_followup` — a lead like "what about…", a bare pronoun, or a
short anchorless phrase), `rewrite_followup` spends one classifier call to turn it
into a standalone question using turn *n*, then routes that. The rewrite is
returned as `rewritten_question`. Caches are bypassed whenever a session id or
`X-User-Id` is present (a follow-up's meaning is context-dependent; a scoped
answer must not be served to another role).

The rewrite only fires when turn *n* was an actual scheme answer (`route` in
`data` / `knowledge`) — a fragment after a greeting, off-topic reply,
clarification or denial has nothing coherent to attach to. A fragment with a
bare pronoun ("how launched it?") and no scheme named, arriving with no such
antecedent, returns a short "name the scheme and what you'd like to know" nudge
instead of being guessed into a query. Toggle: `FOLLOWUP_REWRITE_ENABLED`.

**Edge handler (`edge.py`).** Rule-based, no model call, kept in step with the
NeuralAI reference `edge_handler.py`. Order: (0) hard off-topic (weather,
markets, sport) — wins even over a place name unless a scheme is named;
(1) strong MGNREGA/PMAY-G intent → route it; (2) canned banks — greeting,
identity, thanks, goodbye, profanity, silly, confused; (3) meta-conversation
("what did I ask?") → pass through; (4) follow-up fragments ("add them up",
"which is higher", "explain that") → pass through so the follow-up rewrite can
handle them; (5) ≥3 non-ASCII chars → pass through (model reads Khasi/Garo/
Bengali/Hindi); (6) domain whitelist — a question with **no** MGNREGA/PMAY-G
vocabulary at all is answered as `off_topic` here, so it never reaches a model.
Edge replies that redirect carry `suggestions` (`edge.STARTERS`) as one-tap
chips.

**Next-step suggestions.** After any data/knowledge answer, `followups.py`
(`build_followups`, pure Python, no model call) attaches up to three
`follow_up_options` `[{label, question}]` — the complementary metric for the
same scheme and scope, a one-level-finer breakdown (state→district→block→
village, from the resolved entities), and the rules/data counterpart. Every
suggestion is a real question this service can answer; the bank is keyed on
scheme + which metric words the question used + which entities resolved, so it
never drifts into invented metrics. Edge replies instead carry `suggestions`
(the `edge.STARTERS` starter questions) as one-tap chips. Toggle:
`FOLLOWUP_SUGGEST_ENABLED`.

---

## Auth, multi-tenancy, authorization

**Everything this service owns lives in the `app` schema of `megh_db`** (created
idempotently at startup by `appdb.ensure_schema()`): `tenants`, `users`,
`login_events`, `conversations`, `conversation_turns`, `query_audit`. The
`curated`/`semantic` warehouse stays read-only.

### Identity — `backend/security.py`, `routers/auth.py`, `deps.py`
Officers authenticate with **username + password** (`POST /api/auth/login`).
Passwords are PBKDF2-HMAC-SHA256 (stdlib, 200k iters, per-user salt). Login
returns a **self-contained HS256 JWT** (`sub`, `username`, `tenant_id`, `role`,
`exp`); `/api/query` and every `/api/*`/`/admin/*` route require it as
`Authorization: Bearer`. `POST /api/auth/bootstrap` creates the first
`super_admin` while `app.users` is empty (or with `ADMIN_TOKEN` after).
`GET /api/auth/me`, `POST /api/auth/logout` (advisory — logged). Login/logout/
fail/bootstrap all write `app.login_events` (admin-visible).

### Multi-tenancy — department = tenant
Every user belongs to one **tenant** (a directorate/department; default `RD`,
seeded). `tenant_admin` manages users + sees conversations/audit **for their
tenant only**; `super_admin` (`cross_tenant`) spans all tenants and creates them.
Every conversation, turn and audit row is stamped `tenant_id`, and admin reads
are filtered by it — one department can't see another's officers or chats.

### Authorization — spec 5.7, `backend/auth.py`
`scope_from_user(row)` builds a `UserScope` from `ROLE_PERMISSIONS[role]` narrowed
(never widened) by the user's `districts`/`blocks`/`schemes`. After SQL
generation, before execution, `authorize()` runs three checks on the generated
SQL + resolved entities:

| Check | Denies when |
|---|---|
| **scheme** | the question needs a scheme not in `scope.schemes` |
| **geography** | a district/block literal (or resolved entity) isn't in the user's assigned area — or a geo-restricted user asks a district+-grain question with no area named |
| **granularity** | the query grain (`state < district < block < village`, from GROUP BY / entities) is finer than `scope.granularity_cap` |

Deny ⇒ `route:"denied"`, `denied_by:"<check>"`, plain-English `answer`, no SQL
run. **Admin roles bypass the three checks** (5.7 targets officers, not admins)
but stay tenant-scoped for what they can *see*. Roles: `super_admin`,
`tenant_admin`, `admin`, `state_officer` (district cap), `district_officer` /
`block_officer` (area-pinned), `analyst` (unrestricted read), `public` (state cap).

### Conversations + audit — `backend/conversation_store.py`
`session_store.py` is the in-process L1 for follow-up context; this module is the
durable L2 (fire-and-forget writes, DB outage ⇒ answer still returns). A client
`session_id`/`X-Session-Id` continues a conversation; with none, an officer's
ad-hoc questions roll into one `day-<uid>-<date>` conversation. Officer-private:
`GET /api/history` and `/api/history/{session_id}` filter by the caller's
`user_id` — one officer can never read another's chats.

### Admin API — `routers/admin.py` (admin JWT; `_tenant_filter` scopes every read)
`GET/POST/PATCH/DELETE /admin/users`, `GET /admin/users/{id}` (effective scope),
`GET /admin/roles`, `GET/POST /admin/tenants` (POST = super_admin),
`GET /admin/logins`, `GET /admin/conversations` + `/{conv_id}`,
`GET /admin/audit?denied_only=&user_id=`, `GET /admin/stats`, `GET /admin/schema`.
A JSONL mirror of the audit trail is still written to `AUDIT_FILE`.

### Admin dashboard — `frontend/admin.html`, served at `/admin-ui`
Single-page vanilla JS: login gate → tabs for Overview (stats + recent logins),
Users (create/deactivate/reset-pw with role + district/scheme scope), Login
events, Conversations (per-officer, drill into turns + SQL), Query audit (filter
denied), Tenants (super_admin), Schema catalog. JWT kept in `localStorage`.

---

## Embeddings + reranker

Gateway deployments confirmed: `qwen-model` (SQL), `qwen35-9b` (classify/compose),
**`qwen3-reranker`** (`/openai/v1/rerank`, its own key). **No** embedding or ASR
model. So `EMBEDDING_PROVIDER=local`: `fastembed` runs `BAAI/bge-small-en-v1.5`
(384-dim ONNX) on one dedicated worker thread for KB ingest + query/semantic-cache
embeddings; `llm.call_reranker` now calls the real `qwen3-reranker` and only falls
back to classifier scoring if that route errs. Flip `EMBEDDING_PROVIDER=gateway`
once `qwen3-embedding` is deployed. With the real reranker in place
`RAG_HIGH_CONFIDENCE` is raised to 0.80 so near-miss retrieval is corrected by the
composer instead of returned verbatim.

## Live schema catalog — `backend/schema_introspect.py`

At startup, reads `semantic.table_catalog` / `glossary` / `metric_definitions` /
`join_graph` from `megh_db` and folds a compact, scheme-scoped "LIVE CATALOG"
block (table descriptions, business-term → column mappings, metric formulas) into
the SQL-generation prompt alongside the hand-written hazard rules in
`schema_context.py`. `GET /admin/schema` returns the loaded catalog. Toggle with
`SCHEMA_CATALOG_ENABLED`.

---

## Capacity — 20–40 concurrent, ~200 DAU

### The load

- **200 DAU**, ~5–15 questions each over the working day ≈ **1,000–3,000 questions/day**.
- Peak **20–40 truly concurrent** in-flight requests.
- Split (observed shape): ~15% edge (no model call), ~25% knowledge/RAG, ~60% data/SQL.

### What each request costs

| Route | Model calls | ~Wall time |
|---|---|---|
| edge | 0 | < 50 ms |
| data | 1–4 (classify + intent often short-circuit via keyword/`_shortcut_scheme`; SQL-gen + compose always) → avg ~2.5 | 4–9 s |
| knowledge | embed + rerank + (0–1 compose) → avg ~2 | 2–5 s |
| **exact-cache hit (any route)** | 0 | < 20 ms |
| **semantic-cache hit (any route)** | 1 (embed only) | ~50–150 ms |

At 40 concurrent that is up to ~100 simultaneous model calls if unbounded — which the shared 30B cannot absorb. So the app **bounds and sheds** instead.

### The controls (all in `config.py` / `.env`)

| Control | Setting | Effect |
|---|---|---|
| Model-gateway concurrency cap | `MODEL_MAX_CONCURRENCY=24` (`llm.py` semaphore) | ≤ 24 in-flight model calls per worker; the rest queue. vLLM continuous-batching handles ~24 concurrent decodes on an H200 without latency collapse. |
| Load shedding | `MODEL_QUEUE_TIMEOUT_SECONDS=20` | A call that can't get a slot in 20 s → `ModelBusyError` → **HTTP 503 + `Retry-After: 5`**. Fail fast, don't pile up. |
| Response cache | `RESPONSE_CACHE_ENABLED`, `RESPONSE_CACHE_TTL_SECONDS=900`, `RESPONSE_CACHE_MAX_ENTRIES=2000` (`cache.py`) | Exact-match (normalised) repeated questions skip the pipeline entirely — 0 model calls. Per-worker, 15-min TTL so a DB reload shows within 15 min. Expect 20–40% hit rate in steady state. |
| Semantic cache | `SEMANTIC_CACHE_ENABLED`, `SEMANTIC_CACHE_THRESHOLD=0.93`, `SEMANTIC_CACHE_TTL_SECONDS=900`, `SEMANTIC_CACHE_MAX_ENTRIES=1000` (`semantic_cache.py`) | Catches near-duplicates the exact-match cache misses ("houses in EGH" vs "…East Garo Hills"). Costs **1 embedding call** on an otherwise-uncached question to save the 2–4 chat calls a pipeline run makes; the pipeline reuses that embedding. Embedding failure → plain miss. Excludes edge / low-confidence / clarification results. |
| Guided decoding | `GUIDED_DECODING_ENABLED`, `SQL_GUIDED_DECODING_ENABLED` (`llm.py` + `pipeline.py` schemas) | `guided_json` on the scheme / entity / intent classifier calls, `guided_regex` (bare `SELECT`/`WITH`) on SQL-gen. Removes the "unusable JSON → slow fallback" path and trims ````sql`-fence / prose-preamble repair round-trips. Sent as extra fields on the vLLM body; `_extract_json` / `_extract_sql` still run, so a gateway that ignores them is unaffected. Not a SQL grammar — won't catch a wrong join. |
| Per-request ceiling | `REQUEST_TIMEOUT_SECONDS=60` (enforced in router via `asyncio.wait_for`) | A stuck request → **HTTP 504**, slot freed. |
| Rate limit | 30 req / min / IP on `/api/query*` (`middleware/rate_limit.py`) | Per-worker sliding window; blunt abuse guard. |
| PG pool | `DB_POOL_MIN_SIZE=10`, `DB_POOL_MAX_SIZE=30` | Connections held only for the ~15 ms–15 s execute step, never across model calls — 30 covers 40 concurrent since most requests are in the model-call phase. |
| SQL guards | `SQL_EXECUTION_TIMEOUT_MS=15000`, `SQL_MAX_RESULT_ROWS=1000`, auto-`LIMIT` | One slow/huge query can't monopolise a pool slot. |

### Headroom

- 2 workers/VM × 2 VMs = **4 event loops × 24 slots = 96** model-call slots cluster-wide, against a 20–40 concurrent target → ~2–4× headroom before shedding starts.
- CPU per worker is light (async I/O, no local inference). Memory per worker < 300 MB + cache (~a few MB at 2,000 entries).
- The real ceiling is the model gateway's own throughput — watch `nvidia-smi` / vLLM metrics on `10.48.242.4`, not the app VMs.

### Watch it

`GET /metrics` → `requests_by_route` (incl. `<route>:cache` and `<route>:semcache`), `latency_ms_p95/p99`, `busy_rejections_total`, `errors_total`, `response_cache.hit_rate`, `semantic_cache.{hit_rate,embed_errors}`. If `busy_rejections_total` climbs, either raise `MODEL_MAX_CONCURRENCY` (if the gateway has room) or add a model replica. If `latency_ms_p95` for `data` exceeds ~12 s, the 30B is the bottleneck — add prefix caching + n-gram speculative decoding on its vLLM server, or a second replica.

**Inference-engine techniques — split of where each lives.** Client-side (done, in this service): guided/structured decoding (above). vLLM server flags on `10.48.242.4` (not code — need config access to that box): `--enable-prefix-caching` (every SQL-gen call shares the scoped schema-context prefix), `--speculative-model=[ngram]` (SQL output echoes table/column/district literals straight from the prompt), chunked prefill (keeps a long SQL-gen prefill from head-of-line-blocking short classify calls), `--kv-cache-dtype fp8` (raises the concurrency ceiling without touching weight precision). Weight quantisation is already FP8 — do not push to INT4/AWQ given the "output must be good" constraint.

Worst-case single-request latency ≈ classify + intent + SQL-gen + compose (each ≤ 30 s cap) + one PG round trip; nginx `proxy_read_timeout` 120 s and the app's 60 s ceiling both sit above that, so the app — not nginx — returns the clean 504.

---

## Model roster (`.env` / `config.py`)

| Role | Model | Where |
|---|---|---|
| Scheme + intent classify, SQL generation | `qwen-model` (qwen3-coder-30b-fp8) | gateway `/chat/completions` |
| Answer composition | `qwen35-9b` | gateway `/chat/completions` |
| KB chunk + query embeddings | `qwen3-embedding` | gateway `/embeddings` |
| KB candidate reranking | `qwen3-reranker` | gateway `/rerank` (falls back to prompt-scoring) |
| Voice input | `qwen3-asr` | gateway `/audio/transcriptions` |
