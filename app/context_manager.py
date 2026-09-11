"""
Conversation Context Manager — the ChatGPT-style multi-turn layer around the
existing follow-up rewrite (see app.pipeline.looks_like_followup /
rewrite_followup / the prior_resolved entity carry in resolve_entities).

This module does NOT replace any of that. It sits immediately around it:

    question
       |
       v
    substitute_references()   <- deterministic: "the previous year" -> "FY 2023-24",
       |                          "the former/latter/other one" -> a named entity
       v
    [existing] looks_like_followup() / rewrite_followup()   <- unchanged
       |
       v
    inject_scheme_hint()      <- deterministic: append the pinned scheme's name
       |                          when the (possibly rewritten) question still
       |                          names none, so classify_scheme() doesn't have
       |                          to guess or pause
       v
    merged_prior_resolved()   <- extends resolve_entities()'s existing
                                  prior_resolved fallback with session-level
                                  structured state, so a turn that pinned no
                                  entities of its own (a KNOWLEDGE digression)
                                  doesn't erase the DATA context a later
                                  follow-up still needs
       v
    [existing pipeline continues unchanged: classify_intent, classify_scheme,
     resolve_entities, generate_sql, auth.authorize, execute, compose_response]
       |
       v
    update_state()             <- Context Updater: folds the finished turn's
                                   resolved entities back into session.state
    maybe_update_summary()     <- periodic, best-effort conversation summary

Every function here is defensive by construction: on any internal error it
returns the input unchanged (or an empty enrichment) rather than raising, so
CONTEXT_LAYER_ENABLED=false or a bug in this module can never take the core
pipeline down with it (failure-tolerance requirement — see README section 9
of the spec this was built against).

None of this ever touches authorization. Every SQL query the pipeline builds
is still re-authorized from scratch by auth.authorize() against the live
scope on every turn — this module only ever changes question TEXT and
resolved-entity HINTS that feed the existing SQL-generation prompt; it never
grants access to a query the current turn's own scope wouldn't already pass.
"""
import logging
import re
import time

from app.config import settings
from app.session_store import ConversationState, Session

logger = logging.getLogger(__name__)


