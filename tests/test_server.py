"""Tests for server startup configuration."""

import os
import unittest
from unittest.mock import patch

from server import app
from server.api import chat_completions, list_models
from server.config import MODEL_NAME, MODEL_TONES
from server.schemas import ChatCompletionRequest


class ServerStartupTests(unittest.TestCase):
    @patch("uvicorn.run")
    @patch("copilot.auth.load_auth")
    def test_uses_default_address(self, _load_auth, run):
        with patch.dict(os.environ, {}, clear=True):
            app()

        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 8000)

    @patch("uvicorn.run")
    @patch("copilot.auth.load_auth")
    def test_uses_address_from_environment(self, _load_auth, run):
        with patch.dict(os.environ, {"HOST": "0.0.0.0", "PORT": "8080"}, clear=True):
            app()

        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["port"], 8080)

    @patch("uvicorn.run")
    @patch("copilot.auth.load_auth")
    def test_explicit_address_takes_precedence(self, _load_auth, run):
        with patch.dict(os.environ, {"HOST": "0.0.0.0", "PORT": "8080"}, clear=True):
            app(host="localhost", port=0)

        self.assertEqual(run.call_args.kwargs["host"], "localhost")
        self.assertEqual(run.call_args.kwargs["port"], 0)


class ModelSelectionTests(unittest.TestCase):
    def test_lists_all_models(self):
        self.assertEqual(
            [model["id"] for model in list_models()["data"]],
            list(MODEL_TONES),
        )

    @patch("server.api.client.chat")
    def test_maps_models_to_copilot_tones(self, chat):
        chat.return_value.text = "OK"
        chat.return_value.conversation_id = "conversation-id"

        for model, tone in MODEL_TONES.items():
            with self.subTest(model=model):
                response = chat_completions(ChatCompletionRequest(
                    model=model,
                    messages=[{"role": "user", "content": "Hello"}],
                ))
                self.assertEqual(response["model"], model)
                chat.assert_called_once_with(
                    "Hello", conversation_id=None, model=tone,
                )
                chat.reset_mock()

    @patch("server.api.client.chat")
    def test_defaults_to_reasoning(self, chat):
        chat.return_value.text = "OK"
        chat.return_value.conversation_id = "conversation-id"

        response = chat_completions(ChatCompletionRequest(
            messages=[{"role": "user", "content": "Hello"}],
        ))

        self.assertEqual(response["model"], "copilot-reasoning")
        self.assertEqual(MODEL_NAME, "copilot-reasoning")
        chat.assert_called_once_with(
            "Hello", conversation_id=None, model="Reasoning",
        )

    @patch("server.api.client.chat")
    def test_rejects_unknown_model_without_calling_copilot(self, chat):
        response = chat_completions(ChatCompletionRequest(
            model="not-a-model",
            messages=[{"role": "user", "content": "Hello"}],
        ))

        self.assertEqual(response.status_code, 400)
        chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
