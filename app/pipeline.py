"""
The whole NL -> SQL -> answer flow as one plain sequential function. No graph
framework: each step is an ordinary `await`, branches are ordinary `if`. This
is deliberately simpler than the LangGraph design in the GrantThornton
proposal — appropriate at 2 schemes and 20-40 concurrent users; revisit if
either grows a lot.
"""
import asyncio
import json
import logging
import numbers
import re

import httpx

from app import auth, edge, followups, llm, premise_check, prompt_builder, rag
from app.config import settings
from app.db import UnsafeSQLError, run_readonly
from app.entity_resolver import (
    lookup_geo_term,
    resolve_dimension,
    resolve_house_status,
    resolve_village,
    scan_dimension,
)
from app.schema_context import SCHEME_CATALOG, available_metrics_text
from app.session_store import Session

logger = logging.getLogger(__name__)

# Words that make a question unambiguously a data (numbers) question — no model
# call needed to route it. Anything else with no strong signal is sent to the
# classifier, which decides DATA vs KNOWLEDGE.
_DATA_HINTS = re.compile(
    r"\b(how many|count|total|sum|average|avg|number of|top \d|per capita|"
    r"compare|comparison|trend|by district|by block|by village|by year|"
    r"person[\s-]?days?|expenditure|spend|spent|wage|wages|job cards?|"
    r"houses? (sanction|complet|released|pending)|sanctioned amount|amount released|"
    r"completion rate|utili[sz]ation rate|success rate|per ?cent|percentage|how much|"
    r"\bfy ?20\d\d|20\d\d-\d\d|crore|lakh|highest|lowest|most|least|"
    # correlation / cross-metric comparison phrasing — "do districts with high X
    # also have high Y", "is A related to B by district". These are answered by
    # querying and comparing the figures, not from the reference docs.
    r"relationship between|correlat\w*|associat\w*|linked to|linked with|"
    r"go together|hand in hand|track (?:each other|together)|move together|"
    r"districts? where|blocks? where|villages? where|"
    r"do (?:the )?districts? with|does (?:the )?district with|"
    r"also (?:high|low|higher|lower|lead|leads|greater|larger|smaller|more|less)|"
    r"versus)\b",
    re.IGNORECASE,
)
_KNOWLEDGE_HINTS = re.compile(
    r"\b(what is|what are|who is eligible|eligibility|how do i|how to apply|"
    r"documents? required|what documents|explain|define|meaning of|"
    r"components? of|features? of|objective|purpose of|when was .* launched|"
    r"difference between|guidelines?|rules? for)\b",
    re.IGNORECASE,
)


class OutOfScope(Exception):
    """Raised when the question is about a place the assistant doesn't cover —
    a district or block that isn't in Meghalaya. The pipeline turns this into
    the standard 'I'm Megh One AI — I only cover Meghalaya's MGNREGA / PMAY-G'
    reply instead of dropping the filter and reporting a meaningless 0."""


class ClarificationNeeded(Exception):
    """Raised when the pipeline cannot safely proceed without one more detail
    from the user. `question` is the thing to ask; `options` (optional) is a
    list of {"label", "question"} the UI renders as one-click replies — each
    `question` is a fully-formed standalone question that resumes the flow.
    `rule` (optional) is a short tag for why we paused (shown in the UI)."""

    def __init__(self, question: str, *, options: list[dict] | None = None,
                 rule: str | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []
        self.rule = rule


# ── Follow-up ("what about East Garo Hills?") rewriting ──────────────────────
# A cheap heuristic gates the one extra model call: only questions that read
# like a fragment referring back to the previous turn get rewritten.
_FOLLOWUP_LEAD = re.compile(
    r"^\s*(what about|how about|and |what of |and for |also |ok(ay)? and |"
    r"same for |what if |now |then )", re.IGNORECASE,
)
_FOLLOWUP_PRONOUN = re.compile(
    r"\b(it|its|that|those|these|them|they|there|this one|the same|same one)\b", re.IGNORECASE,
)
# Something that anchors the question on its own — if present, it's not a fragment.
_STANDALONE_ANCHOR = re.compile(
    r"\b(mgnrega|mnrega|nrega|pmay|awaas|person[\s-]?days?|expenditure|houses?|"
    r"job cards?|wages?|sanction|eligib|documents?|scheme|how many|total|list|"
    r"what is|who is|explain)\b", re.IGNORECASE,
)


# A follow-up fragment ("and for 2024-25?", "how launched it?") only means
# something against a real scheme answer. Rewriting it against a greeting,
# off-topic reply, clarification pause or a denial produces a confident bogus
# query — e.g. "how launched it?" right after "what is elon musk?" was being
# turned into a data question. So the previous turn must be one of these.
_ANTECEDENT_ROUTES = ("data", "knowledge")
_CONTEXTLESS_REF = re.compile(r"\b(it|its|that|those|these|them|they|this one)\b", re.IGNORECASE)


def _mentions_scheme(question: str) -> bool:
    return any(p.search(question or "") for p in _SCHEME_NAME_PATTERN.values())


def looks_like_followup(question: str) -> bool:
    q = question.strip()
    if len(q.split()) > 12:
        return False
    if _FOLLOWUP_LEAD.search(q):
        return True
    if _FOLLOWUP_PRONOUN.search(q) and not _STANDALONE_ANCHOR.search(q):
        return True
    # A bare fragment ("in East Garo Hills", "by block") with no anchor of its own.
    return len(q.split()) <= 6 and not _STANDALONE_ANCHOR.search(q)


async def rewrite_followup(question: str, prev: "object") -> str:
    """Turn a fragment into a standalone question using the previous turn.
    Falls back to the original question on any failure — never raises."""
    if not settings.FOLLOWUP_REWRITE_ENABLED or prev is None:
        return question
    try:
        prompt = (
            "Rewrite the FOLLOW-UP as a complete, standalone question by reusing "
            "context from the PREVIOUS question. Keep the user's intent; change only "
            "what the follow-up changes (e.g. a different district, year, or metric). "
            "Return ONLY the rewritten question, nothing else.\n\n"
            f'PREVIOUS question: "{prev.question}"\n'
            f'PREVIOUS answer (for context): "{(prev.answer or "")[:300]}"\n'
            f'FOLLOW-UP: "{question}"\n\n'
            "Standalone question:"
        )
        out = await llm.call_classifier(prompt)
        out = out.strip().strip('"').splitlines()[0].strip()
        if 3 <= len(out) <= 300:
            logger.info("follow-up rewrite: %r -> %r", question, out)
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning("follow-up rewrite failed (%s) — using original", e)
    return question


# ── Guided-decoding schemas ──────────────────────────────────────────────────
# Passed to the classifier/SQL model so it can only emit tokens that fit the
# shape. The _extract_json / _extract_sql parsers below still run, so these are
# a reliability + speed win with no hard dependency on the gateway honouring
# them. Kept next to the prompts they constrain.
_SCHEMES_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "schemes": {
            "type": "array",
            "items": {"type": "string", "enum": list(SCHEME_CATALOG)},
            "minItems": 1,
        }
    },
    "required": ["schemes"],
}
# All optional strings — no null-union type (some xgrammar builds reject it) and
# no `required` (the model emits only the keys it actually finds). The parser in
# extract_entity_mentions already tolerates a partial or empty object.
_ENTITY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "district": {"type": "string"},
        "block": {"type": "string"},
        "village": {"type": "string"},
        "year": {"type": "string"},
    },
    "additionalProperties": False,
}
_INTENT_JSON_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["DATA", "KNOWLEDGE"]}},
    "required": ["intent"],
}
# Force the SQL completion to open on a bare SELECT/WITH (optionally after
# whitespace). Body is unconstrained — this enforces statement shape, not a SQL
# grammar, so it can't catch a wrong join, only a prose preamble or a ```fence.
_SQL_SHAPE_REGEX = r"\s*(SELECT|WITH|select|with)[\s\S]*"


