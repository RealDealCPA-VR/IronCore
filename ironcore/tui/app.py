"""IronCoreApp: the Textual front end (IC-701..704).

A THIN client over the turn engine. It owns no agent logic: it submits input
to ``engine.run_turn`` and renders the resulting ``core.events`` stream, and it
answers approval futures via the engine's ``ApprovalBroker``. Nothing here is
imported by ``core/`` (the dependency arrow only points inward —
docs/ARCHITECTURE.md §4), and the engine never prints or prompts; every visible
thing is an event this app chose to render.

Event → view mapping (SPEC §3.1):

* ``TextDelta``          → append to the current assistant bubble in place.
* ``ToolCallRequested``  → mount a tool card (name, args, risk, gate decision).
* ``ApprovalRequired``   → mark the card awaiting + push the approval modal.
* ``ToolCallFinished``   → collapse the card to an ok/error result.
* ``TurnCompleted``      → bump the status meter; note a non-``done`` stop reason.
* ``TurnError``          → an error note.

Controls:

* Shift+Tab cycles ``safety.modes.CYCLE`` (manual → accept-edits → auto → plan),
  updates the status chip and ``engine.mode``, and announces the change.
* Esc interrupts the running turn — the driving worker is cancelled; already
  streamed output stays on screen (SPEC §3.1).
* ``/`` opens the slash palette (registry commands, ``[planned]`` tagged); Tab
  completes the top match; a full ``/name args`` line dispatches via the
  registry, unknowns get a nearest-match hint.

Phase-8 command integration contract (docs/ARCHITECTURE.md §6, SPEC §3.3).
Every dispatched command receives a ``CommandContext`` whose ``extra`` dict
carries exactly these keys — future handlers (IC-801..807) consume them:

    ``app``                this ``IronCoreApp`` (for pushing screens / notes)
    ``engine``             the live ``TurnEngine`` (mode, workspace, provider)
    ``registry``           the ``CommandRegistry`` (``/help`` lists it)
    ``workspace``          the workspace ``Path``
    ``provider_registry``  the ``ProviderRegistry`` or ``None`` (``/model`` uses it)
    ``settings``           the loaded ``Settings``
    ``schedule``           ``Callable[[Coroutine], None]`` — see ``_schedule``:
                           runs the coroutine as a background worker and posts
                           its (string) result to the transcript. Handlers that
                           do long work (``/loop``, ``/workflow``) return a short
                           acknowledgement immediately and ``schedule`` the rest.
"""

from __future__ import annotations

from collections.abc import Coroutine
from difflib import get_close_matches
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from ironcore.commands import (
    CommandContext,
    CommandRegistry,
    UnknownCommand,
)
from ironcore.commands import build_default_registry as build_command_registry
from ironcore.commands.base import SlashCommand
from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalAnswer, ApprovalRequest
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
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.registry import ProviderRegistry
from ironcore.safety.modes import DESCRIPTIONS, Mode, next_mode
from ironcore.tools.default import build_default_registry as build_tool_registry
from ironcore.tui.screens.approval import ApprovalScreen
from ironcore.tui.widgets import InputBar, StatusBar, Transcript


def match_commands(registry: CommandRegistry, prefix: str) -> list[SlashCommand]:
    """Registry commands matching ``prefix`` — name-prefix hits first, then
    substring hits. Pure and reused by IC-704 completion + tests."""
    prefix = prefix.strip().lower()
    commands = registry.all()
    if not prefix:
        return commands
    starts = [c for c in commands if c.name.startswith(prefix)]
    contains = [c for c in commands if prefix in c.name and c not in starts]
    return starts + contains


def _render_palette(matches: list[SlashCommand]) -> str:
    lines = []
    for i, cmd in enumerate(matches):
        tag = "" if cmd.implemented else "   [planned]"
        marker = "›" if i == 0 else " "
        lines.append(f"{marker} /{cmd.name} — {cmd.summary}{tag}")
    return "\n".join(lines)


