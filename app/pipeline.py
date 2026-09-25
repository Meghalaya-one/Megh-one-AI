"""
The whole NL -> SQL -> answer flow as one plain sequential function. No graph
framework: each step is an ordinary `await`, branches are ordinary `if`. This
is deliberately simpler than the LangGraph design in the GrantThornton
proposal — appropriate at 2 schemes and 20-40 concurrent users; revisit if
either grows a lot.
"""
import asyncio
import itertools
import json
import functools
import logging
import numbers
import re

import httpx
from rapidfuzz import fuzz

from app import annotations, auth, context_manager, edge, followups, llm, premise_check, prompt_builder, rag
from app.config import settings
from app.db import UnsafeSQLError, run_readonly
from app.entity_resolver import (
    all_districts,
    block_parent_district,
    canonical_names,
    collides_across_dimensions,
    collision_canonical_names,
    constituency_contents,
    detect_region,
    lookup_geo_term,
    resolve_cm_scheme,
    resolve_cm_scheme_group,
    resolve_cm_scheme_group_ambiguity,
    resolve_dimension,
    resolve_house_status,
    resolve_tranche_label,
    resolve_village,
    scan_dimension,
    tranche_labels,
    village_names_exact,
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
    # Focus Plus / CM Elevate metric nouns — a bare "<scheme> beneficiaries?" /
    # "disbursements?" with no "how many" in front of it used to have no strong
    # signal either way and fell through to the LLM classifier, which guessed
    # KNOWLEDGE for "focus + beneficiaries?" and returned the glossary answer
    # instead of a count (confirmed live 2026-09-11). These are count/amount
    # nouns, not process words, so adding them carries the same low
    # misroute risk as "person-days"/"expenditure" above.
    r"beneficiar\w*|disburs\w*|"
    # EXISTENCE / NAME-LOOKUP questions. "Is there any Producer Group named as
    # Sakania PG?" is a lookup against stored records (the SME use-case bank
    # lists it as answer_route: sql, TC-13) but carries NO counting word, so it
    # matched no DATA cue above and fell through to the LLM classifier, which
    # called it KNOWLEDGE and answered from the reference docs — "no mention of
    # a specific Producer Group named ... in the provided reference material",
    # true of the prose and irrelevant to the question (reported 2026-09-22).
    # The naming word is what keeps these narrow: "is there any eligibility
    # criteria" names nothing and is still KNOWLEDGE.
    r"\b(?:is|are)\s+there\s+(?:any|a|an)\b[^?]{0,60}?"
    r"\b(?:named|called|by the name of|with (?:the )?name)\b|"
    r"\bproducer[\s-]?groups?\s+(?:named|called)\b|"
    # Same lookup shape using the "PG" abbreviation, which is what users
    # actually type: "is there any pg group with name sakania?", "pg named X".
    # The optional "group" covers the redundant-but-common "pg group".
    r"\bpgs?(?:\s+group)?\s+(?:named|called|with (?:the )?name)\b|"
    r"\b(?:find|search for|look up|lookup)\s+(?:the\s+|a\s+)?producer[\s-]?group\b|"
    # "How did people apply to CM Elevate?" asks for the recorded
    # application_mode breakdown (online vs cmconnectcenter — a real, answerable
    # count), not the application PROCESS ("how do I apply", "how to apply",
    # already a _KNOWLEDGE_HINTS cue). Third-person/past-tense phrasing is the
    # reliable signal that separates the two; the LLM classifier alone picked
    # KNOWLEDGE here and gave a plausible-sounding but unverifiable portal
    # description instead of the real online/cmconnectcenter split (confirmed
    # live 2026-09-11, this exact CM Elevate few-shot question).
    r"how (?:did|do|does) (?:people|applicants|users|they|most people) apply\b|"
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

# "What is the gender split of CM Elevate applicants?" / "what's the workflow
# level breakdown?" both open with the strong _KNOWLEDGE_HINTS cue "what is",
# which used to short-circuit straight to KNOWLEDGE before classify_intent's
# LLM call ever ran. Adding "split"/"breakdown"/"distribution" into _DATA_HINTS
# only removed that false shortcut — it still left the LLM to arbitrate, and it
# guessed KNOWLEDGE for "workflow level breakdown" too (confirmed live
# 2026-09-11: CM Elevate's own headline finding, "female-majority", and its
# online/cmconnectcenter split were both unreachable this way). A "breakdown /
# split / distribution" noun names a computed grouping over real records by
# construction — there is no scheme-mechanics reading of it — so it can force
# DATA outright, the same way _CROSS_SCHEME_SET_QUESTION does below.
_BREAKDOWN_CUE = re.compile(r"\b(split|breakdown|distribution)\b", re.IGNORECASE)

# Same shape of bug as _BREAKDOWN_CUE above, different trigger word: "What is
# the average amount disbursed per beneficiary?" / "What is the total
# disbursed in West Garo Hills?" open with "what is" (a _KNOWLEDGE_HINTS cue)
# AND also match "average"/"total" (a _DATA_HINTS cue), so neither fast-path
# branch fires and it falls to the LLM classifier — which guessed KNOWLEDGE
# for a plain average-per-beneficiary question live (confirmed 2026-09-12: a
# Focus Plus "average disbursed per beneficiary" question got answered from
# the reference docs with a flat ₹5,000 rate instead of the real computed
# average). "What is the average/total/sum/count/number of X" is a computed
# aggregate by construction, never scheme-mechanics — force DATA the same way
# _BREAKDOWN_CUE does.
_METRIC_WHATIS_CUE = re.compile(
    r"\bwhat (?:is|was|are|were)\b.{0,40}\b(average|avg|mean|total|sum|number|count)\b",
    re.IGNORECASE,
)

# The opposite collision: programme-design questions that happen to contain a
# DATA cue. "Who are the intended beneficiaries of CM-ELEVATE?" trips
# "beneficiar\w*", and "What is the target number of entrepreneurs under
# CM-ELEVATE?" trips _METRIC_WHATIS_CUE ("what is … number"), so both went down
# the data path — one answered "the data doesn't cover the intended
# beneficiaries", the other asked "which area / year?" for a policy target and
# never stated it (CM Elevate Legacy use-case QA, TC-03 / TC-08, 2026-09-25).
# Who a scheme is FOR and what it AIMS at are design facts in the reference
# docs, never a computed figure — the qualifiers below are what keep this
# narrow ("how many beneficiaries" and "beneficiaries by district" stay DATA).
_PROGRAMME_DESIGN_CUE = re.compile(
    r"\b(?:intended|target(?:ed)?|eligible)\s+(?:beneficiar\w*|groups?|entrepreneurs?)\b|"
    r"\btarget(?:ed)?\s+(?:number|figure|count)\s+of\b|"
    r"\b(?:entrepreneur|employment|job|outreach)[\s-]+(?:reach\s+)?targets?\b",
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
    `rule` (optional) is a short tag for why we paused (shown in the UI).
    `village_hint` (optional) is the exact village-name text the user typed,
    set only for a village-ambiguity pause — see Session.pending_village_hint
    for why a free-text resume needs it re-supplied deterministically rather
    than trusting the LLM mention-extractor to re-tag it in the merged text."""

    def __init__(self, question: str, *, options: list[dict] | None = None,
                 rule: str | None = None, village_hint: str | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []
        self.rule = rule
        self.village_hint = village_hint


# ── Follow-up ("what about East Garo Hills?") rewriting ──────────────────────
# A cheap heuristic gates the one extra model call: only questions that read
# like a fragment referring back to the previous turn get rewritten.
_FOLLOWUP_LEAD = re.compile(
    r"^\s*(what about|how about|and |what of |and for |also |ok(ay)? and |"
    r"same for |what if |now |then )", re.IGNORECASE,
)
_FOLLOWUP_PRONOUN = re.compile(
    r"\b(it|its|that|those|these|them|they|there|this(?:\s+one)?|the same|same one)\b", re.IGNORECASE,
)
# Something that anchors the question on its own — if present, it's not a fragment.
# Deliberately excludes generic topic words like "documents?", "eligibility?",
# "what is", "who is", "explain": on their own (esp. as a short bare fragment)
# these don't name a scheme or a specific metric, so right after a scheme
# answer "Documents?" / "Eligibility?" reads as a follow-up about THAT scheme,
# not a fresh self-contained question — they used to trip this anchor and get
# judged standalone, which silently dropped the active scheme context.
# "scheme" IS kept as an anchor (unlike those): a fresh, contextless question
# like "Is there any government scheme that can help my family?" uses "that"
# as an ordinary relative pronoun, not a reference to a prior turn, but
# _CONTEXTLESS_REF can't tell the difference — without "scheme" anchoring it,
# this read as a fragment needing prior context and got the "I don't have an
# earlier answer to build on" reply instead of being answered directly (TC-008).
_STANDALONE_ANCHOR = re.compile(
    r"\b(mgnrega|mnrega|nrega|pmay|awaas|person[\s-]?days?|expenditure|houses?|"
    r"job cards?|wages?|sanction|how many|total|list|scheme)\b", re.IGNORECASE,
)
# A bare scheme-name fragment — "in MGNREGA?", "for PMAY-G", "and Focus Plus?",
# "what about MGNREGA" — is the canonical scheme-swap follow-up: it reuses the
# whole previous question and only changes the scheme. It trips _STANDALONE_ANCHOR
# (the scheme names are listed there), so without this it is mistaken for a
# self-contained question and the router reads it as "tell me about MGNREGA".
_SCHEME_SWAP_FOLLOWUP = re.compile(
    r"^\s*(?:in|for|and|also|now|then|what about|how about|what of|same for|"
    r"switch to|change to|with)?\s*"
    r"(?:mgnrega|mnrega|nrega|pmay[\s-]?g?|awaas?|awas|"
    r"focus[\s-]?plus|focus\s*\+|focusplus|"
    # Focus Legacy, and a BARE "focus" — which names neither Focus scheme, so
    # _scheme_swap_rewrite substitutes the word "Focus" and the normal "which
    # Focus?" pause asks. Without these, "for focus" after a CM Elevate answer
    # went to the LLM rewrite, which kept CM Elevate and re-answered it.
    r"focus[\s-]?legacy|focuslegacy|focus|"
    r"cm[\s-]?elevate(?:[\s-]?legacy)?|cmelevate(?:[\s-]?legacy)?)"
    r"\s*(?:instead|now|then|scheme)?\s*[?.!]*\s*$",
    re.IGNORECASE,
)
# "same for the remaining schemes" / "give same like for other schemes" / "what
# about the rest of the schemes" — the previous question, asked of every scheme
# it did NOT name. "all schemes" asks it of every scheme. Left to the LLM
# rewrite, "remaining schemes" after a CM Elevate answer became "the remaining
# CM Elevate schemes" (its sub-schemes) and the same answer came back again.
_REST_OF_SCHEMES = re.compile(
    r"\b(?:remaining|other|rest\s+of\s+(?:the\s+)?)\s*schemes?\b|"
    r"\ball\s+(?:the\s+)?other\s+schemes?\b|\ball\s+(?:the\s+)?(?:others|remaining)\b",
    re.IGNORECASE,
)
_ALL_SCHEMES_FOLLOWUP = re.compile(r"\ball\s+(?:the\s+)?(?:\w+\s+)?schemes\b", re.IGNORECASE)


# A follow-up fragment ("and for 2024-25?", "how launched it?") only means
# something against a real scheme answer. Rewriting it against a greeting,
# off-topic reply, clarification pause or a denial produces a confident bogus
# query — e.g. "how launched it?" right after "what is elon musk?" was being
# turned into a data question. So the previous turn must be one of these.
_ANTECEDENT_ROUTES = ("data", "knowledge")
# An explicit pointer back to the previous turn's area inside a name lookup.
_NAME_LOOKUP_BACKREF = re.compile(
    r"\b(?:in|within|from|of|under)\s+(?:that|this|the\s+same)\s+"
    r"(?:block|district|village|area|constituency|place)\b|"
    r"\b(?:in\s+)?there\s*[?.!]*\s*$|\bsame\s+(?:block|district|village|area)\b",
    re.IGNORECASE)
_CONTEXTLESS_REF = re.compile(r"\b(it|its|that|those|these|them|they|this(?:\s+one)?)\b", re.IGNORECASE)


def _mentions_scheme(question: str) -> bool:
    return any(p.search(question or "") for p in _SCHEME_NAME_PATTERN.values())


def looks_like_followup(question: str) -> bool:
    q = question.strip()
    if len(q.split()) > 12:
        return False
    # "in MGNREGA?" / "for PMAY-G" / "and Focus Plus?" — a scheme-swap fragment.
    # Checked before _STANDALONE_ANCHOR, which would otherwise veto it because it
    # names a scheme.
    if _SCHEME_SWAP_FOLLOWUP.match(q):
        return True
    # "same for the remaining schemes" — names no scheme of its own, so it
    # only means something against the previous question.
    if _REST_OF_SCHEMES.search(q) and not _mentions_scheme(q):
        return True
    # "pick any scheme and explain" — its answer depends on what THIS
    # conversation has already covered, so it must never be served from the
    # shared response cache (which skips follow-ups).
    if _is_scheme_pick_request(q):
        return True
    # A recommendation may draw the user's profile from earlier turns, and a
    # "why did you choose X?" is about the previous answer — both depend on
    # this conversation, so neither may be served from the shared cache.
    if _WHY_CHOICE.search(q) or _is_recommendation_request(q):
        return True
    if _FOLLOWUP_LEAD.search(q):
        return True
    # A question that names its own scheme outright is self-anchoring, same as
    # _STANDALONE_ANCHOR's "scheme"/"mgnrega"/"pmay" entries — but that regex
    # predates Focus Plus/CM Elevate and was never extended to them, so "What
    # benefits will I get under this Focus Plus" tripped _FOLLOWUP_PRONOUN on
    # "this", found no anchor, and got rewritten against the PREVIOUS turn's
    # scheme (e.g. "...compared to PMAY-G?") even though it names its own
    # scheme in full. _mentions_scheme covers all four schemes without having
    # to keep two scheme-name lists in sync.
    if _mentions_scheme(q):
        return False
    # "is there any producer group named Sakania PG?" — a NAME LOOKUP is
    # self-contained: a group name identifies the group statewide. The "there"
    # of "is there" tripped _FOLLOWUP_PRONOUN, the rewrite glued on the previous
    # turn's place ("...named Sakania PG in Betasing block"), and the lookup
    # answered a false "no such group" for a group that exists in East Khasi
    # Hills (reported live 2026-09-25). Only an explicit pointer back ("in that
    # block", "...named X there?") keeps it a follow-up.
    if (_PG_NAMED_ENTITY.search(q) or _GROUP_NAME_LOOKUP.search(q)) \
            and not _NAME_LOOKUP_BACKREF.search(q):
        return False
    if _FOLLOWUP_PRONOUN.search(q) and not _STANDALONE_ANCHOR.search(q):
        return True
    # A bare fragment ("in East Garo Hills", "by block") with no anchor of its own.
    return len(q.split()) <= 6 and not _STANDALONE_ANCHOR.search(q)


def _scheme_swap_rewrite(prev_question: str, followup: str) -> "str | None":
    """Deterministic rewrite for a bare scheme-swap follow-up ("in MGNREGA?",
    "what about PMAY-G?"). Substitutes the follow-up's scheme for the one named
    in the previous question, so the intent ("same question, other scheme") is
    preserved exactly. The LLM rewrite tends to *append* the new scheme instead
    of replacing the old one ("... in Focus Plus ... in MGNREGA?"), which then
    reads as a cross-scheme question. Returns None if the fragment names no
    scheme or the previous question can't be adapted — the caller falls back to
    the LLM path."""
    if not _SCHEME_SWAP_FOLLOWUP.match((followup or "").strip()):
        return None
    target = next((name for name, pat in _SCHEME_NAME_PATTERN.items()
                   if pat.search(followup)), None)
    # A bare "focus" names neither Focus scheme: carry the word itself into the
    # question, so the "which Focus?" pause (_is_ambiguous_focus) asks rather
    # than this rewrite guessing one.
    label = target or ("Focus" if _BARE_FOCUS_WORD.search(followup or "") else None)
    if not label or not prev_question:
        return None
    out = prev_question
    replaced = False
    for name, pat in _SCHEME_NAME_PATTERN.items():
        if name == target:
            continue
        if pat.search(out):
            out = pat.sub(label, out)
            replaced = True
    if not replaced:
        if target and _SCHEME_NAME_PATTERN[target].search(out):
            return out  # previous question was already about the target scheme
        out = f"{out.rstrip(' ?.')} for {label}"
    out = re.sub(r"\s+,", ",", out).strip()
    logger.info("follow-up scheme-swap: %r + %r -> %r", prev_question, followup, out)
    return out


def _rest_of_schemes_rewrite(prev: "object", followup: str) -> "str | None":
    """Deterministic rewrite for "same for the remaining / other / all schemes":
    the previous question with its scheme replaced by the list of schemes it
    asks about. None when the follow-up is not that shape, names a scheme
    itself, or the previous question named no scheme to swap out.

    On a KNOWLEDGE antecedent, schemes that share one knowledge base
    (rag.kb_scheme — CM Elevate and CM Elevate Legacy) count once: after a CM
    Elevate "how to apply", CM Elevate Legacy is not a "remaining" scheme with
    different material, and listing it would repeat the same answer."""
    f = (followup or "").strip()
    if not f or len(f.split()) > 12 or _mentions_scheme(f):
        return None
    rest = bool(_REST_OF_SCHEMES.search(f))
    every = not rest and bool(_ALL_SCHEMES_FOLLOWUP.search(f))
    if not (rest or every):
        return None
    prev_q = getattr(prev, "question", "") or ""
    prev_named = [s for s, pat in _SCHEME_NAME_PATTERN.items() if pat.search(prev_q)]
    if not prev_named:
        return None
    knowledge = getattr(prev, "route", "") == "knowledge"
    key = rag.kb_scheme if knowledge else (lambda s: s)
    done = {key(s) for s in prev_named} if rest else set()
    targets: list[str] = []
    for s in SCHEME_CATALOG:
        if key(s) in done:
            continue
        done.add(key(s))
        targets.append(s)
    if not targets:
        return None
    names = targets[0] if len(targets) == 1 else ", ".join(targets[:-1]) + " and " + targets[-1]
    # Mark every old scheme mention first, then put the list in at the first
    # mark only — substituting the list directly would let a later pattern
    # match a name INSIDE the list just inserted ("CM Elevate") and delete it.
    out = prev_q
    for s in prev_named:
        out = _SCHEME_NAME_PATTERN[s].sub("\x00", out)
    out = out.replace("\x00", names, 1).replace("\x00", "")
    out = re.sub(r"\s+([,?.])", r"\1", re.sub(r"\s{2,}", " ", out)).strip()
    logger.info("follow-up rest-of-schemes: %r + %r -> %r", prev_q, followup, out)
    return out


async def rewrite_followup(question: str, prev: "object", extra_context: str = "") -> str:
    """Turn a fragment into a standalone question using the previous turn.
    Falls back to the original question on any failure — never raises.

    `extra_context` (optional): the context layer's token-budgeted structured
    state / summary / relevant-older-turns block (see
    context_manager.build_followup_context) — spliced into the prompt ahead
    of the previous turn, for a conversation where "the previous turn" alone
    has lost the thread (e.g. a KNOWLEDGE digression sits between the DATA
    answer being followed up on and this fragment). Blank by default, so
    every existing caller is unaffected."""
    if not settings.FOLLOWUP_REWRITE_ENABLED or prev is None:
        return question
    rest = _rest_of_schemes_rewrite(prev, question)
    if rest:
        return rest
    swap = _scheme_swap_rewrite(getattr(prev, "question", "") or "", question)
    if swap:
        return swap
    try:
        prompt = (
            "Rewrite the FOLLOW-UP as a complete, standalone question by reusing "
            "context from the PREVIOUS question. Keep the user's intent; change only "
            "what the follow-up changes (e.g. a different district, year, or metric). "
            "Do not introduce any district, year, tranche, scheme, category or other "
            "filter that is not present in the PREVIOUS question, the PREVIOUS answer, "
            "the Known context below, or the FOLLOW-UP itself — when in doubt, leave it "
            "out rather than guess one. "
            "Return ONLY the rewritten question, nothing else.\n\n"
            + (f"{extra_context}\n\n" if extra_context else "")
            + f'PREVIOUS question: "{prev.question}"\n'
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
        "assembly_constituency": {"type": "string"},
        # Plural — ONLY when the question names two-or-more blocks to compare
        # against each other ("compare X and Y"). Kept separate from "block"
        # rather than making "block" a string-or-array union (some xgrammar
        # builds reject union types, per the note above).
        "blocks": {"type": "array", "items": {"type": "string"}},
        # Same idea, for districts ("compare X and Y between district A and B").
        "districts": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_INTENT_JSON_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["DATA", "KNOWLEDGE"]}},
    "required": ["intent"],
}
# No `required: ["issue"]` — the verifier only needs to emit "issue" when
# ok is false; a passing query should cost the fewest tokens possible.
_SQL_VERIFY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "issue": {"type": "string"},
    },
    "required": ["ok"],
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
    "Focus Plus": re.compile(r"\bfocus[\s-]?plus\b|\bfocus\s*\+|\bfocusplus\b", re.IGNORECASE),
    # The lookarounds keep "CM Elevate Legacy" / "CM Elevate Disbursement" /
    # "Legacy CM Elevate" from ALSO reading as the applications dataset — that is
    # the other scheme below, and matching both turned a single-scheme question
    # into a two-scheme comparison.
    "CM Elevate": re.compile(
        r"(?<!legacy )(?<!legacy-)\bcm[\s-]?elevate\b(?![\s-]*(?:legacy|disbursements?)\b)|"
        r"(?<!legacy )(?<!legacy-)\bcmelevate\b(?![\s-]*(?:legacy|disbursements?)\b)",
        re.IGNORECASE),
    # CM Elevate Legacy is the SANCTION-AND-DISBURSEMENT dataset (DB name: CM
    # Elevate Disbursement), NOT the CM Elevate applications dataset. The two share
    # the name and no key (cmelevatelegacy_entity_resolver.yaml
    # scheme.disambiguation). Only a qualified form names it here; a bare "CM
    # Elevate" that carries money / year / lender vocabulary is re-pointed to it
    # by _pin_cm_elevate_dataset() before routing.
    "CM Elevate Legacy": re.compile(
        r"\bcm[\s-]?elevate[\s-]*(?:legacy|disbursements?)\b|"
        r"\bcmelevate[\s-]*(?:legacy|disbursements?)\b|"
        r"\blegacy[\s-]+cm[\s-]?elevate\b|\belevate[\s-]?legacy\b",
        re.IGNORECASE),
    # Focus Legacy is the producer-group scheme, NOT Focus Plus. The two share the
    # word "Focus" and nothing else (no shared key; Focus Plus holds no
    # producer-group column at all — focuslegacy_entity_resolver.yaml
    # scheme.disambiguation). Both patterns demand a qualifier, so a BARE "focus"
    # matches neither and falls through to the "which Focus?" ask below.
    "Focus Legacy": re.compile(
        r"\bfocus[\s-]?legacy\b|\bfocuslegacy\b|\blegacy[\s-]?focus\b|"
        r"\bold[\s-]?focus\b|\bfocus[\s-]?pg\b|\bpg[\s-]?focus\b",
        re.IGNORECASE),
}
# Fuzzy fallback for a scheme name the exact regex above misses because it's
# misspelled ("manrega", "pamay") — mirrors the RapidFuzz tolerance
# entity_resolver.py already gives district/block names. Kept separate from
# _SCHEME_NAME_PATTERN (exact match stays the fast, zero-false-positive path;
# this only runs when nothing named matches outright).
_SCHEME_FUZZY_ALIASES = {
    "MGNREGA": ["mgnrega", "mnrega", "nrega"],
    "PMAY-G": ["pmay", "pmayg", "awaas", "awas"],
    # NOTE: the bare "focus" alias used to live here, so a typo of the short form
    # ("facus", "fokus") still routed to Focus Plus. It was REMOVED when Focus
    # Legacy landed: with two live Focus schemes a bare or misspelled "focus"
    # identifies neither, and guessing Focus Plus would answer a producer-group
    # question from a partition that holds no producer groups at all. Such a
    # question now reaches _focus_ambiguity_clarification() instead.
    "Focus Plus": ["focusplus"],
    "CM Elevate": ["cmelevate"],
    "Focus Legacy": ["focuslegacy"],
    "CM Elevate Legacy": ["cmelevatelegacy"],
}
_FUZZY_SCHEME_ACCEPT = 80


def _fuzzy_named_schemes(question: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", question or "") if len(w) >= 5]
    if not words:
        return []
    hits: list[str] = []
    for scheme, aliases in _SCHEME_FUZZY_ALIASES.items():
        if any(fuzz.ratio(w, a) >= _FUZZY_SCHEME_ACCEPT for w in words for a in aliases):
            hits.append(scheme)
    return hits


_SCHEME_CANONICAL_SPELLING = {
    "MGNREGA": "MGNREGA", "PMAY-G": "PMAY-G",
    "Focus Plus": "Focus Plus", "CM Elevate": "CM Elevate",
    "Focus Legacy": "Focus Legacy", "CM Elevate Legacy": "CM Elevate Legacy",
}


def _correct_scheme_spelling(question: str) -> str:
    """Fix an obviously misspelled scheme name in the question text itself
    ("manrega" -> "MGNREGA", "pamay" -> "PMAY-G") before routing. Without this,
    a typo'd scheme name only ever gets caught on the DATA path (via
    _fuzzy_named_schemes) — the KNOWLEDGE/RAG path embeds the raw text
    verbatim, so the typo silently tanks vector-search relevance against the
    scheme's own reference docs and the question comes back "not covered".
    Conservative by construction: a word only gets corrected when it does NOT
    already exactly match a scheme alias (nothing to fix) and DOES fuzzy-match
    one closely enough — so it can't mangle an unrelated word."""
    if not question:
        return question

    # A scheme already named correctly elsewhere in the question (exact match,
    # e.g. "CM ELEVATE") must not also be "corrected" word-by-word below — the
    # lone word "Elevate" fuzzy-matches the "cmelevate" alias on its own
    # (fuzz.ratio("elevate", "cmelevate") ~= 87.5, over the 80 threshold), so
    # without this guard "CM ELEVATE" gets the already-present "CM" duplicated
    # into "CM CM Elevate" (and worse on a second pass, e.g. a resumed
    # clarification chip, into "CM CM CM Elevate").
    _already_named = {s for s, pat in _SCHEME_NAME_PATTERN.items() if pat.search(question)}
    # "CM Elevate Legacy" already contains the "CM Elevate" words, but the CM
    # Elevate pattern deliberately does not match it — so without this the lone
    # word "Elevate" would be "corrected" into "CM CM Elevate Legacy".
    if "CM Elevate Legacy" in _already_named:
        _already_named.add("CM Elevate")

    def _sub(m: "re.Match") -> str:
        word = m.group(0)
        if len(word) < 5:
            return word
        wl = word.lower()
        for scheme, aliases in _SCHEME_FUZZY_ALIASES.items():
            if scheme in _already_named:
                continue
            if wl in aliases:
                return word
            if any(fuzz.ratio(wl, a) >= _FUZZY_SCHEME_ACCEPT for a in aliases):
                return _SCHEME_CANONICAL_SPELLING[scheme]
        return word

    return re.sub(r"[A-Za-z]+", _sub, question)


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
    # "households/persons [were/are/have been] employed" — the naive adjacent
    # phrase ("households? employed") missed the common "persons WERE employed"
    # wording (TC-024: a plain past-tense employment question with no scheme
    # named kept falling through to the scheme-clarification prompt).
    r"households?\s+(?:were\s+|are\s+|have\s+been\s+|has\s+been\s+)?employed|"
    r"persons?\s+(?:were\s+|are\s+|have\s+been\s+|has\s+been\s+)?employed|"
    # "received employment" is as common a phrasing as "were employed" — same
    # gap as above, just a different verb (DATA-004 used exactly this).
    r"(?:households?|persons?)\s+receiv\w*\s+employment|"
    # "women [were/are] provided/given employment" / "women employed" — same
    # missing-verb-phrasing gap as above (DATA-008 used exactly this; there
    # was no women-employment pattern here at all before, only in edge.py's
    # unrelated domain whitelist).
    r"women\s+(?:were\s+|are\s+)?(?:provided|given)\s+employment|"
    r"women\s+employ\w*|"
    # assembly_constituency data exists ONLY in mgnrega_employment (see
    # entity_resolver's assembly_constituency handling) — naming a constituency
    # pins the scheme on its own even with no other MGNREGA word present.
    r"assembly constituenc\w*|\bAC\s*\d+\b|"
    # unskilled/semi-skilled wage(s) — "wage" without the "s?" only matched the
    # singular; "unskilled wages" (the common plural phrasing, DATA-010) never
    # matched because \b after "wage" can't land inside "wages".
    r"unskilled wages?|semi[\s-]?skilled|"
    r"labour budget|works? demanded|wage employment|employment guarantee)\b",
    re.IGNORECASE,
)
_PMAY_ONLY_TERMS = re.compile(
    r"\b(house|houses|housing|dwelling units?|pucca house|kutcha house|"
    r"sanctioned houses?|instal{1,2}ments?|geotag|"
    # "tranche"/"tranch" is deliberately NOT here, even though PMAY-G also has
    # an installments_paid column. Focus Plus's own vocabulary IS "tranche"
    # (its stored column is literally tranche_label); PMAY-G's own vocabulary
    # is "installment" (instal?ments? above already covers it). A bare
    # "tranche 1 vs tranche 2" with no scheme named should default to Focus
    # Plus, not pause — see _FOCUSPLUS_ONLY_TERMS below, which is where
    # "tranche" is claimed.
    r"completion certificate|house status|awaas|awas|"
    # "sanctioned"/"released"/"pending" amount phrasing is PMAY-specific — MGNREGA
    # never "sanctions" anything (it has expenditure), CM Elevate has no money
    # column at all, and Focus Plus vocabulary is "disbursement"/"payment", not
    # "sanctioned". Without these, a bare "how much sanctioned amount has been
    # released in [village]?" (no scheme named) fell through to an unnecessary
    # "which scheme?" pause instead of being understood as PMAY-G.
    # Plural "amounts"/"numbers" ('s?') — the singular-only forms below missed
    # "sanctioned amountS" (PMAY-OFF-023) and "amount released" is fine as a
    # fixed phrase, but sanction NUMBER(S) (PMAY-OFF-026) had no pattern at all.
    r"sanctioned amounts?|amounts? (?:released|sanctioned|pending)|"
    r"released amounts?|pending amounts?|sanction dates?|sanction numbers?|"
    # "received (any/the/no) amount", "received full/partial amount" — the
    # release-status phrasing PMAY-OFF-008/009/010 use, distinct from the
    # "amount released" form already covered above.
    r"receiv\w*\s+(?:any|the|no|full|partial)?\s*(?:sanctioned\s+)?amount)\b",
    re.IGNORECASE,
)
# Focus Plus is a Meghalaya STATE farmer cash-benefit scheme. These terms name it or
# its scheme-specific machinery and belong to no other scheme. "tranche"/"tranch" is
# claimed HERE, not left shared with PMAY-G: it's Focus Plus's actual vocabulary (the
# stored column is tranche_label), while PMAY-G's own word for the same idea is
# "installment" (see _PMAY_ONLY_TERMS). "batch"/"disbursement" are still left out —
# those really are generic enough that an unnamed question using only those still
# asks "which scheme?".
_FOCUSPLUS_ONLY_TERMS = re.compile(
    r"\bfocus[\s-]?plus\b|\bfocus\s*\+|\bfocusplus\b|"
    r"\btranche?s?\b|"
    # "producer group" is NOT here — see _FOCUSLEGACY_ONLY_TERMS. It belongs to
    # Focus Legacy, whose grain IS the producer group; Focus Plus holds no such
    # column and focusplus_classification_rules.yaml refuses the question
    # outright (condition producer_group_requested).
    r"\bmeghalayaone\b|\bmbda\b|\bmeghalaya basin development\b|"
    r"\bfocus\+?\s*card\b|\b93k\b|\b12\.5k\b",
    re.IGNORECASE,
)
# CM Elevate covers 15 sub-schemes under one programme. These terms name the
# programme itself or its scheme-specific vocabulary that belongs to no other
# scheme. "on hold" / "verification" / "application mode" are deliberately left
# out — they read as generic status words, so an unnamed question using only
# those still asks "which scheme?".
_CMELEVATE_ONLY_TERMS = re.compile(
    r"\bcm[\s-]?elevate\b(?![\s-]*(?:legacy|disbursements?)\b)|"
    r"\bcmelevate\b(?![\s-]*(?:legacy|disbursements?)\b)|"
    r"\bpiggery\b|\bpoultry\b|\bgoat farming\b|\bwarehouse scheme\b|"
    r"\bsericulture\b|\bmotorcaravan\b|\bagro tourism villa\b|"
    r"\bprime small enterprise\b|\bprime tourism vehicle\b|"
    r"\bprime agriculture response vehicle\b|\bgreen taxi\b|"
    r"\bcinema theatre scheme\b|\bsports and wellness centre\b|"
    r"\bany business venture\b|\brequest_?id\b|"
    # Missing sub-scheme/commodity words and the scheme-FAMILY phrases
    # (vehicle/tourism/PRIME/livestock/enterprise scheme(s)) — without these,
    # a question naming only this vocabulary (no literal "CM Elevate") fell
    # through to the generic 4-way "MGNREGA, PMAY-G, Focus Plus, or CM
    # Elevate?" pause instead of being recognized as CM Elevate at all, even
    # though resolve_cm_scheme's own alias catalogue (or, for the family
    # words, resolve_cm_scheme_group_ambiguity) can place it precisely
    # (confirmed live 2026-09-11: "gender split for the tourism vehicle
    # scheme" and "applications under the vehicle schemes" both asked the
    # top-level 4-scheme question despite being unambiguously CM Elevate).
    r"\bdairy\b|\bvehicle schemes?\b|\btourism vehicle\b|\btourism schemes?\b|"
    r"\bprime schemes?\b|\bprime family\b|\blivestock schemes?\b|"
    r"\benterprise schemes?\b",
    re.IGNORECASE,
)


# Focus Legacy is the legacy producer-group disbursement programme. Its grain IS
# the producer group, so producer-group vocabulary belongs here and nowhere else
# (Focus Plus has no such column at all). "pg"/"pgs" are claimed as whole words —
# the resolver's own disambiguation rule names them as forcing the Focus Legacy
# reading — but "member"/"disbursement"/"payment"/"amount" are deliberately left
# out: they are shared vocabulary, so an unnamed question using only those still
# asks "which scheme?".
_FOCUSLEGACY_ONLY_TERMS = re.compile(
    r"\bfocus[\s-]?legacy\b|\bfocuslegacy\b|\blegacy[\s-]?focus\b|\bold[\s-]?focus\b|"
    r"\bproducer[\s-]?groups?\b|\bpgs?\b|\bpg[\s-]?ids?\b|"
    r"\bpg[\s-]?members?\b|\bpg[\s-]?names?\b|\bpg[\s-]?scheme\b|"
    r"\bpg[\s-]?focus\b|\bpg[\s-]?lamp\b|\bpg[\s-]?existing\b|"
    r"\bfocus[\s-]?\(?addnl\)?\b|\bfocus additional\b|"
    r"\blamp societ(?:y|ies)\b",
    re.IGNORECASE,
)


# ── The two CM Elevate datasets ─────────────────────────────────────────────
# "CM Elevate" names TWO schemes that share nothing but the name
# (cmelevatelegacy_entity_resolver.yaml scheme.disambiguation):
#   * CM Elevate        — 15-scheme APPLICATIONS (curated.v_cm_elevate): status,
#                         on hold, verification, applicant type, gender. No money,
#                         no dates.
#   * CM Elevate Legacy — 13-scheme SANCTION-AND-DISBURSEMENT records
#                         (curated.v_cm_elevate_disbursement): sanctioned amount,
#                         subsidy / loan / total disbursed, lender, financial year.
# The resolver's rule: a bare "CM Elevate" is settled by a field that exists in
# only one of them. Money, a financial year, a lender or a desanction exist only
# in Legacy, so they pin it there — answering them from the applications data
# could only ever refuse. Application-workflow words pin the other one. With
# neither, the bare name keeps its long-standing meaning (the applications data).
#
# Words that name CM Elevate Legacy on their own, with no "CM Elevate" at all.
_CMELEVATELEGACY_ONLY_TERMS = re.compile(
    r"\bcm[\s-]?elevate[\s-]*(?:legacy|disbursements?)\b|"
    r"\bcmelevate[\s-]*(?:legacy|disbursements?)\b|"
    r"\blegacy[\s-]+cm[\s-]?elevate\b|\belevate[\s-]?legacy\b|"
    r"\blifcom\b|\bloan[\s-]?entit(?:y|ies)\b|\blenders?\b|"
    r"\bdesanction\w*|\bde-sanction\w*|\bbank[\s-]?sanctioned\b|"
    r"\bcommon facility cent(?:er|re)\b",
    re.IGNORECASE,
)
# Vocabulary only the sanction-and-disbursement dataset can answer. Consulted
# ONLY once a question is already a CM Elevate question (named, or via a
# sub-scheme word such as "piggery"), so generic money words are safe here.
_CMELEVATELEGACY_FORCING = re.compile(
    r"\bdisburs\w*|\bsanction\w*|\bsubsid(?:y|ies)\b|\bloans?\b|\bgrants?\b|"
    r"\bamounts?\b|\bmoney\b|\bfunds?\b|\brupees?\b|\bcrores?\b|\blakhs?\b|\brs\.?\s*\d|"
    r"\bpaid\b|\bpayments?\b|\breleased\b|\bentitlement\b|\butili[sz]ation\b|"
    # NOT a bare "pending": in the applications data "pending" means on hold
    # (data_verified = 'On Hold'). Only the money reading forces Legacy.
    r"\binstal{1,2}ments?\b|\btranch\w*|\bpending\s+(?:amount|money|disburs\w*)|"
    r"\bpending\s+to\s+be\s+(?:paid|disbursed|released)\b|\byet\s+to\s+be\s+(?:paid|disbursed)\b|"
    r"\brefused\b|\brefusals?\b|\bduplicates?\b|\bdesanction\w*|"
    r"\blifcom\b|\blenders?\b|\bloan[\s-]?entit(?:y|ies)\b|"
    r"\bfinancial\s+years?\b|\bfy\s*\d{2}|\b20\d\d\s*[-/]\s*\d{2,4}\b|\byear[\s-]?wise\b",
    re.IGNORECASE,
)
# Vocabulary only the APPLICATIONS dataset holds. Any of these keeps a bare
# "CM Elevate" on that dataset even when a money word is also present.
_CMELEVATE_APPLICATIONS_ONLY = re.compile(
    r"\bon[\s-]?hold\b|\bverif\w*|\bdata[\s_-]?verified\b|\bwithdraw\w*|"
    r"\bapplicant[\s_-]?categor\w*|\bapplication[\s_-]?mode\b|\bonline\b|"
    r"\bcm\s*connect\w*|\bcurrent[\s_-]?level\b|\bfile[\s_-]?status\b|"
    r"\bapplication[\s_-]?status\b|\bstatus[\s-]?wise\b|\brequest[\s_-]?ids?\b|"
    r"\bgender\b|\bsector\b|\bregistered\b|\bunregistered\b|\bprime small enterprise\b|"
    r"\bseed\b|\bgreen taxi\b|\bcinema\b|\bagro tourism villa\b",
    re.IGNORECASE,
)


def _prefers_cm_elevate_legacy(question: str) -> bool:
    """A CM Elevate question whose vocabulary only the sanction-and-disbursement
    dataset can answer (and none that only the applications dataset holds)."""
    q = question or ""
    if _CMELEVATE_APPLICATIONS_ONLY.search(q):
        return False
    return bool(_CMELEVATELEGACY_FORCING.search(q) or _CMELEVATELEGACY_ONLY_TERMS.search(q))


def _pin_cm_elevate_dataset(question: str) -> str:
    """Re-point a bare "CM Elevate" at CM Elevate Legacy when the question asks
    for something only that dataset holds ("total amount disbursed under CM
    Elevate", "CM Elevate loans by lender", "CM-ELEVATE records in FY 2024-25").

    Done ONCE, on the question text, before routing — the same way a misspelled
    scheme name is corrected — so every later pattern check (scheme shortcut,
    clarification gates, follow-ups, the chips) sees one consistent scheme
    instead of each re-deciding. The rewritten question is returned to the user
    as `rewritten_question`, so the reading is visible, not silent."""
    q = question or ""
    if not q or _SCHEME_NAME_PATTERN["CM Elevate Legacy"].search(q):
        return q
    if not _SCHEME_NAME_PATTERN["CM Elevate"].search(q):
        return q
    if not _prefers_cm_elevate_legacy(q):
        return q
    out = _SCHEME_NAME_PATTERN["CM Elevate"].sub("CM Elevate Legacy", q)
    logger.info("CM Elevate dataset pinned to Legacy: %r -> %r", q, out)
    return out


def _unpin_cm_elevate(question: str) -> str:
    """Undo _pin_cm_elevate_dataset. The pin only ever writes "CM Elevate
    Legacy" into a question that named no Legacy form itself, so every
    occurrence came from the pin and reverts cleanly."""
    return re.sub(r"\bCM Elevate Legacy\b", "CM Elevate", question or "")


def _infer_scheme_from_terms(question: str) -> list[str] | None:
    """A single scheme implied by scheme-specific vocabulary, or None if the
    question could plausibly mean more than one."""
    hits = [
        s for s, rx in (
            ("MGNREGA", _MGNREGA_ONLY_TERMS),
            ("PMAY-G", _PMAY_ONLY_TERMS),
            ("Focus Plus", _FOCUSPLUS_ONLY_TERMS),
            ("CM Elevate", _CMELEVATE_ONLY_TERMS),
            ("Focus Legacy", _FOCUSLEGACY_ONLY_TERMS),
            ("CM Elevate Legacy", _CMELEVATELEGACY_ONLY_TERMS),
        ) if rx.search(question)
    ]
    # A CM Elevate sub-scheme word ("piggery", "dairy") belongs to BOTH CM
    # Elevate datasets. When the rest of the question asks for money, a year or
    # a lender, only CM Elevate Legacy can answer it — settle on that one rather
    # than reading the pair as ambiguous (or sending a money question to the
    # dataset that has no money column).
    if "CM Elevate" in hits and _prefers_cm_elevate_legacy(question):
        hits = [h for h in hits if h != "CM Elevate"]
        if "CM Elevate Legacy" not in hits:
            hits.append("CM Elevate Legacy")
    return hits if len(hits) == 1 else None


# A resumed one-tap question is built by naming the scheme in the stem. Naively
# appending " for <scheme>" reads fine for a DATA stem ("total person-days for
# MGNREGA") but produces broken phrasing for a KNOWLEDGE stem that already says
# the generic word "scheme" ("tell me about scheme" -> "tell me about scheme for
# Focus Plus") — that phrasing is grammatically odd enough that the RAG answer
# composer sometimes reads it as asking about a DIFFERENT, uncovered scheme and
# refuses with "not covered" even though the retrieved passages score well
# above the medium-confidence floor (verified: top score 0.77, well above the
# 0.55 floor, yet the composer still said "not covered" on this exact phrasing).
# When the stem already names "scheme(s)" generically, replace that word with
# the actual scheme name instead of appending — "tell me about scheme" becomes
# "tell me about Focus Plus", which both reads naturally and matches how the
# reference docs open ("Focus Plus is ...").
_GENERIC_SCHEME_WORD = re.compile(r"\bschemes?\b", re.IGNORECASE)


def _scheme_option_question(stem: str, scheme: str) -> str:
    if _GENERIC_SCHEME_WORD.search(stem):
        return _GENERIC_SCHEME_WORD.sub(scheme, stem, count=1)
    return f"{stem} for {scheme}"


def _scheme_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    options = [
        {"label": "MGNREGA (rural employment)",
         "question": _scheme_option_question(stem, "MGNREGA")},
        {"label": "PMAY-G (rural housing)",
         "question": _scheme_option_question(stem, "PMAY-G")},
        {"label": "Focus Plus (farmer cash benefit)",
         "question": _scheme_option_question(stem, "Focus Plus")},
        {"label": "CM Elevate (livelihood / enterprise schemes)",
         "question": _scheme_option_question(stem, "CM Elevate")},
        {"label": "Focus Legacy (producer group payments)",
         "question": _scheme_option_question(stem, "Focus Legacy")},
        {"label": "CM Elevate Legacy (sanctions & disbursements)",
         "question": _scheme_option_question(stem, "CM Elevate Legacy")},
        {"label": "Compare across schemes",
         "question": (f"{stem} across MGNREGA, PMAY-G, Focus Plus, CM Elevate, "
                      f"Focus Legacy and CM Elevate Legacy")},
    ]
    return ClarificationNeeded(
        "Which scheme does your question concern — MGNREGA, PMAY-G, Focus Plus, "
        "CM Elevate, Focus Legacy, or CM Elevate Legacy? Please select one, or choose "
        "to compare across schemes.",
        options=options,
        rule="scheme-not-specified",
    )


# ── "Focus" alone: which of the TWO Focus schemes? ──────────────────────────
# Two live schemes answer to the word "Focus" and they share NOTHING but the name:
# Focus Legacy pays PRODUCER GROUPS (Rs 5,000 per member), Focus Plus pays
# INDIVIDUAL BENEFICIARIES, there is no key between them, and Focus Plus holds no
# producer-group column at all. focuslegacy_entity_resolver.yaml's own
# scheme.disambiguation rule is explicit that a bare "Focus" with no forcing word
# must be ASKED, not guessed — guessing answers a producer-group question from a
# partition that cannot answer it (or vice versa) and reads as authoritative.
#
# This is a TWO-way ask, not the generic five-way one: the user has already told
# us it's a Focus question, so re-offering MGNREGA / PMAY-G / CM Elevate would
# throw that away.
_BARE_FOCUS_WORD = re.compile(r"\bfocus\b", re.IGNORECASE)


def _is_ambiguous_focus(question: str) -> bool:
    """The question says "Focus" but nothing that pins WHICH Focus scheme."""
    q = question or ""
    if not _BARE_FOCUS_WORD.search(q):
        return False
    # Either scheme named outright (or by its own qualified alias) — settled.
    if _SCHEME_NAME_PATTERN["Focus Plus"].search(q):
        return False
    if _SCHEME_NAME_PATTERN["Focus Legacy"].search(q):
        return False
    # A forcing word from either side's own vocabulary — also settled.
    if _FOCUSLEGACY_ONLY_TERMS.search(q) or _FOCUSPLUS_ONLY_TERMS.search(q):
        return False
    return True


def _focus_ambiguity_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    # Replace the bare "Focus" in place rather than appending, so the resumed
    # question reads naturally and re-resolves cleanly on the next turn
    # ("total focus disbursement" -> "total Focus Legacy disbursement").
    def _swap(name: str) -> str:
        swapped = _BARE_FOCUS_WORD.sub(name, stem, count=1)
        return swapped if swapped != stem else f"{stem} for {name}"

    return ClarificationNeeded(
        "Two different schemes are called Focus, and they hold different things — "
        "Focus Legacy pays PRODUCER GROUPS (one payment per group, Rs 5,000 per "
        "member), while Focus Plus pays INDIVIDUAL farmers directly. Which one do "
        "you mean?",
        options=[
            {"label": "Focus Legacy (producer group payments)",
             "question": _swap("Focus Legacy")},
            {"label": "Focus Plus (individual farmer cash benefit)",
             "question": _swap("Focus Plus")},
        ],
        rule="focus-scheme-ambiguous",
    )


# ── CM Elevate: "the vehicle scheme?" — which of several real sub-schemes? ──
# The phrase used to name a scheme-FAMILY the question named in the singular
# (resolve_cm_scheme_group_ambiguity's match) — swapped out for each candidate's
# real scheme_name so the resumed question resolves cleanly via resolve_cm_scheme's
# own exact-alias stage, same trick _scheme_option_question uses for the top-level
# 4-scheme pause above.
_CM_GROUP_PHRASE = {
    "vehicles": re.compile(r"\bvehicle scheme(?!s)\b", re.IGNORECASE),
    "tourism": re.compile(r"\btourism scheme(?!s)\b", re.IGNORECASE),
    "PRIME": re.compile(r"\bprime scheme(?!s)\b", re.IGNORECASE),
    "livestock": re.compile(r"\blivestock scheme(?!s)\b", re.IGNORECASE),
    "enterprise": re.compile(r"\benterprise scheme(?!s)\b", re.IGNORECASE),
}


def _cm_scheme_group_clarification(question: str, group: dict) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    schemes = group["schemes"]
    phrase_re = _CM_GROUP_PHRASE.get(group["group"])
    options = []
    for s in schemes:
        new_q = phrase_re.sub(s, stem, count=1) if phrase_re else None
        options.append({"label": s, "question": new_q if new_q and new_q != stem else f"{stem} — {s}"})
    all_list = ", ".join(schemes[:-1]) + " and " + schemes[-1]
    return ClarificationNeeded(
        f"“{group['group']}” covers {len(schemes)} separate CM Elevate schemes — "
        f"{all_list} — and they are not interchangeable. Which one did you mean?",
        options=options,
        rule="cm-scheme-group-ambiguous",
    )


# ── CM Elevate Legacy: "Sericulture" — spinning, weaving, or both? ──────────
# Sericulture is TWO stored schemes whose names differ by one space before the
# bracket, and an exact match on the wrong spelling returns zero rows silently
# (cmelevatelegacy_classification_rules.yaml sericulture_spelling_ambiguous; the
# prompt-layer bank's clarification K03). A bare "sericulture" is asked, never
# guessed; "both" is taken at its word.
_CM_LEGACY_SERICULTURE = ("Meghalaya Sericulture & Weaving Scheme (spinning)",
                          "Meghalaya Sericulture & Weaving Scheme(weaving)")
_SERICULTURE_WORD = re.compile(r"\bsericulture\b|\bsilk\b", re.IGNORECASE)
_SERICULTURE_SIDE = re.compile(r"\bspinning\b|\bweaving\b|\bhandloom\b", re.IGNORECASE)
_SERICULTURE_BOTH = re.compile(
    r"\bboth\b|\ball\s+(?:the\s+)?sericulture\b|\bsericulture\s+schemes\b|"
    r"\b(?:together|combined|separately)\b",
    re.IGNORECASE)


def _cm_legacy_sericulture_choice(question: str) -> "list[str] | str | None":
    """Both Sericulture literals when the question asks for both, "ask" when it
    names Sericulture without saying which, else None (resolve normally)."""
    q = question or ""
    if not _SERICULTURE_WORD.search(q) or _SERICULTURE_SIDE.search(q):
        return None
    if _SERICULTURE_BOTH.search(q):
        return list(_CM_LEGACY_SERICULTURE)
    return "ask"


def _cm_legacy_sericulture_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")

    def _swap(repl: str) -> str:
        new = _SERICULTURE_WORD.sub(repl, stem, count=1)
        return new if new != stem else f"{stem} — {repl}"

    return ClarificationNeeded(
        "Sericulture is recorded as two separate schemes — spinning and weaving. "
        "Which did you mean, or both?",
        options=[
            {"label": "Spinning", "question": _swap("Sericulture spinning")},
            {"label": "Weaving", "question": _swap("Sericulture weaving")},
            {"label": "Both, shown separately",
             "question": _swap("both Sericulture schemes (spinning and weaving)")},
        ],
        rule="sericulture-spelling-ambiguous",
    )


# ── CM Elevate Legacy: questions the data cannot answer ─────────────────────
# Each pattern is a refusal class from the prompt-layer bank (refusal_code),
# worded by the bank's own reviewed text via annotations.refusal_reason. Checked
# deterministically before SQL generation: the generator can only emit a SELECT,
# so left to it these came back as an improvised figure or a bare "not
# available" with no reason. The patterns are deliberately narrow — the bank's
# re-verification found four over-broad refusal triggers (a relative "who", the
# word "individual", "overview", "focus") and each is avoided here.
_CM_LEGACY_NOT_HELD = (
    # SANCTION_RATE, NOT_SANCTIONED and CONSTITUENCY used to be refused here.
    # All three are answerable from the view (2026-09-25 use-case QA): the rate is
    # COUNT(sanctioned_amount) / COUNT(*) with a caveat about the separate
    # applications dataset, "not sanctioned" is the records with no sanctioned
    # amount, and constituency comes through the dim_geography join. The few-shot
    # bank (X01 / X02 / X07 / X08) teaches the SQL instead.
    ("APPLICANT_NAME", re.compile(
        r"^\W*(?:who|whose)\b|"
        r"\bwho\s+(?:got|received|has|had|took)\s+the\s+(?:most|maximum|highest|largest|biggest)\b|"
        r"\bnames?\s+of\s+(?:the\s+)?(?:\w+\s+)?(?:beneficiar\w*|applicants?|recipients?|"
        r"people|persons?|entrepreneurs?)\b|"
        r"\b(?:beneficiary|applicant)\s+names?\b|\bname\s+list\b",
        re.IGNORECASE)),
    ("MONTHLY", re.compile(
        r"\bmonth[\s-]?wise\b|\bmonthly\b|\bby\s+month\b|\bper\s+month\b|\beach\s+month\b|"
        r"\bquarter(?:ly|[\s-]?wise)?\b",
        re.IGNORECASE)),
    ("APPLICANT_TYPE", re.compile(
        r"\bindividuals?\s+(?:vs\.?|versus|or|and)\s+groups?\b|\bgroup\s+applicants?\b|"
        r"\bshgs?\b|\bself[\s-]?help\s+groups?\b|\b(?:un)?registered\s+groups?\b|"
        r"\bapplicant\s+type\b",
        re.IGNORECASE)),
    ("REPAYMENT", re.compile(r"\brepa(?:id|y|ying|yments?)\b|\bloan\s+recovery\b|"
                             r"\bdefault(?:ed|ers?)\b", re.IGNORECASE)),
    ("DEMOGRAPHICS", re.compile(
        r"\bwom[ae]n\b|\bfemale\b|\bmale\b|\bgender\b|\bcaste\b|\bsc\s*/?\s*st\b|"
        r"\bscheduled\s+(?:caste|tribe)s?\b|\bage[\s-]?(?:group|wise)\b|\bminorit(?:y|ies)\b",
        re.IGNORECASE)),
    ("BANK_SANCTIONED_SHARE", re.compile(
        r"\bbank[\s-]?sanctioned\b|\bbank(?:'s)?\s+(?:share|contribution)\b|"
        r"\bdid\s+the\s+bank\s+contribute\b",
        re.IGNORECASE)),
    ("TARGETS", re.compile(r"\btargets?\b|\bbudget(?:ed|s)?\b|\ballocations?\b",
                           re.IGNORECASE)),
    ("BUSINESS_OUTCOME", re.compile(
        r"\bjobs?\s+(?:were\s+)?(?:created|generated)\b|\bemployment\s+(?:created|generated)\b|"
        r"\bturnover\b|\bventures?\s+surviv\w*|\bbusiness\s+outcomes?\b",
        re.IGNORECASE)),
    ("LINK_APPLICATIONS", re.compile(
        r"\bapplications?\s+behind\b|\blink\w*\s+(?:to|with)\s+(?:the\s+)?applications?\b|"
        r"\bmatch\w*\s+(?:to|with)\s+(?:the\s+)?applications?\b",
        re.IGNORECASE)),
)


def _cm_legacy_not_held(question: str) -> "ClarificationNeeded | None":
    """A not-held explanation for a CM Elevate Legacy question the data cannot
    answer, or None when it can be answered."""
    for code, rx in _CM_LEGACY_NOT_HELD:
        if not rx.search(question or ""):
            continue
        reason = annotations.refusal_reason("CM Elevate Legacy", code)
        if not reason:
            continue
        main, _, offer = reason.partition("Offer instead:")
        text = main.strip()
        if offer.strip():
            text += " What I can offer instead: " + offer.strip()
        logger.info("CM Elevate Legacy not-held (%s): %r", code, question)
        return ClarificationNeeded(text, rule="column-not-held")
    return None


# ── CM Elevate Legacy: caveats that travel with the numbers ─────────────────
# The prompt layer routes its unit / year / village / refusal caveats by CODE,
# not by asking the model to remember them — each is triggered here by what the
# executed SQL actually did, so a caveat appears exactly when its number does.
_CM_LEGACY_MONEY_RE = re.compile(
    r"\b(?:total_disbursement|total_subsidy_disbursement|total_loan_disbursement|"
    r"sanctioned_amount)\b", re.IGNORECASE)
_CM_LEGACY_YEAR_FILTER_RE = re.compile(
    r"\bfinancial_year(?:_short)?\s*(?:=|IN)\s*\(?\s*'|\byear_key\s*(?:=|IN)\s*\(?\s*\d",
    re.IGNORECASE)
_CM_LEGACY_YEAR_GROUP_RE = re.compile(r"\bGROUP\s+BY\b[^;]*\bfinancial_year", re.IGNORECASE)


def _cm_legacy_answer_notes(sql: str, rows: list[dict]) -> list[str]:
    s = sql or ""
    notes: list[str] = []
    cols = {k for r in (rows or [])[:1] if isinstance(r, dict) for k in r}
    if "1e7" in s or any(c.endswith("_cr") for c in cols):
        notes.append(
            "Columns ending in _cr are already in ₹ crore (divided by 1e7); columns ending "
            "in _rupees are rupees. Write money as ₹<value> crore / ₹<value>, copying the "
            "digits exactly.")
    if re.search(r"\btotal_disbursement\b", s, re.IGNORECASE):
        notes.append(
            "Total disbursed means subsidy and loan together. If subsidy and loan columns "
            "are also in the result, give all three; sanctioned is the amount approved, not "
            "the amount paid.")
    if _CM_LEGACY_MONEY_RE.search(s) and "desanctioned_reason_raw" not in s:
        notes.append(
            "Desanctioned records (Refused / Duplicate) are included in these money totals, "
            "as they are in the source file's own totals.")
    if _CM_LEGACY_YEAR_FILTER_RE.search(s):
        notes.append(
            "395 records (both Sericulture schemes) carry no financial year, so they are "
            "outside any single-year figure — say so in one short clause.")
    if _CM_LEGACY_YEAR_GROUP_RE.search(s):
        notes.append(
            "The '(no financial year)' row is the 395 Sericulture records, which carry no "
            "year label — describe it that way, not as missing or erroneous data. Only two "
            "financial years exist, so describe this as a comparison, not a trend.")
    if len(rows or []) > 1 and any(c.endswith("_cr") for c in cols):
        # Seen live (TC-36 district summary, 2026-09-25): the composer added up
        # the per-district _cr figures and stated ₹81.09 / ₹29.24 / ₹51.88 crore
        # against the true ₹81.10 / ₹29.23 / ₹51.87 — each row is rounded to 2
        # decimals, so a sum of them drifts. Counts are exact and may be summed.
        def _is_total(r: dict) -> bool:
            return any(isinstance(v, str) and v.upper().startswith("ALL ") for v in r.values())
        has_total_row = any(_is_total(r) for r in rows)
        # Name the extremes here rather than leave them to the composer: with a
        # trailing ALL row it took the row above it as "the lowest" (TC-36,
        # North Garo Hills ₹2.60 Cr instead of East Jaintia Hills ₹2.36 Cr).
        body = [r for r in rows if not _is_total(r)]
        metric = "total_disbursed_cr" if "total_disbursed_cr" in cols else \
            next((c for c in cols if c.endswith("_cr")), None)
        ranked = [r for r in body if _as_number(r.get(metric)) is not None]
        if metric and len(ranked) >= 2:
            def _label(r: dict) -> str:
                return " / ".join(str(v) for v in r.values()
                                  if isinstance(v, str)) or "(unlabelled)"
            hi = max(ranked, key=lambda r: _as_number(r[metric]))
            lo = min(ranked, key=lambda r: _as_number(r[metric]))
            notes.append(
                f"By {metric}: highest is {_label(hi)} ({_fmt_num(_as_number(hi[metric]))}), "
                f"lowest is {_label(lo)} ({_fmt_num(_as_number(lo[metric]))}). Use these exactly "
                "when naming the top or bottom row.")
        notes.append(
            "Each _cr value is rounded to 2 decimals per row, so NEVER add them across rows "
            "to state a combined money total — the sum drifts from the true figure. "
            + (f"The row labelled 'ALL …' holds the exact totals: quote statewide figures from "
               f"that row only. It is a TOTAL, not an area — the breakdown has exactly "
               f"{len(rows) - 1} rows besides it, so say {len(rows) - 1}, never {len(rows)}. "
               "Also name the top and bottom rows of the breakdown. "
               if has_total_row else
               "Describe the rows (highest, lowest, range); a combined money total may "
               "only be quoted from an 'Exact combined totals' note. ")
            + "Record counts are exact and may be summed.")
    if "sanctioned_records" in cols:
        notes.append(
            "sanctioned_records is the number of SANCTIONED cases (records carrying a "
            "sanctioned amount) — report it under that name, separately from records where "
            "both appear; do not call records 'sanctioned'. It is a COUNT: when listing "
            "rows, give every row's sanctioned_records value, never its _cr money value "
            "in place of the count (TC-19: Motorcaravan 1 record was written as '0.50 crore').")
    if "sanctioned_pct" in cols:
        notes.append(
            "sanctioned_pct is the share of records in THIS sanction-and-disbursement dataset "
            "that carry a sanctioned amount. Give the percentage and the two counts, then say "
            "in one clause that applications which never reached sanction sit in the separate "
            "CM Elevate applications dataset, which cannot be linked to this one.")
    if "not_sanctioned_records" in cols:
        notes.append(
            "not_sanctioned_records are records with no sanctioned amount. Desanctioned "
            "(Refused / Duplicate) counts are sanctions withdrawn later — label them that way, "
            "never add them to the not-sanctioned figure.")
    if re.search(r"\bac_name\b", s, re.IGNORECASE):
        notes.append(
            "The constituency comes from the geography registry through each record's "
            "village; records with no village carry no constituency. Say this in one short "
            "clause, and call it the assembly constituency, not a block.")
    if re.search(r"entity_type\s*<>\s*'Unresolved'", s, re.IGNORECASE):
        notes.append(
            "Records that could not be matched to a village are left out of village "
            "figures (they still count in district and block totals); villages are counted "
            "by LGD code.")
    if re.search(r"\b(?:desanctioned_reason_raw|refused_flag_raw|refused_reason_text)\b",
                 s, re.IGNORECASE):
        notes.append(
            "The desanction reason, the refusal flag and the written reason disagree — name "
            "the field each figure comes from and present none of them as the "
            "authoritative refusal count.")
    return notes


_CR_SUM_RE = re.compile(
    r"ROUND\(\s*SUM\(\s*([\w.]+)\s*\)\s*/\s*1e7\s*,\s*2\s*\)\s+AS\s+(\w+_cr)\b", re.IGNORECASE)
_FROM_TO_GROUP_RE = re.compile(r"\bFROM\b(.*?)\bGROUP\s+BY\b", re.IGNORECASE | re.DOTALL)


async def _cm_legacy_exact_totals(sql: str, rows: list[dict]) -> list[str]:
    """Exact combined money totals for a multi-row _cr breakdown with no ALL row.

    Seen live (TC-27, 2026-09-25): the composer added up 12 rounded per-district
    figures and wrote "sums to ₹82.89 crore" against the true ₹82.90 — the prose
    rule not to sum rounded values did not hold. The total is re-queried from the
    same FROM/WHERE with the GROUP BY removed, so the composer can quote it exactly.
    Only the plain single-SELECT shape is handled; anything else returns no note."""
    s = sql or ""
    if len(rows or []) < 2 or re.search(r"\bROLLUP\b|\bWITH\b|\bHAVING\b", s, re.IGNORECASE):
        return []
    if any(isinstance(v, str) and v.upper().startswith("ALL ") for r in rows for v in r.values()):
        return []
    sums = _CR_SUM_RE.findall(s)
    m = _FROM_TO_GROUP_RE.search(s)
    if not sums or not m or re.search(r"\bSELECT\b", m.group(1), re.IGNORECASE):
        return []
    select = ", ".join(f"ROUND(SUM({expr}) / 1e7, 2) AS {alias}" for expr, alias in sums)
    try:
        got = await run_readonly(f"SELECT {select} FROM {m.group(1).strip()}")
    except Exception:  # noqa: BLE001
        logger.warning("CM Legacy exact-total query failed — no total note", exc_info=True)
        return []
    if not got:
        return []
    figures = ", ".join(f"{k} = {_fmt_num(_as_number(v))}" for k, v in got[0].items()
                        if _as_number(v) is not None)
    if not figures:
        return []
    return [f"Exact combined totals across every group (queried separately, not a sum of "
            f"the rounded rows): {figures}. If you state a combined money total, use "
            f"exactly these figures."]


# Focus Legacy answer notes (use-case QA, 2026-09-25).
#
# TC-19 "Which Producer Groups received more than ₹1,00,000?": 541 groups
# qualify, but the generator capped the list at LIMIT 10/100 and the answer named
# 23 groups without ever saying how many qualify — it read as the complete list.
# When a Focus Legacy list comes back exactly at its LIMIT, the true count is
# re-queried from the same statement and the answer must lead with it.
_FINAL_LIMIT_RE = re.compile(r"\s+LIMIT\s+(\d+)\s*;?\s*\Z", re.IGNORECASE)
# TC-12 "Are there any duplicate Producer Groups?": the answer called the 2,655
# groups paid more than once "duplicate producer groups". They are repeat
# payments in later tranches; a duplicate RECORD would be the same group paid
# twice on the same date, and there are none.
_DUPLICATE_Q = re.compile(r"\bduplicat\w*|\brepeated\s+(?:producer\s+)?groups?\b|\bdoubles?\b",
                          re.IGNORECASE)


async def _focus_legacy_list_total(sql: str, rows: list[dict]) -> "tuple[int, str] | None":
    """(true row count, subject) when a Focus Legacy list was cut off by its own
    LIMIT, else None. The subject is "producer groups" for a pg_id-grained list."""
    m = _FINAL_LIMIT_RE.search(sql or "")
    if not m or len(rows or []) != int(m.group(1)) or len(rows) < 2:
        return None
    subject = "producer groups" if _FL_GROUP_GRAIN.search(sql) else "results"
    for key in ("groups_qualifying", "total_matching", "total_groups"):
        n = _as_number((rows[0] or {}).get(key))
        if isinstance(n, (int, float)) and n > len(rows):
            return int(n), subject
    inner = (sql or "")[: m.start()].rstrip().rstrip(";")
    try:
        got = await run_readonly(f"SELECT COUNT(*) AS n FROM ({inner}) q")
    except Exception:  # noqa: BLE001
        logger.warning("Focus Legacy list-total query failed — no total note", exc_info=True)
        return None
    n = _as_number((got or [{}])[0].get("n"))
    if not isinstance(n, (int, float)) or n <= len(rows):
        return None
    return int(n), subject


def _focus_legacy_answer_notes(question: str) -> list[str]:
    if not _DUPLICATE_Q.search(question or ""):
        return []
    return ["A producer group paid more than once is a REPEAT PAYMENT in a later tranche, NOT a "
            "duplicate group — never call those groups duplicates. A duplicate RECORD would be the "
            "same pg_id paid twice on the same date; report that count as the duplicates figure "
            "(0 means there are no duplicates), and mention the repeat-paid groups separately as "
            "legitimate repeat payments."]


# ── Focus Legacy: "is there a PG named X?" / "how many members are there in X?" ──
# Bulk QA of 290 sampled producer groups (2026-09-25) found the model-written SQL
# unreliable for these two fixed-shape questions: 52 of 131 member-count answers
# were wrong. When several groups share a name ("Chibasal" matches 73) it ended
# the query in LIMIT 1 and reported one arbitrary group's size; it rewrote names
# ("Chelchak Pineapple P.g" -> '%chelchak pineapple p.g.%', zero rows); and some
# never reached the data at all. The answer is fully determined by the data, so
# it is built here: match the name on its WORDS (ignoring "PG" / "Producer
# Group" / punctuation, each word anchored at a word start), rank groups whose
# whole name is exactly those words first, and when several groups match, list
# them instead of guessing.
_PG_STOP_WORDS = {"pg", "pgs", "p", "g", "producer", "producers", "group", "groups", "grp",
                  "the", "shg", "named", "called"}
_PG_SIZE_QUESTION = re.compile(
    r"^\s*(?:how\s+many\s+(?:pg\s+)?members?\s+(?:are\s+(?:there\s+)?|were\s+(?:there\s+)?|is\s+there\s+)?"
    r"(?:in|of)\s+|how\s+many\s+members\s+does\s+|"
    r"what\s+is\s+the\s+(?:member\s+count|group\s+size|number\s+of\s+members)\s+(?:of|in|for)\s+)"
    r"(?:the\s+)?(?:(?:producer\s+group|pg|group)\s+(?:named|called)\s+)?(?P<name>.+?)"
    r"(?:\s+have)?\s*[?.!]*\s*$",
    re.IGNORECASE)
# Scope phrases a clarification chip appends ("... for Focus Legacy for all of
# Meghalaya, all years") — not part of a group name.
_PG_SCOPE_TAIL = re.compile(
    r"\s*,?\s*(?:(?:for|in|under|across|within)\s+(?:the\s+)?(?:focus\s+legacy|focus|all\s+of\s+meghalaya|"
    r"meghalaya|all\s+(?:the\s+)?(?:financial\s+)?years(?:\s+combined)?)|all\s+(?:financial\s+)?years"
    r"(?:\s+combined)?|scheme)\s*[?.!]*\s*$",
    re.IGNORECASE)
_PG_PLACE_WORDS = re.compile(r"\b(?:district|block|village|constituency|state|meghalaya|hills)\b", re.IGNORECASE)
_PG_SUFFIX_WORD = re.compile(r"\b(?:p\.?\s*g\.?|pgs?|producer\s+groups?)\b|\bgroup\b", re.IGNORECASE)


def _pg_name_question(question: str) -> "tuple[str, str] | None":
    """('exists' | 'size', typed name) for a group-name question, else None."""
    q = (question or "").strip()
    kind, name = None, None
    m = _PG_SIZE_QUESTION.match(q)
    if m:
        kind, name = "size", m.group("name")
    else:
        m = _PG_NAMED_ENTITY.search(q)
        if m and re.match(r"^\s*(?:is|are)\s+there\s+(?:any|a|an)\b", q, re.IGNORECASE):
            tail = q[m.end("name"):]
            rest = _PG_SCOPE_TAIL.sub("", tail).strip(" ?.!")
            # "…named X in Betasing block" carries its own place: leave it to the
            # full pipeline, which filters on that place.
            if re.match(r"^\s*,?\s*(?:in|for|under|from|at|within|across|there)\b", rest, re.IGNORECASE):
                return None
            # Otherwise the name simply ran on — a comma inside it ("Ka Seng Ki
            # Nongrep Jhur, Shkenpyrsit") or more words than the pattern takes.
            kind, name = "exists", (m.group("name") + rest) if rest else m.group("name")
    if not kind:
        return None
    prev = None
    while prev != name:
        prev, name = name, _PG_SCOPE_TAIL.sub("", name).strip(" ?.!,\"'“”")
    if not name or _PG_PLACE_WORDS.search(name):
        return None
    # A bare district/block name is a place question ("members in Nongstoin"),
    # unless the user marked it as a group.
    if kind == "size" and not _PG_SUFFIX_WORD.search(name) \
            and not re.search(r"\b(?:producer\s+group|pg)\s+(?:named|called)\b", q, re.IGNORECASE):
        if name.lower() in _known_place_names():
            return None
    if not [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _PG_STOP_WORDS]:
        return None
    return kind, name


def _pg_name_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _PG_STOP_WORDS]


async def _focus_legacy_pg_name_answer(question: str) -> "dict | None":
    parsed = _pg_name_question(question)
    if parsed is None:
        return None
    kind, name = parsed
    toks = _pg_name_tokens(name)

    def _sql(word_end: bool) -> str:
        # tokens are [a-z0-9]+ only, so they are safe inside the regex literal
        end = "\\M" if word_end else ""
        where = " AND ".join(f"pg_name ~* '\\m{t}{end}'" for t in toks)
        return ("SELECT pg_id, MAX(pg_name) AS pg_name, MAX(lgd_district) AS district, "
                "MAX(lgd_block) AS block, MAX(no_of_pg_members) AS group_size, "
                "MIN(no_of_pg_members) AS smallest_recorded_size, COUNT(*) AS payments, "
                "SUM(amount_disbursed) AS amount_disbursed\n"
                f"FROM curated.v_focus_legacy\nWHERE {where}\nGROUP BY pg_id\nORDER BY pg_id\nLIMIT 1000")

    # Whole words first ("ma" must not match every "Mawlai…"); word prefixes only
    # when nothing matches whole ("Bak15" typed for "Bak-15" still resolves).
    sql = _sql(True)
    rows = await run_readonly(sql)
    if not rows:
        sql = _sql(False)
        rows = await run_readonly(sql)
    exact = [r for r in rows if _pg_name_tokens(r.get("pg_name")) == toks]
    ranked = exact + [r for r in rows if r not in exact]

    def place(r):
        bits = [f"{str(r['block']).title()} block" if r.get("block") else None,
                str(r.get("district") or "").title() or None]
        return ", ".join(b for b in bits if b)

    def line(r):
        return (f"- **{r['pg_name']}** ({r['pg_id']}) — {place(r)}: "
                f"{r['group_size']} member{'s' if r['group_size'] != 1 else ''}")

    single = exact[0] if len(exact) == 1 else (rows[0] if len(rows) == 1 else None)
    if not rows:
        answer = (f"No producer group named “{name}” was found in the Focus Legacy data "
                  "(names are matched on their words, ignoring “PG” / “Producer Group”)."
                  + (" So there is no member count to report." if kind == "size" else ""))
    elif single is not None:
        r = single
        if kind == "size":
            answer = f"**{r['pg_name']}** ({r['pg_id']}, {place(r)}) has **{r['group_size']} members**."
            if r["smallest_recorded_size"] != r["group_size"]:
                answer += (f" Its recorded size changed between its {r['payments']} payments "
                           f"({r['smallest_recorded_size']} to {r['group_size']}); "
                           f"{r['group_size']} is the largest recorded.")
        else:
            answer = (f"Yes — **{r['pg_name']}** ({r['pg_id']}) is a Focus Legacy producer group in "
                      f"{place(r)}, with {r['group_size']} members and {r['payments']} "
                      f"payment{'s' if r['payments'] != 1 else ''} totalling "
                      f"₹{_fmt_num(_as_number(r['amount_disbursed']) or 0)}.")
        if len(rows) > 1:
            answer += (f" ({len(rows) - 1} other group{'s' if len(rows) > 2 else ''} have "
                       f"“{name}” within a longer name.)")
    else:
        head = (f"Yes — {len(rows)} producer groups match “{name}”" if kind == "exists"
                else f"{len(rows)} producer groups match “{name}”, so the member count depends on "
                     "which one you mean")
        if exact:
            head += f" ({len(exact)} named exactly that)"
        shown = ranked[:10]
        answer = head + ":\n\n" + "\n".join(line(r) for r in shown)
        if len(ranked) > len(shown):
            answer += f"\n\n…and {len(ranked) - len(shown)} more in the table."
        if kind == "size":
            answer += "\n\nTell me the PG ID or the district to pin down one group."
    logger.info("Focus Legacy PG-name %s question for %r: %d match(es), %d exact", kind, name,
                len(rows), len(exact))
    return {"route": "data", "intent": "DATA", "confidence": "high", "schemes": ["Focus Legacy"],
            "resolved_entities": {}, "sql": sql, "sql_query": sql, "row_count": len(rows),
            "rows": ranked[:20], "data": ranked, "answer": answer}


def _cm_legacy_small_money_notes(rows: list[dict]) -> list[str]:
    """A _cr value of 0.00 is a real amount under ₹0.5 lakh, not zero (TC-18:
    Sericulture spinning ₹26,000 was written as "₹0.00 crore")."""
    tiny = [r for r in rows or [] for k, v in r.items()
            if k.endswith("_cr") and _as_number(v) == 0]
    if not tiny:
        return []
    return ["A _cr value of 0.00 means under ₹0.01 crore (less than ₹1 lakh), not "
            "nothing — write it as 'under ₹0.01 crore', never '₹0.00 crore'."]


_CM_LEGACY_TRAILING_OFFER = re.compile(
    r"\s*(?:Would you like|Shall I|Do you want)[^?]*\?\s*$", re.IGNORECASE)


def _cm_legacy_style_block(question: str) -> str:
    """Two worked answers from the bank as WORDING examples for the composer.
    The trailing "Would you like…?" is dropped — the UI already offers next-step
    chips, and a second offer in the text would duplicate them."""
    shots = [a for a in annotations.answer_shots("CM Elevate Legacy", question, top_k=4)
             if a.get("rows")][:2]
    if not shots:
        return ""
    parts = []
    for a in shots:
        answer = _CM_LEGACY_TRAILING_OFFER.sub("", " ".join(str(a["answer"]).split()))
        parts.append(f'Q: "{a["question"]}"\nColumns: {", ".join(a["columns"])}\n'
                     f"Rows: {json.dumps(a['rows'], ensure_ascii=False)}\nAnswer: {answer}")
    return ("\nWORDING EXAMPLES for this scheme — copy the style only. Their numbers "
            "belong to OTHER questions and must never appear in this answer:\n"
            + "\n\n".join(parts) + "\n")


# Plain-language, user-facing scheme summaries — separate from SCHEME_CATALOG
# in schema_context.py, which is written for the SQL-generation prompt (DB
# grain, money units, join keys) and reads as database jargon to an end user.
_SCHEME_USER_SUMMARY = {
    "MGNREGA": "Rural employment guarantee scheme — up to 100 days of guaranteed "
               "wage employment per household per year.",
    "PMAY-G": "Rural housing scheme (Gramin) — financial assistance to build a "
              "pucca house for eligible rural households.",
    "Focus Plus": "Meghalaya state farmer cash-benefit scheme — direct cash "
                  "payments (DBT) to registered farmers.",
    "CM Elevate": "Meghalaya livelihood & enterprise support programme — covers "
                  "15 individual schemes (piggery, poultry, small enterprise "
                  "loans, tourism vehicles, and more) under one umbrella.",
    "Focus Legacy": "Meghalaya state producer-group scheme (the original FOCUS) — "
                    "payments to farmer Producer Groups at Rs 5,000 per member. "
                    "Different from Focus Plus, which pays individual farmers.",
    "CM Elevate Legacy": "CM-ELEVATE sanction and disbursement records — the amount "
                         "sanctioned to each applicant across 13 schemes (piggery, "
                         "poultry, dairy, warehouse, tourism vehicles and more) and the "
                         "subsidy and loans actually paid, FY 2024-25 and 2025-26. "
                         "Different from CM Elevate, which holds the applications.",
}

# "What schemes are available?" / "what can you help with?" — answered directly
# and concisely instead of falling through to RAG, which has no single document
# listing all 4 schemes and tends to elaborate at length on whichever one scores
# highest in vector search (QA repeatedly saw an over-detailed PMAY-only answer).
#
# The bare "(what|which) schemes?" alternative used to have NO tail anchor, so it
# also swallowed every "which scheme has the most applications?" / "which scheme
# dominates each district?" / "which schemes are not statewide?" DATA question —
# CM Elevate's few-shot corpus alone has a dozen of exactly this shape (superlative
# or filter questions over its 15 sub-schemes), and every one of them was being
# answered with the generic four-scheme blurb instead of a real query (confirmed
# live 2026-09-11: "which scheme has the most applications under CM Elevate?" ->
# the canned listing, never reaching classify_scheme/resolve_entities/SQL gen).
# Anchoring the bare form to the tail of the question — "which schemes?" / "what
# schemes are there/available/offered/supported" / "do you have/know/cover" —
# keeps the genuinely scheme-agnostic listing asks while letting a superlative or
# filter question (which always has more text after "scheme(s)") fall through to
# normal DATA routing.
_SCHEME_LISTING_CUE = re.compile(
    r"\b(?:what|which) schemes?\??\s*$|"
    r"\b(?:what|which) schemes?\b\s*(?:are\s+(?:there|available|offered|supported)|"
    r"exist|do (?:you|i) (?:have|know|cover)|can you (?:tell|list))\b|"
    r"\bschemes? (?:are|is) available\b|"
    r"\blist (?:the |all )?schemes?\b|\bschemes? (?:do you|you) (?:support|cover|know|have)\b|"
    r"\bwhat (?:can|do) you (?:help with|assist with|cover)\b",
    re.IGNORECASE,
)

# A vague eligibility ask — "is there any scheme that can help my family?",
# "which scheme should I apply for?" — carries no vocabulary naming a scheme
# outright, so it used to fall into the "which scheme does your question
# concern?" pause. That pause is the wrong move here: unlike a DATA question
# (where guessing the scheme risks a confidently wrong number), there's
# nothing to get wrong about naming all four — so skip the tap and lay them
# out directly. When the wording DOES carry scheme-specific vocabulary (e.g.
# "...to help me build a house") _infer_scheme_from_terms already pins it to
# one scheme and _needs_scheme_clarification never pauses in the first place —
# this cue only needs to cover the genuinely scheme-agnostic case, so it
# defers to that inference rather than overriding it.
_SCHEME_HELP_CUE = re.compile(
    r"\bis there (?:any|a) (?:govt\.?|government )?schemes?\b|"
    r"\b(?:any|a) scheme (?:that|which|to) (?:can |could )?help\b|"
    r"\bscheme(?:s)? (?:that|which) (?:can|could) help\b|"
    r"\bwhich scheme (?:can|could|should) (?:i|we)\b|"
    r"\bwhat scheme should (?:i|we)\b",
    re.IGNORECASE,
)


# CM Elevate and CM Elevate Legacy are ONE programme (CM-ELEVATE) held as two
# datasets — its applications, and its sanctions and disbursements. Listed as
# two schemes, the reply read as if there were two unrelated programmes (and the
# count was hard-coded "four" against six lines). The listing shows the
# programme once and names its two data parts; every other scheme's line is
# unchanged. _SCHEME_USER_SUMMARY keeps both entries, for the comparison answer.
_PROGRAMME_DATASETS = {"CM Elevate": ["CM Elevate Legacy"]}
_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def _count_word(n: int) -> str:
    return _COUNT_WORDS.get(n, str(n))


def _scheme_listing_lines() -> list[str]:
    folded = {d for ds in _PROGRAMME_DATASETS.values() for d in ds}
    lines = []
    for name, desc in _SCHEME_USER_SUMMARY.items():
        if name in folded:
            continue
        if name == "CM Elevate":
            desc = (
                "Meghalaya livelihood & enterprise support programme (CM-ELEVATE) — "
                "piggery, poultry, dairy, small enterprise, tourism vehicles and more. "
                "Its data comes in two parts: the **applications** (15 schemes — status, "
                "verification, applicant type; no amounts) and **CM Elevate Legacy**, the "
                "sanctions and disbursements (13 schemes — amount sanctioned, subsidy and "
                "loans paid, FY 2024-25 and 2025-26)."
            )
        lines.append(f"- **{name}** — {desc}")
    return lines


# ── "Pick any scheme and explain it" — the user hands the CHOICE to the bot ──
# "pick any scheme out of these and explain", "no, pick yourself any scheme",
# "you choose one", "tell me about any one of them", "explain a scheme of your
# choice", "surprise me"... The user is not naming a scheme and does not want to
# be asked for one — asking "which scheme?" is the exact opposite of the request
# (reported 2026-09-24: three turns of the bot either asking back or answering
# "not covered" against the raw wording). Answered deterministically: the scheme
# the user did name if any, else one this conversation has not covered yet,
# explained as key points from that scheme's own reference docs.
_PICK_VERB = (r"(?:pick|choose|chose|select|take|go\s+with|explain|describe|"
              r"tell\s+(?:me\s+)?about|talk\s+about|give|show|share|summari[sz]e|brief|elaborate)")
_PICK_DELEGATION = re.compile(
    # "pick any scheme", "choose one of these", "take a random scheme", "pick yourself",
    # "select one scheme", "pick another scheme"
    rf"\b{_PICK_VERB}\b[^.?!]{{0,40}}?\b(?:any(?:\s*one)?|some|a\s+random|random|one\s+of|"
    r"another|your\s*self|yourself|your\s+own|of\s+your\s+choice|whichever|"
    r"(?:a|one)\s+(?:single\s+)?(?:scheme|programm?e))\b|"
    # "any one of these / them", "any scheme", "one of the schemes"
    r"\bany\s*(?:one|1)\s+(?:of\s+)?(?:these|them|those|the\s+schemes?)\b|"
    r"\b(?:any|a\s+random|random)\s+scheme\b|"
    # "you choose", "your choice", "you decide", "up to you", "surprise me"
    r"\b(?:you|u)\s+(?:pick|choose|decide|select)\b|\byour\s+(?:choice|pick|call)\b|"
    r"\bup\s+to\s+you\b|\bsurprise\s+me\b|\bwhichever\s+(?:you|u)\b",
    re.IGNORECASE)
# What makes it a question about a SCHEME (not "pick any district and show ..."):
# the word scheme / programme, a reference back to the list just shown, or a
# delegation phrase that can only mean the scheme choice.
_PICK_OBJECT = re.compile(
    r"\bschemes?\b|\bprogramm?e?s?\b|\b(?:these|them|those)\b|\bone\s+of\b|"
    r"\byour\s*self\b|\byourself\b|\byour\s+(?:own|choice|pick|call)\b|\bup\s+to\s+you\b|"
    r"\bsurprise\s+me\b|\b(?:you|u)\s+(?:pick|choose|decide|select)\b|"
    # a bare "just pick any" / "pick any one" / "choose another" — nothing else in
    # the message, so the only thing on offer to pick is a scheme
    r"^\W*(?:no\W+|ok(?:ay)?\W+|then\W+|so\W+)?(?:just\s+)?(?:pick|choose|select)\s+"
    r"(?:any(?:\s*one)?|one|another(?:\s+one)?|one\s+more|the\s+next\s+one|next\s+one)\W*$",
    re.IGNORECASE)
# "another one" / "one more" / "next one" / "explain another" on its own. Only a
# pick when the PREVIOUS answer was a pick — after a data answer the same words
# mean another district or year, and go to the follow-up rewrite as before.
_PICK_CONTINUE = re.compile(
    r"^\W*(?:no\W+|ok(?:ay)?\W+|then\W+|so\W+|and\W+)?(?:(?:explain|tell\s+(?:me\s+)?about|"
    r"describe|give|show)\s+(?:me\s+)?)?(?:another(?:\s+(?:one|scheme))?|one\s+more|"
    r"(?:the\s+)?next\s+(?:one|scheme))\W*$",
    re.IGNORECASE)
_PICK_OVERVIEW_PREFIX = "Explain its key points — the objective"
# A NEED-based ask ("is there any scheme that can help my family?", "any scheme
# for farmers?") wants the scheme that FITS — the existing help/listing path
# answers that. Picking one arbitrarily would be the wrong answer to it.
_PICK_NEED_GUARD = re.compile(
    r"\b(?:help|helps|helpful|eligible|suitable|should\s+i|can\s+i|could\s+i|"
    r"apply\s+for|is\s+there)\b|"
    r"\bfor\s+(?:farmers?|women|youth|students?|the\s+poor|poor|my|us|families|"
    r"entrepreneurs?|widows?|elderly|disabled)\b",
    re.IGNORECASE)
# A request for a FIGURE is a data question even when it says "pick any".
_PICK_DATA_GUARD = re.compile(
    r"\b(?:how\s+many|how\s+much|total|number\s+of|count|sum|average|top\s*\d|"
    r"highest|lowest|most|least|rank\w*|district|block|village|panchayat|"
    r"financial\s+year|fy\s*\d|20\d\d|expenditure|person[\s-]?days?|houses?|"
    r"disburs\w*|amount|payments?|records?|applications?|beneficiar\w*|"
    r"data|figures?|numbers|statistics|stats)\b",
    re.IGNORECASE)
# "another" / "different" / "other" / "next" — explicitly not the one just done.
_PICK_ANOTHER = re.compile(r"\b(?:another|different|other|next|new|else)\b", re.IGNORECASE)


def _is_scheme_pick_request(question: str) -> bool:
    q = question or ""
    # "why did you choose MGNREGA?" asks for a REASON, not another pick.
    if _WHY_CHOICE.search(q):
        return False
    if not _PICK_DELEGATION.search(q) or not _PICK_OBJECT.search(q):
        return False
    return not (_PICK_DATA_GUARD.search(q) or _PICK_NEED_GUARD.search(q))


def _pickable_schemes() -> list[str]:
    """The programmes a user can be given, in listing order. CM Elevate Legacy
    is CM Elevate's data, not a separate programme (see _PROGRAMME_DATASETS)."""
    folded = {d for ds in _PROGRAMME_DATASETS.values() for d in ds}
    return [s for s in _SCHEME_USER_SUMMARY if s not in folded]


def _pick_scheme(question: str, session: "Session | None") -> tuple[str, bool]:
    """(scheme, chosen_by_bot). A scheme the user named wins. Otherwise the first
    programme this conversation has not been told about yet, so "pick another"
    / a repeated "pick one yourself" moves on instead of repeating itself."""
    named = [s for s in _named_schemes(question) if s in _SCHEME_USER_SUMMARY]
    if named:
        folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
        return folded.get(named[0], named[0]), False
    options = _pickable_schemes()
    folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
    covered: list[str] = []
    for t in (getattr(session, "turns", None) or []):
        for s in (t.schemes or []):
            s = folded.get(s, s)
            if s in options and s not in covered:
                covered.append(s)
    fresh = [s for s in options if s not in covered]
    if fresh:
        return fresh[0], True
    # Everything has been covered once — cycle on from the most recent one.
    last = covered[-1] if covered else options[-1]
    return options[(options.index(last) + 1) % len(options)], True


# A request for a summary of the scheme as a whole (see the knowledge route).
_OVERVIEW_REQUEST = re.compile(
    r"\boverview\b|\bbriefing\b|\bbrief\s+(?:note|summary|introduction)\b|"
    r"\bsummar(?:y|ise|ize)\s+(?:of\s+)?(?:the\s+)?(?:scheme|programme|program|focus)\b|"
    r"\bat\s+a\s+glance\b|\bexecutive\s+summary\b",
    re.IGNORECASE)


def _scheme_overview_question(scheme: str) -> str:
    """The standalone knowledge question the pick is answered with — phrased as
    the reference docs are written, so retrieval lands on the overview sections
    rather than on whatever the user's delegation wording happened to match."""
    return (f"What is {scheme}? Explain its key points — the objective, who is "
            f"eligible, the benefits it provides and how to apply.")


# ── "Suggest a scheme that suits me" — a RECOMMENDATION from the user's profile ──
# "i am living in rural area, suggest me the best scheme", "my friend is starting
# a startup, suggest him a scheme", "i am a farmer, suggest a scheme". None of
# these names a scheme, so the knowledge route inherited the PREVIOUS turn's
# scheme and searched only its documents — every one of them was answered from
# MGNREGA's material, or "not covered" (reported 2026-09-24). A recommendation
# is a question ACROSS schemes: it is answered here, deterministically, by
# matching what the user says about themselves to who each scheme is for.
#
# Who each scheme is for — taken from each scheme's own reference FAQ
# (data/reference/*_general_faq.md, FOCUS_LEGACY_FAQ.md), not invented. Focus
# Plus in particular is NOT for any farmer: its FAQ makes Producer Group
# membership the precondition.
_SCHEME_FIT = {
    "MGNREGA": (
        "guaranteed paid work",
        "any adult member of a rural household who is willing to do unskilled manual "
        "work can get up to 100 days of paid work a year, with no income test — you "
        "register for a job card at your Gram Panchayat."),
    "PMAY-G": (
        "a permanent (pucca) house",
        "for rural households that do not own a pucca house and have not had government "
        "housing help before; households are identified from the SECC / Awaas+ list "
        "through the Gram Panchayat."),
    "Focus Plus": (
        "direct cash support for farm and livelihood activity",
        "cash paid directly (DBT) to Meghalaya households that are members of a Producer "
        "Group of 10 or more, for farm inputs and extra income activities such as "
        "piggery, poultry, horticulture, ginger or turmeric."),
    "Focus Legacy": (
        "seed money for a farmers' Producer Group",
        "farmers organised into (or willing to form) a Producer Group get ₹5,000 per "
        "member as seed / working capital; urban groups of 10 or more members are also "
        "eligible — register at the C&RD Block office."),
    "CM Elevate": (
        "starting or growing a business",
        "individuals, registered businesses, SHGs and Producer Groups in Meghalaya can "
        "get support for a venture in one of 15 sectors — piggery, poultry, dairy, goat "
        "farming, warehousing, tourism vehicles and more — or under the \"Any Business "
        "Venture\" category; you apply on the MeghalayaOne portal."),
}
# (profile, what it says about the user, pattern, schemes in order of fit)
_PROFILE_RULES = (
    ("business", "starting or running a business",
     re.compile(r"\bstart[\s-]?ups?\b|\bstart(?:ing|ed)?\s+(?:a\s+|an\s+|my\s+|our\s+|his\s+|her\s+|"
                r"their\s+|the\s+)?(?:own\s+|new\s+|small\s+)*(?:business|company|venture|enterprise|"
                r"shop|unit|firm)\b|\bbusiness\w*\b|\benterprises?\b|\bentrepreneur\w*|"
                r"\bself[\s-]?employ\w*|\bventures?\b|\bcompany\b|\bshop\b|\bmsme\b|"
                r"\bpiggery\b|\bpoultry\b|\bdairy\b|\bgoat\w*|\btourism\b|\btaxi\b|"
                r"\bwarehouse\b|\bhomestay\b", re.IGNORECASE),
     ["CM Elevate"]),
    ("group", "part of a farmers' group / SHG",
     re.compile(r"\bproducer\s+groups?\b|\bfarmers?['’]?\s+groups?\b|\bgroup\s+of\s+farmers\b|"
                r"\bshgs?\b|\bself[\s-]?help\s+groups?\b|\bcollective\b|\bco-?operative\b|\bpgs?\b",
                re.IGNORECASE),
     ["Focus Legacy", "Focus Plus", "CM Elevate"]),
    ("farmer", "a farmer",
     re.compile(r"\bfarm(?:er|ers|ing)?\b|\bagricultur\w*|\bcultivat\w*|\bcrops?\b|\bkisan\b|"
                r"\bhorticultur\w*|\bkheti\b", re.IGNORECASE),
     ["Focus Plus", "Focus Legacy", "CM Elevate", "MGNREGA"]),
    ("housing", "in need of a house",
     re.compile(r"\bhouse\b|\bhouses\b|\bhome\b|\bhousing\b|\bkutcha\b|\bhomeless\b|\bshelter\b|"
                r"\broof\b|\bpucca\b", re.IGNORECASE),
     ["PMAY-G"]),
    ("work", "looking for work / income",
     re.compile(r"\bjobs?\b|\bunemploy\w*|\bemployment\b|\blabou?r\w*|\bwages?\b|\bdaily\s+wage\b|"
                r"\bneed\s+(?:some\s+)?(?:work|income|money)\b|\blooking\s+for\s+work\b|"
                r"\bno\s+(?:work|income|job)\b", re.IGNORECASE),
     ["MGNREGA"]),
    ("rural", "living in a rural area",
     re.compile(r"\brural\b|\bvillages?\b|\bgaon\b|\bcountryside\b", re.IGNORECASE),
     ["MGNREGA", "PMAY-G"]),
)
_RECOMMEND_CUE = re.compile(
    r"\bsuggest\w*|\brecommend\w*|\badvi[cs]e\b|\bbest\s+(?:suited\s+)?schemes?\b|"
    r"\bright\s+scheme\b|\bsuit(?:s|able|ed)?\b|\bfits?\s+(?:me|him|her|us|them|my|our)\b|"
    r"\b(?:which|what)\s+schemes?\s+(?:should|can|could|would|will|do|does)\s+"
    r"(?:i|we|he|she|they|my|our|you\s+(?:suggest|recommend))\b|"
    r"\b(?:which|what)\s+schemes?\s+(?:is|are|would\s+be)\s+(?:the\s+)?"
    r"(?:best|good|right|suitable|useful)\s+for\b|"
    r"\bschemes?\s+for\s+(?:me|him|her|us|them|my|our|a|an)\b|\bany\s+schemes?\s+for\b|"
    r"\beligible\s+for\s+(?:which|what)\b|\bhelp\s+(?:me|him|her|us)\s+(?:choose|find|pick)\b",
    re.IGNORECASE)
# Looser question shapes that are a recommendation ONLY when the message also
# describes the person's need ("my friend is starting a startup, which scheme
# benefits him"). On their own they are far too common in data and listing
# questions ("which scheme has the most houses", "is there any scheme that can
# help my family?"), which keep their existing handling.
_RECOMMEND_CUE_WITH_NEED = re.compile(
    r"\b(?:which|what)\s+schemes?\b|\bany\s+schemes?\b|\bis\s+there\s+(?:any|a)\b|"
    r"\bschemes?\b[^?.!]{0,40}\b(?:benefit|help|support|useful|avail|apply|get|give|offer)\w*|"
    r"\b(?:benefit|help|support)\w*\s+(?:to\s+|for\s+)?(?:me|him|her|us|them|my|our|his)\b|"
    r"\b(?:can|could|will|would)\s+(?:i|he|she|we|they|my\s+\w+)\s+(?:get|avail|apply|benefit)\b|"
    r"\bwhat\s+(?:can|could|will)\s+(?:i|he|she|we|they|my\s+\w+)\s+get\b",
    re.IGNORECASE)
_RECOMMENDATION_LEAD = "Based on what you've told me"


def _user_profile(text: str) -> list[tuple[str, str, list[str]]]:
    return [(k, desc, schemes) for k, desc, rx, schemes in _PROFILE_RULES if rx.search(text or "")]


def _latest_profile(session: "Session | None", n: int = 4) -> list[tuple[str, str, list[str]]]:
    """The profile from the MOST RECENT of the user's last n messages that
    described someone. Never a blend of several messages: "my friend is
    starting a startup" and later "I am a farmer" are two different people,
    and merging them recommended the friend's scheme to the farmer."""
    for t in reversed((getattr(session, "turns", None) or [])[-n:]):
        prof = _user_profile(t.raw_question or "")
        if prof:
            return prof
    return []


# "Is there any Producer Group named Nongstoin PG?" is a NAME LOOKUP in the data,
# not a person describing themselves: the loose cue "is there any" plus the
# "group" profile word sent it to the recommender, which replied with a list of
# schemes (Focus Legacy QA TC-13, 2026-09-25). A naming word attached to a group
# noun vetoes the recommendation; "my friend named Ram is a farmer" is untouched.
_GROUP_NAME_LOOKUP = re.compile(
    r"\b(?:producer\s+groups?|pgs?|groups?|shgs?)\s+(?:(?:is|are|was)\s+)?"
    r"(?:named|called|titled|with\s+(?:the\s+)?name|by\s+(?:the\s+)?name)\b",
    re.IGNORECASE)


def _is_recommendation_request(question: str) -> bool:
    q = question or ""
    if _WHY_CHOICE.search(q):
        return False
    if _GROUP_NAME_LOOKUP.search(q):
        return False
    if not (_RECOMMEND_CUE.search(q)
            or (_RECOMMEND_CUE_WITH_NEED.search(q) and _user_profile(q))):
        return False
    # "is PMAY-G suitable for me?" is an eligibility question about THAT scheme
    # — its own reference docs answer it (the normal knowledge route).
    if _named_schemes(q):
        return False
    if _RECOMMEND_DATA_GUARD.search(q):
        return False
    # It must actually be about a scheme: a scheme word, or a need a scheme can
    # meet stated in the message itself. "give me some suggestions" on its own
    # is not a scheme question — reading it as one borrowed the previous turn's
    # need and recommended PMAY-G to "i want to rob bank, give me some
    # suggestions" (reported 2026-09-24).
    return bool(_SCHEME_CONTEXT.search(q) or _user_profile(q))


_SCHEME_CONTEXT = re.compile(
    r"\bschemes?\b|\byojana\b|\byojna\b|\bprogramm?e?s?\b|\bgovt\.?\b|\bgovernment\b|"
    r"\bsubsid\w*|\bassistance\b|\bbenefits?\b|\beligible\b|\beligibility\b|\bapply\b",
    re.IGNORECASE)


# Only FIGURE words — unlike the pick guard, "house" / "village" / "district"
# describe the user's situation here ("I live in a village, I need a house").
_RECOMMEND_DATA_GUARD = re.compile(
    r"\b(?:how\s+many|how\s+much|total|number\s+of|count|sum|average|top\s*\d|"
    r"highest|lowest|most|least|maximum|minimum|largest|biggest|smallest|"
    r"rank\w*|expenditure|person[\s-]?days?|disburs\w*|amount\s+(?:of|paid|spent)|"
    r"data|figures?|statistics|stats|20\d\d)\b",
    re.IGNORECASE)


def _recommendation_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    needs = [("I need paid work", "I need work"), ("I need a house", "I need a house"),
             ("I am a farmer", "I am a farmer"),
             ("I am in a farmers' group / SHG", "I am in a farmers' producer group"),
             ("I want to start a business", "I want to start a business")]
    return ClarificationNeeded(
        "Happy to suggest one — tell me a little about the need, so I can match it to "
        "the right scheme:",
        options=[{"label": lbl, "question": f"{stem} — {why}"} for lbl, why in needs],
        rule="recommendation-needs-profile",
    )


def _scheme_recommendation_answer(question: str, session: "Session | None") -> "dict | None":
    if not _is_recommendation_request(question):
        return None
    profile = _user_profile(question)
    from_history = False
    if not profile:
        profile = _latest_profile(session)
        from_history = bool(profile)
    if not profile:
        raise _recommendation_clarification(question)
    ranked: list[str] = []
    for _k, _d, schemes in profile:
        for s in schemes:
            if s not in ranked:
                ranked.append(s)
    about = " and ".join(d for _k, d, _s in profile)
    lead = f"{_RECOMMENDATION_LEAD} ({about}{', from earlier in our chat' if from_history else ''}), "
    lead += ("this scheme fits best:" if len(ranked) == 1 else "these schemes fit, best match first:")
    lines = []
    for i, s in enumerate(ranked, 1):
        what, who = _SCHEME_FIT[s]
        lines.append(f"{i}. **{s}** — for {what}: {who}")
    tail = ("\n\nThese are suggestions based on each scheme's published eligibility — the "
            "implementing department makes the final decision. Ask me \"how to apply for "
            f"{ranked[0]}\" for the steps.")
    logger.info("scheme recommendation: %r -> %s (profile=%s)", question, ranked,
                [k for k, _d, _s in profile])
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": f"{lead}\n\n" + "\n".join(lines) + tail,
            **_empty_data_fields(), "schemes": [ranked[0]]}


# ── "my friend is starting a startup, which PMAY-G benefits him?" ───────────
# A named scheme plus a stated need it does NOT serve. The scheme's own documents
# cannot say "this is the wrong scheme for you" — they either describe the scheme
# anyway or reply that startups are "not mentioned" (reported 2026-09-24). Said
# plainly here, with the scheme that does fit. When the named scheme DOES fit the
# need, this steps aside and its own documents answer, exactly as before.
_FIT_CUE = re.compile(r"\beligib\w*|\bsuit\w*|\bfits?\b|\buseful\b|\bgood\s+for\b|\bright\s+for\b",
                      re.IGNORECASE)


def _scheme_fit_check_answer(question: str) -> "dict | None":
    q = question or ""
    if _WHY_CHOICE.search(q) or _RECOMMEND_DATA_GUARD.search(q):
        return None
    if not (_FIT_CUE.search(q) or _RECOMMEND_CUE.search(q) or _RECOMMEND_CUE_WITH_NEED.search(q)):
        return None
    folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
    named = list(dict.fromkeys(folded.get(s, s) for s in _named_schemes(q)))
    if len(named) != 1 or named[0] not in _SCHEME_FIT:
        return None
    profile = _user_profile(q)
    if not profile:
        return None
    ranked: list[str] = []
    for _k, _d, schemes in profile:
        ranked += [s for s in schemes if s not in ranked]
    x = named[0]
    if x in ranked:
        return None
    about = " and ".join(d for _k, d, _s in profile)
    what, who = _SCHEME_FIT[x]
    parts = [f"**{x}** isn't meant for this need ({about}): it is for {what} — {who}"]
    best = ranked[0]
    parts.append(f"For this need, **{best}** fits: {_SCHEME_FIT[best][1]}")
    if len(ranked) > 1:
        parts.append("Also worth a look: " + ", ".join(f"**{s}** ({_SCHEME_FIT[s][0]})"
                                                      for s in ranked[1:]) + ".")
    parts.append(f"Ask me \"how to apply for {best}\" for the steps.")
    logger.info("scheme fit check: %r -> %s does not fit %s; suggest %s", q, x,
                [k for k, _d, _s in profile], best)
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": "\n\n".join(parts), **_empty_data_fields(), "schemes": [best]}


# ── "Why did you choose / give MGNREGA (instead of Focus)?" ─────────────────
# A question about the BOT'S OWN previous answer, not about a scheme's rules —
# no reference document can answer it, so the knowledge route either repeated
# the overview or said "not covered" (reported 2026-09-24). It is answered from
# what the previous answer actually was: a pick, a recommendation, or a scheme
# the conversation was already on.
_WHY_CHOICE = re.compile(
    r"\bwhy\b[^?.!]{0,40}\b(?:choose|chose|chosen|pick|picked|picking|give|given|gave|"
    r"suggest\w*|recommend\w*|select\w*|instead|rather|only|not)\b",
    re.IGNORECASE)
_INSTEAD_OF = re.compile(r"\b(?:instead\s+of|rather\s+than|over|and\s+not|not)\s+(?P<alt>.+)$",
                         re.IGNORECASE)


def _why_choice_answer(question: str, session: "Session | None") -> "dict | None":
    q = question or ""
    if not _WHY_CHOICE.search(q):
        return None
    last = getattr(session, "last_turn", None) if session is not None else None
    if last is None:
        return None
    folded = {d: p for p, ds in _PROGRAMME_DATASETS.items() for d in ds}
    # The scheme the question says was given, and the alternative it names.
    m = _INSTEAD_OF.search(q)
    alt_text = m.group("alt") if m else ""
    given_text = q[: m.start()] if m else q
    given = [folded.get(s, s) for s in _named_schemes(given_text)]
    given = given or [folded.get(s, s) for s in (last.schemes or [])]
    alts = [folded.get(s, s) for s in _named_schemes(alt_text)] if alt_text else []
    if alt_text and not alts and _BARE_FOCUS_WORD.search(alt_text):
        alts = ["Focus Plus", "Focus Legacy"]
    alts = [a for a in dict.fromkeys(alts) if a in _SCHEME_FIT and a not in given]
    x = given[0] if given and given[0] in _SCHEME_FIT else None
    if x is None:
        return None

    was_pick = _PICK_OVERVIEW_PREFIX in (last.question or "")
    was_rec = (last.answer or "").startswith(_RECOMMENDATION_LEAD)
    if was_pick:
        why = (f"I picked **{x}** only because you left the choice to me and it was the "
               "first scheme on my list we hadn't covered yet — it isn't a ranking, and it "
               "doesn't mean it suits you best.")
    elif was_rec:
        # Where each scheme actually sat in that list — the question may assume
        # one was left out when it was in fact ranked higher.
        listed = re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", last.answer or "", re.MULTILINE)
        rank = {s: i + 1 for i, s in enumerate(listed)}
        if x in rank and len(listed) > 1:
            why = (f"**{x}** was number {rank[x]} of {len(listed)} in my suggestions, not the "
                   f"only one — it is there for {_SCHEME_FIT[x][0]}: {_SCHEME_FIT[x][1]}")
        else:
            why = (f"I suggested **{x}** because of what you told me about yourself: it is for "
                   f"{_SCHEME_FIT[x][0]} — {_SCHEME_FIT[x][1]}")
        ranked_above = [a for a in alts if a in rank and (x not in rank or rank[a] < rank[x])]
        if ranked_above:
            why += ("\n\nIn fact " + " and ".join(f"**{a}** (number {rank[a]})" for a in ranked_above)
                    + (" was" if len(ranked_above) == 1 else " were")
                    + f" ranked above {x} in that same list.")
            alts = [a for a in alts if a not in ranked_above]
    else:
        why = (f"My last answer drew only on **{x}**'s reference material because our "
               f"conversation was about {x} at that point — I didn't compare it with the "
               "other schemes.")
    parts = [why]
    for a in alts:
        what, who = _SCHEME_FIT[a]
        parts.append(f"**{a}** is for {what}: {who}")
    profile = _user_profile(q) or _latest_profile(session)
    if profile and not was_rec:
        ranked: list[str] = []
        for _k, _d, schemes in profile:
            ranked += [s for s in schemes if s not in ranked]
        about = " and ".join(d for _k, d, _s in profile)
        parts.append(f"From what you've said ({about}), the best match is **{ranked[0]}**"
                     + (f", then {', '.join(ranked[1:3])}" if len(ranked) > 1 else "") + ".")
    elif not profile:
        parts.append("Tell me what you need — paid work, a house, farming support or starting "
                     "a business — and I'll suggest the scheme that fits best.")
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": "\n\n".join(parts), **_empty_data_fields(),
            "schemes": [alts[0] if alts else x]}


def _last_turn_was_pick(session: "Session | None") -> bool:
    last = getattr(session, "last_turn", None) if session is not None else None
    return bool(last and _PICK_OVERVIEW_PREFIX in (last.question or ""))


async def _scheme_pick_answer(question: str, session: "Session | None") -> "dict | None":
    if not (_is_scheme_pick_request(question)
            or (_PICK_CONTINUE.match(question or "") and _last_turn_was_pick(session))):
        return None
    scheme, chosen = _pick_scheme(question, session)
    overview_q = _scheme_overview_question(scheme)
    kb = None
    try:
        kb = await rag.answer_from_kb(overview_q, scheme=scheme)
    except Exception:  # noqa: BLE001 — fall back to the summary below
        logger.warning("scheme pick: knowledge lookup failed for %s", scheme, exc_info=True)
    lead = (f"I'll pick **{scheme}** — here are its key points:" if chosen
            else f"Here are the key points of **{scheme}**:")
    if kb:
        body, conf, sources = kb["answer"], kb["confidence"], kb["sources"]
    else:
        # No passage retrieved (KB unreachable, or nothing scored): still answer
        # the request from the scheme's own one-line summary, never "not covered".
        body, conf, sources = f"- {_SCHEME_USER_SUMMARY[scheme]}", "medium", []
    others = [s for s in _pickable_schemes() if s != scheme]
    tail = ("\n\nWant me to explain another one? I also cover "
            + ", ".join(others[:-1]) + " and " + others[-1] + "." if chosen and others else "")
    logger.info("scheme pick: %r -> %s (chosen_by_bot=%s)", question, scheme, chosen)
    return {"route": "knowledge", "intent": "RAG", "confidence": conf,
            "answer": f"{lead}\n\n{body}{tail}", "sources": sources,
            "rewritten_question": overview_q,
            **_empty_data_fields(), "schemes": [scheme]}


def _scheme_listing_answer(question: str) -> "dict | None":
    explicit = _SCHEME_LISTING_CUE.search(question or "")
    if not explicit:
        if not _SCHEME_HELP_CUE.search(question or ""):
            return None
        # A "help me" ask that already names scheme-specific vocabulary (a
        # house, job cards, farmer cash, an enterprise, ...) has a clear
        # intent to route on — leave it to the normal KNOWLEDGE/RAG path
        # instead of burying that signal under the generic four-scheme list.
        if _infer_scheme_from_terms(question) is not None:
            return None
    lines = _scheme_listing_lines()
    answer = (
        f"I cover {_count_word(len(lines))} Meghalaya government schemes:\n\n" + "\n".join(lines) +
        "\n\nAsk me about any of these — eligibility, benefits, how to apply, "
        "or the actual data (numbers, by district or year)."
    )
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": answer, **_empty_data_fields()}


# "What is the difference between the schemes?" / "compare MGNREGA and PMAY-G" as
# a KNOWLEDGE question — no per-scheme reference doc contains a cross-scheme
# comparison, so plain RAG retrieval reliably comes back "not covered". Compose
# a structured side-by-side directly from _SCHEME_USER_SUMMARY instead.
_SCHEME_COMPARISON_CUE = re.compile(
    r"\bdifference between\b[^?]{0,40}\bschemes?\b|\bschemes?\b[^?]{0,40}\bdiffer(?:ence)?\b|"
    r"\bcompare\b[^?]{0,40}\bschemes?\b|\bhow (?:do|does)\b[^?]{0,40}\bschemes?\b[^?]{0,20}\bdiffer\b|"
    r"\bdifference between\b.{0,60}\b(mgnrega|pmay|focus\s*plus|cm\s*elevate)\b.{0,20}\b(and|vs\.?|versus)\b",
    re.IGNORECASE,
)
# "compare Focus Plus and CM Elevate schemes BY BENEFICIARIES" / "... by amount" /
# "... by expenditure" names a real figure to pull from megh_db, not "how do the
# schemes differ conceptually" — _SCHEME_COMPARISON_CUE's bare "compare ... schemes"
# still matches that (it doesn't require a metric to be absent), so without this
# guard _scheme_comparison_answer intercepts a genuine statistics question at step
# 0-a, before classify_intent / DATA routing ever runs, and answers it with the
# canned RAG-style scheme blurb instead of a queried number.
_SCHEME_COMPARISON_METRIC_GUARD = re.compile(
    r"\b(beneficiar\w*|amount|amounts|expenditure|spend\w*|wages?|"
    r"person[\s-]?days?|job\s?cards?|houses?|disburs\w*|payments?|"
    r"number of|how many|how much|count of|total|percentage|per ?cent|"
    r"\brate\b)\b",
    re.IGNORECASE,
)


def _scheme_comparison_clarification() -> "ClarificationNeeded":
    names = list(SCHEME_CATALOG)
    options = [
        {"label": f"{a} vs {b}", "question": f"difference between {a} and {b}"}
        for a, b in itertools.combinations(names, 2)
    ]
    _all = ", ".join(names[:-1]) + " and " + names[-1]
    _any = ", ".join(names[:-1]) + ", or " + names[-1]
    options.append({"label": f"All {len(names)} schemes",
                     "question": f"difference between {_all}"})
    return ClarificationNeeded(
        f"Which schemes would you like to compare — {_any}? Pick a pair, or compare "
        f"all {len(names)}.",
        options=options,
        rule="scheme-comparison-not-specified",
    )


def _scheme_comparison_answer(question: str) -> "dict | None":
    if not _SCHEME_COMPARISON_CUE.search(question or ""):
        return None
    if _SCHEME_COMPARISON_METRIC_GUARD.search(question or ""):
        return None  # names a real figure — let classify_intent send it to DATA
    named = _named_schemes(question)
    if len(named) == 1 or (not named and _infer_scheme_from_terms(question) is not None):
        # The question already resolves to exactly one of the 4 top-level schemes
        # — this is a WITHIN-scheme comparison ("compare Piggery and Poultry
        # schemes under CM Elevate", "compare the three PRIME schemes"), not a
        # cross-scheme "MGNREGA vs PMAY-G" ask. CM Elevate's 15 sub-units are
        # themselves called "schemes", so the bare _SCHEME_COMPARISON_CUE word
        # "scheme(s)" fires here too; asking "which of the 4 schemes?" is
        # nonsensical when the question already named the one it means (confirmed
        # live 2026-09-11: "compare Piggery and Poultry schemes under CM Elevate"
        # raised the 4-way clarification instead of running the sub-scheme
        # comparison in cmelevate_few_shot.yaml). Let normal DATA routing handle it.
        return None
    if len(named) < 2:
        raise _scheme_comparison_clarification()
    targets = named
    lines = [f"- **{name}** — {_SCHEME_USER_SUMMARY.get(name, '')}" for name in targets]
    answer = (
        "Here's a high-level comparison:\n\n" + "\n".join(lines) +
        "\n\nAsk about a specific one — eligibility, benefits, or the application "
        "process — for more detail."
    )
    return {"route": "knowledge", "intent": "RAG", "confidence": "high", "sources": [],
            "answer": answer, **_empty_data_fields()}


# ── Named a scheme we simply don't hold ─────────────────────────────────────
# A user can reasonably name another Meghalaya / central scheme ("PM-KISAN",
# "Ujjwala", "Jal Jeevan"). The right answer is not the generic "which scheme?"
# prompt — it's to say plainly that only four schemes are loaded right now,
# then let them pick one. Keep this list to real scheme names/aliases; a bare
# word that is also scheme vocabulary must not appear. CM Elevate is now a
# supported scheme (see _SCHEME_NAME_PATTERN) and must NOT appear here.
_UNSUPPORTED_SCHEME = re.compile(
    r"\b("
    r"prime\s+meghalaya|meghalaya\s+prime|"
    r"pm[\s-]?kisan|pmkisan|kisan\s+samman|"
    r"pmay[\s-]?u\b|pmay\s+urban|awas\s+yojana\s+urban|"
    r"pmgsy|gram\s+sadak|"
    r"pmuy|ujjwala|ujwala|"
    r"saubhagya|"
    r"jal\s+jeevan(\s+mission)?|jjm\b|har\s+ghar\s+jal|nal\s+se\s+jal|"
    r"ayushman(\s+bharat)?|pm[\s-]?jay|pmjay|"
    r"mhis\b|cmhis\b|megha\s+health|"
    r"nsap\b|ignoaps|old[\s-]?age\s+pension|widow\s+pension|disability\s+pension|"
    r"nrlm\b|day[\s-]?nrlm|aajeevika|ajeevika|livelihoods?\s+mission|"
    r"swachh\s+bharat|sbm\b|nirmal\s+bharat|"
    r"mid[\s-]?day\s+meal|pm[\s-]?poshan|"
    r"icds\b|anganwadi|poshan\s+abhiyaan?|"
    r"kanyashree|ladli|sukanya|"
    r"atal\s+pension|apy\b|"
    r"mudra|pmmy\b|stand[\s-]?up\s+india|"
    r"kisan\s+credit\s+card|kcc\b|"
    r"fasal\s+bima|pmfby|crop\s+insurance|"
    r"e[\s-]?shram|"
    r"nfsa\b|one\s+nation\s+one\s+ration|public\s+distribution|ration\s+card"
    r")\b",
    re.IGNORECASE,
)


def _unsupported_scheme_named(question: str) -> "str | None":
    """The name of a scheme the user asked about that we don't hold — or None.
    Only fires when NO supported scheme is also named, so mixed questions
    ("compare CM Elevate with MGNREGA") still route normally. The unsupported
    match is blanked out before the supported-scheme check so a near-miss like
    "PMAY-U" isn't swallowed by the PMAY-G pattern."""
    m = _UNSUPPORTED_SCHEME.search(question or "")
    if not m:
        return None
    without = (question[:m.start()] + " " + question[m.end():])
    if any(p.search(without) for p in _SCHEME_NAME_PATTERN.values()):
        return None
    return m.group(0)


def _unsupported_scheme_clarification(question: str, name: str) -> "ClarificationNeeded":
    # Drop the unsupported scheme name (plus any preposition left dangling by
    # its removal, e.g. "... years in pm-kisan?" -> "... years") so the
    # resumed one-tap question reads cleanly.
    stem = re.sub(re.escape(name), " ", question, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip().rstrip(" ?.")
    stem = re.sub(r"\s+(?:in|for|of|under|from|about|on)$", "", stem, flags=re.IGNORECASE).strip()
    stem = stem or question.strip().rstrip(" ?.")
    options = [
        {"label": "MGNREGA (rural employment)",
         "question": _scheme_option_question(stem, "MGNREGA")},
        {"label": "PMAY-G (rural housing)",
         "question": _scheme_option_question(stem, "PMAY-G")},
        {"label": "Focus Plus (farmer cash benefit)",
         "question": _scheme_option_question(stem, "Focus Plus")},
        {"label": "CM Elevate (livelihood / enterprise schemes)",
         "question": _scheme_option_question(stem, "CM Elevate")},
        {"label": "Focus Legacy (producer group payments)",
         "question": _scheme_option_question(stem, "Focus Legacy")},
        {"label": "CM Elevate Legacy (sanctions & disbursements)",
         "question": _scheme_option_question(stem, "CM Elevate Legacy")},
        {"label": "Compare across schemes",
         "question": (f"{stem} across MGNREGA, PMAY-G, Focus Plus, CM Elevate, "
                      f"Focus Legacy and CM Elevate Legacy")},
    ]
    # Names the scheme asked for, says plainly that it is outside what is
    # loaded, then points somewhere useful. Deliberately about SCHEME COVERAGE
    # rather than "no data": the user asked about a real government scheme that
    # simply isn't one of the four here, and "I don't have information about
    # that" reads as though the assistant is broken rather than out of scope
    # (reported 2026-09-18).
    # Echo the scheme name back in a presentable form — the matched text is
    # whatever the user typed ("ujjwala", "pm kisan"), and a reply that opens
    # with a lowercase scheme name reads careless. An all-caps acronym
    # ("PM-KISAN", "JJM") is already right and left alone.
    _display = " ".join(w if w.isupper() else w.capitalize()
                        for w in name.strip().split())
    return ClarificationNeeded(
        f"{_display} isn't one of the schemes I cover, so I can't answer questions "
        "about it — not its rules, eligibility or its data. I cover these Meghalaya "
        "schemes: MGNREGA (rural employment), PMAY-G (rural housing), Focus Plus "
        "(farmer cash benefit), CM Elevate (livelihood and enterprise applications), "
        "Focus Legacy (producer group payments) and CM Elevate Legacy (CM-ELEVATE "
        "sanctions and disbursements). "
        "If one of those is what you need, pick it below and I'll take the question "
        "from there.",
        options=options,
        rule="scheme-not-available",
    )


# ── Bank / financial-channel details — not held for most loaded schemes ─────
# Focus Plus now exposes bank_name_raw via curated.v_focus_plus (added so
# bank-wise disbursement questions can be answered), but it has no
# account-number/IFSC field, so those specific asks still need the
# not-held explanation. PMAY-G, CM Elevate and MGNREGA never captured any
# bank field at ingest at all. Without this check an account/IFSC question
# reaches SQL generation, which correctly finds no column to use but can only
# hand back a bare "not available in this data" — true, but it doesn't say
# *why*, so route it to a real explanation up front.
_BANK_REQUESTED = re.compile(
    r"\bbanks?\b|\bbanking\b|\bifsc\b|\bbank[\s-]?account|\baccount[\s-]?number|"
    r"\bbank[\s-]?wise\b|\bbank[\s-]?transfer|\bdbt\b|\blifcom\b",
    re.IGNORECASE,
)
# Focus Plus only lacks account-number/IFSC fields — bare bank-name questions
# ("which bank", "bank-wise disbursement") should reach SQL generation instead.
_BANK_ACCOUNT_DETAIL_REQUESTED = re.compile(
    r"\bifsc\b|\bbank[\s-]?account|\baccount[\s-]?number|\bbank[\s-]?transfer|"
    r"\bdbt\b",
    re.IGNORECASE,
)
# CM Elevate Legacy's loan_entity holds only the categories 'Bank' / 'LIFCOM'.
# These ask for an individual bank or branch, which is not recorded.
_LENDER_DETAIL_REQUESTED = re.compile(
    r"\bbranch(?:es)?\b|\bbank[\s-]?names?\b|\bname of (?:the )?banks?\b|"
    r"\bwhich (?:particular |specific )?banks?\b|\bsbi\b|\bstate bank\b|\bmrb\b|"
    r"\bmeghalaya rural bank\b|\bcooperative bank\b",
    re.IGNORECASE,
)
_BANK_NOT_HELD_TEXT = {
    "Focus Plus": (
        "Account numbers and IFSC codes aren't held for Focus Plus — only the "
        "bank name is. Shall I answer using bank name instead?"
    ),
    "CM Elevate": (
        "There is no loan-channel field in the CM Elevate applications data — Bank and "
        "LIFCOM are recorded only in CM Elevate Legacy (the sanction and disbursement "
        "records). Shall I answer from CM Elevate Legacy, or on applications by scheme "
        "or district instead?"
    ),
    "CM Elevate Legacy": (
        "Individual bank names, branches, IFSC codes and account numbers are not held "
        "for CM Elevate Legacy — the only lender values recorded are 'Bank' and "
        "'LIFCOM'. Shall I show loans by those two lender categories instead?"
    ),
    "PMAY-G": (
        "Bank transfer status, DBT outcomes and account details are not held for "
        "PMAY-G — only the amount released and the number of installments are. "
        "Shall I use those instead?"
    ),
    "MGNREGA": (
        "Bank details are not held for MGNREGA — only wages, person-days, job "
        "cards and expenditure are. Shall I answer using one of those instead?"
    ),
}
_BANK_GENERIC_TEXT = (
    "Bank name is only held for Focus Plus and Focus Legacy — MGNREGA, PMAY-G "
    "and CM Elevate never captured a bank field at ingest. Focus Legacy also "
    "holds the IFSC code; no scheme exposes account numbers. Shall I answer "
    "using Focus Plus or Focus Legacy bank name, or by scheme, district, block "
    "or village instead?"
)


def _named_or_inferred_schemes(question: str) -> list[str]:
    named = [s for s, rx in _SCHEME_NAME_PATTERN.items() if rx.search(question)]
    return named or _infer_scheme_from_terms(question) or []


def _bank_clarification(question: str) -> "ClarificationNeeded | None":
    schemes = _named_or_inferred_schemes(question)
    is_account_detail = bool(_BANK_ACCOUNT_DETAIL_REQUESTED.search(question))
    if not is_account_detail and schemes == ["Focus Plus"]:
        # Bare bank-name question scoped to Focus Plus only — bank_name_raw is
        # queryable via curated.v_focus_plus, so let SQL generation handle it.
        return None
    if schemes == ["CM Elevate Legacy"] and not is_account_detail \
            and not _LENDER_DETAIL_REQUESTED.search(question):
        # "loans by Bank vs LIFCOM", "how many LIFCOM loans" — loan_entity is a
        # real, queryable lender column there. Only a named bank / branch /
        # account asks for something the data does not hold.
        return None
    text = _BANK_NOT_HELD_TEXT[schemes[0]] if len(schemes) == 1 else _BANK_GENERIC_TEXT
    return ClarificationNeeded(text, rule="column-not-held")


# ── Administrative expenditure — deliberately excluded from reporting ──────
# The raw source has adm_exp_rec / adm_exp_non_rec / adm_exp_total (MGNREGA) and
# an equivalent programme-expenditure figure (PMAY-G), but both are intentionally
# left out of the curated views/facts (data/mgnrega/README.md marks them
# **excluded**; mgnrega_classification_rules.yaml and pmay_classification_rules.yaml
# both define a dedicated "administrative_expenditure_requested" condition for
# exactly this). That YAML condition was never wired into the live pipeline
# (see the "deliberately NOT loaded here yet" note in annotations.py), so without
# this check the question reached SQL generation, which has no such column to
# read and either errors or returns a wrong/empty figure instead of explaining
# the exclusion.
_ADMIN_EXPENDITURE_REQUESTED = re.compile(
    r"\badministrative\s+(?:expenditure|expense|cost|spend(?:ing)?)\b|"
    r"\badmin\s+(?:expenditure|expense|cost)\b|\boverhead\s+(?:expenditure|cost)s?\b|"
    r"\bprogramme\s+expenditure\b|\bprogram\s+expenditure\b",
    re.IGNORECASE,
)
# The MGNREGA wording says the figure is EMPTY rather than absent, because that
# is what the data actually shows. curated.v_expenditure does expose
# admin_total_exp, but it is non-zero on exactly ONE of its 18,818 rows (₹0.90
# lakh, Alokdia / DEMDEMA block / FY 2023-24) against ₹362,866 lakh of total
# expenditure — docs/schema_for_developers.md records it as "effectively
# unpopulated". Saying it "isn't held" was misleading: a user who checks the
# schema finds the column and reasonably concludes the assistant is wrong
# (reported 2026-09-17, "admin expenditure in demdema"). Reporting the real
# 0.00 would be worse — it reads as a measured finding rather than an empty
# column — so the honest answer names the column, says it was never populated,
# and offers the figures that were.
_ADMIN_EXPENDITURE_NOT_HELD_TEXT = {
    "MGNREGA": (
        "MGNREGA's administrative expenditure column exists but was never populated "
        "at ingest — it is zero on every row but one in the whole state, so there is "
        "no real figure to report{scope}. The expenditure that IS recorded is "
        "unskilled wage, semi-skilled wage, material and total. Did you mean total "
        "expenditure instead?"
    ),
    "PMAY-G": (
        "Administrative and programme expenditure are not held for PMAY-G{scope} — only "
        "the per-house sanctioned and released amounts are. Did you mean amount released "
        "instead?"
    ),
}
_ADMIN_EXPENDITURE_GENERIC_TEXT = (
    "There is no usable administrative-expenditure figure{scope} in any scheme I cover. "
    "MGNREGA has the column but it was never populated (zero on every row but one "
    "statewide), and PMAY-G records only sanctioned and released amounts. Shall I "
    "answer using wage, material or total expenditure instead?"
)


# The place the question asked about, so the reply can say "…for Demdema"
# rather than answering in the abstract. Deliberately a light touch: the name
# is echoed back as the user typed it (title-cased), NOT resolved — this check
# runs long before resolve_entities, and a refusal about an empty column is the
# same refusal at any admin level, so there is nothing to disambiguate.
_ADMIN_EXP_PREP = r"(?:in|for|at|of|under|from|within)"
_ADMIN_EXP_PLACE_RE = re.compile(
    rf"\b{_ADMIN_EXP_PREP}\s+(?:{_ADMIN_EXP_PREP}\s+)*"
    r"(?P<name>[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})\s*[?.!]*\s*$",
    re.IGNORECASE,
)
_ADMIN_EXP_NOT_A_PLACE = {
    "mgnrega", "mnrega", "nrega", "pmay", "pmay g", "pmayg", "awaas", "awas",
    "focus plus", "focusplus", "cm elevate", "cmelevate", "meghalaya",
    "the state", "each district", "every district", "all districts",
}


def _admin_expenditure_scope(question: str) -> str:
    """' in Demdema' when the question names a place, else ''."""
    # Strip any scheme name first. "…for PMAY-G in Tura" otherwise lets the
    # trailing-phrase match start at "PMAY-G" and produce "for Pmay-G In Tura",
    # which also duplicates the scheme the sentence has already named.
    text = question or ""
    for rx in _SCHEME_NAME_PATTERN.values():
        text = rx.sub(" ", text)
    m = _ADMIN_EXP_PLACE_RE.search(re.sub(r"\s+", " ", text).strip())
    if not m:
        return ""
    name = m.group("name").strip().rstrip(".,")
    if name.lower() in _ADMIN_EXP_NOT_A_PLACE or len(name) < 3:
        return ""
    return f" in {name.title()}"


def _admin_expenditure_clarification(question: str) -> "ClarificationNeeded":
    schemes = _named_or_inferred_schemes(question)
    if len(schemes) == 1 and schemes[0] in _ADMIN_EXPENDITURE_NOT_HELD_TEXT:
        text = _ADMIN_EXPENDITURE_NOT_HELD_TEXT[schemes[0]]
    else:
        text = _ADMIN_EXPENDITURE_GENERIC_TEXT
    return ClarificationNeeded(text.format(scope=_admin_expenditure_scope(question)),
                               rule="column-not-held")


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
    # A superlative asked ACROSS schemes ("which scheme spent the most?") names
    # the scheme as its ANSWER — pausing to ask which scheme is meant would be
    # asking the user for the thing they came to find out.
    if _CROSS_SCHEME_SUPERLATIVE.search(question or ""):
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
    r"|\blist\s+(?:of\s+)?all\b|\bfor\s+all\b|\bacross\s+all\b|\bno limit\b|\bevery row\b|"
    # A free-text reply to the "top 3/5/10 or the complete list?" pause — the
    # dimension word (blocks/districts/...) already sits earlier in the merged
    # question, not right after "all", so these stand on their own instead of
    # requiring one of the words above within 20 chars.
    r"\ball of (?:them|it)\b|\beverything\b|"
    r"\b(?:show|give)\s+(?:me\s+)?all\b|\bthe\s+(?:complete|full)\s+list\b|"
    # A bare "all" as the whole reply, merged on as the trailing comma-fragment
    # ("...sanctioned in 2023-24, all") — anchored to end-of-string so an
    # unrelated mid-question "all" (e.g. "all of Meghalaya") isn't caught here;
    # that shape is already handled by the dimension-adjacent alternative above.
    r",\s*all\s*[?.]?\s*$",
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
    r"instal{1,2}ments?|disbursements?|utili[sz]ation)\b",
    re.IGNORECASE,
)
# The question already fixes its own scope (a breakdown, a trend, a comparison
# across a dimension, or an explicit "all of Meghalaya") — nothing to ask.
_HAS_BREAKDOWN = re.compile(
    r"\bby (?:district|block|village|panchayat|gp|year|month|scheme|program(?:me)?|sector)\b|"
    r"\b(?:per|each|every|for all|across all|all the) "
    r"(?:district|block|village|panchayat|year|month|scheme|program(?:me)?|sector)s?\b|"
    # "under each CM ELEVATE program", "for every PMAY-G scheme" — a scheme name
    # or other short qualifier can sit between "each/every" and the dimension
    # word itself; allow up to a few words of slack for scheme/program/sector
    # specifically (kept out of the tight pattern above to avoid over-matching
    # "each ... district" style geography phrasing where slack isn't needed).
    r"\b(?:per|each|every)\b[^?.!]{0,25}\b(?:scheme|program(?:me)?|sector)s?\b|"
    # "in each financial year", "total disbursed through each loan entity" —
    # a qualifier before "year", and category dimensions the tight pattern
    # above never listed. Both are groupings over every value, so asking
    # "which area / year?" first was wrong, and the "all years" reply then got
    # collapsed into ONE total instead of a per-year split (CM Elevate Legacy
    # QA TC-21 / TC-22 / TC-23 / TC-24, 2026-09-25).
    r"\b(?:per|each|every|by)\s+(?:financial|fiscal)\s+years?\b|"
    r"\b(?:per|each|every|by)\s+(?:loan\s+)?(?:entit(?:y|ies)|lenders?|categor(?:y|ies)|"
    r"tranches?|instal{1,2}ments?)\b|"
    r"\b(?:district|block|village|year|scheme|program(?:me)?|sector)[\s-]?wise\b|"
    r"\bbreak[\s-]?down\b|\btrend\b|\byear[\s-]?on[\s-]?year\b|"
    r"\bover (?:the )?(?:last|past) \w+ years?\b|"
    r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|"
    r"\bcorrelat\w*|\brelationship between\b|\blinked (?:to|with)\b|"
    r"\b(?:districts?|blocks?|villages?|panchayats?|gps?)\s+"
    r"(?:with|where|that|having|which)\b|"
    # A rank window over a geography dimension — "top 5 districts", "bottom
    # blocks", "10 largest villages". It inherently spans every area in that
    # dimension, so geography is already scoped; only the year is still open.
    r"\b(?:top|bottom|leading|largest|biggest|smallest|highest|lowest)\s+"
    r"(?:\d+\s+)?(?:districts?|blocks?|villages?|panchayats?|gps?|schemes?|program(?:me)?s?|sectors?)\b|"
    r"\b\d+\s+(?:largest|biggest|smallest|highest|lowest)\s+"
    r"(?:districts?|blocks?|villages?|panchayats?|gps?|schemes?|program(?:me)?s?|sectors?)\b|"
    # "which district received the highest ...", "district with the lowest
    # ..." — the same rank-window logic as "top N districts" above, just
    # phrased as "which <geo> ... <superlative>" instead of "<superlative>
    # <geo>". Still inherently spans every area in the dimension, so
    # geography is already scoped; only the year is still open. Anchored
    # loosely (superlative anywhere within ~40 chars either side of the
    # geography noun) so "which district received the highest total
    # disbursement" and "highest total expenditure in which district" both
    # match.
    r"\bwhich (?:district|block|village|panchayat|gp)s?\b[^?]{0,40}\b"
    r"(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b|"
    r"\b(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b[^?]{0,40}\bwhich (?:district|block|village|panchayat|gp)s?\b|"
    # Same rank-window shape as above, but for scheme/program/sector — a scheme
    # name or other qualifier ("CM Elevate", "PMAY-G") often sits between
    # "which" and the dimension word itself ("which CM Elevate programs have
    # the highest..."), so this variant allows slack there too.
    r"\bwhich\b[^?.!]{0,25}\b(?:schemes?|program(?:me)?s?|sectors?)\b[^?]{0,40}\b"
    r"(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b|"
    r"\b(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b[^?]{0,40}\bwhich\b[^?.!]{0,25}\b(?:schemes?|program(?:me)?s?|sectors?)\b|"
    r"\bacross (?:the )?(?:districts?|blocks?|villages?|panchayats?|state|years?|schemes?|program(?:me)?s?|sectors?)\b",
    re.IGNORECASE,
)
_EXPLICIT_STATEWIDE = re.compile(
    r"\b(?:in|for|across|over|of) (?:all of |the (?:whole|entire) )?meghalaya\b|"
    r"\bstate[\s-]?(?:wide|level|total)\b|\boverall\b|\bin total\b|\bgrand total\b|"
    r"\ball (?:the )?(?:years|districts|blocks|villages)\b|\bentire state\b|"
    r"\bevery year\b|\bsince inception\b|\ball[\s-]?time\b|"
    r"\b(?:up )?(?:to|till|until) (?:date|now)\b|\bso far\b|\bcumulative\b",
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
    if any(resolved.get(k) for k in
           ("district", "district_list", "block", "block_list", "village_code",
            "village_code_list", "assembly_constituency", "year_key")):
        return False
    return True


# A reply to the scope pause is normally a bare fragment ("Ri Bhoi, 2023-24").
# These shapes instead mean the user dropped the earlier question and asked a
# fresh one — don't fold them into the original.
_REPLY_IS_NEW_QUESTION = re.compile(
    r"\b(how many|how much|number of|count of|what(?:'s| is| are| was) the|"
    r"who (?:is|can)|how do i|how to apply|what documents?|which documents?|"
    r"explain|define|difference between|tell me about)\b|"
    # A reply OPENING with "which ..." is a question of its own, not a place or
    # a year — "Which banks handle Focus Legacy payments?" was being merged into
    # the stale paused question. Anchored to the start so "the block, which is
    # in Ri Bhoi" is unaffected.
    r"^\s*which\b",
    re.IGNORECASE,
)


# A reply that OPENS with one of these is giving an instruction, not naming a
# place or a year. _REPLY_IS_NEW_QUESTION covers interrogative phrasings only,
# so an imperative slipped through and got glued onto the stale paused question
# ("List top 5 PGs which has more than 10 of members." came back as the previous
# turn's Nongstoin block-or-village prompt — reported 2026-09-23).
_REPLY_IS_IMPERATIVE = re.compile(
    r"^\s*(?:please\s+)?(?:list|show|give|display|rank|compare|find|search)\b",
    re.IGNORECASE,
)

# A scope pause asks for exactly a place and/or a year, so that vocabulary is
# the EXPECTED reply and must never read as "a new question". Stripping it out
# before the metric test is what separates "West Garo Hills, 2023-24" (a scope
# reply — nothing left once place and year are removed) from "Total amount
# disbursed by district" (a real question — the metric survives). An earlier
# version tested only for a BARE year and wrongly abandoned the pause on the
# commonest reply shape of all, place-plus-year.
_SCOPE_REPLY_VOCAB = re.compile(
    r"\b(?:fy\s*)?\d{4}(?:\s*-\s*\d{2,4})?\b|"           # 2023-24, FY 2021-22
    r"\ball\s+(?:of\s+)?meghalaya\b|\ball\s+years?\b|\ball\s+districts?\b|"
    r"\bstatewide\b|\bthe\s+(?:block|village|district|constituency)\b|"
    r"\bnot\s+(?:the|another)\b|\barea\s+type\b|\bcombined\b|"
    r"[,\.]",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=1)
def _known_place_names() -> tuple[str, ...]:
    """Every district and block name the resolvers know, lowercased, longest
    first. Used only to strip a place out of a scope reply before testing it for
    a metric — so "West Garo Hills" cannot be mistaken for question vocabulary.
    Built from the catalogues already loaded at startup; empty if they are not,
    in which case the test simply falls back to the year/phrase stripping."""
    from app import entity_resolver

    names: set[str] = set()
    for scheme in list(entity_resolver._catalog):
        for dim in ("district", "block"):
            for v in entity_resolver._catalog.get(scheme, {}).get(dim, []):
                c = str(v.get("canonical") or "").strip().lower()
                if len(c) >= 4:
                    names.add(c)
    return tuple(sorted(names, key=len, reverse=True))


def _reply_abandons_scope_pause(reply: str) -> bool:
    """True when the reply to a 'which area / year?' pause is itself a new,
    self-standing question rather than the scope fragment we asked for."""
    r = (reply or "").strip()
    if len(r.split()) > 12:
        return True
    if _REPLY_IS_NEW_QUESTION.search(r) or _KNOWLEDGE_HINTS.search(r):
        return True
    if _REPLY_IS_IMPERATIVE.search(r):
        return True
    # A scope fragment names a place and/or a year and nothing else. Strip that
    # expected vocabulary, plus any place NAME the resolver knows, and see
    # whether a METRIC survives: if one does, the reply is a question in its own
    # right whatever its grammatical shape (the same signal the DATA router
    # trusts). "West Garo Hills, 2023-24" reduces to nothing and still merges.
    residue = _SCOPE_REPLY_VOCAB.sub(" ", r)
    for place in _known_place_names():
        if place in residue.lower():
            residue = re.sub(re.escape(place), " ", residue, flags=re.IGNORECASE)
    if _DATA_HINTS.search(residue):
        return True
    return False


# ── "Which district in this region?" clarification ─────────────────────────
# "Garo Hills" / "Khasi Hills" / "Jaintia Hills" name a hill RANGE, not a district —
# each covers several districts (region_groupings in the *_entity_resolver.yaml). When
# a question names one and no specific district, offer its districts as one-tap chips
# instead of the generic "which area?" free-text pause. Applies to every scheme.
def _swap_region_phrase(question: str, aliases: list[str], repl: str) -> str:
    """Replace the first hill-range phrase in `question` with `repl`. Longest alias
    first so 'South West Garo Hills' isn't half-matched by 'Garo Hills'."""
    forms = sorted({a for a in aliases if a}, key=len, reverse=True)
    pat = re.compile(r"\b(" + "|".join(re.escape(a) for a in forms) + r")\b", re.IGNORECASE)
    new, n = pat.subn(repl, question, count=1)
    return new if n else f"{question.rstrip(' ?.')} for {repl}"


def _region_clarification(question: str, region: dict) -> "ClarificationNeeded":
    canon = region["canonical"]
    dists = region["districts"]
    aliases = [canon, *region.get("aliases", [])]
    all_list = ", ".join(dists[:-1]) + " and " + dists[-1]
    options = [{"label": d, "question": _swap_region_phrase(question, aliases, d)}
               for d in dists]
    options.append({
        "label": f"All of {canon} combined",
        "question": _swap_region_phrase(question, aliases, f"all of {canon}"),
    })
    return ClarificationNeeded(
        f"“{canon}” is a region covering {len(dists)} districts "
        f"— {all_list}. Which district, or all of {canon} combined?",
        options=options,
        rule="region-needs-district",
    )


def _scope_clarification(question: str, schemes: list[str]) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    # District chips first (picking one still leaves the year open, so it falls
    # straight into `_year_clarification` on the next turn — a second round of
    # buttons rather than free text), then the one-tap statewide/all-years
    # shortcut, mirroring how `_year_clarification` appends its own "combined"
    # option last.
    seen: dict[str, None] = {}
    for s in schemes or []:
        for d in all_districts(s):
            seen.setdefault(d, None)
    # Schemes with NO time dimension at all (CM Elevate — see
    # _needs_year_clarification's identical check) still need the AREA half of
    # this ask (a bare "how many applications" is a real, useful district-level
    # question, and CM Elevate's own few-shot corpus has district-scoped
    # examples) — but asking "and which financial year?" on top is nonsensical
    # when no year exists anywhere on the fact. Reword rather than skip the
    # gate outright (an earlier pass tried skipping it entirely and lost the
    # district ask too — see cmelevate-routing-gates-bug memory).
    live = [s for s in (schemes or []) if s in _SCHEME_DATA_YEARS]
    area_only = bool(live) and all(not _SCHEME_DATA_YEARS[s] for s in live)
    if area_only:
        options = [{"label": d, "question": f"{stem} for {d}"} for d in seen]
        options.append({
            "label": "All of Meghalaya",
            "question": f"{stem} for all of Meghalaya",
        })
        return ClarificationNeeded(
            "Which area should the answer cover — a specific district, block, or "
            "village? Pick a district below, or choose the statewide total; you can "
            "also just type a block or village name.",
            options=options,
            rule="scope-not-specified",
        )
    options = [{"label": d, "question": f"{stem} for {d}"} for d in seen]
    options.append({
        "label": "All of Meghalaya, all years",
        "question": f"{stem} for all of Meghalaya, all years",
    })
    return ClarificationNeeded(
        "Which area and time period should the answer cover — a specific district, "
        "block, or village, and which financial year? Pick a district below, or "
        "choose the statewide, all-years total; you can also just type a block or "
        "village name (for example, \"West Garo Hills, 2023-24\").",
        options=options,
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
# Focus Plus holds only FY 2022-23 (Tranch 1) and FY 2025-26 (Tranches 2-4) — the two
# middle years are empty for it (focusplus_schema_partitions.yaml). A three-year gap is
# expected, and asking a Focus Plus question for FY 2023-24 / 2024-25 correctly triggers
# the "that year isn't in the data" guard.
# CM Elevate is DIFFERENT IN KIND, not just sparser: it holds an empty list on
# purpose. There is no year_key, no FK to dim_year, no date column of any kind
# anywhere in fact_cm_elevate_application (cmelevate_schema_partitions.yaml
# semantic_rules.time_rule) — refresh_scheme_years() below does not even probe
# it. _needs_year_clarification and _year_out_of_range_clarification both treat
# an empty list here as "no time dimension exists" and refuse a time filter
# outright instead of offering year chips.
_SCHEME_DATA_YEARS: dict[str, list[str]] = {
    "MGNREGA": ["2022-23", "2023-24", "2024-25", "2025-26"],
    "PMAY-G":  ["2017-18", "2018-19", "2019-20", "2020-21", "2021-22", "2022-23", "2023-24"],
    "Focus Plus": ["2022-23", "2025-26"],
    "CM Elevate": [],
    # Focus Legacy holds FOUR years with a HOLE IN THE MIDDLE: FY2023-24 has no
    # payments at all — an absent year, not a zero one (focuslegacy_schema_
    # partitions.yaml semantic_rules.time_gap_rule). It is deliberately NOT
    # listed, so the year chips never offer it and the out-of-range guard
    # correctly refuses a FY2023-24 question instead of returning an empty total.
    "Focus Legacy": ["2021-22", "2022-23", "2024-25", "2025-26"],
    # CM Elevate Legacy holds TWO years. 395 records (both Sericulture schemes)
    # carry no year at all — they are not a third year, so they get no chip; an
    # "all financial years" answer still includes them because it applies no
    # year filter (cmelevatelegacy_schema_partitions.yaml year_key rules).
    "CM Elevate Legacy": ["2024-25", "2025-26"],
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
        "Focus Plus": (
            "SELECT DISTINCT year_key FROM curated.v_focus_plus WHERE year_key IS NOT NULL"
        ),
        "Focus Legacy": (
            "SELECT DISTINCT year_key FROM curated.v_focus_legacy WHERE year_key IS NOT NULL"
        ),
        "CM Elevate Legacy": (
            "SELECT DISTINCT year_key FROM curated.v_cm_elevate_disbursement "
            "WHERE year_key IS NOT NULL"
        ),
        # CM Elevate deliberately has no entry here — curated.v_cm_elevate has no
        # year_key column at all (not merely NULL), so there is nothing to probe.
        # Its default of [] in _SCHEME_DATA_YEARS above is the permanent value.
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
    r"\ball[\s-]?time\b|\bsince inception\b|"
    r"\b(?:up )?(?:to|till|until) (?:date|now)\b|\bso far\b|\bcumulative\b|"
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
    r"instal{1,2}ments?|disbursements?|utili[sz]ation|completion rate|success rate|"
    # Focus Plus's own metric nouns — same gap as _DATA_HINTS (see 2026-09-11
    # fix note there): without these, a bare "<scheme> beneficiaries?" matched
    # neither this cue nor _MENTIONS_TRANCHE_WORD, so both the year AND
    # tranche clarification gates were skipped and an unscoped question
    # reached the SQL generator with no year/tranche pin at all — which then
    # sometimes guessed a specific tranche on its own instead of correctly
    # reasoning "no tranche named -> every tranche".
    r"beneficiar\w*|payments?|"
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
    # Schemes with NO time dimension at all (CM Elevate has no year_key, no date
    # column of any kind) can never be asked "which year" — there is nothing to
    # choose. Skip the whole gate when every scheme in play is one of these, so
    # _year_clarification never runs on an empty _SCHEME_DATA_YEARS[scheme] list
    # (which would otherwise crash indexing years[-1]).
    live = [s for s in (schemes or []) if s in _SCHEME_DATA_YEARS]
    if live and all(not _SCHEME_DATA_YEARS[s] for s in live):
        return False
    # PMAY-G used to be exempted here (beneficiary counts / house status are
    # cumulative-to-date, so "which year?" felt like an unnecessary interruption
    # to an earlier QA pass) and silently defaulted to "all years to date"
    # instead of asking. Reinstated to ask same as MGNREGA, 2026-09-09, by
    # explicit product decision — a plain PMAY-G question with no year now
    # pauses for a FY choice too; say "all years" / "cumulative" explicitly
    # (see _ALL_YEARS_CUE) to get the old default without the prompt.
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
    if len(live) == 1:
        scope_word = live[0]
    elif len(live) == 2:
        scope_word = " and ".join(live)
    else:
        scope_word = ", ".join(live[:-1]) + " and " + live[-1]
    year_list = ", ".join(f"FY {y}" for y in years[:-1]) + f" and FY {years[-1]}"
    return ClarificationNeeded(
        f"{scope_word} data is available for {year_list}. "
        "Which of these is required — a single financial year, or all of them "
        "combined?",
        options=options,
        rule="year-not-specified",
    )


# ── "Which tranche?" clarification (Focus Plus only) ───────────────────────
# Focus Plus's amount_disbursed differs sharply by tranche_label (5,000 for
# Tranch 1, 2,500 for every later tranche — see schema_context.py rule 7), so
# silently summing every tranche together produces a total that reads as a
# single entitlement when it's actually a mix ratio. Mirrors
# _needs_year_clarification / _year_clarification in shape: ask with one-tap
# chips (Tranch 1..4 plus "all combined") rather than guess, unless the
# question already names a tranche, asks for a per-tranche breakdown, or
# explicitly wants every tranche combined.
_TRANCHE_BREAKDOWN_CUE = re.compile(
    r"\bby tranche\b|\btranche[\s-]?wise\b|\bper tranche\b|\beach tranche\b|"
    r"\bacross (?:all )?(?:the )?tranches\b|\btranche breakdown\b|"
    r"\bsplit by tranche\b|\bbreak(?:down|\s+down)? by tranche\b|"
    # "which tranche has the most X" / "what tranche..." asks the data to
    # identify one BY comparing across all of them — the opposite of a
    # question that's missing a tranche pin, so it must not be asked to pick.
    r"\bwhich tranche\b|\bwhat tranche\b",
    re.IGNORECASE,
)
_ALL_TRANCHES_CUE = re.compile(
    r"\ball[\s-]?tranches?\b|\btranches? combined\b|\bcombined tranches?\b|"
    r"\bcumulative\b|\boverall\b|\bin total\b|\bgrand total\b|\ball[\s-]?time\b",
    re.IGNORECASE,
)
# The literal word "tranche"/"tranch" appearing anywhere with no specific
# tranche resolved (resolve_tranche_label found nothing) is itself the
# clearest possible signal that the question is tranche-scoped but doesn't
# say which one — regardless of whether the wording also happens to match
# _METRIC_OR_BREAKDOWN_CUE. That cue was borrowed from the year gate and is
# tuned for money/count metrics ("how much", "disbursements"); it has no
# "status" or "breakdown" vocabulary, so "give me status breakdown for
# tranche?" matched neither cue and silently fell through to the SQL
# generator, which picked one tranche (Tranch 4) on its own with nothing
# to base that choice on.
_MENTIONS_TRANCHE_WORD = re.compile(r"\btranche?s?\b", re.IGNORECASE)

# focus_status / verification_status / gender / occupation are populated ONLY
# on the '12.5K' cohort, which per FOCUS PLUS RULES rule 8 exists ONLY at
# Tranch 4 (schema_context.py _FOCUSPLUS_RULES #4, #8). A question about one of
# these columns has no real "which tranche?" to ask — every other tranche has
# zero such rows, so "all tranches combined" and "Tranch 4" are the same
# answer. Pausing to ask anyway produces a rewritten question ("... across all
# tranches") that then fights the Tranch-4-only constraint during SQL
# generation and burns the repair budget for nothing — skip the gate instead
# and let it hit the existing focus_status few-shots directly (see
# "How many Focus Plus registrations are still pending?" in
# data/focus_plus/focusplus_few_shot.yaml).
_PERSON_LEVEL_COLUMN_CUE = re.compile(
    # Bare "status" is included, not just the "focus status" / "verification
    # status" compounds — schema_context.py rule 13 makes an unqualified
    # "status"/"status breakdown"/"status-wise" mean focus_status by default,
    # which is exactly as cohort-locked as the compound forms. Missing this
    # let "give me the status breakdown for tranche 1" slip past both this
    # gate and _person_level_tranche_conflict straight into a guaranteed-empty
    # query (confirmed live 2026-09-12).
    r"\bstatus\b|\bfocus[\s-]?status\b|\bverification[\s-]?status\b|\bverified\b|\bverification\b|"
    r"\bgender\b|\bfemale\b|\bmale\b|\bwomen\b|\bmen\b|"
    r"\boccupation\b|\bfarmers?\b|"
    r"\bpending\b|\bapproved\b|\brejected\b|\bregistrations?\b",
    re.IGNORECASE,
)

# A tranche label is "Tranch 4 - Feb-March" — the ONLY tranche the 12.5K
# person-level cohort belongs to (schema_context.py rule 4/8). Matches the
# canonical value's leading "Tranch 4" regardless of the month suffix.
_TRANCH4_LABEL_RE = re.compile(r"^tranch\s*4\b", re.IGNORECASE)


def _person_level_tranche_conflict(question: str, schemes: list[str], resolved: dict) -> bool:
    """True when the question asks for a person-level column (status/gender/
    occupation/verification — the 12.5K-cohort-only fields, rule 4) AND names
    one or more SPECIFIC tranches, NONE of which is Tranch 4. That combination
    can never have any matching rows — the 12.5K cohort IS Tranch 4, so a
    filter for Tranch 1/2/3 plus any of these columns is a structural
    contradiction, not an ordinary "no rows matched" outcome. Left to run as
    plain SQL this produces the confusing generic "couldn't find any matching
    records" fallback (`_no_data_answer`) with no explanation of WHY — catching
    it here lets `_answer_data` explain the real reason instead, and skips a
    wasted SQL round trip. Scoped to single-scheme Focus Plus only, mirroring
    `_needs_tranche_clarification`."""
    if (schemes or []) != ["Focus Plus"]:
        return False
    if not _PERSON_LEVEL_COLUMN_CUE.search(question or ""):
        return False
    tranche_label = resolved.get("tranche_label")
    if not tranche_label:
        return False
    labels = tranche_label if isinstance(tranche_label, list) else [tranche_label]
    return not any(_TRANCH4_LABEL_RE.match(str(lbl)) for lbl in labels)


def _person_level_tranche_conflict_answer(question: str, schemes: list[str],
                                          entity_result: dict) -> dict:
    """Deterministic explanation for `_person_level_tranche_conflict` — no SQL
    is run because the answer (zero rows, always) is already known from the
    schema, not from the data."""
    display = entity_result.get("display", {}).get("tranche_label") or "that tranche"
    resolved = entity_result["resolved"]
    answer = (
        f"{display} has no status, gender, occupation or verification data. "
        "Those fields are recorded only for the 12.5K registration cohort, and "
        "that cohort falls entirely under Tranch 4 — ask for Tranch 4, or drop "
        "the tranche filter, to see that breakdown."
    )
    return {
        "route": "data",
        "intent": "DATA",
        "confidence": "high",
        "schemes": schemes,
        "resolved_entities": resolved,
        "sql": "",
        "sql_query": "",
        "row_count": 0,
        "rows": [],
        "data": [],
        "answer": answer,
    }


# Client UAT sheet (FOCUS-030, 2026-09-13): "Give me an overall Focus+ data
# summary" got a correct but thin answer — just payments/beneficiaries/amount/
# districts/blocks/villages from one plain SELECT. The client's remark wants a
# richer report: years/batches/tranches available, the top district by each
# of two different rankings (most beneficiaries vs. highest disbursement —
# genuinely different districts in this data), top bank, and the 12.5K-only
# gender/occupation/status/verification splits. That shape can't come from one
# flat SELECT the normal way (each of those needs its own GROUP BY / ORDER BY /
# LIMIT 1), and letting the LLM free-write ad hoc SQL plus prose for a dozen
# figures at once is exactly the kind of multi-fact answer this project's
# numeric-faithfulness guard exists to catch failures of, not prevent them.
# Deterministic instead: one CTE query gets every figure in a single round
# trip, and the answer is built directly from those rows — no composer call,
# so nothing here can be transcribed wrong.
_FOCUSPLUS_OVERALL_SUMMARY_CUE = re.compile(
    r"\boverall\b[^.?!]{0,40}\b(summary|picture|snapshot)\b|"
    r"\b(summary|snapshot|overview)\b[^.?!]{0,40}\boverall\b|"
    r"\b(complete|full|entire)\s+(data\s+)?summary\b|"
    r"\bsummari[sz]e\b.{0,30}\bfocus\b|\bfocus\b.{0,30}\bdata\s+summary\b",
    re.IGNORECASE,
)

_FOCUSPLUS_OVERALL_SUMMARY_SQL = """
WITH totals AS (
  SELECT COUNT(*) AS payments,
         COUNT(DISTINCT beneficiary_key) AS beneficiaries,
         SUM(amount_disbursed) AS amount,
         COUNT(DISTINCT lgd_district) AS districts,
         COUNT(*) FILTER (WHERE lgd_district IS NULL) AS missing_district_rows
  FROM curated.v_focus_plus
),
years AS (
  SELECT string_agg(DISTINCT financial_year_short, ', ' ORDER BY financial_year_short) AS years_list
  FROM curated.v_focus_plus
),
batches AS (
  SELECT string_agg(DISTINCT batch_label, ', ' ORDER BY batch_label) AS batch_list
  FROM curated.v_focus_plus
),
tranches AS (
  SELECT COUNT(DISTINCT tranche_label) AS tranche_count
  FROM curated.v_focus_plus
),
top_beneficiary_district AS (
  SELECT lgd_district AS top_ben_district, COUNT(DISTINCT beneficiary_key) AS top_ben_district_count
  FROM curated.v_focus_plus WHERE lgd_district IS NOT NULL
  GROUP BY lgd_district ORDER BY top_ben_district_count DESC LIMIT 1
),
top_disbursement_district AS (
  SELECT lgd_district AS top_amt_district, SUM(amount_disbursed) AS top_amt_district_amount
  FROM curated.v_focus_plus WHERE lgd_district IS NOT NULL
  GROUP BY lgd_district ORDER BY top_amt_district_amount DESC LIMIT 1
),
top_bank AS (
  SELECT bank_name_raw AS top_bank_name, SUM(amount_disbursed) AS top_bank_amount
  FROM curated.v_focus_plus WHERE bank_name_raw IS NOT NULL AND bank_name_raw !~ '^[0-9]+$'
  GROUP BY bank_name_raw ORDER BY top_bank_amount DESC LIMIT 1
),
gender AS (
  SELECT COUNT(*) FILTER (WHERE gender = 'Female') AS female,
         COUNT(*) FILTER (WHERE gender = 'Male') AS male
  FROM curated.v_focus_plus WHERE batch_label = '12.5K'
),
occupation AS (
  SELECT COUNT(*) FILTER (WHERE occupation = 'Farmer') AS farmers
  FROM curated.v_focus_plus WHERE batch_label = '12.5K'
),
status AS (
  SELECT COUNT(*) FILTER (WHERE focus_status = 'Pending') AS pending,
         COUNT(*) FILTER (WHERE focus_status = 'Approved') AS approved,
         COUNT(*) FILTER (WHERE focus_status = 'Rejected') AS rejected,
         COUNT(*) FILTER (WHERE verification_status = 'Approved') AS verified_approved
  FROM curated.v_focus_plus WHERE batch_label = '12.5K'
)
SELECT totals.payments, totals.beneficiaries, totals.amount, totals.districts,
       totals.missing_district_rows, years.years_list, batches.batch_list,
       tranches.tranche_count, top_beneficiary_district.top_ben_district,
       top_beneficiary_district.top_ben_district_count,
       top_disbursement_district.top_amt_district,
       top_disbursement_district.top_amt_district_amount,
       top_bank.top_bank_name, top_bank.top_bank_amount,
       gender.female, gender.male, occupation.farmers,
       status.pending, status.approved, status.rejected, status.verified_approved
FROM totals, years, batches, tranches, top_beneficiary_district,
     top_disbursement_district, top_bank, gender, occupation, status
LIMIT 1;
""".strip()


# ── "Which scheme has the highest spend?" — the scheme IS the answer ────────
# A superlative asked ACROSS schemes ("which scheme paid out the most?", "the
# scheme with the highest money spent") is answered by ranking the schemes
# against each other. Asking "which scheme does your question concern?" first
# is backwards — the scheme is the thing being asked for, not a filter the
# user forgot (reported 2026-09-17: "what about the scheme with highest money
# paid?" raised the four-way scheme pause instead of answering).
#
# _EXPLICIT_BOTH already recognises the phrasings that ask for every scheme
# ("both schemes", "by scheme", "scheme-wise"), but not this superlative
# shape, where the cross-scheme intent is carried by "which/what scheme" plus
# a ranking word rather than by an "all/each" quantifier.
_CROSS_SCHEME_SUPERLATIVE = re.compile(
    r"\b(?:which|what)\s+scheme\b[^?.!]{0,60}\b"
    r"(?:highest|lowest|most|least|maximum|minimum|greatest|biggest|largest|"
    r"smallest|top|best|worst|more|less)\b|"
    r"\b(?:highest|lowest|most|least|maximum|minimum|greatest|biggest|largest|"
    r"smallest|top)\b[^?.!]{0,40}\bscheme\b|"
    r"\bscheme\s+with\s+(?:the\s+)?(?:highest|lowest|most|least|maximum|minimum|"
    r"greatest|biggest|largest|smallest|top)\b|"
    r"\b(?:rank|compare)\s+(?:the\s+)?schemes\b",
    re.IGNORECASE,
)
# The superlative has to be about MONEY for the ranking below to apply — a
# "which scheme has the most applications" question is a different figure and
# is left to normal SQL generation.
_MONEY_SUPERLATIVE = re.compile(
    r"\bmoney\b|\bamount\b|\bspend\w*\b|\bspent\b|\bexpenditure\b|\bpaid\b|"
    r"\bpayment\w*\b|\bdisburs\w*\b|\breleas\w*\b|\bfunds?\b|\bcrore\b|\blakh\b|"
    r"\bcost\b|\bbudget\b|\boutlay\b",
    re.IGNORECASE,
)


def _wants_cross_scheme_money_ranking(question: str) -> bool:
    """True when the question asks which scheme spent/paid the most — a
    comparison ACROSS schemes whose answer names a scheme."""
    q = question or ""
    return bool(_CROSS_SCHEME_SUPERLATIVE.search(q) and _MONEY_SUPERLATIVE.search(q))


# One query, every scheme that records money, each normalised to CRORE.
# curated.v_cross_scheme_money_district_year is the sanctioned MGNREGA-vs-PMAY
# comparison object (docs/DATA_MODEL.md); Focus Plus keeps its money on its own
# view in RUPEES (/1e7 -> crore). CM Elevate is deliberately absent — it has no
# money column of any kind (v_cm_elevate carries applications only), so it is
# reported as "not held" rather than as a misleading zero.
_CROSS_SCHEME_MONEY_SQL = """
SELECT scheme_code AS scheme,
       ROUND(SUM(amount_crore), 2) AS amount_crore,
       MIN(measure_semantics) AS measure_semantics
FROM curated.v_cross_scheme_money_district_year
GROUP BY scheme_code
UNION ALL
SELECT 'Focus Plus' AS scheme,
       ROUND(SUM(amount_disbursed) / 1e7, 2) AS amount_crore,
       'amount disbursed to farmers (DBT), rupees' AS measure_semantics
FROM curated.v_focus_plus
UNION ALL
SELECT 'Focus Legacy' AS scheme,
       ROUND(SUM(amount_disbursed) / 1e7, 2) AS amount_crore,
       'amount remitted to producer groups, rupees' AS measure_semantics
FROM curated.v_focus_legacy
UNION ALL
SELECT 'CM Elevate Legacy' AS scheme,
       ROUND(SUM(total_disbursement) / 1e7, 2) AS amount_crore,
       'subsidy and loan disbursed to sanctioned applicants, rupees' AS measure_semantics
FROM curated.v_cm_elevate_disbursement
ORDER BY amount_crore DESC
""".strip()

_SCHEME_DISPLAY_NAME = {"MGNREGA": "MGNREGA", "PMAY": "PMAY-G", "PMAY-G": "PMAY-G",
                        "Focus Plus": "Focus Plus", "CM Elevate": "CM Elevate",
                        "Focus Legacy": "Focus Legacy",
                        "CM Elevate Legacy": "CM Elevate Legacy"}
# Plain-English rendering of each scheme's measure_semantics. The stored strings
# are written for the SQL prompt ("annual FLOW (lakh rupees)", "an EVENT, not a
# clean annual flow") and read as database jargon in a chat bubble; the caveat
# they carry is preserved in the closing paragraph either way.
_MEASURE_PLAIN = {
    "MGNREGA": "expenditure actually incurred",
    "PMAY-G": "money released against sanctions",
    "Focus Plus": "cash disbursed to farmers (DBT)",
    "Focus Legacy": "cash remitted to producer groups",
    "CM Elevate Legacy": "subsidy and loans disbursed to sanctioned applicants",
}


async def _cross_scheme_money_answer(question: str) -> dict:
    """Rank the schemes by money, deterministically. Built from the rows rather
    than composed by the LLM, for the same reason as the Focus Plus overall
    summary above: several figures of DIFFERENT kinds in one answer is exactly
    where a composer misattributes numbers, and the measure_semantics caveat
    must survive verbatim — docs/DATA_MODEL.md requires carrying it into any
    cross-scheme money comparison, because MGNREGA's figure is expenditure
    incurred while PMAY-G's is money released against sanctions."""
    rows = await run_readonly(_CROSS_SCHEME_MONEY_SQL)
    ranked = [r for r in rows if r.get("amount_crore") is not None]
    if not ranked:
        return {"route": "data", "intent": "DATA", "confidence": "low",
                "answer": "I couldn't read the scheme spending figures just now.",
                **_empty_data_fields()}
    top = ranked[0]
    top_name = _SCHEME_DISPLAY_NAME.get(str(top["scheme"]), str(top["scheme"]))
    lines = [
        f"{top_name} has the highest amount at ₹{float(top['amount_crore']):,.2f} crore. "
        "Across every scheme that records money:"
    ]
    for r in ranked:
        name = _SCHEME_DISPLAY_NAME.get(str(r["scheme"]), str(r["scheme"]))
        lines.append(f"- **{name}** — ₹{float(r['amount_crore']):,.2f} crore "
                     f"({_MEASURE_PLAIN.get(name, r['measure_semantics'])})")
    lines.append(
        "\nThese are not the same kind of figure, so treat the ranking as indicative "
        "rather than like-for-like: MGNREGA's is expenditure actually incurred, PMAY-G's "
        "is money released against sanctions, Focus Plus's is cash disbursed to "
        "individual farmers, and Focus Legacy's is cash remitted to producer groups "
        "(where every payment is Rs 5,000 per member, so the figure is really a "
        "membership count), and CM Elevate Legacy's is subsidy and loans together paid "
        "to sanctioned applicants. The CM Elevate applications data records no payment "
        "of any kind, so it cannot appear in a money comparison at all."
    )
    return {
        "route": "data", "intent": "DATA", "confidence": "high",
        "schemes": [_SCHEME_DISPLAY_NAME.get(str(r["scheme"]), str(r["scheme"])) for r in ranked],
        "resolved_entities": {},
        "sql": _CROSS_SCHEME_MONEY_SQL, "sql_query": _CROSS_SCHEME_MONEY_SQL,
        "row_count": len(ranked), "rows": ranked, "data": ranked,
        "answer": "\n".join(lines),
    }


def _focusplus_wants_overall_summary(question: str, schemes: list[str]) -> bool:
    return schemes == ["Focus Plus"] and bool(_FOCUSPLUS_OVERALL_SUMMARY_CUE.search(question or ""))


def _money(v) -> str:
    return f"₹{float(v):,.2f}"


async def _focusplus_overall_summary_answer(schemes: list[str], entity_result: dict) -> dict:
    rows = await run_readonly(_FOCUSPLUS_OVERALL_SUMMARY_SQL)
    r = rows[0]
    avg_per_beneficiary = float(r["amount"]) / r["beneficiaries"]
    answer = (
        f"Focus+ overall summary:\n"
        f"- Disbursement records (payments): {r['payments']:,}\n"
        f"- Unique beneficiaries: {r['beneficiaries']:,}\n"
        f"- Total amount disbursed: {_money(r['amount'])}\n"
        f"- Average disbursement per beneficiary: {_money(avg_per_beneficiary)}\n"
        f"- Financial years available: {r['years_list']}\n"
        f"- Batches: {r['batch_list']} (93K = legacy paid cohort, 12.5K = registration cohort)\n"
        f"- Tranches: {r['tranche_count']}\n"
        f"- Districts represented: {r['districts']}, plus {r['missing_district_rows']} "
        "payment records with no district resolved\n"
        f"- District with the most beneficiaries: {r['top_ben_district']} — "
        f"{r['top_ben_district_count']:,}\n"
        f"- District with the highest disbursement: {r['top_amt_district']} — "
        f"{_money(r['top_amt_district_amount'])}\n"
        f"- Top bank by disbursement: {r['top_bank_name']} — {_money(r['top_bank_amount'])}\n"
        f"- Gender split (12.5K cohort only, ~3% of beneficiaries): "
        f"{r['female']:,} female, {r['male']:,} male\n"
        f"- Farmers (12.5K cohort only): {r['farmers']:,}\n"
        f"- Status (12.5K cohort only): {r['pending']:,} Pending, {r['approved']:,} Approved, "
        f"{r['rejected']:,} Rejected\n"
        f"- Verification status (12.5K cohort only): {r['verified_approved']:,} Approved"
    )
    return {
        "route": "data",
        "intent": "DATA",
        "confidence": "high",
        "schemes": schemes,
        "resolved_entities": entity_result["resolved"],
        "sql": _FOCUSPLUS_OVERALL_SUMMARY_SQL,
        "sql_query": _FOCUSPLUS_OVERALL_SUMMARY_SQL,
        "row_count": 1,
        "rows": rows,
        "data": rows,
        "answer": answer,
    }


def _needs_tranche_clarification(question: str, schemes: list[str], resolved: dict,
                                  *, already_all_combined: bool = False) -> bool:
    """True when Focus Plus is the ONLY scheme in play, the question is a
    metric / breakdown question, and it pins no tranche — not in its text
    (resolve_tranche_label found nothing during resolve_entities) and not via
    a resolved entity — and doesn't ask for a per-tranche breakdown or an
    explicit "all tranches combined". Callers must have run resolve_entities
    first so `resolved` reflects any tranche actually named. Scoped to
    single-scheme Focus Plus questions only, not "Focus Plus" in schemes —
    a cross-scheme comparison (schemes has more than one entry) wants one
    row per scheme, not Focus Plus fragmented into its four tranches on top,
    and that flow already has its own tuned behaviour this must not disturb.

    `already_all_combined` (optional): True when this session already
    answered a Focus Plus question with "all tranches combined" earlier in
    the conversation (see context_manager's ConversationState.tranche_all_combined
    / session_store) and the current turn is a short follow-up that never
    re-says "tranche" at all — e.g. "give me top 3 only" right after "...
    across all tranches". Without this, that kind of bare follow-up has no
    tranche cue of its own, re-trips this gate, and either re-asks a question
    the user just answered or (worse) falls through to the SQL generator with
    no tranche signal and no memory of the choice already made."""
    if not settings.TRANCHE_CLARIFY_ENABLED:
        return False
    if (schemes or []) != ["Focus Plus"]:
        return False
    q = question or ""
    if resolved.get("tranche_label"):
        return False
    if already_all_combined:
        return False
    if _TRANCHE_BREAKDOWN_CUE.search(q) or _ALL_TRANCHES_CUE.search(q):
        return False
    # A question about a person-level / cohort-locked column is always
    # confined to the 12.5K cohort at Tranch 4 regardless of what the user
    # says about tranche — asking "which tranche?" has no real answer to
    # collect, so skip straight past the gate.
    if _PERSON_LEVEL_COLUMN_CUE.search(q):
        return False
    # Trigger ONLY when the question literally says "tranche"/"tranch" without
    # pinning which one. `_METRIC_OR_BREAKDOWN_CUE` used to also trigger this
    # gate (mirroring the year gate), but that cue matches "beneficiar*" and
    # "payments?" — i.e. almost every Focus Plus money/count question, not
    # just tranche-sensitive ones. None of the Focus Plus few-shot SQL (totals,
    # geography, gender, batch, status, FY breakdowns) scopes by tranche_label
    # at all — SUM(amount_disbursed) is well-defined across tranches, and the
    # one genuine mix-ratio trap (a naive per-beneficiary AVERAGE) is handled
    # by scoping to batch_label, not by asking the user to pick a tranche (see
    # "What is the average Focus Plus payment per member?" in
    # focusplus_few_shot.yaml). Confirmed live 2026-09-12: this over-broad
    # trigger paused nearly every Focus Plus beneficiary/disbursement question
    # for an unwanted tranche pick, and a scope-pause reply that re-entered
    # here with tranche_label already resolved but not yet threaded through
    # the merged question text sent the SQL generator into a repair loop that
    # burned the retry budget and crashed out to the KB fallback.
    if not _MENTIONS_TRANCHE_WORD.search(q):
        return False
    return True


def _tranche_clarification(question: str) -> "ClarificationNeeded":
    stem = question.strip().rstrip(" ?.")
    labels = tranche_labels("Focus Plus")
    options = [
        {"label": lbl, "question": f"{stem} for {lbl}"}
        for lbl in labels
    ]
    options.append({
        "label": "All tranches combined",
        "question": f"{stem} across all tranches",
    })
    label_list = ", ".join(labels[:-1]) + f" and {labels[-1]}" if len(labels) > 1 else labels[0]
    return ClarificationNeeded(
        f"Focus Plus data is split into {label_list}. Which of these is required "
        "— a single tranche, or all of them combined?",
        options=options,
        rule="tranche-not-specified",
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


_MALFORMED_YEAR_CUE_RE = re.compile(
    # "of" deliberately excluded — it's the ending of ordinary amount phrasing
    # too ("total expenditure of 150000"), not just year phrasing, so it would
    # false-positive on a real currency figure. "in"/"for"/"during"/"fy" are
    # unambiguously temporal.
    r"\b(?:in|for|during|fy|financial\s+year|fiscal(?:\s+year)?)\s*[:\-]?\s*(\d{4,8})"
    r"\s*[?.!]*\s*\Z",
    re.IGNORECASE,
)


def _years_in_question(text: str, schemes: "list[str] | None" = None
                       ) -> "tuple[list[str], list[str]]":
    """(available, unavailable) raw year tokens named in `text`.

    _out_of_range_year_in below returns only the FIRST unavailable token, which
    is the right answer for "is there a bad year here?" but the wrong basis for
    "should the whole question be refused?" — a question can name a year the
    scheme lacks AND a year it holds ("compare FY 2023-24 and FY 2024-25", the
    Focus Legacy gap case). Refusing that discards a question the data can
    largely answer."""
    ok: list[str] = []
    bad: list[str] = []
    for m in _YEAR_RANGE_TOKEN_RE.finditer(text or ""):
        tok = m.group(1)
        yk = _parse_year_key(tok)
        if yk is not None:
            (ok if _year_in_data_range(yk, schemes) else bad).append(tok)
        elif _YEAR_SHAPED_RE.search(tok):
            bad.append(tok)
    return ok, bad


# A question that asks for a COMPARISON needs two operands. Dropping one of
# them answers a different, narrower question than the one asked.
_COMPARES_YEARS = re.compile(
    r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|\bagainst\b|"
    r"\bdifference between\b|\bbetween\b.{0,40}\band\b|"
    r"\bhigher than\b|\blower than\b|\bmore than\b.{0,20}\bfy\b",
    re.IGNORECASE,
)


def _nearest_available_year(bad_token: str, schemes: "list[str] | None") -> "str | None":
    """The year with data closest to `bad_token`, preferring the one BEFORE it.

    The scheme's own contract calls for exactly this on a gap comparison —
    focuslegacy_few_shot.yaml: "The preceding year WITH PAYMENTS is FY2022-23,
    not FY2023-24", and response_template's fy_gap_comparison_note. Preferring
    the earlier year keeps "compare X with the year before it" meaning what it
    says; a later year is used only when nothing earlier exists."""
    target = _parse_year_key(bad_token)
    if target is None:
        return None
    available, _live = _available_years_for(schemes or [])
    keys = sorted(k for k in (_parse_year_key(y) for y in available) if k is not None)
    if not keys:
        return None
    earlier = [k for k in keys if k < target]
    if earlier:
        return _fy_short(earlier[-1])
    later = [k for k in keys if k > target]
    return _fy_short(later[0]) if later else None


def _substitute_year_token(question: str, old_tok: str, new_short: str) -> str:
    """Replace one year token in place, keeping the surrounding wording (and any
    "FY " prefix) so the sentence still reads as the comparison it is."""
    return re.sub(
        r"(?:fy\s*)?" + re.escape(old_tok.strip()) + r"\b",
        f"FY {new_short}", question, count=1, flags=re.IGNORECASE,
    )


def _apply_year_gap(question: str, schemes: "list[str] | None"
                    ) -> "tuple[str, str | None, bool]":
    """Decide what to do with a question naming a financial year the scheme has
    no data for. Returns (question, note, handled).

    `handled` is False only when NOTHING in the question is answerable — the
    caller then raises the out-of-range clarification. Otherwise the question is
    rewritten and a note explains what changed, so the answer states the gap
    rather than silently ignoring it.

    Two rewrites, and which one applies is the whole point:

    * COMPARISON ("compare FY 2023-24 and FY 2024-25", "X vs Y") — a comparison
      needs two operands, so the absent year is SUBSTITUTED with the nearest
      year that holds data. Deleting it left one year and answered with a single
      figure, leaving the verb the user typed unmet (reported 2026-09-23). The
      substitute is the preceding year with payments, which is exactly what the
      scheme's own exemplar and fy_gap_comparison_note prescribe.
    * ANYTHING ELSE — the absent year is dropped. There is no second operand to
      preserve, and substituting would answer about a year the user never named.

    Lives here rather than inline in resolve_entities so the tests exercise the
    real decision: an earlier version of this logic was inline, the regression
    test re-implemented it, and the test stayed green when the production branch
    was disabled."""
    ok_years, bad_years = _years_in_question(question, schemes)
    if not ok_years:
        return question, None, False

    swapped: list[tuple[str, str]] = []
    if _COMPARES_YEARS.search(question) and len(ok_years) + len(bad_years) >= 2:
        for bad in list(bad_years):
            near = _nearest_available_year(bad, schemes)
            if near and near not in ok_years:
                question = _substitute_year_token(question, bad, near)
                ok_years.append(near)
                bad_years.remove(bad)
                swapped.append((bad, near))
    if bad_years:
        question = _strip_year_tokens(question, bad_years)

    parts: list[str] = []
    if swapped:
        parts.append(
            "; ".join(
                f"FY {b} holds no data for this scheme, so FY {n} — the nearest "
                f"financial year that does — is compared instead"
                for b, n in swapped
            )
            + ". That is a gap in the records, not a zero."
        )
    if bad_years:
        parts.append(
            ", ".join(f"FY {y}" for y in bad_years)
            + " holds no data for this scheme, so it is left out — "
            "that is a gap in the records, not a zero."
        )
    logger.info("year gap: swapped=%s dropped=%s, answering for %s",
                swapped, bad_years, ok_years)
    return question, (" ".join(parts) or None), True


def _strip_year_tokens(question: str, tokens: "list[str]") -> str:
    """Remove the given year tokens (and any "for "/"in "/"FY " lead-in, or a
    dangling "and"/",") from the question, so what remains reads naturally and
    cannot re-trip the year guard on a later turn."""
    out = question
    for tok in tokens:
        # Take any connector that FOLLOWS the token too ("between FY 2023-24
        # and ..." -> "between ..."), otherwise dropping the first of a pair
        # leaves a dangling "and": "Compare the total remittance and FY
        # 2024-25." Both sides are optional, so a lone year is still removed.
        out = re.sub(
            r"\s*(?:,|\band\b)?\s*(?:for\s+|in\s+|during\s+|of\s+|between\s+)?"
            r"(?:fy\s*)?" + re.escape(tok.strip()) + r"\b\s*(?:,|\band\b)?",
            " ", out, count=1, flags=re.IGNORECASE,
        )
    out = re.sub(r"\s{2,}", " ", out)
    # "between" / "from" left with nothing to join, and a trailing connector
    # before the closing punctuation.
    out = re.sub(r"\b(?:between|from)\s+(?=[\"\u201d.?]|$)", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+(?:and|,)\s*(?=[\"\u201d.?]|$)", "", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip().strip(",").strip()


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
    # A digit run right after an explicit temporal cue, at the very END of the
    # question ("in 20217", "...beneficiaries in Bamil Reserve Apal in 20217?")
    # that _YEAR_RANGE_TOKEN_RE above didn't match at all — its 20[1-3]\d
    # alternative needs a \b right after the 4th digit, so one extra trailing
    # digit ("20217") makes the WHOLE token invisible to it, not just
    # unparseable. Without this, "in 20217" skipped the guard entirely and the
    # SQL generator copied the literal straight into year_key = 20217, ran
    # clean, and silently returned 0 (2026-09-09 bug report — no clarification
    # was ever offered). Anchored to end-of-question (not just gated on a cue
    # word) so a real amount stated mid-sentence ("sanctioned amount of 500000
    # in Siju") is never mistaken for a year — a year mention is normally the
    # last thing named in these questions, an amount normally isn't.
    m = _MALFORMED_YEAR_CUE_RE.search(text or "")
    if m and _parse_year_key(m.group(1)) is None:
        return m.group(1)
    return None


def _available_years_for(schemes: list[str]) -> "tuple[list[str], list[str]]":
    """(distinct FY list across the given scheme(s), scheme names used). Falls
    back to every scheme when `schemes` is empty or unrecognised.

    Years are merged scheme-by-scheme, so without an explicit sort the result
    lands in whatever order the schemes happen to combine in (e.g. MGNREGA's
    2022-23..2025-26 ahead of PMAY-G's earlier 2017-18..2021-22) rather than
    chronological order. Sort ascending (oldest first) so the year chips read
    in a sane order regardless of scheme combination."""
    live = [s for s in (schemes or []) if s in _SCHEME_DATA_YEARS] or list(_SCHEME_DATA_YEARS)
    seen: set[str] = set()
    years: list[str] = []
    for s in live:
        for y in _SCHEME_DATA_YEARS[s]:
            if y not in seen:
                seen.add(y)
                years.append(y)
    years.sort(key=_fy_start)
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
    if not years:
        # Every scheme in play has NO time dimension at all (CM Elevate: no
        # year_key, no date column of any kind) — there is no "these years are
        # available" chip list to offer. Refuse the time filter outright rather
        # than showing an out-of-range message with zero year options.
        scheme_word = live[0] if len(live) == 1 else (" and ".join(live) if live else "This scheme")
        return ClarificationNeeded(
            f"{scheme_word} has no date field in this data, so records cannot be "
            f"placed in a financial year or any other time period. I can answer "
            "without a time filter instead.",
            options=[{"label": "Answer without a time filter", "question": stem}],
            rule="no-time-dimension",
        )
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
    """Schemes named outright in the question text, in catalog order. Falls
    back to fuzzy matching (_fuzzy_named_schemes) when the exact regex finds
    nothing — otherwise a misspelled scheme name ("manrega", "pamay") reads as
    "no scheme named" and forces an unnecessary clarification prompt instead
    of being understood."""
    exact = [s for s, pattern in _SCHEME_NAME_PATTERN.items() if pattern.search(question)]
    if exact:
        return exact
    return _fuzzy_named_schemes(question)


def _shortcut_scheme(question: str) -> list[str] | None:
    """Skip the classifier call when the text already pins the scheme set —
    faster and more reliable than the model for the common case, and one fewer
    model call per query at scale. Deterministic whenever:
      * two or more schemes are named  -> use exactly those (the user listed
        them; "compare MGNREGA and PMAY" needs no classification), or
      * an explicit cross-scheme phrase ("both schemes", "across schemes")
        is present  -> the whole catalog, or
      * exactly one scheme is named    -> that one, or
      * no scheme is named but the vocabulary pins exactly one (same check
        _needs_scheme_clarification uses to skip its own pause) -> that one.
    Without this last case, a vocabulary-only question ("sanctioned amount",
    "person-days") skipped the "which scheme?" pause (correctly inferring
    MGNREGA/PMAY-G) but then still went to the LLM classifier here, which
    doesn't apply the same vocabulary rule and can return every scheme "to be
    safe" — inconsistent with the pause having just been skipped, and it broke
    the PMAY-G year-gate exemption (which only applies when the scheme set is
    exactly ["PMAY-G"]).
    Returns None only when nothing in the text pins it, so the classifier still
    handles the genuinely unnamed/ambiguous case."""
    named = _named_schemes(question)
    if len(named) >= 2:
        return named
    # CM Elevate Legacy's 13 sub-units are themselves called "schemes"
    # (Piggery, Poultry, ...), so "CM Elevate Legacy records by scheme" /
    # "scheme-wise" asks for ITS scheme_name breakdown — not every scheme in
    # the catalog. Read as cross-scheme, it pulled in all six schemes and the
    # year pause offered their union (FY 2017-18..2025-26) for a scheme that
    # holds only FY 2024-25 and 2025-26.
    if named == ["CM Elevate Legacy"]:
        return named
    if _EXPLICIT_BOTH.search(question):
        return list(SCHEME_CATALOG)
    if named:
        return named
    return _infer_scheme_from_terms(question)


async def classify_scheme(question: str) -> list[str]:
    """Which scheme(s) — MGNREGA, PMAY-G, or both — does this question touch?"""
    shortcut = _shortcut_scheme(question)
    if shortcut:
        logger.info("classify_scheme: shortcut matched %s, skipping model call", shortcut)
        return shortcut

    catalog = "\n".join(f'  - "{name}": {desc}' for name, desc in SCHEME_CATALOG.items())
    scheme_options = " | ".join(f'"{name}"' for name in SCHEME_CATALOG)
    prompt = f"""Classify which scheme(s) this question needs. Available schemes:
{catalog}

Return ONLY JSON: {{"schemes": [{scheme_options}, ...]}}
Use several if the question compares or combines schemes. If it names none specifically
and gives no scheme-specific vocabulary, return every scheme.

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


# Real Meghalaya village names carry a parenthesised suffix that is part of the
# name: "NONGCHRAM (I)", "NONGCHRAM (II)", "Existing site(Old House)". Stripping
# trailing punctuation blindly removed the CLOSING paren while leaving the
# opening one, so "NONGCHRAM (I)" became "NONGCHRAM (I" — which matches no
# stored name exactly, stays permanently ambiguous, and makes the
# village-disambiguation chip regenerate the identical question forever
# (reported 2026-09-17: the pause repeated on every click). Strip only what is
# genuinely punctuation around the name, and keep a closing bracket whenever it
# balances an opening one still inside the value.
_MENTION_EDGE_CHARS = "\"'`.,?!;: \t"


def _drop_place_phrase(question: str, name: str) -> str:
    """Remove "in <name>" from `question`, taking any part-marker suffix with
    it. Stripping the bare name left an orphan fragment behind — dropping
    "NONGSPUNG" from "... in NONGSPUNG - A, UMLING block, RI BHOI" produced
    "... to be released - A, UMLING block, RI BHOI", which then read as a
    brand-new question and sent the next few turns badly wrong (reported
    2026-09-17). Also tidies a doubled comma left by the removal."""
    out = re.sub(
        rf"\s*\b(?:in|for|of|at|from|within)\s+{re.escape(name)}"
        r"(?:\s*\([^)]{0,20}\)|\s*-\s*[A-Za-z0-9]{1,12})?",
        "", question or "", count=1, flags=re.IGNORECASE)
    out = re.sub(r"\s*,\s*,", ",", out)
    return re.sub(r"\s{2,}", " ", out).strip().lstrip(",").strip()


def _place_title(value: str) -> str:
    """Title-case a place name without mangling a roman-numeral or acronym
    suffix: str.title() turns "NONGCHRAM (II)" into "Nongchram (Ii)". Any
    parenthesised run of roman numerals / digits is preserved as-is."""
    out = str(value or "").title()
    return re.sub(r"\(([IVXLCDM\d]+)\)", lambda m: "(" + m.group(1).upper() + ")",
                  out, flags=re.IGNORECASE)


def _strip_mention_punctuation(value: str) -> str:
    v = (value or "").strip()
    # Leading: brackets are never part of a name at the start.
    v = v.lstrip(_MENTION_EDGE_CHARS + "([{")
    # Trailing: drop plain punctuation always, but a bracket only when it is
    # unbalanced (i.e. nothing opened it earlier in the value).
    while v:
        last = v[-1]
        if last in _MENTION_EDGE_CHARS:
            v = v[:-1]
            continue
        if last in ")]}":
            opener = {")": "(", "]": "[", "}": "{"}[last]
            if v.count(opener) >= v.count(last):
                break            # balanced — "(I)" belongs to the name
            v = v[:-1]
            continue
        break
    return v.strip()


def _clean_mention(value: str) -> str | None:
    """Normalise a raw mention; return None if it's a bare dimension word / the
    state name (i.e. not an actual place or period)."""
    v = _strip_mention_punctuation(value)
    core = re.sub(r"^(the|a|an|this|that|each|every|all)\s+", "", v, flags=re.IGNORECASE).strip()
    if not core or core.lower() in _GENERIC_PLACE_TERMS:
        return None
    # A scheme name is never a place — "... for Focus Plus" reads like a place
    # after a preposition (the extractor prompt deliberately teaches it to
    # follow "of"/"for"/"under" onto place names), so the model occasionally
    # tags the scheme itself as the district/block/village. Reject it here as
    # a deterministic backstop regardless of what the LLM returned, or it goes
    # on to fail district/block/village resolution and gets reported as a
    # place "not in Meghalaya" (reported 2026-09-10: "which district received
    # highest total disbursement for Focus Plus" -> mentions.district ==
    # "Focus Plus" -> OutOfScope).
    if any(rx.search(core) for rx in _SCHEME_NAME_PATTERN.values()):
        return None
    return v


def _mention_in_question(value: str, question: str) -> bool:
    """True when `value` actually occurs in `question`, case-insensitively and
    tolerant of whitespace differences. The extractor prompt requires every
    span to be copied verbatim from the question text, so a value that fails
    this check isn't a real span — it's a hallucination, not an extraction.
    Guards against the classifier echoing one of its own few-shot examples
    (reported 2026-09-11: "Top 5 CM Elevate schemes by applications" — no
    year mentioned anywhere in the text — came back with {"year": "2017-18"},
    lifted straight from the FY 2017-18 example in the prompt. For a
    zero-time-dimension scheme like CM Elevate that phantom year immediately
    tripped the "no date field" refusal, and because "2017-18" isn't literally
    in the question, the refusal's own year-stripping regex had nothing to
    strip — the follow-up chip resent the identical question text and looped
    forever)."""
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()
    return norm(value) in norm(question)


# A name the question itself introduces as a PRODUCER GROUP. Focus Legacy group
# names routinely collide with real places ("Nongstoin PG", "Mairang Producer
# Group") because groups are named after where they are, so the LLM extractor
# tags them as a block or village despite being told not to — and the geography
# branches then ask "did you mean the block or the village?" about a name the
# user never offered as a place (reported 2026-09-23).
_PG_NAMED_ENTITY = re.compile(
    r"\b(?:producer[\s-]?group|pg|group)s?\s+"
    r"(?:named|called|by the name of)\s+"
    r"(?:as\s+)?"
    # Name words may start with a digit or a bracket \u2014 "Bak 15 Banana Dijogre",
    # "Ieintylli Pg (cham Cham Pig Fattening Pg)" \u2014 and run to 8 words.
    r"[\"\u201c\u2018']?(?P<name>[A-Za-z0-9(][\w.\-()&']*(?:\s+[A-Za-z0-9(][\w.\-()&']*){0,7}?)"
    # The name ends the sentence, or is followed by a place / scope phrase:
    # "named Nongstoin PG in Betasing block" \u2014 without the second branch the
    # whole pattern missed and "Nongstoin" was resolved as a BLOCK.
    r"[\"\u201d\u2019']?\s*(?=[?.,;:]|$|(?:in|for|under|from|at|within|across|there)\b)",
    re.IGNORECASE,
)
# The group-type suffix, so "Nongstoin PG" strips to the core "Nongstoin" that
# the extractor actually tagged as a block.
_PG_SUFFIX = re.compile(
    r"[\s,]*\b(?:producer\s+groups?|producer\s+grp|p\.?\s*g\.?|group)\.?\s*$",
    re.IGNORECASE,
)


def _question_without_pg_name(question: str) -> str:
    """The question with a producer-group NAME the user labelled as one blanked
    out, for the place-name SCANS in resolve_entities. _drop_producer_group_names
    only cleans the extractor's mentions; the admin-level gate and the block
    backstop then re-scan the raw text and put the name straight back — "Is
    there any Producer Group named Nongstoin PG?" (Focus Legacy QA TC-13,
    2026-09-25) became "Nongstoin: the block or the village?", and the chip
    turned a name lookup into a geography question. Unchanged when no group is
    named."""
    m = _PG_NAMED_ENTITY.search(question or "")
    if not m:
        return question
    return (question[: m.start("name")] + question[m.end("name"):]).strip()


def _drop_producer_group_names(question: str, mentions: dict) -> dict:
    """Remove geography mentions that the question introduced as a PRODUCER
    GROUP name. The name still reaches SQL generation in the question text,
    where the pg_name ILIKE rule handles it; what must not happen is a
    block/village disambiguation about a group the user named.

    Narrow by construction: only a name the question itself labelled with group
    phrasing is stripped, so "disbursement in Nongstoin" still resolves as
    geography."""
    m = _PG_NAMED_ENTITY.search(question or "")
    if not m:
        return mentions
    named = m.group("name").strip()
    core = _PG_SUFFIX.sub("", named).strip().lower()
    if not core:
        return mentions

    out = dict(mentions)
    for dim in ("district", "block", "village"):
        val = str(out.get(dim) or "").strip().lower()
        if val and (val == core or val in core or core in val):
            out.pop(dim, None)
            logger.info("dropped %s mention %r — the question names it as a producer group",
                        dim, mentions.get(dim))
    for dim in ("districts", "blocks"):
        vals = out.get(dim)
        if isinstance(vals, list):
            kept = [v for v in vals
                    if str(v).strip().lower() not in (core,)
                    and core not in str(v).strip().lower()]
            if len(kept) != len(vals):
                logger.info("dropped %s entries naming the producer group %r", dim, named)
            if kept:
                out[dim] = kept
            else:
                out.pop(dim, None)
    return out


# An unmistakable financial-year range: "2024-25", "FY 2024-25", "2024-2025".
_EXPLICIT_FY_RANGE_RE = re.compile(r"\b((?:19|20|21)\d\d\s*[-/]\s*\d{2}(?:\d{2})?)\b")


def _backfill_explicit_year(question: str, schemes: list[str], mentions: dict) -> dict:
    """Fill the year slot from the text when the LLM extractor dropped it.

    The extractor sometimes returns no "year" for a question that plainly names
    one (TC-23, 2026-09-25: "What was the total amount remitted in FY 2024-25
    for Focus Legacy" -> {}). With no year_key resolved, the scope gate then
    asked "which area and time period?" and its "all years" chip made the SQL
    drop the FY — ₹51.01 Cr (every year) reported as FY 2024-25's ₹11.50 Cr.

    Deliberately narrow: only an explicit NNNN-NN range, only when exactly one
    distinct one is named, and only when the scheme holds that year (an absent
    year is the year-gap guard's business, already settled before this runs)."""
    if mentions.get("year"):
        return mentions
    toks = {yk for m in _EXPLICIT_FY_RANGE_RE.finditer(question or "")
            if (yk := _parse_year_key(m.group(1))) is not None}
    if len(toks) != 1:
        return mentions
    yk = next(iter(toks))
    if not _year_in_data_range(yk, schemes):
        return mentions
    fy = f"{yk}-{(yk + 1) % 100:02d}"
    logger.info("year mention back-filled from the question text: FY %s", fy)
    return {**mentions, "year": fy}


async def extract_entity_mentions(question: str) -> dict:
    """Which spans of the question name a district, block, village, year or
    assembly constituency? Span-finding only — resolving each span to a
    canonical DB value is a separate, deterministic step (entity_resolver),
    not this LLM call."""
    prompt = f"""Extract place/time names from this question, verbatim as the user typed
them. Do not correct spelling or guess the canonical form.

Return ONLY JSON with keys from: "district", "block", "village", "year",
"assembly_constituency", "blocks", "districts". Include a key ONLY when the
question NAMES a specific one. A bare word like "village", "villages",
"district", "block", "year", or the state name "Meghalaya" is NOT a name —
omit it. If the question names none, return {{}}.

When the question names TWO OR MORE blocks to compare against each other
("compare X and Y", "X vs Y", "X and Y, the block, not the village"), put ALL
of their names in the plural "blocks" array — do NOT use the singular "block"
key and keep only one, that silently drops the other block from the answer.
Use "block" only when exactly one block is named. The same rule applies to
districts: TWO OR MORE named to compare against each other go in the plural
"districts" array, never the singular "district" key — even when one of them
is a short acronym like "EKH" or "WGH". Use "district" only when exactly one
district is named.

Block, assembly-constituency and village names overlap heavily in Meghalaya —
a bare name (e.g. "Mawlai") could be any of the three. Use "assembly_constituency"
ONLY when the question itself signals a constituency: it says "constituency",
"AC", "assembly", "MLA", or gives a number before the name (e.g. "15 Mawlai",
"AC 9"). Otherwise tag an ambiguous bare name as "block".

A place name can follow ANY preposition, not just "in"/"for" — "disbursement
OF Selsella", "expenditure OF West Garo Hills", "spending OF Abagre" all name
Selsella/West Garo Hills/Abagre as the AREA the figure is about, exactly like
"disbursement in Selsella" would. Do not read "of" as meaning the metric noun
(disbursement, expenditure, spending, amount, total) is itself the thing being
named — the name after "of" is still a place, and must still be extracted.

A SCHEME name (MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy, or a
close variant) is NEVER a place — do not extract it as a district/block/village
even when it follows "for"/"of"/"under" exactly like a place would
("disbursement for Focus Plus" names the scheme, not an area; extract nothing).

A PRODUCER GROUP name or id is NEVER a place either. A Focus Legacy pg_id looks
like "PG-FOCUS-WGH-7089" and contains a district abbreviation — do NOT extract
that abbreviation, or any part of the id, as a district. A producer group NAME
("Muskan Producer Group", "Bak 15 Banana", "Iainehlang Pg") often reads like a
village name; it is a group, not an area, so extract nothing from it.

Likewise, a CM Elevate SUB-SCHEME name is NEVER a place, even though several
of them sound like plausible village/block names in isolation: Piggery,
Poultry, Dairy, Goat (Farming), Warehouse, Sericulture (& Weaving), Green
Taxi, Motorcaravan, Cinema Theatre, Sports & Wellness (Centre), Any Business
Venture, Agro Tourism Villa, PRIME Tourism Vehicle, PRIME Agriculture
Response Vehicle, PRIME Small Enterprise Empowerment / SEED. "What is the
status distribution for Piggery?" names a sub-scheme, not a district, block
or village — extract nothing.

Examples:
Question: "Tell me about total disbursement of Selsella across all financial years for MGNREGA."
JSON: {{"block": "Selsella"}}
Question: "What is the total expenditure of West Garo Hills under PMAY-G?"
JSON: {{"district": "West Garo Hills"}}
Question: "how many job cards issued in Ri Bhoi"
JSON: {{"district": "Ri Bhoi"}}
Question: "What is the status distribution for Piggery?"
JSON: {{}}
Question: "How many applicants under Poultry are on hold?"
JSON: {{}}
Question: "show me MGNREGA spend"
JSON: {{}}
Question: "Compare the sanctioned amounts of Dambo Rongjeng and Samanda, the block, not the village"
JSON: {{"blocks": ["Dambo Rongjeng", "Samanda"]}}
Question: "Compare PMAY performance between ekh and wgh for FY 2017-18"
JSON: {{"districts": ["ekh", "wgh"], "year": "2017-18"}}
Question: "which district received highest total disbursement for Focus Plus"
JSON: {{}}

Question: "{question}"
JSON:"""
    raw = await llm.call_classifier(prompt, guided={"guided_json": _ENTITY_JSON_SCHEMA})
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        return {}
    out: dict[str, object] = {}
    for k, v in payload.items():
        if k in ("district", "block", "village", "year", "assembly_constituency") \
                and isinstance(v, str) and v.strip():
            cleaned = _clean_mention(v)
            if cleaned and _mention_in_question(cleaned, question):
                out[k] = cleaned
    blocks_raw = payload.get("blocks")
    if isinstance(blocks_raw, list):
        cleaned_blocks: list[str] = []
        seen: set[str] = set()
        for v in blocks_raw:
            if not isinstance(v, str):
                continue
            c = _clean_mention(v)
            if c and _mention_in_question(c, question) and c.lower() not in seen:
                seen.add(c.lower())
                cleaned_blocks.append(c)
        if len(cleaned_blocks) >= 2:
            out["blocks"] = cleaned_blocks
        elif len(cleaned_blocks) == 1 and "block" not in out:
            # A single-element array is just a singular mention the model
            # phrased as a list — fold it back so the existing singular path
            # (block-vs-village disambiguation etc.) still runs for it.
            out["block"] = cleaned_blocks[0]
    districts_raw = payload.get("districts")
    if isinstance(districts_raw, list):
        cleaned_districts: list[str] = []
        seen_d: set[str] = set()
        for v in districts_raw:
            if not isinstance(v, str):
                continue
            c = _clean_mention(v)
            if c and _mention_in_question(c, question) and c.lower() not in seen_d:
                seen_d.add(c.lower())
                cleaned_districts.append(c)
        if len(cleaned_districts) >= 2:
            out["districts"] = cleaned_districts
        elif len(cleaned_districts) == 1 and "district" not in out:
            # A single-element array is just a singular mention the model
            # phrased as a list — fold it back so the existing singular path
            # runs for it.
            out["district"] = cleaned_districts[0]
    return out


def _parse_year_key(text: str) -> "int | None":
    """FY start year as an int from '2023', '2023-24', '2023-2024', 'FY 2023-24',
    'FY23', or a bare two-digit range like '25-26' with no century at all —
    users type financial years this way constantly (2026-09-10 UAT: "25-26" was
    not understood as a year), and without this branch the mention never
    resolves to a year_key at all. Returns None if no plausible year
    (2010-2039) is present."""
    m = re.search(r"\b(20[1-3]\d)\s*[-/]\s*(?:20)?\d{2}\b", text)   # 2023-24 / 2023-2024
    if m:
        return int(m.group(1))
    m = re.search(r"\bfy\s*'?(\d{2})\b", text, re.IGNORECASE)        # FY23
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"(?<!\d)(\d{2})\s*[-/]\s*(\d{2})(?!\d)", text)    # bare 25-26
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if 10 <= y1 <= 39 and y2 == (y1 + 1) % 100:
            return 2000 + y1
    m = re.search(r"\b(20[1-3]\d)\b", text)                          # bare 2023
    if m:
        return int(m.group(1))
    return None


def _village_chip_question(question: str, raw_text: str, candidate: dict) -> str:
    """The follow-up question text for one village-disambiguation chip.

    The old approach appended "<block> block, <district>" to the ORIGINAL
    ambiguous fragment (e.g. "siju") and left the fragment itself unchanged.
    That only narrows resolve_village's search scope — it does not say WHICH
    candidate was picked. Two candidates that both fuzzy-match the same raw
    text and also sit in the same block (e.g. "siju" fuzzy-matches both SIJU
    SONGMONG and Siju Arteka, both in SIJU block, SOUTH GARO HILLS) stay tied
    even after the block/district is appended, so every chip regenerates the
    IDENTICAL question and the clarification loops forever — reported
    2026-09-09 for both an exact-duplicate dim_geography row (Asimgre, DALU
    block) and this fuzzy-match case (siju). Substituting the candidate's own
    canonical name into the question fixes it: re-resolution then runs an
    EXACT match on a name that is (almost always, and after the duplicate-row
    fix in entity_resolver.resolve_village, effectively always) unique.

    A second, compounding bug (also reported 2026-09-09, "nongthymmai" in
    EAST/WEST KHASI HILLS): when the LLM mention-extractor can't cleanly split
    a resumed chip's OWN text ("Nongthymmai, RI MULIANG block, WEST KHASI
    HILLS") into village/block, resolve_entities lands back on this same
    ambiguous-village branch and calls this function AGAIN — with `question`
    now already carrying the previous round's appended ", <block> block,
    <district>". Blindly appending another one lets the suffix grow every
    round ("..., RI MULIANG block, WEST KHASI HILLS, MAWSHYNRUT block, WEST
    KHASI HILLS, ...", naming more and more blocks at once), which defeats any
    single-candidate text match downstream and loops forever. Strip a
    previously-appended suffix (there is at most one meaningful one — this
    function is the only writer of that shape) before appending the current
    candidate's, so the text never grows past one such suffix."""
    stem = question.strip().rstrip(" ?.")
    stem = re.sub(r"(,\s*[^,]+?\s+block,\s*[^,]+)+$", "", stem, flags=re.IGNORECASE).rstrip()
    name = candidate["name"]
    if raw_text and re.search(re.escape(raw_text), stem, re.IGNORECASE):
        pinned = re.sub(re.escape(raw_text), name, stem, count=1, flags=re.IGNORECASE)
    else:
        pinned = f"{stem} ({name})"
    return f"{pinned}, {candidate['block']} block, {candidate['district']}"


# ── "Is that a village, a block, or a constituency?" ────────────────────────
# Block / assembly-constituency / village names overlap massively in Meghalaya
# (see entity_resolver.collides_across_dimensions and the
# cross_dimension_collisions block in mgnrega_entity_resolver.yaml). A bare
# "Sohra" is a real assembly constituency (AC 28, East Khasi Hills); it is NOT
# a block and NOT a village, but "Sohrarim" IS a village, so the old flow —
# extractor tags the bare name "block" -> no such block -> fall back to
# resolve_village -> fuzzy-match Sohrarim -> answer — reported a specific
# village's 643 beneficiaries as though the user had asked about Sohra
# (reported 2026-09-15). Every level the name could mean is now offered as a
# one-tap chip, and nothing is filtered until the user picks one.
#
# The chip question appends an explicit level word, which the resolvers and the
# mention-extractor both already key on: the extractor's own prompt uses
# "constituency"/"AC"/"assembly" to tag assembly_constituency, and the
# block/village branches below check for a literal "block"/"village" word to
# skip their own disambiguation. So a resumed chip resolves straight through
# without re-triggering this pause.
_DIM_CHIP_WORD = {
    "district": "district",
    "block": "block",
    "assembly_constituency": "assembly constituency",
    "village": "village",
}
_DIM_CHIP_LABEL = {
    "district": "district",
    "block": "C&RD block",
    "assembly_constituency": "assembly constituency",
    "village": "village",
}


def _dimension_collision_clarification(question: str, name: str,
                                       dims: "dict[str, str]") -> "ClarificationNeeded":
    """Ask which ADMIN LEVEL a bare, level-ambiguous place name refers to.

    `dims` is {dimension: canonical display name} from
    entity_resolver.collides_across_dimensions. Each chip substitutes that
    canonical name for whatever the user typed, so a misspelling is corrected
    at the same time as the level is chosen ("malwai" -> "Mawlai"); without
    that substitution the resumed question carries the typo forward and fails
    block/constituency resolution all over again, silently landing back on the
    village. The village chip keeps the user's own text — village names are
    DB-backed and resolved by their own branch."""
    stem = question.strip().rstrip(" ?.")
    options = []
    for dim, canonical in dims.items():
        shown = canonical or name
        # Swap the typed name for the canonical one in the question itself.
        if canonical and canonical.lower() != str(name).lower():
            resumed = re.sub(re.escape(str(name)), canonical, stem, count=1,
                             flags=re.IGNORECASE)
        else:
            resumed = stem
        options.append({
            "label": f"The {shown} {_DIM_CHIP_LABEL[dim]}",
            "question": f"{resumed}, the {_DIM_CHIP_WORD[dim]}, not another area type",
        })
    labels = list(dims)
    listed = ", ".join(_DIM_CHIP_LABEL[d] for d in labels[:-1]) + \
        f" or {_DIM_CHIP_LABEL[labels[-1]]}"
    return ClarificationNeeded(
        f"“{name}” could refer to more than one kind of area in Meghalaya — the "
        f"{listed}. These cover different places and give different numbers, so "
        "please pick the one you mean.",
        options=options,
        rule="entity-ambiguous",
    )


# The level word a resumed collision chip (or the user, unprompted) put in the
# question — "..., the assembly constituency, not another area type". When
# present, the level is already settled and the collision gate must not fire
# again; it also tells the branches below which dimension to force.
_EXPLICIT_LEVEL_RE = {
    "assembly_constituency": re.compile(
        r"\b(assembly\s+constituenc\w*|constituenc\w*|\bAC\b|assembly|MLA)\b", re.IGNORECASE),
    "block": re.compile(r"\bblock\b", re.IGNORECASE),
    "village": re.compile(r"\bvillage\b", re.IGNORECASE),
    "district": re.compile(r"\bdistrict\b", re.IGNORECASE),
}


# Assembly constituency is recorded on exactly ONE fact directly:
# curated.fact_mgnrega_employment (surfaced as curated.v_employment, see
# data/schema/schema_for_developers.md). It does not exist on MGNREGA
# expenditure, nor on PMAY-G / Focus Plus / CM Elevate at all — the resolver
# YAML says so outright ("expenditure: null — this dimension does not exist in
# mgnrega_expenditure"). Offering "the X assembly constituency" as a chip for a
# question about expenditure or houses would therefore invite the user to pick
# a reading that can never be answered, so the chip is suppressed for those.
#
# FOCUS LEGACY IS THE EXCEPTION, and its own contract is explicit about it:
# v_focus_legacy exposes geography_key, so joining curated.dim_geography for
# ac_name/ac_number is a permitted dimension join on a declared FK
# (focuslegacy_schema_partitions.yaml semantic_rules.constituency_rule,
# status ANSWERABLE_ONLY_BY_AN_EXPLICIT_DIMENSION_JOIN; the worked SQL is
# sanctioned_patterns.constituency in the join-graph YAML, and
# schema_context's Focus Legacy block already ships it). The rule's own
# runtime_behavior says "Answer the question ... Do not silently refuse", so
# suppressing the AC reading for this scheme hid a level the data can answer:
# "How many Producer Groups are mapped to Amlarem?" offered only block and
# village, though Amlarem is also a constituency (reported 2026-09-23).
#
# The employment measures that DO carry it, per that same schema: person-days,
# households/persons employed, job cards, 100-days completions, women
# employment. Anything else on MGNREGA is expenditure-side.
_AC_CAPABLE_METRIC = re.compile(
    r"\bperson[\s-]?days?\b|\bjob\s?cards?\b|\bmuster\b|"
    r"\b100[\s-]?days?\b|\bhundred\s+days?\b|"
    r"\b(?:households?|persons?|people|women|men)\b[^?.!]{0,30}\b"
    r"(?:employ\w*|work\w*|receiv\w*)\b|"
    r"\bemploy\w*\b|\bbeneficiar\w*\b|\bworkers?\b",
    re.IGNORECASE,
)
# Metric words that are unambiguously expenditure-side — no AC column exists
# for these even within MGNREGA.
_AC_INCAPABLE_METRIC = re.compile(
    r"\bexpenditure\b|\bspend(?:ing)?\b|\bspent\b|\bwages?\b|\bwage\s+bill\b|"
    r"\bmaterial\s+cost\b|\bamount\b|\bcost\b|\butili[sz]ation\b|"
    r"\bhouses?\b|\bsanction\w*\b|\breleas\w*\b|\bdisburs\w*\b|"
    r"\binstal{1,2}ments?\b|\bapplications?\b",
    re.IGNORECASE,
)


# The schemes whose data can answer a constituency question at all. MGNREGA
# carries ac_name on its employment fact; Focus Legacy reaches it through the
# documented dim_geography join on geography_key. PMAY-G, Focus Plus and CM
# Elevate have no route to it and keep the unconditional refusal. CM Elevate
# Legacy reaches it the same way as Focus Legacy (geography_key -> dim_geography,
# a declared FK), and the join matches the source workbook's own
# mapped_constituency_name exactly (Mairang 52 = 52, verified 2026-09-25).
_AC_CAPABLE_SCHEMES = ("MGNREGA", "Focus Legacy", "CM Elevate Legacy")


def _ac_dimension_available(question: str, schemes: list[str]) -> bool:
    """True when an assembly-constituency reading of a place name could
    actually be queried for THIS question. False suppresses the AC chip."""
    _live = list(schemes or [])
    if len(_live) != 1 or _live[0] not in _AC_CAPABLE_SCHEMES:
        return False
    if _live[0] == "CM Elevate Legacy":
        # Every measure on this view (records, sanctioned, subsidy, loan,
        # disbursement) hangs off the same geography_key, so the MGNREGA
        # employment-vs-expenditure metric test does not apply.
        return True
    q = question or ""
    # An explicit expenditure/housing metric rules it out even if an
    # employment-ish word also appears ("wage employment expenditure").
    if _AC_INCAPABLE_METRIC.search(q) and not _AC_CAPABLE_METRIC.search(q):
        return False
    if _AC_CAPABLE_METRIC.search(q):
        return True
    # No metric named at all ("figures for Sohra") — leave the reading open
    # rather than silently dropping a valid choice.
    return not _AC_INCAPABLE_METRIC.search(q)


# ── Step 2 of the hierarchy: narrowing inside a chosen constituency ─────────
# Once the user has said "I meant the constituency", they may still want only
# part of it. An AC is an electoral boundary rather than an administrative
# parent — 8 of the 56 straddle two districts — so the narrowing offered is
# built from what that constituency ACTUALLY contains in the data
# (entity_resolver.constituency_contents), never from a static hierarchy.
#
# The phrasing of each chip is what makes the next turn resolve cleanly: the
# district/block chips keep the constituency name AND add the area, so the
# question stays scoped to both; "the whole constituency" simply confirms.
_AC_SCOPED_RE = re.compile(r",\s*within\s+the\s+", re.IGNORECASE)
# The narrowing a resumed drill-down chip carries: ", within the <NAME>
# <district|block> only". Read deterministically rather than left to the LLM
# mention-extractor — the extractor has no reason to tag a second place name
# in a question that already names a constituency, and when it doesn't, the
# narrowing is silently lost and the answer covers the whole constituency
# again (the very thing the user just declined).
_AC_NARROW_RE = re.compile(
    r",\s*within\s+the\s+(?P<name>.+?)\s+(?P<level>district|block)\s+only\b",
    re.IGNORECASE,
)


def _ac_drilldown_clarification(question: str, ac_display: str,
                                contents: dict) -> "ClarificationNeeded | None":
    """Offer to narrow inside a just-chosen assembly constituency, or None when
    there is nothing meaningful to narrow to."""
    districts = contents.get("districts") or []
    blocks = contents.get("blocks") or []
    if not districts and not blocks:
        return None
    stem = question.strip().rstrip(" ?.")
    options = [{"label": f"The whole {ac_display} constituency",
                "question": f"{stem}, the whole constituency"}]
    # A constituency spanning two districts is the one case where the district
    # step is a real question rather than a formality.
    if len(districts) > 1:
        for d in districts:
            options.append({
                "label": f"Only the {str(d).title()} part",
                "question": f"{stem}, within the {d} district only",
            })
    for b in blocks[:8]:
        options.append({
            "label": f"{str(b).title()} block",
            "question": f"{stem}, within the {b} block only",
        })
    if len(options) < 2:
        return None
    where = (f"spans {len(districts)} districts and " if len(districts) > 1 else "covers ")
    return ClarificationNeeded(
        f"The {ac_display} assembly constituency {where}"
        f"{len(blocks)} C&RD block(s), with {contents.get('villages', 0)} villages in the "
        "employment data. Do you want the whole constituency, or just part of it?",
        options=options,
        rule="ac-narrow-scope",
    )


# ── Deterministic village backstop ──────────────────────────────────────────
# scan_dimension() already backstops a district the LLM mention-extractor
# dropped. Villages had no equivalent, and the extractor drops them too —
# confirmed live 2026-09-15: "total beneficiaries in ASIMGRE for MGNREGA for
# East Khasi Hills" came back {"district": "East Khasi Hills"} with ASIMGRE
# missing entirely. With no village mention there is nothing to resolve, the
# ambiguity check never runs, and the filter silently disappears: the query
# counted the WHOLE district while the answer still said "for ASIMGRE".
#
# A village scan has to be much more careful than the district one. There are
# ~6,000 villages and resolve_village()'s trigram stage matches loosely enough
# that ordinary words — and fragments of district names like "East", "Garo",
# "Hills" — all hit something. So this scan is doubly constrained:
#   1. candidates come only from a place-preposition phrase ("in X", "for X",
#      "of X"), the grammar that actually introduces a place; and
#   2. a candidate must match a village name EXACTLY (village_names_exact),
#      never fuzzily.
# A name that is already a known district / block / constituency is skipped —
# those dimensions own it, and the admin-level collision gate handles the
# genuinely ambiguous ones.
# The name may carry a parenthesised suffix that is PART of it — Meghalaya has
# "NONGCHRAM (I)" and "NONGCHRAM (II)" as two distinct villages in the same
# block. Without the optional "(...)" tail the scan proposes a bare "NONGCHRAM",
# which matches no stored name exactly, so the backstop finds nothing and the
# village filter is silently lost (reported 2026-09-17).
# Candidate place phrases after a preposition. Deliberately tolerant on TWO
# axes, because both were observed dropping a real village:
#   CASE — users type lowercase ("in william nagar(mb) - ward no.4"). A
#     capital-first pattern skipped those entirely, so the backstop never ran
#     and the truncated extractor mention went unchallenged.
#   SUFFIX CHAIN — a name can carry MORE THAN ONE part-marker:
#     "William Nagar (MB) - Ward No.4" is a parenthesised marker AND a
#     hyphenated one. Matching only the first stops at "William Nagar (MB)",
#     which is ambiguous across 12 wards and loops the clarification forever
#     (reported 2026-09-17).
# Precision still comes from village_names_exact(): a candidate only counts
# if it matches a stored village name EXACTLY, so a loose phrase costs one
# lookup and nothing more.
_PLACE_PREP_RE = re.compile(
    r"\b(?:in|for|of|at|from|within|under)\s+"
    r"(?P<name>[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3}"
    # A hyphenated marker is at most TWO short tokens ("- A", "- Ward No.4") —
    # bounded so it cannot run on into the rest of the sentence ("- Ward No.4
    # for CM Elevate"), which would never match a stored name.
    r"(?:\s*\([A-Za-z0-9 .'-]{1,20}\)"
    r"|\s*-\s*[A-Za-z0-9][A-Za-z0-9.']{0,11}(?:\s+[A-Za-z0-9][A-Za-z0-9.']{0,11})?"
    r")"
    r"{0,2})",
)
# Words that open a phrase without naming a place; a candidate that is only
# these is never a village.
_NOT_A_PLACE_WORD = {
    "mgnrega", "mnrega", "nrega", "pmay", "pmayg", "awaas", "awas",
    "focus", "focus plus", "focusplus", "cm", "cm elevate", "cmelevate",
    "meghalaya", "fy", "financial", "financial year", "all", "each", "every",
    "the", "a", "an", "this", "that", "total", "district", "block", "village",
    "assembly", "constituency", "state", "india", "government", "scheme",
}


def _village_scan_candidates(question: str, scheme: str) -> list[str]:
    """Names in `question` that could be a village the extractor missed."""
    known: set[str] = set()
    for dim in ("district", "block", "assembly_constituency"):
        for n in canonical_names(scheme, dim):
            known.add(n.strip().upper())
    out: list[str] = []
    # Lookahead so matches can OVERLAP: finditer consumes what it matches, so a
    # phrase starting at an earlier preposition ("under goat farming scheme in")
    # would swallow the "in" that introduces the real place and the village
    # would never be scanned at all (reported 2026-09-17, lowercase "in william
    # nagar(mb) - ward no.4"). Every preposition now gets its own attempt.
    for m in re.finditer(rf"(?={_PLACE_PREP_RE.pattern})", question or "",
                         re.IGNORECASE if _PLACE_PREP_RE.flags & re.IGNORECASE else 0):
        raw = (m.group("name") or "").strip().rstrip(".,")
        if not raw:
            continue
        # Try the longest phrase first, then progressively shorter prefixes, so
        # "ASIMGRE for MGNREGA" still yields the bare "ASIMGRE".
        words = raw.split()
        for take in range(len(words), 0, -1):
            cand = " ".join(words[:take]).strip()
            low = cand.lower()
            if len(cand) < 4 or low in _NOT_A_PLACE_WORD:
                continue
            if cand.upper() in known:
                break        # a district/block/AC owns this name — not our job
            if cand not in out:
                out.append(cand)
    return out


async def _scan_village_in_question(question: str, scheme: str) -> "tuple[str, list[dict]] | None":
    """(name, candidate villages) for a village named in the question that the
    extractor dropped, or None. Exact matches only."""
    cands = _village_scan_candidates(question, scheme)
    if not cands:
        return None
    found = await village_names_exact(cands)
    if not found:
        return None
    # Preserve the order the names appear in the question.
    for c in cands:
        hits = found.get(c.upper())
        if hits:
            return c, hits
    return None


def _canonical_in_question(question: str, scheme: str, dimension: str) -> "str | None":
    """The catalogue name for `dimension` that appears verbatim in `question`,
    or None. Used when resuming an admin-level collision chip: the chip put the
    CANONICAL name into the question text, so reading it back from there is
    more reliable than trusting the LLM extractor, which may still be echoing
    the user's original typo. Longest match wins, so "North Tura" is not
    shadowed by "Tura"."""
    best: str | None = None
    # collision_canonical_names, not canonical_names: for block / assembly
    # constituency it falls back to another scheme's catalogue when the asking
    # scheme has none, which is what lets the admin-level gate see an AC
    # reading under CM Elevate (its catalogue has no AC dimension at all).
    for canon in collision_canonical_names(scheme, dimension):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(canon)}(?![A-Za-z0-9])",
                     question or "", re.IGNORECASE):
            if best is None or len(canon) > len(best):
                best = canon
    return best


# A village-disambiguation chip appends its scope as ", <BLOCK> block,
# <DISTRICT>" (see _village_chip_question). Those words name the CONTAINING
# area, not the level the question is about — the question is about the
# village. Left in place they make _explicit_level_in report "block", which
# both suppresses the village backstop and lets the block slot win, so the chip
# resolves to block grain and the village is lost (confirmed 2026-09-15 on the
# ASIMGRE chips). Strip that trailing scope before detecting the level.
# Matches only the village-chip scope tail: a block name (never the word "the")
# followed by a district name, both in CAPS as _village_chip_question writes
# them, at the very end. The admin-level CHOICE chip (", the block, not another
# area type") does not match — it has "the" before "block" and trailing prose
# after the second comma.
_CHIP_SCOPE_SUFFIX_RE = re.compile(
    r",\s*[A-Z][A-Za-z.'\- ]*\s+block\s*,\s*[A-Z][A-Za-z.'\- ]*\s*$")
# The level phrase a disambiguation chip always carries. Its presence is what
# marks a question as a chip resume, so the scope tail above is only stripped
# then — never from an ordinary question that happens to end the same way.
_CHIP_LEVEL_PHRASE_RE = re.compile(
    r",\s*the\s+(?:village|block|district|assembly\s+constituency)\b", re.IGNORECASE)
# "each village", "by block", "per district", "village-wise", "all villages" —
# these name the GROUPING a breakdown is computed over, never the place the
# question is scoped to. Removed before level detection so the surviving level
# word (if any) is the one that actually names a place.
_BREAKDOWN_LEVEL_PHRASE_RE = re.compile(
    r"\b(?:by|per|each|every|all|across)\s+(?:the\s+)?"
    r"(?:village|block|district|assembly\s+constituenc\w*|constituenc\w*)s?\b|"
    r"\b(?:village|block|district)[\s-]?wise\b",
    re.IGNORECASE,
)


def _explicit_level_in(question: str) -> "str | None":
    """The admin level the question names outright, or None. Finest-first so a
    question naming two levels ("village in X district") reports the one being
    asked about rather than the containing area."""
    q = question or ""
    # Only strip the chip's scope tail when the question ALSO carries a chip's
    # level phrase (", the village, not another area type"). A user-typed
    # question can end in the same ", <NAME> block, <DISTRICT>" shape —
    # "... , BATABARI block, WEST GARO HILLS" — and stripping that deleted the
    # word "block" the user had explicitly written, so the level read as
    # unstated and a same-named VILLAGE won instead (reported 2026-09-18: the
    # BATABARI block question resolved to village 272854, 1 row, against 55 in
    # the block).
    if _CHIP_LEVEL_PHRASE_RE.search(q):
        q = _CHIP_SCOPE_SUFFIX_RE.sub("", q)
    # A BREAKDOWN phrase names the grouping, not the place being asked about:
    # "in each village of Betasing block" asks for a per-village split OF THE
    # BLOCK. Reading "village" as the stated level there made the block branch
    # prefer a village reading and resolve a same-named village instead, so the
    # query filtered one village while grouping by village — 0 rows against a
    # real 6 (reported 2026-09-18). Drop the grouping words before detecting.
    q = _BREAKDOWN_LEVEL_PHRASE_RE.sub(" ", q)
    for dim in ("village", "assembly_constituency", "block", "district"):
        if _EXPLICIT_LEVEL_RE[dim].search(q):
            return dim
    return None


async def resolve_entities(question: str, schemes: list[str],
                            prior_resolved: "dict | None" = None,
                            village_hint: "str | None" = None) -> dict:
    """Resolve every extracted mention to a canonical DB value. Ambiguous ->
    raise ClarificationNeeded (ask, per the resolver's own hard rule — never
    guess). Not-found is not an error: it means the value genuinely is not in
    this data, which the response composer should say plainly, not silently
    drop the filter.

    `prior_resolved` (optional): the previous turn's resolved entities, passed
    only when this question is a rewritten follow-up. Used purely as a
    fallback — for any dimension (district/block/village_code/year_key) the
    CURRENT question names nothing for at all, carry the prior turn's value
    forward instead of leaving it unset. This covers a village/year the
    follow-up rewrite paraphrased away in its wording (the rewrite works from
    prev.question/answer TEXT, so an entity can silently vanish if it doesn't
    literally reappear). A dimension the current question DOES name something
    for (even something that fails to resolve) is left alone — the user is
    talking about something new for that slot, not carrying the old one over.

    `village_hint` (optional): the exact village-name text from a just-resumed
    village-ambiguity pause (ClarificationNeeded.village_hint / Session.
    pending_village_hint). Used ONLY when this question's own mention
    extraction finds no village at all — the merged resume text ("...the one
    in Betasing block") does not reliably make the LLM re-tag the original
    village name, and without it SQL generation has no village_code to filter
    on and can invent one instead of using the now-resolved block/district."""
    # Out-of-range financial year — checked FIRST, before the LLM mention call
    # and any geography resolution. Works purely off the raw question text, so it
    # still fires when the extractor is unavailable or drops the year slot (a
    # comma-joined scope-pause reply like "wgh, 1999-20" does exactly that). A
    # year outside the data window makes the whole question unanswerable
    # regardless of the place, so this must not depend on anything downstream.
    _year_gap_note: "str | None" = None
    if settings.YEAR_RANGE_GUARD_ENABLED:
        _yraw = _out_of_range_year_in(question, schemes)
        if _yraw is not None:
            # Only refuse when NOTHING in the question is answerable. A question
            # naming an absent year AND a valid one ("compare FY 2023-24 and FY
            # 2024-25" — the Focus Legacy gap) used to be refused outright, which
            # asked the user to re-pick years they had already named, from a list
            # that deliberately excludes the one they asked about. Drop the
            # absent year, note the gap so the answer states it, and answer for
            # the years that exist — which is what the scheme's own response
            # contract requires (fy_gap_note).
            question, _year_gap_note, _handled = _apply_year_gap(question, schemes)
            if not _handled:
                raise _year_out_of_range_clarification(question, _yraw, schemes)

    mentions = await extract_entity_mentions(question)
    mentions = _drop_producer_group_names(question, mentions)
    mentions = _backfill_explicit_year(question, schemes, mentions)
    # Every raw-text place SCAN below reads this copy, so a producer-group name
    # the user labelled as one is never re-found as a block/village/constituency.
    _scan_q = _question_without_pg_name(question)

    resolved: dict[str, object] = {}
    notes: list[str] = []
    if _year_gap_note:
        # Stated in the answer, so a dropped gap year is visible to the user
        # rather than silently ignored.
        notes.append(_year_gap_note)
    # Human-readable names for whatever the query ends up filtering on, keyed by
    # dimension. Handed to the response composer so it says "West Garo Hills",
    # not the "wgh" the user typed or the "WEST GARO HILLS" DB literal.
    display: dict[str, str] = {}

    # The admin level the question states outright — either because the user
    # said it ("the Sohra constituency") or because this is a resumed
    # admin-level collision chip ("..., the assembly constituency, not another
    # area type"). Computed HERE, before any per-dimension branch runs: the
    # district branch below can resolve a name as a village on its own
    # fall-through, so a guard placed later would come too late to stop it.
    _stated_level = _explicit_level_in(question)

    # A resumed collision chip states the level outright, but the LLM
    # mention-extractor still has to notice and re-tag the name into the
    # matching slot — the same extractor whose mis-tagging is what raised the
    # pause to begin with. When the level is stated and the extractor put the
    # name in a DIFFERENT slot, move it deterministically: the user has said
    # which level they mean, so it is no longer the model's call. Runs before
    # every resolution branch, so the name resolves at the chosen level and
    # nowhere else.
    # When the question states a level outright, that level is settled — put
    # the place name there and clear every other place slot, so nothing
    # downstream can re-resolve it somewhere else.
    #
    # The name is re-derived from the question TEXT rather than taken from the
    # extractor. A resumed chip rewrites the question to carry the CANONICAL
    # name ("...in MAWLAI..."), but the extractor frequently echoes the user's
    # ORIGINAL typo back — often into the very slot the chip names
    # ("block": "malwai" for the Mawlai-block chip). That value is present but
    # unresolvable, so trusting it makes the branch fail and fall through to
    # the village: the typo's own version of the bug this gate exists to stop.
    if _stated_level in ("district", "block", "assembly_constituency", "village"):
        _placed = None
        if _stated_level == "village":
            # Village is DB-backed — its own branch resolves it. Keep whatever
            # name is already in hand (extractor value, else the hint).
            _placed = mentions.get("village") or mentions.get("block")                 or mentions.get("district") or village_hint
        else:
            _placed = _canonical_in_question(_scan_q, schemes[0], _stated_level)
            if not _placed:
                # No catalogue name in the text — fall back to whatever the
                # extractor found, but only if it resolves at this level.
                for _slot in ("district", "block", "village", "assembly_constituency"):
                    _v = mentions.get(_slot)
                    if _v and resolve_dimension(
                            str(_v), schemes[0], _stated_level).status == "resolved":
                        _placed = str(_v)
                        break
        if _placed:
            for _slot in ("district", "block", "village", "assembly_constituency"):
                mentions.pop(_slot, None)
            mentions[_stated_level] = _placed
            logger.info("explicit level %r — placed %r, cleared other place slots",
                        _stated_level, _placed)

    # ── Admin-level collision gate ──────────────────────────────────────────
    # Before resolving ANY single-name mention, check whether the bare name can
    # name more than one KIND of area (block vs assembly constituency vs
    # village vs district). This runs ahead of every per-dimension branch below
    # on purpose: those branches each resolve one slot in isolation and cannot
    # see that the same text also names a different level, which is how a bare
    # "Sohra" (assembly constituency) ended up fuzzy-matched to the village
    # Sohrarim and answered silently.
    #
    # Skipped when the question already names the level outright ("the Sohra
    # constituency", or a resumed chip's "..., the assembly constituency, not
    # another area type") — the level is settled, nothing to ask. Also skipped
    # for a plural comparison ("districts"/"blocks" arrays), which name their
    # own level by construction, and when a village_hint is carrying a
    # just-resumed village-ambiguity choice.
    if _stated_level is None and not village_hint \
            and not mentions.get("districts") and not mentions.get("blocks"):
        _scheme0 = schemes[0] if schemes else ""
        # The gate can only examine names the extractor handed over, and the
        # extractor drops a plainly-named place often enough to matter: it
        # returned {} on 5 of 5 calls for "applicants in mylliem under Green
        # Taxi CM Elevate and ware house Scheme" (2026-09-18), the lowercase
        # name buried between two scheme names. With no mention there was
        # nothing to test for ambiguity, the gate stayed silent, and the
        # generator filtered lgd_village_name = 'MYLLIEM' — a confident false
        # zero, where MYLLIEM is really a block (and an assembly constituency)
        # holding 2 applicants for those two schemes.
        #
        # So when no place mention survived, scan the raw question for a
        # catalogue name at either ambiguous level. This only ever ADDS a
        # candidate to test; whether it actually pauses is still decided by
        # collides_across_dimensions below, which needs 2+ readings.
        if not any(mentions.get(s) for s in
                   ("district", "block", "village", "assembly_constituency")):
            _found = None
            for _dim in ("block", "assembly_constituency"):
                _found = _canonical_in_question(_scan_q, _scheme0, _dim)
                if _found:
                    mentions = {**mentions, _dim: _found}
                    logger.info("admin-level gate: %r scanned from question text "
                                "(extractor returned no place)", _found)
                    break
            # _canonical_in_question matches names VERBATIM, so a MISSPELLED
            # place the extractor also dropped stayed invisible: "applicants in
            # tikrikulla ..." (a typo of the TIKRIKILLA block) resolved no
            # place at all, the generator passed the user's own spelling into
            # `lgd_block = 'TIKRIKULLA'`, and the query returned a confident
            # zero where the block really holds 243 applicants for those three
            # schemes (2026-09-18). resolve_dimension and
            # collides_across_dimensions both handle that typo; only the scan
            # feeding them was exact-only.
            #
            # So fall back to the place-phrase candidates ("in <name>") and let
            # the resolver's own fuzzy stage judge them. Whether this actually
            # pauses is still decided by collides_across_dimensions below,
            # which needs 2+ readings — this only supplies a name to test.
            if not _found:
                for _cand in _village_scan_candidates(question, _scheme0):
                    for _dim in ("block", "assembly_constituency"):
                        _r = resolve_dimension(_cand, _scheme0, _dim)
                        if _r.status == "resolved":
                            mentions = {**mentions, _dim: _cand}
                            logger.info("admin-level gate: %r fuzzy-matched %s %r "
                                        "(extractor returned no place)",
                                        _cand, _dim, _r.canonical)
                            _found = _cand
                            break
                    if _found:
                        break
        for _slot in ("district", "block", "village", "assembly_constituency"):
            _name = mentions.get(_slot)
            if not _name:
                continue
            # Every admin level this name resolves to. The in-memory catalogues
            # (district / block / assembly_constituency) are checked exact +
            # alias only — never fuzzy, or the gate would fire on almost every
            # name. The village catalogue is DB-backed (curated.dim_geography)
            # so it cannot be in that check; probe it here and pass the result
            # in. That village half is what catches "Sohra": an exact
            # assembly-constituency hit whose only competing reading is a
            # village. Without it the AC hit stands alone, the gate stays
            # quiet, and the old fall-through silently answers about the wrong
            # place.
            # The extractor routinely returns a TRUNCATED mention for a village
            # whose name carries part-markers: "william nagar(mb) - ward no.4"
            # comes back as "william nagar(mb)", which is ambiguous across 12
            # wards and makes this gate ask a question the text already answers
            # (reported 2026-09-17 — the pause repeated forever, the question
            # growing each round). When the raw text contains a LONGER name
            # that resolves to exactly one village, that is the real mention:
            # nothing is ambiguous, so the gate must stand down.
            _scanned = await _scan_village_in_question(_scan_q, _scheme0)
            if _scanned and len(_scanned[1]) == 1 \
                    and str(_name).strip().lower() in _scanned[0].strip().lower() \
                    and len(_scanned[0]) > len(str(_name)):
                logger.info("admin-level gate: %r is a truncation of %r, which resolves "
                            "to one village — not ambiguous", _name, _scanned[0])
                break
            _vr_probe = await resolve_village(str(_name))
            _dims = collides_across_dimensions(
                str(_name), _scheme0,
                village_hit=_vr_probe.status in ("resolved", "ambiguous"))
            # Drop the assembly-constituency reading when this question's
            # metric has no AC column to read (see _ac_dimension_available) —
            # offering it would invite a choice that can never be answered.
            # If that leaves fewer than two readings, there is nothing left to
            # ask about and the remaining branches resolve it normally.
            if "assembly_constituency" in _dims and not _ac_dimension_available(question, schemes):
                _dims = {k: v for k, v in _dims.items() if k != "assembly_constituency"}
                logger.info("admin-level collision on %r: AC reading suppressed "
                            "(no constituency data for this metric)", _name)
                if len(_dims) < 2:
                    _dims = {}
            if _dims:
                logger.info("admin-level collision on %r: %s — asking", _name, list(_dims))
                raise _dimension_collision_clarification(question, str(_name), _dims)
            break

    district_canon = None
    if mentions.get("districts"):
        # Two-or-more named districts to compare against each other (see
        # extract_entity_mentions) — resolve each independently and carry the
        # whole set forward as an IN-list, instead of the singular "district"
        # dimension which can only ever hold one value and would silently
        # drop every district but the last (the "compare ekh and wgh" bug).
        _district_canons: list[str] = []
        _district_displays: list[str] = []
        for _dname in mentions["districts"]:
            r = resolve_dimension(_dname, schemes[0], "district")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which district is being referred to by “{_dname}”?",
                                           rule="entity-ambiguous")
            if r.status != "resolved":
                notes.append(f"'{_dname}' is not a known district — say so, do not filter on it.")
                continue
            _district_canons.append(r.canonical)
            _district_displays.append(r.display or str(_dname).title())
        if _district_canons:
            resolved["district_list"] = _district_canons
            display["district"] = " and ".join(_district_displays)
    else:
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
        # A hill-range name ("Garo Hills", "Khasi region") the extractor tagged as a
        # district isn't one — leave it unresolved so _answer_data's region handler can
        # offer the range's districts as chips (or expand it). Don't raise the generic
        # "which district?" here.
        _mdist = mentions.get("district")
        if _mdist and detect_region(_mdist, schemes[0] if schemes else "") is not None:
            _mdist = None
        if _mdist:
            r = resolve_dimension(_mdist, schemes[0], "district")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which district is being referred to by “{_mdist}”?",
                                           rule="entity-ambiguous")
            if r.status == "resolved":
                district_canon = r.canonical
                resolved["district"] = r.canonical
                display["district"] = r.display or str(r.canonical).title()
            else:
                # Not a known district — it may be a VILLAGE the extractor
                # mistagged as "district" (a bare place name after "in" gives the
                # model no reliable signal for which admin level it is). Try
                # village resolution before concluding this is out of scope.
                _vr = await resolve_village(_mdist)
                if _vr.status == "ambiguous":
                    listed = ", ".join(
                        f"{c['name']} in {c['block']} block ({c['district']})" for c in _vr.candidates[:5])
                    options = [
                        {"label": f"{c['name']} — {c['block']} block, {c['district']}",
                         "question": _village_chip_question(question, _mdist, c)}
                        for c in _vr.candidates[:5]
                    ]
                    raise ClarificationNeeded(
                        f"“{_mdist}” corresponds to more than one village: {listed}. "
                        "Which of these is intended?",
                        options=options, rule="entity-ambiguous", village_hint=_mdist)
                if _vr.status == "resolved":
                    resolved["village_code"] = _vr.canonical
                    display["village"] = _vr.display or _place_title(_mdist)
                elif settings.OUT_OF_SCOPE_GUARD_ENABLED:
                    # Genuinely not a district, block, or village of ours — almost
                    # always a place in another state ("districts in Guwahati").
                    # Don't silently drop the filter and count 0; say plainly
                    # this is out of scope.
                    raise OutOfScope(f"district '{_mdist}' is not in Meghalaya")
                else:
                    notes.append(f"'{_mdist}' is not a known district — say so, do not filter on it.")

    if mentions.get("blocks"):
        # Two-or-more named blocks to compare against each other (see
        # extract_entity_mentions) — resolve each independently and carry the
        # whole set forward as an IN-list, instead of the singular "block"
        # dimension which can only ever hold one value and would silently
        # drop every block but the last.
        _has_block_word = bool(re.search(r"\bblock\b", question, re.IGNORECASE))
        _block_canons: list[str] = []
        _block_displays: list[str] = []
        # A bare comparison name with no admin-level word ("X or Y", "compare X
        # and Y") gets tagged into this "blocks" array by default (see the
        # extractor prompt's own "tag an ambiguous bare name as block" rule),
        # even when both names are actually VILLAGES — e.g. "which has more
        # completed houses: <village 1> or <village 2>". When a name here
        # isn't a resolvable block at all, it used to just get a "not a known
        # block" note and get dropped; with every name dropped, geography
        # stayed fully unresolved and the generic scope pause fired, offering
        # every district statewide instead of just the two villages named
        # (reported 2026-09-10, PMAY-OFF-022). Try village resolution as a
        # fallback for exactly the names that fail as a block, mirroring the
        # `_vcheck` block-vs-village disambiguation a few lines below.
        _village_canons: list[int] = []
        _village_displays: list[str] = []
        for _bname in mentions["blocks"]:
            r = resolve_dimension(_bname, schemes[0], "block")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which block is being referred to by “{_bname}”?",
                                           rule="entity-ambiguous")
            if r.status != "resolved":
                _vr = await resolve_village(_bname, district=district_canon)
                if _vr.status == "ambiguous":
                    listed = ", ".join(
                        f"{c['name']} in {c['block']} block ({c['district']})" for c in _vr.candidates[:5])
                    options = [
                        {"label": f"{c['name']} — {c['block']} block, {c['district']}",
                         "question": _village_chip_question(question, _bname, c)}
                        for c in _vr.candidates[:5]
                    ]
                    raise ClarificationNeeded(
                        f"“{_bname}” corresponds to more than one village: {listed}. "
                        "Which of these is intended?",
                        options=options, rule="entity-ambiguous", village_hint=_bname)
                if _vr.status == "resolved":
                    _village_canons.append(_vr.canonical)
                    _village_displays.append(_vr.display or str(_bname).title())
                else:
                    notes.append(f"'{_bname}' is not a known block or village — say so, "
                                 "do not filter on it.")
                continue
            if not _has_block_word:
                _vcheck = await resolve_village(_bname)
                if _vcheck.status in ("resolved", "ambiguous"):
                    stem = question.strip().rstrip(" ?.")
                    raise ClarificationNeeded(
                        f"“{_bname}” could mean either the block or a village with "
                        "that name. Which did you mean?",
                        options=[
                            {"label": f"The {_bname} block", "question": f"{stem}, the block, not the village"},
                            {"label": f"The {_bname} village", "question": f"{stem}, the village, not the block"},
                        ],
                        rule="entity-ambiguous")
            _block_canons.append(r.canonical)
            _block_displays.append(r.display or str(_bname).title())
        if _block_canons:
            resolved["block_list"] = _block_canons
            display["block"] = " and ".join(_block_displays)
            # A district named in the same breath as several blocks normally
            # qualifies just ONE of them — it is there to separate a
            # same-named block from its twin elsewhere ("BATABARI block, WEST
            # GARO HILLS": Batabari exists in both South and West Garo Hills).
            # ANDing that district against the WHOLE block list then silently
            # deletes every block that legitimately sits somewhere else:
            # "applicants in Shallang ... BATABARI block, WEST GARO HILLS"
            # returned nothing for Shallang, because Shallang is a WEST KHASI
            # HILLS block and `lgd_district = 'WEST GARO HILLS' AND lgd_block
            # IN ('SHALLANG','BATABARI')` can never match it (reported
            # 2026-09-18). Each block already knows its own district, so drop
            # the standalone district filter whenever the named blocks do not
            # all belong to it — the block names are the more specific filter
            # and they carry their own geography.
            _parents = {b: block_parent_district(b, schemes[0] if schemes else "")
                        for b in _block_canons}
            if district_canon:
                _outside = [b for b, d in _parents.items()
                            if d and d.upper() != str(district_canon).upper()]
                if _outside:
                    logger.info(
                        "district %r dropped: blocks %s sit outside it (block list spans "
                        "districts)", district_canon, _outside)
                    resolved.pop("district", None)
                    display.pop("district", None)
                    district_canon = None
            # Tell the generator which district each block belongs to, so a
            # name that exists in two districts is still pinned to the right
            # one rather than double-counted across both.
            _known = {b: d for b, d in _parents.items() if d}
            if _known:
                resolved["block_list_districts"] = _known
        if _village_canons:
            resolved["village_code_list"] = _village_canons
            display["village"] = " and ".join(_village_displays)
    elif mentions.get("block") or (
            _explicit_level_in(question) == "block"
            and not mentions.get("village") and not mentions.get("blocks")
            and (_block_scan := scan_dimension(question, schemes[0], "block",
                                               level_is_explicit=True)) is not None
            and _block_scan.status == "resolved"):
        # The extractor drops a plainly-named block just as it drops districts
        # — "in shallang block under Piggery Scheme, PRIME ... (SEED) and
        # Meghalaya Poultry Farming Scheme" returned {} on 5 of 5 calls
        # (2026-09-18), the lowercase name buried between two long scheme
        # names. With no resolved block the generator invented
        # `lgd_block = 'SHALLANG'` off the raw text; the verifier demanded a
        # `lgd_district = 'MEGHALAYA'` filter instead, the state-pseudo-row
        # guard rejected that, the repair put it back, and the loop burned its
        # budget and fell through to the KB fallback. The answer is a plain 45.
        #
        # Only runs when the question names the level outright, so a bare
        # "Shallang" still reaches the village/AC clarification flow.
        if not mentions.get("block"):
            mentions = {**mentions, "block": _block_scan.canonical}
            logger.info("resolve_entities: block backstop matched %r in question text",
                        _block_scan.canonical)
        r = resolve_dimension(mentions["block"], schemes[0], "block")
        if r.status == "ambiguous":
            raise ClarificationNeeded(f"Which block is being referred to by “{mentions['block']}”?",
                                       rule="entity-ambiguous")
        if r.status == "resolved":
            # Block and village names collide constantly in Meghalaya (26 of 56
            # block names are also village names, per the resolver YAML) — a bare
            # name the user didn't explicitly call a "block" is genuinely
            # ambiguous. Silently answering at the block level here is exactly
            # the bug QA reported (bot picks village data for a block question,
            # or vice versa, without ever asking).
            # Same truncation guard as the admin-level gate above: when the raw
            # text carries a LONGER name than this mention and that longer name
            # resolves to exactly one village, the mention is a fragment of it
            # ("william nagar(mb)" from "william nagar(mb) - ward no.4"), not a
            # genuine block-vs-village ambiguity. Resolve the village instead of
            # asking a question the text already answers.
            _longer = await _scan_village_in_question(_scan_q, schemes[0] if schemes else "")
            if _longer and len(_longer[1]) == 1 \
                    and str(mentions["block"]).strip().lower() in _longer[0].strip().lower() \
                    and len(_longer[0]) > len(str(mentions["block"])):
                _pick = _longer[1][0]
                resolved["village_code"] = _pick["village_code"]
                display["village"] = _place_title(_pick["name"])
                logger.info("block mention %r is a truncation of village %r — resolved it",
                            mentions["block"], _longer[0])
            elif not re.search(r"\bblock\b", question, re.IGNORECASE):
                _vcheck = await resolve_village(mentions["block"])
                if _vcheck.status in ("resolved", "ambiguous"):
                    stem = question.strip().rstrip(" ?.")
                    raise ClarificationNeeded(
                        f"“{mentions['block']}” could mean either the block or a village with "
                        "that name. Which did you mean?",
                        options=[
                            {"label": f"The {mentions['block']} block", "question": f"{stem}, the block, not the village"},
                            {"label": f"The {mentions['block']} village", "question": f"{stem}, the village, not the block"},
                        ],
                        rule="entity-ambiguous")
            # Not when the mention turned out to be a truncated VILLAGE name
            # (handled just above): village_code already pins the exact place,
            # and adding the block it was a fragment of would filter a
            # different, larger area alongside it.
            if "village_code" not in resolved:
                resolved["block"] = r.canonical
                display["block"] = r.display or str(r.canonical).title()
                # Same conflict as the block_list branch above, for one block:
                # a district named alongside a block that provably sits in a
                # DIFFERENT district makes `lgd_district = X AND lgd_block = Y`
                # match zero rows. The block is the more specific filter and
                # knows its own district, so keep the block and drop the
                # contradicting district rather than answering "no records".
                _bp = r.parent_district or block_parent_district(
                    r.canonical, schemes[0] if schemes else "")
                if _bp and district_canon and _bp.upper() != str(district_canon).upper():
                    logger.info("district %r dropped: block %r sits in %r",
                                district_canon, r.canonical, _bp)
                    resolved.pop("district", None)
                    display.pop("district", None)
                    district_canon = None
        else:
            # The mention-extractor sometimes tags a full DISTRICT name as the
            # "block" when the question itself says "by block" right next to it
            # ("compare ... by block in West Garo Hills") — it reads the phrase
            # "by block in X" as naming X as the block. Before treating this as
            # not-a-known-block (or, worse, OutOfScope — a real district getting
            # rejected as "not in Meghalaya" is a confusing wrong answer), check
            # whether the same text is actually a district and use it as one.
            _as_district = resolve_dimension(mentions["block"], schemes[0], "district")
            if _as_district.status == "resolved":
                # Either newly discovered here, or (commonly) the SAME text was
                # already captured as the district via the raw-text backstop
                # scan above (mentions.get("district") was empty, so that scan
                # ran on the full question text and found "West Garo Hills"
                # there too) — in that case district_canon is already set and
                # this "block" mention is just a duplicate tag for it. Either
                # way, there's nothing wrong here: don't overwrite an existing
                # district_canon, and don't fall through to "not a known block".
                # Also skip when a district_list comparison already resolved —
                # this "block" mention is then just the extractor's other tag
                # for one of those same districts.
                if not district_canon and not resolved.get("district_list"):
                    district_canon = _as_district.canonical
                    resolved["district"] = _as_district.canonical
                    display["district"] = _as_district.display or str(_as_district.canonical).title()
            else:
                # Also not a district — it may be a VILLAGE the extractor
                # mistagged as "block" (villages far outnumber blocks, and a
                # bare name after "in" gives the model no reliable signal for
                # which admin level it is — e.g. "PMAY beneficiaries in
                # Abagre", a real West Garo Hills village). Try village
                # resolution before concluding this is out of scope. Scope by
                # district_canon when the question already pinned one down —
                # otherwise a village-disambiguation chip's own answer text
                # ("Nongthymmai, MAWRYNGKNENG block, EAST KHASI HILLS") gets
                # re-resolved with no district scope, rediscovers the exact
                # same statewide candidate set the chip was meant to narrow,
                # and raises the identical clarification forever (reported
                # 2026-09-09, "nongthymmai" in EAST KHASI HILLS).
                #
                # NOT when the user explicitly said "the block" (an admin-level
                # choice chip, or their own wording): the whole point of that
                # answer is that they do not want the village reading, so
                # quietly resolving one anyway answers at a grain they just
                # declined. Say the block doesn't exist instead.
                if _stated_level == "block":
                    notes.append(
                        f"'{mentions['block']}' is not a C&RD block in this data — say so "
                        "plainly. Do NOT answer using a village or district of the same "
                        "name; the question asked specifically for the block.")
                    _vr = None
                else:
                    _vr = await resolve_village(mentions["block"], district=district_canon)
                if _vr is not None and _vr.status == "ambiguous":
                    # District scoping alone doesn't always get to one candidate
                    # (e.g. several distinctly-blocked villages share a name inside
                    # the same district). The chip that got us here already names
                    # the intended block in its own text ("Nongthymmai, MAWRYNGKNENG
                    # block, EAST KHASI HILLS") — the LLM extractor just keeps
                    # re-tagging the village as "block" and dropping the real block,
                    # so re-resolving lands back on the SAME candidate set every
                    # round and the clarification loops forever (reported
                    # 2026-09-09, "nongthymmai" in EAST KHASI HILLS). Before asking
                    # again, check the raw text deterministically: if exactly one
                    # candidate's own block name appears in it, that already IS the
                    # user's answer.
                    _text_hits = [
                        c for c in _vr.candidates
                        if c.get("block") and re.search(
                            rf"\b{re.escape(c['block'])}\b", question, re.IGNORECASE)
                    ]
                    if len(_text_hits) == 1:
                        _pick = _text_hits[0]
                        resolved["village_code"] = _pick["village_code"]
                        display["village"] = _place_title(_pick["name"])
                        _vr = None
                    else:
                        listed = ", ".join(
                            f"{c['name']} in {c['block']} block ({c['district']})" for c in _vr.candidates[:5])
                        options = [
                            {"label": f"{c['name']} — {c['block']} block, {c['district']}",
                             "question": _village_chip_question(question, mentions["block"], c)}
                            for c in _vr.candidates[:5]
                        ]
                        raise ClarificationNeeded(
                            f"“{mentions['block']}” corresponds to more than one village: {listed}. "
                            "Which of these is intended?",
                            options=options, rule="entity-ambiguous", village_hint=mentions["block"])
                if _vr is None:
                    pass  # already resolved directly from the single text hit above
                elif _vr.status == "resolved":
                    resolved["village_code"] = _vr.canonical
                    display["village"] = _vr.display or _place_title(mentions["block"])
                elif settings.OUT_OF_SCOPE_GUARD_ENABLED:
                    raise OutOfScope(f"block '{mentions['block']}' is not in Meghalaya")
                else:
                    notes.append(f"'{mentions['block']}' is not a known block — say so, do not filter on it.")

    if mentions.get("assembly_constituency"):
        r = resolve_dimension(mentions["assembly_constituency"], schemes[0], "assembly_constituency")
        if r.status == "ambiguous":
            raise ClarificationNeeded(
                f"Which assembly constituency is being referred to by "
                f"“{mentions['assembly_constituency']}”?", rule="entity-ambiguous")
        if r.status == "resolved":
            resolved["assembly_constituency"] = r.canonical
            display["assembly_constituency"] = r.display or str(r.canonical).title()
            # Step 2 of the hierarchy: having settled that this IS the
            # constituency, offer to narrow inside it. Only when the question
            # hasn't already narrowed itself — a resumed drill-down chip says
            # "within the X block only" / "the whole constituency", and a
            # question that independently names a district/block/village has
            # answered this too. Best-effort: if the contents can't be read,
            # the flow continues with the whole constituency, as before.
            _already_narrowed = (
                _AC_SCOPED_RE.search(question)
                or re.search(r"\bwhole\s+constituency\b", question, re.IGNORECASE)
                or any(resolved.get(k) for k in
                       ("district", "district_list", "block", "block_list",
                        "village_code", "village_code_list"))
            )
            _narrow = _AC_NARROW_RE.search(question)
            if _narrow:
                # A resumed drill-down chip — apply its district/block filter
                # alongside the constituency, so the answer covers exactly the
                # overlap the user asked for.
                _lvl = _narrow.group("level").lower()
                _nm = _narrow.group("name").strip()
                _nr = resolve_dimension(_nm, schemes[0], _lvl)
                if _nr.status == "resolved":
                    _canon, _disp = _nr.canonical, _nr.display or str(_nr.canonical).title()
                else:
                    # These chips are built from values read live out of
                    # curated.v_employment (constituency_contents), and the
                    # YAML block catalogue does not necessarily list every one
                    # of them. The value came from the database itself, so it
                    # is a valid literal even when the catalogue has no entry
                    # — use it rather than dropping the narrowing the user
                    # explicitly picked. Storage is upper-case for both
                    # lgd_district and lgd_block (docs/DATA_MODEL.md).
                    _canon, _disp = _nm.upper(), _nm.title()
                resolved[_lvl] = _canon
                display[_lvl] = _disp
                if _lvl == "district":
                    district_canon = _canon
                logger.info("AC drill-down: also filtering %s = %r", _lvl, _canon)
            elif not _already_narrowed:
                _contents = await constituency_contents(str(r.canonical))
                _drill = _ac_drilldown_clarification(
                    question, display["assembly_constituency"], _contents)
                if _drill is not None:
                    raise _drill
        else:
            # assembly_constituency data exists ONLY in mgnrega_employment (no
            # catalogue loaded for other schemes, or the name genuinely isn't
            # one) — say so instead of silently falling back to a block/
            # district filter, which would answer a different, unintended level.
            notes.append(
                f"'{mentions['assembly_constituency']}' is not a known assembly constituency "
                f"for {schemes[0] if schemes else 'this scheme'} — say so, do not silently "
                "filter on a block or district instead.")

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

    # Prefer the current question's own village mention; fall back to the
    # remembered hint from a just-resumed village-ambiguity pause only when
    # this turn's extraction found no village at all (see the docstring above).
    #
    # A question that states a NON-village level outright ("..., the assembly
    # constituency, not another area type" — a resumed admin-level collision
    # chip) must not resolve a village at all: the pending village hint is
    # stale in that case (it was remembered when the level was still open), and
    # the block/district branches above have already placed the name at the
    # level the user chose. Letting it through set village_code alongside the
    # chosen level, which then filters the query down to one village even
    # though the user explicitly asked for the constituency.
    _village_text = mentions.get("village") or village_hint
    if _stated_level is not None and _stated_level != "village":
        _village_text = None
    # Deterministic backstop for a village the extractor dropped (see
    # _scan_village_in_question). Only when NOTHING else already placed this
    # turn's geography at village grain, and only when the question names no
    # block/AC of its own for the same span — those dimensions own their names.
    # Skipped when the question states a level of its own: either the user
    # chose "village" (in which case the extractor's own mention already went
    # down the normal village path above) or they chose a NON-village level,
    # and filling a village in behind that choice would answer at a grain they
    # explicitly declined.
    if not _village_text and not resolved.get("village_code") \
            and not resolved.get("village_code_list") and _stated_level is None:
        _scanned = await _scan_village_in_question(_scan_q, schemes[0] if schemes else "")
        if _scanned:
            _vname, _vhits = _scanned
            logger.info("village backstop: %r matched %d village(s) in the question text "
                        "the extractor dropped", _vname, len(_vhits))
            # Narrow by a district the question already pinned before deciding
            # whether this is still ambiguous — "ASIMGRE ... for East Garo
            # Hills" has exactly one ASIMGRE in that district.
            # Narrow by a block the question already pinned first — it is the
            # finer of the two scopes, and a resumed village chip names both
            # ("..., SELSELLA block, WEST GARO HILLS"). Without this the
            # district scope alone can still leave several same-named villages
            # and the chip resolves only to block grain, losing the village.
            _blk = resolved.get("block")
            if _blk:
                _by_block = [c for c in _vhits
                             if str(c.get("block") or "").upper() == str(_blk).upper()]
                if _by_block:
                    _vhits = _by_block
            if district_canon:
                _scoped = [c for c in _vhits
                           if str(c["district"]).upper() == str(district_canon).upper()]
                if _scoped:
                    _vhits = _scoped
                elif _blk and len(_vhits) == 1:
                    # The block filter already pinned exactly one village; the
                    # district mismatch is just the stale one from the original
                    # question, so let the block's own district stand.
                    district_canon = _vhits[0]["district"]
                    resolved["district"] = _vhits[0]["district"]
                    display["district"] = str(_vhits[0]["district"]).title()
                else:
                    # The question named a district AND a village, but no
                    # village of that name exists there ("ASIMGRE ... for East
                    # Khasi Hills" — every ASIMGRE is in the Garo Hills). The
                    # two filters contradict each other, so neither answering
                    # district-wide (the old silent-drop bug, which reported a
                    # whole district's figure as though it were the village's)
                    # nor offering villages in OTHER districts is right. Say
                    # so plainly instead.
                    _dname = display.get("district") or str(district_canon).title()
                    _elsewhere = ", ".join(sorted({str(c["district"]).title() for c in _vhits}))
                    # Each village chip must drop the district the user named,
                    # or the resumed question carries TWO conflicting districts
                    # ("... for East Khasi Hills ..., SELSELLA block, WEST GARO
                    # HILLS") and resolves to neither the village nor the right
                    # district. Strip the original district phrase first, then
                    # let _village_chip_question pin the village.
                    _no_dist = re.sub(
                        rf"\s*\b(?:in|for|of|at|from)\s+{re.escape(str(_dname))}\b",
                        "", question, count=1, flags=re.IGNORECASE).strip()
                    raise ClarificationNeeded(
                        f"There is no village called “{_vname}” in {_dname}. "
                        f"Villages with that name are in {_elsewhere}. Did you mean one of "
                        f"those, or the whole of {_dname}?",
                        options=[
                            {"label": f"{c['name']} — {c['block']} block, {c['district']}",
                             "question": _village_chip_question(_no_dist, _vname, c)}
                            for c in _vhits[:5]
                        ] + [{
                            "label": f"All of {_dname}",
                            "question": _drop_place_phrase(question, _vname),
                        }],
                        rule="entity-ambiguous")
            if len(_vhits) == 1:
                resolved["village_code"] = _vhits[0]["village_code"]
                display["village"] = _place_title(_vhits[0]["name"])
            else:
                # Several real villages share this name — ask, never guess.
                listed = ", ".join(
                    f"{c['name']} in {c['block']} block ({c['district']})" for c in _vhits[:5])
                raise ClarificationNeeded(
                    f"“{_vname}” corresponds to more than one village: {listed}. "
                    "Which of these is intended?",
                    options=[
                        {"label": f"{c['name']} — {c['block']} block, {c['district']}",
                         "question": _village_chip_question(question, _vname, c)}
                        for c in _vhits[:5]
                    ],
                    rule="entity-ambiguous", village_hint=_vname)
    if _village_text:
        r = await resolve_village(_village_text, district=district_canon,
                                   block=resolved.get("block"))
        if r.status == "ambiguous":
            # Two candidates can share the same (name, district) while being
            # genuinely different villages in different blocks with their own
            # data (e.g. two "Adugre"s in SOUTH WEST GARO HILLS — one in
            # Betasing block, one in Rerapara) — block is the distinguishing
            # feature, so it MUST be in both the message and the chip label, or
            # they read as duplicates and the user can't tell them apart.
            listed = ", ".join(
                f"{c['name']} in {c['block']} block ({c['district']})" for c in r.candidates[:5])
            options = [
                {"label": f"{c['name']} — {c['block']} block, {c['district']}",
                 "question": _village_chip_question(question, _village_text, c)}
                for c in r.candidates[:5]
            ]
            raise ClarificationNeeded(
                f"“{_village_text}” corresponds to more than one village: {listed}. "
                "Which of these is intended?",
                options=options,
                rule="entity-ambiguous",
                village_hint=_village_text)
        if r.status == "resolved":
            # Symmetric check to the block branch above: this name might also be
            # a block name. Only worth asking when the district/block scope
            # didn't already pin it down (a village resolved WITH a district
            # scope is unambiguous) and the user didn't already say "village".
            if not district_canon and not re.search(r"\bvillage\b", question, re.IGNORECASE):
                _bcheck = resolve_dimension(_village_text, schemes[0], "block")
                if _bcheck.status == "resolved":
                    stem = question.strip().rstrip(" ?.")
                    raise ClarificationNeeded(
                        f"“{_village_text}” could mean either the village or a block "
                        "with that name. Which did you mean?",
                        options=[
                            {"label": f"The {_village_text} village", "question": f"{stem}, the village, not the block"},
                            {"label": f"The {_village_text} block", "question": f"{stem}, the block, not the village"},
                        ],
                        rule="entity-ambiguous")
            resolved["village_code"] = r.canonical
            display["village"] = r.display or _place_title(_village_text)
        else:
            notes.append(f"'{_village_text}' is not a known village — say so, do not filter on it.")

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

    # Focus Plus tranche_label ("Tranch 1".."Tranch 4 - Feb-March") — closed set,
    # not covered by the LLM mention-extractor, and routinely mistyped against
    # its odd stored spelling ("tranche2" vs "Tranch 2 - August"). Resolved
    # deterministically (with a RapidFuzz fallback for misspellings) so a
    # near-miss maps to the exact stored label instead of silently filtering
    # the SQL to zero rows and reporting a misleading null total.
    if "Focus Plus" in schemes:
        tr = resolve_tranche_label(question, "Focus Plus")
        if tr and tr.values:
            resolved["tranche_label"] = tr.values if len(tr.values) > 1 else tr.values[0]
            display["tranche_label"] = tr.display or " and ".join(tr.values)

    # CM Elevate sub-scheme ("Piggery", "Dairy Development", ... — a 15-value
    # closed set) — resolved deterministically against the SME alias catalogue.
    # Without this, a typo'd or loosely-phrased sub-scheme name reaches the SQL
    # generator as raw text, which then guesses a scheme_name literal that
    # doesn't exactly match storage and silently counts zero rows (see
    # resolve_cm_scheme's docstring).
    if "CM Elevate" in schemes:
        cs = resolve_cm_scheme(question, "CM Elevate")
        if cs and cs.values:
            resolved["cm_scheme"] = cs.values if len(cs.values) > 1 else cs.values[0]
            display["cm_scheme"] = cs.display or " and ".join(cs.values)
        else:
            # resolve_cm_scheme found no single real sub-scheme — check whether
            # the question instead named a scheme-FAMILY word in the singular
            # ("the vehicle scheme") that covers 2+ real, non-interchangeable
            # sub-schemes. Left unhandled, this reached the SQL generator as
            # bare text, which guessed a scheme_name literal that doesn't exist
            # ('Meghalaya Vehicle Scheme') and silently returned 0 rows dressed
            # up as a refusal (confirmed live 2026-09-11) — exactly the "never
            # guess, ask" case cmelevate_entity_resolver.yaml's overloaded_terms
            # section documents but was never wired to any code path.
            grp = resolve_cm_scheme_group_ambiguity(question, "CM Elevate")
            if grp:
                raise _cm_scheme_group_clarification(question, grp)
            # Not singular-ambiguous — check the PLURAL/collective reading
            # ("vehicle schemes", "livestock", "PRIME family"): a real,
            # already-answerable group-breakdown question (cmelevate_few_shot.yaml
            # has worked IN-list examples), not something to ask about. Populate
            # cm_scheme as a multi-value resolve, same shape resolve_cm_scheme
            # itself uses for "compare Piggery and Poultry" — without this, the
            # SQL generator's hardcoded IN-list for the group got rejected by the
            # semantic verifier for having no resolved entity to justify it
            # (confirmed live 2026-09-11, once the routing fix above let this
            # question reach SQL generation for the first time).
            grp2 = resolve_cm_scheme_group(question, "CM Elevate")
            if grp2:
                resolved["cm_scheme"] = grp2["schemes"]
                display["cm_scheme"] = f"the {grp2['group']} schemes ({', '.join(grp2['schemes'])})"

    # CM Elevate Legacy's 13 schemes — its OWN catalogue (the stored spellings
    # differ from the applications dataset's: 'Prime Tourism Vehicle Scheme' vs
    # 'PRIME Tourism Vehicle Scheme'), resolved into the same cm_scheme slot.
    # Skipped when the applications dataset is also in play, so the two
    # catalogues never overwrite each other's literal.
    if "CM Elevate Legacy" in schemes and "CM Elevate" not in schemes:
        _seri = _cm_legacy_sericulture_choice(question)
        if _seri == "ask":
            raise _cm_legacy_sericulture_clarification(question)
        cs = resolve_cm_scheme(question, "CM Elevate Legacy")
        if _seri:
            resolved["cm_scheme"] = _seri if len(_seri) > 1 else _seri[0]
            display["cm_scheme"] = " and ".join(_seri)
        elif cs and cs.values:
            resolved["cm_scheme"] = cs.values if len(cs.values) > 1 else cs.values[0]
            display["cm_scheme"] = cs.display or " and ".join(cs.values)

    # An assembly constituency cuts across districts (8 of 56 straddle two), so a
    # district the user never named must not narrow it. Seen 2026-09-25 (CM
    # Elevate Legacy TC-31, "applications mapped to Mairang constituency"):
    # resolution returned district EASTERN WEST KHASI HILLS beside the AC — Mairang
    # is also a block there — and the SQL ANDed it in. Harmless for Mairang, which
    # sits wholly in that district; a silent undercount for a straddling one. A
    # district the user did name (or picked via the ", within the X district only"
    # chip) is in the question text, so the resolver's own scan finds it and it stays.
    if resolved.get("assembly_constituency") and resolved.get("district"):
        _named = scan_dimension(_scan_q, schemes[0] if schemes else "", "district")
        if not (_named and _named.status == "resolved"
                and str(_named.canonical).upper() == str(resolved["district"]).upper()):
            logger.info("AC %r: dropped district %r — not named in the question",
                        resolved["assembly_constituency"], resolved["district"])
            resolved.pop("district", None)
            display.pop("district", None)

    if prior_resolved:
        if not mentions.get("district") and "district" not in resolved and prior_resolved.get("district") \
                and not resolved.get("assembly_constituency"):
            resolved["district"] = prior_resolved["district"]
        if not mentions.get("block") and "block" not in resolved and prior_resolved.get("block"):
            resolved["block"] = prior_resolved["block"]
        if not mentions.get("year") and "year_key" not in resolved and prior_resolved.get("year_key"):
            resolved["year_key"] = prior_resolved["year_key"]
        if not mentions.get("village") and "village_code" not in resolved and prior_resolved.get("village_code"):
            resolved["village_code"] = prior_resolved["village_code"]
        # Focus Plus tranche pin — same fallback as district/block/village/year
        # above. There is no LLM `mentions` signal for tranche_label (it's
        # resolved deterministically, not via mention extraction — see
        # resolve_tranche_label), so the only guard needed is that THIS
        # question's own resolution found nothing: a bare short follow-up
        # ("top 3 only") that never re-says "tranche" would otherwise lose the
        # tranche the previous turn pinned. Only ever carries a real stored
        # label — never a sentinel — so it stays safe to drop straight into
        # the SQL prompt's tranche_label filter (see prompt_builder._entities_block).
        if (schemes == ["Focus Plus"] and "tranche_label" not in resolved
                and prior_resolved.get("tranche_label")):
            resolved["tranche_label"] = prior_resolved["tranche_label"]
        # CM Elevate sub-scheme pin — same fallback as tranche_label above:
        # there is no LLM `mentions` signal for cm_scheme (resolved
        # deterministically, not via mention extraction), so a bare follow-up
        # that never re-names the sub-scheme would otherwise lose it.
        if (schemes == ["CM Elevate"] and "cm_scheme" not in resolved
                and prior_resolved.get("cm_scheme")):
            resolved["cm_scheme"] = prior_resolved["cm_scheme"]
        # Same carry for CM Elevate Legacy — but only a value from ITS catalogue,
        # so a sub-scheme pinned while talking about the applications dataset
        # cannot leak a literal this view does not store.
        if (schemes == ["CM Elevate Legacy"] and "cm_scheme" not in resolved
                and prior_resolved.get("cm_scheme")):
            _prior = prior_resolved["cm_scheme"]
            _vals = _prior if isinstance(_prior, list) else [_prior]
            _known = set(canonical_names("CM Elevate Legacy", "cm_scheme"))
            if _vals and all(v in _known for v in _vals):
                resolved["cm_scheme"] = _prior

    out = {"resolved": resolved, "notes": notes, "display": display}
    if _year_gap_note:
        # _apply_year_gap rewrote the question (the absent year stripped, or
        # swapped for the nearest year with data on a comparison). SQL must be
        # generated from THAT text: from the original, the generator filtered
        # on the absent year and the note above then contradicted the result
        # (TC-24, 2026-09-25: "2024-25: 114,990,000" with no 2023-24 mention).
        out["question"] = question
    return out


_FOCUSPLUS_BENEFICIARY_QUALIFIER = re.compile(
    r"\b(wom[ae]n|female|male|gender|farmer|occupation|status|pending|approved|"
    r"rejected|verif\w*|tranch\w*|instal{1,2}ments?|batch\w*|legacy|93k|12\.5k|"
    r"average|mean|\bper\b|percentage|per ?cent|compare\w*|\bvs\.?\b|versus|each|"
    r"every|wise|breakdown|split|distribution|top\s*\d|highest|lowest|rank\w*)\b",
    re.IGNORECASE,
)


def _focusplus_single_district_beneficiary_guard(
        question: str, schemes: list[str], entity_result: dict, sql: str) -> str:
    """User-directed exception (2026-09-13): "how many Focus Plus beneficiaries
    in <one district>" must answer with ONE number, not the three-reading
    ambiguity table (FOCUS PLUS RULES rule 6). The prose exception added to
    schema_context.py doesn't reliably win against the concrete multi-column
    worked example under LLM sampling — confirmed live, the generator kept
    emitting the 3/4-column shape even with the exception text in-prompt and
    the correctly-ranked single-column few-shot example alongside it.
    Deterministic rewrite instead, scoped narrowly so every OTHER beneficiary
    shape (statewide, "each district", a comparison, a gender/status/tranche/
    batch-scoped question, or one with a specific year or tranche pinned) is
    left completely alone — those still need their own, different SQL.

    Updated 2026-09-13 (client UAT sheet, FOCUS-001): the original version of
    this template used `COUNT(DISTINCT source_sl_no) WHERE batch_label = '93K'
    AND NOT has_geo_conflict`, written before curated.fact_focus_plus_disbursement
    grew the generated beneficiary_key column (batch_label || ':' || source_sl_no,
    see semantic.column_catalog). That template was reproduced live against
    megh_db and silently dropped every real beneficiary it wasn't built to see:
    the entire 12.5K cohort (4,670 people in West Garo Hills alone — batch_label
    = '93K' excludes them outright) and every has_geo_conflict row (136 more in
    West Garo Hills) even though has_geo_conflict only means the source's block
    name disagreed with the LGD roster for that village — the roster's district
    is what v_focus_plus.lgd_district stores either way, so those rows belong in
    the district count as much as any other row. beneficiary_key already spans
    both cohorts as one identifier space, so neither filter is needed any more."""
    if schemes != ["Focus Plus"]:
        return sql
    if not re.search(r"beneficiar\w*", question, re.IGNORECASE):
        return sql
    resolved = entity_result.get("resolved", {})
    district = resolved.get("district")
    if not district or resolved.get("district_list"):
        return sql
    if resolved.get("block") or resolved.get("village_code"):
        return sql
    if resolved.get("year_key") or resolved.get("tranche_label"):
        return sql
    if _FOCUSPLUS_BENEFICIARY_QUALIFIER.search(question):
        return sql
    return (
        "SELECT COUNT(DISTINCT beneficiary_key) AS beneficiaries\n"
        "FROM curated.v_focus_plus\n"
        f"WHERE lgd_district = '{district}';"
    )


async def generate_sql(question: str, schemes: list[str], entity_result: dict) -> str:
    # The whole prompt — hand-written backbone + live schema + SME catalog +
    # prohibited joins + few-shot + resolved entities — is assembled in one place.
    prompt = prompt_builder.build_sql_prompt(question, schemes, entity_result)
    raw = await llm.call_sql_generator(prompt, guided={"guided_regex": _SQL_SHAPE_REGEX})
    sql = _extract_sql(raw)
    return _focusplus_single_district_beneficiary_guard(question, schemes, entity_result, sql)


# The generator sometimes reads "in Meghalaya" as a place filter and invents a
# WHERE on a state pseudo-row that no fact row matches — the query then runs
# clean but counts 0. Catch that shape and force one repair pass (the repair
# prompt carries the "whole dataset is Meghalaya" rule, so it drops the filter).
# An assembly constituency cuts ACROSS blocks and districts, so its name is
# usually not a block/district name at all. When only the constituency was
# resolved, the generator still reaches for `lgd_block = '<same name>'` next to
# the AC filter — two conditions that can never both hold, giving a clean,
# confident ZERO (confirmed live 2026-09-17: "which villages in Rangsakona
# received employment in FY 2025-26" answered "no matching records"; RANGSAKONA
# spans the BETASING, RERAPARA and RONGRAM blocks and the real answer is 146
# villages). The prompt now warns against it (prompt_builder._entities_block),
# but a prose rule doesn't reliably win under sampling — this catches the shape
# deterministically, the same way the village_code guards above do.
_AC_FILTER_RE = re.compile(r"\bassembly_constituency_name\b", re.IGNORECASE)
# Matches the filter in every shape the generator writes it: a bare column, a
# table-qualified one (dg.lgd_block), and either side wrapped in UPPER().
_GEO_EQ_LITERAL_RE = re.compile(
    r"(?:\w+\.)?\blgd_(?P<col>district|block)\b\s*\)?\s*(?:=|ILIKE)\s*"
    r"(?:UPPER\s*\(\s*)?'(?P<val>[^']+)'",
    re.IGNORECASE,
)


def _ac_with_invented_geo_filter(entity_result: dict, sql: str) -> "str | None":
    """The geography literal a query added alongside an assembly-constituency
    filter that entity resolution never produced, or None.

    Only fires when the resolved entities carry an assembly_constituency and NO
    district/block of their own — i.e. the filter cannot have come from
    resolution, so the generator invented it from the question text."""
    resolved = entity_result.get("resolved") or {}
    if not resolved.get("assembly_constituency"):
        return None
    if resolved.get("district") or resolved.get("block") \
            or resolved.get("district_list") or resolved.get("block_list"):
        return None
    if not _AC_FILTER_RE.search(sql or ""):
        return None
    m = _GEO_EQ_LITERAL_RE.search(sql or "")
    return f"lgd_{m.group('col')} = '{m.group('val')}'" if m else None


# MGNREGA's two facts have NO shared grain — both are at source-row level, many
# rows per village-year on each side. Joining them directly (however sensible
# the ON clause looks: year_key + lgd_block, or geography_key) is a cartesian
# fan-out: every expenditure row pairs with every employment row for the same
# area, and the SUMs come back multiplied. schema_context MGNREGA rule 1 has
# always forbidden it, but the rule alone does not hold under sampling —
# confirmed live 2026-09-17, "compare expenditure and person-days in RERAPARA":
# the generator joined v_expenditure to v_employment and returned ₹367,647 lakh
# / 109,448,220 person-days against a truth of ₹2,450.98 lakh / 986,020. That is
# far more dangerous than the missing-column error it replaced, because it runs
# clean and the numbers look plausible. The correct shape is one CTE per fact,
# each aggregated independently, then combined.
_MGNREGA_FACT_OBJECTS = (
    "v_employment", "fact_mgnrega_employment",
    "v_expenditure", "fact_mgnrega_expenditure",
)
_EMPLOYMENT_OBJ_RE = re.compile(r"\b(?:curated\.)?(?:v_employment|fact_mgnrega_employment)\b",
                                re.IGNORECASE)
_EXPENDITURE_OBJ_RE = re.compile(r"\b(?:curated\.)?(?:v_expenditure|fact_mgnrega_expenditure)\b",
                                 re.IGNORECASE)
_JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)
_CTE_RE = re.compile(r"^\s*WITH\b", re.IGNORECASE)


def _mgnrega_facts_joined(sql: str) -> bool:
    """True when the query JOINs the employment fact to the expenditure fact in
    one statement — the fan-out shape rule 1 forbids.

    The safe shapes name both objects too, but wrap each in its own aggregated
    scope so anything joined afterwards is one row per side:
      * a CTE per fact  (WITH emp AS (...), exp AS (...) SELECT ...), and
      * an inline subquery per fact  (FROM (SELECT SUM(...) ...) exp CROSS JOIN
        (SELECT SUM(...) ...) emp) — which is what the repair prompt actually
        produces most often, and is equally correct.
    Both are recognised by each fact object sitting inside a parenthesised
    SELECT that aggregates. Only a bare top-level join of the two raw facts
    multiplies, so only that is flagged."""
    s = sql or ""
    if not (_EMPLOYMENT_OBJ_RE.search(s) and _EXPENDITURE_OBJ_RE.search(s)
            and _JOIN_RE.search(s)):
        return False
    if _CTE_RE.match(s.strip()):
        return False
    # Each fact reference that sits inside a parenthesised aggregating SELECT is
    # already collapsed to one row; if BOTH are, nothing can fan out.
    return not all(_fact_ref_is_aggregated(s, rx)
                   for rx in (_EMPLOYMENT_OBJ_RE, _EXPENDITURE_OBJ_RE))


_AGG_SELECT_RE = re.compile(r"\b(?:SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)


def _fact_ref_is_aggregated(sql: str, obj_re: "re.Pattern") -> bool:
    """True when every reference to this fact object lies inside a parenthesised
    SELECT that aggregates — i.e. it contributes one row, not many."""
    for m in obj_re.finditer(sql):
        depth, start = 0, None
        for i in range(m.start() - 1, -1, -1):   # walk back to the enclosing "("
            c = sql[i]
            if c == ")":
                depth += 1
            elif c == "(":
                if depth == 0:
                    start = i
                    break
                depth -= 1
        if start is None:
            return False                          # top-level reference
        if not _AGG_SELECT_RE.search(sql[start:m.start()]):
            return False                          # a plain subquery, not aggregated
    return True


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


# Client UAT sheet (FOCUS-018/019, 2026-09-13): "Give me the status breakdown
# for [tranche]" / "for [batch]" came back GROUP BY focus_status,
# verification_status even though schema_context.py rule 13 and the matching
# focusplus_few_shot.yaml examples both say an unqualified status breakdown
# means focus_status alone — verification_status is a near-constant single
# value ("Approved" on every 12.5K row) that only adds noise. The prose rule
# and the correctly-written few-shot examples exist and are scoped right; the
# generator still occasionally copies the two-column GROUP BY from the
# deliberately-different "what status values are recorded" enumeration
# example once a tranche/batch filter is also in play — the same class of
# prose-doesn't-reliably-win gap as _focusplus_single_district_beneficiary_guard
# above. Deterministic strip instead of a repair round trip, since the fix
# (drop one column) is unambiguous once the shape is detected.
# CM Elevate Legacy: the Unresolved-placeholder exclusion belongs ONLY on village
# counts and village lists (schema_context rule 9, geography_unresolved_rule).
# The 404 no-village records still have a known district, so a district / scheme
# / year total that drops them is simply wrong. Confirmed in the 2026-09-25
# use-case QA (TC-27, "total disbursement for each district"): the generator
# copied the filter from the village shots and returned ₹53.30 Cr statewide
# against a true ₹81.10 Cr. The rule is in the prompt; it does not reliably win,
# and the fix (delete one predicate) is unambiguous once the shape is detected.
_UNRESOLVED_NE = r"(?:\w+\.)?entity_type\s*(?:<>|!=)\s*'Unresolved'"
_UNRESOLVED_WHERE_AND_RE = re.compile(rf"\bWHERE\s+{_UNRESOLVED_NE}\s+AND\s+", re.IGNORECASE)
_UNRESOLVED_AND_RE = re.compile(rf"\s+AND\s+{_UNRESOLVED_NE}", re.IGNORECASE)
_UNRESOLVED_WHERE_ONLY_RE = re.compile(rf"\s*\bWHERE\s+{_UNRESOLVED_NE}(?=\s*(?:GROUP|ORDER|LIMIT|;|\)|$))",
                                       re.IGNORECASE)
_VILLAGE_SQL_RE = re.compile(r"\bvillage_code\b|\blgd_village_name\b", re.IGNORECASE)
_VILLAGE_WORD_RE = re.compile(r"\bvillages?\b", re.IGNORECASE)
# Focus Legacy has the same placeholder design and the same rule (README §5,
# "entity_type <> 'Unresolved' on village counts and lists only — never on
# money, district"). Its 2026-09-25 QA hit the identical bug: TC-18 "total amount
# disbursed for East Khasi Hills" dropped 11 no-village payments (₹5,35,15,000
# against a true ₹5,42,05,000).
_UNRESOLVED_PLACEHOLDER_SCHEMES = (["CM Elevate Legacy"], ["Focus Legacy"])


def _cm_legacy_keep_unresolved_off_village(question: str, schemes: list[str], sql: str) -> str:
    if (schemes not in _UNRESOLVED_PLACEHOLDER_SCHEMES
            or not re.search(_UNRESOLVED_NE, sql or "", re.IGNORECASE)):
        return sql
    if _VILLAGE_SQL_RE.search(sql) or _VILLAGE_WORD_RE.search(question or ""):
        return sql
    out = _UNRESOLVED_WHERE_AND_RE.sub("WHERE ", sql)
    out = _UNRESOLVED_AND_RE.sub("", out)
    out = _UNRESOLVED_WHERE_ONLY_RE.sub("", out)
    if out != sql:
        logger.info("%s: dropped entity_type <> 'Unresolved' from a non-village query — "
                    "the no-village records still count at district grain", schemes[0])
    return out


# CM Elevate Legacy constituency SQL joins curated.dim_geography for ac_name, and
# the view already carries every geography column dim_geography has
# (lgd_district, lgd_block, …). An unqualified one is then "ambiguous" to
# Postgres, and the repair loop kept re-emitting it until the budget ran out
# (TC-31 "applications mapped to Mairang constituency", 2026-09-25). The view's
# copy is always the intended one, so qualify bare references with its alias.
_CML_VIEW_ALIAS_RE = re.compile(
    r"\bFROM\s+curated\.v_cm_elevate_disbursement\s+(?:AS\s+)?(?!JOIN\b|WHERE\b|GROUP\b|ORDER\b|LIMIT\b)(\w+)",
    re.IGNORECASE)
_DIM_GEO_JOIN_RE = re.compile(r"\bJOIN\s+curated\.dim_geography\b", re.IGNORECASE)
_SHARED_GEO_COLS = ("lgd_district", "lgd_block", "lgd_village_name", "village_code",
                    "entity_type", "on_roster", "has_geo_conflict")


def _cm_legacy_qualify_shared_geo_cols(schemes: list[str], sql: str) -> str:
    if schemes != ["CM Elevate Legacy"] or not _DIM_GEO_JOIN_RE.search(sql or ""):
        return sql
    m = _CML_VIEW_ALIAS_RE.search(sql)
    if not m:
        return sql
    alias = m.group(1)
    out = sql
    for col in _SHARED_GEO_COLS:
        # Leave output aliases ("AS lgd_district") and already-qualified refs alone.
        out = re.sub(rf"(\bAS\s+)?(?<![\w.]){col}\b",
                     lambda mm, c=col: mm.group(0) if mm.group(1) else f"{alias}.{c}",
                     out, flags=re.IGNORECASE)
    if out != sql:
        logger.info("CM Elevate Legacy: qualified view geography columns with %r "
                    "(dim_geography join)", alias)
    return out


# Focus Legacy: a group's member count is recorded on EACH of its payments, so
# "how many members does <group> have" / "groups with more than N members" is the
# group's SIZE — MAX(no_of_pg_members) per pg_id. The generator SUMmed it across
# the group's payments instead, and 2,655 groups were paid more than once: a
# 20-member group paid twice read "40 members", the 190-member outlier "201"
# (use-case QA TC-14b / TC-15, 2026-09-25). SUM(no_of_pg_members) stays correct
# for memberships PAID FOR (a district / year / scheme total), so this fires only
# when the SQL works at group grain — GROUP BY pg_id, or a filter on one group.
_FL_GROUP_SIZE_Q = re.compile(
    r"\bhow\s+many\s+members\b|\bnumber\s+of\s+members\s+(?:in|of)\b|"
    r"\b(?:more|less|fewer)\s+than\s+\d+\s+(?:of\s+)?members\b|"
    r"\b(?:over|above|under|below|at\s+least|at\s+most)\s+\d+\s+members\b|"
    r"\bgroup\s+size\b|\bmember\s+count\b|\b(?:largest|biggest|smallest)\s+(?:producer\s+)?groups?\b",
    re.IGNORECASE)
_FL_MONEY_Q = re.compile(r"\bpaid\b|\bmemberships\b|\bamount\b|\bdisburs\w*|\breceiv\w*|\bmoney\b|"
                         r"\bremit\w*|\brupees?\b|₹|\brs\.?\s*\d", re.IGNORECASE)
_FL_SUM_MEMBERS = re.compile(r"\bSUM\s*\(\s*(?:\w+\.)?no_of_pg_members\s*\)", re.IGNORECASE)
_FL_GROUP_GRAIN = re.compile(
    r"\bGROUP\s+BY\s+(?:[\w.]+\s*,\s*)*(?:\w+\.)?pg_id\b|\b(?:\w+\.)?pg_(?:name|id)\s*(?:=|ILIKE|LIKE|IN)\b",
    re.IGNORECASE)


# Focus Legacy: the view's audit copies block_name_raw / district_name_raw are NULL
# on every row since the 2026-09-25 database change (the curated lgd_block /
# lgd_district were fixed to carry the source values instead). A query filtering
# on them returns nothing: "How many Producer Groups are mapped to Nongstoin
# block?" answered "no matching records" against a true 460 (bulk block QA, 3 of
# 168 questions). Read the curated columns, which hold the same values.
_FL_RAW_GEO_COL = re.compile(r"\b(block|district)_name_raw\b", re.IGNORECASE)


def _focus_legacy_geo_columns(schemes: list[str], sql: str) -> str:
    if schemes != ["Focus Legacy"] or not _FL_RAW_GEO_COL.search(sql or ""):
        return sql
    out = _FL_RAW_GEO_COL.sub(lambda m: f"lgd_{m.group(1).lower()}", sql)
    logger.info("Focus Legacy: block/district_name_raw -> lgd_block/lgd_district (raw copies are empty)")
    return out


def _focus_legacy_group_size_summed(question: str, schemes: list[str], sql: str) -> bool:
    return (schemes == ["Focus Legacy"]
            and bool(_FL_GROUP_SIZE_Q.search(question or ""))
            and not _FL_MONEY_Q.search(question or "")
            and bool(_FL_SUM_MEMBERS.search(sql or ""))
            and bool(_FL_GROUP_GRAIN.search(sql or "")))


_VERIFICATION_MENTION = re.compile(r"verif\w*", re.IGNORECASE)
_STATUS_ENUMERATE_AUDIT = re.compile(
    r"what\s+\S+\s+status\s+values|which\s+\S+\s+status\s+values|"
    r"status\s+values\s+are\s+recorded|what\s+status(?:es)?\s+(?:exist|are\s+there)",
    re.IGNORECASE,
)
_GROUP_BY_COLS_RE = re.compile(r"\bGROUP BY\s+([^\n;]+)", re.IGNORECASE)


def _focusplus_drop_unrequested_verification_status(question: str, schemes: list[str],
                                                     sql: str) -> str:
    if schemes != ["Focus Plus"]:
        return sql
    if _VERIFICATION_MENTION.search(question) or _STATUS_ENUMERATE_AUDIT.search(question):
        return sql
    m = _GROUP_BY_COLS_RE.search(sql)
    if not m:
        return sql
    group_cols = [c.strip() for c in m.group(1).split(",")]
    if "focus_status" not in group_cols or "verification_status" not in group_cols:
        return sql
    new_group_by = "GROUP BY " + ", ".join(c for c in group_cols if c != "verification_status")
    sql = sql[:m.start()] + new_group_by + sql[m.end():]
    sql = re.sub(r"\bverification_status\s*,\s*", "", sql, count=1)
    sql = re.sub(r",\s*verification_status\b(?!\s*=)", "", sql, count=1)
    logger.info("dropped unrequested verification_status column from a focus_status breakdown")
    return sql


# A `column "X" does not exist` error usually means the generator picked a view
# that lacks a geography column (e.g. v_pmay_monthly_sanctions) rather than a
# genuine typo. Point the repair at the objects that DO carry the column instead
# of just echoing the Postgres message back.
_MISSING_COL_RE = re.compile(r'column "([\w.]+)" does not exist', re.IGNORECASE)
_COL_HOMES = {
    "lgd_district": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                    "curated.v_district_year_summary, curated.v_focus_plus, "
                    "curated.v_cm_elevate, "
                    "curated.v_cross_scheme_money_district_year, curated.dim_geography",
    "lgd_block": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                 "curated.v_focus_plus, curated.v_cm_elevate, curated.dim_geography",
    "lgd_village_name": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                        "curated.v_focus_plus, curated.v_cm_elevate, curated.dim_geography",
}


# MGNREGA's two facts are separate objects with NO overlapping measures:
# employment (person_days, persons_employed, households_employed,
# households_completed_100_days, job_cards_issued_total) lives on
# curated.v_employment; money (total_exp, unskilled_wage_exp,
# semi_skilled_wage_exp, material_exp) lives on curated.v_expenditure. A
# question that wants one of each ("compare expenditure and person-days in
# RERAPARA") cannot be answered from a single view, and the generator reliably
# tries anyway — selecting person_days off v_expenditure, which errors, and
# then failing to recover because the generic missing-column hint only says
# "select from an object that exposes every column" without explaining that no
# such object exists at block grain (confirmed live 2026-09-17: the question
# burned its whole repair budget and fell through to "couldn't build a working
# query"). docs/schema_for_developers.md: v_district_year_summary is the only
# sanctioned combined object, and it is district x year ONLY — so at block or
# village grain the answer is two CTEs.
_EMPLOYMENT_MEASURES = {
    "person_days", "persons_employed", "households_employed",
    "households_completed_100_days", "job_cards_issued_total",
    "women_employment_provided",
}
_EXPENDITURE_MEASURES = {
    "total_exp", "unskilled_wage_exp", "semi_skilled_wage_exp",
    "material_exp", "tax_exp", "admin_total_exp",
}


def _mgnrega_split_fact_hint(col: str) -> "str | None":
    """The recipe for combining MGNREGA employment and expenditure measures,
    when the missing column is one that lives on the OTHER fact."""
    if col in _EMPLOYMENT_MEASURES:
        wanted, home, other = col, "curated.v_employment", "curated.v_expenditure"
    elif col in _EXPENDITURE_MEASURES:
        wanted, home, other = col, "curated.v_expenditure", "curated.v_employment"
    else:
        return None
    return (
        f'"{wanted}" lives on {home}, NOT on {other} — MGNREGA keeps employment and '
        "expenditure in two separate facts that share no measures, so ONE view can "
        "never supply both. To report a measure from each, aggregate them "
        "independently and join the results:\n"
        "  WITH emp AS (SELECT SUM(person_days) AS person_days FROM curated.v_employment "
        "WHERE <same filters>),\n"
        "       exp AS (SELECT SUM(total_exp) AS total_exp_lakh FROM curated.v_expenditure "
        "WHERE <same filters>)\n"
        "  SELECT exp.total_exp_lakh, emp.person_days FROM emp, exp\n"
        "Put the SAME geography and year_key filters on BOTH CTEs. Add a GROUP BY plus a "
        "FULL OUTER JOIN on the grain columns only if the question asks for a per-district "
        "/ per-block / per-year breakdown. At DISTRICT x YEAR grain you may instead select "
        "both measures directly from curated.v_district_year_summary, which already "
        "combines the two facts; it has no block or village column, so it cannot be used "
        "for a block- or village-level question."
    )


def _missing_column_hint(error: str) -> "str | None":
    m = _MISSING_COL_RE.search(error)
    if not m:
        return None
    col = m.group(1).split(".")[-1].lower()
    split_fact = _mgnrega_split_fact_hint(col)
    if split_fact:
        return split_fact
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
                "curated.v_cross_scheme_money_district_year. The per-scheme fact tables "
                "and views (curated.v_pmay, curated.v_expenditure, curated.v_employment, "
                "curated.v_focus_plus, curated.v_cm_elevate, curated.fact_pmay_house, "
                "curated.fact_mgnrega_*, curated.fact_focus_plus_disbursement, "
                "curated.fact_cm_elevate_application) are each already a single scheme — "
                "DELETE the scheme filter and any JOIN to curated.dim_scheme entirely, and "
                "keep every other clause (year_key, geography, NOT is_placeholder, "
                "aggregation) exactly as it was.")
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


_VILLAGE_CODE_FILTER_RE = re.compile(r"\bvillage_code\s*(?:=|IN)\s*", re.IGNORECASE)
_VILLAGE_NAME_FILTER_RE = re.compile(r"\blgd_village_name\s*(?:=|ILIKE|IN)\s*", re.IGNORECASE)
# The generator also substitutes the WRONG ADMIN LEVEL for a resolved village:
# it writes lgd_block = 'NONGLADEW' or lgd_district = 'NONGLADEW' — a village
# name placed in a block/district column. That matches zero rows and returns a
# clean, confident 0 ("the data doesn't cover applicants in Nongladew") when the
# village has 32 real records. Same root cause as the lgd_village_name case
# above, and just as invisible: non-deterministic across runs, so the same
# question answers district one time and block the next (reported 2026-09-17).
_WRONG_LEVEL_FILTER_RE = re.compile(
    r"(?:\w+\.)?\blgd_(?P<col>district|block)\b\s*\)?\s*(?:=|ILIKE|IN)\s*\(?\s*"
    r"(?:UPPER\s*\(\s*)?'(?P<val>[^']+)'",
    re.IGNORECASE,
)
_DIV_100_RE = re.compile(r"/\s*100\b")
# CM Elevate's is_withdraw is a real, well-defined boolean column that is FALSE
# on every one of the 8,543 rows today (schema_context.py rule 15) — a filtered
# COUNT against it is a genuine, correct zero, not a sign of a missing/hallucinated
# metric. compose_response's generic zero-hedging guidance (written to catch
# hallucinated columns that always read NULL/0) can't tell the two apart on its
# own, so a query that visibly filters is_withdraw gets an explicit note telling
# the composer this particular zero IS the real, complete answer (confirmed live
# 2026-09-12: without this, "how many applications have been withdrawn in Ri
# Bhoi" — SQL correctly `is_withdraw = TRUE`, 0 rows — was composed as "doesn't
# cover a withdrawal count", a false refusal over a query that ran exactly right).
_IS_WITHDRAW_FILTER_RE = re.compile(r"\bis_withdraw\b", re.IGNORECASE)


def _genuine_zero_notes(sql: str) -> list[str]:
    """Notes overriding compose_response's default zero-hedge for CM Elevate
    columns where a real 0 is the correct, complete answer (not a missing
    metric) — see _IS_WITHDRAW_FILTER_RE above."""
    if _IS_WITHDRAW_FILTER_RE.search(sql):
        return [
            "is_withdraw is a real, always-queryable boolean column (FALSE on every "
            "current row) — if the result is 0, that IS the true, complete withdrawal "
            "count for this scope. State it plainly as '0 applications withdrawn', "
            "never as data that 'isn't tracked' or 'doesn't cover' withdrawals."
        ]
    return []


def _sector_not_tracked_notes(rows: list[dict]) -> list[str]:
    """CM Elevate scheme_specific ->> 'sector_id' rule 9b requires stating
    plainly that sector isn't recorded for a scheme rather than reading a bare
    sector_recorded=0 as "checked and found none" — but that instruction lives
    in the SQL-generation prompt (schema_context.py), which compose_response
    never sees. Without a note here, a row like {scheme_name: Piggery,
    poultry_sector_applicants: 0, sector_recorded: 0, scheme_total: 1944} gets
    composed as "Piggery has 0" — technically not wrong, but exactly the
    misleading "checked and found none" reading rule 9b exists to avoid
    (confirmed live 2026-09-13 UAT on "applicants under Piggery and Poultry
    associated with poultry sector"). Detected generically from the result
    shape (a sector_recorded column that's 0 in a row where some OTHER count
    in that same row is non-zero) rather than a hardcoded scheme list, so it
    keeps working if which schemes carry sector_id ever changes."""
    notes: list[str] = []
    for r in rows:
        if not isinstance(r, dict) or "sector_recorded" not in r:
            continue
        if r.get("sector_recorded"):
            continue
        if any(k != "sector_recorded" and isinstance(v, (int, float)) and v
               for k, v in r.items()):
            name = r.get("scheme_name") or "this scheme"
            notes.append(
                f"sector_recorded is 0 for {name} in this result, with no sector "
                f"value recorded for it at all in this scope. Say exactly this, as "
                f"a plain fact alongside the other rows' real numbers: 'sector "
                f"isn't tracked for {name}.' Do NOT phrase it as 'has 0', and do "
                "NOT use hedge wording like 'doesn't cover', 'not covered', 'no "
                "data' or 'not available' — this is one specific, known fact about "
                "one row, not a reason to doubt or soften the OTHER rows' real, "
                "reportable numbers in the same result."
            )
    return notes


def _crore_conversion_for_single_village(entity_result: dict, sql: str) -> bool:
    """True when the SQL converts a MGNREGA money column to CRORE (÷100) while
    the question is scoped to a single village — a grain small enough that
    the true figure is routinely well under 1 crore, so rounding the crore
    value to 2 decimal places can display a real, non-zero lakh amount as
    "0.00 crore" (reported 2026-09-10 UAT: Maska's real 0.49 lakh MGNREGA
    expenditure came back as "0.00 crore" for exactly this reason). MGNREGA
    money is natively LAKH (schema_context's MGNREGA rule 4) — the crore
    conversion exists only for cross-scheme/statewide normalisation against
    PMAY's rupee figures, never for a single village's own figure."""
    if not entity_result.get("resolved", {}).get("village_code"):
        return False
    # "crore" is checked as a plain substring, not \bcrore\b — it is always used
    # here as an identifier suffix ("total_expenditure_crore"), and an
    # underscore is a word character, so \b never falls between "_" and "c".
    return bool(_DIV_100_RE.search(sql)) and "crore" in sql.lower()


def _village_name_filter_instead_of_code(entity_result: dict, sql: str) -> "int | None":
    """The resolved village_code when the question resolved to one but the
    generated SQL filters on lgd_village_name instead — the exact "Bamil
    Reserve Apal" bug (2026-09-09): entity resolution correctly picked
    village_code, prompt_builder's RESOLVED ENTITIES block told the generator
    to use it verbatim and NOT filter on lgd_village_name, and the generator
    did it anyway, inventing an upper-cased lgd_village_name literal. Storage
    keeps village names in mixed/title case (curated.v_pmay has "Bamil
    Reserve Apal", not "BAMIL RESERVE APAL"), so the literal silently matches
    zero rows and the query runs clean but returns 0. village_code is the
    only column guaranteed to match."""
    code = entity_result.get("resolved", {}).get("village_code")
    if code is None or _VILLAGE_CODE_FILTER_RE.search(sql):
        return None
    return code if _VILLAGE_NAME_FILTER_RE.search(sql) else None


def _village_filtered_at_wrong_level(entity_result: dict, sql: str) -> "tuple[int, str] | None":
    """(village_code, offending clause) when a resolved village is filtered as a
    BLOCK or DISTRICT instead, else None.

    Only fires when the literal is the village's OWN display name — a genuine
    district scope alongside a village ("... in NONGLADEW, Ri Bhoi") is a real,
    correct filter and must be left alone.

    A correct `village_code` filter being present is NOT on its own a reason to
    pass: the generator sometimes emits BOTH, ANDing a bogus
    `lgd_block = '<village name>'` onto the right village_code. That still
    matches zero rows, so the query returns a confident 0 for a village with
    real records — confirmed live 2026-09-17 on "william nagar(mb) - ward
    no.4", where village_code = 70675 was correct and the added
    `lgd_block = 'WILLIAM NAGAR(MB)'` zeroed a true count of 1."""
    resolved = entity_result.get("resolved") or {}
    code = resolved.get("village_code")
    if code is None:
        return None
    display = (entity_result.get("display") or {}).get("village")
    if not display:
        return None
    # Compare with punctuation and spacing squashed out, and accept a PREFIX of
    # the village name as well as the whole of it: the literal the generator
    # writes is often the same truncation the extractor produced
    # ("WILLIAM NAGAR(MB)" for the village "William Nagar (MB) - Ward No.4").
    # It is still a village name sitting in a block/district column either way.
    _squash = lambda t: re.sub(r"[^A-Z0-9]", "", str(t).upper())
    want = _squash(display)
    for m in _WRONG_LEVEL_FILTER_RE.finditer(sql or ""):
        got = _squash(m.group("val"))
        # Guard against a 1-2 character fragment matching by accident.
        if got and len(got) >= 4 and want.startswith(got):
            return code, f"lgd_{m.group('col').lower()} = '{m.group('val')}'"
    return None


def _village_code_as_geography_key(entity_result: dict, sql: str) -> "int | None":
    """The resolved village_code when the generated SQL filters `geography_key`
    directly to that same integer literal instead of `village_code` — a
    silent-wrong-number bug distinct from the lgd_village_name one above.
    geography_key is a small surrogate key on curated.dim_geography, entirely
    unrelated to the LGD village_code (e.g. MASKA village is geography_key =
    5999 but village_code = 277769), so plugging the resolved village_code
    value straight into geography_key's WHERE clause matches no row and the
    query runs clean but returns NULL (reported 2026-09-10 UAT: a real ~0.5
    lakh MGNREGA expenditure for a small village came back "doesn't cover
    that metric" because the generated SQL filtered geography_key = 277769 —
    a village_code — instead of village_code = 277769). Filtering geography_key
    via a dim_geography subquery keyed on village_code is fine and NOT
    flagged — only a bare literal placed in geography_key's slot is."""
    code = entity_result.get("resolved", {}).get("village_code")
    if code is None:
        return None
    if re.search(rf"\bgeography_key\s*=\s*{code}\b", sql):
        return code
    return None


# The SQL verifier (a small model) occasionally hallucinates that the
# RESOLVED ENTITIES block it was just handed "is empty" even when
# prompt_builder._entities_block plainly rendered entries into it — confirmed
# live 2026-09-13: "How many Focus+ beneficiaries are there in wgh across all
# financial years" resolved district = WEST GARO HILLS correctly, the verify
# prompt genuinely contained "lgd_district = 'WEST GARO HILLS'" under
# "RESOLVED ENTITIES — MANDATORY...", and the verifier still claimed the
# block was empty on every one of 5 repeat calls with an unchanged prompt.
# Each "repair" attempt then re-sent the same (correct) SQL, got the same
# false complaint back, and after 4 attempts the whole question fell through
# to the KB fallback instead of ever running the query. entity_result
# ["resolved"] is ground truth this process built itself (not the model's
# guess), so a claim that contradicts it is a verifier error to discard, not
# a real Check 2 hit — unlike the case where resolved really is empty, which
# this guard leaves alone.
_VERIFIER_FALSE_EMPTY_ENTITIES = re.compile(
    r"resolved entities\b[^.]{0,200}\bis empty\b", re.IGNORECASE)

# Second known verifier false positive, same family as the one above. The
# RESOLVED ENTITIES block renders an assembly-constituency filter in a
# case-insensitive wrapper — UPPER(assembly_constituency_name) = UPPER('X') —
# because the SME catalogue's spelling and the stored spelling can differ in
# case. assembly_constituency_name is stored ALL CAPS
# (mgnrega_entity_resolver.yaml: stored_case: ALL CAPS), so a generator that
# writes the equivalent bare `assembly_constituency_name = 'X'` has produced a
# query that selects exactly the same rows. The verifier nonetheless flags the
# difference in FORM as a Check-2 entity mismatch (confirmed live 2026-09-15:
# "…within the MAWLAI block only" — attempt 1 was flagged purely for dropping
# the UPPER() wrapper). That rejection then feeds a repair prompt telling the
# generator its entity filter is "missing", and the repair reliably "fixes" it
# by dropping the OTHER filter instead, so every later attempt is flagged for a
# genuinely missing entity and the whole question falls through to the KB
# fallback ("couldn't build a working query").
#
# Only discarded when the column really is filtered to the resolved value in
# the SQL — a genuinely absent filter still raises, which is the check's whole
# purpose.
_VERIFIER_CASE_FORM_COMPLAINT = re.compile(
    r"\b(?:lowercase|uppercase|upper\(|case[- ]insensitive|verbatim|"
    r"correct case)\b", re.IGNORECASE)


def _verifier_complaint_is_cosmetic(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier's complaint is about the FORM of a filter that is
    in fact present and correct in the SQL."""
    if not _VERIFIER_CASE_FORM_COMPLAINT.search(issue or ""):
        return False
    ac = (resolved or {}).get("assembly_constituency")
    if not ac:
        return False
    # The AC value is genuinely filtered on, in either form.
    return bool(re.search(
        rf"assembly_constituency_name\s*\)?\s*=\s*(?:UPPER\s*\(\s*)?'{re.escape(str(ac))}'",
        sql or "", re.IGNORECASE))


# Third known verifier false positive. When a village_code is resolved, the
# RESOLVED ENTITIES block deliberately SUPPRESSES the district/block that
# merely scoped the village lookup (prompt_builder._entities_block: they are
# redundant with village_code, which already pins one exact row-set, and
# rendering them as equally MANDATORY made the verifier demand filters the
# generator was right to omit). The verifier nonetheless sometimes "quotes" a
# district entity that was never in its prompt and rejects village-only SQL for
# omitting it — confirmed live 2026-09-15 on "ASIMGRE ... for East Garo Hills",
# where the block plainly contained only `village_code = 275373` yet the issue
# read "RESOLVED ENTITIES block lists lgd_district = 'EAST GARO HILLS'".
# Correct SQL is then repaired away and the question dies in the KB fallback.
#
# Discarded only when a village_code IS resolved AND the SQL genuinely filters
# on it — i.e. exactly the shape where the suppressed district is redundant.
_VERIFIER_SUPPRESSED_GEO_COMPLAINT = re.compile(
    r"\b(?:lgd_district|lgd_block|district|block)\b[^.]{0,120}"
    r"\b(?:missing|not present|omitted|absent|instead)\b|"
    r"\b(?:missing|not present|omitted|absent)\b[^.]{0,120}"
    r"\b(?:lgd_district|lgd_block|district|block)\b",
    re.IGNORECASE,
)


def _verifier_wants_suppressed_geography(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier demands a district/block that _entities_block
    intentionally left out because a resolved village_code supersedes it."""
    code = (resolved or {}).get("village_code")
    if code is None:
        return False
    if not _VERIFIER_SUPPRESSED_GEO_COMPLAINT.search(issue or ""):
        return False
    return bool(re.search(rf"\bvillage_code\s*(?:=|IN)\s*\(?\s*{re.escape(str(code))}\b",
                          sql or "", re.IGNORECASE))


# Fourth known verifier false positive. A financial year is stored by its START
# year — FY 2023-24 IS year_key = 2023 (docs/DATA_MODEL.md; entity resolution
# emits exactly that). The verifier repeatedly reads the "-24" half as the value
# that should appear and flags correct SQL as using the wrong year: confirmed
# live 2026-09-17 on "…in RERAPARA … for FY 2023-24", where SQL filtering
# year_key = 2023 — matching the resolved entity verbatim — was rejected on 5 of
# 5 calls ("the question asks for FY 2023-24, but the SQL filters on year_key =
# 2023"). Every repair then re-sent the same correct query, the budget ran out,
# and a perfectly answerable question died in the KB fallback.
#
# Discarded only when the SQL's year_key genuinely equals the resolved one, so a
# real year mismatch still raises.
_VERIFIER_YEAR_COMPLAINT = re.compile(r"\byear_key\b|\bfinancial year\b|\bfy\b", re.IGNORECASE)


def _verifier_year_complaint_is_false(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier disputes the year but the SQL already filters on
    exactly the resolved year_key."""
    year = (resolved or {}).get("year_key")
    if year is None:
        return False
    if not _VERIFIER_YEAR_COMPLAINT.search(issue or ""):
        return False
    filters = set(re.findall(r"\byear_key\s*=\s*(\d{4})\b", sql or ""))
    # A view that carries the FY label filters on it instead — financial_year_short
    # = '2024-25' IS year_key 2024 (Focus Legacy QA TC-23, 2026-09-25: rejected as
    # "the resolved entity value (2024) is not present in the WHERE clause").
    filters |= set(re.findall(r"\bfinancial_year(?:_short)?\s*=\s*'(\d{4})-\d{2}'", sql or "",
                              re.IGNORECASE))
    return filters == {str(int(year))}


# Fifth known verifier false positive, and the most clear-cut of the family: the
# verifier states as fact that the WHERE clause "omits these filters entirely"
# when the filters are sitting in it verbatim. Confirmed live 2026-09-18 on
# "How many applicants are there in BATABARI under Agro Tourism Villa Scheme,
# PRIME Small Enterprise Empowerment and Development (SEED) and Meghalaya
# Poultry Farming Scheme, BATABARI block, WEST GARO HILLS": entity resolution
# produced block=BATABARI + district=WEST GARO HILLS, the generator emitted
#   AND lgd_block = 'BATABARI' AND lgd_district = 'WEST GARO HILLS'
# and the verifier still returned "RESOLVED ENTITIES block lists lgd_block =
# 'BATABARI' and lgd_district = 'WEST GARO HILLS', but the SQL WHERE clause
# omits these filters entirely" on 10 of 10 calls. Each repair re-sent the same
# correct SQL, the 3-repair budget ran out, and a question whose answer is a
# plain 46 (SEED 43 + Poultry 2 + Agro Tourism Villa 1) died in the KB fallback
# as "couldn't build a working query".
#
# Unlike the assembly-constituency guard above, this is not about the FORM of a
# filter — the verifier is misreading a long multi-line WHERE clause and denying
# a literal that is plainly there. So the test is the strongest one available:
# discard the complaint only when EVERY resolved geography entity it names is
# provably filtered on in the SQL. A genuinely missing filter still raises,
# which keeps Check 2 doing its job.
_VERIFIER_MISSING_GEO_COMPLAINT = re.compile(
    r"\b(?:omits?|omitted|omitting|missing|absent|not present|no filter|lacks?|"
    r"does not (?:include|filter|contain)|fails to (?:include|filter))\b",
    re.IGNORECASE)

# resolved-entity key -> the SQL column prompt_builder._entities_block renders
# it as, which is the column the verifier names back in its complaint.
_RESOLVED_GEO_COLUMNS = {
    "district": "lgd_district",
    "block": "lgd_block",
    "assembly_constituency": "assembly_constituency_name",
}


def _sql_filters_on(sql: str, column: str, value: str) -> bool:
    """True when `sql` constrains `column` to `value`, in any of the forms the
    generator legitimately produces: bare equality, an UPPER()/LOWER() wrapper
    on either side, or membership in an IN (...) list."""
    val = re.escape(str(value))
    col = re.escape(column)
    # col = 'V'  |  UPPER(col) = 'V'  |  col = UPPER('V')
    eq = (rf"(?:UPPER|LOWER)?\s*\(?\s*{col}\s*\)?\s*=\s*"
          rf"(?:(?:UPPER|LOWER)\s*\(\s*)?'{val}'")
    if re.search(eq, sql or "", re.IGNORECASE):
        return True
    # col IN ('A', 'V', ...) — the value must be one of the listed literals.
    for m in re.finditer(
            rf"(?:UPPER|LOWER)?\s*\(?\s*{col}\s*\)?\s+IN\s*\(([^)]*)\)",
            sql or "", re.IGNORECASE):
        if re.search(rf"'{val}'", m.group(1), re.IGNORECASE):
            return True
    return False


def _verifier_missing_geo_is_false(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier claims resolved geography filters are absent but
    every one it could be referring to is in fact present in the SQL."""
    if not _VERIFIER_MISSING_GEO_COMPLAINT.search(issue or ""):
        return False
    # Restrict to the geography entities the complaint actually names, by
    # column name or by value — so an unrelated grievance never trips this.
    named = {}
    for key, col in _RESOLVED_GEO_COLUMNS.items():
        val = (resolved or {}).get(key)
        if not val or not isinstance(val, str):
            continue
        if re.search(rf"\b{re.escape(col)}\b", issue or "", re.IGNORECASE) or \
           re.search(re.escape(val), issue or "", re.IGNORECASE):
            named[col] = val
    if not named:
        return False
    return all(_sql_filters_on(sql, col, val) for col, val in named.items())


# Sixth known verifier false positive: a scheme name containing an apostrophe.
# In SQL a literal apostrophe is written doubled ('Chief Minister''s Green Taxi
# Scheme'), which is the SAME string as the resolved entity once parsed. The
# verifier reads the two spellings as different values and rejects correct SQL,
# quoting the identical text on both sides of its own complaint — confirmed
# live 2026-09-18 on "applicants in mylliem, the block ... under Green Taxi CM
# Elevate and ware house Scheme", flagged on 8 of 8 calls with
#   "RESOLVED ENTITIES block specifies scheme_name = 'Chief Minister''s Green
#    Taxi Scheme' (verbatim), but the SQL filters on ... 'Chief Minister''s
#    Green Taxi Scheme'"
# The query is right and returns 2; only the verifier disagrees, so the repair
# loop burned its budget and the question died in the KB fallback.
#
# Discarded only when un-doubling the SQL's apostrophes makes the resolved
# value genuinely present — a real value mismatch still raises.
_VERIFIER_VERBATIM_COMPLAINT = re.compile(
    r"\bverbatim\b|\bexact(?:ly)?\b|\bdoes not match\b|\bmismatch\b|"
    r"\bdiffer(?:ent|s)?\b|\bslightly different\b", re.IGNORECASE)


def _verifier_apostrophe_complaint_is_false(issue: str, resolved: dict, sql: str) -> bool:
    """True when the verifier disputes a resolved value that the SQL does carry,
    differing only by SQL's doubled-apostrophe escaping."""
    if not _VERIFIER_VERBATIM_COMPLAINT.search(issue or ""):
        return False
    # Only values that actually contain an apostrophe can hit this.
    vals = [v for v in (resolved or {}).values() if isinstance(v, str) and "'" in v]
    for v in (resolved or {}).values():
        if isinstance(v, list):
            vals.extend(x for x in v if isinstance(x, str) and "'" in x)
    if not vals:
        return False
    unescaped = (sql or "").replace("''", "'")
    return all(v in unescaped for v in vals)


# Seventh known verifier false positive: a "prohibited join" in SQL that joins
# nothing. The PROHIBITED JOINS block carries self-edges that are really grain
# rules ("NEVER join curated.v_focus_legacy -> curated.v_focus_legacy directly.
# Use GROUP BY pg_id instead.") and "any -> <base fact>" edges, and the small
# verifier reads them as forbidding the view itself. Confirmed 2026-09-25 in the
# Focus Legacy QA: TC-22 "records for each financial year" was rejected on every
# attempt ("The SQL joins directly to 'curated.v_focus_legacy', which is a
# prohibited join") for a one-table GROUP BY, and TC-20/TC-21 intermittently
# ("joins directly to curated.fact_focus_legacy_disbursement" — a table the SQL
# never names). The repair budget ran out and a plain COUNT(*) died as
# "couldn't build a working query".
#
# Discarded only when the complaint is about a PROHIBITED join and is provably
# false: the SQL has no JOIN and reads a single table, or every table the
# complaint names is absent from the SQL. A query that really joins a named
# table still raises.
_VERIFIER_PROHIBITED_JOIN = re.compile(r"\bprohibit\w*", re.IGNORECASE)
_SQL_TABLE_REF = re.compile(r"\b(?:curated|raw|semantic|meta|app)\.\w+", re.IGNORECASE)
_SQL_JOIN_KW = re.compile(r"\bJOIN\b", re.IGNORECASE)
_SQL_FROM_LIST = re.compile(r"\bFROM\s+[\w.]+(?:\s+(?:AS\s+)?\w+)?\s*,", re.IGNORECASE)


# Eighth: a Check 2 (resolved-entity) complaint when NOTHING was resolved. The
# verify prompt tells the verifier that "an empty or absent block means there is
# nothing to check here, so answer this check true" — yet it flagged "How many
# Producer Groups are mapped to each district" (GROUP BY lgd_district, no WHERE)
# on every attempt: "The RESOLVED ENTITIES block is empty ... the SQL attempts
# to group by lgd_district, which implies a geography filter exists" (Focus
# Legacy QA TC-25, 2026-09-25). Discarded only when the resolved block really is
# empty of filter entities and the complaint names no other check.
_VERIFIER_CHECK2 = re.compile(r"\bcheck\s*2\b|\bresolved\s+entit", re.IGNORECASE)
# Only an explicit reference to another check keeps the complaint alive — its
# wording ("filtering/aggregating on a dimension that was not resolved") is the
# same false Check 2 complaint, not a grain or metric finding.
_VERIFIER_OTHER_CHECK = re.compile(r"\bcheck\s*[134]\b|\bprohibit\w*|\bmissing\s+(?:sum|count|avg)\b",
                                   re.IGNORECASE)
def _verifier_check2_on_empty_entities(issue: str, resolved: dict) -> bool:
    if any(v not in (None, "", [], {}) for v in (resolved or {}).values()):
        return False
    return bool(_VERIFIER_CHECK2.search(issue or "")) and not _VERIFIER_OTHER_CHECK.search(issue or "")


def _verifier_join_complaint_is_false(issue: str, sql: str) -> bool:
    if not (_VERIFIER_PROHIBITED_JOIN.search(issue or "") and re.search(r"\bjoin", issue or "", re.IGNORECASE)):
        return False
    in_sql = {t.lower() for t in _SQL_TABLE_REF.findall(sql or "")}
    joins = bool(_SQL_JOIN_KW.search(sql or "") or _SQL_FROM_LIST.search(sql or ""))
    if not joins and len(in_sql) <= 1:
        return True
    named = {t.lower() for t in _SQL_TABLE_REF.findall(issue or "")}
    return bool(named) and not (named & in_sql)


async def _verify_sql(question: str, schemes: list[str], entity_result: dict, sql: str) -> "str | None":
    """One short issue sentence if the semantic verifier (SQL_VERIFY_MODEL,
    qwen4-deploy — see app/config.py) flags this SQL as not actually
    answering the question, else None.

    This is the catch-all for the class of bug the regex guards above can't
    be: they only recognise SQL shapes a past incident already taught them to
    match. The verifier judges the query against the same schema rules and
    resolved entities the generator itself was given, instead of a fixed
    pattern.

    Best-effort like every other auxiliary check in this module (premise_check,
    followups, context layer): a verifier outage, timeout, or unparseable
    response degrades to "no issue found" rather than blocking an answer the
    pipeline would otherwise have produced successfully."""
    if not settings.SQL_VERIFY_ENABLED:
        return None
    try:
        prompt = prompt_builder.build_verify_prompt(question, schemes, entity_result, sql)
        raw = await llm.call_sql_verifier(prompt, guided={"guided_json": _SQL_VERIFY_JSON_SCHEMA})
        data = _extract_json(raw)
    except Exception:  # noqa: BLE001
        logger.warning("SQL verifier call failed — continuing without it", exc_info=True)
        return None
    if not data or data.get("ok", True):
        return None
    issue = data.get("issue") or "the SQL verifier flagged this query as not answering the question"
    if entity_result.get("resolved") and _VERIFIER_FALSE_EMPTY_ENTITIES.search(issue):
        logger.warning(
            "SQL verifier claimed RESOLVED ENTITIES is empty when resolved=%r says otherwise "
            "— discarding as a known verifier hallucination: %s", entity_result["resolved"], issue)
        return None
    if _verifier_complaint_is_cosmetic(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier complained about the FORM of an assembly-constituency filter that "
            "is present and correct — discarding as cosmetic: %s", issue)
        return None
    if _verifier_year_complaint_is_false(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier disputed the financial year when the SQL already filters on the "
            "resolved year_key (a FY is stored by its START year) — discarding: %s", issue)
        return None
    if _verifier_apostrophe_complaint_is_false(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier disputed a value that differs only by SQL apostrophe escaping "
            "('' is a literal ') — discarding: %s", issue)
        return None
    if _verifier_missing_geo_is_false(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier claimed the resolved geography filters are missing when the "
            "SQL filters on every one of them verbatim — discarding: %s", issue)
        return None
    if _verifier_wants_suppressed_geography(issue, entity_result.get("resolved") or {}, sql):
        logger.warning(
            "SQL verifier demanded a district/block that the RESOLVED ENTITIES block "
            "deliberately suppressed as redundant with village_code — discarding: %s", issue)
        return None
    if _verifier_check2_on_empty_entities(issue, entity_result.get("resolved") or {}):
        logger.warning(
            "SQL verifier raised a resolved-entity (check 2) complaint with no resolved "
            "entities — its own instructions make that check pass — discarding: %s", issue)
        return None
    if _verifier_join_complaint_is_false(issue, sql):
        logger.warning(
            "SQL verifier reported a prohibited join the SQL does not make (no JOIN, or the "
            "named table is not in the query) — discarding: %s", issue)
        return None
    return issue


async def execute_with_repair(question: str, schemes: list[str], entity_result: dict,
                              initial_sql: str | None = None, *,
                              max_repairs: int = 3) -> tuple[str, list[dict]]:
    sql = initial_sql if initial_sql is not None else await generate_sql(question, schemes, entity_result)
    for attempt in range(max_repairs + 1):
        sql = _focus_legacy_geo_columns(schemes, sql)
        sql = _uppercase_geo_literals(sql)
        sql = _focusplus_drop_unrequested_verification_status(question, schemes, sql)
        sql = _cm_legacy_keep_unresolved_off_village(question, schemes, sql)
        sql = _cm_legacy_qualify_shared_geo_cols(schemes, sql)
        try:
            if _focus_legacy_group_size_summed(question, schemes, sql):
                raise ValueError(
                    "this question asks for a producer group's SIZE (its member count), but the "
                    "query SUMs no_of_pg_members across the group's payments. A group's member "
                    "count is recorded on EACH payment and 2,655 groups were paid more than once, "
                    "so a SUM double-counts them (a 20-member group paid twice reads 40). Use "
                    "MAX(no_of_pg_members) AS group_size per pg_id — GROUP BY pg_id with "
                    "MAX(pg_name) for display, and put any 'more than N members' test in HAVING "
                    "MAX(no_of_pg_members) > N. Keep every other clause as it was."
                )
            bad_code = _village_name_filter_instead_of_code(entity_result, sql)
            if bad_code is not None:
                raise ValueError(
                    f"the question resolved to village_code = {bad_code} but this query filters on "
                    "lgd_village_name instead — village name spelling/case is not reliable for "
                    "matching (storage keeps mixed/title case, not upper-case), so that filter can "
                    "silently match zero rows. Replace the lgd_village_name filter with "
                    f"village_code = {bad_code} exactly, and keep every other clause as it was."
                )
            wrong_level = _village_filtered_at_wrong_level(entity_result, sql)
            if wrong_level is not None:
                _code, _clause = wrong_level
                if _VILLAGE_CODE_FILTER_RE.search(sql):
                    # village_code is already correct — the bogus clause just
                    # has to go, not be replaced.
                    raise ValueError(
                        f"this query already filters village_code = {_code} correctly, but it "
                        f"ALSO filters {_clause} — a VILLAGE name placed in a block/district "
                        "column, which matches no row and forces the whole query to 0 even "
                        f"though village {_code} has real records. DELETE that clause entirely "
                        "and keep village_code and every other filter exactly as they are."
                    )
                raise ValueError(
                    f"the question resolved to village_code = {_code}, but this query filters "
                    f"{_clause} — that is a VILLAGE name placed in a block/district column, so "
                    "it matches zero rows and returns a confident 0 for a village that has "
                    f"real records. Replace that clause with village_code = {_code} exactly "
                    "(the curated view carries village_code as a direct column), and keep "
                    "every other clause as it was."
                )
            bad_geo_code = _village_code_as_geography_key(entity_result, sql)
            if bad_geo_code is not None:
                raise ValueError(
                    f"the question resolved to village_code = {bad_geo_code} but this query filters "
                    f"geography_key = {bad_geo_code} instead — geography_key is a different surrogate "
                    "key, unrelated in value to village_code, so this filter matches no row and "
                    "silently returns NULL/zero instead of the real figure. Replace "
                    f"geography_key = {bad_geo_code} with village_code = {bad_geo_code} exactly (the "
                    "curated view carries village_code as a direct column), and keep every other "
                    "clause as it was."
                )
            if _crore_conversion_for_single_village(entity_result, sql):
                raise ValueError(
                    "this query converts a MGNREGA money column to CRORE (dividing by 100) for a "
                    "question scoped to a single village — MGNREGA money is natively LAKH RUPEES, "
                    "and a single village's figure is routinely well under 1 crore, so rounding it "
                    "to crore at 2 decimal places can display a real, non-zero amount as a "
                    "misleading '0.00 crore'. Report the figure directly in LAKH instead (remove "
                    "the ÷100 conversion and the crore alias/label), and keep every other clause "
                    "as it was."
                )
            if _mgnrega_facts_joined(sql):
                raise ValueError(
                    "this query JOINs curated.v_employment to curated.v_expenditure "
                    "directly. MGNREGA's two facts have no shared grain — both hold many "
                    "rows per village-year — so that join fans out and every SUM comes back "
                    "multiplied (a real 986,020 person-days became 109,448,220). Rebuild it "
                    "with one CTE per fact, each aggregated on its own and carrying the SAME "
                    "filters, then combine the aggregates:\n"
                    "  WITH emp AS (SELECT SUM(person_days) AS person_days "
                    "FROM curated.v_employment WHERE <filters>),\n"
                    "       exp AS (SELECT SUM(total_exp) AS total_exp_lakh "
                    "FROM curated.v_expenditure WHERE <filters>)\n"
                    "  SELECT exp.total_exp_lakh, emp.person_days FROM emp, exp\n"
                    "Add GROUP BY inside each CTE and FULL OUTER JOIN them on the grain "
                    "columns only if a per-area or per-year breakdown was asked for. Keep "
                    "every filter exactly as it was."
                )
            bad_geo = _ac_with_invented_geo_filter(entity_result, sql)
            if bad_geo is not None:
                raise ValueError(
                    f"this query filters on the assembly constituency AND on {bad_geo}, but "
                    "no district or block was resolved for this question — that geography "
                    "filter was invented. An assembly constituency cuts ACROSS blocks and "
                    "districts, so its name is usually not a block or district name, and "
                    f"ANDing {bad_geo} with the constituency filter matches zero rows and "
                    "returns a false zero. Remove that filter entirely and keep only "
                    "assembly_constituency_name (plus year_key and the aggregation) exactly "
                    "as they were."
                )
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
            # Last — the free regex guards above catch known bug shapes without
            # spending a model call; only a query that clears all of them goes to
            # the semantic verifier, which is the paid check.
            verify_issue = await _verify_sql(question, schemes, entity_result, sql)
            if verify_issue:
                raise ValueError(
                    f"semantic verifier flagged this query: {verify_issue}. Fix the SQL to "
                    "address that specific problem and keep every other clause (filters, "
                    "year_key, geography, aggregation) that is not implicated."
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
    r"do(?:es)?n['’]?t\s+cover|does\s+not\s+cover(?:ed)?|not\s+cover(?:ed)?|isn['’]?t\s+covered|"
    r"not\s+available|no\s+data\b|"
    r"(?:doesn['’]?t|does\s+not|don['’]?t|do\s+not)\s+(?:have|include|contain|provide)|"
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
        row = rows[0]
        nums = _row_metrics(row)
        # A single-row GROUP BY result (e.g. a category breakdown that happens
        # to have exactly one value present, like verification_status =
        # 'Approved' on every 12.5K row) still carries the category as a
        # non-numeric column. Drop the number alone and the answer misreports
        # a labelled breakdown as an unlabelled total.
        labels = [str(v) for k, v in row.items() if v is not None and _as_number(v) is None]
        prefix = ", ".join(labels) + ": " if labels else ""
        if len(nums) == 1:
            k, v = nums[0]
            return f"{prefix}{_fmt_num(v)} {k.replace('_', ' ')}."
        if len(nums) >= 2:
            return prefix + "; ".join(f"{k.replace('_', ' ')}: {_fmt_num(v)}" for k, v in nums) + "."
    # Multi-row: one labelled line per row. This used to be a raw
    # "Results — col: val; col: val" dump of the first 5 rows only, which read as
    # debug output and silently dropped the rest (a 12-district summary showed 5
    # districts — CM Elevate Legacy use-case QA TC-25 / TC-36, 2026-09-25).
    shown = rows[:_DETERMINISTIC_MAX_ROWS]
    lines = []
    for r in shown:
        labels = [str(v) for v in r.values() if v is not None and _as_number(v) is None]
        metrics = ", ".join(f"{_metric_label(k)}: {_fmt_num(v)}" for k, v in _row_metrics(r))
        head = " / ".join(labels) or "(no label)"
        lines.append(f"- {head} — {metrics}" if metrics else f"- {head}")
    more = len(rows) - len(shown)
    tail = f"\n…and {more} more row{'s' if more != 1 else ''} in the table." if more > 0 else ""
    return f"Here are the {len(rows)} results:\n" + "\n".join(lines) + tail


_DETERMINISTIC_MAX_ROWS = 15


def _metric_label(col: str) -> str:
    """A readable name for a result column: total_disbursed_cr -> 'total disbursed
    (₹ crore)'. The unit suffixes are the SQL-prompt conventions (_cr, _lakh,
    _pct, _rupees)."""
    for suffix, unit in (("_cr", " (₹ crore)"), ("_lakh", " (₹ lakh)"),
                         ("_pct", " (%)"), ("_rupees", " (₹)")):
        if col.endswith(suffix):
            return col[: -len(suffix)].replace("_", " ") + unit
    return col.replace("_", " ")


def _no_data_answer(schemes: list[str] | None, entities: dict[str, str] | None) -> str:
    """Plain 'nothing matched' message for a query that returned no rows at all.
    Built deterministically rather than left to the composer — on an empty
    result it sometimes free-forms RAG-style refusal wording ("the reference
    material does not contain...") that reads like an internal document search
    failed, when the honest answer is just that no records match the filters."""
    scope_bits = [v for v in (entities or {}).values() if v]
    scope = f" for {', '.join(scope_bits)}" if scope_bits else ""
    # One sentence, nothing else. The full capability catalogue used to be
    # appended here ("Data I do have here:" + every metric for the scheme),
    # which buried a one-line fact under a ~10-line dump the user did not ask
    # for and could not act on (reported 2026-09-18). An empty result answers
    # the question asked; what else the dataset could report is a different
    # question, and the NEXT STEPS chips already offer it.
    return f"I couldn't find any matching records{scope} in the data available."


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
    "cm_scheme": "CM Elevate sub-scheme",
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
                           schemes: list[str] | None = None,
                           style_examples: str = "",
                           extra_numbers: "set[str] | None" = None) -> str:
    # extra_numbers: figures a caller re-queried and handed over in a note (a
    # list's true total), which the answer may quote although no row holds
    # them. None for every caller that doesn't pass it — unchanged behaviour.
    # style_examples: optional worked answers for the scheme (CM Elevate
    # Legacy's answer shots). Empty for every other scheme, which leaves the
    # prompt exactly as it was.
    if not rows:
        return _no_data_answer(schemes, entities)
    preview = rows[:40]
    truncated = len(rows) > len(preview)
    # "No usable value" — every numeric cell is 0 or null (rows is non-empty
    # here; a truly empty result returns via _no_data_answer above). This is
    # the shape a metric the data simply doesn't track comes back as; when we
    # see it, hand the composer the real metric list so it can tell the user
    # exactly what IS available rather than a vague "not covered".
    _nums = [n for r in rows for _k, n in _row_metrics(r)]
    # An aggregate that matched NOTHING comes back as one row of NULLs, not as
    # zero rows, so the `if not rows` guard above cannot catch it. _row_metrics
    # drops NULL cells (they are not numbers), which left _nums empty and this
    # test False — the composer then received a row whose only value was None
    # and reported "the membership count is null", as if the database held a
    # null for that group rather than nothing having matched the filters
    # (reported 2026-09-22, "members in Sakania Producer Group"). Detect the
    # all-NULL row explicitly.
    _all_null = bool(rows) and not _nums and all(
        v is None for r in rows for v in r.values()
    )
    no_usable_value = (bool(_nums) and all(n in (0, None) for n in _nums)) or _all_null
    metrics_block = ""
    if no_usable_value:
        metrics_block = (
            "\nMetrics the data DOES carry — if the question asked for something "
            "that is not in this list, say plainly it isn't tracked in the "
            "available scheme data, then name what is:\n"
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
        "user's assumed number as if it were right. Make that first sentence ONE "
        "clean clause naming the real figure (e.g. 'Tranche 1 shows 0 beneficiaries, "
        "not the 4 the question assumes.') — never a reasoning trail like 'contradicting "
        "the assumed figure of X, so Y cannot be calculated because Z is empty'. Put "
        "the supporting figures in a separate, plainly listed second sentence (e.g. "
        "'Tranches 2, 3 and 4 each had 93,286 beneficiaries, out of 373,144 total.') "
        "rather than chaining them onto the correction with 'The available figures "
        "are...'. If the result is a list of "
        "names or rows and carries no numeric column, that list IS the answer — "
        "it is the exact set the query already selected as matching the question "
        "(e.g. 'which districts have both schemes'). Name those values as the "
        "answer; never say the data 'only lists names' or lacks the detail to "
        "decide — the filtering happened in the query. Never state or imply a "
        "scope the query wasn't actually filtered to — a specific tranche, year, "
        "district, block, village or category — unless it appears in the Entity "
        "names block below or literally in the question; if the question and the "
        "Entity names block name no tranche/year/area, the result covers all of "
        "them and must be described that way (e.g. 'across all tranches'), not "
        "attributed to one you're not told about."
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
differently than shown. Write like a direct briefing: lead with the fact or figure
itself, not with "The data shows..." or a description of what the query returned.
Keep each sentence to one idea — short and declarative, not a chain of clauses
joined by "so" / "as" / "which means".
{guidance}
{metrics_block}{names_block}{summary_block}{notes_block}{style_examples}
Question: "{question}"
Result ({len(rows)} row(s), showing up to {len(preview)}):
{json.dumps(preview, default=str)}

Answer:"""
    answer = await llm.call_response_composer(prompt)

    data_nums = _data_numbers(preview) | digest_nums | set(extra_numbers or ())
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
        elif len(preview) >= 2 and any(
            v not in (0, None) for r in preview for _k, v in _row_metrics(r)
        ):
            # A multi-row breakdown (e.g. GROUP BY category, COUNT(*)) that
            # carries at least one real, non-zero metric — neither of the two
            # branches above catches this shape (not a single row, and
            # _is_plain_list_result excludes rows with numeric metrics), so a
            # composer hedge here used to slip through unchecked while the
            # chart/table built from the same rows showed real data.
            logger.warning("compose_response: hedged over a %d-row breakdown %r — "
                           "deterministic answer", len(preview), answer[:160])
            answer = _deterministic_answer(preview)
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
    # "How many members are there in Bak-15 Wachal Pg?" is a lookup in the data;
    # with no counting noun the classifier sometimes sent it to the reference
    # docs ("the reference material does not contain…").
    if _pg_name_question(question):
        return "DATA"
    if _PROGRAMME_DESIGN_CUE.search(question):
        return "KNOWLEDGE"
    if _BREAKDOWN_CUE.search(question):
        return "DATA"
    if _METRIC_WHATIS_CUE.search(question):
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
                       skip_scope_clarify: bool = False,
                       prior_resolved: "dict | None" = None,
                       village_hint: "str | None" = None) -> dict:
    # Named a scheme we don't hold ("CM Elevate", "PM-KISAN", …) — say so plainly
    # instead of the generic "which scheme?" prompt, then offer the three we have.
    _unsupported = _unsupported_scheme_named(question)
    if _unsupported:
        raise _unsupported_scheme_clarification(question, _unsupported)

    # Ask which scheme before doing anything expensive, when the question could
    # honestly mean either one. Guessing here is worse than a one-tap follow-up.
    # "Which scheme paid out the most?" — the scheme is the ANSWER, not a
    # missing filter, so this must run before the "which scheme?" pause below
    # (which would otherwise ask the user to supply the very thing they asked
    # for). Answered deterministically across every scheme that records money.
    if _wants_cross_scheme_money_ranking(question):
        return await _cross_scheme_money_answer(question)

    # "Focus" with nothing to say WHICH Focus — a two-way ask that keeps what the
    # user already told us, rather than the generic five-way pause below.
    if _is_ambiguous_focus(question):
        raise _focus_ambiguity_clarification(question)

    if _needs_scheme_clarification(question):
        raise _scheme_clarification(question)

    # Ask "top how many?" before spending model calls when the question wants a
    # ranked list over a dimension but never says how long.
    if _needs_topn_clarification(question):
        raise _topn_clarification(question)

    schemes = await classify_scheme(question)
    # Only Focus Legacy holds producer groups and their members. A group name can
    # contain another scheme's word — "How many members are there in Chisam
    # Piggery?" was classified CM Elevate (its Piggery sub-scheme) and answered
    # "3010 members" of the Piggery scheme. A group-name question that names no
    # scheme of its own is a Focus Legacy question.
    if schemes != ["Focus Legacy"] and _pg_name_question(question) and not _mentions_scheme(question):
        logger.info("group-name question %r -> Focus Legacy (was %s)", question, schemes)
        schemes = ["Focus Legacy"]
    # Focus Legacy group-name questions are answered deterministically (see
    # _focus_legacy_pg_name_answer) before any geography resolution, which would
    # otherwise read a group name like "Nongstoin PG" as a place.
    if schemes == ["Focus Legacy"]:
        _pg_answer = await _focus_legacy_pg_name_answer(question)
        if _pg_answer is not None:
            return _pg_answer
    # CM Elevate Legacy: a question its data cannot answer (a sanction rate,
    # applicant names, constituency, monthly figures, ...) gets the reviewed
    # not-held explanation now, before any model call.
    if schemes == ["CM Elevate Legacy"]:
        _not_held = _cm_legacy_not_held(question)
        if _not_held is not None:
            raise _not_held
    # raises ClarificationNeeded if ambiguous
    entity_result = await resolve_entities(question, schemes, prior_resolved=prior_resolved,
                                            village_hint=village_hint)
    # A year-gap rewrite (see resolve_entities) replaces the question for
    # everything downstream — SQL generation above all.
    if entity_result.get("question"):
        question = entity_result["question"]

    # "Garo Hills" / "Khasi Hills" name a hill RANGE, not a district. When one is
    # named and no specific district resolved, either offer its districts as one-tap
    # chips ("which Garo Hills district?") or, if the question already says "all of
    # Garo Hills" or asks for a per-district breakdown, expand it to every district in
    # the range and carry that forward for SQL generation.
    if not entity_result["resolved"].get("district") and not entity_result["resolved"].get("district_list"):
        _scheme0 = schemes[0] if schemes else ""
        region = detect_region(question, _scheme0)
        if region:
            _want_all = region["expand"] or bool(_HAS_BREAKDOWN.search(question))
            if not _want_all and not skip_scope_clarify:
                raise _region_clarification(question, region)
            entity_result["resolved"]["district_list"] = [d.upper() for d in region["districts"]]
            entity_result["resolved"]["district_list_region"] = region["canonical"]
            entity_result["display"]["district"] = region["canonical"]

    # "Give me an overall Focus+ data summary" — a genuinely whole-scheme
    # question with no district/year/tranche to pin. Handled before the scope/
    # year/tranche clarification gates below so it never gets mistaken for an
    # aggregate question that merely forgot to name a scope.
    if _focusplus_wants_overall_summary(question, schemes):
        return await _focusplus_overall_summary_answer(schemes, entity_result)

    # Ask which district / block / village / year when an aggregate question
    # pins none of them — unless we're already resuming that very clarification
    # (a reply that still names no scope must not loop us back here).
    if not skip_scope_clarify and _needs_scope_clarification(question, entity_result["resolved"]):
        raise _scope_clarification(question, schemes)

    # Geography is settled but the year isn't: ask which financial year (one-tap
    # chips) rather than silently answering across every year. The chips resume
    # with a concrete year or "all financial years", so this can't loop.
    if _needs_year_clarification(question, schemes, entity_result["resolved"]):
        raise _year_clarification(question, schemes)

    # Focus Plus only: year is settled but the tranche isn't — ask which one
    # (one-tap chips) rather than silently combining every tranche's payments.
    # `already_all_combined` lets a session that already answered this once
    # ("all tranches combined") skip a redundant re-ask on a later bare
    # follow-up that doesn't restate "tranche" itself.
    if _needs_tranche_clarification(
            question, schemes, entity_result["resolved"],
            already_all_combined=bool((prior_resolved or {}).get("tranche_all_combined"))):
        raise _tranche_clarification(question)

    # A specific non-Tranch-4 tranche plus a person-level column (status/
    # gender/occupation/verification) can never match any row — that data
    # exists only on the 12.5K cohort, which is entirely Tranch 4. Explain why
    # instead of running SQL that is guaranteed to come back empty.
    if _person_level_tranche_conflict(question, schemes, entity_result["resolved"]):
        return _person_level_tranche_conflict_answer(question, schemes, entity_result)

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
    notes.extend(_genuine_zero_notes(sql))
    notes.extend(_sector_not_tracked_notes(rows))
    _style = ""
    if schemes == ["CM Elevate Legacy"]:
        notes.extend(_cm_legacy_answer_notes(sql, rows))
        notes.extend(_cm_legacy_small_money_notes(rows))
        notes.extend(await _cm_legacy_exact_totals(sql, rows))
        _style = _cm_legacy_style_block(question)
    _fl_total = None
    if schemes == ["Focus Legacy"]:
        notes.extend(_focus_legacy_answer_notes(question))
        _fl_total = await _focus_legacy_list_total(sql, rows)
        if _fl_total:
            notes.append(f"{_fl_total[0]:,} {_fl_total[1]} match in total; the result lists only the "
                         f"first {len(rows)}. Say that {_fl_total[0]:,} {_fl_total[1]} match, then name "
                         "the top ones — never present the list as complete.")
    if settings.PREMISE_CHECK_ENABLED:
        try:
            notes.extend(premise_check.check_premises(question, rows))
        except Exception:  # noqa: BLE001
            logger.warning("premise check failed — continuing without it", exc_info=True)

    answer = await compose_response(question, sql, rows, notes=notes,
                                    entities=entity_result.get("display"),
                                    schemes=schemes, style_examples=_style,
                                    extra_numbers={str(_fl_total[0])} if _fl_total else None)
    if _fl_total and not re.search(rf"\b{_fl_total[0]:,}\b|\b{_fl_total[0]}\b",
                                   answer.split("\n", 1)[0]):
        # Deterministic guarantee: the composer (or its row-by-row fallback)
        # did not lead with the true count, so the list would read as complete.
        answer = (f"{_fl_total[0]:,} {_fl_total[1]} match in total; the top {len(rows)} are "
                  f"shown below.\n\n{answer}")
    # Year-gap guarantee: the question named a year the scheme does not hold and
    # _apply_year_gap answered for another. The note says so, but the composer
    # dropped it (TC-24: "Compare FY 2023-24 and FY 2024-25" answered with
    # 2022-23 and 2024-25 and no word about 2023-24). Lead with the note.
    _gap_note = next((n for n in (entity_result.get("notes") or []) if "holds no data" in n), None)
    if entity_result.get("question") and _gap_note:
        _absent = re.search(r"\bFY\s*(\d{4}-\d{2})", _gap_note)
        if _absent and _absent.group(1) not in answer:
            answer = f"{_gap_note}\n\n{answer}"
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
        # The full result the query returned, for the table/chart renderer.
        # Was capped at 200, which silently truncated a legitimate answer — a
        # "which villages…" question over a constituency returns ~150 rows and
        # the user has no way to reach the rest (reported 2026-09-17).
        # run_readonly() already bounds every query at SQL_MAX_RESULT_ROWS
        # (1000), so this is not an unbounded payload; `row_count` above stays
        # the true total either way.
        "data": rows,
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
    # Context Updater — fold this turn into the session's structured state and
    # refresh the rolling summary (both best-effort; see context_manager).
    # Skipped for a clarification pause: nothing was actually answered yet,
    # and _run_pipeline never reaches here for one anyway (it raises).
    if settings.CONTEXT_LAYER_ENABLED and session is not None:
        try:
            context_manager.update_state(
                session, raw_question, result.get("rewritten_question") or raw_question, result)
            await context_manager.maybe_update_summary(session)
        except Exception:  # noqa: BLE001 — the context layer must never break an answer
            logger.warning("context layer post-processing failed (non-fatal)", exc_info=True)
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
    question = _correct_scheme_spelling(question)
    # "total amount disbursed under CM Elevate" can only be answered by CM
    # Elevate Legacy — settle which CM Elevate dataset is meant, once, here.
    # A DATA decision only: the KNOWLEDGE route undoes it (see _unpin_cm_elevate).
    _pinned = _pin_cm_elevate_dataset(question)
    _cm_pinned = _pinned != question
    question = _pinned
    scope_resumed = False

    # Computed early (moved ahead of the original follow-up step) so the step-0
    # edge check below can relax its whitelist gate for a plausible follow-up —
    # see edge.detect_edge_case's has_context param.
    prev = session.last_turn if session is not None else None
    has_antecedent = prev is not None and prev.route in _ANTECEDENT_ROUTES
    ctx_state = (session.state if (session is not None and settings.CONTEXT_LAYER_ENABLED
                                   and settings.CONTEXT_STATE_ENABLED) else None)

    # 0a. Resuming a "which area / year?" pause — fold the user's free-text reply
    #     ("West Garo Hills 2023-24", "all of Meghalaya, all years") back into the
    #     question that triggered it. A reply that stands on its own as a fresh
    #     question, or an edge case like "thanks", is left alone; either way the
    #     pending state is consumed so it never leaks into a later turn.
    pending = getattr(session, "pending_scope_q", None) if session is not None else None
    _village_hint = getattr(session, "pending_village_hint", None) if session is not None else None
    if pending:
        session.pending_scope_q = None
        session.pending_village_hint = None
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

    # 0--. A request for help with something illegal ("i want to rob a bank,
    #      give me suggestions") is refused FIRST — before the recommendation /
    #      pick / listing steps below, any of which could otherwise claim it on a
    #      word like "suggestions" and answer with an unrelated scheme.
    _harm = edge.detect_harmful(question)
    if _harm:
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": _harm["response"], "edge_type": _harm["type"],
                "suggestions": [], **_empty_data_fields()}

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

    # 0-a. "What schemes are available?" / "what's the difference between the
    #      schemes?" — answered directly (see _scheme_listing_answer /
    #      _scheme_comparison_answer) rather than falling through to RAG, which
    #      has no single document covering either and either over-elaborates on
    #      one scheme or says "not covered". Same scope-pause carve-out as above.
    if not scope_resumed:
        # "Pick any scheme and explain it" — checked FIRST: it is neither a
        # listing nor a follow-up, and must not reach the "which scheme?" pause
        # or the follow-up rewrite (which re-reads it against the last scheme).
        # "Why did you choose MGNREGA?" / "suggest a scheme that suits me" — a
        # question about the bot's own choice, and a recommendation ACROSS
        # schemes. Neither is in any one scheme's documents, so both are
        # answered here, before the knowledge route narrows to one scheme.
        explained = _why_choice_answer(question, session)
        if explained:
            return explained
        recommended = (_scheme_fit_check_answer(question)
                       or _scheme_recommendation_answer(question, session))
        if recommended:
            return recommended
        picked = await _scheme_pick_answer(question, session)
        if picked:
            return picked
        direct = _scheme_listing_answer(question) or _scheme_comparison_answer(question)
        if direct:
            if question != raw_question:
                direct["rewritten_question"] = question
            return direct

    # 0. Edge — greetings, identity, thanks, off-topic, abuse. Checked on the RAW
    #    text first: the follow-up heuristic treats any short anchorless phrase as
    #    a fragment, which would otherwise turn "hello" into a bogus follow-up.
    #    Skipped when we just merged a scope-pause reply: the merged text is a
    #    bare "<question>, <place>, <year>" fragment that the whitelist gate
    #    would wrongly flag as off-topic, and the conversational-reply case was
    #    already handled at the merge above.
    hit = None if scope_resumed else edge.detect_edge_case(question, has_context=has_antecedent)
    if hit:
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": hit["response"], "edge_type": hit["type"],
                "suggestions": hit.get("suggestions", []),
                **_empty_data_fields()}

    # 0f. Deterministic reference resolution ("the previous year", "the
    #     current year", "the former/latter", "the other one", "both") against
    #     the session's structured conversation state — see
    #     context_manager.substitute_references. Runs before the follow-up
    #     detector so a bare "compare that with the previous year" already has
    #     a concrete year by the time it gets there. Ambiguous ("the other
    #     one" with no recorded comparison) reuses the existing clarification
    #     mechanism rather than guessing. Any other failure degrades to the
    #     question unchanged — the pre-existing follow-up path still runs.
    if not scope_resumed and ctx_state is not None:
        try:
            question = context_manager.substitute_references(question, ctx_state)
        except context_manager.AmbiguousReference as e:
            raise ClarificationNeeded(e.question, options=e.options, rule="entity-ambiguous")
        except Exception:  # noqa: BLE001
            logger.warning("context_manager.substitute_references failed — continuing unchanged",
                           exc_info=True)

    # 1. Follow-up — rewrite a fragment ("what about EGH?") to a standalone
    #    question using the previous turn, before routing. Only when the previous
    #    turn was an actual scheme answer; otherwise the fragment has nothing
    #    coherent to attach to. (prev / has_antecedent computed above, ahead of
    #    the step-0 edge check.)
    is_followup_rewrite = False
    if looks_like_followup(question):
        if has_antecedent:
            _extra_ctx = ""
            if settings.CONTEXT_LAYER_ENABLED and session is not None:
                try:
                    _extra_ctx = await context_manager.build_followup_context(session, question)
                except Exception:  # noqa: BLE001
                    logger.warning("context_manager.build_followup_context failed — continuing without it",
                                   exc_info=True)
            question = await rewrite_followup(question, prev, extra_context=_extra_ctx)
            # The rewrite can introduce a bare "CM Elevate" alongside a money word
            # ("...and the amount disbursed?") — same dataset decision as above.
            _pinned = _pin_cm_elevate_dataset(question)
            _cm_pinned = _pinned != question
            question = _pinned
            is_followup_rewrite = True
        elif _CONTEXTLESS_REF.search(question) and not _mentions_scheme(question):
            # "how launched it?" with no prior scheme answer — don't guess.
            return {"route": "edge", "intent": "EDGE", "confidence": "high",
                    "edge_type": "confused",
                    "answer": ("I don't have an earlier answer to build on, so I'm not "
                               "sure what that refers to. Tell me the scheme — MGNREGA, "
                               "PMAY-G, Focus Plus, CM Elevate, Focus Legacy, or CM Elevate "
                               "Legacy — and what "
                               "you'd like to know."),
                    "suggestions": list(edge.STARTERS),
                    **_empty_data_fields()}

    # 1b. Re-check edge on the rewrite (cheap, and the rewrite can surface one).
    #     Same scope-resume carve-out as step 0.
    hit = None if scope_resumed else edge.detect_edge_case(question, has_context=has_antecedent)
    if hit:
        return {"route": "edge", "intent": "EDGE", "confidence": "high",
                "answer": hit["response"], "edge_type": hit["type"],
                "suggestions": hit.get("suggestions", []),
                "rewritten_question": question if question != raw_question else None,
                **_empty_data_fields()}

    # 1f. A follow-up fragment that rewrote to a standalone question still
    #     naming no scheme (and no scheme-specific vocabulary of its own) gets
    #     the session's pinned scheme appended deterministically — see
    #     context_manager.inject_scheme_hint. Only for an actual continuation
    #     (is_followup_rewrite), never for a brand-new question: that case is
    #     deliberately left to the existing "which scheme?" pause below.
    if is_followup_rewrite and ctx_state is not None:
        try:
            question = context_manager.inject_scheme_hint(question, ctx_state)
        except Exception:  # noqa: BLE001
            logger.warning("context_manager.inject_scheme_hint failed — continuing unchanged",
                           exc_info=True)

    # 1c. Bank / financial-channel details are not held for any loaded scheme —
    #     say so, with the reason, before routing. Checked ahead of the DATA/
    #     KNOWLEDGE split: phrasing like "what is the bank-wise disbursement…"
    #     trips _KNOWLEDGE_HINTS on "what is" and would otherwise dead-end in
    #     RAG ("not covered in the reference material") instead of explaining
    #     that the column itself isn't queryable.
    if _BANK_REQUESTED.search(question):
        _clarification = _bank_clarification(question)
        if _clarification is not None:
            raise _clarification

    # 1d. Administrative expenditure is deliberately excluded from reporting for
    #     every scheme that has it (see _ADMIN_EXPENDITURE_REQUESTED above) —
    #     same reasoning and same place as the bank check just above.
    if _ADMIN_EXPENDITURE_REQUESTED.search(question):
        raise _admin_expenditure_clarification(question)

    # 1e. Named a scheme we simply don't hold ("PM-KISAN", "Ujjwala", "Jal
    #     Jeevan"). Checked HERE, before the DATA/KNOWLEDGE split, because the
    #     question is just as often a KNOWLEDGE one ("what is PM Kisan
    #     Yojana?") as a data one. The check used to live only inside
    #     _answer_data, so a knowledge-shaped ask sailed past it into RAG,
    #     found nothing — correctly, it isn't in the reference docs — and got
    #     the flat "I don't have information about that for MGNREGA, PMAY-G,
    #     Focus Plus or CM Elevate", which never says WHY or what to do next
    #     (reported 2026-09-18). The clarification below names the scheme the
    #     user asked for, says plainly that only four are loaded, and offers
    #     them as one-tap chips.
    _unsupported_named = _unsupported_scheme_named(question)
    if _unsupported_named:
        raise _unsupported_scheme_clarification(question, _unsupported_named)

    # 2. Route: number question or scheme-rules question?
    intent = await classify_intent(question)

    # 3. KNOWLEDGE -> RAG over the scheme reference docs.
    if intent == "KNOWLEDGE":
        # Both CM Elevate datasets share one knowledge base (rag.kb_scheme), so
        # the data-side "which CM Elevate?" pin means nothing here — give the
        # question back in the user's own words rather than showing a rewrite.
        if _cm_pinned:
            question = _unpin_cm_elevate(question)
        # Scope retrieval to a single scheme when we're confident which one this
        # is about — named outright, or (for a follow-up with nothing named of
        # its own) the scheme the previous turn was about. Without this, vector
        # search has no scheme filter at all and can blend in another scheme's
        # content (e.g. PMAY-Urban passages into a PMAY-G-scoped answer).
        _kb_scheme = None
        _named = _named_schemes(question)
        if len(_named) == 1:
            _kb_scheme = _named[0]
        else:
            # Not named outright — but scheme-specific VOCABULARY pins it just
            # as reliably, and this is the same signal the DATA path has always
            # used (_infer_scheme_from_terms returns a scheme only when exactly
            # one scheme's vocabulary matches). Without it, "What is the role of
            # Producer Groups under FOCUS?" searched the whole KB unfiltered:
            # measured on the real corpus, 3 of the top 8 chunks came back from
            # Focus Plus — which holds no producer-group data at all — and the
            # single best hit was one of them, so the composer answered the
            # wrong scheme's question.
            _inferred = _infer_scheme_from_terms(question)
            if _inferred and len(_inferred) == 1:
                _kb_scheme = _inferred[0]
            # Not when the question says a bare "Focus": that names a scheme —
            # just not WHICH of the two — so inheriting the previous turn's
            # scheme would answer the wrong one ("for focus" after a CM Elevate
            # answer re-answered CM Elevate). It goes to the "which Focus?"
            # pause below instead.
            elif (prev is not None and len(prev.schemes or []) == 1
                  and not _is_ambiguous_focus(question)):
                _kb_scheme = prev.schemes[0]

        # Genuinely scheme-agnostic ("tell me about the scheme", "how do I
        # apply", "what are the benefits") — no scheme named, no follow-up
        # antecedent, and no vocabulary that pins it to one. Guessing here (or
        # letting bare vector search pick whichever doc scores highest) is how
        # a vague question came back "not covered" while quietly assuming
        # MGNREGA. Ask which of the four schemes instead, same one-tap chips
        # the DATA path already uses (see _needs_scheme_clarification).
        # ...but a question that NAMES a scheme we don't recognise ("what is
        # amma yedi scheme?") is not vague — the user was specific, we simply
        # don't hold it. Asking "which scheme does your question concern?"
        # there ignores what they actually asked; say plainly that it isn't
        # one of the four (reported 2026-09-18).
        if _kb_scheme is None and _names_unknown_scheme(question):
            return {"route": "knowledge", "intent": "RAG", "confidence": "low", "sources": [],
                    "answer": _knowledge_not_covered_answer(question, None),
                    "rewritten_question": question if question != raw_question else None,
                    "schemes": [],
                    **_empty_data_fields()}
        if _kb_scheme is None and _is_ambiguous_focus(question):
            raise _focus_ambiguity_clarification(question)
        if _kb_scheme is None and _needs_scheme_clarification(question):
            raise _scheme_clarification(question)

        # Two or more schemes named outright ("how do I apply across MGNREGA,
        # PMAY-G, Focus Plus and CM Elevate") — retrieve each scheme's chunks
        # separately (see rag.answer_from_kb_multi) instead of one unscoped
        # search, which otherwise lets one scheme's passages crowd out another's.
        if len(_named) > 1:
            kb = await rag.answer_from_kb_multi(question, schemes=_named)
            base = {"rewritten_question": question if question != raw_question else None,
                    "schemes": _named}
        else:
            # "Give me a short overview of Focus Legacy for an official briefing":
            # the words "official"/"briefing" pulled the where-to-find-official-info
            # and institutional-structure chunks, so the answer was a list of
            # departments with no benefit amount, objective or scale (Focus Legacy
            # QA TC-09, 2026-09-25). Retrieve with the scheme-overview phrasing
            # instead; the answer is still written to the user's own question.
            _retrieval_q = (_scheme_overview_question(_kb_scheme)
                            if _kb_scheme == "Focus Legacy" and _OVERVIEW_REQUEST.search(question)
                            else None)
            if _retrieval_q:
                kb = await rag.answer_from_kb(question, scheme=_kb_scheme, retrieval_query=_retrieval_q)
            else:
                kb = await rag.answer_from_kb(question, scheme=_kb_scheme)
            base = {"rewritten_question": question if question != raw_question else None,
                    "schemes": [_kb_scheme] if _kb_scheme else []}
        if kb:
            return {"route": "knowledge", "intent": "RAG", "confidence": kb["confidence"],
                    "answer": kb["answer"], "sources": kb["sources"],
                    **_empty_data_fields(), **base}
        return {"route": "knowledge", "intent": "RAG", "confidence": "low", "sources": [],
                "answer": _knowledge_not_covered_answer(question, _kb_scheme),
                **_empty_data_fields(), **base}

    # 4. DATA -> NL->SQL. On a hard failure, try the KB once before giving up.
    # A rewritten follow-up's entities are seeded with the previous turn's
    # resolved district/block/village/year as a fallback (see resolve_entities'
    # `prior_resolved` param) — the LLM rewrite works from prev.question/answer
    # TEXT, so an entity can silently drop if it doesn't literally reappear in
    # the rewritten wording (e.g. a village name the rewrite paraphrases away).
    _prior_resolved = prev.resolved_entities if (is_followup_rewrite and prev is not None) else None
    if is_followup_rewrite and ctx_state is not None:
        # Extends the fallback above with session-level structured state, so a
        # KNOWLEDGE/EDGE turn sitting between the last DATA answer and this
        # follow-up (which leaves resolved_entities empty — see
        # context_manager.update_state) doesn't erase district/block/village/
        # year a later "and in 2023-24?" still needs. Turn-level prev values
        # still win where both are present.
        try:
            _prior_resolved = context_manager.merged_prior_resolved(_prior_resolved, ctx_state)
        except Exception:  # noqa: BLE001
            logger.warning("context_manager.merged_prior_resolved failed — using turn-level only",
                           exc_info=True)
    try:
        result = await _answer_data(question, scope=scope, skip_scope_clarify=scope_resumed,
                                     prior_resolved=_prior_resolved,
                                     village_hint=_village_hint if scope_resumed else None)
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


# "what is <something> scheme/yojana/mission?" — the user named a specific
# programme by name. When it is none of our four and not in the known
# unsupported catalogue either, it is still a NAMED ask, not a vague one, so it
# must not get the "which scheme does your question concern?" pause.
_NAMED_UNKNOWN_SCHEME_RE = re.compile(
    r"\b(?:what|which|tell me about|explain|describe|about)\b[^?.!]{0,60}?"
    r"\b(?P<name>[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,3})\s+"
    r"(?:scheme|yojana|yojna|mission|abhiyaan?|programme|program)\b",
    re.IGNORECASE,
)
# Words that make the phrase generic rather than a name ("what is THIS scheme",
# "about the scheme"), so they must not count as naming one.
_GENERIC_SCHEME_WORDS = {
    "the", "this", "that", "a", "an", "any", "each", "every", "all", "these",
    "those", "your", "which", "what", "some", "other", "another", "such",
    "government", "govt", "state", "central", "rural", "welfare", "above",
    # Verbs / fillers the opener can leave in the captured span ("what IS the
    # scheme") — on their own they name nothing.
    "is", "are", "was", "were", "do", "does", "did", "me", "about", "of",
    "for", "in", "on", "it", "they", "you", "i", "we", "tell", "explain",
    # Generic nouns a scheme question asks ABOUT, never the scheme's name
    # ("what are the BENEFITS of the scheme").
    "benefit", "benefits", "eligibility", "criteria", "document", "documents",
    "purpose", "objective", "objectives", "feature", "features", "detail",
    "details", "rule", "rules", "process", "procedure", "amount", "subsidy",
}


def _names_unknown_scheme(question: str) -> bool:
    """True when the question names a specific scheme by name that is neither
    one of ours nor in the unsupported catalogue."""
    if _named_schemes(question) or _infer_scheme_from_terms(question):
        return False                       # one of ours — nothing unknown here
    # A bare "Focus" names one of OUR schemes — we just don't yet know which of
    # the two (see _is_ambiguous_focus). Without this, "what is the FOCUS
    # scheme?" matched the "<name> scheme" pattern below, found "focus" in
    # neither registry above, and was answered "that isn't a scheme I hold" —
    # for a scheme with a full reference doc and FAQ in the KB. It must fall
    # through to the which-Focus ask instead.
    if _is_ambiguous_focus(question):
        return False
    if _unsupported_scheme_named(question):
        return False                       # handled by its own, better reply
    m = _NAMED_UNKNOWN_SCHEME_RE.search(question or "")
    if not m:
        return False
    words = m.group("name").split()
    if not words:
        return False
    # The scheme's NAME is the word immediately before "scheme"/"yojana"/… —
    # "amma yedi scheme" names one, "the benefits of the scheme" does not.
    # Testing the last word alone (rather than the whole captured span) keeps
    # this from firing on any question that merely mentions a generic noun on
    # its way to the word "scheme".
    if words[-1].lower() in _GENERIC_SCHEME_WORDS:
        return False
    # A bare "<word> scheme" where that word is an ordinary English filler is
    # still generic; require something that reads like a proper name.
    return len(words[-1]) >= 3


def _knowledge_not_covered_answer(question: str, scheme: "str | None") -> str:
    """The reply when the knowledge base genuinely has nothing for a question.

    The old text — "I don't have information about that for MGNREGA, PMAY-G,
    Focus Plus or CM Elevate" — reads as a dead end: it never says whether the
    SUBJECT is out of scope or the assistant simply failed, and offers nowhere
    to go (reported 2026-09-18, "what is PM kisan yojana?"). A question that
    named a scheme gets a scheme-scoped answer; one that named none is told
    what IS covered."""
    if scheme:
        # Name the reference material actually searched — CM Elevate Legacy has
        # none of its own, it reads CM Elevate's (rag.kb_scheme). Identity for
        # every other scheme.
        return (
            f"I don't have that detail in the {rag.kb_scheme(scheme)} reference material. I can "
            f"cover {scheme}'s eligibility, benefits, documents and how to apply, "
            "and its data by district, block, village or financial year — so it may "
            "just be worth rephrasing. If you meant a different scheme, tell me which."
        )
    return (
        "That isn't something I hold. I cover five Meghalaya schemes — MGNREGA "
        "(rural employment), PMAY-G (rural housing), Focus Plus (farmer cash "
        "benefit), CM Elevate (livelihood and enterprise support — its applications, "
        "and as CM Elevate Legacy its sanctions and disbursements) and Focus Legacy "
        "(producer group payments) — both how "
        "each one works and its actual data. If your question is about one of "
        "those, name it and I'll answer; if it's about another scheme or another "
        "state, that's outside what I can see."
    )


async def _data_path_kb_fallback(question: str) -> dict:
    """The DATA path genuinely couldn't produce a query (bad/uncoverable question,
    not an infra blip) — try the knowledge base once, then give the 'rephrase it'
    reply. Transient gateway errors are handled by the caller and never reach here."""
    kb = await rag.answer_from_kb(question)
    if kb:
        return {"route": "knowledge", "intent": "RAG", "confidence": kb["confidence"],
                "answer": kb["answer"], "sources": kb["sources"], **_empty_data_fields()}
    # Name the scheme(s) actually in play, not a hardcoded pair — this message used
    # to always say "MGNREGA / PMAY-G data" even for a Focus Plus / CM Elevate
    # question, which reads as if the conversation's own context had been dropped
    # (it hadn't — this text just never grew past the original two-scheme build).
    _fallback_schemes = _named_schemes(question) or _infer_scheme_from_terms(question)
    _schemes_text = " / ".join(_fallback_schemes) if _fallback_schemes else " / ".join(SCHEME_CATALOG)
    return {"route": "data", "intent": "DATA", "confidence": "low",
            "answer": ("I understood the question but couldn't build a working query for it "
                       f"against the current {_schemes_text} data. Try rephrasing it, or ask "
                       "for a simpler breakdown first (e.g. \"PMAY-G sanctions by district "
                       "for 2023\")."),
            **_empty_data_fields()}