def _extract_json(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _extract_sql(raw: str) -> str:
    # Strip a ```sql fence if the model added one despite being told not to.
    cleaned = re.sub(r"```(?:sql)?", "", raw).strip()
    cleaned = cleaned.rstrip(";").strip()
    # The generator sometimes copies a worked example that ends its outer query
    # with ';' and then appends its own trailing "LIMIT n" AFTER that ';'
    # ("... ) d;\nLIMIT 1"). That is one statement, not two — fold the stray
    # terminator away so it isn't rejected as "multiple statements". Only a bare
    # trailing LIMIT/OFFSET/FETCH tail is spliced back; anything else after a ';'
    # is left for the multi-statement guard to reject.
    m = re.search(
        r";\s*((?:LIMIT|OFFSET|FETCH)\b[\s\S]*)$", cleaned, re.IGNORECASE)
    if m:
        cleaned = (cleaned[: m.start()] + "\n" + m.group(1)).strip()
    return cleaned


_SCHEME_NAME_PATTERN = {
    "MGNREGA": re.compile(r"\bmgnrega\b|\bmnrega\b|\bnrega\b", re.IGNORECASE),
    "PMAY-G": re.compile(r"\bpmay[\s-]?g?\b|\bawa+s?\b", re.IGNORECASE),
}


# The user explicitly wants a cross-scheme answer — honour it, don't ask.
_EXPLICIT_BOTH = re.compile(
    r"\b(both schemes?|all schemes?|each scheme|per scheme|by scheme|across schemes?|"
    r"every scheme|either scheme|the two schemes|scheme[\s-]?wise)\b",
    re.IGNORECASE,
)
# Vocabulary that only makes sense for ONE scheme, so we can infer it without
# asking even when the scheme is not named. Kept deliberately narrow — a term
# that both schemes use (e.g. "beneficiaries", "paid", "pending", "amount")
# must NOT appear here, or an ambiguous question gets silently mis-routed.
_MGNREGA_ONLY_TERMS = re.compile(
    r"\b(person[\s-]?days?|job cards?|100[\s-]?days?|hundred days?|muster|"
    r"households? employed|persons? employed|unskilled wage|semi[\s-]?skilled|"
    r"labour budget|works? demanded|wage employment|employment guarantee)\b",
    re.IGNORECASE,
)
_PMAY_ONLY_TERMS = re.compile(
    r"\b(house|houses|housing|dwelling units?|pucca house|kutcha house|"
    r"sanctioned houses?|instal?ments?|tranche|geotag|"
    r"completion certificate|house status|awaas|awas)\b",
    re.IGNORECASE,
)


def _infer_scheme_from_terms(question: str) -> list[str] | None:
    """A single scheme implied by scheme-specific vocabulary, or None if the
    question could plausibly mean either."""
    m = bool(_MGNREGA_ONLY_TERMS.search(question))
    p = bool(_PMAY_ONLY_TERMS.search(question))
    if m and not p:
        return ["MGNREGA"]
    if p and not m:
        return ["PMAY-G"]
    return None


def _scheme_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    options = [
        {"label": "MGNREGA (rural employment)", "question": f"{stem} for MGNREGA"},
        {"label": "PMAY-G (rural housing)", "question": f"{stem} for PMAY-G"},
        {"label": "Compare both schemes", "question": f"{stem} across MGNREGA and PMAY-G"},
    ]
    return ClarificationNeeded(
        "Which scheme does your question concern — MGNREGA or PMAY-G? "
        "Please select one, or choose to have the two schemes compared.",
        options=options,
        rule="scheme-not-specified",
    )


def _needs_scheme_clarification(question: str) -> bool:
    """True when the question names no scheme, doesn't ask for a cross-scheme
    view outright, and uses no vocabulary that pins it to one scheme. In that
    case we ask rather than guess — a wrong scheme guess produces a confident
    but meaningless answer (e.g. 'PMAY has 0 paid beneficiaries')."""
    if not settings.SCHEME_CLARIFY_ENABLED:
        return False
    if any(p.search(question) for p in _SCHEME_NAME_PATTERN.values()):
        return False
    if _EXPLICIT_BOTH.search(question):
        return False
    if _infer_scheme_from_terms(question) is not None:
        return False
    return True


# ── "Top how many?" clarification ───────────────────────────────────────────
# A ranking question over a dimension ("top districts", "which blocks spent the
# most", "rank villages by person-days") that names no count. Silently defaulting
# to a fixed top-N is a guess the user can't see or correct; a one-tap follow-up
# ("Top 3 / 5 / 10 / all") is cheaper than returning the wrong list length.
_RANKING_CUE = re.compile(
    r"\b(top|bottom|highest|lowest|most|least|best|worst|leading|leader|leads?|"
    r"rank(?:ed|ing)?|largest|smallest|biggest|maximum|minimum)\b",
    re.IGNORECASE,
)
# A plural dimension noun — a ranking only needs a length when it returns rows to
# cut. "which district spent the most" (singular) already means the single top row.
_RANK_DIMENSION = re.compile(
    r"\b(districts|blocks|villages|panchayats|gram panchayats|gps|g\.?p\.?s)\b",
    re.IGNORECASE,
)
# An explicit count is already in the question — nothing to ask.
_EXPLICIT_COUNT = re.compile(
    r"\btop[\s-]*\d+\b"
    r"|\b(?:top|bottom|first|last)\s+(?:one|two|three|four|five|six|seven|eight|nine|ten)\b"
    r"|\b\d+\s+(?:highest|lowest|largest|biggest|smallest|top|best|worst|most|least)\b",
    re.IGNORECASE,
)
# The user wants the whole list, not a ranked head — also nothing to ask.
_WANTS_ALL = re.compile(
    r"\b(all|every|each|entire|complete|full)\b.{0,20}\b(districts?|blocks?|villages?|"
    r"panchayats?|list|row|rows)\b"
    r"|\blist\s+(?:of\s+)?all\b|\bfor\s+all\b|\bacross\s+all\b|\bno limit\b|\bevery row\b",
    re.IGNORECASE,
)
_TOPN_CHOICES = (3, 5, 10)


def _needs_topn_clarification(question: str) -> bool:
    """True when the question asks for a ranked list over a dimension but never
    says how long the list should be."""
    if not settings.TOPN_CLARIFY_ENABLED:
        return False
    q = question or ""
    if not (_RANKING_CUE.search(q) and _RANK_DIMENSION.search(q)):
        return False
    if _EXPLICIT_COUNT.search(q) or _WANTS_ALL.search(q):
        return False
    return True


def _inject_topn(question: str, n: int) -> str:
    """Rewrite the ranking question so it states the count `n`, so the resumed
    turn is a complete standalone question the SQL layer can LIMIT on."""
    q = question.strip().rstrip(" ?.")
    new_q, hit = re.subn(r"\btop\b(?![\s-]*\d)", f"top {n}", q, count=1, flags=re.IGNORECASE)
    if hit:
        return new_q + "?"
    # No literal "top" to splice into ("which blocks spent the most") — append it.
    return f"{q}, showing only the top {n}?"


def _topn_clarification(question: str) -> "ClarificationNeeded":
    q = question.strip().rstrip(" ?.")
    options = [{"label": f"Top {n}", "question": _inject_topn(question, n)} for n in _TOPN_CHOICES]
    options.append({"label": "All of them", "question": f"{q}, return every row with no limit?"})
    return ClarificationNeeded(
        "How many results should be returned — the top 3, the top 5, the top 10, "
        "or the complete list?",
        options=options,
        rule="ranking-count-not-specified",
    )


# ── "Which area / year?" clarification ──────────────────────────────────────
# An aggregate question ("how many houses sanctioned", "total MGNREGA
# expenditure", or a bare "show MGNREGA spend") that names no place and no year
# gets answered statewide across every year — a scope the user never asked for
# and can't see in the reply. One free-text follow-up ("West Garo Hills
# 2023-24") is cheaper than a number that silently means something else.
# Applies to MGNREGA and PMAY-G alike.
_AGGREGATE_CUE = re.compile(
    r"\b(how many|how much|number of|count of|no\.? of|total(?:\s+number)?|"
    r"sum of|what(?:'s| is| was) the total|average|avg|mean)\b",
    re.IGNORECASE,
)
# A bare metric noun with no quantity word in front of it — "show MGNREGA
# spend", "PMAY-G houses", "person-days". On its own the metric name still means
# "give me the total <metric>", so it needs a place and a year just as much as
# an explicit "how much" does. Kept to unambiguous scheme metrics — generic
# words ("amount", "money", "funds", "works") are left out so a membership or
# list question isn't dragged into a scope pause.
_BARE_METRIC_CUE = re.compile(
    r"\b(person[\s-]?days?|expenditure|spend(?:ing)?|spent|"
    r"wages?|wage bill|job cards?|muster rolls?|"
    r"houses?(?:\s+(?:sanctioned|completed|approved|released|pending))?|"
    r"dwelling units?|sanctioned amount|amount released|"
    r"instal?ments?|disbursements?|utili[sz]ation)\b",
    re.IGNORECASE,
)
# The question already fixes its own scope (a breakdown, a trend, a comparison
# across a dimension, or an explicit "all of Meghalaya") — nothing to ask.
_HAS_BREAKDOWN = re.compile(
    r"\bby (?:district|block|village|panchayat|gp|year|month)\b|"
    r"\b(?:per|each|every|for all|across all|all the) "
    r"(?:district|block|village|panchayat|year|month)s?\b|"
    r"\b(?:district|block|village|year)[\s-]?wise\b|"
    r"\bbreak[\s-]?down\b|\btrend\b|\byear[\s-]?on[\s-]?year\b|"
    r"\bover (?:the )?(?:last|past) \w+ years?\b|"
    r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|"
    r"\bcorrelat\w*|\brelationship between\b|\blinked (?:to|with)\b|"
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?)\s+"
    r"(?:with|where|that|having|which)\b|"
    r"\bacross (?:the )?(?:districts?|blocks?|villages?|panchayats?|state|years?)\b",
    re.IGNORECASE,
)
_EXPLICIT_STATEWIDE = re.compile(
    r"\b(?:in|for|across|over|of) (?:all of |the (?:whole|entire) )?meghalaya\b|"
    r"\bstate[\s-]?(?:wide|level|total)\b|\boverall\b|\bin total\b|\bgrand total\b|"
    r"\ball (?:the )?(?:years|districts|blocks|villages)\b|\bentire state\b|"
    r"\bevery year\b|\bsince inception\b|\ball[\s-]?time\b|\bto date\b|\bcumulative\b",
    re.IGNORECASE,
)
# A cross-scheme set / overlap question whose answer IS a geography list or its
# count — "how many common districts in both schemes", "which villages are
# covered by both", "districts with both MGNREGA and PMAY-G activity". Geography
# is the thing being counted, not a filter, so there is no area or year to ask
# for — it is inherently statewide across the whole data window. This must not
# swallow a plain cross-scheme metric question ("how much did both schemes
# spend"), so every branch is anchored to a geography noun.
_CROSS_SCHEME_SET_QUESTION = re.compile(
    r"\b(?:common|shared|overlapping|overlap(?:ping)?|mutual|convergen\w*|"
    r"distinct|unique)\s+(?:districts?|blocks?|villages?|panchayats?|gps?|areas?|"
    r"geograph\w+)\b|"
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?|areas?)\s+"
    r"(?:(?:are|is|were|was|that|which|have|having|has)\s+){0,2}"
    r"(?:common (?:to|across|between)|shared (?:by|between|across)|"
    r"in (?:both|all|either)|covered (?:by|under) both|"
    r"covered by (?:both )?(?:mgnrega|pmay|schemes)|"
    r"with both|have both|having both|present in both|"
    r"served by both|running both|under both|in common)\b|"
    r"\bhow many (?:districts?|blocks?|villages?|panchayats?|gps?)\b[^?]*"
    r"\b(?:both schemes?|both mgnrega and pmay|pmay and mgnrega|"
    r"in common|overlap)\b|"
    # "blocks (wise) common in both schemes", "which blocks are common in both",
    # "common blocks in both schemes" — a geography noun sitting next to both
    # "common"/"shared"/"overlap" and "both", in either order. Still anchored to
    # the geography noun so a bare metric question can't match.
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?|areas?)\b(?:[\s-]?wise)?"
    r"[^?]{0,30}\b(?:common|shared|overlap\w*|convergen\w*)\b[^?]{0,20}\bboth\b|"
    r"\b(?:common|shared|overlap\w*|convergen\w*)\b[^?]{0,20}"
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?|areas?)\b[^?]{0,20}\bboth\b",
    re.IGNORECASE,
)


def _needs_scope_clarification(question: str, resolved: dict) -> bool:
    """True when an aggregate question — an explicit "how many / total …" or a
    bare metric noun on its own ("show MGNREGA spend") — pins no geography and no
    year, neither in its text nor via a resolved entity, and doesn't ask for a
    breakdown or an explicit statewide total. Callers must have run
    `resolve_entities` first so `resolved` reflects any district/block/village/
    year actually named."""
    if not settings.SCOPE_CLARIFY_ENABLED:
        return False
    q = question or ""
    if not (_AGGREGATE_CUE.search(q) or _BARE_METRIC_CUE.search(q)):
        return False
    if _HAS_BREAKDOWN.search(q) or _EXPLICIT_STATEWIDE.search(q):
        return False
    if _CROSS_SCHEME_SET_QUESTION.search(q):
        return False
    if any(resolved.get(k) for k in ("district", "block", "village_code", "year_key")):
        return False
    return True


# A reply to the scope pause is normally a bare fragment ("Ri Bhoi, 2023-24").
# These shapes instead mean the user dropped the earlier question and asked a
# fresh one — don't fold them into the original.
_REPLY_IS_NEW_QUESTION = re.compile(
    r"\b(how many|how much|number of|count of|what(?:'s| is| are| was) the|"
    r"who (?:is|can)|how do i|how to apply|what documents?|which documents?|"
    r"explain|define|difference between|tell me about)\b",
    re.IGNORECASE,
)


def _reply_abandons_scope_pause(reply: str) -> bool:
    """True when the reply to a 'which area / year?' pause is itself a new,
    self-standing question rather than the scope fragment we asked for."""
    r = (reply or "").strip()
    if len(r.split()) > 12:
        return True
    return bool(_REPLY_IS_NEW_QUESTION.search(r) or _KNOWLEDGE_HINTS.search(r))


def _scope_clarification(question: str) -> "ClarificationNeeded":
    return ClarificationNeeded(
        "Which area and time period should the answer cover — a specific district, "
        "block, or village, and which financial year? Please reply with the "
        "location and the financial year (for example, \"West Garo Hills, "
        "2023-24\"), or reply \"all of Meghalaya, all years\" if the statewide "
        "total is required.",
        rule="scope-not-specified",
    )