class IronCoreApp(App):
    """The interactive TUI. Tests inject their own MockProvider-backed engine
    and command registry; production builds them via ``from_settings``."""

    CSS = """
    Screen { layout: vertical; }
    #transcript { height: 1fr; padding: 0 1; }
    #transcript .user { text-style: bold; color: $accent; }
    #transcript .assistant { color: $text; }
    #transcript .note { text-style: dim; }
    #transcript .tool-card { color: $secondary; margin: 0 0 0 1; }
    #palette {
        display: none;
        height: auto;
        max-height: 8;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    InputBar { height: 3; border: tall $primary; }
    StatusBar { height: 1; background: $boost; color: $text; padding: 0 1; }
    ApprovalScreen { align: center middle; }
    #approval-box {
        width: 80%;
        max-width: 100;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #approval-title { text-style: bold; width: 100%; }
    #approval-preview { height: auto; max-height: 20; margin: 1 0; }
    #approval-buttons { height: auto; align: center middle; }
    #approval-buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("shift+tab", "cycle_mode", "Cycle mode", priority=True),
        Binding("escape", "interrupt", "Interrupt turn"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def __init__(
        self,
        engine: TurnEngine,
        registry: CommandRegistry,
        settings: Settings,
        *,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.registry = registry
        self.settings = settings
        self.provider_registry = provider_registry
        self.workspace: Path = Path(engine.workspace)
        self._goal: str | None = engine.state.goal
        self._turn_worker: Worker | None = None
        self._matches: list[SlashCommand] = []
        #: call id of the request currently awaiting an approval verdict.
        self._awaiting_call_id: str | None = None

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Transcript()
        yield Static(id="palette")
        yield InputBar()
        yield StatusBar(mode=self.engine.mode, model=self.settings.provider.model)

    def on_mount(self) -> None:
        self.transcript = self.query_one(Transcript)
        self.status_bar = self.query_one(StatusBar)
        # The engine emits ApprovalRequired AND awaits the broker; wiring the
        # broker's on_request to our modal is how the ask becomes a keystroke.
        self.engine.approvals.on_request = self._on_approval_request
        self.query_one(InputBar).focus()
        self._post_note(
            "IronCore ready. Type a message or /help. "
            "Shift+Tab cycles mode · Esc interrupts."
        )

    # -- input handling -------------------------------------------------------

    def on_input_changed(self, event: InputBar.Changed) -> None:
        self._refresh_palette(event.value)

    def on_input_submitted(self, event: InputBar.Submitted) -> None:
        self.query_one(InputBar).value = ""
        self._hide_palette()
        value = event.value.strip()
        if not value:
            return
        if value.startswith("/"):
            self._submit_slash(value)
            return
        if self._turn_running():
            self._post_note("[busy — a turn is already running; press Esc to interrupt]")
            return
        self._start_turn(value)

    # -- slash palette (IC-704) ----------------------------------------------

    def _refresh_palette(self, value: str) -> None:
        palette = self.query_one("#palette", Static)
        if value.startswith("/") and " " not in value:
            self._matches = match_commands(self.registry, value[1:])
            if self._matches:
                palette.update(Text(_render_palette(self._matches)))
                palette.display = True
                return
        self._matches = []
        palette.display = False

    def _hide_palette(self) -> None:
        self._matches = []
        self.query_one("#palette", Static).display = False

    def action_complete(self) -> None:
        """Tab: fill the top palette match (IC-704)."""
        if self._matches:
            self.query_one(InputBar).value = f"/{self._matches[0].name} "

    def _submit_slash(self, value: str) -> None:
        name = value[1:].split(" ", 1)[0]
        # A bare, unknown prefix completes rather than erroring — Enter completes.
        if " " not in value and self.registry.get(name) is None:
            matches = match_commands(self.registry, name)
            if matches and matches[0].name != name:
                self.query_one(InputBar).value = f"/{matches[0].name} "
                return
        self._dispatch(value)

    def _dispatch(self, line: str) -> None:
        ctx = self._command_context()
        try:
            result = self.registry.dispatch(line, ctx)
        except UnknownCommand as exc:
            bad = exc.args[0] if exc.args else line.lstrip("/")
            near = get_close_matches(str(bad), [c.name for c in self.registry.all()], n=1)
            hint = f" Did you mean /{near[0]}?" if near else " Type /help for the list."
            self._post_note(f"Unknown command /{bad}.{hint}")
            return
        except ValueError as exc:
            self._post_note(f"[command error] {exc}")
            return
        # Commands mutate only the context; reflect mode/goal changes back.
        if ctx.mode != self.engine.mode:
            self._set_mode(ctx.mode, announce=False)
        self._goal = ctx.goal
        if result:
            self._post_note(result)

    def _command_context(self) -> CommandContext:
        ctx = CommandContext(settings=self.settings, mode=self.engine.mode, goal=self._goal)
        ctx.extra = {
            "app": self,
            "engine": self.engine,
            "registry": self.registry,
            "workspace": self.workspace,
            "provider_registry": self.provider_registry,
            "settings": self.settings,
            "schedule": self._schedule,
        }
        return ctx

    def _schedule(self, coro: Coroutine) -> None:
        """Phase-8 contract: run ``coro`` in a background worker and post its
        string result (or an error) to the transcript. Returns immediately so a
        command handler never blocks the UI (docs/ARCHITECTURE.md §6)."""

        async def _runner() -> None:
            try:
                result = await coro
            except Exception as exc:  # a scheduled task must not kill the app
                result = f"[error] {exc}"
            if result:
                await self.transcript.add_note(str(result))

        self.run_worker(_runner(), group="command")

    # -- modes (IC-703) -------------------------------------------------------

    def action_cycle_mode(self) -> None:
        self._set_mode(next_mode(self.engine.mode), announce=True)

    def _set_mode(self, mode: Mode, *, announce: bool) -> None:
        self.engine.mode = mode  # the engine reads self.mode at gate time
        self.status_bar.set_mode(mode)
        if announce:
            self._post_note(f"Mode → {mode.value}: {DESCRIPTIONS[mode]}")

    # -- turn driving ---------------------------------------------------------

    def _turn_running(self) -> bool:
        w = self._turn_worker
        return w is not None and w.state in (WorkerState.PENDING, WorkerState.RUNNING)

    def _start_turn(self, text: str) -> None:
        self._turn_worker = self.run_worker(
            self._drive_turn(text), group="turn", exclusive=True
        )

    async def _drive_turn(self, text: str) -> None:
        await self.transcript.add_user(text)
        self.status_bar.set_running(True)
        try:
            async for event in self.engine.run_turn(text):
                await self._handle_event(event)
        except Exception as exc:  # engine/provider defect must not crash the app
            await self.transcript.add_note(f"[error] {exc}")
        finally:
            # CancelledError (Esc) skips the except, runs this, then re-raises —
            # partial output already rendered stays on screen (SPEC §3.1).
            self.status_bar.set_running(False)

    async def _handle_event(self, event: Event) -> None:
        t = self.transcript
        if isinstance(event, TurnStarted):
            return
        if isinstance(event, TextDelta):
            await t.append_assistant(event.text)
        elif isinstance(event, ToolCallRequested):
            await t.add_card(event.call, event.risk, event.decision)
        elif isinstance(event, ApprovalRequired):
            self._awaiting_call_id = event.call.id
            card = t.card(event.call.id)
            if card is not None:
                card.set_state("awaiting approval")
        elif isinstance(event, ToolCallFinished):
            card = t.card(event.call.id)
            if card is not None:
                card.set_finished(event.result)
        elif isinstance(event, TurnCompleted):
            t.end_assistant()
            self.status_bar.record_turn(event.usage)
            if event.stop_reason != "done":
                await t.add_note(f"[turn ended: {event.stop_reason}]")
        elif isinstance(event, TurnError):
            t.end_assistant()
            await t.add_note(f"[error] {event.message}")

    def action_interrupt(self) -> None:
        """Esc: cancel the running turn, keep partial output."""
        w = self._turn_worker
        if w is not None and w.state in (WorkerState.PENDING, WorkerState.RUNNING):
            w.cancel()
            self.transcript.end_assistant()
            self._post_note("[interrupted]")

    # -- approvals (IC-703) ---------------------------------------------------

    async def _on_approval_request(self, request: ApprovalRequest) -> None:
        """Broker ``on_request`` callback: raise the modal, resolve on dismiss.

        Runs inside the turn worker (same event loop). It only *shows* the
        modal and returns; the broker then awaits its future, which the dismiss
        callback resolves via ``broker.answer`` (approvals.py). The dismiss
        value maps y/n/a → ApprovalAnswer in ``ApprovalScreen``.
        """
        call_id = self._awaiting_call_id

        def _answered(answer: ApprovalAnswer | None) -> None:
            if answer is None:  # defensive: no ambiguous-dismiss path exists
                answer = ApprovalAnswer(decision="deny", reason="dismissed")
            card = self.transcript.card(call_id) if call_id else None
            if card is not None and answer.decision != "approve":
                card.set_denied(answer.reason)
            try:
                self.engine.approvals.answer(request.id, answer)
            except KeyError:
                pass  # already resolved by timeout or an Esc interrupt

        self.push_screen(ApprovalScreen(request), _answered)

    # -- notes ----------------------------------------------------------------

    def _post_note(self, text: str) -> None:
        """Add a transcript note from a non-worker context, ordered via the
        message pump (call_later awaits the returned coroutine)."""
        self.call_later(self.transcript.add_note, text)

    # -- test / introspection surface ----------------------------------------

    def transcript_text(self) -> str:
        """The whole transcript as plain text (read surface for tests)."""
        return self.transcript.plain_text()

    # -- production factory ---------------------------------------------------

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        workspace: str | Path | None = None,
    ) -> IronCoreApp:
        """Build the real engine + registries from ``Settings`` (the ``ironcore``
        launch path). Tests bypass this and inject their own engine."""
        ws = Path(workspace) if workspace is not None else Path.cwd()
        if settings is None:
            settings = Settings.load(project_dir=ws)
        provider_registry = ProviderRegistry.from_settings(settings)
        tools = build_tool_registry(settings, ws)
        model = settings.provider.model
        envelope_dir = Path.home() / ".ironcore" / "envelopes"
        profile = CapabilityProfile.load(envelope_dir, model) or CapabilityProfile(model_id=model)
        try:
            mode = Mode(settings.safety.mode)
        except ValueError:
            mode = Mode.MANUAL
        engine = TurnEngine(
            provider_registry.default,
            tools,
            settings,
            profile,
            mode,
            workspace=ws,
        )
        return cls(engine, build_command_registry(), settings, provider_registry=provider_registry)


def run_app(settings: Settings | None = None, workspace: str | Path | None = None) -> int:
    """Launch the TUI (the ``ironcore`` no-subcommand entry point)."""
    app = IronCoreApp.from_settings(settings=settings, workspace=workspace)
    app.run()
    return 0
