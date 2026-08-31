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
