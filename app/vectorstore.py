"""
Qdrant access for the scheme-knowledge base (the RAG path).

One module-level AsyncQdrantClient, opened at startup and closed at shutdown —
same reasoning as the shared httpx client and the asyncpg pool: a fresh client
per request wastes connections at the 20-40 concurrent target.

Qdrant runs on the data box (10.48.242.4:6333), alongside PostgreSQL and the
model gateway. It is only used for scheme-knowledge retrieval; all numeric
answers come from megh_db, never from here.
"""
import logging

from qdrant_client import AsyncQdrantClient, models

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncQdrantClient | None = None


async def init_qdrant() -> None:
    global _client
    _client = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY or None,
        timeout=30,
    )
    logger.info("qdrant client ready (%s)", settings.QDRANT_URL)


async def close_qdrant() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def get_client() -> AsyncQdrantClient:
    if _client is None:
        raise RuntimeError("qdrant client not initialized — call init_qdrant() at startup")
    return _client


async def collection_count() -> int:
    """Point count for the KB collection, or -1 if the collection is absent."""
    client = get_client()
    try:
        info = await client.get_collection(settings.QDRANT_COLLECTION)
        return info.points_count or 0
    except Exception:  # noqa: BLE001 — missing collection is not an error here
        return -1


async def recreate_collection(dim: int) -> None:
    client = get_client()
    await client.recreate_collection(
        collection_name=settings.QDRANT_COLLECTION,
        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
    )
    logger.info("qdrant collection %s (re)created, dim=%d", settings.QDRANT_COLLECTION, dim)


async def upsert(points: list[models.PointStruct]) -> None:
    client = get_client()
    await client.upsert(collection_name=settings.QDRANT_COLLECTION, points=points, wait=True)


async def search(vector: list[float], top_k: int) -> list[dict]:
    """Returns [{score, text, doc, heading, scheme}, ...] best score first."""
    client = get_client()
    # qdrant-client >= 1.10 replaced .search() with .query_points().
    response = await client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=vector,
        limit=top_k,
        with_payload=True,
    )
    hits = response.points
    return [
        {
            "score": float(h.score),
            "text": h.payload.get("text", ""),
            "doc": h.payload.get("doc", ""),
            "heading": h.payload.get("heading", ""),
            "scheme": h.payload.get("scheme", ""),
            "source_type": h.payload.get("source_type", "sme"),
        }
        for h in hits
    ]