# ── "Which financial year?" clarification ───────────────────────────────────
# The scope gate above only fires when a question pins NO geography at all. A
# question that already fixes its place — "compare districts for MGNREGA", "total
# PMAY-G houses in West Garo Hills" — sails past it and is then answered across
# EVERY financial year at once, a scope the user never chose and can't see in the
# reply. This gate catches exactly that: geography is settled, the year is not.
# One-tap year chips (the scheme's FYs + "all years combined") resume the flow.
#
# The financial years each scheme actually holds in megh_db — the SINGLE source
# of truth for every year chip we offer (the "which year?" pause AND the
# out-of-range guard). These are DEFAULTS only: refresh_scheme_years() overwrites
# them at startup with the real DISTINCT year_key set from the curated views, so
# the chips and the guard always match the live data even after a data reload.
# Values below were verified against curated.v_employment / v_expenditure
# (MGNREGA) and curated.v_pmay (PMAY-G) on 2026-08-30 — MGNREGA covers FY
# 2022-23..2025-26; PMAY-G covers FY 2017-18..2023-24 (NOT 2024-25 / 2025-26).
_SCHEME_DATA_YEARS: dict[str, list[str]] = {
    "MGNREGA": ["2022-23", "2023-24", "2024-25", "2025-26"],
    "PMAY-G":  ["2017-18", "2018-19", "2019-20", "2020-21", "2021-22", "2022-23", "2023-24"],
}


def _fy_short(year_key: int) -> str:
    """2022 -> '2022-23'."""
    return f"{year_key}-{(year_key + 1) % 100:02d}"


def _fy_start(short: str) -> int:
    """'2022-23' -> 2022."""
    return int(str(short)[:4])


async def refresh_scheme_years() -> None:
    """Load each scheme's real DISTINCT financial years from megh_db so the year
    chips and the out-of-range guard track the live data. Best-effort — on any
    failure the verified defaults in _SCHEME_DATA_YEARS stay in place."""
    from app.db import run_readonly
    sql_by_scheme = {
        "MGNREGA": (
            "SELECT DISTINCT year_key FROM curated.v_employment WHERE year_key IS NOT NULL "
            "UNION "
            "SELECT DISTINCT year_key FROM curated.v_expenditure WHERE year_key IS NOT NULL"
        ),
        "PMAY-G": "SELECT DISTINCT year_key FROM curated.v_pmay WHERE year_key IS NOT NULL",
    }
    for scheme, sql in sql_by_scheme.items():
        try:
            rows = await run_readonly(sql)
            yrs = sorted({int(r["year_key"]) for r in rows if r.get("year_key") is not None})
            if yrs:
                _SCHEME_DATA_YEARS[scheme] = [_fy_short(y) for y in yrs]
                logger.info("scheme years loaded: %s -> %s", scheme, _SCHEME_DATA_YEARS[scheme])
            else:
                logger.warning("scheme years: %s query returned no rows, keeping default %s",
                               scheme, _SCHEME_DATA_YEARS[scheme])
        except Exception as e:  # noqa: BLE001
            logger.warning("scheme years: %s query failed, keeping default %s (%s)",
                           scheme, _SCHEME_DATA_YEARS[scheme], e)

# The question already fixes its time scope — a specific year, an explicit
# "all years", or a request for a per-year series. Any of these => don't ask.
_ALL_YEARS_CUE = re.compile(
    r"\ball[\s-]?(?:the\s+)?(?:financial |fiscal |fy )?years?\b|"
    r"\bevery (?:financial |fiscal )?year\b|\beach year\b|"
    r"\bacross (?:all )?(?:the )?(?:financial |fiscal )?years?\b|"
    r"\ball[\s-]?time\b|\bsince inception\b|\bto date\b|\bcumulative\b|"
    r"\ball years combined\b|\boverall\b|\bin total\b|\bgrand total\b",
    re.IGNORECASE,
)
_TIME_SERIES_CUE = re.compile(
    r"\btrend\b|\bover time\b|\bover (?:the )?(?:last|past) \w+ (?:years?|fys?)\b|"
    r"\byear[\s-]?on[\s-]?year\b|\byear[\s-]?wise\b|\bby year\b|\bby financial year\b|"
    r"\bper year\b|\bannually\b|\bannual\b|\bhistory\b|\bhistorical\b|"
    r"\bgrowth\b|\bchange over\b|\beach (?:financial )?year\b",
    re.IGNORECASE,
)
# The question is actually about a metric or a per-dimension breakdown — the only
# shapes where "which year?" is a meaningful missing filter. A pure membership /
# rules question ("which villages have both schemes", "who is eligible") isn't.
_METRIC_OR_BREAKDOWN_CUE = re.compile(
    r"\b(how many|how much|number of|count of|no\.? of|total|sum of|average|avg|mean|"
    r"person[\s-]?days?|expenditure|spend(?:ing)?|spent|wages?|wage bill|job cards?|"
    r"muster rolls?|houses?|dwelling units?|sanctioned amount|amount released|"
    r"instal?ments?|disbursements?|utili[sz]ation|completion rate|success rate|"
    r"compare|comparison|versus|\bvs\.?\b|rank(?:ed|ing)?|top \d|highest|lowest|"
    r"most|least|by district|by block|by village|by panchayat|"
    r"district[\s-]?wise|block[\s-]?wise|village[\s-]?wise)\b",
    re.IGNORECASE,
)


def _year_choices_for(schemes: list[str]) -> list[str]:
    """Every financial year the given scheme(s) hold, distinct, oldest first.
    Falls back to the full set when `schemes` is empty or unrecognised."""
    years, _live = _available_years_for(schemes)
    return years


def _needs_year_clarification(question: str, schemes: list[str], resolved: dict) -> bool:
    """True when the question is a metric / breakdown question whose geography is
    already settled (so the scope gate skipped it) but which pins no financial
    year — not in its text, not via a resolved `year_key` — and doesn't ask for a
    time series or an explicit all-years total. Callers must have run
    `resolve_entities` first so `resolved` reflects any year actually named."""
    if not settings.YEAR_CLARIFY_ENABLED:
        return False
    q = question or ""
    if resolved.get("year_key") or _parse_year_key(q) is not None:
        return False
    if _ALL_YEARS_CUE.search(q) or _TIME_SERIES_CUE.search(q):
        return False
    # A cross-scheme "which / how many districts are in both schemes" question is
    # a membership/overlap set question — inherently across the whole data window,
    # so there is no single financial year to ask for.
    if _CROSS_SCHEME_SET_QUESTION.search(q):
        return False
    if not _METRIC_OR_BREAKDOWN_CUE.search(q):
        return False
    return True


def _year_clarification(question: str, schemes: list[str]) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    years, live = _available_years_for(schemes)
    options = [
        {"label": f"FY {y}", "question": f"{stem} for FY {y}"}
        for y in years
    ]
    options.append({
        "label": "All financial years combined",
        "question": f"{stem} across all financial years",
    })
    scope_word = live[0] if len(live) == 1 else "MGNREGA and PMAY-G"
    year_list = ", ".join(f"FY {y}" for y in years[:-1]) + f" and FY {years[-1]}"
    return ClarificationNeeded(
        f"{scope_word} data is available for {year_list}. "
        "Which of these is required — a single financial year, or all of them "
        "combined?",
        options=options,
        rule="year-not-specified",
    )


# ── "That year isn't in the data" guard ────────────────────────────────────
# Year coverage per scheme lives in _SCHEME_DATA_YEARS (loaded from megh_db at
# startup by refresh_scheme_years()). This guard is PER SCHEME: a year MGNREGA
# lacks (say FY 2019-20) but PMAY-G has is fine for a PMAY-G question and only
# blocked for an MGNREGA one.
#
# A question that names a financial year no scheme in play holds ("1999-20",
# "2010", "FY 2027-28", or "2019-20" for MGNREGA) can't be answered. Without the
# guard the year is dropped as a note and the flow either falls into the generic
# "which year?" pause (looks like we ignored what the user typed) or answers
# across the years that DO exist (a figure for the wrong period). Instead: tell
# the user which years that scheme has, with those years as one-tap chips.

# A raw mention that is clearly meant as a year/financial-year even though it
# didn't resolve — a 4-digit 19xx/20xx/21xx, or an "NN-NN" / "NNNN-NN" range.
_YEAR_SHAPED_RE = re.compile(
    r"\b(?:19|20|21)\d{2}\b|\b\d{2}\s*[-/]\s*\d{2}\b|\b\d{4}\s*[-/]\s*\d{2,4}\b"
)


def _looks_like_year_mention(text: str) -> bool:
    return bool(_YEAR_SHAPED_RE.search(text or ""))


def _allowed_year_starts(schemes: "list[str] | None") -> set:
    """FY start years the given scheme(s) actually hold (union). Empty schemes =>
    every scheme's years."""
    years, _live = _available_years_for(schemes or [])
    return {_fy_start(y) for y in years}


def _year_in_data_range(year_key: int, schemes: "list[str] | None" = None) -> bool:
    return year_key in _allowed_year_starts(schemes)


# A token that is unambiguously a financial year: an "NNNN-NN" / "NNNN-NNNN"
# range (any century), a 19xx or 21xx four-digit year, or a 20xx year in the
# 2010-2039 plausible band. A bare "2000" / "2500" is deliberately NOT matched
# so "top 2000 villages" isn't mistaken for a year.
_YEAR_RANGE_TOKEN_RE = re.compile(
    r"\b(?:fy\s*|financial\s+year\s*|fiscal(?:\s+year)?\s*)?"
    r"((?:19|20|21)\d\d\s*[-/]\s*\d{2,4}|(?:19|21)\d\d|20[1-3]\d)\b",
    re.IGNORECASE,
)


def _out_of_range_year_in(text: str, schemes: "list[str] | None" = None) -> "str | None":
    """Raw text of the first financial-year token in `text` that NONE of the
    given scheme(s) hold, or None if every year mentioned is available / none is
    mentioned. Runs on raw text only — no LLM, no DB."""
    for m in _YEAR_RANGE_TOKEN_RE.finditer(text or ""):
        tok = m.group(1)
        yk = _parse_year_key(tok)
        if yk is not None:
            if not _year_in_data_range(yk, schemes):
                return tok
        elif _YEAR_SHAPED_RE.search(tok):   # e.g. "1999-20" — a year we can't parse
            return tok
    return None


def _available_years_for(schemes: list[str]) -> "tuple[list[str], list[str]]":
    """(distinct FY list across the given scheme(s), scheme names used). Falls
    back to every scheme when `schemes` is empty or unrecognised."""
    live = [s for s in (schemes or []) if s in _SCHEME_DATA_YEARS] or list(_SCHEME_DATA_YEARS)
    seen: set[str] = set()
    years: list[str] = []
    for s in live:
        for y in _SCHEME_DATA_YEARS[s]:
            if y not in seen:
                seen.add(y)
                years.append(y)
    return years, live


def _year_out_of_range_clarification(question: str, raw_year: str,
                                    schemes: list[str]) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    # Strip the offending year (and any "for "/"in "/"FY " lead-in, or a bare
    # comma before it) out of the stem, so the chip questions we build below
    # don't carry it back in and trip this same pause on the next turn.
    stem = re.sub(
        r"\s*,?\s*(?:for\s+|in\s+|during\s+|of\s+)?(?:fy\s*)?" +
        re.escape(raw_year.strip()) + r"\b",
        "", stem, count=1, flags=re.IGNORECASE,
    ).strip().rstrip(",").strip() or question.strip().rstrip(" ?.")

    years, live = _available_years_for(schemes)
    if len(live) == 1:
        coverage = (f"For {live[0]}, data is available only for the financial "
                    f"years {', '.join(years)}.")
    else:
        per_scheme = "; ".join(
            f"{s} — {', '.join(_SCHEME_DATA_YEARS[s])}" for s in live
        )
        coverage = ("Data is available only for the following financial years: "
                    f"{per_scheme}.")

    options = [
        {"label": f"FY {y}", "question": f"{stem} for FY {y}"}
        for y in years
    ]
    options.append({
        "label": "All available years combined",
        "question": f"{stem} across all financial years",
    })
    return ClarificationNeeded(
        f"{coverage} No data is held for “{raw_year.strip()}”. "
        "Please select one of the financial years listed above, or all of them "
        "combined.",
        options=options,
        rule="year-out-of-range",
    )


