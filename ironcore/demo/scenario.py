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

from rich.text import Text

from ironcore import term
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


def _diff_style(diff_line: str) -> str:
    """Colour a rendered edit line by its polarity; the sign always stays."""
    if diff_line.startswith("-"):
        return term.STYLE_DIFF_MINUS
    if diff_line.startswith("+"):
        return term.STYLE_DIFF_PLUS
    return term.STYLE_MUTED


#: Hanging indent for a card's supporting lines, matching the TUI's tool cards
#: (ironcore/tui/widgets/transcript.py) so the demo reads like the real app.
_INDENT = "    "


class _Narrator:
    """Turns a ``core.events`` stream into a readable, honest transcript.

    The transcript mirrors what the TUI draws for the same event stream: the
    user's line in the accent, assistant prose plain, and each tool call as a
    card with a risk-coloured rule down its left edge, a risk chip, its
    arguments muted, and a green/red result. Nothing is invented for the CLI —
    if the two surfaces disagreed about what a WRITE looks like, the safety
    signal a reader is being trained on would be worth less.

    Every beat is emitted as a :class:`rich.text.Text`, so a caller that wants
    the plain string (the tests, ``--smoke``) reads ``.plain`` and a caller
    printing to a terminal gets the colour. The words are identical either way.
    """

    def __init__(self, emit: Callable[[Text], object]) -> None:
        self._emit = emit
        self._buffer: list[str] = []
        self._glyphs = term.glyphs()
        #: Risk of the call currently on screen, so its result row can be drawn
        #: with the same accent rule as its header.
        self._risk = "read"
        self.completed = False
        self.stop_reason: str | None = None
        self.error: str | None = None

    # -- emit helpers -------------------------------------------------------

    def line(self, text: Text | str = "") -> None:
        self._emit(text if isinstance(text, Text) else Text(text))

    def styled(self, text: str, style: str) -> None:
        out = Text()
        out.append(text, style=style)
        self.line(out)

    def _card_line(self, risk: str, body: Text) -> None:
        """One row of a tool card: the risk-coloured rule, then the content."""
        out = Text()
        out.append(f"{self._glyphs.bar} ", style=term.risk_rule_style(risk))
        out.append(body)
        self.line(out)

    # -- sections -----------------------------------------------------------

    def header(self, mode: Mode, workspace: Path) -> None:
        # The two-tone wordmark, so the demo opens as the same product the TUI
        # masthead brands — IRON molten, CORE steel (term.wordmark).
        self.line(term.wordmark("offline end-to-end demo"))
        self.line(term.rule())
        self.line(term.field("workspace", str(workspace)))
        chip = Text()
        chip.append(term.mode_chip(mode.value), style=term.mode_style(mode.value))
        self.line(term.field("mode", chip))
        self.line(term.field("model", "mock (scripted; no network, no real model)"))
        self.line("")
        request = Text()
        request.append(f"{self._glyphs.user} ", style=term.STYLE_MUTED)
        request.append(f"user: {USER_REQUEST}", style=term.STYLE_USER)
        self.line(request)

    def _flush_text(self) -> None:
        text = "".join(self._buffer).strip()
        self._buffer.clear()
        for para in text.splitlines():
            if para.strip():
                self.line(f"  {para.strip()}")

    def handle(self, event: Event, verifier: _RecordingVerifier) -> None:
        if isinstance(event, TurnStarted):
            self.line("")
            self.styled(
                f"turn {event.turn_id} started {self._glyphs.dot} mode {event.mode}",
                term.STYLE_MUTED,
            )
        elif isinstance(event, TextDelta):
            self._buffer.append(event.text)
        elif isinstance(event, ToolCallRequested):
            self._flush_text()
            self._tool_request(event)
        elif isinstance(event, ApprovalRequired):
            self._flush_text()
            self.styled(f"{_INDENT}approval required: {event.preview}", term.GATE_STYLE["ask"])
        elif isinstance(event, ToolCallFinished):
            self._tool_finished(event)
        elif isinstance(event, TurnCompleted):
            self._flush_text()
            self._verify_section(verifier)
            self.stop_reason = event.stop_reason
            self.completed = True
            self.line("")
            done = event.stop_reason == "done"
            out = Text()
            out.append(f"turn completed {self._glyphs.dot} ", style=term.STYLE_MUTED)
            out.append("stop_reason: ", style=term.STYLE_MUTED)
            out.append(
                str(event.stop_reason),
                style=term.STYLE_OK if done else term.STYLE_WARN,
            )
            self.line(out)
            if event.usage:
                self.styled(f"{_INDENT}usage: {event.usage}", term.STYLE_MUTED)
        elif isinstance(event, TurnError):
            self._flush_text()
            self.error = event.message
            self.styled(f"turn error: {event.message}", term.STYLE_FAIL)

    def _tool_request(self, event: ToolCallRequested) -> None:
        call = event.call
        risk = str(event.risk)
        self.line("")
        header = Text()
        header.append(call.name, style=term.STYLE_TOOL_NAME)
        header.append("  ")
        header.append(term.risk_chip(risk), style=term.risk_style(risk))
        header.append("  ")
        header.append(str(event.decision), style=term.gate_style(str(event.decision)))
        self._card_line(risk, header)
        if call.name == "edit_file":
            args = Text()
            args.append(
                f"{_INDENT}path={call.arguments.get('path')!r}  "
                f"format={call.arguments.get('format')!r}",
                style=term.STYLE_MUTED,
            )
            self._card_line(risk, args)
            for diff_line in _render_edit(str(call.arguments.get("edit", ""))):
                body = Text()
                body.append(f"{_INDENT}{diff_line}", style=_diff_style(diff_line))
                self._card_line(risk, body)
        else:
            body = Text()
            body.append(f"{_INDENT}{_args_preview(call.arguments)}", style=term.STYLE_MUTED)
            self._card_line(risk, body)
        self._risk = risk

    def _tool_finished(self, event: ToolCallFinished) -> None:
        result = event.result
        body = (result.output or result.error or "").strip().splitlines()
        out = Text()
        out.append(_INDENT)
        out.append(
            self._glyphs.ok if result.ok else self._glyphs.bad,
            style=term.STYLE_OK if result.ok else term.STYLE_FAIL,
        )
        if body:
            out.append(f"  {body[0]}", style=term.STYLE_MUTED)
        self._card_line(self._risk, out)
        # Close the card. With a blank row above the header too, the transcript
        # reads prose / card / prose / card instead of one undifferentiated run.
        self.line("")

    def _verify_section(self, verifier: _RecordingVerifier) -> None:
        result = verifier.last
        if result is None:
            return
        self.line("")
        head = Text()
        # Bold, matching the "final greeter.py" heading below: the two beats that
        # close a session should carry the same weight as each other.
        head.append("verify ", style=term.STYLE_HEADING)
        head.append(
            "passed" if result.ok else "FAILED",
            style=term.STYLE_OK if result.ok else term.STYLE_FAIL,
        )
        self.line(head)
        for command in result.ran:
            self.styled(f"{_INDENT}$ {command}", term.STYLE_MUTED)
        for summary_line in result.summary.splitlines():
            if summary_line.strip():
                self.styled(f"{_INDENT}{summary_line}", term.STYLE_MUTED)

    def epilogue(self, workspace: Path, verifier: _RecordingVerifier) -> None:
        self.line("")
        self.line(term.rule())
        self.styled(f"final {GREETER_FILENAME}", term.STYLE_HEADING)
        # The file itself stays at the terminal's own foreground: it is the
        # artifact the whole session was for, not supporting detail.
        for content_line in (workspace / GREETER_FILENAME).read_text(encoding="utf-8").splitlines():
            self.line(f"{_INDENT}{content_line}")
        self.line("")
        if self.succeeded(verifier):
            self.styled(
                "demo complete: feature edited, verified green, and the turn stopped "
                "on evidence (done).",
                f"bold {term.SUCCESS}",
            )
        else:
            self.styled(
                f"demo did NOT reach a clean done (stop_reason={self.stop_reason}).",
                term.STYLE_FAIL,
            )

    def succeeded(self, verifier: _RecordingVerifier) -> bool:
        return (
            self.error is None
            and self.completed
            and self.stop_reason == "done"
            and verifier.last is not None
            and verifier.last.ok
        )


