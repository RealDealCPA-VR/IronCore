"""Guided decoding (strict_json rung): response_format builder, system fragment,
and the one-object parser (docs/plans/guided-decoding.md; SPEC §6, CONTRACTS §2).

Covers the accept table: a well-formed schema whose tool enum ends in ``done``,
a few-shot fragment carrying every tool + the finish, and a parser that turns a
call, a ``done`` finish, and garbage into a ``GuidedParse`` — never an exception.
"""

import json

import pytest

from ironcore.core.guided import (
    GuidedParse,
    parse_guided_tool_call,
    render_json_system_fragment,
    tool_call_response_format,
)
from ironcore.providers.base import ToolCall
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolRegistry

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _ReadFile(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace."
    risk = ToolRisk.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "workspace-relative path"},
            "start": {"type": "integer"},
        },
        "required": ["path"],
    }

    async def run(self, **kwargs):  # pragma: no cover - not exercised here
        raise NotImplementedError


class _ListDir(Tool):
    name = "list_dir"
    description = "List entries in a directory."
    risk = ToolRisk.READ
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs):  # pragma: no cover - not exercised here
        raise NotImplementedError


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_ReadFile())
    reg.register(_ListDir())
    return reg


# --------------------------------------------------------------------------- #
# tool_call_response_format
# --------------------------------------------------------------------------- #


