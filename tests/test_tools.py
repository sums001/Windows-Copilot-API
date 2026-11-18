"""Tests for the OpenAI-compatible tool-calling bridge."""

import json
import unittest
from unittest.mock import patch

from server.api import _stream, chat_completions
from server.prompt import messages_to_prompt
from server.schemas import ChatCompletionRequest
from server.tools import FunctionCallParser, ToolCallParseError, parse_tool_calls


TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a workspace text file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}]


class FakeStream:
    conversation_id = "conversation-id"

    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)


def request(messages, stream=False, tool_choice=None):
    return ChatCompletionRequest(
        model="copilot-reasoning",
        messages=messages,
        stream=stream,
        tools=TOOLS,
        tool_choice=tool_choice,
    )


def sse_payloads(events):
    return [
        json.loads(event[len("data: "):])
        for event in events
        if event.startswith("data: {")
    ]


class ToolCallParsingTests(unittest.TestCase):
    def test_builds_openai_tool_call(self):
        calls = parse_tool_calls(
            '{"tool_calls":[{"name":"read_file","arguments":{"path":"README.md"}}]}',
            TOOLS,
        )

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["id"].startswith("call_"))
        self.assertEqual(calls[0]["type"], "function")
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"path": "README.md"},
        )

    def test_accepts_fenced_json_and_string_arguments(self):
        calls = parse_tool_calls(
            '```json\n{"tool_calls":[{"name":"read_file",'
            '"arguments":"{\\"path\\":\\"README.md\\"}"}]}\n```',
            TOOLS,
        )

        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"path": "README.md"},
        )

    def test_plain_answer_is_not_a_tool_call(self):
        self.assertIsNone(parse_tool_calls("The answer is 42.", TOOLS))

    def test_tool_choice_none_disables_calls(self):
        text = '{"tool_calls":[{"name":"read_file","arguments":{}}]}'
        self.assertIsNone(parse_tool_calls(text, TOOLS, tool_choice="none"))

    def test_rejects_invalid_tool_calls(self):
        invalid = [
            '{"tool_calls":[{"name":"unknown","arguments":{}}]}',
            '{"tool_calls":[{"name":"read_file","arguments":[]}]}',
            '{"tool_calls":[{"name":"read_file","arguments":"not json"}]}',
            '{"tool_calls":[]}',
            '{"tool_calls":[{"name":"read_file"}',
        ]
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(ToolCallParseError):
                    parse_tool_calls(text, TOOLS)

    def test_preserves_normal_text_with_fenced_tool_call(self):
        result = FunctionCallParser(TOOLS).parse_non_stream(
            'I will inspect the file.\n```json\n'
            '{"tool_calls":[{"name":"read_file","arguments":{"path":"README.md"}}]}'
            '\n```'
        )

        self.assertEqual(result.text, "I will inspect the file.")
        self.assertEqual(result.calls[0]["function"]["name"], "read_file")

    def test_required_tool_choice_rejects_plain_text(self):
        with self.assertRaises(ToolCallParseError):
            FunctionCallParser(TOOLS, tool_choice="required").parse_non_stream("No call")

    def test_named_tool_choice_rejects_a_different_call(self):
        text = '{"tool_calls":[{"name":"read_file","arguments":{}}]}'
        choice = {"type": "function", "function": {"name": "another_tool"}}

        with self.assertRaises(ToolCallParseError):
            FunctionCallParser(TOOLS, tool_choice=choice).parse_non_stream(text)

    def test_rejects_parallel_calls_when_disabled(self):
        text = '{"tool_calls":[' \
            '{"name":"read_file","arguments":{"path":"one.txt"}},' \
            '{"name":"read_file","arguments":{"path":"two.txt"}}]}'

        with self.assertRaises(ToolCallParseError):
            FunctionCallParser(TOOLS, parallel_tool_calls=False).parse_non_stream(text)


class ToolPromptTests(unittest.TestCase):
    def test_preserves_tool_definitions_calls_and_results(self):
        req = request([
            {"role": "user", "content": "Read the README"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "name": "read_file",
                "content": "FILE_CONTENT_SENTINEL",
            },
        ])

        prompt = messages_to_prompt(req.messages, req.tools, req.tool_choice)

        self.assertIn('"name":"read_file"', prompt)
        self.assertIn('"description":"Read a workspace text file."', prompt)
        self.assertIn("call_123", prompt)
        self.assertIn("Tool result", prompt)
        self.assertIn("FILE_CONTENT_SENTINEL", prompt)

    def test_tool_choice_none_omits_tool_instructions(self):
        req = request([{"role": "user", "content": "Hello"}], tool_choice="none")

        prompt = messages_to_prompt(req.messages, req.tools, req.tool_choice)

        self.assertNotIn("Available tools", prompt)


class ToolResponseTests(unittest.TestCase):
    def test_nonstreaming_response_returns_tool_calls(self):
        response_text = (
            '{"tool_calls":[{"name":"read_file",'
            '"arguments":{"path":"README.md"}}]}'
        )
        with patch("server.api._rate_limited_response", return_value=None), \
                patch("server.api.client.chat") as chat:
            chat.return_value.text = response_text
            chat.return_value.conversation_id = "conversation-id"
            response = chat_completions(request([
                {"role": "user", "content": "Read the README"},
            ]))

        choice = response["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "read_file")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "README.md"})
        self.assertIn("Available tools", chat.call_args.args[0])

    def test_streaming_response_returns_tool_call_delta(self):
        chunks = [
            '{"tool_calls":[{"name":"read_file",',
            '"arguments":{"path":"README.md"}}]}',
        ]
        with patch("server.api.client.stream", return_value=FakeStream(chunks)) as stream:
            events = list(_stream(
                "Read the README", "copilot-reasoning", tools=TOOLS,
            ))

        payloads = sse_payloads(events)
        call_chunks = [p for p in payloads if "tool_calls" in p["choices"][0]["delta"]]
        self.assertEqual(len(call_chunks), 1)
        call = call_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["index"], 0)
        self.assertEqual(call["function"]["name"], "read_file")
        self.assertEqual(payloads[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(payloads[-1]["conversation_id"], "conversation-id")
        self.assertFalse(any(
            "tool_calls" in p["choices"][0]["delta"].get("content", "")
            for p in payloads
        ))
        self.assertEqual(stream.call_args.kwargs["model"], "Reasoning")
        self.assertEqual(events[-1], "data: [DONE]\n\n")

    def test_tool_enabled_stream_still_returns_plain_answers(self):
        with patch("server.api.client.stream", return_value=FakeStream(["plain ", "answer"])):
            events = list(_stream(
                "Hello", "copilot-reasoning", tools=TOOLS,
            ))

        payloads = sse_payloads(events)
        content = "".join(
            p["choices"][0]["delta"].get("content", "") for p in payloads
        )
        self.assertEqual(content, "plain answer")
        self.assertEqual(payloads[-1]["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
