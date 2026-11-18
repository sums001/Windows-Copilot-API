"""Tests for workspace file tools and their MCP transport."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.workspace_mcp import MCPServer
from server.workspace_tools import ToolError, WorkspaceTools


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self._temp.name).resolve()
        self.tools = WorkspaceTools(str(self.root))

    def tearDown(self):
        self._temp.cleanup()

    def test_create_edit_and_delete_file(self):
        self.tools.create_directory("nested/folder")
        self.tools.create_file("nested/folder/example.txt", "before\r\n")
        target = self.root / "nested" / "folder" / "example.txt"
        self.assertEqual(target.read_bytes(), b"before\r\n")

        result = self.tools.edit_file(
            "nested/folder/example.txt", "before", "after",
        )
        self.assertIn("replacements=1", result)
        self.assertEqual(target.read_bytes(), b"after\r\n")

        self.tools.delete_file("nested/folder/example.txt")
        self.assertFalse(target.exists())

    def test_create_file_never_overwrites(self):
        self.tools.create_file("example.txt", "original")

        with self.assertRaises(ToolError):
            self.tools.create_file("example.txt", "replacement")

        self.assertEqual((self.root / "example.txt").read_text(), "original")

    def test_edit_requires_unique_match_unless_replace_all(self):
        self.tools.create_file("example.txt", "same same")

        with self.assertRaises(ToolError):
            self.tools.edit_file("example.txt", "same", "new")
        self.tools.edit_file("example.txt", "same", "new", replace_all=True)

        self.assertEqual((self.root / "example.txt").read_text(), "new new")

    def test_rejects_paths_outside_workspace(self):
        for path in ("../outside.txt", str(self.root.parent / "outside.txt")):
            with self.subTest(path=path):
                with self.assertRaises(ToolError):
                    self.tools.create_file(path, "must not be written")

    def test_rejects_invalid_powershell_timeout(self):
        for timeout in (0, 601, True, "10"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ToolError):
                    self.tools.powershell("Get-Location", timeout=timeout)

    @patch("server.workspace_tools.subprocess.run")
    @patch("server.workspace_tools.shutil.which", return_value="/usr/local/bin/pwsh")
    def test_powershell_uses_noninteractive_utf8_command(self, _which, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        result = json.loads(self.tools.powershell("Write-Output 'ok'", timeout=30))

        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["stdout"], "ok\n")
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:5], [
            "/usr/local/bin/pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
        ])
        self.assertIn("[System.Text.Encoding]::UTF8", arguments[5])
        self.assertIn("Write-Output 'ok'", arguments[5])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.root))
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    @patch("server.workspace_tools.shutil.which")
    def test_windows_powershell_fallback(self, which):
        which.side_effect = lambda name: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" if name == "powershell" else None
        with patch("server.workspace_tools.os.name", "nt"):
            executable = self.tools._powershell_executable()

        self.assertTrue(executable.endswith("powershell.exe"))


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self._temp.name).resolve()

    def tearDown(self):
        self._temp.cleanup()

    def test_initialize_discover_and_call(self):
        server = MCPServer(str(self.root))
        initialized = server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        called = server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "create_file", "arguments": {"path": "from-mcp.txt", "content": "works"}},
        })

        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(listed["result"]["tools"]), 5)
        self.assertFalse(called["result"]["isError"])
        self.assertEqual((self.root / "from-mcp.txt").read_text(), "works")

    def test_tool_errors_are_returned_to_the_model(self):
        server = MCPServer(str(self.root))

        response = server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "delete_file", "arguments": {"path": "missing.txt"}},
        })

        self.assertTrue(response["result"]["isError"])
        self.assertIn("does not exist", response["result"]["content"][0]["text"])

    def test_stdio_newline_delimited_protocol(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "create_directory", "arguments": {"path": "stdio/nested"},
            }},
        ]
        payload = b"".join(json.dumps(message).encode() + b"\n" for message in messages)

        completed = subprocess.run(
            [sys.executable, "-m", "server.workspace_mcp", "--workspace", str(self.root)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertFalse(responses[-1]["result"]["isError"])
        self.assertTrue((self.root / "stdio" / "nested").is_dir())


if __name__ == "__main__":
    unittest.main()
