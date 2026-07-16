"""Workflow orchestrator (IC-903): sequential phases, concurrent isolated items.

Every case drives the REAL ``WorkflowRunner`` over tmp workspaces. Most phases use
the REAL ``TurnEngine`` + a scripted ``MockProvider`` (zero network, zero model); the
concurrency cases swap in a tiny ``_Probe`` provider that counts in-flight subagents
and can reverse completion order, proving the semaphore cap and gather-order
collection. Async is driven with ``asyncio.run`` (no pytest-asyncio). The reducers are
also unit-tested directly via ``apply_reduce``.
"""

from __future__ import annotations

import asyncio
import re

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, Provider, StreamEvent
from ironcore.providers.mock import MockProvider, RaiseError
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry
from ironcore.workflows.engine import (
    WorkflowProgress,
    WorkflowResult,
    WorkflowRunner,
    apply_reduce,
)
from ironcore.workflows.schema import Workflow, WorkflowError

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    """A profile whose ladder recommends the native tool protocol."""
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
    """An ``engine_factory`` that hands each successive call a fresh MockProvider-backed
    engine with the next script. Under MockProvider's no-suspension text responses the
    orchestrator runs items atomically in order, so script N drives item N."""
    calls = {"n": 0}
    it = iter(scripts)

    def make() -> TurnEngine:
        calls["n"] += 1
        return _engine_over(tmp_path, MockProvider(list(next(it))))

    make.calls = calls  # type: ignore[attr-defined]
    return make


def _run(runner: WorkflowRunner, workflow: Workflow, inputs: dict) -> WorkflowResult:
    return asyncio.run(runner.run(workflow, inputs))


_FINDING_SCHEMA = {
    "type": "object",
    "required": ["title", "severity"],
    "properties": {"title": {"type": "string"}, "severity": {"type": "string"}},
}


# --------------------------------------------------------------------------- #
# (1) full 3-phase workflow: fanout -> foreach -> reduce
# --------------------------------------------------------------------------- #


def _three_phase_workflow() -> Workflow:
    return Workflow.model_validate(
        {
            "name": "demo",
            "inputs": ["diff"],
            "phases": [
                {
                    "id": "find",
                    "fanout": {
                        "items": ["bugs", "security"],
                        "agent": {
                            "role": "reviewer",
                            "prompt": "Review {{diff}} for {{item}}",
                            "output_schema": _FINDING_SCHEMA,
                        },
                    },
                },
                {
                    "id": "verify",
                    "foreach": "{{find}}",
                    "agent": {"role": "verifier", "prompt": "Verify {{item.title}}"},
                },
                {"id": "report", "reduce": "count"},
            ],
        }
    )


def test_three_phase_workflow_keys_outputs_by_phase(tmp_path):
    factory = _script_factory(
        tmp_path,
        [
            [_text('{"title": "bug-a", "severity": "high"}')],  # find[0]
            [_text('{"title": "sec-b", "severity": "low"}')],  # find[1]
            [_text("verified bug-a")],  # verify[0]
            [_text("verified sec-b")],  # verify[1]
        ],
    )
    events: list[WorkflowProgress] = []
    runner = WorkflowRunner(engine_factory=factory, on_progress=events.append)
    result = _run(runner, _three_phase_workflow(), {"diff": "HEAD~1"})

    assert result.ok is True
    assert result.notes == []
    # outputs keyed by phase id
    assert set(result.outputs) == {"find", "verify", "report"}
    assert result.outputs["find"] == [
        {"title": "bug-a", "severity": "high"},
        {"title": "sec-b", "severity": "low"},
    ]
    assert result.outputs["verify"] == ["verified bug-a", "verified sec-b"]
    assert result.outputs["report"] == 2  # reduce=count over the 2 verify results
    assert result.final == 2
    assert factory.calls["n"] == 4  # one fresh engine per subagent, no retries

    # progress: exact deterministic beat sequence (atomic MockProvider items)
    beats = [(e.kind, e.phase_id) for e in events]
    assert beats == [
        ("phase_start", "find"),
        ("item_done", "find"),
        ("item_done", "find"),
        ("phase_done", "find"),
        ("phase_start", "verify"),
        ("item_done", "verify"),
        ("item_done", "verify"),
        ("phase_done", "verify"),
        ("phase_start", "report"),
        ("phase_done", "report"),
        ("workflow_done", "report"),
    ]
    # phase_start carries 1-based phase position; item_done carries item position
    start_find = next(e for e in events if e.kind == "phase_start" and e.phase_id == "find")
    assert (start_find.index, start_find.total, start_find.detail) == (1, 3, "fanout")