def _named_schemes(question: str) -> list[str]:
    """Schemes named outright in the question text, in catalog order."""
    return [s for s, pattern in _SCHEME_NAME_PATTERN.items() if pattern.search(question)]


def _shortcut_scheme(question: str) -> list[str] | None:
    """Skip the classifier call when the text already pins the scheme set —
    faster and more reliable than the model for the common case, and one fewer
    model call per query at scale. Deterministic whenever:
      * two or more schemes are named  -> use exactly those (the user listed
        them; "compare MGNREGA and PMAY" needs no classification), or
      * an explicit cross-scheme phrase ("both schemes", "across schemes")
        is present  -> the whole catalog, or
      * exactly one scheme is named    -> that one.
    Returns None only when nothing in the text pins it, so the classifier still
    handles the genuinely unnamed/ambiguous case."""
    named = _named_schemes(question)
    if len(named) >= 2:
        return named
    if _EXPLICIT_BOTH.search(question):
        return list(SCHEME_CATALOG)
    return named if len(named) == 1 else None


async def classify_scheme(question: str) -> list[str]:
    """Which scheme(s) — MGNREGA, PMAY-G, or both — does this question touch?"""
    shortcut = _shortcut_scheme(question)
    if shortcut:
        logger.info("classify_scheme: shortcut matched %s, skipping model call", shortcut)
        return shortcut

    catalog = "\n".join(f'  - "{name}": {desc}' for name, desc in SCHEME_CATALOG.items())
    prompt = f"""Classify which scheme(s) this question needs. Available schemes:
{catalog}

Return ONLY JSON: {{"schemes": ["MGNREGA"|"PMAY-G", ...]}}
Use both if the question compares or combines the two schemes, or names neither specifically.

Question: "{question}"
JSON:"""
    raw = await llm.call_classifier(prompt, guided={"guided_json": _SCHEMES_JSON_SCHEMA})
    payload = _extract_json(raw)
    if payload and isinstance(payload.get("schemes"), list):
        schemes = [s for s in payload["schemes"] if s in SCHEME_CATALOG]
        # Safety net: a scheme the user named outright must never be dropped by
        # the classifier. Union it back in (catalog order) so "across MGNREGA and
        # PMAY-G" can't come back PMAY-only.
        for s in _named_schemes(question):
            if s not in schemes:
                schemes.append(s)
        if schemes:
            return [s for s in SCHEME_CATALOG if s in schemes]
    # Deterministic fallback if the model call fails or returns unusable JSON.
    logger.warning("classify_scheme: unusable response %r — defaulting to both schemes", raw[:200])
    return list(SCHEME_CATALOG)


# Bare dimension nouns and the state name are NOT place mentions — "which
# villages have activity in Meghalaya" names no specific village or district.
# The classifier sometimes extracts them anyway; drop them before resolution,
# or `resolve_village("villages")` fuzzy-matches "Model Village" etc. and raises
# a nonsense clarification.
_GENERIC_PLACE_TERMS = {
    "village", "villages", "vill", "hamlet", "hamlets", "gaon",
    "district", "districts", "dist", "distt",
    "block", "blocks", "dev block", "development block", "cd block", "c.d. block",
    "panchayat", "panchayats", "gram panchayat", "gp", "gps", "vec",
    "state", "meghalaya", "region", "regions", "area", "areas",
    "place", "places", "location", "locations", "zone", "zones",
    "year", "years", "fy", "financial year", "all", "every", "each", "any",
}


def _clean_mention(value: str) -> str | None:
    """Normalise a raw mention; return None if it's a bare dimension word / the
    state name (i.e. not an actual place or period)."""
    v = value.strip().strip("\"'`.,?!()[]").strip()
    core = re.sub(r"^(the|a|an|this|that|each|every|all)\s+", "", v, flags=re.IGNORECASE).strip()
    if not core or core.lower() in _GENERIC_PLACE_TERMS:
        return None
    return v


async def extract_entity_mentions(question: str) -> dict:
    """Which spans of the question name a district, block, village or year?
    Span-finding only — resolving each span to a canonical DB value is a
    separate, deterministic step (entity_resolver), not this LLM call."""
    prompt = f"""Extract place/time names from this question, verbatim as the user typed
them. Do not correct spelling or guess the canonical form.

Return ONLY JSON with keys from: "district", "block", "village", "year".
Include a key ONLY when the question NAMES a specific one. A bare word like
"village", "villages", "district", "block", "year", or the state name
"Meghalaya" is NOT a name — omit it. If the question names none, return {{}}.

Question: "{question}"
JSON:"""
    raw = await llm.call_classifier(prompt, guided={"guided_json": _ENTITY_JSON_SCHEMA})
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in payload.items():
        if k in ("district", "block", "village", "year") and isinstance(v, str) and v.strip():
            cleaned = _clean_mention(v)
            if cleaned:
                out[k] = cleaned
    return out


def _parse_year_key(text: str) -> "int | None":
    """FY start year as an int from '2023', '2023-24', '2023-2024', 'FY 2023-24',
    'FY23'. Returns None if no plausible year (2010-2039) is present."""
    m = re.search(r"\b(20[1-3]\d)\s*[-/]\s*(?:20)?\d{2}\b", text)   # 2023-24 / 2023-2024
    if m:
        return int(m.group(1))
    m = re.search(r"\bfy\s*'?(\d{2})\b", text, re.IGNORECASE)        # FY23
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"\b(20[1-3]\d)\b", text)                          # bare 2023
    if m:
        return int(m.group(1))
    return None


async def resolve_entities(question: str, schemes: list[str]) -> dict:
    """Resolve every extracted mention to a canonical DB value. Ambiguous ->
    raise ClarificationNeeded (ask, per the resolver's own hard rule — never
    guess). Not-found is not an error: it means the value genuinely is not in
    this data, which the response composer should say plainly, not silently
    drop the filter."""
    # Out-of-range financial year — checked FIRST, before the LLM mention call
    # and any geography resolution. Works purely off the raw question text, so it
    # still fires when the extractor is unavailable or drops the year slot (a
    # comma-joined scope-pause reply like "wgh, 1999-20" does exactly that). A
    # year outside the data window makes the whole question unanswerable
    # regardless of the place, so this must not depend on anything downstream.
    if settings.YEAR_RANGE_GUARD_ENABLED:
        _yraw = _out_of_range_year_in(question, schemes)
        if _yraw is not None:
            raise _year_out_of_range_clarification(question, _yraw, schemes)

    mentions = await extract_entity_mentions(question)

    resolved: dict[str, object] = {}
    notes: list[str] = []
    # Human-readable names for whatever the query ends up filtering on, keyed by
    # dimension. Handed to the response composer so it says "West Garo Hills",
    # not the "wgh" the user typed or the "WEST GARO HILLS" DB literal.
    display: dict[str, str] = {}

    district_canon = None
    if not mentions.get("district"):
        # The LLM mention-extractor intermittently drops a plainly-named district
        # ("how many villages are covered in West Garo Hills" -> {}); fall back to
        # a deterministic scan of the raw question against the 12-name closed set.
        backstop = scan_dimension(question, schemes[0], "district")
        if backstop and backstop.status == "resolved":
            district_canon = backstop.canonical
            resolved["district"] = backstop.canonical
            display["district"] = backstop.display or str(backstop.canonical).title()
            logger.info("resolve_entities: district backstop matched %r in question text",
                        backstop.canonical)
    if mentions.get("district"):
        r = resolve_dimension(mentions["district"], schemes[0], "district")
        if r.status == "ambiguous":
            raise ClarificationNeeded(f"Which district is being referred to by “{mentions['district']}”?")
        if r.status == "resolved":
            district_canon = r.canonical
            resolved["district"] = r.canonical
            display["district"] = r.display or str(r.canonical).title()
        elif settings.OUT_OF_SCOPE_GUARD_ENABLED:
            # A named district that isn't one of Meghalaya's 12 — almost always a
            # place in another state ("districts in Guwahati"). Don't silently
            # drop the filter and count 0; say plainly this is out of scope.
            raise OutOfScope(f"district '{mentions['district']}' is not in Meghalaya")
        else:
            notes.append(f"'{mentions['district']}' is not a known district — say so, do not filter on it.")

    if mentions.get("block"):
        r = resolve_dimension(mentions["block"], schemes[0], "block")
        if r.status == "ambiguous":
            raise ClarificationNeeded(f"Which block is being referred to by “{mentions['block']}”?")
        if r.status == "resolved":
            resolved["block"] = r.canonical
            display["block"] = r.display or str(r.canonical).title()
        elif settings.OUT_OF_SCOPE_GUARD_ENABLED:
            raise OutOfScope(f"block '{mentions['block']}' is not in Meghalaya")
        else:
            notes.append(f"'{mentions['block']}' is not a known block — say so, do not filter on it.")

    if mentions.get("year"):
        raw_year = mentions["year"]
        year_key = None
        # MGNREGA has a curated year catalogue (handles aliases like "FY24",
        # "last year"); try it first when MGNREGA is in play.
        if "MGNREGA" in schemes:
            r = resolve_dimension(raw_year, "MGNREGA", "year")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which financial year is being referred to by “{raw_year}”?")
            if r.status == "resolved":
                year_key = r.canonical
        # Fallback / PMAY path — PMAY has no year catalogue, so parse the FY start
        # year straight from the text ("2023", "2023-24", "FY 2023-24" -> 2023).
        if year_key is None:
            year_key = _parse_year_key(raw_year)
        if year_key is not None and not _year_in_data_range(year_key, schemes):
            # A real, parseable year — just not one these scheme(s) hold.
            if settings.YEAR_RANGE_GUARD_ENABLED:
                raise _year_out_of_range_clarification(question, raw_year, schemes)
            _yrs, _lv = _available_years_for(schemes)
            notes.append(f"'{raw_year}' is outside the data "
                         f"({', '.join(_lv)} cover FY {_yrs[0]} to FY {_yrs[-1]} only).")
        elif year_key is None:
            # Couldn't pin it to a year at all. If it was plainly typed as one
            # ("1999-20", "FY 2019-20"), it's an out-of-range year we simply
            # failed to parse — say the coverage, same as above; otherwise it's
            # gibberish in the year slot, so leave a soft note and move on.
            if settings.YEAR_RANGE_GUARD_ENABLED and _looks_like_year_mention(raw_year):
                raise _year_out_of_range_clarification(question, raw_year, schemes)
            notes.append(f"'{raw_year}' is not a recognisable financial year.")
        else:
            # Both v_employment and v_pmay key the year on `year_key` (smallint).
            resolved["year_key"] = year_key
            display["year"] = f"FY {year_key}-{(year_key + 1) % 100:02d}"

    if mentions.get("village"):
        r = await resolve_village(mentions["village"], district=district_canon)
        if r.status == "ambiguous":
            options = ", ".join(f"{c['name']} ({c['district']})" for c in r.candidates[:5])
            raise ClarificationNeeded(f"“{mentions['village']}” corresponds to more than one village: {options}. Which of these is intended?")
        if r.status == "resolved":
            resolved["village_code"] = r.canonical
            display["village"] = r.display or str(mentions["village"]).title()
        else:
            notes.append(f"'{mentions['village']}' is not a known village — say so, do not filter on it.")

    # PMAY house-construction-stage ("Proposed Site", "Existing site(Old House)",
    # "plinth stage", "not started", …). A closed set the LLM mention-extractor
    # above doesn't cover — matched deterministically against the SME alias
    # catalogue over the whole question. "completed" / "in progress" are left to
    # the is_completed / is_in_progress boolean path on purpose.
    if "PMAY-G" in schemes:
        hs = resolve_house_status(question, "PMAY-G")
        if hs and hs.values:
            resolved["house_status"] = hs.values if len(hs.values) > 1 else hs.values[0]
            display["house_status"] = hs.display or " and ".join(hs.values)

    return {"resolved": resolved, "notes": notes, "display": display}


