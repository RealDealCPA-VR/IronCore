"""Read-side filesystem tools: read_file / list_dir / glob / grep.

RULES (SPEC §6.1, CONTRACTS §3): every tool here is `ToolRisk.READ` and
side-effect-free — no writes, no prompts, no printing. Each tool is
constructed with a `workspace: Path`; path arguments are workspace-relative
and are resolved against it. There is deliberately NO path jail here: the
engine layer (IC-401) gates out-of-workspace reads before a tool runs.

Output truncation is honest: whenever output is capped, the marker states
the EXACT remaining count (e.g. ``... [truncated: 37 more lines]``).
User-input errors (missing file, bad regex) return ``ToolResult(ok=False)``;
exceptions are reserved for programmer errors.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolResult

#: read_file returns at most this many lines when no explicit limit is given.
DEFAULT_READ_LIMIT = 2000
#: glob / grep return at most this many entries, then an honest truncation note.
MAX_GLOB_MATCHES = 200
MAX_GREP_MATCHES = 200
#: Bytes sniffed from the head of a file to detect binary content (null byte).
_BINARY_SNIFF_BYTES = 8192


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head


class _FsReadTool(Tool):
    """Shared plumbing: workspace anchoring and relative-path display."""

    risk = ToolRisk.READ

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def _resolve(self, path: str) -> Path:
        # Relative args anchor to the workspace; absolute args pass through
        # untouched (the engine, not the tool, decides whether that is allowed).
        return self.workspace / path

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return path.as_posix()


class ReadFileTool(_FsReadTool):
    """Read a text file with cat -n style line numbers."""

    name = "read_file"
    description = (
        "Read a text file from the workspace. Output is line-numbered like `cat -n`. "
        "Use offset/limit to page through large files. "
        "Example: read_file(path='src/app.py', offset=10, limit=40) shows lines 10-49."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path, e.g. 'src/app.py'.",
            },
            "offset": {
                "type": "integer",
                "description": "1-based line number to start from. Default 1.",
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum lines to return. Default {DEFAULT_READ_LIMIT}.",
            },
        },
        "required": ["path"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(ok=False, output="", error="'path' (string) is required")
        offset = kwargs.get("offset", 1)
        limit = kwargs.get("limit", DEFAULT_READ_LIMIT)
        if not isinstance(offset, int) or offset < 1:
            return ToolResult(ok=False, output="", error="'offset' must be an integer >= 1")
        if not isinstance(limit, int) or limit < 1:
            return ToolResult(ok=False, output="", error="'limit' must be an integer >= 1")

        target = self._resolve(path)
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            return ToolResult(ok=False, output="", error=f"file not found: {self._rel(target)}")
        except OSError as exc:
            return ToolResult(
                ok=False, output="", error=f"cannot read {self._rel(target)}: {exc}"
            )
        if _is_binary(raw[:_BINARY_SNIFF_BYTES]):
            return ToolResult(
                ok=False,
                output="",
                error=f"{self._rel(target)} looks binary (null byte in head); not showing it",
            )

        lines = raw.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        if total == 0:
            return ToolResult(ok=True, output="(empty file)", data={"total_lines": 0})
        if offset > total:
            return ToolResult(
                ok=True,
                output=f"(file has {total} lines; offset {offset} is past the end)",
                data={"total_lines": total, "shown": 0},
            )

        window = lines[offset - 1 : offset - 1 + limit]
        numbered = [f"{offset + i:6d}\t{line}" for i, line in enumerate(window)]
        remaining = total - (offset - 1) - len(window)
        if remaining > 0:
            numbered.append(f"... [truncated: {remaining} more lines]")
        return ToolResult(
            ok=True,
            output="\n".join(numbered),
            data={"total_lines": total, "shown": len(window)},
        )


class ListDirTool(_FsReadTool):
    """List directory entries, directories first with a trailing slash."""

    name = "list_dir"
    description = (
        "List the entries of a workspace directory: directories first (with a trailing '/'), "
        "then files, one per line. Example: list_dir(path='src') -> 'utils/\\napp.py'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative directory. Default '.' (workspace root).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path", ".")
        if not isinstance(path, str) or not path:
            return ToolResult(ok=False, output="", error="'path' must be a non-empty string")
        target = self._resolve(path)
        if not target.exists():
            return ToolResult(
                ok=False, output="", error=f"directory not found: {self._rel(target)}"
            )
        if not target.is_dir():
            return ToolResult(
                ok=False, output="", error=f"not a directory: {self._rel(target)}"
            )
        try:
            entries = list(target.iterdir())
        except OSError as exc:
            return ToolResult(
                ok=False, output="", error=f"cannot list {self._rel(target)}: {exc}"
            )
        # Directories first, then files; each group sorted by name (deterministic).
        entries.sort(key=lambda p: (not p.is_dir(), p.name))
        if not entries:
            return ToolResult(ok=True, output="(empty directory)", data={"entries": 0})
        lines = [e.name + "/" if e.is_dir() else e.name for e in entries]
        return ToolResult(ok=True, output="\n".join(lines), data={"entries": len(entries)})


class GlobTool(_FsReadTool):
    """Find files by glob pattern."""

    name = "glob"
    description = (
        "Find files matching a glob pattern; returns workspace-relative paths, sorted. "
        "Use '**' to recurse. Example: glob(pattern='**/*.py', path='src') lists every "
        ".py file under src/."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '*.toml' or '**/*.py'.",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative directory to search in. Default '.'.",
            },
        },
        "required": ["pattern"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        pattern = kwargs.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(ok=False, output="", error="'pattern' (string) is required")
        path = kwargs.get("path", ".")
        if not isinstance(path, str) or not path:
            return ToolResult(ok=False, output="", error="'path' must be a non-empty string")
        base = self._resolve(path)
        if not base.is_dir():
            return ToolResult(
                ok=False, output="", error=f"directory not found: {self._rel(base)}"
            )
        try:
            matches = sorted(self._rel(p) for p in base.glob(pattern) if p.is_file())
        except (ValueError, NotImplementedError) as exc:
            return ToolResult(ok=False, output="", error=f"bad glob pattern {pattern!r}: {exc}")
        if not matches:
            return ToolResult(ok=True, output="(no matches)", data={"matches": 0})
        shown = matches[:MAX_GLOB_MATCHES]
        remaining = len(matches) - len(shown)
        if remaining > 0:
            shown.append(f"... [truncated: {remaining} more matches]")
        return ToolResult(ok=True, output="\n".join(shown), data={"matches": len(matches)})


class GrepTool(_FsReadTool):
    """Regex search across workspace text files (binary files are skipped)."""

    name = "grep"
    description = (
        "Search file contents with a Python regular expression. Binary files are skipped. "
        "Each hit is 'path:lineno:line'. Example: grep(pattern='def main', path='src', "
        "glob='*.py') -> 'src/app.py:12:def main():'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression, e.g. 'TODO|FIXME'.",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative directory or file to search. Default '.'.",
            },
            "glob": {
                "type": "string",
                "description": "Only search files matching this glob, e.g. '*.py'.",
            },
        },
        "required": ["pattern"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        pattern = kwargs.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(ok=False, output="", error="'pattern' (string) is required")
        path = kwargs.get("path", ".")
        if not isinstance(path, str) or not path:
            return ToolResult(ok=False, output="", error="'path' must be a non-empty string")
        glob_filter = kwargs.get("glob")
        if glob_filter is not None and (not isinstance(glob_filter, str) or not glob_filter):
            return ToolResult(ok=False, output="", error="'glob' must be a non-empty string")

        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult(ok=False, output="", error=f"invalid regex {pattern!r}: {exc}")

        base = self._resolve(path)
        if base.is_file():
            files = [base]
        elif base.is_dir():
            try:
                files = sorted(
                    (p for p in base.rglob(glob_filter or "*") if p.is_file()),
                    key=lambda p: self._rel(p),
                )
            except (ValueError, NotImplementedError) as exc:
                return ToolResult(
                    ok=False, output="", error=f"bad glob filter {glob_filter!r}: {exc}"
                )
        else:
            return ToolResult(ok=False, output="", error=f"path not found: {self._rel(base)}")

        hits: list[str] = []
        extra = 0
        for f in files:
            try:
                raw = f.read_bytes()
            except OSError:
                continue  # unreadable file: skip, never crash a READ tool
            if _is_binary(raw[:_BINARY_SNIFF_BYTES]):
                continue  # binary: skip silently
            rel = self._rel(f)
            for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    if len(hits) < MAX_GREP_MATCHES:
                        hits.append(f"{rel}:{lineno}:{line}")
                    else:
                        extra += 1  # keep counting so the truncation note is exact

        if not hits:
            return ToolResult(ok=True, output="(no matches)", data={"matches": 0})
        total = len(hits) + extra
        if extra > 0:
            hits.append(f"... [truncated: {extra} more matches]")
        return ToolResult(ok=True, output="\n".join(hits), data={"matches": total})
