"""Workspace-scoped file and PowerShell tools exposed to VS Code over MCP."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List


class ToolError(ValueError):
    """A tool request cannot be completed safely."""


def _annotations(destructive: bool = False) -> dict:
    return {
        "readOnlyHint": False,
        "destructiveHint": destructive,
        "openWorldHint": False,
    }


TOOL_DEFINITIONS = [
    {
        "name": "edit_file",
        "description": "Edit an existing UTF-8 workspace file by replacing exact text. The old string must be unique unless replace_all is true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path or workspace-relative path."},
                "old_string": {"type": "string", "description": "Exact existing text to replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
        "annotations": _annotations(),
    },
    {
        "name": "create_file",
        "description": "Create a new UTF-8 workspace file. Fails instead of overwriting an existing file. Create the parent directory first if needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path or workspace-relative path."},
                "content": {"type": "string", "description": "Complete file content; may be empty."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "annotations": _annotations(),
    },
    {
        "name": "delete_file",
        "description": "Permanently delete one file inside the workspace. Does not delete directories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path or workspace-relative path."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": _annotations(destructive=True),
    },
    {
        "name": "create_directory",
        "description": "Create a workspace directory and any missing parent directories. Existing directories are left unchanged.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path or workspace-relative directory path."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": _annotations(),
    },
    {
        "name": "powershell",
        "description": "Run a PowerShell command on the host, using pwsh or Windows PowerShell. Review the command carefully in the VS Code approval prompt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "PowerShell command to execute."},
                "cwd": {"type": "string", "description": "Optional absolute or workspace-relative working directory."},
                "timeout": {"type": "integer", "default": 120, "minimum": 1, "maximum": 600},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "annotations": _annotations(destructive=True),
    },
]


class WorkspaceTools:
    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ToolError(f"Workspace is not a directory: {self.workspace}")

    @staticmethod
    def definitions() -> List[dict]:
        return TOOL_DEFINITIONS

    def path(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ToolError("path must be a non-empty string")
        supplied = Path(value)
        candidate = supplied if supplied.is_absolute() else self.workspace / supplied
        candidate = Path(os.path.abspath(str(candidate)))
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError(f"Path escapes the workspace: {value}") from exc
        if candidate.is_symlink():
            raise ToolError(f"Refusing to modify a symlink: {value}")
        return candidate

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_bytes().decode("utf-8")
        except FileNotFoundError as exc:
            raise ToolError(f"File does not exist: {path}") from exc
        except IsADirectoryError as exc:
            raise ToolError(f"Expected a file, not a directory: {path}") from exc
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {path}") from exc
        except OSError as exc:
            raise ToolError(f"Cannot read {path}: {exc}") from exc

    @staticmethod
    def _write(path: Path, text: str) -> None:
        try:
            path.write_bytes(text.encode("utf-8"))
        except OSError as exc:
            raise ToolError(f"Cannot write {path}: {exc}") from exc

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        target = self.path(path)
        if not target.is_file():
            raise ToolError(f"File does not exist: {target}")
        if not isinstance(old_string, str) or not old_string:
            raise ToolError("old_string must be a non-empty string")
        if not isinstance(new_string, str):
            raise ToolError("new_string must be a string")
        text = self._read(target)
        occurrences = text.count(old_string)
        if not occurrences:
            raise ToolError("old_string was not found")
        if occurrences > 1 and not replace_all:
            raise ToolError(f"old_string occurs {occurrences} times; set replace_all=true or use more context")
        updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        self._write(target, updated)
        return f"Edited {target}; replacements={occurrences if replace_all else 1}"

    def create_file(self, path: str, content: str) -> str:
        target = self.path(path)
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        if not target.parent.is_dir():
            raise ToolError(f"Parent directory does not exist: {target.parent}; use create_directory first")
        try:
            with target.open("xb") as handle:
                handle.write(content.encode("utf-8"))
        except FileExistsError as exc:
            raise ToolError(f"File already exists: {target}") from exc
        except OSError as exc:
            raise ToolError(f"Cannot create {target}: {exc}") from exc
        return f"Created {target}; bytes={len(content.encode('utf-8'))}"

    def delete_file(self, path: str) -> str:
        target = self.path(path)
        if not target.is_file():
            raise ToolError(f"File does not exist or is not a regular file: {target}")
        try:
            target.unlink()
        except OSError as exc:
            raise ToolError(f"Cannot delete {target}: {exc}") from exc
        return f"Deleted {target}"

    def create_directory(self, path: str) -> str:
        target = self.path(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"Cannot create directory {target}: {exc}") from exc
        if not target.is_dir():
            raise ToolError(f"Path is not a directory: {target}")
        return f"Directory ready: {target}"

    @staticmethod
    def _powershell_executable() -> str:
        names = ("pwsh", "powershell", "powershell.exe") if os.name == "nt" else ("pwsh",)
        for name in names:
            executable = shutil.which(name)
            if executable:
                return executable
        raise ToolError("PowerShell was not found; install PowerShell 7 (pwsh) or enable Windows PowerShell")

    @staticmethod
    def _truncate(text: str, limit: int = 50000) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n... truncated {len(text) - limit} characters"

    def powershell(self, command: str, cwd: str = ".", timeout: int = 120) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600:
            raise ToolError("timeout must be an integer from 1 to 600 seconds")
        directory = self.path(cwd)
        if not directory.is_dir():
            raise ToolError(f"Working directory does not exist: {directory}")
        executable = self._powershell_executable()
        wrapped = "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8;\n" + command
        try:
            completed = subprocess.run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", wrapped],
                cwd=str(directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"PowerShell command timed out after {timeout} seconds") from exc
        except OSError as exc:
            raise ToolError(f"Cannot start PowerShell: {exc}") from exc
        return json.dumps({
            "exitCode": completed.returncode,
            "stdout": self._truncate(completed.stdout),
            "stderr": self._truncate(completed.stderr),
        }, ensure_ascii=False)

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        handlers = {
            "edit_file": self.edit_file,
            "create_file": self.create_file,
            "delete_file": self.delete_file,
            "create_directory": self.create_directory,
            "powershell": self.powershell,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolError(f"Unknown workspace tool: {name}")
        if not isinstance(arguments, dict):
            raise ToolError("Tool arguments must be an object")
        try:
            return handler(**arguments)
        except TypeError as exc:
            raise ToolError(f"Invalid arguments for {name}: {exc}") from exc
