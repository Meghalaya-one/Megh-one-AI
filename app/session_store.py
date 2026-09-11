"""
Short-lived chat session store for follow-up ("what about East Garo Hills?")
resolution.

In-process, per worker, everything expires — the same stance as cache.py and
semantic_cache.py. It holds only what a follow-up rewrite needs: the last few
turns (question + how it was answered) and the user's scope for the session, so
the authorization check doesn't have to rebuild it every turn.

Nothing here is authoritative and none of it survives a restart. A caller that
sends no session id just gets no follow-up context — the pipeline still works.
"""
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock

from app.config import settings


@dataclass
class Turn:
    question: str                    # the standalone question actually run
    raw_question: str                # what the user typed (may be a fragment)
    route: str                       # "data" | "knowledge" | "edge" | "denied"
    schemes: list[str] = field(default_factory=list)
    resolved_entities: dict = field(default_factory=dict)
    answer: str = ""


@dataclass
class ConversationState:
    """Structured multi-turn state — the "what are we talking about" the
    context layer maintains alongside the raw turn list, so a follow-up like
    "what about 2023-24?" doesn't have to be re-derived from prose every time.

    Deliberately a plain, JSON-round-trippable bag of scalars/lists (see
    to_dict/from_dict) so it can be persisted on app.conversations.context_state
    (see conversation_store.save_context_state) and survive a worker restart —
    the in-process Session is L1, that JSONB column is L2, same split as the
    turn list vs app.conversation_turns.

    Nothing here is ever used for authorization — every SQL query is still
    re-authorized from scratch against the live scope (see auth.authorize);
    this only feeds question rewriting / entity hints.
    """
    scheme: str | None = None                    # single active scheme, if pinned
    district: str | None = None
    block: str | None = None
    village: str | None = None
    year: int | None = None                       # year_key, e.g. 2024 for FY2024-25
    previous_year: int | None = None
    metric: str | None = None                     # last metric keyword the user asked about
    tranche: str | None = None                    # single pinned Focus Plus tranche_label, if any
    tranche_all_combined: bool = False            # True once "all tranches combined" was chosen
    comparison_entities: list[str] = field(default_factory=list)  # for "the former/latter/other one"
    comparison_kind: str | None = None            # "district" | "block" | "scheme" | "year"
    last_intent: str | None = None                # "DATA" | "KNOWLEDGE" | "EDGE" | "CLARIFY"
    last_route: str | None = None
    last_question: str | None = None              # raw text as typed
    last_standalone_question: str | None = None   # fully resolved/rewritten form
    turn_count: int = 0

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme, "district": self.district, "block": self.block,
            "village": self.village, "year": self.year, "previous_year": self.previous_year,
            "metric": self.metric, "tranche": self.tranche,
            "tranche_all_combined": self.tranche_all_combined,
            "comparison_entities": list(self.comparison_entities),
            "comparison_kind": self.comparison_kind, "last_intent": self.last_intent,
            "last_route": self.last_route, "last_question": self.last_question,
            "last_standalone_question": self.last_standalone_question,
            "turn_count": self.turn_count,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ConversationState":
        d = d or {}
        return cls(
            scheme=d.get("scheme"), district=d.get("district"), block=d.get("block"),
            village=d.get("village"), year=d.get("year"), previous_year=d.get("previous_year"),
            metric=d.get("metric"), tranche=d.get("tranche"),
            tranche_all_combined=bool(d.get("tranche_all_combined")),
            comparison_entities=list(d.get("comparison_entities") or []),
            comparison_kind=d.get("comparison_kind"), last_intent=d.get("last_intent"),
            last_route=d.get("last_route"), last_question=d.get("last_question"),
            last_standalone_question=d.get("last_standalone_question"),
            turn_count=int(d.get("turn_count") or 0),
        )


@dataclass
class Session:
    session_id: str
    user_id: str | None
    created: float
    last_seen: float
    turns: list[Turn] = field(default_factory=list)
    scope: object | None = None      # backend.auth.UserScope, cached for the session
    # Set when the pipeline paused to ask "which area / year?" (scope-not-specified).
    # Holds the original question so the next turn's free-text reply can be merged
    # back into it. Cleared as soon as it's consumed. Not persisted, per-worker.
    pending_scope_q: str | None = None
    # Set alongside pending_scope_q when the pause was a village-name ambiguity
    # ("entity-ambiguous"). Holds the exact text the user typed for the village
    # (e.g. "Adugre") so the resume can re-run resolve_village on it directly —
    # the merged reply text ("...the one in Betasing block") does NOT reliably
    # make the LLM mention-extractor re-tag the village on the merged sentence,
    # which otherwise leaves village_code unresolved and lets SQL generation
    # invent one instead of asking again or using the block. Cleared with
    # pending_scope_q.
    pending_village_hint: str | None = None
    # Structured conversation state (app/context_manager.py) — the L1 copy,
    # mirrored to app.conversations.context_state (L2) on each turn.
    state: ConversationState = field(default_factory=ConversationState)
    # Compact rolling summary of the conversation so far (schemes/locations/
    # years/metrics/comparisons/unresolved references) — see
    # context_manager.maybe_update_summary. None until the turn count first
    # crosses CONTEXT_SUMMARY_EVERY_N_TURNS.
    summary: str | None = None
    summary_turn_count: int = 0   # turn_count as of the last summary update

    @property
    def last_turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None


class SessionStore:
    def __init__(self, ttl: float, max_sessions: int, max_turns: int):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self._store: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = Lock()

    def _expired(self, s: Session, now: float) -> bool:
        return now - s.last_seen > self.ttl

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        now = time.monotonic()
        with self._lock:
            s = self._store.get(session_id)
            if s is None:
                return None
            if self._expired(s, now):
                del self._store[session_id]
                return None
            s.last_seen = now
            self._store.move_to_end(session_id)
            return s

    def ensure(self, session_id: str | None, user_id: str | None) -> Session:
        """Return the live session for this id, creating it if needed. A blank id
        gets a fresh random one (single-turn — the caller just won't send it back)."""
        now = time.monotonic()
        sid = session_id or f"anon-{uuid.uuid4().hex[:16]}"
        with self._lock:
            s = self._store.get(sid)
            if s is not None and not self._expired(s, now):
                s.last_seen = now
                if user_id and not s.user_id:
                    s.user_id = user_id
                self._store.move_to_end(sid)
                return s
            s = Session(session_id=sid, user_id=user_id, created=now, last_seen=now)
            self._store[sid] = s
            self._store.move_to_end(sid)
            while len(self._store) > self.max_sessions:
                self._store.popitem(last=False)
            return s

    def add_turn(self, session_id: str, turn: Turn) -> None:
        with self._lock:
            s = self._store.get(session_id)
            if s is None:
                return
            s.turns.append(turn)
            if len(s.turns) > self.max_turns:
                s.turns = s.turns[-self.max_turns :]
            s.last_seen = time.monotonic()

    def stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            live = sum(1 for s in self._store.values() if not self._expired(s, now))
            return {"sessions": len(self._store), "live": live}


session_store = SessionStore(
    ttl=settings.SESSION_TTL_SECONDS,
    max_sessions=settings.SESSION_MAX,
    max_turns=settings.SESSION_MAX_TURNS,
)