async def generate_sql(question: str, schemes: list[str], entity_result: dict) -> str:
    # The whole prompt — hand-written backbone + live schema + SME catalog +
    # prohibited joins + few-shot + resolved entities — is assembled in one place.
    prompt = prompt_builder.build_sql_prompt(question, schemes, entity_result)
    raw = await llm.call_sql_generator(prompt, guided={"guided_regex": _SQL_SHAPE_REGEX})
    return _extract_sql(raw)


# The generator sometimes reads "in Meghalaya" as a place filter and invents a
# WHERE on a state pseudo-row that no fact row matches — the query then runs
# clean but counts 0. Catch that shape and force one repair pass (the repair
# prompt carries the "whole dataset is Meghalaya" rule, so it drops the filter).
_STATE_PSEUDO_FILTER = re.compile(
    r"entity_type\s*=\s*'\s*state\s*'"
    r"|lgd_(?:village_name|district|block)\s*(?:=|ilike)\s*'\s*%?\s*meghalaya\s*%?\s*'",
    re.IGNORECASE,
)


# curated.* stores lgd_district / lgd_block UPPERCASE (docs/DATA_MODEL.md). When
# entity resolution misses and the generator copies the question's Title-Case
# spelling straight into the literal (lgd_district = 'West Garo Hills'), the
# query runs clean and counts zero. Upper-case *only* the string literals
# compared with `=` / `!=` / `IN` against those two columns — nothing else is
# touched, and an already-uppercase or ILIKE clause is left as-is.
_GEO_LITERAL_RE = re.compile(
    r"(?P<pre>\blgd_(?:district|block)\s*(?:=|!=|<>|\bIN\b)\s*\(?\s*)"
    r"(?P<lits>'(?:[^']|'')*'(?:\s*,\s*'(?:[^']|'')*')*)",
    re.IGNORECASE,
)


def _uppercase_geo_literals(sql: str) -> str:
    changed = _GEO_LITERAL_RE.sub(lambda m: m.group("pre") + m.group("lits").upper(), sql)
    if changed != sql:
        logger.info("normalised lgd_district/lgd_block literal(s) to upper-case for storage match")
    return changed


# A `column "X" does not exist` error usually means the generator picked a view
# that lacks a geography column (e.g. v_pmay_monthly_sanctions) rather than a
# genuine typo. Point the repair at the objects that DO carry the column instead
# of just echoing the Postgres message back.
_MISSING_COL_RE = re.compile(r'column "([\w.]+)" does not exist', re.IGNORECASE)
_COL_HOMES = {
    "lgd_district": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                    "curated.v_district_year_summary, "
                    "curated.v_cross_scheme_money_district_year, curated.dim_geography",
    "lgd_block": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                 "curated.dim_geography",
    "lgd_village_name": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                        "curated.dim_geography",
}


def _missing_column_hint(error: str) -> "str | None":
    m = _MISSING_COL_RE.search(error)
    if not m:
        return None
    col = m.group(1).split(".")[-1].lower()
    if col in ("scheme_key", "scheme_code"):
        # The generator added a scheme filter/join to a per-scheme object that
        # carries neither column. scheme_key / scheme_code live ONLY on
        # curated.dim_scheme and curated.v_cross_scheme_money_district_year.
        # Every row in curated.v_pmay / v_expenditure / v_employment /
        # fact_pmay_house / fact_mgnrega_* already belongs to one scheme, so a
        # single-scheme question needs no scheme filter and no join to
        # dim_scheme at all.
        return (f'"{col}" does not exist on the object the previous query selected from. '
                "It lives ONLY on curated.dim_scheme and "
                "curated.v_cross_scheme_money_district_year. The PMAY-G and MGNREGA "
                "fact tables and views (curated.v_pmay, curated.v_expenditure, "
                "curated.v_employment, curated.fact_pmay_house, curated.fact_mgnrega_*) "
                "are each already a single scheme — DELETE the scheme filter and any "
                "JOIN to curated.dim_scheme entirely, and keep every other clause "
                "(year_key, geography, NOT is_placeholder, aggregation) exactly as it was.")
    homes = _COL_HOMES.get(col)
    if not homes:
        return (f'The previous query used a column "{col}" that its source object does not '
                "have. Select from a curated object that exposes every column you reference.")
    tail = (" — for a per-district PMAY-G breakdown use curated.v_pmay and GROUP BY lgd_district")
    if col in ("lgd_block", "lgd_village_name"):
        # The usual cause here is a cross-scheme money question at block/village grain
        # pointed at v_cross_scheme_money_district_year, which is district x year only.
        tail = (". For combined MGNREGA + PMAY-G money at block or village grain there is no "
                "cross-scheme view: aggregate curated.v_expenditure and curated.v_pmay to that "
                "grain in separate CTEs (MGNREGA total_exp is LAKH -> /100; PMAY amount_released "
                "is RUPEES -> /1e7, WHERE NOT is_placeholder), then FULL OUTER JOIN the two CTEs "
                "on the grain columns and sum the two crore figures")
    return (f'"{col}" does not exist on the object the previous query selected from. That '
            f"column lives on: {homes}. Rebuild against one of those{tail}.")


# The generator reaches for COUNT(DISTINCT x) OVER (...) on "top N% / decile /
# concentration" questions; Postgres rejects DISTINCT (and nested aggregates)
# inside a window function. A plain error echo doesn't get the model out of the
# pattern — hand it the rank-in-a-CTE recipe explicitly.
_WINDOW_FN_ERR_RE = re.compile(
    r"DISTINCT is not implemented for window functions"
    r"|window function calls cannot contain"
    r"|aggregate function calls cannot contain window function calls",
    re.IGNORECASE,
)


def _window_fn_hint(error: str) -> "str | None":
    if not _WINDOW_FN_ERR_RE.search(error):
        return None
    return (
        "PostgreSQL forbids DISTINCT and nested aggregates inside a window function, so "
        "COUNT(DISTINCT ...) OVER (...) cannot work. Rebuild as: (1) a CTE that aggregates "
        "the metric per unit — SUM(<metric>) AS m ... GROUP BY <unit>; (2) a CTE that ranks "
        "those units — NTILE(<100/percent>) OVER (ORDER BY m DESC) AS bucket (top 10% -> "
        "NTILE(10), quartile -> NTILE(4)); (3) an outer SELECT returning "
        "ROUND(100.0 * SUM(m) FILTER (WHERE bucket = 1) / NULLIF(SUM(m), 0), 1). "
        "Keep every filter (year, scheme, geography) from the failed query on the first CTE."
    )


def _repair_hint(error: str) -> "str | None":
    """The single targeted hint fed to the repair prompt — most specific first."""
    return _missing_column_hint(error) or _window_fn_hint(error)


