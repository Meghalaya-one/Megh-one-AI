"""
Shared OpenAI-compatible client for every model role (classifier, SQL generator,
response composer). One module-level httpx.AsyncClient, reused across every
request — creating a new client (and TLS handshake) per call is the single
easiest way to blow the concurrency budget at 20-40 concurrent users.
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# Bounds total concurrent calls to the shared model gateway across the whole
# worker. The 30B SQL model is the scarce resource; without this, 40 concurrent
# pipeline runs would fan out to ~100 simultaneous model calls and collapse
# gateway latency for everyone. Past the limit callers queue; past
# MODEL_QUEUE_TIMEOUT they get ModelBusyError -> 503 (shed, don't pile up).
_gate: asyncio.Semaphore | None = None


class ModelBusyError(RuntimeError):
    """Raised when a model slot can't be acquired within the queue timeout."""


async def init_client() -> None:
    global _client, _gate
    verify = settings.AI_MODEL_CA_BUNDLE_PATH or True
    _client = httpx.AsyncClient(
        verify=verify,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
    )
    _gate = asyncio.Semaphore(settings.MODEL_MAX_CONCURRENCY)


async def close_client() -> None:
    if _client is not None:
        await _client.aclose()


@asynccontextmanager
async def _slot():
    """Hold one model-gateway concurrency slot for the duration of a call."""
    if _gate is None:
        raise RuntimeError("llm client not initialized — call init_client() at startup")
    try:
        await asyncio.wait_for(_gate.acquire(), timeout=settings.MODEL_QUEUE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as e:
        raise ModelBusyError("model gateway saturated — try again shortly") from e
    try:
        yield
    finally:
        _gate.release()


async def call_model(
    *, base_url: str, model: str, api_key: str, prompt: str,
    temperature: float = 0.0, max_tokens: int = 1024, timeout: float = 30.0,
    guided: dict | None = None,
) -> str:
    if _client is None:
        raise RuntimeError("llm client not initialized — call init_client() at startup")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # These Qwen3 models emit a full "Thinking Process: ..." chain-of-thought
        # inline in `content` unless told not to — confirmed live against this
        # gateway. Every pipeline call wants a direct answer, not a reasoning
        # trace, so this is unconditional here rather than per-caller.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if guided:
        # vLLM reads guided_json / guided_choice / guided_regex / guided_grammar
        # as top-level request fields on the OpenAI-compatible route. A gateway
        # that doesn't understand them typically ignores them; one that rejects
        # the schema outright (4xx) is handled below by retrying without them.
        payload.update(guided)

    async with _slot():
        resp = await _client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=timeout,
        )
        if guided and resp.status_code in (400, 404, 422):
            logger.warning(
                "guided decoding rejected by gateway (%s) — retrying without constraints",
                resp.status_code,
            )
            for key in guided:
                payload.pop(key, None)
            resp = await _client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=timeout,
            )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def call_classifier(prompt: str, *, guided: dict | None = None) -> str:
    return await call_model(
        base_url=settings.CLASSIFIER_BASE_URL, model=settings.CLASSIFIER_MODEL,
        api_key=settings.CLASSIFIER_API_KEY, prompt=prompt,
        temperature=settings.CLASSIFIER_TEMPERATURE, max_tokens=200,
        timeout=settings.CLASSIFIER_TIMEOUT_SECONDS,
        guided=guided if settings.GUIDED_DECODING_ENABLED else None,
    )


async def call_sql_generator(prompt: str, *, guided: dict | None = None) -> str:
    return await call_model(
        base_url=settings.SQL_GENERATION_BASE_URL, model=settings.SQL_GENERATION_MODEL,
        api_key=settings.SQL_GENERATION_API_KEY, prompt=prompt,
        temperature=settings.SQL_GENERATION_TEMPERATURE, max_tokens=1024,
        timeout=settings.SQL_GENERATION_TIMEOUT_SECONDS,
        guided=guided if settings.SQL_GUIDED_DECODING_ENABLED else None,
    )