async def _drive(engine: TurnEngine, verifier: _RecordingVerifier, emit: Callable[[Text], object],
                 workspace: Path) -> bool:
    """Run one turn, narrating each event; return True on a clean, verified done."""
    narrator = _Narrator(emit)
    narrator.header(engine.mode, workspace)
    async for event in engine.run_turn(USER_REQUEST):
        narrator.handle(event, verifier)
    narrator.epilogue(workspace, verifier)
    return narrator.succeeded(verifier)


def run_demo(
    *, workspace: str | Path | None = None, emit: Callable[[str], object] | None = None
) -> int:
    """Run the scripted offline session; return an exit code (0 == success).

    ``workspace`` is where the demo scaffolds its files and the engine works; when
    ``None`` a throwaway ``tempfile`` directory is created and removed afterward.

    ``emit`` receives each narration line as a plain ``str``; a test can pass a
    list's ``append`` to capture the transcript without touching stdout, and
    ``--smoke`` passes one to collapse the narration. Left at ``None`` the
    transcript goes to the shared terminal console *styled* — the words are
    identical either way, because the styled and plain forms are the same
    :class:`~rich.text.Text` read two ways.
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

    sink: Callable[[Text], object] = (
        term.line if emit is None else (lambda text: emit(text.plain))
    )
    succeeded = asyncio.run(_drive(engine, verifier, sink, ws))
    return 0 if succeeded else 1