class AmbiguousReference(Exception):
    """Raised by substitute_references() when a pronoun-like reference ("the
    other one", "the latter") cannot be resolved with confidence — e.g. no
    comparison_entities on record, or more than two candidates. The caller
    (app.pipeline) turns this into the existing ClarificationNeeded pause
    (same UI, same one-tap-chip mechanism as every other clarification) —
    this module deliberately does not know about ClarificationNeeded so it
    stays free of a dependency back on pipeline.py at import time."""

    def __init__(self, question: str, options: list[dict] | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []


# ── Reference resolution ────────────────────────────────────────────────────
_PREV_YEAR_RX = re.compile(
    r"\b(the\s+)?(previous|last|prior)\s+(financial\s+)?year\b", re.IGNORECASE)
_CURR_YEAR_RX = re.compile(
    r"\b(the\s+)?(current|this|same)\s+(financial\s+)?year\b", re.IGNORECASE)
_FORMER_RX = re.compile(r"\bthe\s+former\b", re.IGNORECASE)
_LATTER_RX = re.compile(r"\bthe\s+latter\b", re.IGNORECASE)
_OTHER_ONE_RX = re.compile(r"\b(the\s+)?other\s+one\b", re.IGNORECASE)
# Negative lookahead on "schemes?" so "both schemes"/"both scheme" is left
# untouched — that exact phrasing already has its own established meaning
# (pipeline._EXPLICIT_BOTH -> classify_scheme's whole-catalog shortcut) and
# must not be clobbered by a district/block comparison recorded earlier in
# the conversation (e.g. "compare both schemes" after "compare West Garo
# Hills and East Garo Hills" would otherwise garble into "... schemes").
_BOTH_RX = re.compile(r"\bboth\b(?!\s+schemes?\b)", re.IGNORECASE)


def _fy_text(year_key: int) -> str:
    return f"FY {year_key}-{(year_key + 1) % 100:02d}"


def substitute_references(question: str, state: "ConversationState | None") -> str:
    """Deterministically resolve the reference phrases the spec calls out
    explicitly ("the previous year", "the current year", "the former/latter",
    "the other one", "both") against structured session state, BEFORE the
    question reaches the existing follow-up detector. Anything this function
    doesn't recognise is left untouched — "it"/"that"/"this"/"them"/"there"
    stay exactly as typed, for the existing looks_like_followup /
    rewrite_followup LLM path to handle from the previous turn's text (that
    path already exists and already works for plain pronouns; duplicating it
    here would be the "duplicate follow-up system" the spec says not to
    build).

    Raises AmbiguousReference (never guesses) when a comparison reference
    ("the former", "the other one") has no recorded comparison_entities to
    resolve against, or more than two candidates."""
    if not question or not state:
        return question
    q = question

    if state.year is not None and _PREV_YEAR_RX.search(q):
        q = _PREV_YEAR_RX.sub(_fy_text(state.year - 1), q)
    if state.year is not None and _CURR_YEAR_RX.search(q):
        q = _CURR_YEAR_RX.sub(_fy_text(state.year), q)

    if _FORMER_RX.search(q) or _LATTER_RX.search(q) or _OTHER_ONE_RX.search(q):
        ents = state.comparison_entities or []
        if len(ents) < 2:
            raise AmbiguousReference(
                "Which one did you mean? I don't have two things on the table to "
                "compare yet — please name it directly."
            )
        if len(ents) > 2 and (_FORMER_RX.search(q) or _LATTER_RX.search(q)):
            # "the former"/"the latter" only make sense for exactly two —
            # with 3+ recorded, don't guess which pair the user means.
            raise AmbiguousReference(
                f"You mentioned {len(ents)} — {', '.join(ents)}. Which one do you mean?",
                options=[{"label": e, "question": q.replace("the former", e).replace("the latter", e)}
                         for e in ents],
            )
        if _FORMER_RX.search(q):
            q = _FORMER_RX.sub(ents[0], q)
        if _LATTER_RX.search(q):
            q = _LATTER_RX.sub(ents[-1], q)
        if _OTHER_ONE_RX.search(q):
            if len(ents) != 2:
                raise AmbiguousReference(
                    f"You mentioned {len(ents)} — {', '.join(ents)}. Which one is "
                    "\"the other one\"?",
                    options=[{"label": e, "question": q.replace("the other one", e)} for e in ents],
                )
            # Which one is "the other" depends on which one the rest of the
            # question already names; if neither/both are named, don't guess.
            named = [e for e in ents if e.lower() in q.lower()]
            other = [e for e in ents if e not in named]
            if len(named) == 1 and len(other) == 1:
                q = _OTHER_ONE_RX.sub(other[0], q)
            else:
                raise AmbiguousReference(
                    f"\"The other one\" of {', '.join(ents)} — which is already named and "
                    "which is \"the other\"?",
                    options=[{"label": e, "question": q.replace("the other one", e)} for e in ents],
                )

    if _BOTH_RX.search(q) and len(state.comparison_entities or []) == 2:
        q = _BOTH_RX.sub(" and ".join(state.comparison_entities), q)

    if q != question:
        logger.info("context_manager.substitute_references: %r -> %r", question, q)
    return q


# ── Scheme-hint injection ───────────────────────────────────────────────────
def inject_scheme_hint(question: str, state: "ConversationState | None") -> str:
    """When a follow-up fragment (already rewritten to standalone form by the
    existing rewrite_followup) still names no scheme and no scheme-specific
    vocabulary of its own, deterministically append the session's pinned
    scheme — so classify_scheme()'s existing exact-match shortcut
    (_named_schemes) picks it up instead of falling through to "which
    scheme?" or guessing "both". Only fires when state.scheme is a single
    pinned scheme; a multi-scheme comparison state is left alone (nothing
    safe to inject). Never touches a question that already names/implies a
    scheme — this is purely a gap-filler, never an override."""
    if not question or not state or not state.scheme:
        return question
    try:
        # Lazy import: avoids a module-load-order cycle with app.pipeline,
        # which imports this module. By the time this runs, pipeline is
        # already fully loaded (this is only ever called from within a
        # pipeline request handler).
        from app import pipeline as _pipeline
    except Exception:  # noqa: BLE001
        return question
    try:
        if _pipeline._named_schemes(question):
            return question
        if _pipeline._infer_scheme_from_terms(question):
            return question
    except Exception:  # noqa: BLE001 — private helpers may change shape; degrade safely
        return question
    out = f"{question.rstrip(' ?.')} under {state.scheme}?"
    logger.info("context_manager.inject_scheme_hint: %r -> %r", question, out)
    return out


# ── Entity-inheritance fallback (extends resolve_entities' prior_resolved) ──
def state_to_resolved_entities(state: "ConversationState | None") -> dict:
    """The subset of structured state that resolve_entities' prior_resolved
    fallback already knows how to consume (district/block/year_key/
    village_code) — see app.pipeline.resolve_entities. Used as the
    session-level floor beneath the turn-level prev.resolved_entities, so a
    KNOWLEDGE digression in between (which leaves state untouched — see
    update_state) doesn't erase what a later DATA follow-up still needs."""
    if not state:
        return {}
    out: dict = {}
    if state.district:
        out["district"] = state.district
    if state.block:
        out["block"] = state.block
    if state.village:
        out["village_code"] = state.village
    if state.year is not None:
        out["year_key"] = state.year
    if state.tranche:
        out["tranche_label"] = state.tranche
    if state.tranche_all_combined:
        # Not a real stored value — must never be copied into resolve_entities'
        # `resolved` dict (that feeds the SQL prompt's WHERE-clause filter
        # verbatim, see prompt_builder._entities_block). This key exists only
        # so _answer_data can tell _needs_tranche_clarification "the user
        # already picked 'all tranches combined' earlier this session" without
        # re-asking on a bare follow-up that doesn't restate "tranche".
        out["tranche_all_combined"] = True
    return out


def merged_prior_resolved(turn_resolved: dict | None, state: "ConversationState | None") -> dict:
    """turn-level prior_resolved (the immediately previous turn's own
    resolved_entities) takes priority; session-level structured state fills
    in whatever that turn didn't have (typically because it was a KNOWLEDGE
    or EDGE turn with no resolved_entities of its own)."""
    base = state_to_resolved_entities(state)
    base.update({k: v for k, v in (turn_resolved or {}).items() if v is not None})
    return base


# ── Metric-label tracking (for structured state / summary only — never fed
#    into SQL generation, which reads the question text directly as today) ──
_METRIC_KEYWORDS: list[tuple[str, "re.Pattern"]] = [
    ("person-days", re.compile(r"person[\s-]?days?|man[\s-]?days?|work[\s-]?days?|muster", re.IGNORECASE)),
    ("job-cards", re.compile(r"job\s*cards?", re.IGNORECASE)),
    ("100-days-completion", re.compile(r"100[\s-]?days?|hundred[\s-]?days?", re.IGNORECASE)),
    ("expenditure", re.compile(r"expenditure|wages?|spend|spent|material cost|\bcost\b", re.IGNORECASE)),
    ("houses-sanctioned", re.compile(r"sanction", re.IGNORECASE)),
    ("houses-completed", re.compile(r"complet", re.IGNORECASE)),
    ("fund-utilisation", re.compile(r"utili[sz]ation|fund releas|amount released", re.IGNORECASE)),
    ("disbursement", re.compile(r"disburs", re.IGNORECASE)),
    ("applications", re.compile(r"applications?", re.IGNORECASE)),
    ("beneficiaries", re.compile(r"beneficiar", re.IGNORECASE)),
]


def detect_metric(text: str) -> "str | None":
    for label, rx in _METRIC_KEYWORDS:
        if rx.search(text or ""):
            return label
    return None


_COMPARE_CUE_RX = re.compile(r"\b(compare|comparison|versus|\bvs\.?\b|between)\b", re.IGNORECASE)


def detect_comparison_districts(text: str) -> list[str]:
    """Best-effort: two-or-more district names in a question that reads like
    a comparison ("compare X and Y", "X versus Y", "between X and Y"). Feeds
    comparison_entities so a later "the former"/"the latter"/"the other one"
    has something concrete to resolve against. Deliberately conservative —
    only fires with an explicit comparison cue, so an ordinary "in X and Y
    both" state-wide question doesn't get mistaken for a two-way comparison."""
    if not text or not _COMPARE_CUE_RX.search(text):
        return []
    try:
        from app.entity_resolver import all_districts
        names = all_districts("MGNREGA") or all_districts("PMAY-G")
    except Exception:  # noqa: BLE001
        return []
    tl = text.lower()
    found = [d for d in names if d.lower() in tl]
    return found[:4]


# ── Context Updater ──────────────────────────────────────────────────────────
def update_state(session: "Session | None", raw_question: str, standalone_question: str,
                 result: dict) -> None:
    """Fold the finished turn back into session.state. Never raises."""
    if session is None or not settings.CONTEXT_STATE_ENABLED:
        return
    try:
        state = session.state
        route = result.get("route")
        state.last_question = raw_question
        state.last_standalone_question = standalone_question
        state.last_intent = result.get("intent")
        state.last_route = route
        state.turn_count += 1

        if route != "data":
            # A KNOWLEDGE / EDGE / denied turn is a digression, not a topic
            # reset: leave scheme/district/block/village/year/metric exactly
            # as they were so a later "and in 2023-24?" still resolves
            # against the last DATA context (context-bleed-prevention: this
            # is the "must NOT inherit INTO the digression" rule working in
            # the other direction — the digression must not overwrite what
            # came before it either).
            return

        schemes = result.get("schemes") or []
        resolved = result.get("resolved_entities") or {}
        if len(schemes) == 1:
            state.scheme = schemes[0]
        elif len(schemes) >= 2:
            state.scheme = None
            state.comparison_entities = list(schemes)
            state.comparison_kind = "scheme"

        if resolved.get("district"):
            state.district = str(resolved["district"])
        if resolved.get("block"):
            state.block = str(resolved["block"])
        if resolved.get("village_code") or resolved.get("village"):
            state.village = str(resolved.get("village") or resolved.get("village_code"))
        if resolved.get("year_key") is not None:
            try:
                y = int(resolved["year_key"])
                state.year = y
                state.previous_year = y - 1
            except (TypeError, ValueError):
                pass

        # Focus Plus tranche — mirrors district/block/village/year above, plus
        # an explicit "all combined" flag (see state_to_resolved_entities):
        # resolve_tranche_label only ever returns something for a SPECIFIC
        # named tranche (see app.entity_resolver), so a Focus Plus-only turn
        # that reached here with no tranche_label at all only did so because
        # _needs_tranche_clarification's gate was already satisfied by an
        # explicit "all tranches combined" / breakdown cue (or a prior
        # all-combined choice) — never by silent default. Recording that
        # keeps a later bare follow-up ("top 3 only") from losing the choice
        # or getting re-asked.
        if schemes == ["Focus Plus"]:
            tl = resolved.get("tranche_label")
            if tl:
                state.tranche = tl if isinstance(tl, str) else (tl[0] if len(tl) == 1 else None)
                state.tranche_all_combined = False
            else:
                state.tranche = None
                state.tranche_all_combined = True

        metric = detect_metric(standalone_question or raw_question)
        if metric:
            state.metric = metric

        districts = detect_comparison_districts(standalone_question or raw_question)
        if len(districts) >= 2:
            state.comparison_entities = districts
            state.comparison_kind = "district"
    except Exception:  # noqa: BLE001 — the context layer must never break an answer
        logger.warning("context_manager.update_state failed (non-fatal)", exc_info=True)


# ── Token-aware context window for the follow-up rewrite prompt ────────────
def _approx_tokens(text: str) -> int:
    """~4 chars/token — good enough for a soft budget; no tokenizer dependency."""
    return max(1, len(text or "") // 4)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    limit_chars = max(0, max_tokens * 4)
    return text if len(text) <= limit_chars else text[:limit_chars].rstrip() + "…"


def build_state_block(state: "ConversationState | None") -> str:
    """One compact line of KNOWN CONTEXT — the "current structured state"
    layer of the context window. Deterministic, no model call."""
    if not state:
        return ""
    parts = []
    if state.scheme:
        parts.append(f"scheme={state.scheme}")
    if state.comparison_entities:
        parts.append(f"comparing={'/'.join(state.comparison_entities)} ({state.comparison_kind})")
    if state.district:
        parts.append(f"district={state.district}")
    if state.block:
        parts.append(f"block={state.block}")
    if state.village:
        parts.append(f"village={state.village}")
    if state.year is not None:
        parts.append(f"year={_fy_text(state.year)}")
    if state.metric:
        parts.append(f"metric={state.metric}")
    if state.tranche:
        parts.append(f"tranche={state.tranche}")
    elif state.tranche_all_combined:
        parts.append("tranche=all combined")
    return "Known context: " + ", ".join(parts) if parts else ""


async def build_followup_context(session: "Session | None", question: str) -> str:
    """The enrichment block passed to rewrite_followup() as `extra_context` —
    structured state + conversation summary + (only for a long/resumed
    conversation, where the in-process turn window is thin) semantically
    relevant older turns pulled from Qdrant. Token-budgeted per
    CONTEXT_MAX_TOKENS; each section gets its own sub-budget so one long
    section can't crowd out the others entirely.

    Priority order (highest first, per the spec): current question (not
    built here — the caller appends it separately), system instructions
    (also the caller's), structured state, recent turns (already in
    rewrite_followup's own prompt as PREVIOUS question/answer), relevant
    historical turns, summary. Truncation drops the LOWEST-priority section
    first when the budget is tight.
    """
    if session is None or not settings.CONTEXT_LAYER_ENABLED:
        return ""
    budget = settings.CONTEXT_MAX_TOKENS
    sections: list[str] = []

    try:
        state_block = build_state_block(session.state)
    except Exception:  # noqa: BLE001
        state_block = ""
    if state_block:
        sections.append(state_block)
        budget -= _approx_tokens(state_block)

    # Relevant historical turns — only reached for once the in-process window
    # is thin (a resumed/long conversation), to keep the common case free of
    # an extra embed + Qdrant round trip.
    if (settings.CONTEXT_SEMANTIC_MEMORY_ENABLED and budget > 0
            and len(session.turns) < settings.CONTEXT_MEMORY_MIN_SESSION_TURNS):
        try:
            from app import conversation_memory
            scope = getattr(session, "scope", None)
            hits = await conversation_memory.search_relevant_turns(
                question=question,
                tenant_id=getattr(scope, "tenant_id", None),
                user_id=getattr(scope, "db_user_id", None),
                session_id=session.session_id,
                top_k=settings.CONTEXT_HISTORICAL_TURNS_MAX,
            )
        except Exception:  # noqa: BLE001 — memory retrieval failing must never break the turn
            logger.warning("context_manager: semantic memory lookup failed, continuing without it",
                           exc_info=True)
            hits = []
        if hits:
            lines = [f'- Q: "{h["standalone_question"] or h["question"]}" A: "{h["answer"][:160]}"'
                     for h in hits]
            block = "Relevant earlier turns:\n" + "\n".join(lines)
            block = _truncate_to_tokens(block, max(0, min(budget, 400)))
            if block:
                sections.append(block)
                budget -= _approx_tokens(block)

    if settings.CONTEXT_SUMMARY_ENABLED and session.summary and budget > 0:
        block = _truncate_to_tokens(
            f"Conversation summary so far: {session.summary}",
            min(budget, settings.CONTEXT_SUMMARY_MAX_TOKENS),
        )
        sections.append(block)

    return "\n".join(sections)


# ── Conversation summary (periodic, best-effort) ────────────────────────────
def should_update_summary(session: "Session") -> bool:
    if not settings.CONTEXT_SUMMARY_ENABLED:
        return False
    n = session.state.turn_count
    return n > 0 and (n - session.summary_turn_count) >= settings.CONTEXT_SUMMARY_EVERY_N_TURNS


async def maybe_update_summary(session: "Session | None") -> None:
    """Refresh session.summary from the last few standalone questions, every
    CONTEXT_SUMMARY_EVERY_N_TURNS turns. Never raises: a failed summary just
    means the conversation keeps running without an updated one (requirement
    9) — the caller doesn't need to check the return value."""
    if session is None or not should_update_summary(session):
        return
    try:
        from app import llm

        recent = [t.question for t in session.turns[-settings.CONTEXT_RECENT_TURNS:] if t.question]
        if not recent:
            return
        state_block = build_state_block(session.state)
        prompt = (
            "Summarise this conversation in ONE short paragraph (max 60 words): which "
            "scheme(s), locations, financial years, metrics and comparisons have come up, "
            "and any question left unresolved. Plain prose, no headings.\n\n"
            + (f"{state_block}\n" if state_block else "")
            + "Questions so far:\n"
            + "\n".join(f"- {q}" for q in recent)
            + "\n\nSummary:"
        )
        out = await llm.call_classifier(prompt)
        out = out.strip().strip('"')
        if out and len(out) <= 800:
            session.summary = out
            session.summary_turn_count = session.state.turn_count
            logger.info("context_manager: summary updated (turn_count=%d)", session.state.turn_count)
            try:
                from app import conversation_store
                conversation_store.save_context_state(
                    session_id=session.session_id,
                    context_state=session.state.to_dict(),
                    summary=session.summary,
                )
            except Exception:  # noqa: BLE001 — L2 persistence is best-effort
                logger.warning("context_manager: could not persist summary (non-fatal)", exc_info=True)
    except Exception:  # noqa: BLE001 — requirement 9: continue without the new summary
        logger.warning("context_manager: summary generation failed (non-fatal)", exc_info=True)