def test_response_format_is_a_well_formed_json_schema_object():
    rf = tool_call_response_format(_registry().specs())
    assert rf["type"] == "json_schema"
    js = rf["json_schema"]
    assert js["name"] == "ironcore_tool_call"
    assert js["strict"] is True

    schema = js["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "tool" in schema["required"]
    assert "args" in schema["required"]

    props = schema["properties"]
    # tool enum == every tool name, in order, then the done pseudo-tool
    assert props["tool"]["type"] == "string"
    assert props["tool"]["enum"] == ["read_file", "list_dir", "done"]
    # args is an unconstrained object
    assert props["args"]["type"] == "object"


def test_response_format_empty_tools_still_allows_done():
    rf = tool_call_response_format([])
    assert rf["json_schema"]["schema"]["properties"]["tool"]["enum"] == ["done"]


def test_response_format_tolerates_flat_spec_and_dedupes():
    flat = [
        {"name": "grep", "description": "Search files."},
        {"name": "grep", "description": "dup dropped"},
        {"function": {"name": "write_file"}},
    ]
    enum = tool_call_response_format(flat)["json_schema"]["schema"]["properties"][
        "tool"
    ]["enum"]
    assert enum == ["grep", "write_file", "done"]


def test_response_format_is_json_serializable():
    # It goes straight into a request body, so it must round-trip through JSON.
    rf = tool_call_response_format(_registry().specs())
    assert json.loads(json.dumps(rf)) == rf


# --------------------------------------------------------------------------- #
# render_json_system_fragment
# --------------------------------------------------------------------------- #


def test_fragment_includes_every_tool_name_done_and_two_examples():
    frag = render_json_system_fragment(_registry().specs())
    # every tool name rendered in the catalog
    assert "read_file" in frag
    assert "list_dir" in frag
    # descriptions and params surfaced
    assert "Read a UTF-8 text file" in frag
    assert "path" in frag
    assert "required" in frag
    # the done pseudo-tool is taught
    assert "done" in frag
    # two worked examples: at least two tool-object literals appear as examples
    assert frag.count('{"tool": "') >= 2
    assert '"message"' in frag  # the done example carries a summary


def test_fragment_examples_parse_back_to_a_call_and_a_done():
    # The few-shot examples must model the exact syntax the parser accepts.
    frag = render_json_system_fragment([])
    call_ex = '{"tool": "read_file", "args": {"path": "src/app.py"}}'
    done_ex = '{"tool": "done", "args": {"message": "Read the file and fixed the bug."}}'
    assert call_ex in frag
    assert done_ex in frag
    assert parse_guided_tool_call(call_ex).call.name == "read_file"
    assert parse_guided_tool_call(done_ex).done is True


def test_fragment_handles_empty_tool_list():
    frag = render_json_system_fragment([])
    assert "no tools" in frag.lower()
    assert "done" in frag  # the finish is still taught


def test_fragment_is_ascii_safe():
    frag = render_json_system_fragment(_registry().specs())
    assert frag.isascii()


# --------------------------------------------------------------------------- #
# parse_guided_tool_call — happy paths
# --------------------------------------------------------------------------- #


def test_parse_valid_tool_call():
    res = parse_guided_tool_call('{"tool": "read_file", "args": {"path": "x"}}')
    assert isinstance(res, GuidedParse)
    assert res.error is None
    assert res.done is False
    assert isinstance(res.call, ToolCall)
    assert res.call.id == "gd-0"
    assert res.call.name == "read_file"
    assert res.call.arguments == {"path": "x"}


def test_parse_done_finishes_with_message():
    res = parse_guided_tool_call('{"tool": "done", "args": {"message": "all set"}}')
    assert res.done is True
    assert res.message == "all set"
    assert res.call is None
    assert res.error is None


def test_parse_done_without_message_is_empty_string():
    res = parse_guided_tool_call('{"tool": "done", "args": {}}')
    assert res.done is True
    assert res.message == ""
    assert res.error is None


def test_parse_empty_args_object_is_a_valid_call():
    res = parse_guided_tool_call('{"tool": "list_dir", "args": {}}')
    assert res.error is None
    assert res.call.name == "list_dir"
    assert res.call.arguments == {}


def test_parse_tolerates_surrounding_whitespace():
    res = parse_guided_tool_call('\n  {"tool": "list_dir", "args": {}}  \n')
    assert res.error is None
    assert res.call.name == "list_dir"


def test_parse_extracts_object_embedded_in_prose():
    # A server that ignored response_format may wrap the object in prose; a
    # single embedded object is still recovered.
    text = 'Sure! {"tool": "list_dir", "args": {}} hope that helps.'
    res = parse_guided_tool_call(text)
    assert res.error is None
    assert res.call.name == "list_dir"


# --------------------------------------------------------------------------- #
# parse_guided_tool_call — errors (repairable data, never exceptions)
# --------------------------------------------------------------------------- #


def test_parse_invalid_json_is_error_not_exception():
    res = parse_guided_tool_call("{oops")
    assert res.call is None
    assert res.done is False
    assert res.error is not None
    assert "tool" in res.error  # actionable template for the model
    assert res.text == "{oops"  # raw preserved


def test_parse_missing_tool_key_is_error():
    res = parse_guided_tool_call('{"args": {"path": "x"}}')
    assert res.call is None
    assert res.error is not None
    assert "tool" in res.error


def test_parse_non_string_tool_is_error():
    res = parse_guided_tool_call('{"tool": 5, "args": {}}')
    assert res.call is None
    assert res.error is not None


def test_parse_args_not_a_dict_is_error():
    res = parse_guided_tool_call('{"tool": "read_file", "args": ["x"]}')
    assert res.call is None
    assert res.error is not None
    assert "args" in res.error


def test_parse_missing_args_key_is_error():
    res = parse_guided_tool_call('{"tool": "read_file"}')
    assert res.call is None
    assert res.error is not None
    assert "args" in res.error


def test_parse_non_object_body_is_error():
    for body in ('"just a string"', "42", "[1, 2, 3]", "null", ""):
        res = parse_guided_tool_call(body)
        assert res.call is None
        assert res.done is False
        assert res.error is not None


@pytest.mark.parametrize(
    "body",
    [
        '{"tool": "read_file", "args": {"path": "x"}}',  # valid call
        '{"tool": "done", "args": {"message": "ok"}}',  # valid done
        "{not json",  # malformed
        '{"args": {}}',  # missing tool
        '"a string"',  # not an object
        "42",  # not an object
        '{"tool": "x", "args": 3}',  # bad args
        '{"tool": "done"}',  # done, no args
    ],
)
def test_parse_never_raises_on_any_body(body):
    res = parse_guided_tool_call(body)
    assert isinstance(res, GuidedParse)
    assert res.text == body
    # exactly one clean outcome, or an error — never a partial/contradictory state
    clean = (res.call is not None) or res.done
    assert clean != (res.error is not None)
