"""Compatibility helpers for OpenAI-style local tool calls.

Copilot only returns text, while coding-agent clients such as OpenClaude expect
OpenAI ``tool_calls`` objects. These helpers add a small text protocol around
Copilot and translate matching replies back into structured tool calls.
"""

import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


RAW_TOOL_PREFIX = "Tool calls requested:"
_TOOL_LINE_RE = re.compile(
    r"^-\s*([A-Za-z_][A-Za-z0-9_.-]*)\(([\s\S]*)\)\s*\[id:\s*([^\]\s]+)\]\s*$"
)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", re.I)
_XML_TOOL_RE = re.compile(r"<tool_call>([\s\S]*?)(?:</tool_call>|$)", re.I)
_XML_FUNCTION_RE = re.compile(r"<function=([^>\s]+)\s*>", re.I)
_XML_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>([\s\S]*?)</parameter>", re.I)


def tool_choice_allows_tools(tool_choice: Any) -> bool:
    """Return whether an OpenAI ``tool_choice`` value permits tool use."""
    if tool_choice is None:
        return True
    if isinstance(tool_choice, str):
        return tool_choice.lower() != "none"
    if isinstance(tool_choice, dict):
        return str(tool_choice.get("type", "")).lower() != "none"
    return True


def build_tool_instructions(tools: Optional[Sequence[Any]]) -> str:
    """Build concise prompt instructions that teach text-only Copilot tool use."""
    specs = list(_iter_tool_specs(tools or []))
    if not specs:
        return ""

    lines = [
        "Local tool use is available through the client.",
        "You are connected to the user's real local machine and workspace through these tools.",
        "Ignore any built-in environment you believe you have, such as an upload sandbox with input/, output/, or working/ directories - it does not exist here and the user cannot upload files.",
        "When you need to inspect or modify local files, search the workspace, or run commands, request a tool instead of guessing.",
        "Never answer questions about local files, directories, or repository state from memory; request a tool first.",
        "If you need a tool, reply with only this exact format:",
        RAW_TOOL_PREFIX,
        '- ToolName({"arg":"value"}) [id: call_short_unique_id]',
        "Use JSON object arguments that match the selected tool. Do not claim you checked files until a Tool result appears in the conversation.",
        "",
        "Available tools:",
    ]

    for name, description, parameters in specs[:40]:
        lines.append(f"- {name}: {_truncate(description, 500)}")
        if parameters:
            lines.append(f"  parameters: {_truncate(_json(parameters), 1500)}")
    return "\n".join(lines)


def parse_tool_calls(text: str, tools: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
    """Parse Copilot text into OpenAI ``tool_calls`` objects."""
    allowed = {name for name, _, _ in _iter_tool_specs(tools or [])}
    calls: List[Tuple[Optional[str], str, Any]] = []
    calls.extend(_parse_raw_tool_block(text))
    calls.extend(_parse_json_tool_calls(text))
    calls.extend(_parse_xml_tool_calls(text))

    result: List[Dict[str, Any]] = []
    seen = set()
    for call_id, name, arguments in calls:
        if allowed and name not in allowed:
            continue
        args = _coerce_arguments(arguments)
        key = (name, _json(args))
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "id": call_id or _new_tool_call_id(),
            "type": "function",
            "function": {
                "name": name,
                "arguments": _json(args),
            },
        })
    return result


def fallback_local_tool_calls(messages: Sequence[Any], tools: Sequence[Any], assistant_text: str = "") -> List[Dict[str, Any]]:
    """Force a read-only local tool call when the upstream answers without one.

    Some text-only backends ignore tool-use instructions and answer from stale
    environment context. For obvious local-inspection requests, returning a
    tool call is safer than passing through a guessed workspace answer.
    """
    if not tools or _has_tool_result_after_last_user(messages):
        return []

    user_text = _last_user_text(messages).lower()
    if not user_text:
        return []

    if _asks_to_list_files(user_text) or _looks_like_stale_workspace_answer(assistant_text):
        if _has_tool(tools, "Glob"):
            return [_tool_call("Glob", {"pattern": "**/*"})]
        if _has_tool(tools, "Bash"):
            return [_tool_call("Bash", {
                "command": "pwd; find . -maxdepth 2 -print | sort",
                "description": "List files in current directory",
            })]

    if _asks_for_git_status(user_text) and _has_tool(tools, "Bash"):
        return [_tool_call("Bash", {
            "command": "git status --short",
            "description": "Show working tree status",
        })]

    if _asks_for_current_directory(user_text) and _has_tool(tools, "Bash"):
        return [_tool_call("Bash", {
            "command": "pwd",
            "description": "Show current directory",
        })]

    return []


def _iter_tool_specs(tools: Iterable[Any]) -> Iterable[Tuple[str, str, Any]]:
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = fn.get("description") if isinstance(fn.get("description"), str) else ""
        parameters = fn.get("parameters") or fn.get("input_schema") or {}
        yield name, description, parameters


