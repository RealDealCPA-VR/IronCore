"""The scripted offline session and its headless narrator (IC-1103, SPEC §14).

``run_demo`` scaffolds a tiny "project" (a ``greet()`` function and a failing
feature check), then drives the REAL :class:`~ironcore.core.engine.TurnEngine`
against a scripted :class:`~ironcore.providers.mock.MockProvider` through one
turn: the "model" reads the file, plans, edits it with ``edit_file``, the engine
gates (ACCEPT_EDITS → allow) and applies the edit, then the post-mutation VERIFY
pass runs the check command — which now passes — and the turn stops with an
evidence-based ``stop_reason == "done"``.

The narration is honest: it renders the actual ``core.events`` stream
(``TurnStarted → TextDelta → ToolCallRequested/Finished → TurnCompleted``) plus
the real :class:`~ironcore.core.protocols.VerifyResult` the engine consumed. The
engine emits no event on a verify *pass*, so a thin recording wrapper remembers
the result it returned to the engine — nothing is faked, and if a tool runs the
file really changes on disk.

Hermeticity: every write lands under the given ``workspace`` (state persists at
``<ws>/.ironcore/state.json``; handoff writing is disabled); with no workspace a
throwaway ``tempfile`` dir is created and removed. ``sys.executable`` runs the
verify command, so it works on Windows and POSIX with no hardcoded paths.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import (
    ApprovalRequired,
    Event,
    TextDelta,
    ToolCallFinished,
    ToolCallRequested,
    TurnCompleted,
    TurnError,
    TurnStarted,
)
from ironcore.core.protocols import Verifier, VerifyResult
from ironcore.core.verify import CommandVerifier
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools import build_default_registry
from ironcore.tools.patch import parse_search_replace

#: The feature file the "model" edits, and the check that gates the feature.
GREETER_FILENAME = "greeter.py"
CHECK_FILENAME = "check_feature.py"

#: Starting state: greet() has no exclamation mark, so the check below FAILS.
GREETER_BEFORE = '''"""A tiny greeter used by the IronCore offline demo."""


def greet(name):
    return f"Hello, {name}"
'''

#: A real feature check: importable, exits non-zero until greet() ends with '!'.
CHECK_SCRIPT = '''"""Feature check: greet() must end with an exclamation mark."""

from greeter import greet

result = greet("World")
assert result == "Hello, World!", f"greet('World') returned {result!r}, expected a trailing '!'"
print("feature check passed:", repr(result))
'''

#: What the user asks the agent to do this turn.
USER_REQUEST = (
    "The feature check in check_feature.py is failing. Update greet() in greeter.py "
    "so the greeting ends with an exclamation mark, then confirm it is done."
)

#: The exact SEARCH/REPLACE payload the scripted model emits (matches once).
_EDIT_PAYLOAD = (
    "<<<<<<< SEARCH\n"
    '    return f"Hello, {name}"\n'
    "=======\n"
    '    return f"Hello, {name}!"\n'
    ">>>>>>> REPLACE\n"
)

_WIDTH = 66


def _assistant(content: str = "", calls: list[ToolCall] | None = None) -> CompletionResult:
    """One scripted completion: streamed as text chunks + native tool calls."""
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=list(calls or []))
    )


def _script() -> list[CompletionResult]:
    """The three-step scripted session: read → plan+edit → confirm (then stop)."""
    read = ToolCall(id="c1", name="read_file", arguments={"path": GREETER_FILENAME})
    edit = ToolCall(
        id="c2",
        name="edit_file",
        arguments={"path": GREETER_FILENAME, "format": "search_replace", "edit": _EDIT_PAYLOAD},
    )
    return [
        _assistant("Let me read greeter.py to see the current greeting.", calls=[read]),
        _assistant(
            "The greeting has no exclamation mark. I'll append '!' to the return value.",
            calls=[edit],
        ),
        _assistant("Done - greet() now ends with '!'. The feature check should pass."),
    ]


def _demo_profile() -> CapabilityProfile:
    """A profile that reports native tool-calling, so the mock's tool_calls apply."""
    return CapabilityProfile(
        model_id="ironcore-demo", honest_context=8192, tool_protocols={"native": 1.0}
    )


