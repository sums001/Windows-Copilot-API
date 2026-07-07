import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.api import app
from server.openai_format import completion_response
from server.prompt import TRUNCATION_NOTICE, messages_to_prompt
from server.schemas import ChatMessage
from server.tool_calls import (
    build_tool_instructions,
    fallback_local_tool_calls,
    parse_tool_calls,
    tool_choice_allows_tools,
)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a local file.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


class ToolCallTests(unittest.TestCase):
    def test_parse_raw_tool_call_block(self):
        calls = parse_tool_calls(
            'Tool calls requested:\n- Read({"file_path":"server/api.py"}) [id: call_read_api]',
            TOOLS,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_read_api")
        self.assertEqual(calls[0]["function"]["name"], "Read")
        self.assertEqual(calls[0]["function"]["arguments"], '{"file_path":"server/api.py"}')

    def test_parse_json_tool_call(self):
        calls = parse_tool_calls(
            '{"name":"Bash","arguments":{"command":"rg --files"}}',
            TOOLS,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "Bash")
        self.assertEqual(calls[0]["function"]["arguments"], '{"command":"rg --files"}')

    def test_parse_fenced_json_tool_call_with_nested_arguments(self):
        calls = parse_tool_calls(
            '```json\n{"name":"Read","arguments":{"file_path":"server/prompt.py"}}\n```',
            TOOLS,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "Read")
        self.assertEqual(calls[0]["function"]["arguments"], '{"file_path":"server/prompt.py"}')

    def test_ignores_unknown_tools(self):
        calls = parse_tool_calls(
            'Tool calls requested:\n- Unknown({"x":1}) [id: call_unknown]',
            TOOLS,
        )

        self.assertEqual(calls, [])

    def test_completion_response_can_return_tool_calls(self):
        tool_calls = parse_tool_calls(
            'Tool calls requested:\n- Read({"file_path":"README.md"}) [id: call_readme]',
            TOOLS,
        )
        response = completion_response("ignored", "copilot", "conv", tool_calls=tool_calls)
        choice = response["choices"][0]

        self.assertIsNone(choice["message"]["content"])
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "Read")

    def test_prompt_includes_tool_instructions_and_results(self):
        prompt = messages_to_prompt(
            [
                ChatMessage(role="system", content="You are a coding agent."),
                ChatMessage(role="user", content="Inspect the API."),
                ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_read_api",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": '{"file_path":"server/api.py"}',
                            },
                        }
                    ],
                ),
                ChatMessage(role="tool", tool_call_id="call_read_api", content="def chat_completions(...):"),
            ],
            TOOLS,
        )

        self.assertIn("Local tool use is available", prompt)
        self.assertIn("Assistant tool calls: Read", prompt)
        self.assertIn("Tool result (call_read_api): def chat_completions", prompt)

    def test_prompt_untouched_when_under_max_chars(self):
        messages = [
            ChatMessage(role="system", content="You are a coding agent."),
            ChatMessage(role="user", content="hello"),
        ]
        unlimited = messages_to_prompt(messages)
        capped = messages_to_prompt(messages, max_chars=10_000)

        self.assertEqual(unlimited, capped)
        self.assertNotIn(TRUNCATION_NOTICE, capped)

    def test_prompt_truncation_keeps_system_and_newest_turns(self):
        messages = [ChatMessage(role="system", content="SYSTEM RULES")]
        for i in range(40):
            messages.append(ChatMessage(role="user", content=f"question {i}: " + "x" * 500))
            messages.append(ChatMessage(role="assistant", content=f"answer {i}: " + "y" * 500))
        messages.append(ChatMessage(role="user", content="newest question"))

        prompt = messages_to_prompt(messages, max_chars=6_000)

        self.assertLessEqual(len(prompt), 6_000)
        self.assertIn("SYSTEM RULES", prompt)
        self.assertIn("newest question", prompt)
        self.assertIn(TRUNCATION_NOTICE, prompt)
        self.assertNotIn("question 0:", prompt)
        self.assertTrue(prompt.endswith("Assistant:"))

    def test_prompt_truncation_middle_cuts_huge_tool_result(self):
        messages = [
            ChatMessage(role="user", content="read the big file"),
            ChatMessage(role="tool", tool_call_id="call_big", content="A" * 50_000),
            ChatMessage(role="user", content="now summarize it"),
        ]

        prompt = messages_to_prompt(messages, max_chars=8_000)

        self.assertLessEqual(len(prompt), 8_000)
        self.assertIn("...[truncated]...", prompt)
        self.assertIn("now summarize it", prompt)

    def test_tool_choice_none_disables_tools(self):
        self.assertFalse(tool_choice_allows_tools("none"))
        self.assertFalse(tool_choice_allows_tools({"type": "none"}))
        self.assertTrue(tool_choice_allows_tools({"type": "auto"}))

    def test_tool_instructions_are_empty_without_tools(self):
        self.assertEqual(build_tool_instructions([]), "")

    def test_fallback_for_list_files_uses_glob(self):
        calls = fallback_local_tool_calls(
            [ChatMessage(role="user", content="list the files")],
            TOOLS + [{
                "type": "function",
                "function": {
                    "name": "Glob",
                    "description": "Find files by glob pattern.",
                    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                },
            }],
            "Workspace files:\nRoot (`/mnt/workspace`)\n- input/ empty",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "Glob")
        self.assertEqual(calls[0]["function"]["arguments"], '{"pattern":"**/*"}')

    def test_fallback_for_list_my_files_phrasing(self):
        glob_tool = [{
            "type": "function",
            "function": {
                "name": "Glob",
                "description": "Find files by glob pattern.",
                "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
            },
        }]
        calls = fallback_local_tool_calls(
            [ChatMessage(role="user", content="List my files.")],
            TOOLS + glob_tool,
            "",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "Glob")

    def test_fallback_detects_sandbox_persona_answer(self):
        glob_tool = [{
            "type": "function",
            "function": {
                "name": "Glob",
                "description": "Find files by glob pattern.",
                "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
            },
        }]
        calls = fallback_local_tool_calls(
            [ChatMessage(role="user", content="Anything in the workspace?")],
            TOOLS + glob_tool,
            "No files. input/, output/, working/ all empty. Nothing uploaded yet.",
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "Glob")

    def test_tool_instructions_override_sandbox_persona(self):
        instructions = build_tool_instructions(TOOLS)

        self.assertIn("real local machine", instructions)
        self.assertIn("upload sandbox", instructions)

    def test_fallback_does_not_repeat_after_tool_result(self):
        calls = fallback_local_tool_calls(
            [
                ChatMessage(role="user", content="list the files"),
                ChatMessage(role="tool", tool_call_id="call_glob", content="README.md"),
            ],
            TOOLS,
            "",
        )

        self.assertEqual(calls, [])

    def test_chat_completion_route_returns_structured_tool_call(self):
        client = TestClient(app)
        upstream = SimpleNamespace(
            text='Tool calls requested:\n- Read({"file_path":"server/api.py"}) [id: call_route_read]',
            conversation_id="conv_route",
        )

        with patch("server.api.client.chat", return_value=upstream):
            response = client.post("/v1/chat/completions", json={
                "model": "copilot",
                "messages": [{"role": "user", "content": "Read server/api.py"}],
                "tools": TOOLS,
            })

        self.assertEqual(response.status_code, 200)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "Read")

    def test_chat_completion_route_falls_back_to_local_tool_call(self):
        client = TestClient(app)
        upstream = SimpleNamespace(
            text="Workspace files:\nRoot (`/mnt/workspace`)\n- input/ empty",
            conversation_id="conv_route",
        )

        with patch("server.api.client.chat", return_value=upstream):
            response = client.post("/v1/chat/completions", json={
                "model": "copilot",
                "messages": [{"role": "user", "content": "list the files"}],
                "tools": TOOLS + [{
                    "type": "function",
                    "function": {
                        "name": "Glob",
                        "description": "Find files by glob pattern.",
                        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                    },
                }],
            })

        self.assertEqual(response.status_code, 200)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "Glob")

    def test_streaming_route_falls_back_to_local_tool_call(self):
        class FakeStream:
            conversation_id = "conv_stream"

            def __iter__(self):
                yield "Workspace files:\nRoot (`/mnt/workspace`)\n- input/ empty"

        client = TestClient(app)
        with patch("server.api.client.stream", return_value=FakeStream()):
            with client.stream("POST", "/v1/chat/completions", json={
                "model": "copilot",
                "stream": True,
                "messages": [{"role": "user", "content": "list the files"}],
                "tools": TOOLS + [{
                    "type": "function",
                    "function": {
                        "name": "Glob",
                        "description": "Find files by glob pattern.",
                        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                    },
                }],
            }) as response:
                body = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('"finish_reason": "tool_calls"', body)
        self.assertIn('"name": "Glob"', body)


if __name__ == "__main__":
    unittest.main()