# curated.v_employment and curated.v_expenditure are at source-row grain (many
# rows per village) — schema_context MGNREGA rule 2: "always SUM ... GROUP BY,
# never read a row raw". The generator sometimes answers an aggregate question
# ("how many job cards", "how much was spent") with a bare
# `SELECT <metric> FROM curated.v_employment WHERE ... LIMIT 1` — it runs clean
# and returns one village's number as if it were the statewide total (the
# "116 job cards for all of Meghalaya" bug). Detect that shape and force a
# repair pass; the hint tells the generator to wrap the metric in SUM(...).
_ROWGRAIN_VIEWS = ("curated.v_employment", "curated.v_expenditure")
_OUTER_SELECT_FROM_RE = re.compile(
    r"^\s*SELECT\b(?P<cols>.*?)\bFROM\b\s+(?P<src>[A-Za-z_][\w.]*)",
    re.IGNORECASE | re.DOTALL,
)
_AGG_CALL_RE = re.compile(r"\b(?:SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


def _rowgrain_no_aggregate(question: str, sql: str) -> "str | None":
    """The source-row-grain view a bare-row aggregate query reads from, or None.

    Fires only when the question is aggregate-shaped, the statement is a plain
    (non-CTE) SELECT whose FROM target is one of the source-row-grain views, the
    outer select list has no aggregate call, and there is no GROUP BY."""
    q = question or ""
    if not (_AGGREGATE_CUE.search(q) or _BARE_METRIC_CUE.search(q)):
        return None
    s = sql.strip()
    if re.match(r"^\s*WITH\b", s, re.IGNORECASE):
        return None
    m = _OUTER_SELECT_FROM_RE.match(s)
    if not m:
        return None
    src = m.group("src").lower().strip('"')
    if src not in _ROWGRAIN_VIEWS:
        return None
    if _AGG_CALL_RE.search(m.group("cols")) or _GROUP_BY_RE.search(s):
        return None
    if re.match(r"^\s*SELECT\s+'", s, re.IGNORECASE):  # canned text answer, not a data read
        return None
    return src


async def execute_with_repair(question: str, schemes: list[str], entity_result: dict,
                              initial_sql: str | None = None, *,
                              max_repairs: int = 2) -> tuple[str, list[dict]]:
    sql = initial_sql if initial_sql is not None else await generate_sql(question, schemes, entity_result)
    for attempt in range(max_repairs + 1):
        sql = _uppercase_geo_literals(sql)
        try:
            if _STATE_PSEUDO_FILTER.search(sql):
                raise ValueError(
                    "generated SQL filters on a non-existent 'Meghalaya' / entity_type='State' "
                    "pseudo-row — the whole dataset is already Meghalaya; remove that geographic "
                    "filter entirely and keep every other clause"
                )
            bad_view = _rowgrain_no_aggregate(question, sql)
            if bad_view:
                raise ValueError(
                    f"this is an aggregate question but the query reads raw rows from {bad_view}, "
                    "which is at source-row grain (many rows per village) — a bare "
                    "SELECT <metric> ... LIMIT 1 returns ONE arbitrary row, not a total. Wrap the "
                    "metric in SUM(...): for a statewide total use "
                    "SELECT SUM(<metric>) AS <name> FROM <view> [WHERE year_key = ...] with no "
                    "GROUP BY; add GROUP BY <unit> only if the question asks for a per-district / "
                    "per-block / per-year breakdown. job_cards_issued_total is a STOCK — pin a "
                    "single year_key (the latest if none is named) and SUM across geographies, "
                    "never across years. Keep every other clause exactly as it was."
                )
            rows = await run_readonly(sql)
            return sql, rows
        except (UnsafeSQLError, Exception) as e:
            if attempt == max_repairs:
                raise  # repair budget spent — let the caller fall back
            logger.warning("SQL failed (attempt %d/%d), repairing: %s",
                           attempt + 1, max_repairs + 1, e)
            # The repair prompt carries the same schema + resolved-entities context as
            # the first attempt (not a schema-only stub) — a repair usually fails for
            # want of exactly that context — plus a targeted hint when the error names
            # a missing column.
            repair_prompt = prompt_builder.build_repair_prompt(
                question, schemes, entity_result, failed_sql=sql, error=str(e),
                extra_hint=_repair_hint(str(e)))
            raw = await llm.call_sql_generator(repair_prompt, guided={"guided_regex": _SQL_SHAPE_REGEX})
            sql = _extract_sql(raw)
    return sql, []  # unreachable: the loop always returns rows or re-raises


# ── Numeric faithfulness guard for the composed answer ──────────────────────
# The composer is an LLM and will occasionally transcribe a number wrong
# ("170981" -> "17098"). On a government dashboard that is unacceptable, so the
# composed sentence is checked against the actual result values: any number it
# states that is not in the data (beyond rounding tolerance) triggers one strict
# retry, then a deterministic fallback sentence built straight from the rows.
_NUM_TOKEN = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# "the data doesn't cover / isn't available / can't be broken down" — a hedge the
# composer must not use when the query actually returned a usable non-zero value.
_HEDGE_RE = re.compile(
    r"do(?:es)?n['’]?t\s+cover|not\s+cover(?:ed)?|isn['’]?t\s+covered|"
    r"not\s+available|no\s+data\b|doesn['’]?t\s+(?:have|include|contain|provide)|"
    r"only\s+provides?\b|can(?:not|['’]?t)\s+(?:be\s+)?(?:broken\s+down|split)|"
    r"no\s+(?:specific\s+)?(?:breakdown|split)\b",
    re.IGNORECASE,
)


def _as_number(v: object) -> "int | float | None":
    """Coerce a result cell to int/float, or None if it isn't numeric. Handles
    decimal.Decimal (asyncpg returns it for SUM/AVG over numeric columns — the
    reason a cross-scheme SUM was being skipped) without relying on
    Decimal.is_integer(), which is Python 3.12+ only."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, numbers.Number):          # float, Decimal, Fraction, …
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return int(s) if "." not in s else float(s)
    return None


def _data_numbers(rows: list[dict]) -> set[str]:
    """Every numeric leaf value in the result, as normalised digit strings."""
    out: set[str] = set()
    for row in rows:
        for v in row.values():
            n = _as_number(v)
            if n is None:
                continue
            if isinstance(n, int):
                out.add(str(n))
            else:
                out.add(f"{n:g}")
                out.add(f"{n:.2f}".rstrip("0").rstrip("."))
    return out


def _answer_numbers_faithful(answer: str, data_nums: set[str]) -> bool:
    """True if every 'reported' number in the answer traces back to a data value.
    Years and small integers (< 100, no decimal) are treated as prose, not data."""
    data_floats: list[float] = []
    for d in data_nums:
        try:
            data_floats.append(float(d))
        except ValueError:
            pass
    for tok in _NUM_TOKEN.findall(answer):
        raw = tok.replace(",", "")
        # A year or a financial-year range fragment — "2020", "2020-2021",
        # "2020-21" — is prose, not a measured quantity. The number tokenizer
        # splits "2020-2021" into "2020" and "-2021" (the hyphen read as a
        # sign), so the second-half forms "-YYYY" / "-YY" are matched here too.
        if re.fullmatch(r"-?(19|20|21)\d\d|-\d{2}", raw):
            continue
        try:
            fval = float(raw)
        except ValueError:
            continue
        if abs(fval) < 100 and "." not in raw:         # ordinal / "one or two"
            continue
        if raw in data_nums:
            continue
        if any(dv == fval or (dv and abs(dv - fval) / abs(dv) <= 0.005)
               or abs(dv - fval) < 0.5 for dv in data_floats):
            continue
        return False
    return True


def _row_metrics(row: dict) -> list[tuple[str, "int | float"]]:
    """(column, numeric value) for every numeric cell in the row — Decimal included."""
    out: list[tuple[str, "int | float"]] = []
    for k, v in row.items():
        n = _as_number(v)
        if n is not None:
            out.append((k, n))
    return out


def _fmt_num(v: "int | float") -> str:
    return f"{v:,}" if isinstance(v, int) else f"{v:,.2f}"


def _answer_covers_metrics(answer: str, row: dict) -> bool:
    """Every numeric metric in a single aggregate row must appear in the answer —
    catches a cross-scheme result where the composer names only one side."""
    ans = answer.replace(",", "")
    for _k, v in _row_metrics(row):
        forms = {str(v)}
        if isinstance(v, float):
            forms.add(f"{v:.2f}".rstrip("0").rstrip("."))
        if not any(f in ans for f in forms):
            return False
    return True


# Numeric-looking columns that must NOT be summed/averaged — years, codes, ids,
# rank/serial numbers, resolved *_key columns. Reported as dimension coverage
# (distinct values) instead. Deliberately narrow: "no"/"sr" are omitted because
# they collide with "no_of_*" count columns; the real offenders are
# financial_year, *_code, *_id and pincode.
_NONSTAT_COL = re.compile(
    r"(^|_)(year|yr|fy|pincode|rank|serial)($|_)|_key$|_code$|_id$|(^|_)id$",
    re.IGNORECASE,
)


def _result_digest(rows: list[dict], max_values: int = 40) -> "tuple[str, set[str]]":
    """A deterministic whole-result summary, computed from EVERY row (not just the
    preview the composer is shown): the distinct coverage of each dimension
    column, and sum / mean / max / min of each numeric column together with which
    dimension row holds the max and the min.

    Returns (indented_text_block, allowed_number_strings). The strings are unioned
    into the numeric-faithfulness whitelist so a total or average the composer
    copies out of this block is not flagged as an invented number. Without this,
    any multi-row result (a district x year matrix, a per-block list) could only
    be described one visible cell at a time — never "the total is X" or "Y is
    highest" — because those figures are not present in the raw rows."""
    if len(rows) < 2:
        return "", set()

    dim_order: dict[str, list[str]] = {}
    dim_seen: dict[str, set[str]] = {}
    num_cols: list[str] = []
    for row in rows:
        for k, v in row.items():
            is_num = _as_number(v) is not None
            if is_num and not _NONSTAT_COL.search(k):
                if k not in num_cols:
                    num_cols.append(k)
                continue
            if v is None:
                continue
            s = str(v)
            if k not in dim_order:
                dim_order[k], dim_seen[k] = [], set()
            if s not in dim_seen[k]:
                dim_seen[k].add(s)
                dim_order[k].append(s)

    def _label(row: dict) -> str:
        parts = [str(row[k]) for k in dim_order if row.get(k) is not None]
        return " / ".join(parts) if parts else "(row)"

    lines: list[str] = [f"total rows: {len(rows)}"]
    allowed: set[str] = set()

    for k, vals in dim_order.items():
        shown = ", ".join(vals) if len(vals) <= max_values else f"{len(vals)} distinct values"
        lines.append(f"{k} ({len(vals)}): {shown}")

    for k in num_cols:
        pairs = [(r, _as_number(r.get(k))) for r in rows if _as_number(r.get(k)) is not None]
        if not pairs:
            continue
        nums = [n for _r, n in pairs]
        total = sum(nums)
        hi_row, hi = max(pairs, key=lambda p: p[1])
        lo_row, lo = min(pairs, key=lambda p: p[1])
        mean = total / len(nums)
        for val in (total, hi, lo, mean):
            f = float(val)
            allowed.add(str(int(f)) if f.is_integer() else f"{f:g}")
            allowed.add(f"{f:.2f}".rstrip("0").rstrip("."))
        lines.append(
            f"{k}: sum={_fmt_num(total)}; mean={_fmt_num(round(mean, 2))}; "
            f"max={_fmt_num(hi)} at [{_label(hi_row)}]; min={_fmt_num(lo)} at [{_label(lo_row)}]"
        )

    return "\n".join(f"  - {ln}" for ln in lines), allowed


def _deterministic_answer(rows: list[dict]) -> str:
    """A plain, exact sentence from the rows — used only when the LLM composer
    keeps misquoting or dropping numbers."""
    if len(rows) == 1:
        nums = _row_metrics(rows[0])
        if len(nums) == 1:
            k, v = nums[0]
            return f"{_fmt_num(v)} {k.replace('_', ' ')}."
        if len(nums) >= 2:
            return "; ".join(f"{k.replace('_', ' ')}: {_fmt_num(v)}" for k, v in nums) + "."
    return "Results — " + "; ".join(
        ", ".join(f"{k}: {v}" for k, v in r.items()) for r in rows[:5]
    )


def _is_plain_list_result(rows: list[dict]) -> bool:
    """A multi-row result whose rows carry no numeric metric — a pure list of
    dimension values (e.g. the districts that satisfy a coverage filter). Those
    rows ARE the answer; a 'not covered / can't tell' hedge over them is wrong."""
    return len(rows) >= 2 and not any(_row_metrics(r) for r in rows)


def _deterministic_list_answer(rows: list[dict]) -> str:
    """Plain sentence naming every value in a single-column list result — used
    when the composer hedges over a membership answer whose rows already are the
    answer set."""
    cols = list(rows[0].keys())
    if len(cols) == 1:
        vals = [str(r[cols[0]]) for r in rows if r.get(cols[0]) is not None]
        label = cols[0].replace("_", " ")
        if len(vals) <= 40:
            return f"{len(vals)} {label} values match: " + ", ".join(vals) + "."
        return (f"{len(vals)} {label} values match, including: "
                + ", ".join(vals[:40]) + ", …")
    return _deterministic_answer(rows)


_DIM_LABEL = {
    "district": "district",
    "block": "block (C&RD block)",
    "village": "village",
    "year": "financial year",
    "house_status": "house construction stage",
}


def _entity_names_block(entities: dict[str, str] | None) -> str:
    """A context block naming the canonical form of every place / year / category
    the query actually filtered on, so the composer uses the full proper name in
    its answer instead of echoing the user's abbreviation, code or misspelling
    ("wgh" -> "West Garo Hills", "fy24" -> "FY 2024-25")."""
    if not entities:
        return ""
    lines = [f"  - {_DIM_LABEL.get(k, k)}: {v}" for k, v in entities.items() if v]
    if not lines:
        return ""
    return (
        "\nEntity names — the query filtered on exactly these values. In your "
        "answer, refer to each place, year or category by the full name given "
        "here, even when the question used a short form, code or misspelling; "
        "never echo the user's shorthand back as the name.\n" + "\n".join(lines) + "\n"
    )


async def compose_response(question: str, sql: str, rows: list[dict],
                           notes: list[str] | None = None,
                           entities: dict[str, str] | None = None,
                           schemes: list[str] | None = None) -> str:
    preview = rows[:40]
    truncated = len(rows) > len(preview)
    # "No usable value" — an empty result, or one whose every numeric cell is 0
    # or null. This is the shape a metric the data simply doesn't track comes
    # back as; when we see it, hand the composer the real metric list so it can
    # tell the user exactly what IS available rather than a vague "not covered".
    _nums = [n for r in rows for _k, n in _row_metrics(r)]
    no_usable_value = (not rows) or (bool(_nums) and all(n in (0, None) for n in _nums))
    metrics_block = ""
    if no_usable_value:
        metrics_block = (
            "\nMetrics the data DOES carry — if the question asked for something "
            "that is not in this list, say plainly it isn't tracked in the "
            "MGNREGA / PMAY-G data available, then name what is:\n"
            + available_metrics_text(schemes or []) + "\n"
        )
    # A metric the schema simply doesn't carry comes back as no rows, or as a
    # single 0 / NULL. Tell the composer to say that plainly instead of
    # reporting a confident "0" the user will read as a real measurement.
    guidance = (
        "If the result contains a number that answers the question, state it "
        "plainly as the answer — a nonzero COUNT is exactly the count that was "
        "asked for; never reply that the data 'doesn't cover' a metric the query "
        "just counted. ONLY when the result is empty, or the value is 0 or null, "
        "do NOT assert a real count of zero — instead say the data available "
        "doesn't cover that metric for the scheme/area asked, and name what IS "
        "available if you can tell from the query. Never describe a column the "
        "query didn't select. Copy every number digit-for-digit from the result "
        "JSON or the whole-result summary — do not shorten, round or reformat it "
        "(adding thousands separators is fine). If a Context note below says the "
        "question assumed a figure the data contradicts, correct that figure in "
        "your first sentence and answer from the real value — never echo the "
        "user's assumed number as if it were right. If the result is a list of "
        "names or rows and carries no numeric column, that list IS the answer — "
        "it is the exact set the query already selected as matching the question "
        "(e.g. 'which districts have both schemes'). Name those values as the "
        "answer; never say the data 'only lists names' or lacks the detail to "
        "decide — the filtering happened in the query."
    )
    # For any multi-row result, hand the composer a deterministic summary built
    # from EVERY row — totals, mean, extremes, full dimension coverage — so its
    # answer matches the charts/table instead of describing only the ~40 rows it
    # can see. Also stops it declaring that rows it can't see "have no data".
    digest_text, digest_nums = _result_digest(rows)
    summary_block = ""
    if digest_text:
        warn = ""
        if truncated:
            warn = (
                f" Only the first {len(preview)} of {len(rows)} rows appear below; "
                "do NOT say any district, block, village, year or category is "
                "missing or has no data merely because it is absent from them."
            )
        summary_block = (
            "\nWhole-result summary — computed from EVERY row. Use THESE figures "
            "for any total, average, highest/lowest or coverage statement; they "
            "are authoritative even though only some rows are shown below." + warn
            + "\n" + digest_text + "\n"
        )
    notes_block = ""
    if notes:
        notes_block = "\nContext (question premises to reconcile against the data — " \
                      "correct any the result contradicts):\n" + \
                      "\n".join(f"  - {n}" for n in notes) + "\n"
    names_block = _entity_names_block(entities)
    prompt = f"""Answer the user's question in one to three plain sentences, using ONLY the
numbers in the result and the whole-result summary below — do not invent or round
differently than shown.
{guidance}
{metrics_block}{names_block}{summary_block}{notes_block}
Question: "{question}"
Result ({len(rows)} row(s), showing up to {len(preview)}):
{json.dumps(preview, default=str)}

Answer:"""
    answer = await llm.call_response_composer(prompt)

    data_nums = _data_numbers(preview) | digest_nums
    # A note may ask the composer to name the figure the question wrongly assumed
    # ("not the 1.71 L assumed…") — allow those premise numbers through the
    # faithfulness check so the correction itself isn't flagged as a misquote.
    if notes:
        for _p in premise_check.extract_premises(question):
            data_nums.add(f"{_p.value:g}")
            data_nums.add(re.sub(r"[^\d.]", "", _p.text) or f"{_p.value:g}")
    misquoted = bool(data_nums) and not _answer_numbers_faithful(answer, data_nums)
    # A single aggregate row with 2+ metrics (e.g. a cross-scheme MGNREGA + PMAY
    # count) — the answer must report every one, not just the first scheme.
    dropped_metric = (len(preview) == 1 and len(_row_metrics(preview[0])) >= 2
                      and not _answer_covers_metrics(answer, preview[0]))

    if misquoted or dropped_metric:
        why = "MISQUOTED a number" if misquoted else "left out one of the result values"
        logger.warning("compose_response: answer %s (%r) — strict retry", why, answer[:160])
        if len(preview) == 1:
            allowed = "; ".join(f"{k} = {v}" for k, v in preview[0].items())
        else:
            allowed = ", ".join(sorted(data_nums, key=len, reverse=True))
        strict_prompt = (
            prompt + "\n" + answer.strip() +
            f"\n\nThat answer {why}. State EVERY value in the result, each labelled with what "
            "it measures, digit-for-digit (thousands separators allowed). The values are:\n  "
            + allowed + "\nRewrite the answer now.\n\nAnswer:"
        )
        answer = await llm.call_response_composer(strict_prompt)
        still_bad = (bool(data_nums) and not _answer_numbers_faithful(answer, data_nums)) or (
            len(preview) == 1 and len(_row_metrics(preview[0])) >= 2
            and not _answer_covers_metrics(answer, preview[0]))
        if still_bad:
            logger.warning("compose_response: retry still wrong — using deterministic answer")
            answer = _deterministic_answer(preview)

    # Deterministic safety net for the opposite failure: the query DID return a
    # real, non-zero number, but the composer hedged with a "not covered / no
    # data / can't break it down" phrasing anyway (seen when the question names
    # two categories joined by "or" and the query returns their combined COUNT).
    # A usable number must be reported as the answer.
    if _HEDGE_RE.search(answer):
        metrics_now = _row_metrics(preview[0]) if len(preview) == 1 else []
        if metrics_now and all(v not in (0, None) for _k, v in metrics_now):
            logger.warning("compose_response: hedged over a real value %r — deterministic answer",
                           answer[:160])
            answer = _deterministic_answer(preview)
        elif _is_plain_list_result(rows):
            logger.warning("compose_response: hedged over a %d-row list result %r — "
                           "deterministic list answer", len(rows), answer[:160])
            answer = _deterministic_list_answer(rows)
    return answer


async def classify_intent(question: str) -> str:
    """DATA (a number from megh_db) vs KNOWLEDGE (how the scheme works, from the
    reference docs). Keyword fast-path first; one classifier call otherwise."""
    # "what are common districts in both schemes?" opens with "what are", which
    # otherwise reads as KNOWLEDGE — but it is a list computed from the coverage
    # data, never something in the reference docs. Force DATA before the keyword
    # fast-path so the "what are" knowledge cue can't win.
    if _CROSS_SCHEME_SET_QUESTION.search(question):
        return "DATA"
    if _DATA_HINTS.search(question) and not _KNOWLEDGE_HINTS.search(question):
        return "DATA"
    if _KNOWLEDGE_HINTS.search(question) and not _DATA_HINTS.search(question):
        return "KNOWLEDGE"
    prompt = f"""Classify the question into exactly one label:
  DATA      - needs a number/count/list computed from the scheme database. This
              INCLUDES comparison and correlation questions ("do districts with
              high X also have high Y", "is A related to B by district", "which
              districts lead on both schemes") — answered by querying the figures
              and comparing them, NOT from the reference docs.
  KNOWLEDGE - asks how a scheme works: eligibility, documents, components, rules, history

Return ONLY JSON: {{"intent": "DATA"|"KNOWLEDGE"}}

Question: "{question}"
JSON:"""
    raw = await llm.call_classifier(prompt, guided={"guided_json": _INTENT_JSON_SCHEMA})
    payload = _extract_json(raw)
    if payload and payload.get("intent") in ("DATA", "KNOWLEDGE"):
        return payload["intent"]
    logger.warning("classify_intent: unusable response %r — defaulting to DATA", raw[:120])
    return "DATA"


def _empty_data_fields() -> dict:
    # `data` / `sql_query` are the keys the frontend renderer reads; keep them
    # present (empty) on every route so the UI never sees `undefined`.
    return {"schemes": [], "resolved_entities": {}, "sql": None, "sql_query": None,
            "row_count": 0, "rows": [], "data": []}


def _denied(decision: "auth.AuthDecision", schemes: list[str], resolved: dict) -> dict:
    return {
        "route": "denied", "intent": "DATA", "confidence": "high",
        "answer": decision.reason, "denied": True, "denied_by": decision.check,
        **{**_empty_data_fields(), "schemes": schemes, "resolved_entities": resolved},
    }


def _out_of_scope_result(question: str, raw_question: str) -> dict:
    """Standard 'I'm Megh One AI …' reply for an OutOfScope raised mid-pipeline —
    shaped like the edge returns in _run_pipeline so the frontend renders it the
    same way as an off-topic hit caught up front."""
    hit = edge.out_of_scope()
    return {
        "route": "edge", "intent": "EDGE", "confidence": "high",
        "answer": hit["response"], "edge_type": hit["type"],
        "suggestions": hit.get("suggestions", []),
        "rewritten_question": question if question != raw_question else None,
        **_empty_data_fields(),
    }


async def _answer_data(question: str, scope: "auth.UserScope | None" = None,
                       skip_scope_clarify: bool = False) -> dict:
    # Ask which scheme before doing anything expensive, when the question could
    # honestly mean either one. Guessing here is worse than a one-tap follow-up.
    if _needs_scheme_clarification(question):
        raise _scheme_clarification(question)

    # Ask "top how many?" before spending model calls when the question wants a
    # ranked list over a dimension but never says how long.
    if _needs_topn_clarification(question):
        raise _topn_clarification(question)

    schemes = await classify_scheme(question)
    entity_result = await resolve_entities(question, schemes)  # raises ClarificationNeeded if ambiguous

    # Ask which district / block / village / year when an aggregate question
    # pins none of them — unless we're already resuming that very clarification
    # (a reply that still names no scope must not loop us back here).
    if not skip_scope_clarify and _needs_scope_clarification(question, entity_result["resolved"]):
        raise _scope_clarification(question)

    # Geography is settled but the year isn't: ask which financial year (one-tap
    # chips) rather than silently answering across every year. The chips resume
    # with a concrete year or "all financial years", so this can't loop.
    if _needs_year_clarification(question, schemes, entity_result["resolved"]):
        raise _year_clarification(question, schemes)

    sql = await generate_sql(question, schemes, entity_result)

    # Authorization — role/scope vs. what the query actually asks for. Runs on the
    # generated SQL so granularity (GROUP BY) and geography literals are visible.
    if scope is not None and settings.AUTH_ENABLED:
        decision = auth.authorize(scope, schemes=schemes,
                                  resolved_entities=entity_result["resolved"], sql=sql)
        if not decision.allow:
            logger.info("auth deny (%s) user=%s: %s", decision.check, scope.user_id, decision.reason)
            return _denied(decision, schemes, entity_result["resolved"])

    sql, rows = await execute_with_repair(question, schemes, entity_result, initial_sql=sql)

    # A number the question states as already-true ("the 1.71 L sanctioned
    # houses") is never part of the SQL — check it against the result so the
    # composer corrects a false premise instead of repeating it as fact.
    notes = list(entity_result.get("notes") or [])
    if settings.PREMISE_CHECK_ENABLED:
        try:
            notes.extend(premise_check.check_premises(question, rows))
        except Exception:  # noqa: BLE001
            logger.warning("premise check failed — continuing without it", exc_info=True)

    answer = await compose_response(question, sql, rows, notes=notes,
                                    entities=entity_result.get("display"),
                                    schemes=schemes)
    return {
        "route": "data",
        "intent": "DATA",
        "confidence": "high" if rows else "low",
        "schemes": schemes,
        "resolved_entities": entity_result["resolved"],
        "sql": sql,
        "sql_query": sql,
        "row_count": len(rows),
        "rows": rows[:20],
        "data": rows[:200],
        "answer": answer,
    }


# ── "What is EKH?" — spell out a district / block name or abbreviation ──────
# A bare "what is <term>", "<term> full form", "what does <term> stand for"
# where <term> is one of Meghalaya's 12 districts or its ~56 C&RD blocks. The
# KB has no glossary for these, and the edge whitelist bounces a lone "ekh" as
# off-topic — so both routes fail the user. Answer it straight from the
# entity-resolver catalogue instead. A question that also wants a figure
# ("person-days in EKH") carries a data cue and is left for the DATA path.
_DEFN_CUE_RE = re.compile(
    r"\b(what(?:'?s| is| are| was| does| do)?|whats|what do you (?:mean by|call)|"
    r"full[\s-]?form|full name|long form|short form|expand(?:ed)?|expansion|"
    r"meaning|abbreviat\w*|acronym|stands? for|stand for|define|definition)\b",
    re.IGNORECASE,
)
_DEFN_STRIP_RE = re.compile(
    r"\b(what(?:'?s| is| are| was| does| do)?|whats|what do you (?:mean by|call)|"
    r"the full form of|full[\s-]?form of|full name of|full form for|long form of|"
    r"short form of|full[\s-]?form|full name|long form|short form|"
    r"the meaning of|meaning of|abbreviat\w* of|abbreviat\w* for|"
    r"acronym for|acronym of|expansion of|expanded|expand|"
    r"definition of|define|tell me|please|does|do|stands? for|stand for|"
    r"means?|in full|name of|the name of|"
    r"districts?|distt|dist|blocks?|c&rd|cd|"
    r"in meghalaya|of meghalaya|meghalaya)\b",
    re.IGNORECASE,
)


def _format_geo_definition(hit: dict) -> str:
    name = hit["display"]
    acronym = str(hit.get("acronym") or "").strip()
    if hit["type"] == "district":
        lead = (f"“{acronym}” is short for {name}"
                if acronym and hit.get("used_abbrev") else name)
        hq = hit.get("hq")
        seat = f" (headquarters: {hq})" if hq else ""
        return (
            f"{lead} — one of the 12 districts of Meghalaya{seat}. "
            f"Ask for its MGNREGA or PMAY-G figures, e.g. "
            f"“PMAY-G houses completed in {name}” or "
            f"“MGNREGA person-days in {name} in 2023-24”."
        )
    parent = hit.get("district")
    where = f" in {parent} district" if parent else ""
    return (
        f"{name} is a C&RD (community & rural development) block{where} of "
        f"Meghalaya. Ask for its MGNREGA or PMAY-G figures, e.g. "
        f"“MGNREGA person-days in {name}” or "
        f"“PMAY-G houses sanctioned in {name}”."
    )


def _geo_definition_answer(question: str) -> "dict | None":
    """A direct answer for a 'spell out this district / block' question, or None
    to let normal routing handle it."""
    q = (question or "").strip()
    if not q or len(q.split()) > 10:
        return None
    if not _DEFN_CUE_RE.search(q):
        return None
    if _DATA_HINTS.search(q) or _AGGREGATE_CUE.search(q):
        return None
    core = _DEFN_STRIP_RE.sub(" ", q)
    core = re.sub(r"[^\w&./ -]+", " ", core)
    core = re.sub(r"\s+", " ", core).strip(" -.")
    if not core or len(core.split()) > 5:
        return None
    hit = lookup_geo_term(core)
    if not hit:
        return None
    logger.info("geo-definition shortcut: %r -> %s %r", question, hit["type"], hit["display"])
    return {
        "route": "knowledge", "intent": "RAG", "confidence": "high",
        "answer": _format_geo_definition(hit), "sources": [],
        **_empty_data_fields(),
    }


async def answer_question(question: str, session: "Session | None" = None,
                          scope: "auth.UserScope | None" = None) -> dict:
    """Public entry point. Runs the pipeline, then attaches deterministic
    'Next steps' suggestions to any data/knowledge answer."""
    raw_question = question
    result = await _run_pipeline(question, session=session, scope=scope)
    _attach_followups(result, result.get("rewritten_question") or raw_question)
    return result


def _attach_followups(result: dict, question: str) -> None:
    """Add `follow_up_options` (+ back-compat `follow_ups` / `follow_up`) to a
    data/knowledge result. Best-effort — a failure here never breaks the answer."""
    if not settings.FOLLOWUP_SUGGEST_ENABLED:
        return
    if result.get("route") not in ("data", "knowledge"):
        return
    try:
        opts = followups.build_followups(
            result["route"], question,
            result.get("schemes") or [], result.get("resolved_entities") or {},
            sql=result.get("sql"),
            rows=result.get("rows") or result.get("data"))
    except Exception:  # noqa: BLE001
        logger.warning("follow-up suggestion build failed", exc_info=True)
        return
    if opts:
        result["follow_up_options"] = opts
        result["follow_ups"] = [o["question"] for o in opts]
        result["follow_up"] = opts[0]["question"]


async def _run_pipeline(question: str, session: "Session | None" = None,
                        scope: "auth.UserScope | None" = None) -> dict:
    raw_question = question
    scope_resumed = False

    # 0a. Resuming a "which area / year?" pause — fold the user's free-text reply
    #     ("West Garo Hills 2023-24", "all of Meghalaya, all years") back into the
    #     question that triggered it. A reply that stands on its own as a fresh
    #     question, or an edge case like "thanks", is left alone; either way the
    #     pending state is consumed so it never leaks into a later turn.
    pending = getattr(session, "pending_scope_q", None) if session is not None else None
    if pending:
        session.pending_scope_q = None
        # A scope-pause reply is normally a bare fragment ("Ri Bhoi, 2023-24",
        # "wgh, 1999-20") with no scheme vocabulary of its own, so the edge
        # whitelist would tag it "off_topic" every time — do NOT use that as the
        # signal to drop the pause. Only a clearly conversational reply (a
        # greeting / thanks / goodbye / abuse) or a fresh standalone question
        # abandons it; everything else is merged back into the paused question.
        _edge_hit = edge.detect_edge_case(question)
        # "off_topic" is the whitelist gate mis-firing on a fragment with no
        # scheme words — NOT a reason to drop the pause. Any other edge verdict
        # (greeting / thanks / goodbye / abuse / "never mind") genuinely is.
        _bailed = bool(_edge_hit) and _edge_hit.get("type") != "off_topic"
        # A one-tap chip (year pause) sends the whole rewritten question, which
        # already begins with the paused stem — merging would just duplicate it
        # ("total expenditure for MGNREGA, total expenditure for MGNREGA for FY
        # 2023-24"). Detect that and pass the chip's question straight through.
        _stem = pending.rstrip(" ?.").lower()
        _is_chip_resume = question.strip().lower().startswith(_stem)
        if _is_chip_resume:
            scope_resumed = True
            logger.info("scope/year clarification resumed via full question -> %r", question)
        elif not _bailed and not _reply_abandons_scope_pause(question):
            question = f"{pending.rstrip(' ?.')}, {question.strip()}"
            scope_resumed = True
            logger.info("scope clarification resumed -> %r", question)

    # 0-. "What is EKH?" / "MYLLIEM full form" — spell out a district or block
    #     straight from the resolver catalogue. Must run BEFORE the edge layer
    #     (which bounces a lone abbreviation as off-topic) and before routing
    #     (KNOWLEDGE / RAG has no glossary for these). Skipped on a scope-pause
    #     resume — that text is a merged fragment, never a definition request.
    if not scope_resumed:
        geo_def = _geo_definition_answer(question)
        if geo_def:
            if question != raw_question:
                geo_def["rewritten_question"] = question
            return geo_def

    # 0. Edge — greetings, identity, thanks, off-topic, abuse. Checked on the RAW
    #    text first: the follow-up heuristic treats any short anchorless phrase as
    #    a fragment, which would otherwise turn "hello" into a bogus follow-up.
    #    Skipped when we just merged a scope-pause reply: the merged text is a
    #    bare "<question>, <place>, <year>" fragment that the whitelist gate
    #    would wrongly flag as off-topic, and the conversational-reply case was
    #    already handled at the merge above.
    hit = None if scope_resumed else edge.detect_edge_case(question)
    if hit:
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": hit["response"], "edge_type": hit["type"],
                "suggestions": hit.get("suggestions", []),
                **_empty_data_fields()}

    # 1. Follow-up — rewrite a fragment ("what about EGH?") to a standalone
    #    question using the previous turn, before routing. Only when the previous
    #    turn was an actual scheme answer; otherwise the fragment has nothing
    #    coherent to attach to.
    prev = session.last_turn if session is not None else None
    has_antecedent = prev is not None and prev.route in _ANTECEDENT_ROUTES
    if looks_like_followup(question):
        if has_antecedent:
            question = await rewrite_followup(question, prev)
        elif _CONTEXTLESS_REF.search(question) and not _mentions_scheme(question):
            # "how launched it?" with no prior scheme answer — don't guess.
            return {"route": "edge", "intent": "EDGE", "confidence": "high",
                    "edge_type": "confused",
                    "answer": ("I don't have an earlier answer to build on, so I'm not "
                               "sure what that refers to. Tell me the scheme (MGNREGA or "
                               "PMAY-G) and what you'd like to know."),
                    "suggestions": list(edge.STARTERS),
                    **_empty_data_fields()}

    # 1b. Re-check edge on the rewrite (cheap, and the rewrite can surface one).
    #     Same scope-resume carve-out as step 0.
    hit = None if scope_resumed else edge.detect_edge_case(question)
    if hit:
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": hit["response"], "edge_type": hit["type"],
                "suggestions": hit.get("suggestions", []),
                "rewritten_question": question if question != raw_question else None,
                **_empty_data_fields()}

    # 2. Route: number question or scheme-rules question?
    intent = await classify_intent(question)

    # 3. KNOWLEDGE -> RAG over the scheme reference docs.
    if intent == "KNOWLEDGE":
        kb = await rag.answer_from_kb(question)
        base = {"rewritten_question": question if question != raw_question else None}
        if kb:
            return {"route": "knowledge", "intent": "RAG", "confidence": kb["confidence"],
                    "answer": kb["answer"], "sources": kb["sources"], **base, **_empty_data_fields()}
        return {"route": "knowledge", "intent": "RAG", "confidence": "low", "sources": [],
                "answer": "That isn't covered in the MGNREGA / PMAY-G reference material I have.",
                **base, **_empty_data_fields()}

    # 4. DATA -> NL->SQL. On a hard failure, try the KB once before giving up.
    try:
        result = await _answer_data(question, scope=scope, skip_scope_clarify=scope_resumed)
        if question != raw_question:
            result["rewritten_question"] = question
        return result
    except ClarificationNeeded:
        raise
    except OutOfScope as e:
        logger.info("out of scope (%s) — returning Megh One AI scope reply", e)
        return _out_of_scope_result(question, raw_question)
    except (llm.ModelBusyError, asyncio.TimeoutError, httpx.TimeoutException,
            httpx.TransportError) as e:
        # The model gateway hiccuped (saturated slot, read timeout, connection
        # reset) part-way through the DATA path — this is NOT "the question can't
        # be answered from the data". Propagate it so the router returns a
        # 503/504 "busy, please retry" instead of the misleading "couldn't build
        # a working query" fallback below, which reads as if the question itself
        # were at fault.
        logger.warning("data path hit a transient gateway error (%s) — re-raising", e)
        raise
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code >= 500:
            logger.warning("data path hit gateway %s — re-raising", e.response.status_code)
            raise
        logger.warning("data path failed (%s) — trying KB fallback", e, exc_info=True)
        return await _data_path_kb_fallback(question)
    except Exception as e:  # noqa: BLE001
        logger.warning("data path failed (%s) — trying KB fallback", e, exc_info=True)
        return await _data_path_kb_fallback(question)


async def _data_path_kb_fallback(question: str) -> dict:
    """The DATA path genuinely couldn't produce a query (bad/uncoverable question,
    not an infra blip) — try the knowledge base once, then give the 'rephrase it'
    reply. Transient gateway errors are handled by the caller and never reach here."""
    kb = await rag.answer_from_kb(question)
    if kb:
        return {"route": "knowledge", "intent": "RAG", "confidence": kb["confidence"],
                "answer": kb["answer"], "sources": kb["sources"], **_empty_data_fields()}
    return {"route": "data", "intent": "DATA", "confidence": "low",
            "answer": ("I understood the question but couldn't build a working query for it "
                       "against the current MGNREGA / PMAY-G data. Try rephrasing it, or ask "
                       "for a simpler breakdown first (e.g. \"PMAY-G sanctions by district "
                       "for 2023\")."),
            **_empty_data_fields()}
