"""
Entity resolution — "the user typed some text, which stored database value did
they mean?" Ported from data/*/*_entity_resolver.yaml (the SME-curated
alias catalogues), scoped down from that file's full 8-stage pipeline to what's
implementable without an embedding index: stages 1-3 and 6 (exact, alias,
squash, RapidFuzz), skipping stage 4 acronym as a separate stage (folded into
alias matching below) and stage 7 embedding fallback.

District/block/year are small closed sets (12, ~56, 4) — loaded once from the
YAML at startup and matched entirely in memory. Village is NOT: the YAML only
documents the 238 names that collide, not the full ~6,000-village catalogue —
that lives in curated.dim_geography / curated.dim_geography_alias, so village
resolution is a live DB query (using the GIN trigram index already built
there), not a YAML lookup.
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from app.db import fetch_rows

logger = logging.getLogger(__name__)

_DATA_PART = Path(__file__).resolve().parents[1] / "data"
_RESOLVER_FILE = {
    "MGNREGA": _DATA_PART / "mgnrega" / "mgnrega_entity_resolver.yaml",
    "PMAY-G": _DATA_PART / "pmay" / "pmay_entity_resolver.yaml",
}

# scheme -> dimension ("district" | "block" | "year") -> list of value dicts
_catalog: dict[str, dict[str, list[dict]]] = {}
# scheme -> set of (folded_a, folded_b) pairs fuzzy must never resolve across
_blocked: dict[str, set[tuple[str, str]]] = {}
# scheme -> [{canonical, tokens, stage_order}] for the house_status closed set,
# and scheme -> [{tokens, members}] for its derived stage groups. PMAY-G only in
# practice; keyed by scheme to stay parallel with _catalog.
_house_status: dict[str, list[dict]] = {}
_house_status_groups: dict[str, list[dict]] = {}

_FUZZY_ACCEPT = 90
_FUZZY_RUNNER_UP_GAP = 5

# Only the construction stages that have NO is_completed / is_in_progress boolean
# of their own are resolved to a status_name literal. "Completed" and the
# in-progress group are deliberately excluded — schema_context._PMAY_RULES rule 2
# mandates the boolean roll-ups for those, and the SQL few-shots already cover
# them; resolving them here would fight that path (and the roll-up NULL trap).
_HOUSE_STATUS_RESOLVABLE = {
    "Roof Cast", "Plinth", "House Sanctioned",
    "Existing site(Old House)", "Proposed Site",
}
# Single-token phrases distinctive enough to match on their own. Any other
# one-word alias ("roof", "foundation", "sanctioned") is dropped at load.
_HOUSE_STATUS_SOLO_OK = {"PROPOSED", "PLINTH"}
# Value-alias phrases (folded) too overloaded to match: "sanctioned" is also a
# date filter and an amount (pmay_entity_resolver.yaml flags this), and
# "not started" names the 3-stage group, not the single House Sanctioned stage.
_HOUSE_STATUS_ALIAS_DENY = {
    "SANCTIONED", "SANCTION", "NOT STARTED", "YET TO START",
    "APPROVED NOT STARTED", "YET TO BEGIN", "NO CONSTRUCTION",
}


@dataclass
class Resolved:
    status: str  # "resolved" | "ambiguous" | "not_found"
    entity_type: str
    user_text: str
    canonical: str | None = None
    confidence: float = 0.0
    candidates: list[dict] = field(default_factory=list)
    message: str = ""
    values: list[str] = field(default_factory=list)  # multi-valued resolves (house_status)
    display: str | None = None  # human-readable name for the answer ("West Garo Hills"),
    #                             as opposed to `canonical` which is the DB literal.


def fold(text: str) -> str:
    """normalisation.fold from the YAML: trim, collapse space, uppercase, drop
    trailing qualifier words, & -> AND, hyphen/slash/parens -> space."""
    t = text.strip().upper()
    t = re.sub(r"[.,']", "", t)
    t = re.sub(r"\s+(BLOCK|DISTRICT|VILLAGE|C&RD|AC|CONSTITUENCY)$", "", t)
    t = t.replace("&", "AND")
    t = re.sub(r"[-/()]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _squash(folded: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", folded)


def _hs_tokens(text: str) -> list[str]:
    """Fold + split to bare alnum tokens — the unit house_status matching works
    in (order-independent token containment, not substring)."""
    return [w for w in re.sub(r"[^A-Z0-9]+", " ", fold(text)).split() if w]


def load_all() -> None:
    for scheme, path in _RESOLVER_FILE.items():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("entity_resolver: no file for %s at %s", scheme, path)
            _catalog[scheme] = {}
            _blocked[scheme] = set()
            _house_status[scheme] = []
            _house_status_groups[scheme] = []
            continue

        dims = data.get("dimensions", {})
        _catalog[scheme] = {}
        for dim_name in ("district", "block", "year"):
            values = dims.get(dim_name, {}).get("values", [])
            if values:
                _catalog[scheme][dim_name] = values

        _load_house_status(scheme, dims.get("house_status", {}) or {})

        pairs: set[tuple[str, str]] = set()
        for a, b in data.get("blocked_matches", {}).get("pairs", []):
            pairs.add((fold(a), fold(b)))
        for dim_values in _catalog[scheme].values():
            for v in dim_values:
                for other in v.get("never_fuzzy_to", []):
                    pairs.add((fold(v["canonical"]), fold(other)))
        _blocked[scheme] = pairs

        logger.info(
            "entity_resolver: %s loaded — %s, %d blocked pairs, %d house_status phrases",
            scheme, {k: len(v) for k, v in _catalog[scheme].items()}, len(pairs),
            len(_house_status.get(scheme, [])),
        )


def _load_house_status(scheme: str, hs: dict) -> None:
    """Build the house_status phrase table for one scheme from its resolver YAML.
    Each stored stage contributes its canonical label plus every alias, reduced
    to a token list; overloaded / rollup aliases are filtered out (see the
    _HOUSE_STATUS_* constants). `derived_groups.not_started` becomes a
    multi-member group; the in-progress group is skipped (it has a boolean flag)."""
    values: list[dict] = []
    for v in hs.get("values", []) or []:
        canon = v.get("canonical")
        if not canon or canon not in _HOUSE_STATUS_RESOLVABLE:
            continue
        for phrase in [canon, *(v.get("aliases", []) or [])]:
            toks = _hs_tokens(phrase)
            if not toks:
                continue
            if " ".join(toks) in _HOUSE_STATUS_ALIAS_DENY:
                continue
            if len(toks) == 1 and toks[0] not in _HOUSE_STATUS_SOLO_OK:
                continue
            values.append({"canonical": canon, "tokens": toks,
                           "stage_order": v.get("stage_order", 99)})
    _house_status[scheme] = values

    groups: list[dict] = []
    for gname, g in (hs.get("derived_groups", {}) or {}).items():
        if gname != "not_started":  # in_progress -> use is_in_progress, not a string list
            continue
        members = [m for m in (g.get("members", []) or []) if m]
        if not members:
            continue
        for phrase in g.get("aliases", []) or []:
            toks = _hs_tokens(phrase)
            if len(toks) >= 2:
                groups.append({"tokens": toks, "members": members})
    _house_status_groups[scheme] = groups


def resolve_house_status(question: str, scheme: str = "PMAY-G") -> "Resolved | None":
    """Scan a whole question for PMAY construction-stage references and resolve
    them to the exact stored `status_name` label(s). Deterministic closed-set
    match against the SME alias catalogue — no model call, and no dependence on
    the upstream LLM mention-extractor (which does not cover this dimension).

    Returns a Resolved whose `values` holds every matched stage (stage order,
    low -> high), or None when the question names no stage. "completed" and
    "in progress" are intentionally not matched here — those go through the
    is_completed / is_in_progress booleans per the schema rules."""
    vals = _house_status.get(scheme, [])
    groups = _house_status_groups.get(scheme, [])
    if not vals and not groups:
        return None

    q_tokens = set(_hs_tokens(question))
    if not q_tokens:
        return None

    def _phrase_in(tokens: list[str]) -> bool:
        present = sum(1 for t in tokens if t in q_tokens)
        if present == len(tokens):
            return True
        # one word may be missing, but only for a phrase specific enough (3+ tokens)
        return len(tokens) >= 3 and present == len(tokens) - 1

    hits: dict[str, int] = {}          # canonical -> stage_order
    matched: list[str] = []
    for entry in vals:
        if _phrase_in(entry["tokens"]):
            hits[entry["canonical"]] = min(entry["stage_order"],
                                           hits.get(entry["canonical"], 99))
            matched.append(entry["canonical"])
    for g in groups:
        if _phrase_in(g["tokens"]):
            for m in g["members"]:
                hits.setdefault(m, 99)
            matched.append(" ".join(g["tokens"]).title())

    if not hits:
        return None

    ordered = [c for c, _o in sorted(hits.items(), key=lambda kv: (kv[1], kv[0]))]
    return Resolved(
        status="resolved", entity_type="house_status",
        user_text="; ".join(dict.fromkeys(matched)),
        canonical=ordered[0] if len(ordered) == 1 else None,
        values=ordered, confidence=1.0,
        display=" and ".join(ordered),
    )


def _year_key(canonical: str) -> int:
    return int(canonical[:4])


def _display_form(canonical: str, dimension: str) -> str:
    """The name to use when talking to the user — the YAML's canonical display
    form for district/block (Title/Mixed case), a tidy "FY 2024-25" for year.
    Distinct from `_db_form`, which is the literal used in the WHERE clause."""
    if dimension == "year":
        digits = re.findall(r"\d{4}", canonical)
        if digits:
            start = int(digits[0])
            return f"FY {start}-{(start + 1) % 100:02d}"
        return canonical
    return canonical


def _db_form(canonical: str, dimension: str):
    """The value as actually stored in megh_db, not the YAML's display form.
    curated.dim_geography stores district/block UPPERCASE (confirmed live,
    2026-08-28) even though the YAML's canonical field is Title/Mixed Case for
    display. Using the YAML form directly in a WHERE clause silently matches
    zero rows — this bit the first live pipeline test."""
    if dimension == "year":
        return _year_key(canonical)
    if dimension in ("district", "block"):
        return canonical.upper()
    return canonical


def resolve_dimension(text: str, scheme: str, dimension: str) -> Resolved:
    """District/block/year — fully in-memory, stages 1/2/3/6."""
    values = _catalog.get(scheme, {}).get(dimension, [])
    if not values:
        return Resolved("not_found", dimension, text, message=f"no {dimension} catalogue for {scheme}")

    folded_input = fold(text)
    squashed_input = _squash(folded_input)

    # Stage 1 + 2: exact match on canonical or an alias.
    for v in values:
        candidates = [v["canonical"]] + v.get("aliases", [])
        if folded_input in (fold(c) for c in candidates):
            canon = _db_form(v["canonical"], dimension)
            return Resolved("resolved", dimension, text, canonical=canon, confidence=1.0,
                            display=_display_form(v["canonical"], dimension))

    # Stage 3: squash (strip all punctuation/spacing) match.
    for v in values:
        candidates = [v["canonical"]] + v.get("aliases", [])
        if squashed_input in (_squash(fold(c)) for c in candidates):
            canon = _db_form(v["canonical"], dimension)
            return Resolved("resolved", dimension, text, canonical=canon, confidence=0.95,
                            display=_display_form(v["canonical"], dimension))

    # Stage 6: RapidFuzz, blocked-pair aware.
    # process.extract on a dict returns (dict_value, score, dict_key) — pool maps
    # canonical (key) -> folded form (value), so unpack as (folded_match, score, canonical).
    pool = {v["canonical"]: fold(v["canonical"]) for v in values}
    matches = process.extract(folded_input, pool, scorer=fuzz.token_set_ratio, limit=2)
    if matches:
        (_folded, top_score, top_canon), *rest = matches
        blocked = _blocked.get(scheme, set())
        if (folded_input, pool[top_canon]) in blocked or (pool[top_canon], folded_input) in blocked:
            return Resolved("not_found", dimension, text, message=f"'{text}' does not match a known {dimension}")
        if top_score >= _FUZZY_ACCEPT:
            runner_up_score = rest[0][1] if rest else 0
            if top_score - runner_up_score >= _FUZZY_RUNNER_UP_GAP:
                v = next(v for v in values if v["canonical"] == top_canon)
                canon = _db_form(v["canonical"], dimension)
                return Resolved("resolved", dimension, text, canonical=canon, confidence=top_score / 100,
                                display=_display_form(v["canonical"], dimension))
            return Resolved(
                "ambiguous", dimension, text,
                candidates=[{"canonical": m[2]} for m in matches],
                message=f"'{text}' could mean more than one {dimension}",
            )

    return Resolved("not_found", dimension, text, message=f"'{text}' is not a known {dimension}")


def _scannable_forms(value: dict) -> list[str]:
    """Name forms specific enough to spot inside free-running question text.
    The canonical name always; the district acronym when it is 3+ letters
    ("EKH", "SWGH", "EWKH" — distinctive enough to stand alone as a token; the
    2-letter "RB" is left out as too collision-prone); other aliases only when
    they are multi-word and long enough not to fire on an incidental word
    ("E Garo" and bare short acronyms are excluded on purpose)."""
    forms = [value["canonical"]]
    acronym = str(value.get("acronym") or "").strip()
    if len(acronym) >= 3:
        forms.append(acronym)
    forms += [a for a in value.get("aliases", []) if " " in a and len(a) >= 7]
    return forms


_HQ_ALIAS_RE = re.compile(r"^(.+?)\s+district$", re.IGNORECASE)


def _match_forms(value: dict) -> list[str]:
    """Every string that names this catalogue value on an exact / alias /
    acronym basis (not fuzzy)."""
    forms = [value["canonical"], *(value.get("aliases", []) or [])]
    acronym = value.get("acronym")
    if acronym:
        forms.append(str(acronym))
    return forms


def lookup_geo_term(text: str) -> "dict | None":
    """Reverse-lookup a bare place term the user asked to have spelled out
    ("what is EKH", "MYLLIEM full form"). District catalogue first (acronyms
    live there), then blocks. Deterministic only — exact / alias / squash /
    acronym; NO fuzzy, because a definitional answer has to be certain.

    Returns {type, display, canonical, acronym, district, region, hq} or None.
    `canonical` is the DB literal (UPPER for district/block); `display` is the
    human-readable name."""
    folded_input = fold(text)
    squashed_input = _squash(folded_input)
    if not squashed_input:
        return None

    for dimension in ("district", "block"):
        for scheme in _catalog:
            for v in _catalog.get(scheme, {}).get(dimension, []):
                forms = _match_forms(v)
                if folded_input in {fold(f) for f in forms} or \
                        squashed_input in {_squash(fold(f)) for f in forms}:
                    hq = None
                    for a in v.get("aliases", []) or []:
                        m = _HQ_ALIAS_RE.match(str(a).strip())
                        if m and fold(m.group(1)) != fold(v["canonical"]):
                            hq = m.group(1).strip()
                            break
                    acronym = str(v.get("acronym") or "").strip()
                    used_abbrev = bool(acronym) and (
                        squashed_input == _squash(fold(acronym))
                    )
                    # Block canonicals are stored ALL CAPS; Title-case for display.
                    display = (v["canonical"] if dimension == "district"
                               else str(v["canonical"]).title())
                    return {
                        "type": dimension,
                        "display": display,
                        "canonical": _db_form(v["canonical"], dimension),
                        "acronym": v.get("acronym"),
                        "used_abbrev": used_abbrev,
                        "district": v.get("district"),
                        "region": v.get("region"),
                        "hq": hq,
                    }
    return None


def scan_dimension(question: str, scheme: str, dimension: str) -> "Resolved | None":
    """Deterministic backstop for the LLM mention-extractor: find a known
    <dimension> name sitting as a whole phrase in the raw question. Only meant
    to run when the extractor returned nothing for this dimension — the model
    intermittently misses a plainly-named district ("how many villages are
    covered in West Garo Hills" -> {}), and a dropped filter then silently
    counts the whole state or, with a wrong-case literal, zero.

    District has zero name collisions (mgnrega_entity_resolver.yaml: collisions
    none), so a hit is safe to trust. Overlapping names ("West Garo Hills" is a
    substring of "South West Garo Hills") resolve to the LONGEST match. Blocks
    are deliberately not scanned — 26 of 56 block names are also assembly
    constituencies or villages."""
    if dimension != "district":
        return None
    values = _catalog.get(scheme, {}).get(dimension, [])
    if not values:
        return None
    padded = f" {fold(question)} "
    best: tuple[int, str] | None = None
    for v in values:
        for form in _scannable_forms(v):
            ff = fold(form)
            if re.search(rf"(?<![A-Z0-9]){re.escape(ff)}(?![A-Z0-9])", padded):
                if best is None or len(ff) > best[0]:
                    best = (len(ff), v["canonical"])
    if best is None:
        return None
    return Resolved("resolved", dimension, question,
                    canonical=_db_form(best[1], dimension), confidence=0.9,
                    display=_display_form(best[1], dimension))


async def resolve_village(text: str, district: str | None = None, block: str | None = None) -> Resolved:
    """Village — live DB query against curated.dim_geography / dim_geography_alias.
    Resolves to village_code, per the YAML's hard rule (name alone is never a key)."""
    folded = text.strip()
    scope_sql, scope_params = "", []
    if district:
        scope_sql += " AND UPPER(g.lgd_district) = UPPER($%d)" % (len(scope_params) + 2)
        scope_params.append(district)
    if block:
        scope_sql += " AND UPPER(g.lgd_block) = UPPER($%d)" % (len(scope_params) + 2)
        scope_params.append(block)

    # Exact match first (case-insensitive) against the canonical name and every alias.
    sql = f"""
        SELECT DISTINCT g.village_code, g.lgd_village_name, g.lgd_district, g.lgd_block
        FROM curated.dim_geography g
        LEFT JOIN curated.dim_geography_alias a USING (geography_key)
        WHERE (UPPER(g.lgd_village_name) = UPPER($1) OR UPPER(a.source_village_name) = UPPER($1))
        {scope_sql}
        LIMIT 10
    """
    rows = await fetch_rows(sql, [folded, *scope_params])

    if not rows:
        # Trigram fuzzy fallback (uses the GIN trigram index already on dim_geography).
        sql = f"""
            SELECT DISTINCT g.village_code, g.lgd_village_name, g.lgd_district, g.lgd_block,
                   similarity(g.lgd_village_name, $1) AS score
            FROM curated.dim_geography g
            WHERE similarity(g.lgd_village_name, $1) > 0.4
            {scope_sql}
            ORDER BY score DESC
            LIMIT 5
        """
        rows = await fetch_rows(sql, [folded, *scope_params])
        if not rows:
            return Resolved("not_found", "village", text, message=f"'{text}' is not a known village")

    codes = {r["village_code"] for r in rows}
    if len(codes) == 1:
        r = rows[0]
        return Resolved(
            "resolved", "village", text, canonical=r["village_code"],
            confidence=0.9, message=f"{r['lgd_village_name']} ({r['lgd_district']})",
            display=str(r["lgd_village_name"]).title(),
        )
    return Resolved(
        "ambiguous", "village", text,
        candidates=[{"village_code": r["village_code"], "name": r["lgd_village_name"],
                     "district": r["lgd_district"], "block": r["lgd_block"]} for r in rows],
        message=f"“{text}” corresponds to more than one village. In which district or block is it located?",
    )
