"""IRONCALL text protocol: encoder, parser, and ironresult renderer.

Covers the accept table from IC-606 (SPEC §6.3, CONTRACTS §10): clean parse,
prose capture, multi-block warning, malformed JSON, missing/invalid keys, CRLF
framing, system-fragment rendering, and ironresult round-trip.
"""

import json

import pytest

from ironcore.core.ironcall import (
    IroncallParse,
    parse,
    render_result,
    render_system_fragment,
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


def _block(tool: str, args: dict) -> str:
    return f"```ironcall\n{json.dumps({'tool': tool, 'args': args})}\n```"


# --------------------------------------------------------------------------- #
# parse — happy path
# --------------------------------------------------------------------------- #


def test_clean_single_block_parses_to_one_call():
    res = parse(_block("read_file", {"path": "src/app.py"}))
    assert isinstance(res, IroncallParse)
    assert res.error is None
    assert res.warning is None
    (call,) = res.calls
    assert isinstance(call, ToolCall)
    assert call.id == "ic-0"
    assert call.name == "read_file"
    assert call.arguments == {"path": "src/app.py"}


def test_no_block_is_pure_prose_not_an_error():
    res = parse("I think we should read the config first, then decide.")
    assert res.calls == []
    assert res.error is None
    assert res.warning is None
    assert "read the config" in res.text


def test_missing_args_defaults_to_empty_dict():
    res = parse('```ironcall\n{"tool": "list_dir"}\n```')
    assert res.error is None
    (call,) = res.calls
    assert call.name == "list_dir"
    assert call.arguments == {}


def test_empty_args_object_is_valid():
    res = parse(_block("list_dir", {}))
    assert res.error is None
    (call,) = res.calls
    assert call.arguments == {}


def test_prose_around_block_captured_as_text():
    text = (
        "Let me read that file to understand the bug.\n"
        + _block("read_file", {"path": "a.py"})
        + "\nThen I'll patch it."
    )
    res = parse(text)
    (call,) = res.calls
    assert call.name == "read_file"
    assert "read that file" in res.text
    assert "patch it" in res.text
    assert "```ironcall" not in res.text  # block itself is stripped from prose


# --------------------------------------------------------------------------- #
# parse — tolerance
# --------------------------------------------------------------------------- #


def test_crlf_framing_tolerated():
    text = (
        "```ironcall\r\n"
        '{"tool": "read_file", "args": {"path": "a.py"}}\r\n'
        "```\r\n"
    )
    res = parse(text)
    assert res.error is None
    (call,) = res.calls
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.py"}


def test_iron_call_underscore_and_case_tolerated():
    text = '```IRON_CALL\n{"tool": "list_dir", "args": {}}\n```'
    res = parse(text)
    assert res.error is None
    (call,) = res.calls
    assert call.name == "list_dir"


def test_info_line_whitespace_tolerated():
    text = '```  ironcall  \n{"tool": "list_dir", "args": {}}\n```'
    res = parse(text)
    assert res.error is None
    assert res.calls[0].name == "list_dir"


# --------------------------------------------------------------------------- #
# parse — multiple blocks
# --------------------------------------------------------------------------- #


def test_multiple_blocks_takes_first_and_warns():
    text = (
        _block("read_file", {"path": "a.py"})
        + "\nand also\n"
        + _block("list_dir", {})
    )
    res = parse(text)
    assert len(res.calls) == 1
    assert res.calls[0].name == "read_file"
    assert res.calls[0].arguments == {"path": "a.py"}
    assert res.warning is not None
    assert "first" in res.warning.lower()
    assert res.error is None  # multiple blocks is non-fatal


def test_second_block_stripped_from_prose():
    text = _block("read_file", {"path": "a.py"}) + "\n" + _block("list_dir", {})
    res = parse(text)
    assert "```ironcall" not in res.text


# --------------------------------------------------------------------------- #
# parse — errors (repairable data, never exceptions)
# --------------------------------------------------------------------------- #


def test_invalid_json_becomes_actionable_error_not_exception():
    res = parse('```ironcall\n{"tool": "read_file", "args": {path: oops}}\n```')
    assert res.calls == []
    assert res.error is not None
    assert "not valid JSON" in res.error
    assert "```ironcall" in res.error  # actionable template for the model


def test_missing_tool_key_is_error():
    res = parse('```ironcall\n{"args": {"path": "a.py"}}\n```')
    assert res.calls == []
    assert res.error is not None
    assert "tool" in res.error


def test_tool_not_a_string_is_error():
    res = parse('```ironcall\n{"tool": 5, "args": {}}\n```')
    assert res.calls == []
    assert res.error is not None
    assert "tool" in res.error


def test_empty_tool_string_is_error():
    res = parse('```ironcall\n{"tool": "   ", "args": {}}\n```')
    assert res.calls == []
    assert res.error is not None


def test_args_not_a_dict_is_error():
    res = parse('```ironcall\n{"tool": "read_file", "args": ["a.py"]}\n```')
    assert res.calls == []
    assert res.error is not None
    assert "args" in res.error


def test_body_not_an_object_is_error():
    res = parse('```ironcall\n["read_file", {"path": "a.py"}]\n```')
    assert res.calls == []
    assert res.error is not None
    assert "object" in res.error


def test_error_still_preserves_prose_and_warning():
    text = (
        "Reading now.\n"
        '```ironcall\n{"tool": "read_file", "args": {oops}}\n```\n'
        '```ironcall\n{"tool": "list_dir", "args": {}}\n```'
    )
    res = parse(text)
    assert res.error is not None  # first block malformed
    assert res.warning is not None  # two blocks present
    assert "Reading now" in res.text


@pytest.mark.parametrize(
    "payload",
    [
        '{"tool": "read_file", "args": {"path": "a.py"}}',  # valid
        '{"tool": "read_file"}',  # valid (no args)
        "{not json",  # malformed
        '{"args": {}}',  # missing tool
        '"just a string"',  # not an object
        "42",  # not an object
        '{"tool": "x", "args": 3}',  # bad args
    ],
)
def test_parse_never_raises_on_any_body(payload):
    # Fuzz-ish: whatever the body, parse returns a value, never an exception.
    res = parse(f"```ironcall\n{payload}\n```")
    assert isinstance(res, IroncallParse)
    assert (res.calls and res.error is None) or (not res.calls and res.error)


# --------------------------------------------------------------------------- #
# render_system_fragment
# --------------------------------------------------------------------------- #


def test_fragment_includes_every_tool_name_and_both_examples():
    registry = ToolRegistry()
    registry.register(_ReadFile())
    registry.register(_ListDir())
    frag = render_system_fragment(registry.specs())

    # every tool name rendered in the catalog
    assert "read_file" in frag
    assert "list_dir" in frag
    # descriptions and params surfaced
    assert "Read a UTF-8 text file" in frag
    assert "path" in frag
    assert "required" in frag
    # the rule mentions both fenced block kinds
    assert "ironcall" in frag
    assert "ironresult" in frag
    # two worked examples: at least two ironcall fences appear
    assert frag.count("```ironcall") >= 2


def test_fragment_examples_are_themselves_parseable():
    # The few-shot examples must model the exact syntax the parser accepts.
    frag = render_system_fragment([])
    res = parse(frag)
    assert res.error is None
    # first example is the read_file call with args
    assert res.calls[0].name == "read_file"
    assert res.calls[0].arguments == {"path": "src/app.py"}
    # and there are two example blocks -> non-fatal warning
    assert res.warning is not None


def test_fragment_handles_empty_tool_list():
    frag = render_system_fragment([])
    assert "no tools" in frag.lower()
    assert "```ironcall" in frag  # examples still present


def test_fragment_accepts_flat_spec_shape():
    flat = [{"name": "grep", "description": "Search files.", "parameters": {}}]
    frag = render_system_fragment(flat)
    assert "grep" in frag
    assert "Search files." in frag


# --------------------------------------------------------------------------- #
# render_result
# --------------------------------------------------------------------------- #


def test_render_result_roundtrips_as_json():
    block = render_result("ic-0", "hello\nworld", ok=True)
    assert block.startswith("```ironresult")
    assert block.rstrip().endswith("```")
    body = block.split("\n", 1)[1].rsplit("\n", 1)[0]
    data = json.loads(body)
    assert data == {"id": "ic-0", "ok": True, "output": "hello\nworld"}


def test_render_result_carries_ok_false():
    block = render_result("ic-0", "boom", ok=False)
    body = block.split("\n", 1)[1].rsplit("\n", 1)[0]
    data = json.loads(body)
    assert data["ok"] is False
    assert data["output"] == "boom"


def test_render_result_escapes_special_chars():
    tricky = 'quote " and backtick ``` and unicode ✓'
    block = render_result("ic-9", tricky, ok=True)
    body = block.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert json.loads(body)["output"] == tricky
