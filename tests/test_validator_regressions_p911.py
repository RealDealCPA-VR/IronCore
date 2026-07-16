"""Regression pins for the phase-9/10/11 adversarial validation round (2026-07-16)."""

from __future__ import annotations

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.composer import load_project_memory
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.workflows.engine import WorkflowRunner
from ironcore.workflows.schema import load_workflow


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


# --- BLOCKER 1: a PLAN session must not mutate through a workflow (SAFETY T8)

# a one-phase workflow whose single subagent is told to write a file
_WRITER_YAML = """
name: writer
phases:
  - id: act
    fanout:
      items: [go]
      agent:
        role: actor
        prompt: "write the file now ({{item}})"
"""


def test_finding1_plan_session_workflow_cannot_write(tmp_path):
    settings = Settings()
    tools = build_tools(settings, tmp_path)
    write = ToolCall(id="c1", name="write_file", arguments={"path": "pwned.txt", "content": "x"})

    def factory() -> TurnEngine:
        # the "model" tries to write; the subagent's gate must stop it in PLAN
        return TurnEngine(
            MockProvider([CompletionResult(message=Message(role="assistant", tool_calls=[write])),
                          CompletionResult(message=Message(role="assistant", content="blocked"))]),
            tools, settings, _profile(), Mode.AUTO, workspace=tmp_path, snapshots=None,
        )

    workflow = load_workflow(_WRITER_YAML)
    # session mode PLAN must be threaded into the subagent -> WRITE denied
    runner = WorkflowRunner(engine_factory=factory, mode=Mode.PLAN)
    asyncio.run(runner.run(workflow, {}))
    assert not (tmp_path / "pwned.txt").exists()  # PLAN denied the write

    # and AUTO (the explicit power mode) DOES let it through, proving the thread works
    runner_auto = WorkflowRunner(engine_factory=factory, mode=Mode.AUTO)
    asyncio.run(runner_auto.run(workflow, {}))
    assert (tmp_path / "pwned.txt").exists()


# --- MAJOR 2: a binary / non-UTF-8 IRONCORE.md must not crash a turn


def test_finding2_binary_ironcore_md_does_not_crash(tmp_path):
    (tmp_path / "IRONCORE.md").write_bytes(b"\xff\xfe\x00binary junk\x80\x81")
    # load is best-effort: returns a (replaced) string, never raises
    memory = load_project_memory(tmp_path, profile=_profile())
    assert isinstance(memory, str)

    # and a full turn runs cleanly with that file present
    settings = Settings()
    engine = TurnEngine(
        MockProvider([CompletionResult(message=Message(role="assistant", content="ok"))]),
        build_tools(settings, tmp_path), settings, _profile(), Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )

    async def go():
        async for _ in engine.run_turn("hi"):
            pass

    asyncio.run(go())  # must not raise UnicodeDecodeError


# --- MAJOR 3: an ASK-gated subagent action fails fast, not a 300s stall
# (covered end-to-end via the /workflow command's unattended broker; here we
#  just pin that the runner threads a non-AUTO mode without hanging)


def test_finding3_manual_mode_workflow_does_not_hang(tmp_path):
    settings = Settings()
    tools = build_tools(settings, tmp_path)
    from ironcore.commands.workflowcmd import _unattended_broker

    write = ToolCall(id="c1", name="write_file", arguments={"path": "f.txt", "content": "x"})

    def factory() -> TurnEngine:
        return TurnEngine(
            MockProvider([CompletionResult(message=Message(role="assistant", tool_calls=[write])),
                          CompletionResult(message=Message(role="assistant", content="done"))]),
            tools, settings, _profile(), Mode.MANUAL, workspace=tmp_path,
            snapshots=None, approvals=_unattended_broker(),
        )

    workflow = load_workflow(_WRITER_YAML)
    runner = WorkflowRunner(engine_factory=factory, mode=Mode.MANUAL)
    # MANUAL asks for the write; the unattended broker denies fast -> completes
    # quickly (would be a 300s stall on the default broker). Bounded by asyncio.
    result = asyncio.run(asyncio.wait_for(runner.run(workflow, {}), timeout=30))
    assert result.ok is True
    assert not (tmp_path / "f.txt").exists()  # the ASK was auto-denied, not applied
