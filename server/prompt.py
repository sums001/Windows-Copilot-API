"""Flatten OpenAI messages and tool turns into a single Copilot prompt.

Copilot's protocol has no role/system channel — it takes one prompt string per
turn — so we collapse the whole conversation into one piece of text.
"""

import json
from typing import Any, Dict, List, Optional, Union

from .schemas import ChatMessage
from .tools import tool_instructions, tools_enabled


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
        else:
            parts.append(str(part))
    return "\n".join(p for p in parts if p)


def _tool_calls_text(message: ChatMessage) -> str:
    calls = []
    for call in message.tool_calls or []:
        calls.append({
            "id": call.id,
            "name": call.function.name,
            "arguments": call.function.arguments,
        })
    return json.dumps({"tool_calls": calls}, ensure_ascii=False)


def messages_to_prompt(
    messages: List[ChatMessage],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice=None,
    parallel_tool_calls: bool = True,
) -> str:
    """Flatten messages, available tools, and tool results into one prompt."""
    system_parts = [
        content_text(m.content) for m in messages
        if m.role == "system" and m.content
    ]
    if tools_enabled(tools, tool_choice):
        system_parts.insert(0, tool_instructions(tools, tool_choice, parallel_tool_calls))
    system = "\n\n".join(system_parts)
    convo = [m for m in messages if m.role != "system"]

    if len(convo) == 1 and convo[0].role == "user":
        body = content_text(convo[0].content)
    else:
        lines = []
        for message in convo:
            if message.role == "tool":
                lines.append(
                    f"Tool result (name={message.name or 'unknown'}, "
                    f"tool_call_id={message.tool_call_id or 'unknown'}):"
                )
                lines.append(content_text(message.content))
            elif message.role == "assistant" and message.tool_calls:
                lines.append("Assistant previously requested these tool calls:")
                lines.append(_tool_calls_text(message))
                if message.content:
                    lines.append(f"Assistant text: {content_text(message.content)}")
            else:
                label = "User" if message.role == "user" else "Assistant"
                lines.append(f"{label}: {content_text(message.content)}")
        lines.append("Assistant:")
        body = "\n".join(lines)

    if system and body:
        return f"{system}\n\n{body}"
    return system or body
