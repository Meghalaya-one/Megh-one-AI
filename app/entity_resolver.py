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
    "Focus Plus": _DATA_PART / "focus_plus" / "focusplus_entity_resolver.yaml",
    "CM Elevate": _DATA_PART / "cm_elevate" / "cmelevate_entity_resolver.yaml",
}

# scheme -> dimension ("district" | "block" | "year") -> list of value dicts
_catalog: dict[str, dict[str, list[dict]]] = {}
# scheme -> [{canonical, districts, aliases}] — the hill-range groupings a user names
# instead of a district ("Garo Hills" = 5 districts). Loaded from the resolver YAML's
# region_groupings.groups; single-district groups (Ri Bhoi) are skipped since they
# resolve straight to the district with nothing to ask.
_regions: dict[str, list[dict]] = {}
# scheme -> set of (folded_a, folded_b) pairs fuzzy must never resolve across
_blocked: dict[str, set[tuple[str, str]]] = {}
# scheme -> [{name, schemes}] — CM Elevate's scheme-FAMILY groupings ("vehicles" =
# 4 sub-schemes, "tourism" = 2, "PRIME" = 3, ...), loaded from the resolver YAML's
# scheme_groupings.groups. Used only to catch a SINGULAR reference to one of these
# families ("the vehicle scheme") that resolve_cm_scheme couldn't pin to one real
# sub-scheme — see resolve_cm_scheme_group_ambiguity. The plural/group-breakdown
# phrasing ("vehicle schemes", "compare the vehicle schemes") is a different,
# already-answerable question (cmelevate_few_shot.yaml has worked examples) and
# is left entirely to the SQL generator, same as before this was added.
_scheme_groups: dict[str, list[dict]] = {}
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
            _regions[scheme] = []
            _scheme_groups[scheme] = []
            continue

        dims = data.get("dimensions", {})
        _catalog[scheme] = {}
        for dim_name in ("district", "block", "year", "assembly_constituency", "tranche_label", "cm_scheme"):
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

        regs: list[dict] = []
        for canon, g in (data.get("region_groupings", {}) or {}).get("groups", {}).items():
            dists = [str(d).strip() for d in (g.get("districts") or []) if str(d).strip()]
            if len(dists) < 2:            # Ri Bhoi etc. — a single district, nothing to ask
                continue
            regs.append({
                "canonical": str(canon).strip(),
                "districts": dists,
                "aliases": [str(a).strip() for a in (g.get("aliases") or []) if str(a).strip()],
            })
        _regions[scheme] = regs

        groups: list[dict] = []
        for name, g in (data.get("scheme_groupings", {}) or {}).get("groups", {}).items():
            member_schemes = [str(s).strip() for s in (g.get("schemes") or []) if str(s).strip()]
            if len(member_schemes) < 2:   # nothing to disambiguate
                continue
            groups.append({
                "name": str(name).strip(),
                "schemes": member_schemes,
                "aliases": [str(a).strip() for a in (g.get("aliases") or []) if str(a).strip()],
            })
        _scheme_groups[scheme] = groups

        logger.info(
            "entity_resolver: %s loaded — %s, %d blocked pairs, %d house_status phrases, "
            "%d regions, %d scheme groups",
            scheme, {k: len(v) for k, v in _catalog[scheme].items()}, len(pairs),
            len(_house_status.get(scheme, [])), len(regs), len(groups),
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


# The stored tranche_label is spelled "Tranch", not "Tranche", and carries a
# trailing month word ("Tranch 2 - August") — a user typing the ordinary
# spelling ("tranche 2") or dropping the space ("tranche2") produces a string
# that matches neither the canonical value nor its close aliases by exact or
# squashed containment, so the generated SQL's WHERE clause silently matches
# zero rows (a null total, not an error). Cheap guard first (most questions
# don't mention a tranche at all; "tranc" rather than "tranch" so the common
# mishearing "trance" still gets through), then exact/squash containment for
# the common cases, then a RapidFuzz partial-ratio pass so a genuine
# misspelling ("tranch2", "3rd trance") still resolves instead of falling
# through to the SQL generator's own guess.
_TRANCHE_GUARD_RE = re.compile(r"tranc", re.IGNORECASE)

# The fuzzy stage below must NOT fire on a bare "tranche" mention with no
# number ("break it down by tranche", "how many tranches") — partial-ratio
# naturally scores that high against every alias (the bare word is a prefix
# of all of them), which would silently pin a "which tranche" question to
# whichever canonical happens to sort first. Require an actual number/ordinal
# in the question before trusting the fuzzy match.
#
# CONFIRMED BUG (2026-09-11, from production query_audit.jsonl): the number
# had to be ANYWHERE in the question, not next to "tranche" — so "What are
# the top 3 districts ... across all financial years and tranches?" (a
# follow-up rewrite that pulled "tranches" in from a PRIOR turn's "all
# tranches" wording, with no tranche of its own in mind at all) matched this
# guard purely off the unrelated "top 3", then the fuzzy stage below scored
# "2nd tranche" at 90.9 against the combined "...top 3... tranches" text —
# just over the 90-point accept bar — and silently pinned tranche_label to
# "Tranch 2 - August". That value then flowed into both the SQL generator's
# WHERE-clause hint AND the response composer's "Entity names" block, so the
# composer reported "within the Tranch 2 - August data" for a query that was
# never meant to be tranche-scoped at all (see
# focusplus-tranche-context-not-carried-bug in project memory).
#
# Fixed by requiring the number/ordinal to sit immediately next to "tranch"
# itself (either side, "tranche 2" / "2nd tranche" / "trance 3"), not merely
# present somewhere in the sentence.
_TRANCHE_NUMBER_RE = re.compile(
    r"\btranc\w*\s+(?:no\.?|number|#)?\s*"
    r"(?:[1-4]|one|two|three|four|first|second|third|fourth|1st|2nd|3rd|4th)\b"
    r"|\b(?:[1-4]|one|two|three|four|first|second|third|fourth|1st|2nd|3rd|4th)"
    r"\s+tranc\w*",
    re.IGNORECASE,
)


def tranche_labels(scheme: str = "Focus Plus") -> list[str]:
    """Every tranche_label canonical value for `scheme`, in catalogue order
    (empty for a scheme with no tranche_label dimension, e.g. all but Focus
    Plus). Used to build the "which tranche?" clarification chips."""
    return [v["canonical"] for v in _catalog.get(scheme, {}).get("tranche_label", [])]


def resolve_tranche_label(question: str, scheme: str = "Focus Plus") -> "Resolved | None":
    """Focus Plus tranche_label ("Tranch 1" .. "Tranch 4 - Feb-March") — a closed
    set of 4 the LLM mention-extractor doesn't cover (see resolve_house_status).
    Scans the whole question and returns every stored label it names (not just
    the first — "compare tranche 1 and tranche 2" must resolve to both, the
    same multi-value contract resolve_house_status uses), fuzzy-matched so a
    misspelling or missing space still resolves."""
    values = _catalog.get(scheme, {}).get("tranche_label", [])
    if not values or not _TRANCHE_GUARD_RE.search(question):
        return None

    folded_q = fold(question)
    squashed_q = _squash(folded_q)

    # Stage 1/2/3: exact canonical/alias phrase present verbatim in the
    # question, falling back to squashed containment ("tranch2" / "Tranch-2")
    # for any catalog value the exact stage didn't already catch. Checked
    # INDEPENDENTLY per catalog value and merged — never stop at the first
    # stage that finds anything for SOME value, because a two-tranche
    # comparison can easily have one tranche match at the exact stage and the
    # other only at the squash stage: "Tranch 1"'s canonical has no month
    # suffix and matches the bare question text exactly, but "Tranch 2"'s
    # canonical is "Tranch 2 - August" and only matches via its squashed
    # "tranch2" alias. The old code found "Tranch 1" via exact match and
    # returned immediately, silently dropping "Tranch 2" from the result
    # entirely (confirmed live 2026-09-12: "compare Tranch 1 and Tranch 2"
    # resolved to Tranch 1 ONLY — the missing second entity then sent the
    # semantic verifier into a repair loop over SQL that was actually correct,
    # because it looked like "Tranch 2 - August" in the SQL had no matching
    # resolved entity to justify it).
    hits: list[str] = []
    hit_confidence = 1.0
    for v in values:
        forms = [v["canonical"]] + v.get("aliases", [])
        if any(fold(c) in folded_q for c in forms):
            hits.append(v["canonical"])
        elif any(_squash(fold(c)) and _squash(fold(c)) in squashed_q for c in forms):
            hits.append(v["canonical"])
            hit_confidence = min(hit_confidence, 0.95)
    if hits:
        return Resolved("resolved", "tranche_label", question,
                        canonical=hits[0] if len(hits) == 1 else None,
                        confidence=hit_confidence, values=hits,
                        display=" and ".join(hits))

    if not _TRANCHE_NUMBER_RE.search(question):
        return None

    # Stage 6: RapidFuzz partial-ratio over the whole question — catches a
    # genuine misspelling of both the word and the number ("3rd trance").
    # Single-value only: a fuzzy pass over a multi-tranche comparison risks
    # both mentions converging on the same nearest label.
    best_canon, best_score = None, 0.0
    for v in values:
        for c in [v["canonical"]] + v.get("aliases", []):
            score = fuzz.partial_ratio(fold(c), folded_q)
            if score > best_score:
                best_score, best_canon = score, v["canonical"]
    if best_canon and best_score >= _FUZZY_ACCEPT:
        return Resolved("resolved", "tranche_label", question,
                        canonical=best_canon, confidence=best_score / 100,
                        values=[best_canon], display=best_canon)

    return None


def resolve_cm_scheme(question: str, scheme: str = "CM Elevate") -> "Resolved | None":
    """CM Elevate's 15 sub-schemes (scheme_name / cm_scheme_key) — a closed set
    the LLM mention-extractor doesn't cover (see resolve_house_status,
    resolve_tranche_label). cmelevate_entity_resolver.yaml flags this
    dimension "resolve this first. No PMAY counterpart" — without a
    deterministic resolver, a sub-scheme name that's typo'd or loosely phrased
    ("diary development" for "Meghalaya Dairy Development Scheme") reaches the
    SQL generator as raw text, which then has to guess a scheme_name literal;
    a near-miss doesn't match storage exactly and silently counts zero rows
    instead of erroring, and the response composer then reports the scheme as
    "not covered" — wrong, and hard to catch because the query runs clean.

    Scans the whole question and returns every stored scheme it names (not
    just the first — "compare Piggery and Poultry" must resolve to both, the
    same multi-value contract resolve_tranche_label uses); the fuzzy stage is
    single-value only, same reasoning as resolve_tranche_label."""
    values = _catalog.get(scheme, {}).get("cm_scheme", [])
    if not values:
        return None

    padded = f" {fold(question)} "
    squashed_q = _squash(fold(question))

    def _forms(v: dict) -> list[str]:
        return [v["canonical"], *(v.get("aliases", []) or [])]

    # Stage 1/2: exact canonical/alias phrase present as a whole word/phrase in
    # the question — word-boundary, not bare substring, because several
    # aliases are short common words ("milk", "cow", "villa") that would
    # otherwise collide with unrelated text (e.g. "villa" inside "village").
    exact_hits: list[str] = []
    for v in values:
        for form in _forms(v):
            ff = fold(form)
            if ff and re.search(rf"(?<![A-Z0-9]){re.escape(ff)}(?![A-Z0-9])", padded):
                exact_hits.append(v["canonical"])
                break
    exact_hits = list(dict.fromkeys(exact_hits))
    if exact_hits:
        return Resolved("resolved", "cm_scheme", question,
                        canonical=exact_hits[0] if len(exact_hits) == 1 else None,
                        confidence=1.0, values=exact_hits,
                        display=" and ".join(exact_hits))

    # Stage 3: squashed containment — "dairydevelopment", "prime-seed". Only
    # MULTI-WORD forms are eligible: squashing drops the spaces that stage
    # 1/2's word-boundary check relies on, so a short single-word alias
    # ("villa", "cow") would otherwise match as a bare substring of an
    # unrelated squashed word ("villa" inside "villages"). A single-word
    # alias is already covered safely by stage 1/2 above.
    squash_hits: list[str] = []
    for v in values:
        for form in _forms(v):
            if " " not in form.strip():
                continue
            sf = _squash(fold(form))
            if sf and len(sf) >= 5 and sf in squashed_q:
                squash_hits.append(v["canonical"])
                break
    squash_hits = list(dict.fromkeys(squash_hits))
    if squash_hits:
        return Resolved("resolved", "cm_scheme", question,
                        canonical=squash_hits[0] if len(squash_hits) == 1 else None,
                        confidence=0.95, values=squash_hits,
                        display=" and ".join(squash_hits))

    # Stage 6: RapidFuzz partial-ratio over the whole question — catches a
    # genuine misspelling ("diary development" for "Dairy Development").
    # Only phrases of 6+ folded characters are eligible, so a short alias
    # can't fuzzy-match noise in an unrelated question. Single-value only: a
    # fuzzy pass over a multi-scheme comparison risks both mentions
    # converging on the same nearest label. The accept bar plus the
    # runner-up gap (same pair used everywhere else in this module) is what
    # keeps two genuinely confusable schemes (e.g. Dairy vs Piggery,
    # blocked_matches' own worked example) from resolving on a near-tie —
    # not a separate blocklist check, which would also veto a clear winner
    # that merely happens to have one of these schemes as its runner-up.
    scores: dict[str, float] = {}
    for v in values:
        best_for_v = 0.0
        for form in _forms(v):
            ff = fold(form)
            if len(ff) < 6:
                continue
            s = fuzz.partial_ratio(ff, padded)
            if s > best_for_v:
                best_for_v = s
        if best_for_v:
            scores[v["canonical"]] = best_for_v
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_canon, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score >= _FUZZY_ACCEPT and best_score - runner_up_score >= _FUZZY_RUNNER_UP_GAP:
        return Resolved("resolved", "cm_scheme", question,
                        canonical=best_canon, confidence=best_score / 100,
                        values=[best_canon], display=best_canon)

    return None


# Singular reference to a CM Elevate scheme-FAMILY word ("the vehicle scheme",
# "a PRIME scheme") — cmelevate_entity_resolver.yaml's overloaded_terms section
# documents these as "default: ask" (e.g. "vehicle": four schemes are not
# interchangeable; "tourism": 403 applications against 3, picking wrong is not a
# rounding error) with a worked example expecting sql: null and a candidate list.
# That documentation was never wired to any code — resolve_cm_scheme alone returns
# None for these (no single exact/fuzzy winner), and nothing upstream asked the
# question; the SQL generator was then left to guess a scheme_name literal that
# doesn't exist (confirmed live 2026-09-11: "the vehicle scheme" generated
# `scheme_name = 'Meghalaya Vehicle Scheme'`, a name that isn't one of the real
# 15, and silently returned 0 rows dressed up as a refusal).
# Deliberately keyed on the SINGULAR "scheme" (not "schemes") — the plural asks a
# different, already-answerable group-breakdown question (cmelevate_few_shot.yaml
# has worked IN-list examples for "vehicle schemes"/"livestock schemes") and must
# not be redirected into a clarification pause.
_CM_GROUP_SINGULAR_CUE: dict[str, re.Pattern] = {
    "vehicles": re.compile(r"\bvehicle scheme(?!s)\b", re.IGNORECASE),
    "tourism": re.compile(r"\btourism scheme(?!s)\b", re.IGNORECASE),
    "PRIME": re.compile(r"\bprime scheme(?!s)\b", re.IGNORECASE),
    "livestock": re.compile(r"\blivestock scheme(?!s)\b", re.IGNORECASE),
    "enterprise": re.compile(r"\benterprise scheme(?!s)\b", re.IGNORECASE),
}


def resolve_cm_scheme_group_ambiguity(question: str, scheme: str = "CM Elevate") -> "dict | None":
    """{'group': name, 'schemes': [...]} when the question names a scheme-family
    word in the singular that covers 2+ real CM Elevate sub-schemes, or None.
    Callers should only invoke this after resolve_cm_scheme itself returned
    nothing — a confident single/multi resolution from the real alias catalogue
    always wins over this coarser family-word heuristic."""
    for g in _scheme_groups.get(scheme, []):
        cue = _CM_GROUP_SINGULAR_CUE.get(g["name"])
        if cue and cue.search(question or ""):
            return {"group": g["name"], "schemes": g["schemes"]}
    return None


def resolve_cm_scheme_group(question: str, scheme: str = "CM Elevate") -> "dict | None":
    """{'group': name, 'schemes': [...]} when the question names one of these
    scheme-family groups by its own registered PLURAL/collective alias
    ("vehicle schemes", "livestock", "PRIME family", "transport schemes", ...)
    — the group-BREAKDOWN reading cmelevate_few_shot.yaml has worked IN-list
    examples for, as opposed to resolve_cm_scheme_group_ambiguity's singular
    "which one?" reading. Exact word-boundary match, same discipline as
    resolve_cm_scheme's own alias stage (stage 1/2)."""
    padded = f" {fold(question)} "
    for g in _scheme_groups.get(scheme, []):
        for alias in g.get("aliases", []):
            fa = fold(alias)
            if fa and re.search(rf"(?<![A-Z0-9]){re.escape(fa)}(?![A-Z0-9])", padded):
                return {"group": g["name"], "schemes": g["schemes"]}
    return None


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


# Words next to a region name that mean "give me the whole region", i.e. expand it to
# every district rather than pausing to ask which one.
_REGION_ALL_CUE = re.compile(
    r"\ball\b|\bwhole\b|\bentire\b|\bcombined\b|\beach\b|\bevery\b|\bacross\b|"
    r"\bfull\b|\btotal\b|\boverall\b|\bput together\b",
    re.IGNORECASE,
)


def detect_region(question: str, scheme: str) -> "dict | None":
    """A hill-range name the user typed instead of a district — "Garo Hills",
    "Khasi region", "GH". Returns {canonical, districts, aliases, expand} when the
    question names a multi-district region AND names no specific district, else None.

    `expand` is True when the phrasing already says "all of <region>" / "every district
    in <region>" — the caller should then filter on every district in the group and say
    so, rather than asking which one. When False the caller should raise a one-tap
    "which district?" clarification."""
    regs = _regions.get(scheme) or next((v for v in _regions.values() if v), [])
    if not regs:
        return None
    padded = f" {fold(question)} "

    # A full district name present anywhere ⇒ the district wins; do not treat the
    # embedded range word ("Garo Hills" inside "West Garo Hills") as a region.
    for v in _catalog.get(scheme, {}).get("district", []):
        ff = fold(v["canonical"])
        if re.search(rf"(?<![A-Z0-9]){re.escape(ff)}(?![A-Z0-9])", padded):
            return None

    best: tuple[str, dict] | None = None
    for r in regs:
        for form in [r["canonical"], *r.get("aliases", [])]:
            ff = fold(form)
            if len(ff) < 3:                       # skip bare "GH" / "KH" — too collision-prone
                continue
            if re.search(rf"(?<![A-Z0-9]){re.escape(ff)}(?![A-Z0-9])", padded):
                if best is None or len(ff) > len(best[0]):
                    best = (ff, r)
    if best is None:
        return None

    ff, r = best
    m = re.search(re.escape(ff), padded)
    around = padded[max(0, m.start() - 26): m.end() + 10] if m else padded
    return {
        "canonical": r["canonical"],
        "districts": list(r["districts"]),
        "aliases": list(r.get("aliases", [])),
        "expand": bool(_REGION_ALL_CUE.search(around)),
    }


def all_districts(scheme: str) -> list[str]:
    """Every district `scheme`'s resolver catalog knows, canonical Title Case,
    in the YAML's declared order — the same names `detect_region` matches
    against. Used to build one-tap district chips; empty if the scheme has no
    resolver file loaded (`load_all` was not run) or no district dimension."""
    return [v["canonical"] for v in _catalog.get(scheme, {}).get("district", [])]


_ACTIVITY_VIEWS = (
    "curated.v_employment", "curated.v_expenditure", "curated.v_pmay",
    "curated.v_focus_plus", "curated.v_cm_elevate",
)


async def _activity_counts(codes: list[int]) -> dict[int, int]:
    """Row count per village_code across every scheme's data view. Used only to
    break ties between duplicate dim_geography rows (same name, block AND
    district, different village_code) that a user has no way to tell apart
    through the chat UI — never surfaced to the user directly."""
    if not codes:
        return {}
    union = " UNION ALL ".join(
        f"SELECT village_code FROM {t} WHERE village_code = ANY($1)" for t in _ACTIVITY_VIEWS
    )
    rows = await fetch_rows(f"SELECT village_code, COUNT(*) AS n FROM ({union}) x GROUP BY village_code",
                             [codes])
    return {r["village_code"]: r["n"] for r in rows}


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
        # Both sides are UPPER()'d to match the exact-match stage's normalization
        # above — pg_trgm's similarity() is case-sensitive, so a misspelled village
        # typed in the "wrong" case (e.g. lowercase) would otherwise score lower
        # than the same typo in matching case and fall below the 0.4 cutoff.
        sql = f"""
            SELECT DISTINCT g.village_code, g.lgd_village_name, g.lgd_district, g.lgd_block,
                   similarity(UPPER(g.lgd_village_name), UPPER($1)) AS score
            FROM curated.dim_geography g
            WHERE similarity(UPPER(g.lgd_village_name), UPPER($1)) > 0.4
            {scope_sql}
            ORDER BY score DESC
            LIMIT 5
        """
        rows = await fetch_rows(sql, [folded, *scope_params])
        if not rows:
            return Resolved("not_found", "village", text, message=f"'{text}' is not a known village")

    # NOTE: two rows can share the same (name, district) — e.g. two villages
    # both named "Adugre" in SOUTH WEST GARO HILLS, one in BETASING block with
    # real expenditure data and one in RERAPARA block with none — while being
    # genuinely different villages with their own data. Collapsing them by
    # (name, district) alone would silently pick one village_code's data over
    # the other's and misreport the total, so they are deliberately NOT merged
    # here. What DOES need fixing is the ambiguity message below: it must show
    # the block too, or two distinct villages read as if they were duplicates.
    codes = {r["village_code"] for r in rows}
    if len(codes) == 1:
        r = rows[0]
        return Resolved(
            "resolved", "village", text, canonical=r["village_code"],
            confidence=0.9, message=f"{r['lgd_village_name']} ({r['lgd_district']})",
            display=str(r["lgd_village_name"]).title(),
        )

    # Duplicate dim_geography rows for the same real-world village are common
    # (57 (name, block, district) groups statewide as of 2026-09) — almost
    # always an ingestion artifact where one row never got any scheme data
    # attached (e.g. "Asimgre", DALU block, WEST GARO HILLS: village_code
    # 274383 has zero rows anywhere, 274259 has MGNREGA + PMAY-G data). When
    # candidates share name AND block AND district, the ambiguity chips built
    # below are IDENTICAL text — clicking either just re-asks the same
    # question forever, since there is nothing left for the user to say that
    # would tell them apart (2026-09-09 bug report: this looped). A district
    # or block DIFFERENCE (the Adugre case above) is still left for the user
    # to answer, since that's something they can actually specify.
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((fold(r["lgd_village_name"]), r["lgd_block"], r["lgd_district"]), []).append(r)
    if len(groups) < len(rows):
        counts = await _activity_counts([r["village_code"] for r in rows])
        rows = [
            max(group, key=lambda r: (counts.get(r["village_code"], 0), -r["village_code"]))
            for group in groups.values()
        ]
        codes = {r["village_code"] for r in rows}
        if len(codes) == 1:
            r = rows[0]
            logger.info(
                "resolve_village: collapsed same-name/block/district duplicate dim_geography "
                "rows for %r to village_code=%s by data volume", text, r["village_code"])
            return Resolved(
                "resolved", "village", text, canonical=r["village_code"],
                confidence=0.75, message=f"{r['lgd_village_name']} ({r['lgd_district']})",
                display=str(r["lgd_village_name"]).title(),
            )

    return Resolved(
        "ambiguous", "village", text,
        candidates=[{"village_code": r["village_code"], "name": r["lgd_village_name"],
                     "district": r["lgd_district"], "block": r["lgd_block"]} for r in rows],
        message=f"“{text}” corresponds to more than one village. In which district or block is it located?",
    )
