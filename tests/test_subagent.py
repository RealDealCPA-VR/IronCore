"""Subagent runner (IC-901): fresh-context, schema-validated delegated work.

Every case drives the REAL ``TurnEngine`` state machine through an
``engine_factory`` over a tmp workspace with a scripted ``MockProvider`` — zero
network, zero model. Async is driven with ``asyncio.run`` (no pytest-asyncio).
The pure helpers ``extract_json`` / ``validate_against`` are unit-tested directly.
"""

from __future__ import annotations

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry
from ironcore.workflows.subagent import (
    SubagentResult,
    SubagentTask,
    extract_json,
    run_subagent,
    validate_against,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    """A profile whose ladder recommends the native tool protocol."""
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _call(name: str, args: dict, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _factory(tmp_path, scripts: list[list], *, mode: Mode = Mode.MANUAL):
    """An ``engine_factory`` that hands each successive call a fresh engine with the
    next script in ``scripts`` (so a retry gets a different scripted provider). Also
    records how many times it was invoked."""
    calls = {"n": 0}
    it = iter(scripts)

    def make() -> TurnEngine:
        calls["n"] += 1
        script = next(it)
        settings = Settings.model_validate({"safety": {"network_tools": False}})
        tools = build_default_registry(settings, tmp_path)
        return TurnEngine(
            MockProvider(list(script)),
            tools,
            settings,
            _profile(),
            mode,
            workspace=tmp_path,
            snapshots=None,
        )

    make.calls = calls  # type: ignore[attr-defined]
    return make


def _run(task: SubagentTask, factory) -> SubagentResult:
    return asyncio.run(run_subagent(task, engine_factory=factory))


_SCHEMA = {
    "type": "object",
    "required": ["title", "severity"],
    "properties": {"title": {"type": "string"}, "severity": {"type": "string"}},
}


# --------------------------------------------------------------------------- #
# (1) no schema -> final text, ok=True
# --------------------------------------------------------------------------- #


def test_no_schema_returns_final_text_ok(tmp_path):
    factory = _factory(tmp_path, [[_text("All good; nothing to change.")]])
    task = SubagentTask(role="reviewer", prompt="review the diff")
    result = _run(task, factory)

    assert result.ok is True
    assert "All good" in result.text
    assert result.output == result.text
    assert result.error is None
    assert result.turns_used == 0
    assert result.transcript_ref == "reviewer#1"
    assert factory.calls["n"] == 1


# --------------------------------------------------------------------------- #
# (2) schema satisfied -> parsed object returned as output
# --------------------------------------------------------------------------- #


def test_schema_conforming_returns_parsed_object(tmp_path):
    content = 'Here is my finding:\n{"title": "off-by-one", "severity": "high"}\nDone.'
    factory = _factory(tmp_path, [[_text(content)]])
    task = SubagentTask(role="reviewer", prompt="find one bug", output_schema=_SCHEMA)
    result = _run(task, factory)

    assert result.ok is True
    assert result.output == {"title": "off-by-one", "severity": "high"}
    assert isinstance(result.output, dict)
    assert result.error is None
    assert factory.calls["n"] == 1  # no retry needed


# --------------------------------------------------------------------------- #
# (3) non-conforming -> exactly ONE retry; retry fixes it -> ok=True
# --------------------------------------------------------------------------- #


def test_nonconforming_then_retry_succeeds(tmp_path):
    bad = _text('{"title": "missing severity"}')  # required key absent
    good = _text('{"title": "now complete", "severity": "low"}')
    factory = _factory(tmp_path, [[bad], [good]])
    task = SubagentTask(role="reviewer", prompt="find one bug", output_schema=_SCHEMA)
    result = _run(task, factory)

    assert result.ok is True
    assert result.output == {"title": "now complete", "severity": "low"}
    assert result.error is None
    assert factory.calls["n"] == 2  # initial + exactly one retry
    assert result.transcript_ref == "reviewer#2"


# --------------------------------------------------------------------------- #
# (4) non-conforming twice -> ok=False after exactly one retry, clear error
# --------------------------------------------------------------------------- #


def test_nonconforming_twice_fails_after_one_retry(tmp_path):
    bad = _text('{"title": "still missing severity"}')
    # exactly two scripts -> a third factory call would StopIteration and error the
    # test, so passing proves the runner retried AT MOST once.
    factory = _factory(tmp_path, [[bad], [bad]])
    task = SubagentTask(role="verifier", prompt="verify", output_schema=_SCHEMA)
    result = _run(task, factory)

    assert result.ok is False
    assert result.output is None
    assert factory.calls["n"] == 2
    assert "after 1 retry" in result.error
    assert "severity" in result.error  # names the offending required key
    assert result.transcript_ref == "verifier#2"


def test_wrong_type_triggers_retry(tmp_path):
    # severity present but the wrong JSON type -> validation failure -> retry.
    bad = _text('{"title": "t", "severity": 5}')
    good = _text('{"title": "t", "severity": "med"}')
    factory = _factory(tmp_path, [[bad], [good]])
    task = SubagentTask(role="reviewer", prompt="find one bug", output_schema=_SCHEMA)
    result = _run(task, factory)

    assert result.ok is True
    assert result.output == {"title": "t", "severity": "med"}
    assert factory.calls["n"] == 2


# --------------------------------------------------------------------------- #
# (5) max_turns bounds a tool-happy subagent
# --------------------------------------------------------------------------- #


def test_max_turns_bounds_tool_happy_subagent(tmp_path):
    # DISTINCT paths per call so the engine's own identical-call loop detector never
    # fires (it would stop 3 identical calls at the budget); the subagent-level
    # max_turns must be the limiter here.
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text("data\n", encoding="utf-8")
    loop = [_text("", [_call("read_file", {"path": f"f{i}.txt"})]) for i in range(6)]
    factory = _factory(tmp_path, [loop])
    task = SubagentTask(role="looper", prompt="keep reading", max_turns=2, mode=Mode.AUTO)
    result = _run(task, factory)

    assert result.ok is False
    assert result.turns_used == 2  # exactly the cap; the 3rd request was not executed
    assert "max_turns" in result.error


# --------------------------------------------------------------------------- #
# (6) extract_json: last balanced object amid prose / strings / bad tails
# --------------------------------------------------------------------------- #


def test_extract_json_finds_last_object_amid_prose():
    text = 'intro {"a": 1} middle {"b": {"c": 2}} trailing words'
    assert extract_json(text) == {"b": {"c": 2}}


def test_extract_json_ignores_braces_inside_strings():
    text = 'note {"msg": "use {curly} inside", "n": 3} end'
    assert extract_json(text) == {"msg": "use {curly} inside", "n": 3}


def test_extract_json_skips_nonparsing_tail():
    # the last balanced group is not valid JSON -> fall back to the last one that parses
    text = 'valid {"x": 1} then {not: json}'
    assert extract_json(text) == {"x": 1}


def test_extract_json_returns_none_without_object():
    assert extract_json("there is no object here") is None
    assert extract_json("unterminated {open") is None


# --------------------------------------------------------------------------- #
# (7) validate_against: missing key, wrong type, bool!=int, top-level type
# --------------------------------------------------------------------------- #


def test_validate_against_accepts_conforming():
    assert validate_against({"title": "x", "severity": "high"}, _SCHEMA) is None


def test_validate_against_catches_missing_required_key():
    err = validate_against({"title": "x"}, _SCHEMA)
    assert err is not None
    assert "missing required key" in err
    assert "severity" in err


def test_validate_against_catches_wrong_type():
    schema = {
        "type": "object",
        "required": ["title", "count"],
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
    }
    err = validate_against({"title": "x", "count": "not-an-int"}, schema)
    assert err is not None
    assert "count" in err and "type" in err

    # a JSON bool must NOT satisfy integer
    err_bool = validate_against({"title": "x", "count": True}, schema)
    assert err_bool is not None and "count" in err_bool


def test_validate_against_top_level_type_mismatch():
    err = validate_against(["not", "an", "object"], _SCHEMA)
    assert err is not None
    assert "object" in err


def test_validate_against_no_schema_is_permissive():
    assert validate_against({"anything": 1}, None) is None
    assert validate_against({"anything": 1}, {}) is None


def test_validate_against_nested_and_array_items():
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "meta": {"type": "object", "required": ["id"]},
            "items": {"type": "array", "items": {"type": "string"}},
        },
    }
    assert validate_against({"items": ["a", "b"], "meta": {"id": "z"}}, schema) is None
    bad_item = validate_against({"items": ["a", 2]}, schema)
    assert bad_item is not None and "items[1]" in bad_item
    bad_nested = validate_against({"items": [], "meta": {}}, schema)
    assert bad_nested is not None and "id" in bad_nested
