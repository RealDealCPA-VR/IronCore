"""Workflow YAML schema + loader (IC-902).

Fixtures are inline YAML strings (no fixture files → no CRLF surprises). Every
invalid case must surface a pointed WorkflowError; the valid pipeline must load
into a fully typed Workflow.
"""

from pathlib import Path

import pytest

from ironcore.workflows.schema import (
    AgentSpec,
    Fanout,
    Phase,
    Workflow,
    WorkflowError,
    discover_workflows,
    interpolate,
    load_workflow,
    load_workflow_file,
)

VALID_PIPELINE = """
name: review
description: review the working diff across dimensions
inputs: [diff_ref]
phases:
  - id: find
    fanout:
      items: [bugs, security, performance]
      agent:
        role: reviewer
        prompt: |
          Review {{diff_ref}} for {{item}} issues only.
        output_schema:
          findings: list
  - id: verify
    foreach: "{{find.findings}}"
    agent:
      role: verifier
      prompt: "Adversarially verify: {{item.title}}"
  - id: report
    reduce: markdown_table
"""


def test_valid_pipeline_loads():
    wf = load_workflow(VALID_PIPELINE, source="review.yaml")
    assert isinstance(wf, Workflow)
    assert wf.name == "review"
    assert wf.description.startswith("review the working diff")
    assert wf.inputs == ["diff_ref"]
    assert len(wf.phases) == 3

    find, verify, report = wf.phases
    assert isinstance(find, Phase)

    assert isinstance(find.fanout, Fanout)
    assert find.foreach is None and find.reduce is None
    assert find.fanout.items == ["bugs", "security", "performance"]
    assert isinstance(find.fanout.agent, AgentSpec)
    assert find.fanout.agent.role == "reviewer"
    assert "{{diff_ref}}" in find.fanout.agent.prompt
    assert find.fanout.agent.output_schema == {"findings": "list"}

    assert verify.foreach == "{{find.findings}}"
    assert verify.fanout is None
    assert verify.agent is not None and verify.agent.role == "verifier"
    assert verify.agent.output_schema is None  # optional

    assert report.reduce == "markdown_table"
    assert report.fanout is None and report.foreach is None and report.agent is None


def test_defaults_description_and_inputs():
    wf = load_workflow(
        "name: minimal\nphases:\n  - id: only\n    reduce: passthrough\n",
        source="minimal.yaml",
    )
    assert wf.description == ""
    assert wf.inputs == []


# --- invalid fixtures: each must raise a pointed WorkflowError ----------------


def _err(text: str) -> str:
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(text, source="bad.yaml")
    message = str(excinfo.value)
    assert "bad.yaml" in message  # every message names the source
    return message


def test_missing_name():
    msg = _err("phases:\n  - id: a\n    reduce: r\n")
    assert "name" in msg


def test_unknown_phase_kind():
    msg = _err("name: w\nphases:\n  - id: a\n    mapreduce: nope\n")
    assert "mapreduce" in msg  # extra key named


def test_two_phase_kinds_set_at_once():
    text = (
        "name: w\nphases:\n"
        "  - id: a\n"
        "    reduce: r\n"
        "    fanout:\n"
        "      items: [x]\n"
        "      agent: {role: q, prompt: p}\n"
    )
    msg = _err(text)
    assert "multiple phase-kinds" in msg


def test_bad_output_schema_type():
    text = (
        "name: w\nphases:\n"
        "  - id: a\n"
        "    fanout:\n"
        "      items: [x]\n"
        "      agent:\n"
        "        role: q\n"
        "        prompt: p\n"
        "        output_schema: findings.v1\n"  # a string, not a mapping
    )
    msg = _err(text)
    assert "output_schema" in msg


def test_malformed_yaml():
    msg = _err("name: [unclosed\nphases: oops\n")
    assert "invalid YAML" in msg


def test_empty_phases():
    msg = _err("name: w\nphases: []\n")
    assert "no phases" in msg


def test_duplicate_phase_id():
    text = "name: w\nphases:\n  - id: dup\n    reduce: r\n  - id: dup\n    reduce: s\n"
    msg = _err(text)
    assert "duplicate phase id" in msg and "dup" in msg


