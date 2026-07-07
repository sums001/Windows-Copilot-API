"""FastAPI app wiring Copilot onto the OpenAI Chat Completions API."""

import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from copilot import CopilotClient
from copilot.driver import ClearanceRequired

from .config import MAX_PROMPT_CHARS, MODEL_NAME, RATE_LIMIT_BURST, RATE_LIMIT_RPM, WORK_MODEL_NAME
from .openai_format import (
    completion_response,
    new_id,
    sse_event,
    stream_chunk,
    usage_block,
    usage_chunk,
)
from .prompt import messages_to_prompt
from .ratelimit import TokenBucket
from .schemas import ChatCompletionRequest
from .tool_calls import fallback_local_tool_calls, parse_tool_calls, tool_choice_allows_tools

app = FastAPI(title="Copilot OpenAI-compatible API", version="1.0.0")
# Server runs headless and must never pop a visible browser mid-request. With
# both recovery passes disabled, an expired clearance surfaces immediately as a
# 503 (see ClearanceRequired handling below) so an operator can re-clear out of
# band (`python -m copilot login`). Headless auto-solve is intentionally off:
# it's unreliable on low-trust egress and a failed pass can wedge the session.
client = CopilotClient(interactive_clear=False, headless_clear=False)

_CLEARANCE_HELP = (
    "Cloudflare clearance expired and could not be refreshed headlessly. "
    "Re-clear in a browser: run `python -m copilot login` (or `python tests/diagnostic.py`) "
    "and pass the 'verify you're human' check, then retry."
)

# Enterprise (work-account) driver, lazily loaded on first `copilot-work` request
# so the server still starts consumer-only when M365 isn't bootstrapped. Cached
# after the first successful load (it holds the rotating refresh token).
_work_client = None
_work_lock = threading.Lock()
_WORK_HELP = (
    "The work-account (Microsoft 365 Copilot) driver is not set up. Run the "
    "one-time bootstrap once: `python -m copilot.m365.bootstrap`, sign in with the "
    "work account, then retry."
)


def _get_work_client():
    """Return the cached :class:`M365Copilot`, loading it on first use."""
    global _work_client
    if _work_client is None:
        with _work_lock:
            if _work_client is None:
                from copilot.m365 import M365Copilot
                _work_client = M365Copilot()
    return _work_client


def _include_usage(req) -> bool:
    """True when the client asked for the standalone usage chunk (OpenAI
    ``stream_options: {"include_usage": true}``)."""
    return bool(req.stream_options and req.stream_options.get("include_usage"))


def _stream_work(prompt: str, model: str, conversation_id=None, tools=None, messages=None, include_usage=False):
    """Yield OpenAI SSE chunks for the enterprise driver.

    Cowork delivers its reply as one completed message (no token stream), so we
    emit the whole reply as a single content chunk between the role and stop
    chunks — valid OpenAI streaming that clients render as one arrival.

    The upstream turn (lock wait + Cowork think-time) runs 30-120s with no bytes
    of its own, which trips client inactivity timeouts and triggers retry storms.
    So the blocking call runs in a worker thread while this generator emits SSE
    comment keepalives — ignored by clients, but they reset the idle timer. The
    worker also owns the upstream lock, so a client disconnect can no longer
    leave the lock held past the turn: the thread always finishes and releases.
    """
    cid = new_id()
    created = int(time.time())
    yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))

    result = {}
    done = threading.Event()

    def worker():
        try:
            # Serialize upstream turns: each Cowork call blocks a worker on a
            # Trouter socket for ~20-30s, and a coding agent fires several at
            # once; letting them all run concurrently starved the accept loop
            # (clients saw ECONNREFUSED). One at a time is slower but stable.
            with _upstream_lock:
                bot = _get_work_client()
                result["text"] = bot.ask(prompt, conversation_id=conversation_id)
                result["conversation_id"] = bot.last_conversation_id
        except Exception as exc:  # includes missing-bootstrap RuntimeError
            result["error"] = exc
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    while not done.wait(15):
        yield ": keepalive\n\n"

    if "error" in result:
        _debug(f"ERROR(stream) work: {result['error']}")
        yield sse_event(stream_chunk(cid, created, model, {"content": f"\n[error: {result['error']}]"}, finish="error"))
    else:
        text = result["text"]
        _debug(f"REPLY(stream) work len={len(text)} :: {text[:120]!r}")
        yield from _stream_text_or_tool_calls(
            cid, created, model, text, result["conversation_id"], tools, messages,
            prompt=prompt, include_usage=include_usage,
        )
    yield "data: [DONE]\n\n"


