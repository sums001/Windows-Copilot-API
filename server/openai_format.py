"""Builders for the OpenAI wire shapes (completions, SSE chunks)."""

import json
import time
import uuid

# Token counting for the `usage` block. Copilot's protocols report no usage, so
# the bridge counts locally: tiktoken when installed (accurate), else a ~4
# chars/token estimate — close enough for client-side cost displays.
_encoder = None
_encoder_tried = False


def _get_encoder():
    global _encoder, _encoder_tried
    if not _encoder_tried:
        _encoder_tried = True
        try:
            import tiktoken
            _encoder = tiktoken.get_encoding("o200k_base")
        except Exception:
            _encoder = None
    return _encoder


def count_tokens(text: str) -> int:
    """Token count of ``text`` — tiktoken if available, else len/4 estimate."""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, (len(text) + 3) // 4)


def usage_block(prompt_text: str, completion_text: str) -> dict:
    """An OpenAI ``usage`` object counted from the raw prompt/reply text."""
    prompt_tokens = count_tokens(prompt_text)
    completion_tokens = count_tokens(completion_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def new_id() -> str:
    """A fresh ``chatcmpl-...`` id, as the OpenAI API returns."""
    return f"chatcmpl-{uuid.uuid4().hex}"


def completion_response(text: str, model: str, conversation_id=None, tool_calls=None, prompt_text="") -> dict:
    """A non-streaming ``chat.completion`` object.

    ``conversation_id`` is Copilot's own conversation id, surfaced as an extra
    top-level field (not part of OpenAI's schema, so standard clients ignore it)
    for callers that want to track the upstream thread.
    """
    message = {"role": "assistant", "content": None if tool_calls else text}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "id": new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage_block(prompt_text, text),
    }


def sse_event(payload: dict) -> str:
    """Serialize a payload as a Server-Sent Events ``data:`` line."""
    return f"data: {json.dumps(payload)}\n\n"


def stream_chunk(
    cid: str, created: int, model: str, delta: dict, finish=None, conversation_id=None, usage=None
) -> dict:
    """A single ``chat.completion.chunk`` object for streaming responses.

    ``conversation_id`` (Copilot's upstream id) is added as an extra top-level
    field when known — typically only on the final chunk, since a new
    conversation's id isn't available until the stream has started.
    ``usage`` (a :func:`usage_block`) is attached when given — on the final
    chunk, so clients that read ``chunk.usage`` see it exactly once.
    """
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if conversation_id is not None:
        chunk["conversation_id"] = conversation_id
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def usage_chunk(cid: str, created: int, model: str, usage: dict) -> dict:
    """The standalone usage chunk OpenAI sends when ``include_usage`` is set.

    Per spec it has an empty ``choices`` list and arrives after the finish
    chunk, just before ``[DONE]``.
    """
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": usage,
    }
