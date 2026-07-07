"""Flatten an OpenAI ``messages`` array into a single Copilot prompt.

Copilot's protocol has no role/system channel — it takes one prompt string per
turn — so we collapse the whole conversation into one piece of text.
"""

import json
from typing import Any, List, Optional, Sequence, Union

from .schemas import ChatMessage
from .tool_calls import build_tool_instructions


def content_text(content: Optional[Union[str, List[Any]]]) -> str:
    """Extract plain text from a message's content (string or content-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif part.get("type") == "tool_result":
                parts.append(content_text(part.get("content")))
            elif isinstance(part.get("text"), str):
                parts.append(part["text"])
        else:
            parts.append(str(part))
    return "\n".join(p for p in parts if p)


TRUNCATION_NOTICE = (
    "[Note: earlier turns of this conversation were dropped to fit the upstream "
    "prompt limit. Ask for a recap or re-read files if older context is needed.]"
)


def messages_to_prompt(
    messages: List[ChatMessage],
    tools: Optional[Sequence[Any]] = None,
    max_chars: Optional[int] = None,
) -> str:
    """Flatten an OpenAI ``messages`` array into a single Copilot prompt.

    When ``max_chars`` is set and the flattened prompt would exceed it, the
    oldest conversation lines are dropped (and oversized lines middle-cut) so
    the system prompt, tool instructions, and newest turns always survive.
    """
    system = "\n\n".join(
        content_text(m.content) for m in messages if m.role == "system" and m.content
    )
    tool_instructions = build_tool_instructions(tools)
    if system and tool_instructions:
        system = f"{system}\n\n{tool_instructions}"
    elif tool_instructions:
        system = tool_instructions

    convo = [m for m in messages if m.role != "system"]

    if len(convo) == 1 and convo[0].role == "user":
        lines = [content_text(convo[0].content)]  # simple single-turn request
        cue = None
    else:
        lines = []
        for m in convo:
            text = content_text(m.content)
            if m.role == "user":
                lines.append(f"User: {text}")
            elif m.role == "assistant":
                if text:
                    lines.append(f"Assistant: {text}")
                if m.tool_calls:
                    lines.append(f"Assistant tool calls: {_tool_calls_text(m.tool_calls)}")
            elif m.role == "tool":
                name = m.name or m.tool_call_id or "tool"
                lines.append(f"Tool result ({name}): {text}")
            else:
                lines.append(f"{m.role.title()}: {text}")
        cue = "Assistant:"  # cue Copilot to continue

    if max_chars and max_chars > 0:
        overhead = (len(system) + 2 if system else 0) + (len(cue) + 1 if cue else 0)
        budget = max_chars - overhead - (len(TRUNCATION_NOTICE) + 1)
        lines, truncated = _fit_lines(lines, budget)
        if truncated:
            lines.insert(0, TRUNCATION_NOTICE)

    if cue:
        lines.append(cue)
    body = "\n".join(lines)

    if system and body:
        return f"{system}\n\n{body}"
    return system or body


def _fit_lines(lines: List[str], budget: int) -> tuple:
    """Keep the newest lines that fit ``budget`` joined chars; report truncation.

    Oversized individual lines (huge tool results) are middle-cut rather than
    allowed to crowd out whole turns.
    """
    total = sum(len(line) for line in lines) + max(0, len(lines) - 1)
    if total <= budget:
        return lines, False
    if budget <= 0:
        return [], True

    # A single line may claim at most a third of the budget once we are over.
    per_line_cap = max(200, budget // 3)
    clamped = [_middle_cut(line, per_line_cap) for line in lines]

    kept: List[str] = []
    used = 0
    for line in reversed(clamped):  # newest first
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            if not kept:  # even the newest line alone is too big
                kept.append(_middle_cut(line, budget))
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return kept, True


def _middle_cut(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    marker = " ...[truncated]... "
    half = max(1, (limit - len(marker)) // 2)
    return f"{line[:half]}{marker}{line[-half:]}"


def _tool_calls_text(tool_calls: List[Any]) -> str:
    rendered = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = fn.get("name") or call.get("name") or "tool"
        arguments = fn.get("arguments", call.get("arguments", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                pass
        rendered.append(f"{name}({_safe_json(arguments)}) [id: {call.get('id', 'unknown')}]")
    return "; ".join(rendered)


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)
