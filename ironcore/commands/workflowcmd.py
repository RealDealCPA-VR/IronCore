"""/workflow (IC-904): list, confirm, and run deterministic multi-agent workflows.

    /workflow                      — LIST available workflows + one-line descriptions
    /workflow <name> [k=v ...]     — first run in this workspace: show a confirmation
                                     summary (does NOT execute); afterwards: run it
    /workflow run <name> [k=v ...] — CONFIRM + execute now (schedules the run)
    /workflow stop                 — request cancellation of a running workflow

Workflows are YAML files discovered under ``<workspace>/.ironcore/workflows/`` and
the shipped built-ins in ``ironcore/workflows/builtin/`` (IC-905). A workspace file
shadows a built-in of the same name.

FIRST-RUN CONFIRMATION (SAFETY T8). The first time a workflow name is invoked in a
workspace we never auto-run: we return a summary (name / description / phase count /
what it does) and ask the user to confirm with ``/workflow run <name>``. Confirmed
names are held in a module-level set keyed by workspace (the TUI rebuilds a fresh
``CommandContext`` per dispatch, so per-``ctx`` state cannot survive) — after the
first confirmed run, a bare ``/workflow <name>`` runs directly.

The handler is SYNC and never blocks: the actual run is a coroutine handed to
``ctx.extra["schedule"]`` (the phase-8 contract, docs/ARCHITECTURE.md §6), which
posts progress and the final ``render_summary`` to the transcript. Arguments are
simple ``key=value`` pairs folded into the workflow's ``inputs`` dict.

Engine factory. Subagents each need a FRESH ``TurnEngine`` (SPEC §10). We prefer an
injected ``ctx.extra["engine_factory"]`` (the test seam), else mint fresh engines
from the live ``ctx.extra["engine"]`` (reusing its provider/tools/settings/profile/
workspace). No engine and no factory → a clear "needs a live session" message.

Every ``ctx.extra`` key is optional (headless / tests); each accessor degrades to a
readable message rather than raising.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ironcore import workflows as _workflows_pkg
from ironcore.commands._helpers import resolve_workspace
from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.workflows.engine import WorkflowProgress, WorkflowRunner
from ironcore.workflows.schema import (
    Phase,
    Workflow,
    WorkflowError,
    discover_workflows,
    load_workflow_file,
)

if TYPE_CHECKING:  # typing-only; TurnEngine is imported lazily where it is built
    from ironcore.core.engine import TurnEngine

#: Built-in workflows dir (``ironcore/workflows/builtin/``). ``discover_workflows``
#: tolerates it being absent/empty, so this is safe before IC-905 populates it.
_BUILTIN_WORKFLOWS_DIR = Path(_workflows_pkg.__file__).resolve().parent / "builtin"

#: workspace-key -> names confirmed (run at least once) in that workspace. Durable
#: across the ephemeral ``CommandContext`` the TUI builds per dispatch.
_CONFIRMED: dict[str, set[str]] = {}

#: Reserved first tokens that are sub-commands, not workflow names.
_RESERVED = ("run", "stop")


def _key(ctx: CommandContext) -> str:
    ws = resolve_workspace(ctx)
    return str(ws) if ws is not None else "<no-workspace>"


def _phase_kind(phase: Phase) -> str:
    """The single kind a phase declares (schema guarantees exactly one)."""
    if phase.fanout is not None:
        return "fanout"
    if phase.foreach is not None:
        return "foreach"
    return "reduce"


def _discover(ctx: CommandContext) -> dict[str, Path]:
    """All discoverable workflows as ``name -> path`` (workspace shadows built-in)."""
    builtin = discover_workflows(_BUILTIN_WORKFLOWS_DIR)
    ws = resolve_workspace(ctx)
    if ws is not None:
        workspace = discover_workflows(ws / ".ironcore" / "workflows")
    else:
        workspace = {}
    return {**builtin, **workspace}


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def _cmd_workflow(ctx: CommandContext, args: str) -> str:
    args = args.strip()
    if not args:
        return _list(ctx)
    first, _, rest = args.partition(" ")
    lowered = first.lower()
    if lowered == "stop":
        return _stop(ctx)
    if lowered == "run":
        name, _, run_args = rest.strip().partition(" ")
        if not name:
            return "Usage: /workflow run <name> [key=value ...]"
        return _run(ctx, name, run_args, confirm=True)
    return _run(ctx, first, rest, confirm=False)


def _list(ctx: CommandContext) -> str:
    """List every discoverable workflow with a one-line description.

    A malformed file is still listed (its stem was discovered) with an error note
    instead of crashing the whole listing.
    """
    found = _discover(ctx)
    if not found:
        return (
            "No workflows found. Add YAML files under .ironcore/workflows/ in your "
            "workspace (schema: docs/CONTRACTS.md §9), or use a shipped built-in."
        )
    lines = ["Available workflows:"]
    for name in sorted(found):
        try:
            workflow = load_workflow_file(found[name])
        except WorkflowError as exc:
            lines.append(f"  {name} — [error] {exc}")
            continue
        lines.append(f"  {name} — {workflow.description or '(no description)'}")
    lines.append("Run one with /workflow <name> (the first run asks you to confirm).")
    return "\n".join(lines)


def _run(ctx: CommandContext, name: str, arg_text: str, *, confirm: bool) -> str:
    """Confirm-or-execute a single named workflow.

    ``confirm`` is ``True`` on the explicit ``/workflow run`` path (it both records
    the confirmation and executes). A bare ``/workflow <name>`` passes ``False``:
    it shows the confirmation summary the first time and executes thereafter.
    """
    found = _discover(ctx)
    path = found.get(name)
    if path is None:
        if name.lower() in _RESERVED:  # e.g. a bare "/workflow run" with no name
            return f"Usage: /workflow {name.lower()} <name> [key=value ...]"
        return f"No workflow named {name!r}. Run /workflow to list available workflows."
    try:
        workflow = load_workflow_file(path)
    except WorkflowError as exc:
        return f"Workflow {name!r} is invalid: {exc}"

    confirmed = _CONFIRMED.setdefault(_key(ctx), set())
    if not confirm and name not in confirmed:
        return _confirmation_summary(name, workflow, arg_text)

    factory, why = _engine_factory(ctx)
    if factory is None:
        return why
    schedule = ctx.extra.get("schedule")
    if schedule is None:
        return "Running a workflow needs the scheduler (not available here)."

    confirmed.add(name)
    inputs = _parse_inputs(arg_text)
    on_progress = _make_on_progress(ctx)
    schedule(run_workflow(workflow, inputs, factory, on_progress))
    tail = f" with {_fmt_inputs(inputs)}" if inputs else ""
    plural = "phase" if len(workflow.phases) == 1 else "phases"
    return (
        f"Running workflow {name!r}{tail}… ({len(workflow.phases)} {plural}). "
        "Stop with /workflow stop."
    )


def _confirmation_summary(name: str, workflow: Workflow, arg_text: str) -> str:
    """First-run summary (SAFETY T8): what it is + how to confirm. Does NOT run."""
    kinds = ", ".join(f"{phase.id} [{_phase_kind(phase)}]" for phase in workflow.phases)
    plural = "phase" if len(workflow.phases) == 1 else "phases"
    lines = [
        f"Workflow {name!r}: {workflow.description or '(no description)'}",
        f"  {len(workflow.phases)} {plural}: {kinds}",
    ]
    if workflow.inputs:
        lines.append(f"  Declared inputs: {', '.join(workflow.inputs)}")
    lines.append(
        "  It spawns subagents in fresh contexts; in auto mode they may read and "
        "modify files under the workspace."
    )
    run_line = f"/workflow run {name}"
    if arg_text.strip():
        run_line += f" {arg_text.strip()}"
    lines.append(f"First run in this workspace — confirm to execute:  {run_line}")
    return "\n".join(lines)


def _stop(ctx: CommandContext) -> str:
    """Request cancellation of a running workflow via the app hook, if wired."""
    app = ctx.extra.get("app")
    if app is not None and hasattr(app, "stop_workflow"):
        try:
            app.stop_workflow()
        except Exception:  # noqa: BLE001 — an app-hook failure must not crash the command
            pass
        return "Workflow cancellation requested."
    return "No running workflow to cancel here (cancellation needs the live session)."


# --------------------------------------------------------------------------- #
# execution plumbing
# --------------------------------------------------------------------------- #


async def run_workflow(
    workflow: Workflow,
    inputs: dict[str, Any],
    engine_factory: Callable[[], TurnEngine],
    on_progress: Callable[[WorkflowProgress], None] | None,
) -> str:
    """Build a :class:`WorkflowRunner`, run it, and return a report string.

    ``run`` never raises for content/structural errors, so this coroutine always
    returns a human digest (``render_summary``) — the scheduler posts it verbatim.
    """
    runner = WorkflowRunner(engine_factory=engine_factory, on_progress=on_progress)
    result = await runner.run(workflow, inputs)
    return f"Workflow {workflow.name!r}:\n{result.render_summary()}"


def _engine_factory(
    ctx: CommandContext,
) -> tuple[Callable[[], TurnEngine] | None, str]:
    """Return ``(factory, "")`` or ``(None, reason)``.

    Prefers the injected ``engine_factory`` test seam; otherwise mints fresh engines
    off the live ``engine``. A missing engine (and no seam) is the one hard stop.
    """
    seam = ctx.extra.get("engine_factory")
    if seam is not None:
        return seam, ""
    engine = ctx.extra.get("engine")
    if engine is None:
        return None, "Running a workflow needs a live session (no engine available)."
    return _factory_from_engine(engine), ""


def _factory_from_engine(engine: Any) -> Callable[[], TurnEngine]:
    """A factory minting a FRESH ``TurnEngine`` per call off the live engine.

    Subagents (and the retry path) each need a clean context; we reuse the live
    engine's provider/tools/settings/profile/workspace/mode but never its
    conversation. ``snapshots=None`` mirrors the orchestrator's own engines
    (tests/test_workflow_engine.py).
    """
    from ironcore.core.engine import TurnEngine  # lazy: keep module import light

    def make() -> TurnEngine:
        return TurnEngine(
            engine.provider,
            engine.tools,
            engine.settings,
            engine.profile,
            engine.mode,
            workspace=engine.workspace,
            snapshots=None,
        )

    return make


def _make_on_progress(ctx: CommandContext) -> Callable[[WorkflowProgress], None]:
    """A progress sink forwarding each beat to the app's ``on_workflow_progress``.

    The hook is hasattr-guarded (the app need not implement it); when present the
    orchestrator can route beats into a :class:`~ironcore.tui.widgets.workflowview.WorkflowView`.
    Any hook failure is swallowed so a UI defect never aborts the run.
    """
    app = ctx.extra.get("app")

    def on_progress(beat: WorkflowProgress) -> None:
        if app is not None and hasattr(app, "on_workflow_progress"):
            try:
                app.on_workflow_progress(beat)
            except Exception:  # noqa: BLE001 — UI must never break orchestration
                pass

    return on_progress


def _parse_inputs(arg_text: str) -> dict[str, str]:
    """Fold ``key=value`` tokens into an inputs dict; non-``k=v`` tokens are ignored."""
    inputs: dict[str, str] = {}
    for token in arg_text.split():
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip()
            if key:
                inputs[key] = value
    return inputs


def _fmt_inputs(inputs: dict[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in inputs.items())


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "workflow",
        "run a multi-agent workflow",
        "/workflow [<name> [key=value ...]] | run <name> [args] | stop",
        _cmd_workflow,
    ),
)
