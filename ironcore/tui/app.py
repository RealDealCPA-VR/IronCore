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
    ``envelope_dir``       the on-disk envelope cache ``Path`` — resolved ONCE at
                           construction (MS-2); ``/model`` lookups and background
                           deepens share it, and later per-role / outcome
                           consumers must reuse the same resolution
    ``plugin_probes``      entry-point plugin probes (MS-5) for ``/probe`` to
                           append to the default battery; may be empty
    ``schedule``           ``Callable[[Coroutine], None]`` — see ``_schedule``:
                           runs the coroutine as a background worker and posts
                           its (string) result to the transcript. Handlers that
                           do long work (``/loop``, ``/workflow``) return a short
                           acknowledgement immediately and ``schedule`` the rest.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine, Sequence
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING

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
    ResampleProgress,
    TextDelta,
    ToolCallFinished,
    ToolCallRequested,
    TurnCompleted,
    TurnError,
    TurnStarted,
)
from ironcore.core.roles import RoleRouter
from ironcore.envelope.outcomes import OutcomeLedger, apply_tuning
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.suite import default_envelope_dir
from ironcore.memory.sessions import SessionStore
from ironcore.plugins import load_plugins
from ironcore.providers.base import Message
from ironcore.providers.registry import ProviderRegistry, select_provider_factory
from ironcore.safety.modes import DESCRIPTIONS, Mode, next_mode
from ironcore.tools.default import build_default_registry as build_tool_registry
from ironcore.tools.mcp import MCPManager
from ironcore.tui import theme
from ironcore.tui.screens.approval import ApprovalScreen
from ironcore.tui.screens.sessions import SessionPicker
from ironcore.tui.theme import CSS_VARIABLES, IRONCORE_THEME
from ironcore.tui.widgets import InputBar, StatusBar, Transcript

if TYPE_CHECKING:
    from ironcore.commands.loopcmd import LoopSpec

#: ``--resume`` with no id: open the picker at launch instead of resuming one.
RESUME_PICK = "__pick__"

#: How often the /loop driver polls turn-idle / its own liveness (IC-804). Small
#: enough that a tick fires promptly once the engine frees up, large enough not
#: to peg the event loop between ticks of a self-paced loop.
LOOP_POLL_S = 0.05

#: Provider calls the default probe battery issues at full depth (measured:
#: 16+12+30+10+3+1+3 = 75; probes that short-circuit on failure issue fewer).
#: Quoted in the first-run note so the burst is never a silent surprise.
PROBE_CALL_ESTIMATE = 80


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


def _new_session_id() -> str:
    """A filesystem-safe, chronologically sortable session id, stamped now.

    ``YYYYmmddTHHMMSS-<hex>`` — unique per launch and free of path separators, so
    ``SessionStore``'s id validation always accepts it. Runtime ``datetime.now``
    is fine here: app.py is not a frozen-determinism module (the store is)."""
    return f"{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


def _render_palette(matches: list[SlashCommand]) -> Text:
    """The slash palette as styled ``Text``.

    Three ranks: the top match's ``›`` marker is the accent (Tab/Enter takes
    it), the command NAMES are what you scan so they carry the weight, and the
    summaries recede. The wording and column layout are unchanged — only the
    emphasis is new.
    """
    text = Text(no_wrap=True)
    for i, cmd in enumerate(matches):
        if i:
            text.append("\n")
        top = i == 0
        text.append("› " if top else "  ", style=theme.STYLE_USER)
        text.append(f"/{cmd.name}", style=theme.STYLE_TOOL_NAME if top else theme.FOREGROUND)
        text.append(f" — {cmd.summary}", style=theme.STYLE_MUTED)
        if not cmd.implemented:
            text.append("   [planned]", style=theme.STYLE_MUTED)
    return text


