# Meghalaya NL Assistant — MGNREGA + PMAY-G

One FastAPI service (`app/`) that answers natural-language questions about
Meghalaya's **MGNREGA** and **PMAY-G** scheme data:

- **Data questions** → generates a read-only SQL query against the curated
  `megh_db` star schema, runs it, and composes a short answer.
- **Scheme-knowledge questions** ("who is eligible for PMAY-G?", "what documents
  are required?") → retrieves from a Qdrant knowledge base built from the
  `data/` reference docs, reranks, and answers from the passages.
- **Edge input** (greetings, "who are you", off-topic) → an instant canned reply,
  no model call.

Scheme selection (MGNREGA / PMAY-G / both) happens **inside the pipeline**, not in
the URL. The three former per-scheme services (`unified-data`, `cm-elevate`,
`focus`) and the gateway have been removed.

All model inference is remote — the service calls a self-hosted OpenAI-compatible
Qwen gateway and does no local inference.

## Layout

```
meghalaya/
├── app/                       the FastAPI service (package `app`)
│   ├── main.py                app + lifespan (pool, model client, Qdrant, KB ingest)
│   ├── pipeline.py            Edge → intent → {SQL path | RAG path}
│   ├── edge.py                regex edge-case handler (no model call)
│   ├── rag.py                 embed → Qdrant search → rerank → answer
│   ├── kb_ingest.py           chunk data/**/*.md → embed → upsert to Qdrant
│   ├── vectorstore.py         Qdrant client + helpers
│   ├── llm.py                 one httpx client; classify / SQL-gen / compose / embed / rerank / ASR
│   ├── db.py                  asyncpg pool; read-only SQL guard
│   ├── appdb.py               app.* schema (users, tenants, conversations, audit)
│   ├── auth.py                role/geography/granularity checks + audit
│   ├── security.py            HS256 JWT + pbkdf2_sha256 password hashing
│   ├── deps.py                request identity and role gates
│   ├── schema_context.py      in-prompt condensation of the data model
│   ├── entity_resolver.py     district / block / year / village resolution
│   ├── annotations.py         loads few-shot + FK YAML from data/
│   ├── conversation_store.py  chat history + query audit persistence
│   ├── users.yaml             one-time seed for app.users
│   ├── net.py                 client-IP resolution with proxy trust (XFF)
│   ├── routers/               /api/query, /api/auth, /api/history, /api/rag, /admin
│   └── middleware/            security headers/CSP, body-size limit, per-IP rate limit
├── web/                       served UI
│   ├── ai_query.html          chat console          (/ai-query)
│   ├── admin.html             admin console         (/admin-ui)
│   └── Meghalaya_UnifiedPortal_UI.html   portal     (/)
├── data/                      SME-curated inputs, read at startup
│   ├── mgnrega/               few-shot, FK, entity-resolver YAML
│   ├── pmay/                  same, for PMAY-G
│   ├── reference/             scheme reference + FAQ docs, KPI workbooks
│   ├── schema/                schema_for_developers.md
│   └── web/                   scraped background docs (KB source)
├── docs/                      ARCHITECTURE.md · DATA_MODEL.md · INFERENCE_REQUIREMENTS.md
├── deploy/                    nginx conf + systemd unit
├── tests/                     smoke tests
├── certs/                     internal CA bundle          (gitignored)
├── logs/                      audit JSONL + uvicorn out   (gitignored)
├── archive/                   reference zip, rotated .env backups (gitignored)
├── requirements.txt
├── .env                       secrets (gitignored)
└── .env.example
```

## Running

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill in DATABASE_URL + the model API keys
uvicorn app.main:app --host 0.0.0.0 --port 8300
```

Run from the repository root — `.env` is read relative to the working directory.

- Portal:  <http://127.0.0.1:8300/>
- Chat console:  <http://127.0.0.1:8300/ai-query>
- Health:  <http://127.0.0.1:8300/health>

| Route | Purpose |
| --- | --- |
| `POST /api/query` | `{question}` → `{route, answer, sql, rows, data, …}` |
| `POST /api/query/transcribe` | multipart `file` (audio) → `{text}` (qwen3-asr) |
| `GET /api/rag/status` | Qdrant KB collection point count |
| `POST /api/rag/reingest` | rebuild the KB from `data/` |
| `GET /health` | `ok` / `degraded` with the failing component named |

The scheme KB is ingested into Qdrant on startup (idempotent — skipped when the
collection is already populated). Force a rebuild with `POST /api/rag/reingest`.

## Deploy

Two application VMs behind the ESDS vFirewall; run nginx + the systemd unit on
each. See `docs/ARCHITECTURE.md` for the full infrastructure map and
capacity budget (20–40 concurrent, 100–200 DAU), and `docs/SECURITY.md` for the
OWASP Top 10 control map (app-layer hardening + nginx/ModSecurity CRS edge) and
the go-live security checklist.

## Models (self-hosted Qwen gateway, `10.48.242.4`)

| Role | Model |
| --- | --- |
| Scheme + intent classify, SQL generation | `qwen-model` (qwen3-coder-30b-fp8) |
| Answer composition | `qwen35-9b` |
| KB embeddings | `qwen3-embedding` |
| KB reranking | `qwen3-reranker` |
| Voice input | `qwen3-asr` |
