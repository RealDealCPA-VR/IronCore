"""Write-side filesystem tools: write_file / edit_file.

RULES (SPEC §4.3, §6.1; CONTRACTS §3; SAFETY §2 T2)
---------------------------------------------------
- Every tool here is ``ToolRisk.WRITE``. The engine's policy gate decides
  WHETHER a write runs; these tools only decide HOW.
- Every model-supplied path goes through ``safety.jail.resolve_jailed()``
  FIRST — before any filesystem contact, in every mode. A ``JailViolation``
  becomes ``ToolResult(ok=False)``: it never raises and nothing is written
  or created. All I/O targets the RESOLVED path the jail returns.
- Writes are atomic: temp file in the target's own directory + ``os.replace``,
  so a crash never leaves a half-written file.
- ``edit_file`` transforms text via ``ironcore.tools.patch`` (pure appliers)
  and writes ONLY if the apply succeeded; on failure the file is
  byte-unchanged and ``ToolResult.error`` carries the mechanical reason for
  the repair loop (SPEC §5.4).
- No-ops are reported honestly ("no changes"), never dressed up as edits.
- Text is UTF-8. ``edit_file`` refuses binary and non-UTF-8 files outright:
  a lossy decode would corrupt them on write-back.
"""

from __future__ import annotations

import copy
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ironcore.safety.jail import JailViolation, resolve_jailed
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolResult
from ironcore.tools.patch import (
    PatchResult,
    apply_search_replace,
    apply_unified_diff,
    apply_whole_file,
)

#: Bytes sniffed from the head of a file to detect binary content (null byte).
_BINARY_SNIFF_BYTES = 8192

#: edit_file formats in ladder order (SPEC §4.3); IC-605 picks one per envelope.
EDIT_FORMATS = ("unified_diff", "search_replace", "whole_file")
_APPLIERS = {
    "unified_diff": apply_unified_diff,
    "search_replace": apply_search_replace,
    "whole_file": apply_whole_file,
}


def _guarded_applier(
    name: str, applier: Callable[[str, str], PatchResult]
) -> Callable[[str, str], PatchResult]:
    """Wrap a plugin applier (MS-5) so a defect stays a MECHANICAL failure:
    an exception or a non-PatchResult return becomes ``PatchResult(ok=False)``
    and the file stays byte-unchanged. The failure then flows through the
    same ``patch_failure`` branch builtin formats use — but plugin formats
    are never auto-recommended (§5 ladders are closed), never pre-verified
    by best-of-N resampling (a winner must be in a built-in format), and
    never tuned (the tuner reads ladder rungs only)."""

    def _apply(original: str, edit: str) -> PatchResult:
        try:
            result = applier(original, edit)
        except Exception as exc:  # noqa: BLE001 — a plugin crash must not escape the tool
            return PatchResult(
                ok=False,
                reason=f"plugin edit format {name!r} raised {type(exc).__name__}: {exc}",
            )
        if not isinstance(result, PatchResult):
            return PatchResult(
                ok=False,
                reason=(
                    f"plugin edit format {name!r} returned "
                    f"{type(result).__name__}, not a PatchResult"
                ),
            )
        return result

    return _apply


