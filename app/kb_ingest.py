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
    ("reference/pmay_gramin_complete_reference.md", "PMAY-G"),
    ("reference/pmay_gramin_general_faq.md", "PMAY-G"),
    ("reference/focusplus_complete_reference.md", "Focus Plus"),
    ("reference/focusplus_general_faq.md", "Focus Plus"),
    ("reference/cmelevate_complete_reference.md", "CM Elevate"),
    ("reference/cmelevate_general_faq.md", "CM Elevate"),
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
            elif "FOCUS" in p.name.upper():
                scheme = "Focus Plus"
            elif "ELEVATE" in p.name.upper() or "CMELEVATE" in p.name.upper():
                scheme = "CM Elevate"
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

        if heading == "(intro)":
            # No real H2 in this doc — the general_faq.md files are flat
            # "### Question?" lists with no H2 at all, so the whole file lands
            # here as one oversized "(intro)" section. Packing several
            # unrelated Q&As into one chunk buried specific answers (e.g.
            # PMAY-G's "what documents are required" answer ended up inside a
            # chunk labeled after a different, unrelated question, and the
            # compose LLM skimmed past it). Each "###" is a self-contained
            # unit here, so give it its own chunk instead of packing.
            for sub in subs:
                sub = sub.strip()
                if not sub:
                    continue
                m = re.match(r"### (.+)", sub)
                label = m.group(1).strip() if m else heading
                chunks.append((label, f"[{label}]\n{sub}"))
            continue

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


# A PMAY-G-tagged chunk whose OWN heading doesn't say "Urban" can still carry a
# nested subsection that does — e.g. pmay_complete_reference.md's H2 "## 7.
# Application and Registration Process" is short enough to survive as ONE
# chunk (heading "7. Application and Registration Process", no "Urban" in it)
# while its body has both "### 7.1 PMAY-Urban" and "### 7.2 PMAY-Gramin" as
# sub-headings. The heading-only check below can't see that. Strip any such
# Urban sub-heading's block (through the next heading of any level, or end of
# text) out of a PMAY-G chunk's body before it's embedded — the Gramin content
# in the rest of the chunk is unaffected and still answers the question.
_URBAN_SUBSECTION = re.compile(
    r"\n#{2,6}[ \t]*[^\n]*\burban\b[^\n]*\n.*?(?=\n#{2,6}[ \t]|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_urban_subsections(text: str) -> str:
    return _URBAN_SUBSECTION.sub("\n", text)


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
            # A PMAY doc can carry both Gramin and Urban sections under one
            # filename-derived "PMAY-G" tag (e.g. data/web/PMAY_Wikipedia.md's
            # "Income Categories (Urban)", "Eligibility Conditions (Urban
            # CLSS)"). Re-tag those sections "PMAY-U" — a scheme this bot
            # doesn't hold — so a PMAY-G-scoped retrieval (vectorstore.search's
            # scheme filter) can never surface Urban content in a PMAY-G answer.
            section_scheme = scheme
            if scheme == "PMAY-G" and re.search(r"\burban\b", heading, re.IGNORECASE):
                section_scheme = "PMAY-U"
            elif scheme == "PMAY-G":
                chunk_text = _strip_urban_subsections(chunk_text)
            out.append({"scheme": section_scheme, "doc": fname, "heading": heading,
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
