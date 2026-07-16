"""Built-in workflows (IC-905): review / migrate / explain-repo.

Every shipped built-in must (1) load via ``load_workflow_file`` with NO
``WorkflowError`` and expose the name/phase-kinds its filename promises, and
(2) RUN to completion through the real ``WorkflowRunner`` over a scripted
``MockProvider`` — no network, no model. The engine_factories mirror IC-903's
``tests/test_workflow_engine.py``: each successive subagent gets a fresh
MockProvider-backed engine whose next script drives it; under MockProvider's
no-suspension text responses the orchestrator runs items atomically in item
order, so script N drives item N.

The migrate case deliberately chains two ``foreach`` phases — one over an INPUT
list (``{{targets}}``) and one over the PRIOR phase's list output
(``{{discover}}``) — proving a ref actually resolves to a list at runtime with
no "unresolved ref" note.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ironcore import workflows
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, Provider
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry
from ironcore.workflows.engine import WorkflowResult, WorkflowRunner
from ironcore.workflows.schema import Workflow, load_workflow_file

BUILTIN_DIR = Path(workflows.__file__).parent / "builtin"


# --------------------------------------------------------------------------- #
# helpers (mirrors tests/test_workflow_engine.py)
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=content))


def _settings() -> Settings:
    return Settings.model_validate({"safety": {"network_tools": False}})


def _engine_over(tmp_path, provider: Provider) -> TurnEngine:
    settings = _settings()
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
    """Hand each successive subagent a fresh engine driven by the next script."""
    it = iter(scripts)

    def make() -> TurnEngine:
        return _engine_over(tmp_path, MockProvider(list(next(it))))

    return make


def _load(stem: str) -> Workflow:
    return load_workflow_file(BUILTIN_DIR / f"{stem}.yaml")


def _run(factory, workflow: Workflow, inputs: dict) -> WorkflowResult:
    runner = WorkflowRunner(engine_factory=factory)
    return asyncio.run(runner.run(workflow, inputs))


def _phase_kind(phase) -> str:
    if phase.fanout is not None:
        return "fanout"
    if phase.foreach is not None:
        return "foreach"
    return "reduce"


# --------------------------------------------------------------------------- #
# (1) every built-in loads clean and has the promised name + phase kinds
# --------------------------------------------------------------------------- #


def test_all_builtins_load_without_error():
    for stem in ("review", "migrate", "explain-repo"):
        workflow = _load(stem)  # raises WorkflowError on any schema/syntax problem
        # name MUST equal the filename stem (discover_workflows / /workflow key by stem)
        assert workflow.name == stem
        assert workflow.phases  # non-empty


def test_review_shape():
    wf = _load("review")
    assert wf.name == "review"
    assert [p.id for p in wf.phases] == ["find", "report"]
    assert [_phase_kind(p) for p in wf.phases] == ["fanout", "reduce"]
    find = wf.phases[0]
    assert find.fanout.items == ["bugs", "security", "performance"]
    assert find.fanout.agent.output_schema["required"] == ["dimension", "findings"]
    assert wf.phases[1].reduce == "markdown_table"


def test_migrate_shape():
    wf = _load("migrate")
    assert wf.name == "migrate"
    assert [p.id for p in wf.phases] == ["discover", "transform", "report"]
    assert [_phase_kind(p) for p in wf.phases] == ["foreach", "foreach", "reduce"]
    # the two foreach refs point at a list already in context at run time
    assert wf.phases[0].foreach == "{{targets}}"  # an INPUT list
    assert wf.phases[1].foreach == "{{discover}}"  # the PRIOR phase's list output
    assert wf.phases[2].reduce == "count"


def test_explain_repo_shape():
    wf = _load("explain-repo")
    assert wf.name == "explain-repo"
    assert [p.id for p in wf.phases] == ["read", "overview"]
    assert [_phase_kind(p) for p in wf.phases] == ["fanout", "reduce"]
    assert wf.phases[0].fanout.items == ["architecture", "entrypoints", "core", "tests", "tooling"]
    assert wf.phases[1].reduce == "markdown_table"


# --------------------------------------------------------------------------- #
# (2) each built-in runs to completion against a scripted MockProvider engine
# --------------------------------------------------------------------------- #


def test_review_runs_and_tabulates(tmp_path):
    # 3 dimensions -> 3 subagents, then a markdown_table reduce (no subagent).
    factory = _script_factory(
        tmp_path,
        [
            [_text('{"dimension": "bugs", "findings": ["off-by-one"], "summary": "one bug"}')],
            [_text('{"dimension": "security", "findings": [], "summary": "clean"}')],
            [_text('{"dimension": "performance", "findings": ["n+1"], "summary": "slow path"}')],
        ],
    )
    result = _run(factory, _load("review"), {"diff": "diff --git a/x b/x"})

    assert result.ok is True
    assert result.notes == []
    # fanout produced one structured finding per dimension, in item order
    assert [row["dimension"] for row in result.outputs["find"]] == [
        "bugs",
        "security",
        "performance",
    ]
    # reduce rendered a GFM table keyed by the finding dict columns
    table = result.outputs["report"]
    assert isinstance(table, str)
    header = table.splitlines()[0]
    assert "| dimension |" in header and "| summary |" in header
    assert "| bugs |" in table and "| security |" in table and "| performance |" in table
    assert result.final == table


def test_migrate_chains_two_foreach_refs_and_counts(tmp_path):
    # discover(2) over {{targets}} -> transform(2) over {{discover}} -> count.
    # 4 subagents in order: discover[0], discover[1], transform[0], transform[1].
    factory = _script_factory(
        tmp_path,
        [
            [_text('{"target": "billing.py", "kind": "rewrite", "reason": "legacy api"}')],
            [_text('{"target": "auth.py", "kind": "config", "reason": "new secret"}')],
            [_text('{"target": "billing.py", "change": "port to v2 client", "risk": "medium"}')],
            [_text('{"target": "auth.py", "change": "move key to vault", "risk": "low"}')],
        ],
    )
    result = _run(
        factory,
        _load("migrate"),
        {"targets": ["billing.py", "auth.py"], "goal": "upgrade to v2"},
    )

    assert result.ok is True
    # PROOF the refs resolved to lists: no unresolved-ref / structural note anywhere.
    assert result.notes == []
    # discover foreach walked the INPUT list
    assert [row["target"] for row in result.outputs["discover"]] == ["billing.py", "auth.py"]
    # transform foreach walked the PRIOR phase's list output (each item a discover dict)
    assert [row["change"] for row in result.outputs["transform"]] == [
        "port to v2 client",
        "move key to vault",
    ]
    # verify/report counted the proposed transforms
    assert result.outputs["report"] == 2
    assert result.final == 2


def test_explain_repo_runs_and_synthesizes(tmp_path):
    # 5 subsystems -> 5 subagents, then a markdown_table synthesis.
    subsystems = ["architecture", "entrypoints", "core", "tests", "tooling"]
    factory = _script_factory(
        tmp_path,
        [
            [_text(f'{{"subsystem": "{name}", "summary": "notes on {name}"}}')]
            for name in subsystems
        ],
    )
    result = _run(factory, _load("explain-repo"), {"repo": "IronCore"})

    assert result.ok is True
    assert result.notes == []
    assert [row["subsystem"] for row in result.outputs["read"]] == subsystems
    overview = result.outputs["overview"]
    assert isinstance(overview, str)
    assert "| subsystem |" in overview.splitlines()[0]
    for name in subsystems:
        assert f"| {name} |" in overview
    assert result.final == overview