# --------------------------------------------------------------------------- #
# (2) per-agent failure isolation: one item fails -> None slot + note, run ok
# --------------------------------------------------------------------------- #


def _fanout_workflow(items: list[str], *, schema: dict | None = None) -> Workflow:
    return Workflow.model_validate(
        {
            "name": "fan",
            "phases": [
                {
                    "id": "scan",
                    "fanout": {
                        "items": items,
                        "agent": {
                            "role": "scanner",
                            "prompt": "Scan {{item}}",
                            "output_schema": schema,
                        },
                    },
                }
            ],
        }
    )


def test_one_failing_subagent_is_isolated(tmp_path):
    # middle item's provider raises -> subagent returns ok=False -> None slot + note.
    factory = _script_factory(
        tmp_path,
        [
            [_text("result-a")],
            [RaiseError("boom")],
            [_text("result-c")],
        ],
    )
    runner = WorkflowRunner(engine_factory=factory)
    result = _run(runner, _fanout_workflow(["a", "b", "c"]), {})

    assert result.ok is True  # a bad item never fails the workflow
    assert result.outputs["scan"] == ["result-a", None, "result-c"]  # None slot at index 1
    assert len(result.notes) == 1
    assert "scan[1]" in result.notes[0] and "boom" in result.notes[0]
    assert result.final == ["result-a", None, "result-c"]


def test_raised_exception_from_factory_is_isolated(tmp_path):
    # the engine_factory itself raising (run_subagent does NOT catch it) must still
    # isolate to a None slot + note, not abort the phase.
    calls = {"n": 0}

    def make() -> TurnEngine:
        calls["n"] += 1
        if calls["n"] == 2:  # atomic ordering -> call #2 is item index 1
            raise RuntimeError("factory exploded")
        return _engine_over(tmp_path, MockProvider([_text("ok")]))

    runner = WorkflowRunner(engine_factory=make)
    result = _run(runner, _fanout_workflow(["x", "y", "z"]), {})

    assert result.ok is True
    assert result.outputs["scan"] == ["ok", None, "ok"]
    assert len(result.notes) == 1
    assert "scan[1]" in result.notes[0] and "factory exploded" in result.notes[0]


def test_schema_failure_after_retry_is_isolated(tmp_path):
    # a subagent that never satisfies its schema -> ok=False -> None slot + note.
    bad = _text('{"title": "no severity"}')  # missing required key, twice
    factory = _script_factory(
        tmp_path,
        [
            [_text('{"title": "ok", "severity": "high"}')],
            [bad],  # initial attempt
            [bad],  # the one mechanical retry
        ],
    )
    runner = WorkflowRunner(engine_factory=factory)
    result = _run(runner, _fanout_workflow(["good", "bad"], schema=_FINDING_SCHEMA), {})

    assert result.ok is True
    assert result.outputs["scan"] == [{"title": "ok", "severity": "high"}, None]
    assert len(result.notes) == 1
    assert "severity" in result.notes[0]


# --------------------------------------------------------------------------- #
# (3) concurrency: a probe provider proves the cap and item-order collection
# --------------------------------------------------------------------------- #


