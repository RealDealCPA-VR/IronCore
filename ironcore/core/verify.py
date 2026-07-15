"""CommandVerifier (IC-504): the post-mutation verification loop (SPEC §5.5).

Verify-command discovery, FIRST match wins (highest priority first):

  1. ``configured``   — commands passed to the constructor (``commands=``); this
     is also the seam for a future settings-driven list.
  2. ``ironcore.md``  — a ``verify:`` directive in ``IRONCORE.md`` at the
     workspace root: either a single ``verify: <cmd>`` line anywhere, or the
     command lines beneath a ``## Verify`` section (list items / plain lines /
     a fenced block; the section ends at the next heading).
  3. auto-detect by workspace markers:
       * ``pyproject.toml`` / ``pytest.ini`` / a ``tests/`` dir → ``pytest -q``
       * ``package.json`` declaring a ``scripts.test`` entry     → ``npm test``
       * ``Cargo.toml``                                          → ``cargo test``

If nothing is discovered and files were touched, that is reported HONESTLY as
``VerifyResult(ok=True, summary="no verify command configured", ran=[])`` — the
absence of a checker is not a failure, but the engine never mistakes it for a
pass. When ``touched_files`` is ``False`` the pass is skipped entirely
(verification only runs after mutations).

Running: each discovered command runs via ``asyncio.create_subprocess_shell``
with ``cwd=workspace``, merged stdout+stderr, and a per-command timeout
(constructor ``timeout_s``, default 120s). ``ok`` is ``True`` only when EVERY
command exits 0; a killed (timed-out) command never counts as success. On
failure the summary names the failing command and appends a capped TAIL of its
output, so unverified work is never reported as verified (SAFETY T7). Process
handling mirrors :mod:`ironcore.tools.shell` (cross-OS process-group kill on
timeout). Stdlib + asyncio only; no engine import, no new deps.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from ironcore.core.protocols import Verifier, VerifyResult

if TYPE_CHECKING:  # annotations only — no runtime coupling to config/state
    from pathlib import Path

    from ironcore.config.settings import Settings
    from ironcore.core.state import SessionState

#: Per-command wall-clock ceiling before the process tree is killed.
DEFAULT_TIMEOUT_S = 120.0
#: How many trailing chars of a failing command's output to echo in the summary.
_TAIL_CHARS = 2000
#: After a timeout kill: seconds to wait for pipes to close / the exit code to land.
_REAP_GRACE_S = 10.0
#: Honest note when no verify command could be discovered.
_NO_COMMAND = "no verify command configured"


class CommandVerifier(Verifier):
    """Discover the project's verify commands, run them, report honestly.

    ``commands`` pins an explicit list (priority 1, bypassing discovery);
    ``timeout_s`` caps each command. Construction is cheap and side-effect-free —
    all work happens in :meth:`verify`.
    """

    def __init__(
        self, commands: list[str] | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self._commands = [c for c in (commands or []) if c and c.strip()]
        self._timeout_s = float(timeout_s)

    # -- protocol -------------------------------------------------------------

    async def verify(
        self,
        workspace: Path,
        settings: Settings,
        state: SessionState,
        touched_files: bool,
    ) -> VerifyResult:
        """Run discovered verify commands after a mutating turn (SPEC §5.5)."""
        if not touched_files:  # verification only runs after file mutations
            return VerifyResult(ok=True, summary="", ran=[])

        commands, source = self.discover(workspace)
        if not commands:
            return VerifyResult(ok=True, summary=_NO_COMMAND, ran=[])

        ran: list[str] = []
        for command in commands:
            ran.append(command)
            exit_code, output, timed_out = await self._run(command, workspace)
            if timed_out or exit_code != 0:
                status = "timed out" if timed_out else f"exited {exit_code}"
                summary = f"verify failed: `{command}` {status}"
                tail = _tail(output)
                if tail:
                    summary = f"{summary}\n{tail}"
                return VerifyResult(ok=False, summary=summary, ran=ran)

        count = len(ran)
        noun = "command" if count == 1 else "commands"
        return VerifyResult(ok=True, summary=f"verify passed: {count} {noun} ({source})", ran=ran)

    # -- discovery ------------------------------------------------------------

    def discover(self, workspace: Path) -> tuple[list[str], str]:
        """The commands (and their source label) :meth:`verify` would run.

        Pure/side-effect-free — reads marker files only. Returns ``([], "none")``
        when nothing applies. Priority: configured > IRONCORE.md > auto-detect.
        """
        if self._commands:
            return list(self._commands), "configured"
        from_md = self._from_ironcore_md(workspace)
        if from_md:
            return from_md, "ironcore.md"
        auto = self._auto_detect(workspace)
        if auto is not None:
            return [auto], f"auto:{auto.split()[0]}"
        return [], "none"

    @staticmethod
    def _auto_detect(workspace: Path) -> str | None:
        """First applicable auto-detect marker → its conventional test command."""
        if (
            (workspace / "pyproject.toml").is_file()
            or (workspace / "pytest.ini").is_file()
            or (workspace / "tests").is_dir()
        ):
            return "pytest -q"
        package = workspace / "package.json"
        if package.is_file() and _has_npm_test(package):
            return "npm test"
        if (workspace / "Cargo.toml").is_file():
            return "cargo test"
        return None

    @staticmethod
    def _from_ironcore_md(workspace: Path) -> list[str]:
        """Parse verify commands out of ``IRONCORE.md`` (see the module docstring)."""
        try:
            text = (workspace / "IRONCORE.md").read_text(encoding="utf-8")
        except OSError:
            return []
        lines = text.splitlines()

        # (1) a single `verify: <cmd>` directive, anywhere in the file.
        for line in lines:
            match = re.match(r"\s*verify:\s*(.+?)\s*$", line, re.IGNORECASE)
            if match:
                return [match.group(1)]

        # (2) the command lines beneath a `## Verify` heading, up to the next heading.
        commands: list[str] = []
        in_section = False
        for line in lines:
            if re.match(r"\s*#+\s*verify\s*$", line, re.IGNORECASE):
                in_section = True
                continue
            if in_section:
                if line.lstrip().startswith("#"):
                    break  # a new heading ends the Verify section
                command = _clean_md_command(line)
                if command:
                    commands.append(command)
        return commands

    # -- execution (mirrors ironcore.tools.shell) -----------------------------

    async def _run(self, command: str, workspace: Path) -> tuple[int, str, bool]:
        """Run ``command`` in ``workspace``; return (exit_code, merged_output, timed_out)."""
        spawn_kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            # Own process group: addressable as a tree, outside our Ctrl+C group.
            spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # New session => process group whose pgid == child pid (for killpg).
            spawn_kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merged, honestly interleaved
                cwd=str(workspace),
                **spawn_kwargs,
            )
        except OSError as exc:
            return -1, f"failed to start verify command: {exc}", False

        # Kill the tree on timeout rather than cancelling communicate() (which would
        # drop buffered output): pipes hit EOF and the task completes with whatever
        # the command produced before it died.
        comm = asyncio.create_task(proc.communicate())
        done, _pending = await asyncio.wait({comm}, timeout=self._timeout_s)
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
        return exit_code, raw.decode("utf-8", errors="replace"), timed_out


def _has_npm_test(package_json: Path) -> bool:
    """True when ``package.json`` declares a ``scripts.test`` string entry."""
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return isinstance(scripts, dict) and isinstance(scripts.get("test"), str)


def _clean_md_command(line: str) -> str:
    """Strip a Verify-section line down to a bare command (or "" to skip it)."""
    stripped = line.strip()
    if not stripped or stripped.startswith("```"):
        return ""
    for marker in ("- ", "* ", "+ "):
        if stripped.startswith(marker):
            stripped = stripped[len(marker) :].strip()
            break
    return stripped.strip("`").strip()


def _tail(output: str, limit: int = _TAIL_CHARS) -> str:
    """The last ``limit`` chars of ``output`` (the failure lives at the end)."""
    text = output.strip()
    if len(text) <= limit:
        return text
    return "… [earlier output omitted]\n" + text[-limit:]


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Force-kill ``proc`` and every descendant (mechanism per OS, from shell.py)."""
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
