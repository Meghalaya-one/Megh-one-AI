"""
Scheme-knowledge base ingest for the RAG path.

Source of truth: the SME-written narrative docs under data/ — the scheme
references and the general FAQs. These answer "what is MGNREGA", "who is
eligible for PMAY-G", "what are the scheme components" — questions that are
NOT database questions and must never be answered by generating SQL.

Chunking follows the NeuralAiGovernanceProject rag_service._chunk() pattern:
one chunk per H2 section, split at H3 when a section is too long, each chunk
prefixed with its heading path so the heading travels into the embedding.

Idempotent: startup calls ingest_kb(), which skips the work if the Qdrant
collection already holds roughly the expected number of points. POST
/api/rag/reingest forces a full rebuild.
"""
import logging
import re
import uuid
from pathlib import Path

from qdrant_client import models

from app import llm, vectorstore
from app.config import settings

logger = logging.getLogger(__name__)

# app/kb_ingest.py -> repo root -> data/
_DATA_PART = Path(__file__).resolve().parents[1] / "data"

# (path, scheme) — scheme is a coarse payload tag for optional filtering later.
# These are the SME-written docs: the authoritative tier.
_SOURCES = [
    ("reference/mgnrega_complete_reference.md", "MGNREGA"),
    ("reference/mgnrega_general_faq.md", "MGNREGA"),
    ("reference/pmay_complete_reference.md", "PMAY-G"),
    ("reference/pmay_general_faq.md", "PMAY-G"),
]

# Scraped encyclopedic / official background, dropped into data/web/*.md by
# the web-ingest step. Picked up automatically. Tagged source_type="web" so the
# retriever / composer can treat them as context, not as the rule of record —
# the SME docs and the live DB stay authoritative.
_WEB_DIR = "web"

_CHUNK_SIZE = 1500

_SCHEME_TAG = re.compile(r"<!--\s*scheme:\s*([A-Za-z0-9-]+)\s*-->", re.IGNORECASE)


def _web_sources() -> list[tuple[str, str]]:
    """(relative_path, scheme) for every data/web/*.md. Scheme comes from a
    `<!-- scheme: X -->` comment, else the filename, else 'GENERAL'."""
    web = _DATA_PART / _WEB_DIR
    if not web.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(web.glob("*.md")):
        scheme = "GENERAL"
        try:
            head = p.read_text(encoding="utf-8")[:2000]
            m = _SCHEME_TAG.search(head)
            if m:
                scheme = m.group(1).upper()
            elif "PMAY" in p.name.upper():
                scheme = "PMAY-G"
            elif "MGNREGA" in p.name.upper() or "NREGA" in p.name.upper():
                scheme = "MGNREGA"
        except OSError:
            pass
        out.append((f"{_WEB_DIR}/{p.name}", scheme))
    return out


def _chunk(content: str) -> list[tuple[str, str]]:
    """Return [(heading_path, text), ...]. H2 sections, split at H3 when large."""
    lines = content.split("\n")
    sections: list[tuple[str, str]] = []
    current_h2 = "(intro)"
    buf: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if buf:
                sections.append((current_h2, "\n".join(buf).strip()))
            current_h2 = line.lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections.append((current_h2, "\n".join(buf).strip()))

    chunks: list[tuple[str, str]] = []
    for heading, text in sections:
        if not text.strip():
            continue
        if len(text) <= _CHUNK_SIZE:
            chunks.append((heading, f"[{heading}]\n{text}"))
            continue
        # Too big — break at H3 boundaries.
        subs = re.split(r"\n(?=### )", text)
        cur_label, cur = heading, ""
        for sub in subs:
            m = re.match(r"### (.+)", sub)
            label = f"{heading} > {m.group(1).strip()}" if m else heading
            if len(cur) + len(sub) <= _CHUNK_SIZE:
                cur = f"{cur}\n{sub}" if cur else sub
                cur_label = label
            else:
                if cur.strip():
                    chunks.append((cur_label, f"[{cur_label}]\n{cur.strip()}"))
                cur, cur_label = sub, label
        if cur.strip():
            chunks.append((cur_label, f"[{cur_label}]\n{cur.strip()}"))

    return [(h, t) for h, t in chunks if len(t.strip()) > 50]


def _collect_chunks() -> list[dict]:
    out: list[dict] = []
    sources = [(f, s, "sme") for f, s in _SOURCES] + \
              [(f, s, "web") for f, s in _web_sources()]
    for fname, scheme, source_type in sources:
        path = _DATA_PART / fname
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("kb_ingest: source not found, skipping — %s", path)
            continue
        for heading, chunk_text in _chunk(text):
            out.append({"scheme": scheme, "doc": fname, "heading": heading,
                        "text": chunk_text, "source_type": source_type})
    return out


async def _embed_all(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    batch = settings.EMBED_BATCH_SIZE
    for i in range(0, len(texts), batch):
        vectors.extend(await llm.call_embedding(texts[i : i + batch]))
    return vectors


async def ingest_kb(force: bool = False) -> dict:
    """Build (or rebuild) the Qdrant KB collection. Returns a small status dict."""
    chunks = _collect_chunks()
    if not chunks:
        logger.warning("kb_ingest: no source chunks found under %s", _DATA_PART)
        return {"ingested": 0, "skipped": True, "reason": "no sources"}

    existing = await vectorstore.collection_count()
    if not force and existing >= int(len(chunks) * 0.9):
        logger.info("kb_ingest: collection already holds %d points (~%d expected) — skipping",
                    existing, len(chunks))
        return {"ingested": 0, "skipped": True, "points": existing}

    n_docs = len(_SOURCES) + len(_web_sources())
    logger.info("kb_ingest: embedding %d chunks from %d docs", len(chunks), n_docs)
    vectors = await _embed_all([c["text"] for c in chunks])
    if len(vectors) != len(chunks):
        raise RuntimeError(f"embedding count {len(vectors)} != chunk count {len(chunks)}")

    await vectorstore.recreate_collection(dim=len(vectors[0]))
    points = [
        models.PointStruct(id=str(uuid.uuid4()), vector=vec, payload=chunk)
        for vec, chunk in zip(vectors, chunks)
    ]
    for i in range(0, len(points), 128):
        await vectorstore.upsert(points[i : i + 128])

    logger.info("kb_ingest: upserted %d points into %s", len(points), settings.QDRANT_COLLECTION)
    return {"ingested": len(points), "skipped": False}