async def call_response_composer(prompt: str) -> str:
    return await call_model(
        base_url=settings.RESPONSE_MODEL_BASE_URL, model=settings.RESPONSE_MODEL,
        api_key=settings.RESPONSE_MODEL_API_KEY, prompt=prompt,
        temperature=settings.RESPONSE_TEMPERATURE, max_tokens=settings.RESPONSE_MAX_TOKENS,
        timeout=settings.RESPONSE_TIMEOUT_SECONDS,
    )


# ── RAG-path model roles ─────────────────────────────────────────────────────


async def call_embedding(texts: str | list[str]) -> list[list[float]]:
    """qwen3-embedding via the gateway's OpenAI-compatible /embeddings route.
    Always returns a list of vectors, one per input, in input order."""
    if _client is None:
        raise RuntimeError("llm client not initialized — call init_client() at startup")
    batch = [texts] if isinstance(texts, str) else list(texts)
    if not batch:
        return []
    if settings.EMBEDDING_PROVIDER == "local":
        # Local CPU model — no gateway hop, no concurrency slot needed.
        from app import local_embed

        return await local_embed.embed(batch)
    async with _slot():
        resp = await _client.post(
            f"{settings.EMBEDDING_BASE_URL.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"},
            json={"model": settings.EMBEDDING_MODEL, "input": batch},
            timeout=settings.EMBEDDING_TIMEOUT_SECONDS,
        )
    resp.raise_for_status()
    data = resp.json()["data"]
    # The spec guarantees order, but sort on index defensively.
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in ordered]


async def call_reranker(query: str, docs: list[str]) -> list[tuple[int, float]]:
    """qwen3-reranker — returns (original_index, score) pairs, best score first.

    Tries the gateway's rerank route first; if that isn't exposed, falls back to
    scoring each doc 0-1 with the classifier model and sorting on that."""
    if _client is None:
        raise RuntimeError("llm client not initialized — call init_client() at startup")
    if not docs:
        return []
    try:
        async with _slot():
            resp = await _client.post(
                f"{settings.RERANKER_BASE_URL.rstrip('/')}/rerank",
                headers={"Authorization": f"Bearer {settings.RERANKER_API_KEY}"},
                json={"model": settings.RERANKER_MODEL, "query": query, "documents": docs},
                timeout=settings.RERANKER_TIMEOUT_SECONDS,
            )
        resp.raise_for_status()
        results = resp.json()["results"]
        pairs = [(r["index"], float(r.get("relevance_score", r.get("score", 0.0)))) for r in results]
        pairs.sort(key=lambda p: -p[1])
        return pairs
    except Exception as e:  # noqa: BLE001 — any failure means "fall back to prompt scoring"
        logger.warning("reranker route unavailable (%s) — falling back to prompt scoring", e)

    scored: list[tuple[int, float]] = []
    for i, doc in enumerate(docs):
        prompt = (
            "Score from 0.0 to 1.0 how well the passage answers the question. "
            'Reply with ONLY the number.\n\n'
            f'Question: "{query}"\n\nPassage:\n{doc[:1500]}\n\nScore:'
        )
        try:
            raw = await call_classifier(prompt)
            m = re.search(r"[01](?:\.\d+)?", raw)
            scored.append((i, float(m.group(0)) if m else 0.0))
        except Exception:  # noqa: BLE001
            scored.append((i, 0.0))
    scored.sort(key=lambda p: -p[1])
    return scored


async def call_asr(audio: bytes, filename: str = "audio.wav") -> str:
    """qwen3-asr via the gateway's OpenAI-compatible /audio/transcriptions route."""
    if _client is None:
        raise RuntimeError("llm client not initialized — call init_client() at startup")
    async with _slot():
        resp = await _client.post(
            f"{settings.ASR_BASE_URL.rstrip('/')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.ASR_API_KEY}"},
            data={"model": settings.ASR_MODEL},
            files={"file": (filename, audio, "application/octet-stream")},
            timeout=settings.ASR_TIMEOUT_SECONDS,
        )
    resp.raise_for_status()
    data = resp.json()
    return data.get("text", "") if isinstance(data, dict) else str(data)
