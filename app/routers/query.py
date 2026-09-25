import asyncio
import logging
import time
import uuid
from datetime import date

from fastapi import (APIRouter, Depends, File, Form, HTTPException,
                     Request, UploadFile)
from pydantic import BaseModel, Field, field_validator

from app import asr_guard, auth, conversation_memory, conversation_store, llm
from app.cache import metrics, response_cache
from app.config import settings
from app.deps import current_scope, require_user
from app.net import client_ip as _resolve_ip
from app.semantic_cache import semantic_cache
from app.session_store import ConversationState, Turn, session_store
from app.db import UnsafeSQLError
from app.llm import ModelBusyError
from app.pipeline import (
    ClarificationNeeded,
    _empty_data_fields,
    answer_question,
    looks_like_followup,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MB — a minute or so of WAV


class QueryRequest(BaseModel):
    # A03 — bound the free-text field. 2000 chars is far above any real question
    # and well below anything that would bloat a prompt or an audit row.
    question: str = Field(min_length=1, max_length=settings.MAX_QUESTION_CHARS)
    session_id: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )  # continue an existing conversation

    @field_validator("question")
    @classmethod
    def _strip_control_chars(cls, v: str) -> str:
        # Drop C0 control characters except tab/newline/carriage-return.
        cleaned = "".join(c for c in v if c in "\t\n\r" or ord(c) >= 0x20)
        if not cleaned.strip():
            raise ValueError("question must not be empty")
        return cleaned


def _client_ip(request: Request) -> str:
    return _resolve_ip(request)


async def _identity(scope: auth.UserScope = Depends(current_scope)) -> auth.UserScope:
    """Require a real user when AUTH_ENABLED; allow anonymous in dev mode."""
    if settings.AUTH_ENABLED and scope.db_user_id is None:
        raise HTTPException(401, "authentication required — POST /api/auth/login")
    return scope


