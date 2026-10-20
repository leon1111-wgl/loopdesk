"""Voice channel over WebSocket (app/voice/session.py has the flow).

    client                                   server
    {"type":"auth","token",                  {"type":"ready", asr, tts, sample rates}
     "conversation_id","mode"}  ──────────►  (close 4401 / 4404 / 4503 if refused)
    binary: int16 PCM, 16 kHz, mono ──────►  {"type":"vad"|"partial"|"transcript"|"prompt"|"agent", ...}
    {"type":"ptt","down"}                    {"type":"say", turn, id, text, sample_rate}
    {"type":"mode","mode"}                   binary: uint16 turn, uint16 sentence, int16 PCM
    {"type":"played", turn, id, ms, done}    {"type":"say_end"|"barge_in"|"turn_end"|"turn_saved", ...}
    {"type":"stop"}

The agent events inside {"type":"agent"} are the same ones the SSE chat stream sends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..config import get_settings
from ..db import new_session
from ..models import Conversation
from ..security import Principal, get_principal, principal_from_token, rate_limiter
from ..services import get_services
from ..voice.engines import get_engines
from ..voice.session import VoiceSession

router = APIRouter(prefix="/api/voice", tags=["voice"])
logger = logging.getLogger(__name__)
MAX_FRAME_BYTES = 64 * 1024  # 2 s of 16 kHz audio; clients send 100 ms frames
MAX_TEXT_BYTES = 4096
# Messages waiting for the worker (100 ms of audio each): a client sending faster than ASR decodes
# is cut off instead of growing memory without bound.
MAX_BACKLOG = 200


@router.get("/config")
def voice_config(principal: Principal = Depends(get_principal)) -> dict:
    """Whether this server has a voice channel, and how it is set up (the chat page shows the button)."""
    settings = get_settings()
    engines, problem = get_engines(settings)
    return {"enabled": engines is not None, "reason": problem, **(engines.describe() if engines else {}),
            "input_sample_rate": 16000, "end_silence_ms": settings.voice_end_silence_ms,
            "barge_in_ms": settings.voice_barge_in_ms, "modes": ["ptt", "continuous"]}


def _authorize(first: dict) -> tuple[Principal, Conversation]:
    with new_session() as db:
        principal = principal_from_token(db, str(first.get("token") or ""))
        conversation = db.get(Conversation, int(first.get("conversation_id") or 0))
        if conversation is None or conversation.tenant_id != principal.tenant.id or \
                (conversation.user_id != principal.user.id and not principal.is_staff):
            raise LookupError("conversation")
        db.expunge_all()
        return principal, conversation


@router.websocket("/ws")
async def voice_ws(ws: WebSocket) -> None:
    await ws.accept()
    settings = get_settings()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        principal, conversation = await asyncio.to_thread(_authorize, first if isinstance(first, dict) else {})
    except WebSocketDisconnect:
        return
    except (asyncio.TimeoutError, ValueError, TypeError, HTTPException, KeyError):
        await ws.close(code=4401, reason="unauthorized")
        return
    except LookupError:
        await ws.close(code=4404, reason="conversation not found")
        return
    try:
        rate_limiter.check(f"voice:{principal.user.id}", max(1, settings.rate_limit_per_minute // 5))
    except HTTPException:
        await ws.close(code=4429, reason="too many voice sessions")
        return
    engines, problem = await asyncio.to_thread(get_engines, settings)
    if engines is None:
        await ws.close(code=4503, reason="voice unavailable")
        return

    # Server -> client: an asyncio queue filled from the session's threads, so no thread is parked per
    # session waiting on it (the default executor is shared with every sync endpoint).
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue = asyncio.Queue()

    def emit(item) -> None:
        try:
            loop.call_soon_threadsafe(outbox.put_nowait, item)
        except RuntimeError:  # the event loop is gone: nobody to send to
            pass

    inbox: queue.Queue = queue.Queue(maxsize=MAX_BACKLOG)
    mode = first.get("mode") if first.get("mode") in ("ptt", "continuous") else "continuous"
    session = await asyncio.to_thread(lambda: VoiceSession(
        engines=engines, services=get_services(), settings=settings, tenant_id=principal.tenant.id,
        user_id=principal.user.id, conversation_id=conversation.id, emit=emit, db_factory=new_session,
        mode=mode))

    def work() -> None:  # VAD, ASR and control messages, strictly in arrival order
        while (item := inbox.get()) is not None:
            kind, payload = item
            try:
                session.handle_audio(payload) if kind == "audio" else session.handle_control(payload)
            except Exception:
                logger.exception("voice input failed")
                emit({"type": "error", "message": "语音处理失败"})
        session.close()
        emit(None)

    def stop() -> None:
        while True:  # make room for the end marker even when the backlog is full
            try:
                inbox.get_nowait()
            except queue.Empty:
                break
        inbox.put(None)

    worker = threading.Thread(target=work, name="loopdesk-voice-in", daemon=True)
    worker.start()

    async def send() -> None:
        while (item := await outbox.get()) is not None:
            if ws.application_state != WebSocketState.CONNECTED:
                continue
            try:
                if isinstance(item, bytes):
                    await ws.send_bytes(item)
                else:
                    await ws.send_text(json.dumps(item, ensure_ascii=False, default=str))
            except Exception:  # the client went away; keep draining so the session can finish
                pass

    sender = asyncio.create_task(send())
    emit({"type": "ready", "mode": mode, **engines.describe(), "input_sample_rate": 16000})
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                item = ("audio", message["bytes"]) if len(message["bytes"]) <= MAX_FRAME_BYTES else None
            elif message.get("text") is not None and len(message["text"]) <= MAX_TEXT_BYTES:
                try:
                    payload = json.loads(message["text"])
                except ValueError:
                    continue
                item = ("control", payload) if isinstance(payload, dict) else None
            else:
                item = None
            if item is None:
                continue
            try:
                inbox.put_nowait(item)
            except queue.Full:
                logger.warning("voice session %s fell behind; closing", conversation.id)
                await ws.close(code=1013, reason="audio backlog")
                break
    except WebSocketDisconnect:
        pass
    finally:
        stop()  # the worker closes the session (waits for a running turn) and ends the sender
        await sender
