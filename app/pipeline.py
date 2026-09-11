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
import logging
import numbers
import re

import httpx
from rapidfuzz import fuzz

from app import auth, context_manager, edge, followups, llm, premise_check, prompt_builder, rag
from app.config import settings
from app.db import UnsafeSQLError, run_readonly
from app.entity_resolver import (
    all_districts,
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
    r"cm[\s-]?elevate|cmelevate)"
    r"\s*(?:instead|now|then|scheme)?\s*[?.!]*\s*$",
    re.IGNORECASE,
)


# A follow-up fragment ("and for 2024-25?", "how launched it?") only means
# something against a real scheme answer. Rewriting it against a greeting,
# off-topic reply, clarification pause or a denial produces a confident bogus
# query — e.g. "how launched it?" right after "what is elon musk?" was being
# turned into a data question. So the previous turn must be one of these.
_ANTECEDENT_ROUTES = ("data", "knowledge")
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
    if not target or not prev_question:
        return None
    out = prev_question
    replaced = False
    for name, pat in _SCHEME_NAME_PATTERN.items():
        if name == target:
            continue
        if pat.search(out):
            out = pat.sub(target, out)
            replaced = True
    if not replaced:
        if _SCHEME_NAME_PATTERN[target].search(out):
            return out  # previous question was already about the target scheme
        out = f"{out.rstrip(' ?.')} for {target}"
    out = re.sub(r"\s+,", ",", out).strip()
    logger.info("follow-up scheme-swap: %r + %r -> %r", prev_question, followup, out)
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
    "CM Elevate": re.compile(r"\bcm[\s-]?elevate\b|\bcmelevate\b", re.IGNORECASE),
}
# Fuzzy fallback for a scheme name the exact regex above misses because it's
# misspelled ("manrega", "pamay") — mirrors the RapidFuzz tolerance
# entity_resolver.py already gives district/block names. Kept separate from
# _SCHEME_NAME_PATTERN (exact match stays the fast, zero-false-positive path;
# this only runs when nothing named matches outright).
_SCHEME_FUZZY_ALIASES = {
    "MGNREGA": ["mgnrega", "mnrega", "nrega"],
    "PMAY-G": ["pmay", "pmayg", "awaas", "awas"],
    # "focus" alone, not just "focusplus" — a typo of the short form ("facus",
    # "focas", "fokus") is 5 chars against a 9-char target and never clears
    # the 80% ratio bar without it (fuzz.ratio("facus","focusplus") == 57 vs
    # fuzz.ratio("facus","focus") == 80), so "what facus+" fell through to the
    # generic capability blurb instead of routing to Focus Plus.
    "Focus Plus": ["focus", "focusplus"],
    "CM Elevate": ["cmelevate"],
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
    r"\bproducer group\b|\bproducer-group\b|\bproducer groups\b|"
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
    r"\bcm[\s-]?elevate\b|\bcmelevate\b|"
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


def _infer_scheme_from_terms(question: str) -> list[str] | None:
    """A single scheme implied by scheme-specific vocabulary, or None if the
    question could plausibly mean more than one."""
    hits = [
        s for s, rx in (
            ("MGNREGA", _MGNREGA_ONLY_TERMS),
            ("PMAY-G", _PMAY_ONLY_TERMS),
            ("Focus Plus", _FOCUSPLUS_ONLY_TERMS),
            ("CM Elevate", _CMELEVATE_ONLY_TERMS),
        ) if rx.search(question)
    ]
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
        {"label": "Compare across schemes",
         "question": f"{stem} across MGNREGA, PMAY-G, Focus Plus and CM Elevate"},
    ]
    return ClarificationNeeded(
        "Which scheme does your question concern — MGNREGA, PMAY-G, Focus Plus, or "
        "CM Elevate? Please select one, or choose to compare across schemes.",
        options=options,
        rule="scheme-not-specified",
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
    lines = [f"- **{name}** — {desc}" for name, desc in _SCHEME_USER_SUMMARY.items()]
    answer = (
        "I cover four Meghalaya government schemes:\n\n" + "\n".join(lines) +
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
    options.append({"label": "All four schemes",
                     "question": "difference between MGNREGA, PMAY-G, Focus Plus and CM Elevate"})
    return ClarificationNeeded(
        "Which schemes would you like to compare — MGNREGA, PMAY-G, Focus Plus, or "
        "CM Elevate? Pick a pair, or compare all four.",
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
        {"label": "Compare across schemes",
         "question": f"{stem} across MGNREGA, PMAY-G, Focus Plus and CM Elevate"},
    ]
    return ClarificationNeeded(
        f'I don\'t have any data for "{name.strip()}". Right now I only hold '
        "four schemes — MGNREGA, PMAY-G, Focus Plus and CM Elevate. Pick one of those "
        "and I'll answer.",
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
_BANK_NOT_HELD_TEXT = {
    "Focus Plus": (
        "Account numbers and IFSC codes aren't held for Focus Plus — only the "
        "bank name is. Shall I answer using bank name instead?"
    ),
    "CM Elevate": (
        "There is no loan-channel field for CM Elevate — Bank and LIFCOM are not "
        "recorded, and no payment of any kind is held for this scheme. Shall I "
        "answer on applications by scheme or district instead?"
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
    "Bank name is only held for Focus Plus — MGNREGA, PMAY-G and CM Elevate "
    "never captured a bank field at ingest, and none of the four schemes hold "
    "account numbers or IFSC codes. Shall I answer using Focus Plus bank name, "
    "or by scheme, district, block or village instead?"
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
_ADMIN_EXPENDITURE_NOT_HELD_TEXT = {
    "MGNREGA": (
        "Administrative expenditure is deliberately excluded from MGNREGA reporting "
        "here — only unskilled wage, semi-skilled wage, material and total expenditure "
        "are held. Did you mean total expenditure instead?"
    ),
    "PMAY-G": (
        "Administrative and programme expenditure are not held for PMAY-G — only the "
        "per-house sanctioned and released amounts are. Did you mean amount released "
        "instead?"
    ),
}
_ADMIN_EXPENDITURE_GENERIC_TEXT = (
    "Administrative expenditure isn't held for querying in any of the schemes I "
    "cover — MGNREGA reports only wage/material/total expenditure, and PMAY-G only "
    "sanctioned and released amounts. Shall I answer using one of those instead?"
)


def _admin_expenditure_clarification(question: str) -> "ClarificationNeeded":
    schemes = _named_or_inferred_schemes(question)
    if len(schemes) == 1 and schemes[0] in _ADMIN_EXPENDITURE_NOT_HELD_TEXT:
        text = _ADMIN_EXPENDITURE_NOT_HELD_TEXT[schemes[0]]
    else:
        text = _ADMIN_EXPENDITURE_GENERIC_TEXT
    return ClarificationNeeded(text, rule="column-not-held")


# ── "What % were women?" — women_employment_provided has no confirmed ratio ──
# schema_context.py's MGNREGA rule 6: women_employment_provided may be reported
# as a raw count, but NEVER as a computed ratio/percentage/share — its
# definition (persons vs households, and against which denominator) is
# unconfirmed, so a computed percentage risks a confidently wrong number. The
# SQL-gen prompt already carries this as a text rule, but a question that
# explicitly demands the percentage can still make the model try (and fail
# oddly) rather than explain why — so intercept it here, before SQL
# generation, the same way the bank / admin-expenditure checks do.
# PMAY-G's analogous "share of houses allotted to women" IS a well-defined,
# supported computation (house_alloted_to has an explicit Woman* value set) —
# excluded here via _WOMEN_SHARE_PMAYG_CONTEXT so this doesn't also block that.
_WOMEN_SHARE_REQUESTED = re.compile(
    r"\b(percentage|percent|%|share|ratio|proportion)\b[^?.!]{0,40}\bwomen\b|"
    r"\bwomen\b[^?.!]{0,40}\b(percentage|percent|%|share|ratio|proportion)\b",
    re.IGNORECASE,
)
_WOMEN_SHARE_PMAYG_CONTEXT = re.compile(
    r"\bhouses?\b|\bdwelling\b|\ballot\w*\b", re.IGNORECASE,
)
_WOMEN_SHARE_NOT_COMPUTABLE_TEXT = (
    "A reliable percentage or share can't be computed for women's employment — "
    "women_employment_provided's exact definition (which denominator it's a "
    "share of) isn't confirmed in the source data, so publishing a ratio risks "
    "a misleading number. I can give you the raw count of women provided "
    "employment instead — would that help?"
)


def _women_share_clarification(question: str) -> "ClarificationNeeded":
    return ClarificationNeeded(_WOMEN_SHARE_NOT_COMPUTABLE_TEXT, rule="column-not-held")


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
    r"instal{1,2}ments?|disbursements?|utili[sz]ation)\b",
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
    # A rank window over a geography dimension — "top 5 districts", "bottom
    # blocks", "10 largest villages". It inherently spans every area in that
    # dimension, so geography is already scoped; only the year is still open.
    r"\b(?:top|bottom|leading|largest|biggest|smallest|highest|lowest)\s+"
    r"(?:\d+\s+)?(?:districts?|blocks?|villages?|panchayats?|gps?)\b|"
    r"\b\d+\s+(?:largest|biggest|smallest|highest|lowest)\s+"
    r"(?:districts?|blocks?|villages?|panchayats?|gps?)\b|"
    # "which district received the highest ...", "district with the lowest
    # ..." — the same rank-window logic as "top N districts" above, just
    # phrased as "which <geo> ... <superlative>" instead of "<superlative>
    # <geo>". Still inherently spans every area in the dimension, so
    # geography is already scoped; only the year is still open. Anchored
    # loosely (superlative anywhere within ~40 chars either side of the
    # geography noun) so "which district received the highest total
    # disbursement" and "highest total expenditure in which district" both
    # match.
    r"\bwhich (?:district|block|village|panchayat|gp)\b[^?]{0,40}\b"
    r"(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b|"
    r"\b(?:highest|lowest|most|least|maximum|minimum|greatest|top|biggest|"
    r"largest|smallest)\b[^?]{0,40}\bwhich (?:district|block|village|panchayat|gp)\b|"
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
    if any(resolved.get(k) for k in
           ("district", "district_list", "block", "block_list", "village_code", "year_key")):
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
    r"\bfocus[\s-]?status\b|\bverification[\s-]?status\b|\bverified\b|\bverification\b|"
    r"\bgender\b|\bfemale\b|\bmale\b|\bwomen\b|\bmen\b|"
    r"\boccupation\b|\bfarmers?\b|"
    r"\bpending\b|\bapproved\b|\brejected\b|\bregistrations?\b",
    re.IGNORECASE,
)


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
    # Trigger on either signal: a money/count metric question with no tranche
    # named (mirrors the year gate), OR the question literally says
    # "tranche"/"tranch" without pinning which one — covers status/verification
    # breakdowns and any other phrasing _METRIC_OR_BREAKDOWN_CUE doesn't know.
    if not (_METRIC_OR_BREAKDOWN_CUE.search(q) or _MENTIONS_TRANCHE_WORD.search(q)):
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


def _clean_mention(value: str) -> str | None:
    """Normalise a raw mention; return None if it's a bare dimension word / the
    state name (i.e. not an actual place or period)."""
    v = value.strip().strip("\"'`.,?!()[]").strip()
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

A SCHEME name (MGNREGA, PMAY-G, Focus Plus, CM Elevate, or a close variant)
is NEVER a place — do not extract it as a district/block/village even when it
follows "for"/"of"/"under" exactly like a place would ("disbursement for
Focus Plus" names the scheme, not an area; extract nothing).

Examples:
Question: "Tell me about total disbursement of Selsella across all financial years for MGNREGA."
JSON: {{"block": "Selsella"}}
Question: "What is the total expenditure of West Garo Hills under PMAY-G?"
JSON: {{"district": "West Garo Hills"}}
Question: "how many job cards issued in Ri Bhoi"
JSON: {{"district": "Ri Bhoi"}}
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
                    display["village"] = _vr.display or str(_mdist).title()
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
        for _bname in mentions["blocks"]:
            r = resolve_dimension(_bname, schemes[0], "block")
            if r.status == "ambiguous":
                raise ClarificationNeeded(f"Which block is being referred to by “{_bname}”?",
                                           rule="entity-ambiguous")
            if r.status != "resolved":
                notes.append(f"'{_bname}' is not a known block — say so, do not filter on it.")
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
    elif mentions.get("block"):
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
            if not re.search(r"\bblock\b", question, re.IGNORECASE):
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
            resolved["block"] = r.canonical
            display["block"] = r.display or str(r.canonical).title()
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
                _vr = await resolve_village(mentions["block"], district=district_canon)
                if _vr.status == "ambiguous":
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
                        display["village"] = str(_pick["name"]).title()
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
                    display["village"] = _vr.display or str(mentions["block"]).title()
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
    _village_text = mentions.get("village") or village_hint
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
            display["village"] = r.display or str(_village_text).title()
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

    if prior_resolved:
        if not mentions.get("district") and "district" not in resolved and prior_resolved.get("district"):
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
                    "curated.v_district_year_summary, curated.v_focus_plus, "
                    "curated.v_cm_elevate, "
                    "curated.v_cross_scheme_money_district_year, curated.dim_geography",
    "lgd_block": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                 "curated.v_focus_plus, curated.v_cm_elevate, curated.dim_geography",
    "lgd_village_name": "curated.v_pmay, curated.v_employment, curated.v_expenditure, "
                        "curated.v_focus_plus, curated.v_cm_elevate, curated.dim_geography",
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
    return data.get("issue") or "the SQL verifier flagged this query as not answering the question"


async def execute_with_repair(question: str, schemes: list[str], entity_result: dict,
                              initial_sql: str | None = None, *,
                              max_repairs: int = 2) -> tuple[str, list[dict]]:
    sql = initial_sql if initial_sql is not None else await generate_sql(question, schemes, entity_result)
    for attempt in range(max_repairs + 1):
        sql = _uppercase_geo_literals(sql)
        try:
            bad_code = _village_name_filter_instead_of_code(entity_result, sql)
            if bad_code is not None:
                raise ValueError(
                    f"the question resolved to village_code = {bad_code} but this query filters on "
                    "lgd_village_name instead — village name spelling/case is not reliable for "
                    "matching (storage keeps mixed/title case, not upper-case), so that filter can "
                    "silently match zero rows. Replace the lgd_village_name filter with "
                    f"village_code = {bad_code} exactly, and keep every other clause as it was."
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
        nums = _row_metrics(rows[0])
        if len(nums) == 1:
            k, v = nums[0]
            return f"{_fmt_num(v)} {k.replace('_', ' ')}."
        if len(nums) >= 2:
            return "; ".join(f"{k.replace('_', ' ')}: {_fmt_num(v)}" for k, v in nums) + "."
    return "Results — " + "; ".join(
        ", ".join(f"{k}: {v}" for k, v in r.items()) for r in rows[:5]
    )


def _no_data_answer(schemes: list[str] | None, entities: dict[str, str] | None) -> str:
    """Plain 'nothing matched' message for a query that returned no rows at all.
    Built deterministically rather than left to the composer — on an empty
    result it sometimes free-forms RAG-style refusal wording ("the reference
    material does not contain...") that reads like an internal document search
    failed, when the honest answer is just that no records match the filters."""
    scope_bits = [v for v in (entities or {}).values() if v]
    scope = f" for {', '.join(scope_bits)}" if scope_bits else ""
    msg = f"I couldn't find any matching records{scope} in the data available."
    metrics = available_metrics_text(schemes or [])
    if metrics:
        msg += "\n\nData I do have here:\n" + metrics
    return msg


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
                           schemes: list[str] | None = None) -> str:
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
    no_usable_value = bool(_nums) and all(n in (0, None) for n in _nums)
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
    if _BREAKDOWN_CUE.search(question):
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
    if _needs_scheme_clarification(question):
        raise _scheme_clarification(question)

    # Ask "top how many?" before spending model calls when the question wants a
    # ranked list over a dimension but never says how long.
    if _needs_topn_clarification(question):
        raise _topn_clarification(question)

    schemes = await classify_scheme(question)
    # raises ClarificationNeeded if ambiguous
    entity_result = await resolve_entities(question, schemes, prior_resolved=prior_resolved,
                                            village_hint=village_hint)

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
            is_followup_rewrite = True
        elif _CONTEXTLESS_REF.search(question) and not _mentions_scheme(question):
            # "how launched it?" with no prior scheme answer — don't guess.
            return {"route": "edge", "intent": "EDGE", "confidence": "high",
                    "edge_type": "confused",
                    "answer": ("I don't have an earlier answer to build on, so I'm not "
                               "sure what that refers to. Tell me the scheme — MGNREGA, "
                               "PMAY-G, Focus Plus, or CM Elevate — and what you'd like to "
                               "know."),
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

    # 1e. "What percentage were women?" — no confirmed denominator to compute
    #     one from (see _WOMEN_SHARE_REQUESTED above). Same place, same reasoning.
    #     PMAY-G's house-allotment women's share is a different, well-defined
    #     computation — left alone.
    if _WOMEN_SHARE_REQUESTED.search(question) and not _WOMEN_SHARE_PMAYG_CONTEXT.search(question):
        raise _women_share_clarification(question)

    # 2. Route: number question or scheme-rules question?
    intent = await classify_intent(question)

    # 3. KNOWLEDGE -> RAG over the scheme reference docs.
    if intent == "KNOWLEDGE":
        # Scope retrieval to a single scheme when we're confident which one this
        # is about — named outright, or (for a follow-up with nothing named of
        # its own) the scheme the previous turn was about. Without this, vector
        # search has no scheme filter at all and can blend in another scheme's
        # content (e.g. PMAY-Urban passages into a PMAY-G-scoped answer).
        _kb_scheme = None
        _named = _named_schemes(question)
        if len(_named) == 1:
            _kb_scheme = _named[0]
        elif prev is not None and len(prev.schemes or []) == 1:
            _kb_scheme = prev.schemes[0]

        # Genuinely scheme-agnostic ("tell me about the scheme", "how do I
        # apply", "what are the benefits") — no scheme named, no follow-up
        # antecedent, and no vocabulary that pins it to one. Guessing here (or
        # letting bare vector search pick whichever doc scores highest) is how
        # a vague question came back "not covered" while quietly assuming
        # MGNREGA. Ask which of the four schemes instead, same one-tap chips
        # the DATA path already uses (see _needs_scheme_clarification).
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
            kb = await rag.answer_from_kb(question, scheme=_kb_scheme)
            base = {"rewritten_question": question if question != raw_question else None,
                    "schemes": [_kb_scheme] if _kb_scheme else []}
        if kb:
            return {"route": "knowledge", "intent": "RAG", "confidence": kb["confidence"],
                    "answer": kb["answer"], "sources": kb["sources"],
                    **_empty_data_fields(), **base}
        return {"route": "knowledge", "intent": "RAG", "confidence": "low", "sources": [],
                "answer": "I don't have information about that for MGNREGA, PMAY-G, "
                          "Focus Plus or CM Elevate.",
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
