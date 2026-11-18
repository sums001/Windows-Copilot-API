"""VS Code session hook for creating workspace MCP configuration."""

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Tuple


SERVER_NAME = "workspace-tools"
HOOK_FILE_NAME = "windows-copilot-api-workspace-mcp.json"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _python_command() -> str:
    parts = [str(Path(sys.executable).resolve()), "-m", "server.vscode"]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def ensure_vscode_mcp(workspace: str) -> Tuple[Path, bool]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace is not a directory: {root}")

    config_path = root / ".vscode" / "mcp.json"
    if config_path.exists():
        return config_path, False

    config = {
        "servers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": str(Path(sys.executable).resolve()),
                "args": [
                    "-m",
                    "server.workspace_mcp",
                    "--workspace",
                    str(root),
                ],
                "cwd": str(_project_root()),
            },
        },
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        return config_path, False
    return config_path, True


def install_session_start_hook() -> Tuple[Path, bool]:
    default_home = Path.home() / ".copilot"
    copilot_home = Path(os.environ.get("COPILOT_HOME", str(default_home))).expanduser()
    hook_path = copilot_home / "hooks" / HOOK_FILE_NAME
    config = {
        "version": 1,
        "hooks": {
            "sessionStart": [
                {
                    "type": "command",
                    "command": _python_command(),
                    "cwd": str(_project_root()),
                    "timeoutSec": 10,
                },
            ],
        },
    }
    if hook_path.exists():
        try:
            existing = json.loads(hook_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read valid JSON from {hook_path}: {exc}") from exc
        if existing == config:
            return hook_path, False

    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return hook_path, True


def run_session_start_hook() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("Session hook input must be a JSON object")
        workspace = event.get("cwd")
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("Session hook input is missing a valid cwd")
        config_path, created = ensure_vscode_mcp(workspace)
    except (OSError, ValueError) as exc:
        print(f"Workspace MCP hook failed: {exc}", file=sys.stderr)
        raise SystemExit(2)

    status = "created" if created else "already exists; skipped"
    print(f"Workspace MCP config {status}: {config_path}", file=sys.stderr)
    sys.stdout.write("{}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install-hook",
        action="store_true",
        help="install the user-level VS Code SessionStart hook and exit",
    )
    args = parser.parse_args()
    if args.install_hook:
        try:
            hook_path, changed = install_session_start_hook()
        except (OSError, ValueError) as exc:
            parser.exit(2, f"error: {exc}\n")
        status = "installed" if changed else "already current"
        print(f"VS Code SessionStart hook {status}: {hook_path}")
        return
    run_session_start_hook()


if __name__ == "__main__":
    main()