def test_missing_agent_in_fanout():
    text = "name: w\nphases:\n  - id: a\n    fanout:\n      items: [x]\n"
    msg = _err(text)
    assert "agent" in msg


def test_foreach_without_agent():
    text = 'name: w\nphases:\n  - id: a\n    foreach: "{{prev.out}}"\n'
    msg = _err(text)
    assert "agent" in msg


def test_foreach_must_be_reference():
    text = "name: w\nphases:\n  - id: a\n    foreach: prev\n    agent: {role: q, prompt: p}\n"
    msg = _err(text)
    assert "reference" in msg


def test_top_level_agent_only_on_foreach():
    text = (
        "name: w\nphases:\n"
        "  - id: a\n"
        "    reduce: r\n"
        "    agent: {role: q, prompt: p}\n"
    )
    msg = _err(text)
    assert "foreach" in msg


def test_workflow_must_be_mapping():
    msg = _err("- just\n- a\n- list\n")
    assert "mapping" in msg


# --- yaml.safe_load security -------------------------------------------------


def test_yaml_tag_is_rejected_not_executed(tmp_path: Path):
    # yaml.load would construct/execute this tag; yaml.safe_load must refuse it.
    canary = tmp_path / "canary.txt"
    payload = f'name: !!python/object/apply:os.system ["echo pwned > {canary.as_posix()}"]\n'
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(payload, source="evil.yaml")
    assert "invalid YAML" in str(excinfo.value)
    assert not canary.exists()  # nothing executed


# --- interpolate -------------------------------------------------------------


def test_interpolate_simple():
    assert interpolate("hello {{x}}", {"x": "world"}) == "hello world"


def test_interpolate_dotted_lookup():
    ctx = {"find": {"items": ["a", "b"]}}
    assert interpolate("items: {{find.items}}", ctx) == "items: ['a', 'b']"


def test_interpolate_multiple_placeholders_and_whitespace():
    ctx = {"a": 1, "b": 2}
    assert interpolate("{{ a }} and {{b}}", ctx) == "1 and 2"


def test_interpolate_missing_var_raises_naming_it():
    with pytest.raises(WorkflowError) as excinfo:
        interpolate("value: {{missing}}", {"present": 1})
    assert "missing" in str(excinfo.value)


def test_interpolate_missing_dotted_part_names_path():
    with pytest.raises(WorkflowError) as excinfo:
        interpolate("{{find.absent}}", {"find": {"items": []}})
    assert "find.absent" in str(excinfo.value)


# --- file loading + discovery ------------------------------------------------


def test_load_workflow_file(tmp_path: Path):
    path = tmp_path / "review.yaml"
    path.write_text(VALID_PIPELINE, encoding="utf-8")
    wf = load_workflow_file(path)
    assert wf.name == "review"


def test_load_workflow_accepts_path(tmp_path: Path):
    path = tmp_path / "review.yaml"
    path.write_text(VALID_PIPELINE, encoding="utf-8")
    wf = load_workflow(path)  # Path delegates to load_workflow_file
    assert wf.name == "review"


def test_load_workflow_file_missing_raises(tmp_path: Path):
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow_file(tmp_path / "nope.yaml")
    assert "cannot read" in str(excinfo.value)


def test_discover_workflows(tmp_path: Path):
    (tmp_path / "review.yaml").write_text("name: review\n", encoding="utf-8")
    (tmp_path / "migrate.yml").write_text("name: migrate\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignore me\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()  # directories ignored

    found = discover_workflows(tmp_path)
    assert set(found) == {"review", "migrate"}
    assert found["review"] == tmp_path / "review.yaml"
    assert found["migrate"] == tmp_path / "migrate.yml"


def test_discover_prefers_yaml_over_yml(tmp_path: Path):
    (tmp_path / "dup.yaml").write_text("name: a\n", encoding="utf-8")
    (tmp_path / "dup.yml").write_text("name: b\n", encoding="utf-8")
    found = discover_workflows(tmp_path)
    assert found["dup"] == tmp_path / "dup.yaml"


def test_discover_missing_dir_returns_empty(tmp_path: Path):
    assert discover_workflows(tmp_path / "nope") == {}
