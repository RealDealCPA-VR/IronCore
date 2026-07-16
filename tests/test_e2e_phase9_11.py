"""End-to-end proof of outcome for phases 9 (workflows), 10 (memory), 11 (dist).

Drives the SHIPPED artifacts against real subsystems: an actual built-in
workflow YAML executed by the real WorkflowRunner over real (scripted) engines;
the /workflow command surface through the real registry; the handoff lifecycle
proving a compaction handoff lands AND never leaks a secret; and project-memory
injection reaching the composed context. No network, no model beyond MockProvider.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import ironcore.workflows
from ironcore.commands import build_default_registry as build_cmds
from ironcore.commands.base import CommandContext
from ironcore.config.settings import Settings
from ironcore.core.composer import compose, load_project_memory
from ironcore.core.engine import TurnEngine
from ironcore.core.state import SessionState
from ironcore.envelope.profile import CapabilityProfile
from ironcore.memory.handoff import latest_handoff
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.workflows.engine import WorkflowRunner
from ironcore.workflows.schema import load_workflow_file

BUILTIN_DIR = Path(ironcore.workflows.__file__).parent / "builtin"

# review.yaml's fanout agent output_schema requires a "findings" array.
FINDINGS_JSON = '{"dimension": "bugs", "findings": ["off-by-one at line 10"], "summary": "n"}'


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _done(content: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=content))


def _factory(tmp_path, reply: str):
    settings = Settings()
    tools = build_tools(settings, tmp_path)

    def make() -> TurnEngine:
        # a FRESH engine + provider per subagent (workflows demand fresh contexts)
        return TurnEngine(
            MockProvider([_done(reply)]), tools, settings, _profile(), Mode.AUTO,
            workspace=tmp_path, snapshots=None, handoff_path=None,
        )

    return make


# --------------------------------------------------------------------------- #
# 1. A SHIPPED built-in workflow runs end to end
# --------------------------------------------------------------------------- #


def test_builtin_review_workflow_runs_from_the_packaged_yaml(tmp_path):
    workflow = load_workflow_file(BUILTIN_DIR / "review.yaml")
    assert workflow.name == "review"

    runner = WorkflowRunner(engine_factory=_factory(tmp_path, FINDINGS_JSON), concurrency=2)
    result = asyncio.run(runner.run(workflow, {"diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n"}))

    assert result.ok, result.notes
    assert not result.notes  # every dimension's subagent produced conforming output
    # the fanout ran across all three review dimensions
    find_phase = next(p for p in workflow.phases if p.fanout is not None)
    assert len(result.outputs[find_phase.id]) == 3
    # the reduce produced a markdown table over the findings
    report = result.final
    assert isinstance(report, str) and "|" in report  # a GFM table


def test_all_three_builtins_load_and_are_named_by_stem():
    for stem in ("review", "migrate", "explain-repo"):
        wf = load_workflow_file(BUILTIN_DIR / f"{stem}.yaml")
        assert wf.name == stem
        assert wf.phases  # non-empty


def test_workflow_failure_isolation_one_bad_item_does_not_abort(tmp_path):
    workflow = load_workflow_file(BUILTIN_DIR / "review.yaml")
    # a factory whose engine emits NON-conforming output -> subagent ok=False,
    # isolated to a note; the workflow still completes (does not raise / abort)
    runner = WorkflowRunner(engine_factory=_factory(tmp_path, "no json here at all"), concurrency=3)
    result = asyncio.run(runner.run(workflow, {"diff": "x"}))
    assert result.ok is True  # structural success despite per-item failures
    assert result.notes  # the bad items were recorded, not swallowed silently


# --------------------------------------------------------------------------- #
# 2. The /workflow command surface (list + first-run confirmation, SAFETY T8)
# --------------------------------------------------------------------------- #


def test_workflow_command_lists_builtins_and_gates_first_run(tmp_path):
    registry = build_cmds()
    ctx = CommandContext(settings=Settings(), extra={"workspace": tmp_path, "registry": registry})

    listing = registry.dispatch("/workflow", ctx)
    assert "review" in listing and "migrate" in listing and "explain-repo" in listing

    # first invocation must NOT auto-run — it returns a confirmation summary
    summary = registry.dispatch("/workflow review", ctx)
    assert "run" in summary.lower()  # instructs the user to confirm with /workflow run


# --------------------------------------------------------------------------- #
# 3. Handoff lifecycle: a compaction handoff lands and never leaks a secret
# --------------------------------------------------------------------------- #


def test_compaction_handoff_is_written_without_leaking_a_secret(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    hp = tmp_path / "HANDOFF.md"
    settings = Settings()
    engine = TurnEngine(
        MockProvider([_done("Context: worked on X\nGotchas: none"), _done("all done")]),
        build_tools(settings, tmp_path), settings, _profile(), Mode.AUTO,
        workspace=tmp_path, snapshots=None, handoff_path=hp,
    )
    # seed a large history carrying a secret, forcing compaction next turn
    engine._conversation = [Message(role="tool", content=f'API_KEY="{secret}" ' + "x" * 9000)]

    async def go():
        async for _ in engine.run_turn("continue"):
            pass

    asyncio.run(go())

    assert hp.exists()
    block = latest_handoff(hp)
    assert block is not None  # a parseable handoff was appended on compaction
    assert secret not in hp.read_text(encoding="utf-8")  # redacted before it got here


def test_end_session_writes_a_final_handoff(tmp_path):
    hp = tmp_path / "HANDOFF.md"
    settings = Settings()
    engine = TurnEngine(
        MockProvider([]), build_tools(settings, tmp_path), settings, _profile(), Mode.AUTO,
        workspace=tmp_path, snapshots=None, handoff_path=hp,
    )
    engine.state.goal = "ship v0.1"
    block = engine.end_session()
    assert block is not None
    assert hp.exists() and latest_handoff(hp) is not None


# --------------------------------------------------------------------------- #
# 4. Project memory (IRONCORE.md) reaches the composed context, budgeted
# --------------------------------------------------------------------------- #


def test_project_memory_is_loaded_and_injected_into_context(tmp_path):
    (tmp_path / "IRONCORE.md").write_text(
        "# demo\n## Build\n- uv run pytest\nMEMORY_MARKER_XYZ is here\n"
    )
    profile = _profile()
    memory = load_project_memory(tmp_path, profile=profile)
    assert "MEMORY_MARKER_XYZ" in memory

    state = SessionState(mode=Mode.AUTO)
    messages = compose(
        state, profile=profile, settings=Settings(),
        system_prompt="SYS", working_set={}, history=[], user_input="hi", memory=memory,
    )
    blob = "".join(m.content for m in messages)
    assert "MEMORY_MARKER_XYZ" in blob  # the memory reached the model's context


def test_oversize_memory_stays_within_the_context_budget(tmp_path):
    (tmp_path / "IRONCORE.md").write_text("Z" * 200_000)  # far larger than any budget
    profile = _profile()
    memory = load_project_memory(tmp_path, profile=profile)
    state = SessionState(mode=Mode.AUTO)
    messages = compose(
        state, profile=profile, settings=Settings(),
        system_prompt="SYS", working_set={}, history=[], user_input="hi", memory=memory,
    )
    # the budget invariant holds even with a huge IRONCORE.md
    total_chars = sum(len(m.content) for m in messages)
    assert total_chars <= profile.honest_context * 4  # ~4 chars/token headroom bound


def test_missing_ironcore_md_is_a_silent_empty(tmp_path):
    assert load_project_memory(tmp_path, profile=_profile()) == ""
