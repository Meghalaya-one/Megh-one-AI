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

# Matches the exact refusal sentence the compose prompt is told to use (see
# answer_from_kb below), with or without the leading "That" — the composer
# sometimes tacks this on as a trailing hedge after an otherwise complete
# answer, and only that sentence should be stripped, not the whole response.
_REFUSAL_SENTENCE = re.compile(
    r"(?:that\s+)?isn'?t covered in the (?:scheme )?reference material\.?",
    re.IGNORECASE,
)

# The general_faq.md sources are flat "### Question?\nAnswer." pairs (see
# kb_ingest._chunk), so a chunk's own body opens by restating the question as
# its heading. The high-confidence path below returns that body verbatim, and
# without this the chat bubble reads as a literal transcript — "What documents
# are needed for X? Typically..." — instead of a natural answer. Drop that
# leading heading line whenever it's phrased as a question (ends in "?");
# non-FAQ section headings (e.g. "### Eligibility Criteria") don't match and
# are left as useful context.
_LEADING_QUESTION_HEADING = re.compile(r"\A#{1,6}[ \t]+.*\?[ \t]*\n+")


def _clean_for_display(text: str) -> str:
    return _MD_HEADING.sub("", text).strip()


def _strip_heading(text: str) -> str:
    # Chunks are stored as "[heading path]\n<body>" — drop the tag for display.
    if text.startswith("[") and "]\n" in text:
        text = text.split("]\n", 1)[1]
    text = _LEADING_QUESTION_HEADING.sub("", text, count=1)
    return _clean_for_display(text)


async def retrieve(question: str, scheme: str | None = None) -> list[dict]:
    """Return the reranked, score-filtered candidate chunks, best first.
    `scheme`, when given, restricts retrieval to that scheme's chunks."""
    try:
        vecs = await llm.call_embedding(question)
    except Exception as e:  # noqa: BLE001
        logger.warning("rag.retrieve: embedding failed — %s", e)
        return []
    candidates = await vectorstore.search(vecs[0], top_k=settings.RAG_TOP_K, scheme=scheme)
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


async def answer_from_kb(question: str, scheme: str | None = None) -> dict | None:
    """Answer a scheme-knowledge question, or None if the KB doesn't cover it."""
    chunks = await retrieve(question, scheme=scheme)
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
Do not invent numbers, dates, or amounts. No Markdown headings ("#", "##", "###").

Formatting — this is read in a chat bubble, so structure it for a quick scan:
- If the answer is a single fact, give it in one to two plain sentences.
- If the answer lists two or more items (schemes, sectors, steps, documents,
  eligibility conditions, etc.), use one short bullet per item on its own line,
  formatted exactly as "- **Item name** — one-line description", never a
  paragraph that runs items together in prose.
- Bold (**text**) only the item name being defined in each bullet, nothing else.
- Keep each bullet to one line. Put a blank line between an intro sentence and
  the bullets, and between the bullets and any closing sentence.

Question: "{question}"

Reference passages:
{context}

Answer:"""
        answer = await llm.call_response_composer(prompt)
        # The composer sometimes answers correctly AND tacks on a stray
        # "that isn't covered..." hedge sentence (about some tangential detail
        # it wasn't asked for) even though the actual question was answered in
        # full from the passages above it. A blind substring check on the
        # whole answer used to discard that entire good answer whenever the
        # phrase appeared anywhere — strip just the refusal sentence instead,
        # and only treat it as "no answer" when nothing substantive remains.
        cleaned = _REFUSAL_SENTENCE.sub("", answer).strip()
        if not cleaned:
            # Not just a stray hedge sentence — the composer refused outright.
            # Runs at non-zero temperature, so on passages that genuinely do
            # answer the question this is sometimes just a bad sample: verified
            # directly on CM Elevate's intro chunk at top_score ~0.77 (well
            # above RAG_MEDIUM_CONFIDENCE) — the identical prompt outright
            # refused twice in one run, then answered correctly on the next
            # sample with no code change. One retry on the same (already-good)
            # context is cheap; a second outright refusal is trusted as real.
            answer = await llm.call_response_composer(prompt)
            cleaned = _REFUSAL_SENTENCE.sub("", answer).strip()
            if not cleaned:
                return None
        return {
            "answer": _clean_for_display(cleaned),
            "confidence": "high" if top_score >= settings.RAG_HIGH_CONFIDENCE else "medium",
            "sources": sources,
        }

    logger.info("rag.answer_from_kb: top score %.3f below medium floor — no answer", top_score)
    return None