def _tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _new_tool_call_id(),
        "type": "function",
        "function": {
            "name": name,
            "arguments": _json(arguments),
        },
    }


def _has_tool(tools: Sequence[Any], name: str) -> bool:
    return any(tool_name == name for tool_name, _, _ in _iter_tool_specs(tools))


def _last_user_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        role = getattr(message, "role", None)
        if role != "user":
            continue
        return _content_text(getattr(message, "content", None))
    return ""


def _has_tool_result_after_last_user(messages: Sequence[Any]) -> bool:
    for message in reversed(messages):
        role = getattr(message, "role", None)
        if role == "tool":
            return True
        if role == "user":
            content = getattr(message, "content", None)
            if _content_contains_tool_result(content):
                return True
            return False
    return False


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") == "tool_result":
                    parts.append(_content_text(part.get("content")))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _content_contains_tool_result(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "tool_result"
        for part in content
    )


_LIST_FILES_RE = re.compile(
    r"\b(?:list|show|see|what)\b[\w\s'\",:-]{0,40}\bfiles\b"
)


def _asks_to_list_files(text: str) -> bool:
    compact = " ".join(text.split())
    return (
        bool(_LIST_FILES_RE.search(compact))
        or compact in {"ls", "dir", "tree"}
        or compact.startswith("ls ")
        or compact.startswith("dir ")
        or compact.startswith("tree ")
    )


def _asks_for_git_status(text: str) -> bool:
    compact = " ".join(text.split())
    return "git status" in compact or compact in {"status", "repo status"}


def _asks_for_current_directory(text: str) -> bool:
    compact = " ".join(text.split())
    return compact in {"pwd", "where am i", "current directory", "working directory"}


def _looks_like_stale_workspace_answer(text: str) -> bool:
    lowered = text.lower()
    if "/mnt/workspace" in lowered:
        return True
    if any(
        marker in lowered
        for marker in ("no user files uploaded", "nothing uploaded", "no files uploaded")
    ):
        return True
    # Copilot's sandbox persona describes input/, output/, working/ directories.
    sandbox_dirs = sum(d in lowered for d in ("input/", "output/", "working/"))
    return sandbox_dirs >= 2 and ("empty" in lowered or "upload" in lowered)


def _parse_raw_tool_block(text: str) -> List[Tuple[Optional[str], str, Any]]:
    idx = text.find(RAW_TOOL_PREFIX)
    if idx == -1:
        return []
    calls = []
    body = text[idx + len(RAW_TOOL_PREFIX):]
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        match = _TOOL_LINE_RE.match(line)
        if not match:
            if calls:
                break
            continue
        name, raw_args, call_id = match.groups()
        calls.append((call_id, name, _coerce_arguments(raw_args)))
    return calls


def _parse_json_tool_calls(text: str) -> List[Tuple[Optional[str], str, Any]]:
    calls: List[Tuple[Optional[str], str, Any]] = []
    for match in _FENCED_JSON_RE.finditer(text):
        calls.extend(_calls_from_json_text(match.group(1).strip()))

    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        raw = _extract_balanced_json(text, start)
        if raw:
            calls.extend(_calls_from_json_text(raw))
            pos = start + len(raw)
        else:
            pos = start + 1
    return calls


def _parse_xml_tool_calls(text: str) -> List[Tuple[Optional[str], str, Any]]:
    calls = []
    for block in _XML_TOOL_RE.finditer(text):
        inner = block.group(1) or ""
        fn_match = _XML_FUNCTION_RE.search(inner)
        if not fn_match:
            continue
        name = fn_match.group(1)
        args = {
            key: _coerce_xml_value(value)
            for key, value in _XML_PARAM_RE.findall(inner)
        }
        calls.append((None, name, args))
    return calls


def _calls_from_json_text(raw: str) -> List[Tuple[Optional[str], str, Any]]:
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return _calls_from_json_value(parsed)


def _calls_from_json_value(value: Any) -> List[Tuple[Optional[str], str, Any]]:
    if isinstance(value, list):
        calls: List[Tuple[Optional[str], str, Any]] = []
        for item in value:
            calls.extend(_calls_from_json_value(item))
        return calls
    if not isinstance(value, dict):
        return []

    if isinstance(value.get("tool_calls"), list):
        return _calls_from_json_value(value["tool_calls"])

    call_id = value.get("id") if isinstance(value.get("id"), str) else None
    if isinstance(value.get("name"), str):
        return [(call_id, value["name"], value.get("arguments", value.get("input", {})))]

    fn = value.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return [(call_id, fn["name"], fn.get("arguments", {}))]
    return []


def _coerce_arguments(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _coerce_xml_value(raw: str) -> Any:
    stripped = raw.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except Exception:
        return stripped


def _extract_balanced_json(text: str, start: int) -> Optional[str]:
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def _new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
