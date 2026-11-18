"""Tests for VS Code SessionStart MCP registration."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.vscode import (
    SERVER_NAME,
    ensure_vscode_mcp,
    install_session_start_hook,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class VSCodeConfigTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self._temp.name).resolve()

    def tearDown(self):
        self._temp.cleanup()

    def test_creates_config_only_when_absent(self):
        config_path, created = ensure_vscode_mcp(str(self.root))

        self.assertTrue(created)
        self.assertEqual(config_path, self.root / ".vscode" / "mcp.json")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["servers"][SERVER_NAME], {
            "type": "stdio",
            "command": str(Path(sys.executable).resolve()),
            "args": [
                "-m",
                "server.workspace_mcp",
                "--workspace",
                str(self.root),
            ],
            "cwd": str(PROJECT_ROOT),
        })

        original = config_path.read_bytes()
        self.assertEqual(ensure_vscode_mcp(str(self.root)), (config_path, False))
        self.assertEqual(config_path.read_bytes(), original)

    def test_existing_config_is_not_parsed_or_modified(self):
        config_path = self.root / ".vscode" / "mcp.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("not json", encoding="utf-8")

        self.assertEqual(ensure_vscode_mcp(str(self.root)), (config_path, False))

        self.assertEqual(config_path.read_text(encoding="utf-8"), "not json")

    def test_session_hook_uses_workspace_from_event(self):
        workspace = self.root / "hook-workspace"
        workspace.mkdir()
        event = {"sessionId": "session-id", "source": "new", "cwd": str(workspace)}

        completed = subprocess.run(
            [sys.executable, "-m", "server.vscode"],
            cwd=str(PROJECT_ROOT),
            input=json.dumps(event),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "{}")
        self.assertIn("Workspace MCP config created", completed.stderr)
        self.assertTrue((workspace / ".vscode" / "mcp.json").is_file())

    def test_installs_user_session_hook_idempotently(self):
        copilot_home = self.root / "copilot-home"
        with patch.dict(os.environ, {"COPILOT_HOME": str(copilot_home)}):
            hook_path, changed = install_session_start_hook()
            self.assertTrue(changed)
            original = hook_path.read_bytes()
            self.assertEqual(install_session_start_hook(), (hook_path, False))

        self.assertEqual(hook_path.read_bytes(), original)
        config = json.loads(original)
        hook = config["hooks"]["sessionStart"][0]
        self.assertEqual(config["version"], 1)
        self.assertEqual(hook["type"], "command")
        self.assertIn("-m server.vscode", hook["command"])
        self.assertEqual(hook["cwd"], str(PROJECT_ROOT))


if __name__ == "__main__":
    unittest.main()
