"""
Loads the SME-curated annotation layer from data/ at startup — few-shot
question-to-SQL examples and the foreign-key/join-graph edges, for both
schemes. These are the real, reviewed artefacts (see data/mgnrega/README.md
and the PMAY equivalent); this module only reads and reshapes them, it does
not invent content.

Deliberately NOT loaded here yet — each needs a design decision before it can
be wired in safely, not just a parser:
  - *_classification_rules.yaml / *_default_rules.yaml: each rule's `condition`
    is a named label (e.g. "per_capita_requested"), not executable code. Needs
    a matcher (regex/keyword, per scheme) mapping a question to a condition
    name before these rules can fire.
  - *_entity_resolver.yaml: large (1500-1800 lines/scheme) alias tables for
    village/district/block/year matching against curated.dim_geography_alias.
    Real, needed for questions that name a village by spelling, but a bigger
    lift than a straight loader.
  - *_response_template.yaml: template selection logic isn't specified by the
    YAML alone (which template applies to which answer shape).
mgnrega_schema_partitions.yaml / pmay_schema_partitions.yaml describe the
pre-migration Excel tables (table_name: mgnrega_expenditure, not
curated.fact_mgnrega_expenditure) — column meanings/synonyms are still valid,
but names need reconciling against SCHEMA_FOR_DEVELOPERS.md before use;
schema_context.py's hand-written version is used for now instead.
"""
import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# app/annotations.py -> repo root -> data/
_DATA_PART = Path(__file__).resolve().parents[1] / "data"

_SCHEME_DIRS = {
    "MGNREGA": _DATA_PART / "mgnrega",
    "PMAY-G": _DATA_PART / "pmay",
}
_FEW_SHOT_FILE = {
    "MGNREGA": "few_shot.yaml",
    "PMAY-G": "pmay_few_shot.yaml",
}
_FK_FILE = {
    "MGNREGA": "foreign_key_augmentation.yaml",
    "PMAY-G": "pmay_foreign_key_augmentation.yaml",
}

_few_shot_cache: dict[str, list[dict]] = {}
_fk_cache: dict[str, dict] = {}


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_all() -> None:
    """Read every scheme's few-shot and FK files once, at process startup."""
    for scheme, folder in _SCHEME_DIRS.items():
        few_shot_path = folder / _FEW_SHOT_FILE[scheme]
        try:
            data = _load_yaml(few_shot_path)
            examples = data.get("sql_generation_examples", [])
            # Drop RETIRED/negative examples (PMAY v2.0 keeps some on purpose,
            # as things the generator must NOT reproduce).
            examples = [e for e in examples if e.get("status") != "RETIRED_v2.0"]
            _few_shot_cache[scheme] = examples
            logger.info("annotations: loaded %d few-shot examples for %s", len(examples), scheme)
        except FileNotFoundError:
            logger.warning("annotations: no few-shot file for %s at %s", scheme, few_shot_path)
            _few_shot_cache[scheme] = []

        fk_path = folder / _FK_FILE[scheme]
        try:
            data = _load_yaml(fk_path)
            _fk_cache[scheme] = data.get("foreign_key_augmentation", {})
            logger.info(
                "annotations: loaded %d join edges for %s",
                len(_fk_cache[scheme].get("edges", [])), scheme,
            )
        except FileNotFoundError:
            logger.warning("annotations: no FK file for %s at %s", scheme, fk_path)
            _fk_cache[scheme] = {}


# Metric/geo synonyms folded to one canonical token before overlap scoring, so
# "how much was spent" matches the "expenditure" example and "man-days" matches
# "person-days". Kept small and scheme-agnostic — just the words that actually
# differ between how a user phrases a metric and how the few-shot file names it.
_FEWSHOT_SYNONYMS = {
    "spend": "expenditure", "spending": "expenditure", "spent": "expenditure",
    "cost": "expenditure", "expense": "expenditure", "expenses": "expenditure",
    "wagebill": "wages", "wage": "wages",
    "manday": "persondays", "mandays": "persondays", "persondays": "persondays",
    "workday": "persondays", "workdays": "persondays",
    "jobcard": "jobcards",
    "hh": "households", "household": "households",
    "house": "houses", "dwelling": "houses", "dwellings": "houses", "unit": "houses",
    "districts": "district", "blocks": "block", "villages": "village",
    "panchayat": "village", "gp": "village",
}
# Question-shape and filler words carry no signal for which example fits.
_FEWSHOT_STOP = {
    "the", "a", "an", "of", "in", "for", "is", "are", "was", "were", "how", "many",
    "much", "what", "which", "and", "to", "by", "on", "across", "all", "show", "me",
    "list", "total", "number", "count", "give", "get", "there", "have", "has", "been",
    "do", "does", "did", "per", "each", "every", "with", "that", "this", "over",
}


def _fewshot_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(t) < 2 or t in _FEWSHOT_STOP:
            continue
        out.add(_FEWSHOT_SYNONYMS.get(t, t))
    return out


def _fewshot_score(q_tokens: set[str], example_question: str) -> float:
    e = _fewshot_tokens(example_question)
    if not q_tokens or not e:
        return 0.0
    # Overlap, lightly normalised so a short exact-topic example isn't buried by
    # a long one that merely shares more words.
    return len(q_tokens & e) / (len(q_tokens | e) ** 0.5)


def few_shot_examples(schemes: list[str], question: str = "", top_k: int = 4) -> list[dict]:
    """Up to top_k examples per scheme, question+sql only (tables not needed in-prompt).

    Ranked by token overlap with `question` so the examples the generator sees are
    the ones on-topic for this query — not just the first top_k in file order,
    which for MGNREGA are all expenditure queries and left job-card / person-day /
    household questions with no worked example at all. `sorted` is stable, so when
    nothing overlaps (or `question` is empty) the original file order is kept."""
    out: list[dict] = []
    q_tokens = _fewshot_tokens(question)
    for scheme in schemes:
        pool = _few_shot_cache.get(scheme, [])
        if q_tokens:
            pool = sorted(pool, key=lambda ex: _fewshot_score(q_tokens, ex["question"]),
                          reverse=True)
        for ex in pool[:top_k]:
            out.append({"question": ex["question"], "sql": ex["sql"].strip()})
    return out


def prohibited_joins_text(schemes: list[str]) -> str:
    """Human-readable list of joins the generator must refuse, with the alternative."""
    lines = []
    for scheme in schemes:
        for edge in _fk_cache.get(scheme, {}).get("edges", []):
            if edge.get("is_prohibited"):
                use_instead = edge.get("use_instead", "aggregate each side independently first")
                lines.append(
                    f"  - NEVER join {edge['from_table']} -> {edge['to_table']} "
                    f"directly. Use {use_instead} instead."
                )
    return "\n".join(lines)
