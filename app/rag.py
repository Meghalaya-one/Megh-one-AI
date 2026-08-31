"""
RAG path — answers general scheme-knowledge questions from the data/
narrative docs (see kb_ingest.py), never from the database.

Flow (the NeuralAiGovernanceProject standard, adapted to Qdrant + qwen3):
  1. embed the question           -> qwen3-embedding
  2. vector search the KB         -> Qdrant, RAG_TOP_K candidates
  3. rerank the candidates        -> qwen3-reranker, keep RAG_RERANK_TOP_N
  4. confidence tiers:
       >= RAG_HIGH_CONFIDENCE   -> return the top chunk verbatim (no compose call)
       >= RAG_MEDIUM_CONFIDENCE -> compose an answer from the kept chunks
       below                    -> return None; the pipeline says "not in the
                                   scheme reference material"
"""
import logging
import re

from app import llm, vectorstore
from app.config import settings

logger = logging.getLogger(__name__)

# The KB docs are Markdown, but the chat UI renders answers as plain text, so
# raw "### " heading markers leak through and look unprofessional. Drop the
# leading hashes and keep the heading text as its own line.
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}[ \t]+")


def _clean_for_display(text: str) -> str:
    return _MD_HEADING.sub("", text).strip()


def _strip_heading(text: str) -> str:
    # Chunks are stored as "[heading path]\n<body>" — drop the tag for display.
    if text.startswith("[") and "]\n" in text:
        text = text.split("]\n", 1)[1]
    return _clean_for_display(text)


async def retrieve(question: str) -> list[dict]:
    """Return the reranked, score-filtered candidate chunks, best first."""
    try:
        vecs = await llm.call_embedding(question)
    except Exception as e:  # noqa: BLE001
        logger.warning("rag.retrieve: embedding failed — %s", e)
        return []
    candidates = await vectorstore.search(vecs[0], top_k=settings.RAG_TOP_K)
    if not candidates:
        return []

    # Reranker is opt-in — the deployed qwen3-reranker inverts relevance on this
    # KB, so by default we trust the vector order (bge-small) directly.
    if not settings.RERANKER_ENABLED:
        return [c for c in candidates[: settings.RAG_RERANK_TOP_N]
                if c.get("score", 0.0) >= settings.RAG_MIN_SCORE] or candidates[: settings.RAG_RERANK_TOP_N]

    order = await llm.call_reranker(question, [c["text"] for c in candidates])
    reranked: list[dict] = []
    for idx, score in order[: settings.RAG_RERANK_TOP_N]:
        if 0 <= idx < len(candidates) and score >= settings.RAG_MIN_SCORE:
            c = dict(candidates[idx])
            c["score"] = score
            reranked.append(c)
    # If the reranker filtered everything out, fall back to the raw vector order.
    return reranked or candidates[: settings.RAG_RERANK_TOP_N]


async def answer_from_kb(question: str) -> dict | None:
    """Answer a scheme-knowledge question, or None if the KB doesn't cover it."""
    chunks = await retrieve(question)
    if not chunks:
        return None

    top = chunks[0]
    top_score = float(top.get("score", 0.0))
    sources = [{"doc": c["doc"], "heading": c["heading"],
                "source_type": c.get("source_type", "sme")} for c in chunks]

    if top_score >= settings.RAG_HIGH_CONFIDENCE:
        return {
            "answer": _strip_heading(top["text"]),
            "confidence": "high",
            "sources": sources[:1],
        }

    if top_score >= settings.RAG_MEDIUM_CONFIDENCE:
        context = "\n\n---\n\n".join(_strip_heading(c["text"]) for c in chunks)
        prompt = f"""Answer the question using ONLY the reference passages below. If they do
not contain the answer, say "That isn't covered in the scheme reference material."
Keep it to a short paragraph. Do not invent numbers, dates, or amounts.
Write plain prose — no Markdown headings ("#", "##", "###") or bold markers.

Question: "{question}"

Reference passages:
{context}

Answer:"""
        answer = await llm.call_response_composer(prompt)
        if "isn't covered" in answer.lower() or "not covered" in answer.lower():
            return None
        return {
            "answer": _clean_for_display(answer),
            "confidence": "high" if top_score >= settings.RAG_HIGH_CONFIDENCE else "medium",
            "sources": sources,
        }

    logger.info("rag.answer_from_kb: top score %.3f below medium floor — no answer", top_score)
    return None