def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` via temp-file-then-rename in the same dir."""
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class _FsWriteTool(Tool):
    """Shared plumbing: jail routing and relative-path display."""

    risk = ToolRisk.WRITE

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def _jailed(self, path: str) -> tuple[Path | None, ToolResult | None]:
        """Route ``path`` through the jail. Violations become tool errors, never raises."""
        try:
            return resolve_jailed(self.workspace, path), None
        except JailViolation as exc:
            return None, ToolResult(ok=False, output="", error=f"path refused: {exc}")

    def _rel(self, path: Path) -> str:
        # The jail returns RESOLVED paths; try both workspace spellings.
        for root in (self.workspace, self.workspace.resolve()):
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                continue
        return path.as_posix()


class WriteFileTool(_FsWriteTool):
    """Create or overwrite a file, atomically, inside the workspace jail."""

    name = "write_file"
    description = (
        "Create or overwrite a file inside the workspace with exactly the given content. "
        "Parent directories are created automatically; the write is atomic. Writing content "
        "identical to the current file reports 'no changes'. Example: "
        "write_file(path='src/util.py', content='def add(a, b):\\n    return a + b\\n')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path, e.g. 'src/util.py'.",
            },
            "content": {
                "type": "string",
                "description": "The complete file content to write (UTF-8 text).",
            },
        },
        "required": ["path", "content"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(ok=False, output="", error="'path' (string) is required")
        content = kwargs.get("content")
        if not isinstance(content, str):
            return ToolResult(ok=False, output="", error="'content' (string) is required")

        target, refusal = self._jailed(path)
        if refusal is not None:
            return refusal
        assert target is not None
        if target.is_dir():
            return ToolResult(
                ok=False, output="", error=f"{self._rel(target)} is a directory, not a file"
            )

        data = content.encode("utf-8")
        existed = target.exists()
        if existed:
            try:
                if target.read_bytes() == data:
                    return ToolResult(
                        ok=True,
                        output=f"no changes: {self._rel(target)} already has exactly "
                        "this content",
                        data={"no_op": True, "bytes": len(data)},
                    )
            except OSError as exc:
                return ToolResult(
                    ok=False, output="", error=f"cannot read {self._rel(target)}: {exc}"
                )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, data)
        except OSError as exc:
            return ToolResult(
                ok=False, output="", error=f"cannot write {self._rel(target)}: {exc}"
            )
        verb = "overwrote" if existed else "created"
        return ToolResult(
            ok=True,
            output=f"{verb} {self._rel(target)} ({len(data)} bytes)",
            data={"no_op": False, "bytes": len(data), "created": not existed},
        )


class EditFileTool(_FsWriteTool):
    """Apply a deterministic edit (unified_diff / search_replace / whole_file)."""

    name = "edit_file"
    description = (
        "Edit an EXISTING workspace file by applying a patch; the harness applies it "
        "deterministically and reports mechanical failures. Set format to one of: "
        "'unified_diff' (standard @@ hunks), 'search_replace' (blocks of '<<<<<<< SEARCH', "
        "the exact current lines, '=======', the replacement lines, '>>>>>>> REPLACE'), or "
        "'whole_file' (the complete new file content). Example: edit_file(path='app.py', "
        "format='search_replace', edit='<<<<<<< SEARCH\\n    return 1\\n=======\\n"
        "    return 2\\n>>>>>>> REPLACE')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path of an existing file, e.g. 'src/app.py'.",
            },
            "format": {
                "type": "string",
                "enum": list(EDIT_FORMATS),
                "description": "How 'edit' is encoded: unified_diff, search_replace, "
                "or whole_file.",
            },
            "edit": {
                "type": "string",
                "description": "The edit payload matching 'format': a unified diff, "
                "SEARCH/REPLACE block text, or the complete new file content.",
            },
        },
        "required": ["path", "format", "edit"],
    }

    def __init__(
        self,
        workspace: Path,
        extra_formats: Mapping[str, Callable[[str, str], PatchResult]] | None = None,
    ) -> None:
        """``extra_formats`` (additive, MS-5) merges plugin appliers into the
        dispatch — builtins WIN a name clash (a plugin can never shadow the
        ladder rungs resampling verifies and tuning reads). When any plugin
        format lands, ``parameters`` becomes an instance-level snapshot whose
        ``format`` enum advertises it (schemas are not frozen, CONTRACTS §3);
        the class attribute stays intact for plain introspection."""
        super().__init__(workspace)
        self._appliers: dict[str, Callable[[str, str], PatchResult]] = dict(_APPLIERS)
        if extra_formats:
            for name, applier in extra_formats.items():
                if name in self._appliers:
                    continue  # builtins win; loaders refuse these names anyway
                self._appliers[name] = _guarded_applier(name, applier)
            params = copy.deepcopy(type(self).parameters)
            params["properties"]["format"]["enum"] = list(self._appliers)
            self.parameters = params

    async def run(self, **kwargs: Any) -> ToolResult:
        path = kwargs.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(ok=False, output="", error="'path' (string) is required")
        fmt = kwargs.get("format")
        if fmt not in self._appliers:
            return ToolResult(
                ok=False,
                output="",
                error=f"'format' must be one of {', '.join(self._appliers)}; got {fmt!r}",
            )
        edit = kwargs.get("edit")
        if not isinstance(edit, str):
            return ToolResult(ok=False, output="", error="'edit' (string) is required")

        target, refusal = self._jailed(path)
        if refusal is not None:
            return refusal
        assert target is not None
        if not target.exists():
            return ToolResult(
                ok=False,
                output="",
                error=f"file not found: {self._rel(target)} — edit_file needs an existing "
                "file; use write_file to create one",
            )
        if target.is_dir():
            return ToolResult(
                ok=False, output="", error=f"{self._rel(target)} is a directory, not a file"
            )
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return ToolResult(
                ok=False, output="", error=f"cannot read {self._rel(target)}: {exc}"
            )
        if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
            return ToolResult(
                ok=False,
                output="",
                error=f"{self._rel(target)} looks binary (null byte in head); refusing to edit",
            )
        try:
            original = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return ToolResult(
                ok=False,
                output="",
                error=f"{self._rel(target)} is not valid UTF-8 ({exc}); refusing to edit — "
                "a lossy decode would corrupt it on write-back",
            )

        result = self._appliers[fmt](original, edit)
        if not result.ok:
            # NOTHING was written; the reason is mechanical, for the repair loop.
            # `data` (harness-only, CONTRACTS §3) marks this as a PATCH failure —
            # the apply itself failed against readable current text, so a raced
            # best-of-N candidate could fix it (MS-4). Missing-file / binary /
            # jail refusals above never carry it and never trigger resampling.
            return ToolResult(
                ok=False,
                output="",
                error=result.reason,
                data={"patch_failure": True, "format": fmt},
            )
        if result.no_op:
            return ToolResult(
                ok=True,
                output=f"no changes: the {fmt} edit leaves {self._rel(target)} identical",
                data={"no_op": True},
            )
        assert result.new_text is not None
        encoded = result.new_text.encode("utf-8")
        try:
            _atomic_write(target, encoded)
        except OSError as exc:
            return ToolResult(
                ok=False, output="", error=f"cannot write {self._rel(target)}: {exc}"
            )
        old_lines = len(original.splitlines())
        new_lines = len(result.new_text.splitlines())
        return ToolResult(
            ok=True,
            output=f"applied {fmt} edit to {self._rel(target)} "
            f"({old_lines} -> {new_lines} lines)",
            data={"no_op": False, "format": fmt, "bytes": len(encoded)},
        )