def _stream_text_or_tool_calls(cid: str, created: int, model: str, text: str, conversation_id=None, tools=None, messages=None, prompt="", include_usage=False):
    # Usage is counted locally (Copilot reports none). It rides on the finish
    # chunk by default; when the client asked via stream_options.include_usage
    # it instead arrives as the spec's standalone empty-choices chunk — either
    # way it appears exactly once, so clients that sum chunk.usage stay correct.
    usage = usage_block(prompt, text)
    tool_calls = parse_tool_calls(text, tools) if tools else []
    if not tool_calls and tools and messages:
        tool_calls = fallback_local_tool_calls(messages, tools, text)
    if tool_calls:
        _debug(f"TOOL_CALLS(stream) n={len(tool_calls)} names={[c['function']['name'] for c in tool_calls]}")
        for index, call in enumerate(tool_calls):
            delta_call = {
                "index": index,
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }
            yield sse_event(stream_chunk(cid, created, model, {"tool_calls": [delta_call]}))
        yield sse_event(stream_chunk(
            cid, created, model, {}, finish="tool_calls",
            conversation_id=conversation_id,
            usage=None if include_usage else usage,
        ))
        if include_usage:
            yield sse_event(usage_chunk(cid, created, model, usage))
        return

    if text:
        yield sse_event(stream_chunk(cid, created, model, {"content": text}))
    yield sse_event(stream_chunk(
        cid, created, model, {}, finish="stop",
        conversation_id=conversation_id,
        usage=None if include_usage else usage,
    ))
    if include_usage:
        yield sse_event(usage_chunk(cid, created, model, usage))

# Self-imposed rate limit on top of the concurrency lock below: this caps
# requests-per-minute, the lock caps requests-in-flight. See server/ratelimit.py.
_rate_limiter = TokenBucket(RATE_LIMIT_RPM, RATE_LIMIT_BURST)


def _rate_limited_response():
    """Spend a token; return an OpenAI-shaped 429 if none left, else ``None``."""
    allowed, wait = _rate_limiter.try_acquire()
    if allowed:
        return None
    secs = max(1, round(wait))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(secs)},
        content={"error": {
            "message": (
                f"Rate limit exceeded (>{RATE_LIMIT_RPM:g} req/min). "
                f"Retry in {secs}s."
            ),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }},
    )

# Copilot's per-account chat socket doesn't tolerate concurrent conversations
# from one process (parallel requests error out or hang). This server bridges a
# single signed-in account, so we serialize upstream calls: concurrent HTTP
# requests queue here and run one at a time. Predictable, at the cost of
# parallelism — fine for a personal bridge.
_upstream_lock = threading.Lock()

# Lightweight request/reply tracing to session/bridge_debug.log (on by default;
# set BRIDGE_DEBUG=0 to silence). Used to see exactly what an agent client sends
# and gets back when a turn appears to hang.
_DEBUG = os.environ.get("BRIDGE_DEBUG", "1") == "1"


def _debug(msg: str) -> None:
    if not _DEBUG:
        return
    try:
        Path("session").mkdir(exist_ok=True)
        with open("session/bridge_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{time.time():.1f}  {msg}\n")
    except Exception:
        pass


# Coding-agent clients (openclaude) fire a *title-generation* meta-call alongside
# every real turn: a prompt like "Generate a concise, sentence-case title …". On
# a slow backend (Cowork ~20s/call) that meta-call doubles every turn's latency
# for zero user value, and — serialized — makes the real answer wait behind it.
# So we answer title/summary meta-calls locally and instantly, never upstream.
def _is_title_request(prompt: str) -> bool:
    p = prompt.lower()
    return "title" in p and (
        "sentence-case" in p or ("concise" in p and "generate" in p) or "summarize" in p
    )


def _canned_title(prompt: str) -> str:
    """A cheap title so the client has something to show, no upstream call."""
    # openclaude expects a JSON object with a "title" field.
    return '{"title": "New conversation"}'


def _stream_text(text: str, model: str, prompt="", include_usage=False):
    """Emit a fixed string as a one-shot OpenAI SSE stream (role/content/stop)."""
    cid = new_id()
    created = int(time.time())
    usage = usage_block(prompt, text)
    yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
    yield sse_event(stream_chunk(cid, created, model, {"content": text}))
    yield sse_event(stream_chunk(cid, created, model, {}, finish="stop",
                                 usage=None if include_usage else usage))
    if include_usage:
        yield sse_event(usage_chunk(cid, created, model, usage))
    yield "data: [DONE]\n\n"