class _RecordingVerifier:
    """Delegates to a real :class:`Verifier`, remembering the result it returned.

    The engine emits no event on a verification PASS, so the narrator reads the
    ACTUAL :class:`VerifyResult` the engine consumed from here — never a re-run,
    never a fake.
    """

    def __init__(self, inner: Verifier) -> None:
        self._inner = inner
        self.last: VerifyResult | None = None

    async def verify(
        self, workspace: Path, settings: Settings, state: object, touched_files: bool
    ) -> VerifyResult:
        result = await self._inner.verify(workspace, settings, state, touched_files)
        self.last = result
        return result


def _args_preview(arguments: dict[str, object], *, limit: int = 100) -> str:
    """Compact one-line ``k=v`` preview of tool arguments (local; no tui import)."""
    text = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return text if len(text) <= limit else text[:limit] + " ..."


def _render_edit(payload: str) -> list[str]:
    """A SEARCH/REPLACE payload as ``-old`` / ``+new`` lines (falls back to raw)."""
    blocks, error = parse_search_replace(payload)
    if error is not None or not blocks:
        return payload.splitlines()
    lines: list[str] = []
    for search, replace in blocks:
        lines += [f"- {ln}" for ln in search.splitlines()]
        lines += [f"+ {ln}" for ln in replace.splitlines()]
    return lines


class _Narrator:
    """Turns a ``core.events`` stream into a readable, honest transcript."""

    def __init__(self, emit: Callable[[str], object]) -> None:
        self._emit = emit
        self._buffer: list[str] = []
        self.completed = False
        self.stop_reason: str | None = None
        self.error: str | None = None

    def line(self, text: str = "") -> None:
        self._emit(text)

    def header(self, mode: Mode, workspace: Path) -> None:
        self.line("=" * _WIDTH)
        self.line("IronCore - offline end-to-end demo")
        self.line("=" * _WIDTH)
        self.line(f"workspace : {workspace}")
        self.line(f"mode      : {mode.value}")
        self.line("model     : mock (scripted; no network, no real model)")
        self.line("")
        self.line(f"> user: {USER_REQUEST}")

    def _flush_text(self) -> None:
        text = "".join(self._buffer).strip()
        self._buffer.clear()
        for para in text.splitlines():
            if para.strip():
                self.line(f"  assistant: {para.strip()}")

    def handle(self, event: Event, verifier: _RecordingVerifier) -> None:
        if isinstance(event, TurnStarted):
            self.line("")
            self.line(f"* turn {event.turn_id} started  (mode: {event.mode})")
        elif isinstance(event, TextDelta):
            self._buffer.append(event.text)
        elif isinstance(event, ToolCallRequested):
            self._flush_text()
            self._tool_request(event)
        elif isinstance(event, ApprovalRequired):
            self._flush_text()
            self.line(f"|  approval required: {event.preview}")
        elif isinstance(event, ToolCallFinished):
            self._tool_finished(event)
        elif isinstance(event, TurnCompleted):
            self._flush_text()
            self._verify_section(verifier)
            self.stop_reason = event.stop_reason
            self.completed = True
            self.line("")
            self.line(f"* turn completed  ->  stop_reason: {event.stop_reason}")
            if event.usage:
                self.line(f"  usage: {event.usage}")
        elif isinstance(event, TurnError):
            self._flush_text()
            self.error = event.message
            self.line(f"x turn error: {event.message}")

    def _tool_request(self, event: ToolCallRequested) -> None:
        call = event.call
        self.line("")
        self.line(f"+- tool: {call.name}   risk={event.risk}   gate={event.decision}")
        if call.name == "edit_file":
            self.line(
                f"|  path={call.arguments.get('path')!r}  "
                f"format={call.arguments.get('format')!r}"
            )
            for diff_line in _render_edit(str(call.arguments.get("edit", ""))):
                self.line(f"|    {diff_line}")
        else:
            self.line(f"|  {_args_preview(call.arguments)}")

    def _tool_finished(self, event: ToolCallFinished) -> None:
        result = event.result
        mark = "ok " if result.ok else "ERR"
        body = (result.output or result.error or "").strip().splitlines()
        self.line(f"+- [{mark}] {body[0] if body else ''}")

    def _verify_section(self, verifier: _RecordingVerifier) -> None:
        result = verifier.last
        if result is None:
            return
        self.line("")
        self.line(f"# verify {'passed' if result.ok else 'FAILED'}")
        for command in result.ran:
            self.line(f"    $ {command}")
        for summary_line in result.summary.splitlines():
            if summary_line.strip():
                self.line(f"    {summary_line}")

    def epilogue(self, workspace: Path, verifier: _RecordingVerifier) -> None:
        self.line("")
        self.line("-" * _WIDTH)
        self.line(f"final {GREETER_FILENAME}:")
        for content_line in (workspace / GREETER_FILENAME).read_text(encoding="utf-8").splitlines():
            self.line(f"    {content_line}")
        self.line("")
        if self.succeeded(verifier):
            self.line(
                "demo complete: feature edited, verified green, and the turn stopped "
                "on evidence (done)."
            )
        else:
            self.line(f"demo did NOT reach a clean done (stop_reason={self.stop_reason}).")

    def succeeded(self, verifier: _RecordingVerifier) -> bool:
        return (
            self.error is None
            and self.completed
            and self.stop_reason == "done"
            and verifier.last is not None
            and verifier.last.ok
        )