class IronCoreApp(App):
    """The interactive TUI. Tests inject their own MockProvider-backed engine
    and command registry; production builds them via ``from_settings``."""

    #: The look is documented in ``tui/theme.py``; this sheet only spends the
    #: tokens that theme registers. Two rules drive most of what is below:
    #: emphasis comes from weight and spacing (not from more colour), and every
    #: colour has to mean something (risk, outcome, autonomy).
    CSS = """
    Screen { layout: vertical; background: $background; }

    /* -- transcript ------------------------------------------------------- */
    /* Bottom-aligned like a shell: a short session sits just above the input
       where the eye already is, instead of stranding it against a black void
       at the top of the frame. Content taller than the pane scrolls normally. */
    #transcript {
        height: 1fr;
        padding: 0 2;
        align-vertical: bottom;
        scrollbar-size-vertical: 1;
    }
    /* Turn rhythm: the user's line opens a turn, so it carries the whitespace
       above it. Everything the assistant does in reply stays tight to it. */
    #transcript .user { text-style: bold; color: $accent; margin: 1 0 0 0; }
    #transcript .assistant { color: $text; }
    #transcript .note { color: $text-muted; }
    #transcript .masthead { margin: 0 0 1 0; }

    /* Tool cards: a risk-coloured accent rule plus a faint panel, rather than
       a full border — see ToolCard's docstring for why a box loses here. */
    /* `wide` (▎) rather than `outer` (▌): a slim rule reads as an accent, a
       fat one reads as a slab of colour competing with the risk chip. */
    #transcript .tool-card {
        background: $panel 55%;
        border-left: wide $primary;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    #transcript .tool-card.risk-read { border-left: wide $risk-read; }
    #transcript .tool-card.risk-write { border-left: wide $risk-write; }
    #transcript .tool-card.risk-exec { border-left: wide $risk-exec; }
    #transcript .tool-card.risk-net { border-left: wide $risk-net; }
    /* A card that wants a human, or that went wrong, earns a brighter bed —
       loud enough to pull the eye clean out of a column of calm read cards. */
    #transcript .tool-card.state-awaiting { background: $warning 15%; }
    #transcript .tool-card.state-bad { background: $error 15%; }

    /* -- slash palette ---------------------------------------------------- */
    #palette {
        display: none;
        height: auto;
        max-height: 8;
        background: $panel;
        color: $text;
        border-left: wide $accent;
        padding: 0 1;
    }

    /* -- input + status --------------------------------------------------- */
    /* `round` instead of `tall`: the tall variant paints thick blocks down
       both sides, which is the single most dated thing on the old frame. The
       resting border is $edge — a visible steel hairline, not the near-black
       $panel that read as no frame at all — and focus lifts it to the accent. */
    InputBar { height: 3; border: round $edge; background: $surface; }
    InputBar:focus { border: round $primary; }
    StatusBar { height: 1; background: $panel; padding: 0 1; }

    /* -- modals ----------------------------------------------------------- */
    /* A translucent scrim, not a blackout: the tool card that raised this ask
       must stay readable BEHIND the modal, because "what am I approving and
       where did it come from" is answered by that context. */
    ApprovalScreen { align: center middle; background: $background 70%; }
    #approval-box {
        width: 80%;
        max-width: 100;
        height: auto;
        border: round $warning;
        border-title-align: left;
        border-title-color: $warning;
        border-subtitle-align: right;
        border-subtitle-color: $text-muted;
        background: $surface;
        padding: 1 3;
    }
    /* The border is the risk signal here too, so an EXEC/NET ask is red before
       it is read. Defaulting to $warning keeps an unknown class visible. */
    #approval-box.risk-exec, #approval-box.risk-net {
        border: round $error;
        border-title-color: $error;
    }
    #approval-box.risk-read {
        border: round $primary;
        border-title-color: $primary;
    }
    #approval-title { width: 100%; }
    #approval-preview { height: auto; max-height: 20; margin: 1 0; }
    #approval-buttons { height: auto; align: center middle; margin: 1 0 0 0; }
    /* Flat one-row actions. The diff above is the evidence being judged; three
       saturated blocks used to shout louder than the thing they act on. */
    #approval-buttons Button {
        border: none;
        height: 1;
        min-width: 0;
        margin: 0 1;
        padding: 0 2;
        background: transparent;
        color: $text-muted;
        text-style: none;
    }
    #approval-buttons #approve { color: $success; }
    #approval-buttons #deny { color: $error; }
    #approval-buttons #approve-all { color: $warning; }
    /* The unfocused verdicts stay flat coloured text — the diff above is the
       evidence, not these. The one the keyboard will act on (Deny for EXEC/NET,
       Approve otherwise) fills with its own colour, so what Enter does is never
       in doubt without turning all three into competing blocks. */
    #approval-buttons Button:focus { text-style: bold; }
    #approval-buttons #approve:focus { background: $success; color: $background; }
    #approval-buttons #deny:focus { background: $error; color: $background; }
    #approval-buttons #approve-all:focus { background: $warning; color: $background; }

    SessionPicker { align: center middle; }
    #session-box {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 24;
        border: round $primary;
        border-title-align: left;
        border-title-color: $primary;
        border-subtitle-align: right;
        border-subtitle-color: $text-muted;
        background: $surface;
        padding: 1 2;
    }
    #session-list { height: auto; max-height: 16; background: $surface; }
    /* No `background` here: an #id rule out-specifies ListView's own
       `.-highlight` cursor rule and would silently erase the selection band. */
    #session-list > ListItem { padding: 0 1; }
    /* The row cursor itself is themed (theme.py `block-cursor-*`), not styled
       here — ListView's own :focus rule out-specifies this sheet. */
    #session-empty { margin: 1 0; color: $text-muted; }
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
        session_store: SessionStore | None = None,
        resume_id: str | None = None,
        auto_probe: bool = False,
        instant_seed: bool = False,
        envelope_dir: Path | None = None,
        boot_notes: tuple[str, ...] = (),
        mcp_manager: MCPManager | None = None,
        plugin_probes: Sequence[object] = (),
    ) -> None:
        super().__init__()
        # Before anything renders: the app ships its own palette (tui/theme.py)
        # rather than inheriting Textual's default, so the first frame is
        # already IronCore rather than a generic purple.
        self.register_theme(IRONCORE_THEME)
        self.theme = IRONCORE_THEME.name
        self.engine = engine
        self.registry = registry
        self.settings = settings
        #: single source for the on-disk envelope cache (MS-2): /model swap
        #: lookups and the background deepen write here, and later per-role /
        #: outcome consumers reuse the same resolution. Tests inject a tmp dir.
        self.envelope_dir: Path = (
            envelope_dir if envelope_dir is not None else default_envelope_dir()
        )
        #: mold the model in the background on first launch (from_settings sets both
        #: for an unprobed model): ``_instant_seed`` introspects the endpoint into a
        #: provisional-but-usable profile in ~1s; ``_auto_probe`` then measures it.
        self._instant_seed = instant_seed
        self._auto_probe = auto_probe
        #: one-shot notes posted at mount (MS-8: envelope-tuning adjustments +
        #: re-probe hints from ``from_settings``; empty for injected engines).
        self._boot_notes: tuple[str, ...] = tuple(boot_notes)
        #: MCP tool servers (MS-7): connected by a background worker at mount,
        #: closed at unmount. None = no servers configured (or NET tools off).
        self._mcp_manager = mcp_manager
        #: plugin probes (MS-5): appended to the default battery by the
        #: auto-probe path and exposed to /probe via ctx.extra["plugin_probes"].
        self._plugin_probes: tuple[object, ...] = tuple(plugin_probes)
        self.provider_registry = provider_registry
        self.workspace: Path = Path(engine.workspace)
        self._goal: str | None = engine.state.goal
        self._turn_worker: Worker | None = None
        #: the one active /loop for this session (IC-804), or None. Identity is
        #: the driver's liveness token: replacing or stopping the loop swaps/clears
        #: it, and ``_run_loop`` exits the moment ``self._loop_spec is not`` its own.
        self._loop_spec: LoopSpec | None = None
        self._loop_worker: Worker | None = None
        self._matches: list[SlashCommand] = []
        #: call id of the request currently awaiting an approval verdict.
        self._awaiting_call_id: str | None = None
        # -- session recording (IC-706). ``session_store=None`` disables it
        # entirely (existing shell tests); ``from_settings`` injects a real
        # store so production records. ``resume_id`` == RESUME_PICK means "open
        # the picker at launch"; a concrete id resumes that session directly.
        self.session_store = session_store
        self._resume_pick = resume_id == RESUME_PICK
        self._resume_id = None if self._resume_pick else resume_id
        #: id of the session being written; created lazily on the first user turn
        #: (so its first_prompt label is meaningful), reused across a resume.
        self._session_id: str | None = self._resume_id
        self._session_created = self._resume_id is not None
        #: assistant text accumulated across the current turn, flushed on finish.
        self._turn_assistant: list[str] = []

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Transcript()
        yield Static(id="palette")
        yield InputBar()
        yield StatusBar(mode=self.engine.mode, model=self.settings.provider.model)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Design tokens this app's CSS spends that no built-in theme defines.

        Textual parses ``CSS`` before any theme is applied, and a variable
        referenced in CSS but defined nowhere is a startup crash — so the risk
        tokens are declared HERE as fallbacks rather than only inside
        ``IRONCORE_THEME``. That also means switching to a stock Textual theme
        (the command palette can) still resolves them instead of failing.
        """
        return dict(CSS_VARIABLES)

    def on_mount(self) -> None:
        self.transcript = self.query_one(Transcript)
        self.status_bar = self.query_one(StatusBar)
        # The engine emits ApprovalRequired AND awaits the broker; wiring the
        # broker's on_request to our modal is how the ask becomes a keystroke.
        self.engine.approvals.on_request = self._on_approval_request
        self.query_one(InputBar).focus()
        self.call_later(self.transcript.add_masthead)
        for note in self._boot_notes:  # MS-8: tuning adjustments / re-probe hints
            self._post_note(note)
        # Resume flow (IC-706): a picker for a bare --resume, a direct rehydrate
        # for --resume <id>. Both are no-ops without a store.
        if self.session_store is not None:
            if self._resume_pick:
                self.push_screen(SessionPicker(self.session_store), self._on_session_picked)
            elif self._resume_id is not None:
                self.call_later(self._resume_session, self._resume_id)
        # First-use molding (docs/MODELS.md, instant-on-profiling): make an
        # unprobed model usable immediately. The user works on floor defaults now;
        # a background worker SEEDS the profile from endpoint introspection (~1s,
        # hot-swap #1) then DEEP-PROBES to measure + refine it (hot-swap #2).
        if self._instant_seed or self._auto_probe:
            self._post_note(
                f"Model {self.settings.provider.model!r} is unprobed — measuring it "
                "in the background so IronCore molds to it. You can work now on floor "
                "defaults; the profile hot-swaps itself as measurements land (or /probe)."
            )
            if self._auto_probe:
                # Say what it actually costs. A silent burst of ~80 calls at a
                # local 30B reads as "this thing hung", and quitting mid-probe is
                # exactly how the envelope cache used to get corrupted.
                self._post_note(
                    f"  Deep probe: ~{PROBE_CALL_ESTIMATE} short calls, typically 1-3 min "
                    "on a local model; your turns keep working meanwhile. Turn it off with "
                    "[envelope] auto_probe = false."
                )
            self.run_worker(self._mold_to_model(), group="probe")
        # MCP tool servers (MS-7): connect in the background and register their
        # tools into the LIVE registry. Late registration is safe — the engine
        # recomputes ``tools.specs()`` per CALL, so new tools simply appear on
        # the next provider call; a note line reports each server's outcome.
        if self._mcp_manager is not None:
            self.run_worker(self._connect_mcp(), group="mcp")

    async def _connect_mcp(self) -> None:
        """Background MCP registration: post the manager's note lines. Never
        raises — ``register_into`` is per-server fault-isolated, and a surprise
        must not crash mount."""
        try:
            notes = await self._mcp_manager.register_into(self.engine.tools)
        except Exception as exc:  # noqa: BLE001 — a defect must not kill the app
            notes = [f"[mcp] connect failed: {exc}"]
        for note in notes:
            await self.transcript.add_note(note)

    async def on_unmount(self) -> None:
        """App shutdown: close MCP server subprocesses (best-effort)."""
        if self._mcp_manager is not None:
            await self._mcp_manager.aclose()

    async def _mold_to_model(self) -> None:
        """Background first-use molding: SEED the profile from endpoint
        introspection (provisional, instant) then DEEP-PROBE to measure + refine
        it. Each step hot-swaps ``engine.profile`` in order (seed before probe).
        Never raises — a seed failure is noted and the probe still runs."""
        # SEED (hot-swap #1): only for an endpoint-backed provider (Ollama et al.).
        # seed_profile shouldn't raise, but a surprise must not crash mount.
        if self._instant_seed and getattr(self.engine.provider, "base_url", None) is not None:
            try:
                from ironcore.envelope.seed import seed_profile

                model = self.engine.profile.model_id or self.settings.provider.model
                seed = await seed_profile(self.engine.provider, model_id=model)
                self.engine.profile = seed  # the next turn uses the real window + tools
                await self.transcript.add_note(
                    f"Seeded {model!r} from the endpoint: context {seed.honest_context}, "
                    f"tools {seed.recommended_tool_protocol()!r}, "
                    f"edits {seed.recommended_edit_format()!r} "
                    "(provisional — measuring in the background)."
                )
            except Exception as exc:  # noqa: BLE001 — seeding must never crash mount
                await self.transcript.add_note(f"[seed skipped] {exc}")
        # DEEP PROBE (hot-swap #2): refines base=the seed (probe_and_swap catches
        # its own failures, so this never raises).
        if self._auto_probe:
            from ironcore.commands.envelopecmd import probe_and_swap

            # envelope_dir: the app-wide resolution (MS-2) so this deepen's write
            # lands where /model swap lookups will read it. Plugin probes (MS-5)
            # join the default battery here exactly as they do for /probe — the
            # kwarg is passed only when plugins supplied probes, so the
            # zero-plugin call stays byte-identical (and substitute probe
            # functions need not accept it).
            kwargs: dict[str, object] = {"envelope_dir": self.envelope_dir}
            if self._plugin_probes:
                kwargs["extra_probes"] = self._plugin_probes
            report = await probe_and_swap(self.engine, **kwargs)
            await self.transcript.add_note(report)

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
                palette.update(_render_palette(self._matches))
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
        # Echo the command as the user's own line before running it. A command
        # session used to render as an unbroken column of grey results with the
        # question missing — you could not tell which output answered what, and
        # scrolling back told you nothing. The echo is display-only: slash
        # commands are still not recorded to the session transcript.
        self.call_later(self.transcript.add_user, line)
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
        # /model swaps live (MS-2); the settings object is shared, so re-read it.
        self.status_bar.set_model(self.settings.provider.model)
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
            "envelope_dir": self.envelope_dir,
            "plugin_probes": self._plugin_probes,
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
                # A styled result (``/goal check``'s met/unmet payoff) passes
                # through intact; anything else is coerced to a string exactly
                # as it always was.
                await self.transcript.add_note(
                    result if isinstance(result, Text) else str(result)
                )

        self.run_worker(_runner(), group="command")

    def inject_context(self, text: str) -> None:
        """Command hook (``/skill``): place trusted standing text into the running
        conversation so the next turn composes it. Appends a system message to
        ``engine._conversation`` — the same seam the compact/resume paths use
        (IC-706). The command owns the user-facing note; this only injects."""
        if text:
            self.engine._conversation.append(Message(role="system", content=text))

    # -- workflow progress (IC-904 optional hooks) ----------------------------

    def on_workflow_progress(self, beat: object) -> None:
        """Live /workflow progress: post a compact beat line to the transcript.
        Sync by contract (WorkflowRunner.on_progress is a plain callback)."""
        phase = getattr(beat, "phase_id", "")
        kind = getattr(beat, "kind", "")
        line = f"[workflow] {phase}: {kind}"
        detail = getattr(beat, "detail", "")
        if detail:
            line += f" — {detail}"
        index, total = getattr(beat, "index", None), getattr(beat, "total", None)
        if index and total:
            line += f" ({index}/{total})"
        self._post_note(line)

    def stop_workflow(self) -> bool:
        """/workflow stop hook: cancel the running command worker(s)."""
        try:
            self.workers.cancel_group(self, "command")
        except Exception:  # no running group / older Textual — report inaction
            return False
        return True

    # -- loop driver (IC-804) -------------------------------------------------

    def register_loop(self, spec: LoopSpec) -> None:
        """/loop hook: actually START driving a registered loop.

        ``loopcmd`` parses the interval and stores one :class:`LoopSpec` per
        workspace, then hands it here (via ``ctx.extra['app']``) so the RECURRING
        EXECUTION happens app-side — the handler is synchronous and must not
        block. The driver runs as a background worker in its own ``loop`` group
        so ``/loop stop`` (and app shutdown) can cancel it. Registering a new
        loop replaces the previous one: ``loopcmd`` keeps a single loop per
        workspace, and ``_loop_spec`` identity retires the old driver.
        """
        self._loop_spec = spec
        self.workers.cancel_group(self, "loop")  # retire any prior driver
        self._loop_worker = self.run_worker(self._run_loop(spec), group="loop")

    def stop_loop(self) -> None:
        """/loop stop hook: stop driving the loop.

        ``loopcmd`` has already popped the spec from its own map; this cancels
        the live driver so no further ticks fire. Idempotent — clearing an
        absent loop and cancelling an empty group are both no-ops.
        """
        self._loop_spec = None
        try:
            self.workers.cancel_group(self, "loop")
        except Exception:  # no running group / older Textual — nothing to cancel
            pass

    async def _run_loop(self, spec: LoopSpec) -> None:
        """The recurring executor: re-submit ``spec.prompt`` until retired.

        Fixed-interval loops wait the interval, then wait for the engine to be
        idle before firing — a tick NEVER runs while a turn (a human submit or a
        previous tick) is in flight. Self-paced loops carry no interval and
        re-submit as soon as the prior tick completes (a small poll gap keeps a
        run of instant turns from pegging the event loop). Cancellation (stop /
        replacement / shutdown) unwinds this cleanly via the worker group.
        """
        interval = spec.interval_s
        while self._loop_spec is spec:
            await asyncio.sleep(interval if interval is not None else LOOP_POLL_S)
            # hold the tick until the engine is free, re-checking liveness so a
            # stop during a long-running turn still retires the loop promptly.
            while self._turn_running() and self._loop_spec is spec:
                await asyncio.sleep(LOOP_POLL_S)
            if self._loop_spec is not spec:
                return
            await self._loop_tick(spec.prompt)

    async def _loop_tick(self, prompt: str) -> None:
        """Submit one loop iteration as a real turn and wait for it to finish.

        Reuses the ordinary turn machinery (``_start_turn``), so a tick is gated,
        rendered, and session-recorded exactly like a keyboard submit — the loop
        adds scheduling, not a second code path. Waiting for completion is what
        keeps ticks from overlapping.
        """
        await self.transcript.add_note(f"[loop] {prompt}")
        self._start_turn(prompt)
        while self._turn_running():
            await asyncio.sleep(LOOP_POLL_S)

    # -- modes (IC-703) -------------------------------------------------------

    def action_cycle_mode(self) -> None:
        self._set_mode(next_mode(self.engine.mode), announce=True)

    def _set_mode(self, mode: Mode, *, announce: bool) -> None:
        self.engine.mode = mode  # the engine reads self.mode at gate time
        self.status_bar.set_mode(mode)
        if announce:
            # Announced with the mode's own colour, so the transcript line and
            # the status chip agree at a glance about the new posture.
            self.call_later(self.transcript.add_mode_note, mode.value, DESCRIPTIONS[mode])

    # -- turn driving ---------------------------------------------------------

    def _turn_running(self) -> bool:
        w = self._turn_worker
        return w is not None and w.state in (WorkerState.PENDING, WorkerState.RUNNING)

    def _start_turn(self, text: str) -> None:
        self._record_user(text)
        self._turn_worker = self.run_worker(
            self._drive_turn(text), group="turn", exclusive=True
        )

    async def _drive_turn(self, text: str) -> None:
        await self.transcript.add_user(text)
        self.status_bar.set_running(True)
        self._turn_assistant = []
        try:
            async for event in self.engine.run_turn(text):
                if isinstance(event, TextDelta):
                    self._turn_assistant.append(event.text)
                await self._handle_event(event)
        except Exception as exc:  # engine/provider defect must not crash the app
            await self.transcript.add_note(f"[error] {exc}")
        finally:
            # CancelledError (Esc) skips the except, runs this, then re-raises —
            # partial output already rendered stays on screen (SPEC §3.1), and
            # the partial assistant text is still recorded to the session.
            self.status_bar.set_running(False)
            self._record_assistant("".join(self._turn_assistant))

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
        elif isinstance(event, ResampleProgress):
            await t.add_note(f"[resampling {event.attempt}/{event.total}]")
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

    # -- session recording + resume (IC-706, SPEC §11.2) ----------------------

    def _record_user(self, text: str) -> None:
        """Append a user turn to the session transcript (a no-op without a store).

        The session file is created lazily here — on the FIRST user turn — so its
        header's ``first_prompt`` label is meaningful in the picker. A resumed
        session already has a header, so creation is skipped and the line is
        appended to the same file. All writes are best-effort: a full disk must
        never crash a turn (mirrors ``state.save``).
        """
        store = self.session_store
        if store is None:
            return
        if self._session_id is None:
            self._session_id = _new_session_id()
        try:
            if not self._session_created:
                self._session_created = True
                store.create(self._session_id, datetime.now().isoformat(), first_prompt=text)
            store.append_user(self._session_id, text)
        except (OSError, ValueError):
            pass

    def _record_assistant(self, text: str) -> None:
        """Append the turn's finalized assistant text (a no-op if empty/no store)."""
        store = self.session_store
        if store is None or self._session_id is None or not self._session_created or not text:
            return
        try:
            store.append_assistant(self._session_id, text)
        except OSError:
            pass

    def _on_session_picked(self, session_id: str | None) -> None:
        """Picker dismiss callback: rehydrate the chosen id, or start fresh."""
        if session_id is None:
            return  # cancelled / empty store — a fresh session records as normal
        self.call_later(self._resume_session, session_id)

    async def _resume_session(self, session_id: str) -> None:
        """Rehydrate a stored session: seed the transcript + continue writing it.

        Restores the visible conversation and the tail summary, and threads the
        prior messages into the engine's conversation so the next turn has real
        context (``engine._conversation`` — the seam IC-706 owns per its docstring).
        Recording then CONTINUES into the same session file.
        """
        store = self.session_store
        if store is None:
            return
        # A bad/typo'd/stale --resume id must not fabricate a headerless orphan
        # session (invisible to the picker, un-resumable). Start fresh instead.
        if not store.path_for(session_id).exists():
            await self.transcript.add_note(
                f"[no such session {session_id} — starting a fresh session]"
            )
            self._session_id = None
            self._session_created = False
            return
        messages, tail = store.rehydrate(session_id)
        self._session_id = session_id
        self._session_created = True
        if messages:
            self.engine._conversation = list(messages)
        for message in messages:
            if message.role == "user":
                await self.transcript.add_user(message.content)
            elif message.role == "assistant":
                await self.transcript.append_assistant(message.content)
                self.transcript.end_assistant()
        await self.transcript.add_note(f"[resumed session {session_id}] {tail}")

    # -- notes ----------------------------------------------------------------

    def _post_note(self, text: str | Text) -> None:
        """Add a transcript note from a non-worker context, ordered via the
        message pump (call_later awaits the returned coroutine).

        Accepts a pre-styled ``Text`` so a command result that carries its own
        verdict colouring (CONTRACTS.md §6) reaches the pane intact.
        """
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
        *,
        resume: str | None = None,
    ) -> IronCoreApp:
        """Build the real engine + registries from ``Settings`` (the ``ironcore``
        launch path). Tests bypass this and inject their own engine.

        ``resume`` threads the ``--resume`` flag: ``RESUME_PICK`` opens the
        session picker at launch, a concrete id resumes that session. Production
        always gets a real ``SessionStore`` so live turns are recorded."""
        ws = Path(workspace) if workspace is not None else Path.cwd()
        # config_notes: T8 autonomy clamps and skipped MCP servers. A clamp the
        # user cannot see is a silent downgrade, so these ride the boot notes.
        config_notes: list[str] = []
        if settings is None:
            settings, config_notes = Settings.load_with_notes(project_dir=ws)
        # Entry-point plugins (MS-5): discovered ONCE, before ANY registry is
        # built, so plugin providers/tools/commands/probes feed every
        # construction below. load_plugins never raises — broken plugins are
        # skipped and surfaced as boot notes (and by `ironcore doctor`).
        plugins = load_plugins(settings, ws)
        provider_registry = ProviderRegistry.from_settings(
            settings,
            provider_factory=select_provider_factory(
                settings, plugin_factories=plugins.provider_factories
            ),
        )
        tools = build_tool_registry(settings, ws, plugins=plugins)
        model = settings.provider.model
        # resolved ONCE (MS-2): the boot profile load, /model swap lookups, and
        # background deepen writes all share this one directory.
        envelope_dir = default_envelope_dir()
        # A corrupt cache reads as unprobed (it is quarantined, never fatal) and
        # says so: boot must never traceback on state IronCore itself wrote.
        cached, envelope_note = CapabilityProfile.load_with_note(envelope_dir, model)
        profile = cached or CapabilityProfile(model_id=model)
        # Self-improvement loop (MS-8): the model's outcome ledger lives in the
        # SAME envelope dir; live-session evidence may conservatively LOWER
        # ladder scores (downgrade-only — /probe re-measures). auto_tune=false
        # wires neither the tuning nor the in-engine recording.
        outcomes: OutcomeLedger | None = None
        boot_notes: list[str] = list(config_notes)
        if envelope_note is not None:
            boot_notes.append(envelope_note)
        if settings.envelope.auto_tune:
            outcomes = OutcomeLedger.load(envelope_dir, model)
            tuning = apply_tuning(profile, outcomes)
            if tuning.adjustments:
                profile = tuning.profile
                boot_notes.append(
                    "Envelope tuned from live-session evidence (run /probe to re-measure):"
                )
                boot_notes.extend(f"  - {note}" for note in tuning.adjustments)
            boot_notes.extend(f"[envelope] {hint}" for hint in tuning.reprobe_hints)
        # MCP tool servers (MS-7): built only when servers are configured AND
        # NET tools are enabled — an off NET tool is never registered, so with
        # network_tools false the servers are not even spawned; one boot note
        # says why. Connection happens in an on_mount background worker.
        mcp_manager: MCPManager | None = None
        if settings.mcp.servers:
            if settings.safety.network_tools:
                mcp_manager = MCPManager.from_settings(settings)
            else:
                boot_notes.append(
                    f"[mcp] {len(settings.mcp.servers)} server(s) configured but MCP tools "
                    "are NET-risk and stay unregistered until [safety] network_tools = true."
                )
        try:
            mode = Mode(settings.safety.mode)
        except ValueError:
            mode = Mode.MANUAL
        # per-role routing (MS-3): routed roles get their own provider from the
        # registry's per-model cache plus their own envelope from the SAME
        # envelope_dir the boot load / /model swaps / background deepens use.
        roles = RoleRouter(settings, registry=provider_registry, envelope_dir=envelope_dir)
        engine = TurnEngine(
            provider_registry.default,
            tools,
            settings,
            profile,
            mode,
            workspace=ws,
            roles=roles,
            outcomes=outcomes,
        )
        # mold to the model on first use (instant-on-profiling): for an unprobed
        # model, seed instantly from endpoint introspection then deep-probe to
        # refine — each disabled independently by config. ``instant_seed`` may not
        # exist yet (Wave-2B owns settings.py); default it on via getattr.
        unprobed = profile.probed_at is None
        instant_seed = getattr(settings.envelope, "instant_seed", True) and unprobed
        auto_probe = settings.envelope.auto_probe and unprobed
        # command registry built LAST among the registries so its duplicate
        # skips (like the tool registry's) land in plugins.skipped before the
        # boot notes are frozen below.
        command_registry = build_command_registry(plugins=plugins)
        if plugins.skipped:
            boot_notes.extend(
                f"[plugins] skipped {s.group}:{s.name} - {s.reason}" for s in plugins.skipped
            )
        return cls(
            engine,
            command_registry,
            settings,
            provider_registry=provider_registry,
            session_store=SessionStore(ws),
            resume_id=resume,
            auto_probe=auto_probe,
            instant_seed=instant_seed,
            envelope_dir=envelope_dir,
            boot_notes=tuple(boot_notes),
            mcp_manager=mcp_manager,
            plugin_probes=tuple(plugins.probes),
        )


def run_app(
    settings: Settings | None = None,
    workspace: str | Path | None = None,
    *,
    resume: str | None = None,
) -> int:
    """Launch the TUI (the ``ironcore`` no-subcommand entry point)."""
    app = IronCoreApp.from_settings(settings=settings, workspace=workspace, resume=resume)
    app.run()
    return 0