def _stream(prompt: str, model: str, conversation_id=None, tools=None, messages=None, include_usage=False):
    """Yield OpenAI ``chat.completion.chunk`` SSE events for ``prompt``.

    ``conversation_id`` continues an existing Copilot thread; ``None`` starts a
    fresh one (its id is emitted on the final chunk).
    """
    cid = new_id()
    created = int(time.time())
    try:
        with _upstream_lock:  # one upstream chat at a time (released on disconnect)
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            stream = client.stream(prompt, conversation_id=conversation_id)
            # Pieces are buffered even while streamed live, so the full reply is
            # available for token counting (and tool-call parsing) at the end.
            buffered = []
            for piece in stream:
                if isinstance(piece, str) and piece:
                    buffered.append(piece)
                    if not tools:
                        yield sse_event(stream_chunk(cid, created, model, {"content": piece}))
            # Copilot's conversation id is known once the stream has run; emit it
            # on the final chunk so callers can track the upstream thread.
            text = "".join(buffered)
            if tools:
                _debug(f"REPLY(stream) consumer len={len(text)} :: {text[:120]!r}")
                yield from _stream_text_or_tool_calls(
                    cid, created, model, text, stream.conversation_id, tools, messages,
                    prompt=prompt, include_usage=include_usage,
                )
            else:
                usage = usage_block(prompt, text)
                yield sse_event(
                    stream_chunk(
                        cid, created, model, {}, finish="stop",
                        conversation_id=stream.conversation_id,
                        usage=None if include_usage else usage,
                    )
                )
                if include_usage:
                    yield sse_event(usage_chunk(cid, created, model, usage))
    except ClearanceRequired:
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {_CLEARANCE_HELP}]"}, finish="error")
        )
    except Exception as exc:  # surface errors to the client instead of hanging
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {exc}]"}, finish="error")
        )
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "microsoft"},
            {"id": WORK_MODEL_NAME, "object": "model", "created": 0, "owned_by": "microsoft"},
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    active_tools = req.tools if req.tools and tool_choice_allows_tools(req.tool_choice) else None
    prompt = messages_to_prompt(req.messages, active_tools, max_chars=MAX_PROMPT_CHARS)
    if not prompt.strip():
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "no text content in messages", "type": "invalid_request_error"}},
        )
    model = req.model or MODEL_NAME
    is_work = model == WORK_MODEL_NAME
    _debug(f"REQ model={model} stream={req.stream} nmsg={len(req.messages)} "
           f"roles={[m.role for m in req.messages]} tools={len(active_tools or [])} "
           f"prompt[:100]={prompt[:100]!r}")

    # Short-circuit the client's title/summary meta-call: answer locally so it
    # never spends an upstream turn (and never queues ahead of the real answer).
    if _is_title_request(prompt):
        title = _canned_title(prompt)
        _debug(f"SHORTCIRCUIT title-gen -> {title}")
        if req.stream:
            return StreamingResponse(
                _stream_text(title, model, prompt, _include_usage(req)),
                media_type="text/event-stream",
            )
        return completion_response(title, model, None, prompt_text=prompt)

    # Enforce the per-minute ceiling before touching the upstream lock, so excess
    # callers get a fast 429 instead of piling up behind the serialized queue.
    limited = _rate_limited_response()
    if limited is not None:
        return limited

    if req.stream:
        streamer = _stream_work if is_work else _stream
        return StreamingResponse(
            streamer(prompt, model, req.conversation_id, active_tools, req.messages, _include_usage(req)),
            media_type="text/event-stream",
        )

    if is_work:
        try:
            with _upstream_lock:  # serialize (see _stream_work): stable under an agent's bursts
                bot = _get_work_client()
                text = bot.ask(prompt, conversation_id=req.conversation_id)
            _debug(f"REPLY(non-stream) work len={len(text)} :: {text[:120]!r}")
        except Exception as exc:
            _debug(f"ERROR(non-stream) work: {exc}")
            return JSONResponse(
                status_code=502,
                content={"error": {"message": str(exc), "type": "upstream_error"}},
            )
        tool_calls = parse_tool_calls(text, active_tools) if active_tools else []
        if not tool_calls and active_tools:
            tool_calls = fallback_local_tool_calls(req.messages, active_tools, text)
        return completion_response(text, model, bot.last_conversation_id, tool_calls=tool_calls, prompt_text=prompt)

    try:
        with _upstream_lock:  # serialize: one upstream chat at a time
            reply = client.chat(prompt, conversation_id=req.conversation_id)
    except ClearanceRequired:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": _CLEARANCE_HELP, "type": "clearance_required"}},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )
    tool_calls = parse_tool_calls(reply.text, active_tools) if active_tools else []
    if not tool_calls and active_tools:
        tool_calls = fallback_local_tool_calls(req.messages, active_tools, reply.text)
    return completion_response(reply.text, model, reply.conversation_id, tool_calls=tool_calls, prompt_text=prompt)


@app.get("/")
def root():
    return {"service": "Copilot OpenAI-compatible API", "endpoints": ["/v1/models", "/v1/chat/completions"]}