async def _drive(engine: TurnEngine, verifier: _RecordingVerifier, emit: Callable[[str], object],
                 workspace: Path) -> bool:
    """Run one turn, narrating each event; return True on a clean, verified done."""
    narrator = _Narrator(emit)
    narrator.header(engine.mode, workspace)
    async for event in engine.run_turn(USER_REQUEST):
        narrator.handle(event, verifier)
    narrator.epilogue(workspace, verifier)
    return narrator.succeeded(verifier)


def run_demo(*, workspace: str | Path | None = None, emit: Callable[[str], object] = print) -> int:
    """Run the scripted offline session; return an exit code (0 == success).

    ``workspace`` is where the demo scaffolds its files and the engine works; when
    ``None`` a throwaway ``tempfile`` directory is created and removed afterward.
    ``emit`` receives each narration line (default ``print``); a test can pass a
    list's ``append`` to capture the transcript without touching stdout.
    """
    if workspace is None:
        tmp = tempfile.mkdtemp(prefix="ironcore-demo-")
        try:
            return run_demo(workspace=tmp, emit=emit)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / GREETER_FILENAME).write_text(GREETER_BEFORE, encoding="utf-8")
    (ws / CHECK_FILENAME).write_text(CHECK_SCRIPT, encoding="utf-8")

    settings = Settings()
    registry = build_default_registry(settings, ws)
    # A real verify command: run the feature check with THIS interpreter, in the
    # workspace. Quoting the exe path keeps it correct on Windows and POSIX.
    verify_command = f'"{sys.executable}" {CHECK_FILENAME}'
    verifier = _RecordingVerifier(CommandVerifier(commands=[verify_command]))
    engine = TurnEngine(
        MockProvider(_script()),
        registry,
        settings,
        _demo_profile(),
        Mode.ACCEPT_EDITS,  # edits auto-apply, unattended (SPEC safety.modes)
        workspace=ws,
        snapshots=None,
        verifier=verifier,
        handoff_path=None,  # keep the workspace to just the demo's own artifacts
    )

    succeeded = asyncio.run(_drive(engine, verifier, emit, ws))
    return 0 if succeeded else 1
