"""/workflow (IC-904): discovery/listing, first-run confirmation, and scheduled run.

The listing + confirmation paths are pure and asserted directly. The run path uses
a synchronous ``schedule`` (``asyncio.run`` on the coroutine, mirroring
tests/test_cmd_review.py) driving the REAL ``WorkflowRunner`` over a fixture
workflow with a scripted ``MockProvider`` engine_factory (the test seam) — zero
network, zero model. Progress beats are captured through a fake app's
``on_workflow_progress`` hook.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ironcore.commands.base import CommandContext
from ironcore.commands.workflowcmd import _cmd_workflow, _parse_inputs, run_workflow
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry
from ironcore.workflows.engine import WorkflowProgress
from ironcore.workflows.schema import load_workflow

# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #

_DEMO_YAML = """\
name: demo
description: A demo workflow for tests.
inputs: [topic]
phases:
  - id: scan
    fanout:
      items: [a, b]
      agent:
        role: scanner
        prompt: "scan {{item}} about {{topic}}"
  - id: report
    reduce: count
"""


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=content))


def _engine_over(tmp_path, provider) -> TurnEngine:
    settings = Settings.model_validate({"safety": {"network_tools": False}})
    return TurnEngine(
        provider,
        build_default_registry(settings, tmp_path),
        settings,
        _profile(),
        Mode.MANUAL,
        workspace=tmp_path,
        snapshots=None,
    )


def _script_factory(tmp_path, scripts: list[list]):
    """Hand each successive subagent a fresh MockProvider-backed engine (script N)."""
    it = iter(scripts)

    def make() -> TurnEngine:
        return _engine_over(tmp_path, MockProvider(list(next(it))))

    return make


def _workflows_dir(tmp_path: Path) -> Path:
    wf_dir = tmp_path / ".ironcore" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    return wf_dir


def _write_demo(tmp_path: Path) -> None:
    (_workflows_dir(tmp_path) / "demo.yaml").write_text(_DEMO_YAML, encoding="utf-8")


class _FakeApp:
    """Captures progress beats and stop requests via the hasattr-guarded hooks."""

    def __init__(self) -> None:
        self.beats: list[WorkflowProgress] = []
        self.stopped = 0

    def on_workflow_progress(self, beat: WorkflowProgress) -> None:
        self.beats.append(beat)

    def stop_workflow(self) -> None:
        self.stopped += 1


def _sync_schedule():
    captured: list[str] = []

    def schedule(coro):
        captured.append(asyncio.run(coro))

    return schedule, captured


def _ctx(tmp_path, *, factory=None, schedule=None, app=None) -> CommandContext:
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = str(tmp_path)
    if factory is not None:
        ctx.extra["engine_factory"] = factory
    if schedule is not None:
        ctx.extra["schedule"] = schedule
    if app is not None:
        ctx.extra["app"] = app
    return ctx


# --------------------------------------------------------------------------- #
# (1) listing
# --------------------------------------------------------------------------- #


def test_list_includes_fixture_and_notes_malformed(tmp_path):
    _write_demo(tmp_path)
    (_workflows_dir(tmp_path) / "broken.yaml").write_text("just a string\n", encoding="utf-8")

    out = _cmd_workflow(_ctx(tmp_path), "")
    assert "Available workflows:" in out
    assert "demo — A demo workflow for tests." in out
    # the malformed file is still listed, with an error note, and nothing crashed
    assert "broken —" in out
    assert "[error]" in out


def test_list_empty_is_graceful(tmp_path):
    # no workspace dir + (likely) no built-ins yet → a helpful message, no crash
    out = _cmd_workflow(_ctx(tmp_path), "")
    assert "No workflows found" in out or "Available workflows" in out


# --------------------------------------------------------------------------- #
# (2) first-run confirmation (SAFETY T8): summary, does NOT run
# --------------------------------------------------------------------------- #


def test_bare_name_returns_confirmation_and_does_not_run(tmp_path):
    _write_demo(tmp_path)
    schedule, captured = _sync_schedule()
    factory = _script_factory(tmp_path, [])  # must not be consulted
    ctx = _ctx(tmp_path, factory=factory, schedule=schedule)

    out = _cmd_workflow(ctx, "demo topic=x")
    assert "Workflow 'demo'" in out
    assert "2 phases" in out
    assert "scan [fanout]" in out and "report [reduce]" in out
    assert "confirm" in out.lower()
    assert "/workflow run demo topic=x" in out
    assert captured == []  # NOT executed


def test_unknown_name_is_graceful(tmp_path):
    _write_demo(tmp_path)
    out = _cmd_workflow(_ctx(tmp_path), "nope")
    assert "No workflow named 'nope'" in out


# --------------------------------------------------------------------------- #
# (3) confirmed run: executes, captures render_summary, delivers progress
# --------------------------------------------------------------------------- #


def test_run_executes_and_delivers_progress(tmp_path):
    _write_demo(tmp_path)
    schedule, captured = _sync_schedule()
    app = _FakeApp()
    factory = _script_factory(tmp_path, [[_text("found-a")], [_text("found-b")]])
    ctx = _ctx(tmp_path, factory=factory, schedule=schedule, app=app)

    ack = _cmd_workflow(ctx, "run demo topic=bugs")
    assert "Running workflow 'demo'" in ack
    assert "topic=bugs" in ack

    # the scheduled coroutine ran and produced a render_summary with the phases
    assert captured, "the run coroutine was not scheduled"
    summary = captured[0]
    assert "workflow ok" in summary
    assert "scan:" in summary and "report:" in summary
    assert summary.rstrip().endswith("2")  # reduce=count over the 2 scan results

    # progress beats reached the app hook: a phase_start and a workflow_done at least
    kinds = {beat.kind for beat in app.beats}
    assert "phase_start" in kinds
    assert "workflow_done" in kinds


def test_confirmed_name_runs_without_reconfirmation(tmp_path):
    _write_demo(tmp_path)
    schedule, captured = _sync_schedule()
    factory = _script_factory(
        tmp_path,
        [[_text("r1")], [_text("r2")], [_text("r3")], [_text("r4")]],  # two runs, 2 items each
    )
    ctx = _ctx(tmp_path, factory=factory, schedule=schedule)

    # explicit run confirms + executes
    _cmd_workflow(ctx, "run demo")
    assert len(captured) == 1
    # a subsequent bare invocation now runs directly (already confirmed this workspace)
    out = _cmd_workflow(ctx, "demo")
    assert "Running workflow 'demo'" in out
    assert len(captured) == 2


# --------------------------------------------------------------------------- #
# (4) graceful degradation: missing engine/factory, missing scheduler, empty ctx
# --------------------------------------------------------------------------- #


def test_run_without_engine_or_factory_is_graceful(tmp_path):
    _write_demo(tmp_path)
    schedule, _ = _sync_schedule()
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = str(tmp_path)
    ctx.extra["schedule"] = schedule
    out = _cmd_workflow(ctx, "run demo")
    assert "needs a live session" in out


def test_run_without_scheduler_is_graceful(tmp_path):
    _write_demo(tmp_path)
    factory = _script_factory(tmp_path, [])
    ctx = _ctx(tmp_path, factory=factory)  # no schedule
    out = _cmd_workflow(ctx, "run demo")
    assert "scheduler" in out.lower()


def test_empty_ctx_never_crashes():
    ctx = CommandContext(settings=Settings())  # totally empty extra
    # listing, a bare name, run, and stop must all return strings, never raise
    assert isinstance(_cmd_workflow(ctx, ""), str)
    assert isinstance(_cmd_workflow(ctx, "whatever"), str)
    assert isinstance(_cmd_workflow(ctx, "run whatever"), str)
    assert isinstance(_cmd_workflow(ctx, "stop"), str)


# --------------------------------------------------------------------------- #
# (5) stop + arg parsing + the run coroutine in isolation
# --------------------------------------------------------------------------- #


def test_stop_uses_app_hook_when_present(tmp_path):
    app = _FakeApp()
    ctx = _ctx(tmp_path, app=app)
    out = _cmd_workflow(ctx, "stop")
    assert "cancellation requested" in out.lower()
    assert app.stopped == 1


def test_stop_without_app_is_graceful(tmp_path):
    out = _cmd_workflow(_ctx(tmp_path), "stop")
    assert "no running workflow" in out.lower()


def test_parse_inputs_folds_key_values():
    assert _parse_inputs("a=1 b=two   c=") == {"a": "1", "b": "two", "c": ""}
    assert _parse_inputs("bare tokens ignored k=v") == {"k": "v"}
    assert _parse_inputs("") == {}


def test_run_workflow_coroutine_returns_summary_and_emits_beats(tmp_path):
    _write_demo(tmp_path)
    workflow = load_workflow(tmp_path / ".ironcore" / "workflows" / "demo.yaml")
    factory = _script_factory(tmp_path, [[_text("x")], [_text("y")]])
    beats: list[WorkflowProgress] = []

    out = asyncio.run(run_workflow(workflow, {"topic": "t"}, factory, beats.append))
    assert "Workflow 'demo'" in out and "workflow ok" in out
    # beats were delivered to on_progress in order, ending with workflow_done
    assert beats[0].kind == "phase_start"
    assert beats[-1].kind == "workflow_done"
