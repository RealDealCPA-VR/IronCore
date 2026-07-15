"""Shell tool: run one command line through the platform shell.

RULES
-----
- EXEC risk, one class, worst-case honest. The tool applies NO command
  policy — the mode gate (SPEC §7.2) and the command deny/ask policy
  (SPEC §7.4) are the ENGINE's job, applied before ``run`` is called.
  The tool just executes and reports honestly.
- ``data["command"]`` echoes the exact command line and ``data["cwd"]``
  the resolved directory, so approval previews show the real effect,
  never a paraphrase (SAFETY.md §4).
- The platform shell comes from ``asyncio.create_subprocess_shell``:
  cmd.exe (``%COMSPEC%``) on Windows, ``/bin/sh`` on POSIX.
- ``cwd`` defaults to the workspace root; a ``cwd`` argument is joined to
  the workspace. No path jail here: the command may ``cd`` anywhere it
  likes anyway — EXEC blast radius is bounded by the mode gate, not paths.
- stdout and stderr are MERGED at the pipe (interleaved as produced),
  decoded utf-8 with ``errors="replace"``, capped at MAX_OUTPUT_CHARS with
  an honest ``... [truncated: N more chars]`` marker.
- TIMEOUT kills the WHOLE process tree (a shell spawns children), psutil-free:
    POSIX   — the child starts in a new session (``start_new_session=True``),
              so its process-group id equals its pid; ``os.killpg(pid,
              SIGKILL)`` takes out the shell and every descendant at once.
    Windows — the child starts with ``CREATE_NEW_PROCESS_GROUP``;
              ``taskkill /T /F /PID <pid>`` walks and force-kills the child
              tree; ``proc.kill()`` is the fallback if taskkill fails.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolResult

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 300.0
MAX_OUTPUT_CHARS = 20_000
#: After the kill: seconds to wait for pipes to close and the exit code to land.
_REAP_GRACE_S = 10.0


def _clamp_timeout(timeout_s: Any) -> float:
    """Default, validate, and cap the timeout. Raises for non-positive/non-numeric."""
    if timeout_s is None:
        return DEFAULT_TIMEOUT_S
    value = float(timeout_s)  # TypeError/ValueError propagate to the caller's guard
    if value <= 0:
        raise ValueError("timeout_s must be positive")
    return min(value, MAX_TIMEOUT_S)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Force-kill ``proc`` and every descendant. Mechanism per OS in the module docstring."""
    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            pass  # taskkill unavailable — proc.kill() below still takes the shell
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid: start_new_session=True
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already gone, or group unavailable — fall through to proc.kill()
    try:
        proc.kill()  # belt and braces for the direct child
    except (ProcessLookupError, OSError):
        pass


class ShellTool(Tool):
    """Run a command line via the platform shell inside the workspace."""

    name = "shell"
    description = (
        "Run a shell command in the workspace and return its merged stdout+stderr "
        "plus exit code. Example: shell(command='python -m pytest -q'). Optional: "
        "timeout_s (seconds, default 30, max 300; the process tree is killed on "
        "timeout), cwd (directory relative to the workspace root)."
    )
    risk = ToolRisk.EXEC
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command line to run via the platform shell.",
            },
            "timeout_s": {
                "type": "number",
                "description": "Seconds before the process tree is killed (default 30, max 300).",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory relative to the workspace root "
                "(default: the workspace root).",
            },
        },
        "required": ["command"],
    }

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace).resolve()

    @staticmethod
    def _payload(*, exit_code: int, timed_out: bool, command: str, cwd: str) -> dict[str, Any]:
        """The frozen-shape data dict the engine reads for previews and evidence."""
        return {"exit_code": exit_code, "timed_out": timed_out, "command": command, "cwd": cwd}

    async def run(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(
                ok=False,
                output="",
                error="'command' must be a non-empty string",
                data=self._payload(
                    exit_code=-1, timed_out=False, command="", cwd=str(self._workspace)
                ),
            )
        try:
            timeout = _clamp_timeout(kwargs.get("timeout_s"))
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                output="",
                error="'timeout_s' must be a positive number",
                data=self._payload(
                    exit_code=-1, timed_out=False, command=command, cwd=str(self._workspace)
                ),
            )
        cwd_arg = kwargs.get("cwd")
        resolved_cwd = (self._workspace / cwd_arg).resolve() if cwd_arg else self._workspace
        if not resolved_cwd.is_dir():
            return ToolResult(
                ok=False,
                output="",
                error=f"cwd is not a directory: {resolved_cwd}",
                data=self._payload(
                    exit_code=-1, timed_out=False, command=command, cwd=str(resolved_cwd)
                ),
            )

        spawn_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # Own process group: addressable as a tree, outside our Ctrl+C group.
            spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # New session => new process group whose pgid == child pid (for killpg).
            spawn_kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merged at the pipe, honestly interleaved
                cwd=str(resolved_cwd),
                **spawn_kwargs,
            )
        except OSError as exc:
            return ToolResult(
                ok=False,
                output="",
                error=f"failed to start shell: {exc}",
                data=self._payload(
                    exit_code=-1, timed_out=False, command=command, cwd=str(resolved_cwd)
                ),
            )

        # Don't cancel communicate() on timeout (cancellation drops buffered output).
        # Kill the tree instead: pipes hit EOF and the same task completes with
        # whatever the command produced before it died.
        comm = asyncio.create_task(proc.communicate())
        done, _pending = await asyncio.wait({comm}, timeout=timeout)
        timed_out = comm not in done
        raw = b""
        if timed_out:
            await _kill_tree(proc)
            try:
                raw, _ = await asyncio.wait_for(comm, timeout=_REAP_GRACE_S)
            except (TimeoutError, asyncio.CancelledError, OSError):
                raw = b""  # a detached grandchild holds the pipe — give up on output
        else:
            raw, _ = comm.result()

        if proc.returncode is None:  # only reachable on the give-up path above
            try:
                await asyncio.wait_for(proc.wait(), timeout=_REAP_GRACE_S)
            except TimeoutError:
                pass
        exit_code = proc.returncode if proc.returncode is not None else -1

        text = raw.decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT_CHARS:
            dropped = len(text) - MAX_OUTPUT_CHARS
            text = text[:MAX_OUTPUT_CHARS] + f"\n... [truncated: {dropped} more chars]"

        error: str | None = None
        if timed_out:
            # Appended after truncation so the note itself can never be cut off.
            note = f"[timed out after {timeout:g}s; process tree killed]"
            text = f"{text}\n{note}" if text else note
            error = f"command timed out after {timeout:g}s"
            ok = False  # a killed command never counts as success, whatever the code says
        else:
            ok = exit_code == 0
            if not ok:
                error = f"command exited with code {exit_code}"

        return ToolResult(
            ok=ok,
            output=text,
            error=error,
            data=self._payload(
                exit_code=exit_code, timed_out=timed_out, command=command, cwd=str(resolved_cwd)
            ),
        )