class _Tracker:
    """Counts concurrently-live subagent streams and the peak."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0

    def enter(self) -> None:
        self.live += 1
        self.peak = max(self.peak, self.live)

    def exit(self) -> None:
        self.live -= 1


class _Probe(Provider):
    """A provider that echoes a ``<<marker>>`` from the prompt, yields control a
    marker-controlled number of times (so completion order can be steered), and
    records concurrency via a shared tracker."""

    name = "probe"
    _MARK = re.compile(r"<<(.+?)>>")

    def __init__(self, tracker: _Tracker, delays: dict[str, int]) -> None:
        self._tracker = tracker
        self._delays = delays

    async def complete(self, messages, *, tools=None, sampling=None):  # pragma: no cover
        raise NotImplementedError

    async def stream(
        self, messages, *, tools=None, sampling=None, response_format=None, extra_body=None
    ):
        joined = " ".join(m.content for m in messages)
        match = self._MARK.search(joined)
        marker = match.group(1) if match else "?"
        self._tracker.enter()
        try:
            for _ in range(self._delays.get(marker, 2)):
                await asyncio.sleep(0)  # a real suspension point -> genuine overlap
            yield StreamEvent(kind="text", text=marker)
            yield StreamEvent(kind="done", data={"finish_reason": "stop"})
        finally:
            self._tracker.exit()

    async def list_models(self) -> list[str]:
        return ["probe"]


def _probe_factory(tmp_path, tracker: _Tracker, delays: dict[str, int]):
    def make() -> TurnEngine:
        return _engine_over(tmp_path, _Probe(tracker, delays))

    return make


def test_concurrency_cap_is_respected(tmp_path):
    tracker = _Tracker()
    factory = _probe_factory(tmp_path, tracker, {})  # uniform 2-yield streams
    items = [f"p{i}" for i in range(5)]
    workflow = Workflow.model_validate(
        {
            "name": "cap",
            "phases": [
                {
                    "id": "wide",
                    "fanout": {
                        "items": items,
                        "agent": {"role": "w", "prompt": "run <<{{item}}>>"},
                    },
                }
            ],
        }
    )
    runner = WorkflowRunner(engine_factory=factory, concurrency=2)
    result = _run(runner, workflow, {})

    assert result.ok is True
    assert tracker.peak == 2  # never more than the cap in flight
    assert result.outputs["wide"] == items  # collected in item order


def test_results_ordered_regardless_of_completion(tmp_path):
    tracker = _Tracker()
    # m3 finishes first (fewest yields), m0 last -> completion order reverses item order.
    delays = {"m0": 8, "m1": 6, "m2": 4, "m3": 2}
    factory = _probe_factory(tmp_path, tracker, delays)
    items = ["m0", "m1", "m2", "m3"]
    workflow = Workflow.model_validate(
        {
            "name": "order",
            "phases": [
                {
                    "id": "race",
                    "fanout": {
                        "items": items,
                        "agent": {"role": "r", "prompt": "go <<{{item}}>>"},
                    },
                }
            ],
        }
    )
    events: list[WorkflowProgress] = []
    runner = WorkflowRunner(engine_factory=factory, concurrency=4, on_progress=events.append)
    result = _run(runner, workflow, {})

    assert result.outputs["race"] == items  # gather preserves ARGUMENT order
    # but the fastest item (m3, index 4) emitted its item_done FIRST -> completion
    # order genuinely differs from item order.
    item_dones = [e.index for e in events if e.kind == "item_done"]
    assert item_dones[0] == 4
    assert sorted(item_dones) == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# (4) reducers via apply_reduce (count / concat / list / markdown_table + dict spec)
# --------------------------------------------------------------------------- #


def test_reduce_count_ignores_none_slots():
    assert apply_reduce("count", ["a", None, "b", None, "c"]) == 3


def test_reduce_concat_and_list_flatten_one_level():
    values = [["a", "b"], None, ["c"], "d"]
    assert apply_reduce("concat", values) == ["a", "b", "c", "d"]
    assert apply_reduce("list", values) == ["a", "b", "c", "d"]


def test_reduce_markdown_table_from_dicts():
    rows = [
        {"title": "off-by-one", "severity": "high"},
        {"title": "xss", "severity": "med"},
    ]
    table = apply_reduce("markdown_table", rows)
    assert table.splitlines()[0] == "| title | severity |"
    assert table.splitlines()[1] == "| --- | --- |"
    assert "| off-by-one | high |" in table
    assert "| xss | med |" in table


def test_reduce_markdown_table_scalars_and_none():
    table = apply_reduce("markdown_table", ["alpha", None, "beta"])
    assert table.splitlines()[0] == "| value |"
    assert "| alpha |" in table and "| beta |" in table


def test_reduce_inline_dict_spec_selects_columns():
    rows = [{"title": "t", "severity": "high", "file": "a.py"}]
    table = apply_reduce({"op": "markdown_table", "columns": ["severity", "title"]}, rows)
    assert table.splitlines()[0] == "| severity | title |"
    assert "| high | t |" in table


def test_reduce_unknown_name_raises_workflow_error():
    try:
        apply_reduce("frobnicate", [1, 2])
    except WorkflowError as exc:
        assert "frobnicate" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected WorkflowError")


# --------------------------------------------------------------------------- #
# (5) reduce wired end-to-end through run() over a prior phase
# --------------------------------------------------------------------------- #


def test_reduce_markdown_table_end_to_end(tmp_path):
    factory = _script_factory(
        tmp_path,
        [
            [_text('{"title": "t0", "severity": "high"}')],
            [_text('{"title": "t1", "severity": "low"}')],
        ],
    )
    workflow = Workflow.model_validate(
        {
            "name": "rep",
            "phases": [
                {
                    "id": "find",
                    "fanout": {
                        "items": ["a", "b"],
                        "agent": {
                            "role": "r",
                            "prompt": "Find {{item}}",
                            "output_schema": _FINDING_SCHEMA,
                        },
                    },
                },
                {"id": "report", "reduce": "markdown_table"},
            ],
        }
    )
    runner = WorkflowRunner(engine_factory=factory)
    result = _run(runner, workflow, {})

    assert result.ok is True
    assert isinstance(result.final, str)
    assert "| title | severity |" in result.final
    assert "| t0 | high |" in result.final and "| t1 | low |" in result.final


# --------------------------------------------------------------------------- #
# (6) structural errors: missing foreach ref, reduce with no prior phase
# --------------------------------------------------------------------------- #


def test_missing_foreach_ref_records_note_and_flips_ok(tmp_path):
    # documented choice: a bad {{ref}} is a terminal error recorded as a note with
    # ok=False; run() RETURNS (does not raise), preserving partial outputs.
    factory = _script_factory(tmp_path, [[_text("a")], [_text("b")]])
    workflow = Workflow.model_validate(
        {
            "name": "bad-ref",
            "phases": [
                {
                    "id": "scan",
                    "fanout": {
                        "items": ["a", "b"],
                        "agent": {"role": "s", "prompt": "Scan {{item}}"},
                    },
                },
                {
                    "id": "verify",
                    "foreach": "{{nope.missing}}",
                    "agent": {"role": "v", "prompt": "V {{item}}"},
                },
            ],
        }
    )
    events: list[WorkflowProgress] = []
    runner = WorkflowRunner(engine_factory=factory, on_progress=events.append)
    result = _run(runner, workflow, {})

    assert result.ok is False
    assert "scan" in result.outputs and "verify" not in result.outputs  # partial preserved
    assert result.outputs["scan"] == ["a", "b"]
    assert any("nope" in note and "verify" in note for note in result.notes)
    assert result.final == ["a", "b"]  # last COMPLETED phase output
    assert [e for e in events if e.kind == "workflow_done"][0].detail == "error"


def test_reduce_without_prior_phase_records_note(tmp_path):
    factory = _script_factory(tmp_path, [])
    workflow = Workflow.model_validate(
        {"name": "solo", "phases": [{"id": "report", "reduce": "count"}]}
    )
    runner = WorkflowRunner(engine_factory=factory)
    result = _run(runner, workflow, {})

    assert result.ok is False
    assert result.outputs == {}
    assert any("no prior phase" in note for note in result.notes)


# --------------------------------------------------------------------------- #
# (7) render_summary
# --------------------------------------------------------------------------- #


def test_render_summary_reports_status_phases_and_notes():
    ok = WorkflowResult(ok=True, outputs={"find": [1, 2, 3], "report": 3}, notes=[], final=3)
    text = ok.render_summary()
    assert "workflow ok" in text
    assert "find:" in text and "report:" in text

    failed = WorkflowResult(
        ok=False, outputs={"find": ["a"]}, notes=["scan[1] failed: boom"], final=["a"]
    )
    ftext = failed.render_summary()
    assert "FAILED" in ftext
    assert "notes (1)" in ftext
    assert "scan[1] failed: boom" in ftext
