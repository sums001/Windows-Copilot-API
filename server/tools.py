"""Prompt-based function calling for Copilot's text-only consumer protocol."""

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union


class ToolCallParseError(ValueError):
    """Copilot attempted a tool call but returned an invalid payload."""


@dataclass
class ToolParseResult:
    text: str
    calls: List[dict]


def tools_enabled(tools: Optional[List[Dict[str, Any]]], tool_choice=None) -> bool:
    return bool(tools) and tool_choice != "none"


def tool_instructions(
    tools: List[Dict[str, Any]],
    tool_choice=None,
    parallel_tool_calls: bool = True,
) -> str:
    """Describe the available OpenAI tools and the bridge's JSON call format."""
    lines = [
        "You can request function tools through an OpenAI-compatible client.",
        "When a tool would help, or when the user asks you to use one, respond with ONLY this JSON object:",
        '{"tool_calls":[{"name":"function_name","arguments":{"argument_name":"value"}}]}',
        "Use only function names from the available tools. Arguments must be a JSON object that matches the function's parameters.",
        "Do not wrap the JSON in markdown, add commentary, claim a tool ran, or invent its result.",
        "You may request multiple tools in one response when they can run independently."
        if parallel_tool_calls else "Request at most one tool in your response.",
        "If no tool is needed, answer the user normally without a tool_calls JSON object.",
        "Tool results appear later as messages labeled 'Tool result'. Use them to continue the task.",
    ]
    if tool_choice == "required":
        lines.append("You must request at least one tool in your next response.")
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            lines.append(f"Your next response must call the function named {name!r}.")
    lines.append("Available tools (OpenAI JSON schemas):")
    lines.append(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def _json_payload(text: str) -> Optional[Tuple[dict, str]]:
    stripped = text.strip()
    normal_text = ""
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.I | re.S)
    if fenced:
        stripped = fenced.group(1)
    else:
        mixed = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.I | re.S)
        if mixed and '"tool_calls"' in mixed.group(1):
            normal_text = (stripped[:mixed.start()] + stripped[mixed.end():]).strip()
            stripped = mixed.group(1)
    try:
        payload = json.loads(stripped)
    except ValueError as exc:
        if '"tool_calls"' in stripped:
            raise ToolCallParseError("Copilot returned malformed tool-call JSON") from exc
        return None
    if isinstance(payload, list):
        payload = {"tool_calls": payload}
    elif isinstance(payload, dict) and "tool_calls" not in payload and "name" in payload:
        payload = {"tool_calls": [payload]}
    if not isinstance(payload, dict) or "tool_calls" not in payload:
        return None
    return payload, normal_text


class FunctionCallParser:
    """Parse Copilot's JSON into OpenAI calls while preserving normal text."""

    def __init__(
        self,
        tools: Optional[List[Dict[str, Any]]],
        tool_choice=None,
        parallel_tool_calls: bool = True,
    ):
        self.tools = tools or []
        self.tool_choice = tool_choice
        self.parallel_tool_calls = parallel_tool_calls
        self.allowed = {
            (tool.get("function") or {}).get("name")
            for tool in self.tools
            if isinstance(tool, dict) and tool.get("type", "function") == "function"
        }
        self.required_name = None
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            self.required_name = (tool_choice.get("function") or {}).get("name")
        self.required = tool_choice == "required" or bool(self.required_name)

    def parse_non_stream(self, text: str) -> ToolParseResult:
        if not tools_enabled(self.tools, self.tool_choice):
            return ToolParseResult(text, [])
        decoded = _json_payload(text)
        if decoded is None:
            if self.required:
                raise ToolCallParseError("Copilot did not make the required tool call")
            return ToolParseResult(text, [])
        payload, normal_text = decoded
        requested = payload["tool_calls"]
        if not isinstance(requested, list) or not requested:
            raise ToolCallParseError("Copilot returned an empty or invalid tool_calls list")
        if not self.parallel_tool_calls and len(requested) > 1:
            raise ToolCallParseError("Copilot returned parallel calls despite parallel_tool_calls=false")

        calls = []
        for item in requested:
            if not isinstance(item, dict):
                raise ToolCallParseError("Copilot returned a non-object tool call")
            function = item.get("function") if isinstance(item.get("function"), dict) else item
            name = function.get("name")
            if name not in self.allowed:
                raise ToolCallParseError(f"Copilot requested an unavailable tool: {name!r}")
            if self.required_name and name != self.required_name:
                raise ToolCallParseError(
                    f"Copilot called {name!r} instead of required tool {self.required_name!r}"
                )
            arguments: Union[str, Dict[str, Any]] = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError as exc:
                    raise ToolCallParseError(f"Tool {name!r} returned non-JSON arguments") from exc
            if not isinstance(arguments, dict):
                raise ToolCallParseError(f"Tool {name!r} arguments must be a JSON object")
            calls.append({
                "id": f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                },
            })
        return ToolParseResult(normal_text, calls)


def parse_tool_calls(
    text: str,
    tools: Optional[List[Dict[str, Any]]],
    tool_choice=None,
) -> Optional[List[dict]]:
    """Compatibility wrapper returning only calls, or None for plain text."""
    result = FunctionCallParser(tools, tool_choice).parse_non_stream(text)
    return result.calls or None