@router.post("/api/query")
async def query(req: QueryRequest, request: Request,
                scope: auth.UserScope = Depends(_identity)):
    if not req.question or not req.question.strip():
        raise HTTPException(400, "question must not be empty")

    started = time.monotonic()
    ip = _client_ip(request)
    client_session = req.session_id or request.headers.get("x-session-id")
    # The client owns conversation identity: the chat UI mints a session_id per
    # "New chat" so each thread is its own row in app.conversations. Only a
    # caller that sends none falls back to the per-day bucket, which keeps API
    # scripts and older clients from creating a thread per one-off question.
    if client_session:
        session_id = client_session
    elif scope.db_user_id is not None:
        session_id = f"day-{scope.db_user_id}-{date.today().isoformat()}"
    else:
        session_id = f"one-{uuid.uuid4().hex[:16]}"
    session = session_store.ensure(session_id, scope.user_id)
    session.scope = scope
    # Rehydrate the L1 structured state from its L2 (Postgres) mirror when this
    # is a fresh in-process session resuming an existing, client-named thread —
    # a worker restart, a session that TTL'd out of session_store, or a request
    # landing on a different worker. Only on a session with no turns yet, so a
    # live conversation's in-process state (the source of truth while it's
    # active) is never clobbered mid-thread. Best-effort: any failure just
    # means this turn starts with blank state, same as before this layer existed.
    if not session.turns and client_session:
        try:
            durable = await conversation_store.load_context_state(session_id)
            if durable.get("context_state"):
                session.state = ConversationState.from_dict(durable["context_state"])
            if durable.get("summary"):
                session.summary = durable["summary"]
                session.summary_turn_count = session.state.turn_count
        except Exception as e:  # noqa: BLE001
            logger.warning("context state rehydration failed (non-fatal): %s", e)

    # Response + semantic caches are keyed on (question + authorization
    # fingerprint): safe to share across users who resolve to the same scope,
    # since the auth outcome is a pure function of scope + query. Genuine
    # follow-up fragments ("what about EGH?") are never cached — they only mean
    # something against their own conversation.
    scope_key = scope.cache_fingerprint()
    cacheable = not looks_like_followup(req.question)
    sem_vec = None
    result = None
    served_from = None  # metrics route tag when the answer came from a cache

    if cacheable:
        cached = await response_cache.get(req.question, scope_key)
        if cached is not None:
            result = {**cached, "session_id": session_id}
            served_from = f"{cached.get('route', 'data')}:cache"
        else:
            sem_hit, sem_vec = await semantic_cache.lookup(req.question, scope_key)
            if sem_hit is not None:
                result = {**sem_hit, "session_id": session_id}
                served_from = f"{sem_hit.get('route', 'data')}:semcache"

    if result is None:
        try:
            result = await asyncio.wait_for(
                answer_question(req.question, session=session, scope=scope),
                timeout=settings.REQUEST_TIMEOUT_SECONDS,
            )
        except ClarificationNeeded as e:
            # A scope pause ("which area / year?"), a year pause ("which FY?"), or
            # an entity-ambiguity pause ("which of these villages/districts/blocks?")
            # carries one-tap options where they exist, but the user may still type
            # the answer as free text ("West Garo Hills 2023-24", "East Khasi
            # Hills"). Remember the question so the pipeline merges that reply back
            # into it next turn instead of treating it as a brand-new question.
            if e.rule in ("scope-not-specified", "year-not-specified", "entity-ambiguous",
                         "ranking-count-not-specified"):
                session.pending_scope_q = req.question
                session.pending_village_hint = e.village_hint
            # Shape it for the frontend clarification renderer (intent CLARIFY +
            # clarification.options), and keep the flat keys older callers read.
            out = {
                "route": "clarification",
                "intent": "CLARIFY",
                "confidence": "high",
                "answer": e.question,
                "needs_clarification": True,
                "question": e.question,
                "clarification": {"options": e.options, "rule": e.rule},
                "session_id": session_id,
                **_empty_data_fields(),
            }
            # A clarification pause is still a turn the user should see when they
            # reopen the thread — persist it durably (L2) exactly like an answered
            # turn, so the conversation appears in the sidebar and replays the
            # clarifying question + its option chips. L1 follow-up context is left
            # untouched: the resume already runs off session.pending_scope_q.
            clarify_latency_ms = (time.monotonic() - started) * 1000
            conversation_store.persist_turn(
                tenant_id=scope.tenant_id, user_id=scope.db_user_id, session_id=session_id,
                result=out, question=req.question, latency_ms=clarify_latency_ms,
                username=scope.username or None, ip=ip,
            )
            _mirror_audit(scope, session_id, req.question, {"route": "clarification"}, started, ip)
            return out
        except ModelBusyError as e:
            metrics.busy_rejections += 1
            raise HTTPException(503, "The service is busy right now. Please retry in a few seconds.",
                                headers={"Retry-After": "5"}) from e
        except asyncio.TimeoutError as e:
            metrics.errors += 1
            raise HTTPException(504, "The request took too long. Please try a narrower question.") from e
        except UnsafeSQLError as e:
            metrics.errors += 1
            logger.error("Blocked unsafe SQL: %s", e)
            raise HTTPException(500, "the generated query failed a safety check") from e
        except Exception as e:
            metrics.errors += 1
            metrics.model_errors += 1
            logger.exception("query failed")
            raise HTTPException(502, "upstream error while answering the question") from e

    latency_ms = (time.monotonic() - started) * 1000
    result["session_id"] = session_id
    # Surface the real end-to-end time to the UI meta line. On a cache hit this
    # is recomputed here (the fast path), overwriting whatever was cached.
    result["execution_time_ms"] = round(latency_ms)

    # L1 follow-up context — recorded even on a cache hit, so the next turn's
    # follow-up rewrite still has this question as its antecedent.
    session_store.add_turn(session_id, Turn(
        question=result.get("rewritten_question") or req.question,
        raw_question=req.question,
        route=result.get("route", "data"),
        schemes=result.get("schemes", []),
        resolved_entities=result.get("resolved_entities", {}),
        answer=result.get("answer", ""),
    ))
    # L2 durable — conversation + turn + audit (fire-and-forget)
    conversation_store.persist_turn(
        tenant_id=scope.tenant_id, user_id=scope.db_user_id, session_id=session_id,
        result=result, question=req.question, latency_ms=latency_ms,
        username=scope.username or None, ip=ip,
    )
    # Semantic conversation memory — indexes this turn for later "relevant
    # older turns" retrieval (see app.context_manager.build_followup_context).
    # Fire-and-forget, scoped to this tenant/user/session; never blocks or
    # fails the response (app.conversation_memory.index_turn degrades silently).
    conversation_memory.index_turn(
        tenant_id=scope.tenant_id, user_id=scope.db_user_id, session_id=session_id,
        question=req.question, standalone_question=result.get("rewritten_question") or req.question,
        answer=result.get("answer", ""), schemes=result.get("schemes"),
    )
    # L2 mirror of the structured conversation state (context_manager.update_state
    # already ran, in-process, inside answer_question). Runs after persist_turn
    # above so the app.conversations row it needs is guaranteed to exist,
    # including on this session's very first turn.
    conversation_store.save_context_state(
        session_id=session_id, context_state=session.state.to_dict())
    _mirror_audit(scope, session_id, req.question, result, started, ip)

    # Only write back a freshly computed answer, and only under this caller's scope.
    if cacheable and served_from is None:
        await response_cache.put(req.question, result, scope_key)
        await semantic_cache.put(req.question, result, vec=sem_vec, scope_key=scope_key)

    metrics.record(served_from or result.get("route", "data"), latency_ms)
    return result


