# Security — OWASP controls

How this service defends itself, mapped to the [OWASP Top 10 (2021)](https://owasp.org/Top10/).
Controls live at **two layers** — the FastAPI app (`app/`) and the nginx edge
(`deploy/nginx/`) — on purpose: the edge catches generic attack traffic, the app
enforces the guarantees that actually matter (read-only SQL, tenant isolation).
Neither layer trusts the other to be present or correctly configured.

Every app-layer control is config-gated (`app/config.py`, `.env.example`) with a
safe default, so local dev is unaffected.

---

## A01 — Broken Access Control

| Control | Where |
| --- | --- |
| JWT-gated `/api/query`, `/api/history/*`, `/admin/*`; role gates `require_user` / `require_admin` / `require_super` | `app/deps.py`, routers |
| Role + geography + granularity + scheme checks on the **generated SQL** before it runs | `app/auth.py` |
| Tenant isolation — `tenant_admin` pinned to their own tenant on every admin read | `app/routers/admin.py::_tenant_filter` |
| `/api/rag/reingest` (expensive KB rebuild) now **admin-only**; `/api/rag/status` requires a user | `app/routers/rag.py` |
| `/metrics` requires `X-Metrics-Token` (or an admin JWT); IP-allowlisted at the edge | `app/main.py`, `deploy/nginx/…` |
| `/health` returns `{"status"}` only to anonymous callers — the per-component breakdown and the `insecure_config` flag need an admin JWT / metrics token | `app/main.py::health` |
| `/admin` + `/admin-ui` IP-allowlisted to office networks | `deploy/nginx/nginx-nlpservice.conf` |

## A02 — Cryptographic Failures

| Control | Where |
| --- | --- |
| PBKDF2-HMAC-SHA256 password hashing, 200k iterations, constant-time compare | `app/security.py` |
| HS256 JWT, issuer + expiry checked, constant-time signature compare | `app/security.py` |
| **TLS 1.2/1.3 only**, modern ciphers, OCSP stapling, HTTP→HTTPS redirect | `deploy/nginx/nginx-nlpservice.conf` |
| HSTS (`max-age=1y; includeSubDomains`) at both app and edge | `app/middleware/security_headers.py`, nginx |
| Startup **secret guard** — `CRITICAL` log + `/health` `insecure_config` when `JWT_SECRET` / `ADMIN_TOKEN` are still defaults or too short. Does **not** block boot; the deploy checklist owns enforcement | `app/config.py::insecure_secrets`, `app/main.py` |
| `.env` is `chmod 600`, gitignored | `deploy/DEPLOYMENT.md` |

## A03 — Injection

| Control | Where |
| --- | --- |
| LLM-generated SQL is validated: single statement, `SELECT`/`WITH` lead only, no write/DDL keywords, forced `LIMIT` | `app/db.py::_assert_safe`, `run_readonly` |
| DB connection is a **SELECT-only role** (`megh_app` / `megh_readonly`) — second layer, not a substitute | `deploy/sql/01_create_megh_app_role.sql` |
| Our own SQL is always parameter-bound, never interpolated | `app/db.py::fetch_rows` etc. |
| `question` field length-capped (`MAX_QUESTION_CHARS`) and C0-control-char stripped; `session_id` pattern-restricted; length caps on every admin/auth free-text field and list | `app/routers/*.py` |
| **OWASP CRS WAF** in front (SQLi/XSS/RCE families), staged rollout | `deploy/nginx/modsecurity/` |

## A04 — Insecure Design

| Control | Where |
| --- | --- |
| Global request-body cap (`MAX_REQUEST_BYTES`, 256 KB) — header check + streamed-byte guard; transcribe route exempt (own 10 MB cap) | `app/middleware/limits.py` |
| Per-IP sliding-window rate limits: `/api/query` (30/60s), `/api/auth/login` (`LOGIN_RL_MAX`/`LOGIN_RL_WINDOW_SEC`) | `app/middleware/rate_limit.py` |
| Optional **Redis rate-limit backend** so the limit holds across 2 workers × 2 VMs (`RATELIMIT_BACKEND=redis`) | `app/middleware/rate_limit.py` |
| Model-gateway concurrency cap + queue-shed (503) — `MODEL_MAX_CONCURRENCY` | `app/config.py`, `app/llm.py` |
| Edge `limit_req` / `limit_conn` zones, body + header + send timeouts | `deploy/nginx/nginx-nlpservice.conf` |
| Query-param `limit` bounds on admin/history list endpoints | `app/routers/admin.py`, `app/routers/history.py` |

## A05 — Security Misconfiguration

| Control | Where |
| --- | --- |
| Security headers on every response: `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy`; `Cache-Control: no-store` on `/api/*`; `Server` header stripped | `app/middleware/security_headers.py` + nginx `add_header … always` |
| `/docs`, `/redoc`, `/openapi.json` disabled unless `ENV=dev` | `app/main.py` |
| **No stack traces / DB errors in responses** — global exception handler returns `{"detail": "internal error", "request_id": …}`; routers no longer interpolate exception text | `app/main.py::_unhandled`, routers |
| `X-Request-ID` on every response for log correlation without disclosure | `app/middleware/security_headers.py` |
| `server_tokens off`; dotfiles denied at the edge | `deploy/nginx/nginx-nlpservice.conf` |

### CSP note

The served HTML (`web/*.html`) uses inline `<script>`/`<style>` and ~30 inline
`onclick` handlers, so `script-src`/`style-src` keep `'unsafe-inline'`. The
directives that are effective **without** a UI rewrite are enforced:
`frame-ancestors 'none'`, `base-uri 'self'`, `object-src 'none'`,
`form-action 'self'`, `default-src 'self'`. To tighten later: set
`CSP_REPORT_ONLY` to a strict nonce-based policy, watch the browser reports,
externalise the inline JS, then promote it to `CSP_ENFORCE`.

## A06 — Vulnerable & Outdated Components

- Dependencies are pinned `>=working,<next-major` (`requirements.txt`) with a
  fully-locked `requirements.lock` for reproducible builds.
- Run `pip-audit -r requirements.lock` (or `pip install pip-audit && pip-audit`)
  on a schedule / in CI. Not yet wired — tracked as follow-up.

## A07 — Identification & Authentication Failures

| Control | Where |
| --- | --- |
| Brute-force throttle on `POST /api/auth/login` (app) + stricter `limit_req` zone (edge) | `app/middleware/rate_limit.py`, nginx |
| Failed logins recorded (`app.login_events`) with IP + user-agent | `app/routers/auth.py`, `app/appdb.py` |
| Password min length enforced on create/update/bootstrap | `app/security.py`, `app/routers/*` |
| One-time `bootstrap` endpoint closes as soon as a user exists (or needs `ADMIN_TOKEN`) | `app/routers/auth.py::bootstrap` |
| Short-ish session TTL (`JWT_TTL_MINUTES`, 12h default) | `app/config.py` |

**Known gap:** JWTs are stateless — logout is advisory, there is no revocation
list. Acceptable for a 12h officer session; revisit with a `jti` + Redis denylist
if tokens ever need to be killed mid-session.

## A08 — Software & Data Integrity Failures

- No deserialization of untrusted objects; request bodies are JSON via Pydantic
  models only.
- `frame-ancestors 'none'` + `X-Frame-Options: DENY` prevent UI redress.
- CORS is closed by default (`CORS_ALLOW_ORIGINS` blank ⇒ no CORS middleware);
  opening it requires an explicit exact-origin allowlist.

## A09 — Security Logging & Monitoring Failures

| Control | Where |
| --- | --- |
| Every query audited (user, tenant, role, route, granularity, allow/deny, row count, IP, latency) to `app.query_audit` + a JSONL mirror | `app/conversation_store.py`, `app/routers/query.py::_mirror_audit` |
| Login / logout / bootstrap events to `app.login_events` | `app/appdb.py::log_login` |
| Client IP is **proxy-trust aware** — `X-Forwarded-For` only honoured from `TRUSTED_PROXIES`, so the audit trail can't be poisoned by a spoofed header | `app/net.py` |
| `X-Request-ID` ties a client-visible 500 to the server log line | `app/middleware/security_headers.py` |
| WAF audit log for attack traffic | `/var/log/nginx/modsec_audit.log` |

**Follow-up:** ship these logs to a SIEM / alert on `login_fail` spikes and
`route=denied` bursts.

## A10 — Server-Side Request Forgery

- The only outbound calls are to fixed, config-pinned hosts (the model gateway,
  Qdrant, Postgres on `10.48.242.4`). No user input controls a URL, host, or
  port anywhere in the request path.

---

## Deploy checklist (security-relevant)

See `deploy/DEPLOYMENT.md` for the full list. The essentials:

- [ ] `JWT_SECRET`, `ADMIN_TOKEN` are real random values (`python -c "import secrets;print(secrets.token_urlsafe(48))"`) — startup logs `CRITICAL` otherwise
- [ ] `DATABASE_URL` uses the SELECT-only role, not a superuser
- [ ] TLS terminates at nginx; `:80` redirects to `:443`
- [ ] `ENV=prod` (so `/docs` is closed)
- [ ] `METRICS_TOKEN` set; `/admin` + `/metrics` `allow`/`deny` CIDRs filled in
- [ ] `TRUSTED_PROXIES` matches the actual vFirewall/LB subnet
- [ ] `set_real_ip_from` in nginx matches the same
- [ ] ModSecurity + CRS installed, baked in `DetectionOnly`, then flipped to `On` (`deploy/nginx/modsecurity/README.md`)
- [ ] `.env` is `chmod 600`
- [ ] retention job scheduled (`deploy/sql/02_retention_policy.sql`)

## Tests

`python tests/test_security.py` — headers, body/field limits, endpoint gating,
login throttle, error hygiene, secret guard. Runs without a DB or the model
gateway.
