"""Dependency-free MCP stdio server for VS Code workspace tools."""

import argparse
import json
import sys
from pathlib import Path

from .workspace_tools import ToolError, WorkspaceTools


class MCPServer:
    def __init__(self, workspace: str):
        self.tools = WorkspaceTools(workspace)

    def handle(self, message: dict) -> dict:
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            params = message.get("params") or {}
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "vscode-workspace-tools", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools.definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            try:
                text = self.tools.call(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except ToolError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def run(workspace: str) -> None:
    server = MCPServer(workspace)
    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
        except (ValueError, UnicodeDecodeError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        else:
            if "id" not in message:
                continue
            response = server.handle(message)
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="VS Code workspace tools over MCP stdio")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    args = parser.parse_args()
    try:
        run(args.workspace)
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