def _mirror_audit(scope, session_id, question, result, started, ip):
    """JSONL mirror of the DB audit trail — offline grep / belt-and-braces."""
    auth.audit({
        "tenant_id": scope.tenant_id,
        "user_id": scope.db_user_id,
        "username": scope.username or "(anonymous)",
        "role": scope.role,
        "session_id": session_id,
        "question": question,
        "rewritten_question": result.get("rewritten_question"),
        "route": result.get("route"),
        "schemes": result.get("schemes"),
        "granularity": auth.infer_granularity(result.get("resolved_entities", {}), result.get("sql")),
        "allowed": result.get("route") != "denied",
        "deny_check": result.get("denied_by"),
        "row_count": result.get("row_count"),
        "ip": ip,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    })


@router.post("/api/query/transcribe")
async def transcribe(file: UploadFile = File(...),
                     language: str = Form("en"),
                     device: str = Form(""),
                     scope: auth.UserScope = Depends(_identity)):
    """Voice input -> text, via qwen3-asr on the model gateway.

    `language` is already sent by the web UI (ai_query.html appends it to the
    same FormData) and was previously discarded here, because FastAPI drops an
    undeclared form field. Accepting it closes that contract mismatch; the
    value is validated in llm._asr_language before it reaches the gateway,
    which rejects unknown codes with a 400. Defaulted so any client that omits
    the field keeps working.

    Note this was NOT the cause of the "ASR can't convert" failure — that was
    the AudioContext lifecycle bug in ai_query.html's blobToWav()."""
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio upload")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large (max 10 MB)")
    try:
        text = await llm.call_asr(audio, file.filename or "audio.wav",
                                  language=language or "en")
    except ModelBusyError as e:
        raise HTTPException(503, "The service is busy right now. Please retry in a few seconds.",
                            headers={"Retry-After": "5"}) from e
    except Exception as e:
        logger.exception("transcription failed")
        raise HTTPException(502, "transcription upstream error") from e
    # The ASR answers audio with no speech in it with a stock phrase ("Okay.",
    # "I'm not sure.") rather than an empty string. Returning that as the
    # transcript typed it into the composer as if the user had said it.
    # With the vocabulary prompt on, the same audio comes back as the prompt
    # itself, verbatim — the cleaner of the two signals.
    no_speech = (asr_guard.is_no_speech(text)
                 or asr_guard.is_prompt_echo(text, settings.ASR_PROMPT))
    _save_asr_sample(audio, text, no_speech, device)
    if no_speech:
        logger.info("transcribe: no speech (asr said %r, %d bytes)", text[:80], len(audio))
        return {"text": "", "no_speech": True}
    return {"text": text}


def _save_asr_sample(audio: bytes, text: str, no_speech: bool, device: str) -> None:
    """settings.ASR_DEBUG_DIR only: keep the upload and the ASR's answer so a
    bad transcript can be replayed and measured instead of guessed at."""
    if not settings.ASR_DEBUG_DIR:
        return
    try:
        import json
        from pathlib import Path
        d = Path(settings.ASR_DEBUG_DIR)
        d.mkdir(parents=True, exist_ok=True)
        stem = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}"
        (d / f"{stem}.wav").write_bytes(audio)
        (d / f"{stem}.json").write_text(json.dumps(
            {"text": text, "no_speech": no_speech, "device": device, "bytes": len(audio)},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.warning("could not save ASR debug sample", exc_info=True)
