# Inference Requirements — hand to the AIOps team

**Goal:** production model serving for **40 concurrent / ~200 DAU / 4 schemes**,
NL→SQL primary workload, on the 2× H200 NVL node (`meghalaya-3`).
**Cluster:** microk8s, namespace `def`, gateway `https://10.48.242.4`.

Everything below is **server-side** (KServe / vLLM deployments on the GPU box).
The application side is already done — see the last section.

---

## Model roles (target end state)

| Deployment | Model | Role | Endpoint |
|---|---|---|---|
| `qwen-model` | qwen3-coder-30b-fp8 | **SQL generation only** | `/openai/v1` |
| `qwen35-9b` | qwen3.5-9b | classify + intent + entity **and** answer composition | `/openai/v1` |
| `qwen3-4b` *(to deploy)* | Qwen3-4B-Instruct | dedicated classify + intent + entity (takes over from the 9B) | `/openai/v1` |
| `qwen3-embedding` | qwen3-embedding-0.6b | RAG embeddings | `/openai/v1` |
| `qwen3-reranker` | qwen3-reranker-0.6b | RAG rerank | `/openai/v1` |
| `qwen3-asr` | qwen3-asr-1.7b | voice input | `/openai/v1` |

---

## Task 1 — Delete the duplicate

Delete InferenceService **`qwen3-coder-30b`** (huggingface runtime, stuck
`deploying` with `CardInsufficientMemory`). It duplicates `qwen-model`.

## Task 2 — Right-size GPU-memory reservations

Both cards are full **by reservation** (new pods fail `CardInsufficientMemory`
though live use is ~60%). Set explicit per-deployment GPU-memory requests +
matching `--gpu-memory-utilization`:

| Deployment | GPU mem request | `--gpu-memory-utilization` |
|---|---|---|
| `qwen-model` (30B) | 95 GiB | 0.68 |
| `qwen35-9b` | 40 GiB | 0.30 |
| `qwen3-embedding` | 6 GiB | 0.05 |
| `qwen3-reranker` | 6 GiB | 0.05 |
| `qwen3-asr` | 8 GiB | — |

Layout: **Card A** = `qwen-model` alone (big KV batch). **Card B** = 9B +
embedding + reranker + asr (~60 GiB) + headroom for the 4B (Task 6).

## Task 3 — vLLM flags on `qwen-model` (30B)

Append to the vLLM args (keep existing args). **All lossless — output unchanged.**

```
--enable-prefix-caching
--kv-cache-dtype=fp8
--speculative-config={"method":"ngram","num_speculative_tokens":5,"prompt_lookup_max":4}
--guided-decoding-backend=xgrammar
--max-num-seqs=48
--enable-chunked-prefill
--max-model-len=16384
```

- If `--speculative-config` JSON form is rejected (older vLLM):
  `--speculative-model=[ngram] --num-speculative-tokens=5 --ngram-prompt-lookup-max=4`
- If `--kv-cache-dtype=fp8` fails: try `fp8_e4m3`.
- `--enable-chunked-prefill` is default-on in vLLM ≥ 0.6.3 — omit if it errors.
- Apply one at a time; confirm `Running` between each.

## Task 4 — vLLM flags on `qwen35-9b`

Append:
```
--enable-prefix-caching
--guided-decoding-backend=xgrammar
```
(The app sends `guided_json` constraints for the classification calls now routed here.)

## Task 5 — API endpoint consistency

`qwen35-9b` and `qwen3-asr` currently serve **`/v1/predict`** (prebuilt / audio
runtimes). They must answer OpenAI routes on `https://10.48.242.4/openai/v1`:

- `qwen35-9b` → `POST /openai/v1/chat/completions`, body `{"model":"qwen35-9b",…}`
- `qwen3-asr` → `POST /openai/v1/audio/transcriptions` (multipart)

Preferred: **redeploy both on the plain `vllm` runtime** (like `qwen-model`) so
all models share one URL / auth / metrics surface. If the gateway already routes
by `model` name on `/openai/v1`, confirm via the acceptance test and skip.

## Task 6 — Deploy the dedicated classifier (when Card B has room)

Deploy **`qwen3-4b`**:
- Runtime **`vllm`** (not huggingface)
- Weights: `Qwen3-4B-Instruct` on a PVC
- GPU mem request **16 GiB**, `--gpu-memory-utilization=0.12`, `--max-model-len=8192`,
  `--guided-decoding-backend=xgrammar`
- Served model name `qwen3-4b`, on `/openai/v1`

Then the app sets `CLASSIFIER_MODEL=qwen3-4b`. Until then it uses `qwen35-9b`.

## Task 7 — Monitoring to expose (per model)

`vllm:num_requests_running`, `vllm:num_requests_waiting`,
`vllm:gpu_cache_usage_perc`, `vllm:prefix_cache_hit_rate`, speculative-decode
acceptance rate, p50/p95/p99 latency, tokens/sec; plus per-card GPU util + memory
on `meghalaya-3`.

---

## Acceptance tests

**1. All models on the shared OpenAI endpoint**
```bash
curl -sk https://10.48.242.4/openai/v1/chat/completions \
  -H "Authorization: Bearer <qwen35-9b key>" \
  -d '{"model":"qwen35-9b","messages":[{"role":"user","content":"say hi"}]}'
```
HTTP 200 with `choices[0].message.content`. Repeat for `qwen-model`, `qwen3-4b`.
ASR: `POST /openai/v1/audio/transcriptions` with a WAV → `{"text":…}`.

**2. Prefix caching** — same 2 KB SQL prompt twice; 2nd materially faster;
`prefix_cache_hit_rate > 0`.

**3. Speculative decoding** — acceptance-rate metric > 0 on `qwen-model` during
SQL generation.

**4. Load test — 40 concurrent, 5 min**
```bash
hey -z 5m -c 40 -m POST -T application/json \
  -H "Authorization: Bearer <qwen-model key>" \
  -d '{"model":"qwen-model","max_tokens":300,"messages":[{"role":"user","content":"Write one PostgreSQL SELECT: total person_days by district FY 2023-24. SQL only."}]}' \
  https://10.48.242.4/openai/v1/chat/completions
```
**Pass:** p95 ≤ 12 s, 0 errors, `num_requests_waiting` bounded (not growing
unbounded).
**If it fails:** add a 3rd H200 and run `qwen-model` at **2 replicas**
(`maxReplicas: 2`) behind the gateway — the only change that doubles SQL-gen
throughput.

---

## Already handled on the application side (no AIOps action)

- `CLASSIFIER_MODEL` → `qwen35-9b` (was `qwen-model`); the 30B does SQL only.
  `SQL_GENERATION_MODEL` stays `qwen-model`. Config in `backend/config.py`.
- App bounds its own gateway concurrency (`MODEL_MAX_CONCURRENCY`) and sheds with
  HTTP 503 past the limit; exact-match + semantic response caches; sends
  `guided_json` / `guided_regex` constraints (safe if the gateway ignores them).
- App = 2 uvicorn workers × 2 VMs, stateless, behind nginx. `GET /metrics` on the
  app exposes route counts, p50/p95/p99, `busy_rejections_total`, cache hit rates.
