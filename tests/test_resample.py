"""core.resample (MS-4): candidate generation, mechanical verifiers, prompts.

Pure/unit half of the best-of-N escape hatches: every case runs against
``MockProvider`` (zero network, zero model) or plain strings. The engine
integration — seams, budget, gating — lives in tests/test_engine_resample.py.
"""

from __future__ import annotations

import asyncio

from ironcore.core.resample import (
    Candidate,
    generate_candidate,
    reissue_edit_prompt,
    render_call_echo,
    verify_edit_candidate,
    verify_tool_candidate,
)
from ironcore.providers.base import CompletionResult, Message, SamplingPolicy, ToolCall
from ironcore.providers.mock import MockProvider, RaiseError

CURRENT = "def f():\n    return 1\n"

GOOD_SR = "<<<<<<< SEARCH\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE"
#: a hunk whose context does not exist in CURRENT — mechanically unappliable.
BAD_DIFF = "@@ -1,2 +1,2 @@\n def g():\n-    return 9\n+    return 2\n"


def _call(name: str = "edit_file", **args) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=args)


def _edit_call(fmt: str = "search_replace", edit: str = GOOD_SR, path: str = "app.py") -> ToolCall:
    return _call("edit_file", path=path, format=fmt, edit=edit)


def _candidate(*calls: ToolCall, error: str | None = None) -> Candidate:
    return Candidate(calls=list(calls), text="", tokens=0, error=error)


def _completion(
    content: str = "", calls: list[ToolCall] | None = None, **usage
) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or []),
        usage=dict(usage),
    )


def _generate(provider, *, protocol: str, **kwargs) -> Candidate:
    msgs = [Message(role="user", content="fix it")]
    return asyncio.run(generate_candidate(provider, msgs, protocol=protocol, **kwargs))


# --------------------------------------------------------------------------- #
# verify_edit_candidate — the pure patch pre-check
# --------------------------------------------------------------------------- #


def test_edit_verifier_passes_an_applying_search_replace():
    ok, why = verify_edit_candidate(_candidate(_edit_call()), CURRENT)
    assert ok and why == ""


def test_edit_verifier_fails_a_non_applying_unified_diff():
    ok, why = verify_edit_candidate(
        _candidate(_edit_call(fmt="unified_diff", edit=BAD_DIFF)), CURRENT
    )
    assert not ok
    assert "does not apply" in why


def test_edit_verifier_rejects_wrong_tool_unknown_format_and_multi_call():
    ok, why = verify_edit_candidate(_candidate(_call("write_file", path="a", content="x")), CURRENT)
    assert not ok and "edit_file" in why

    ok, why = verify_edit_candidate(_candidate(_edit_call(fmt="magic_format")), CURRENT)
    assert not ok and "magic_format" in why  # plugin/unknown formats cannot pre-verify

    ok, why = verify_edit_candidate(_candidate(_edit_call(), _edit_call()), CURRENT)
    assert not ok and "exactly one" in why


def test_edit_verifier_rejects_a_path_switch_and_failed_candidates():
    ok, why = verify_edit_candidate(
        _candidate(_edit_call(path="other.py")), CURRENT, expected_path="app.py"
    )
    assert not ok and "other.py" in why

    ok, why = verify_edit_candidate(_candidate(error="boom"), CURRENT)
    assert not ok and why == "boom"

    ok, why = verify_edit_candidate(
        _candidate(_call("edit_file", path="app.py", format="whole_file")), CURRENT
    )
    assert not ok and "'edit'" in why  # missing edit payload


# --------------------------------------------------------------------------- #
# verify_tool_candidate — every call must name a registered tool
# --------------------------------------------------------------------------- #


def test_tool_verifier_passes_only_known_tools():
    known = {"read_file", "edit_file"}
    good = _candidate(_call("read_file", path="x"))
    assert verify_tool_candidate(good, known)
    assert not verify_tool_candidate(_candidate(_call("teleport")), known)
    assert not verify_tool_candidate(_candidate(), known)  # no calls at all
    assert not verify_tool_candidate(_candidate(_call("read_file"), error="bad"), known)


# --------------------------------------------------------------------------- #
# generate_candidate — one complete() per candidate, parsed per rung
# --------------------------------------------------------------------------- #


def test_generate_parses_the_native_rung_and_charges_tokens():
    call = _call("read_file", path="x")
    mock = MockProvider([_completion(calls=[call], total_tokens=42)])
    candidate = _generate(mock, protocol="native", sampling=SamplingPolicy(temperature=0.4))
    assert candidate.error is None
    assert [c.name for c in candidate.calls] == ["read_file"]
    assert candidate.tokens == 42
    assert len(mock.calls) == 1  # exactly one provider call per candidate
    assert mock.last_sampling is not None and mock.last_sampling.temperature == 0.4


def test_generate_parses_the_ironcall_text_floor():
    block = '```ironcall\n{"tool": "read_file", "args": {"path": "x"}}\n```'
    mock = MockProvider([_completion(content=f"let me look\n{block}")])
    candidate = _generate(mock, protocol="text_protocol")
    assert candidate.error is None
    assert [c.name for c in candidate.calls] == ["read_file"]
    assert block in candidate.text  # the raw reply is preserved for the history echo


def test_generate_parses_the_guided_rung_and_forwards_response_format():
    mock = MockProvider([_completion(content='{"tool": "read_file", "args": {"path": "x"}}')])
    rf = {"type": "json_schema", "json_schema": {"name": "ironcore_tool_call"}}
    candidate = _generate(mock, protocol="strict_json", response_format=rf)
    assert candidate.error is None
    assert [c.name for c in candidate.calls] == ["read_file"]
    assert mock.last_response_format is rf  # guided candidates stay server-constrained


def test_generate_fails_malformed_done_and_call_free_replies_as_data():
    # malformed ironcall body -> a precise parse error, never an exception
    mock = MockProvider([_completion(content="```ironcall\n{not valid json\n```")])
    candidate = _generate(mock, protocol="text_protocol")
    assert candidate.error is not None and not candidate.calls

    # a guided `done` finish is not a racable call
    mock = MockProvider([_completion(content='{"tool": "done", "args": {"message": "hi"}}')])
    candidate = _generate(mock, protocol="strict_json")
    assert candidate.error is not None and "finished" in candidate.error

    # a native reply with no tool_calls fails the candidate
    mock = MockProvider([_completion(content="I think we should talk about it")])
    candidate = _generate(mock, protocol="native")
    assert candidate.error is not None and "no tool call" in candidate.error


def test_generate_turns_provider_errors_into_failed_candidates():
    mock = MockProvider([RaiseError("backend exploded")])
    candidate = _generate(mock, protocol="native")
    assert candidate.error is not None and "backend exploded" in candidate.error
    assert candidate.calls == []


# --------------------------------------------------------------------------- #
# reissue_edit_prompt / render_call_echo — deterministic framing
# --------------------------------------------------------------------------- #


def test_reissue_prompt_names_path_format_and_reason_deterministically():
    call = _edit_call(fmt="unified_diff", edit=BAD_DIFF)
    prompt = reissue_edit_prompt(call, "hunk 1 does not apply")
    assert "'app.py'" in prompt
    assert "unified_diff" in prompt
    assert "hunk 1 does not apply" in prompt
    assert prompt == reissue_edit_prompt(call, "hunk 1 does not apply")  # pure
    # an empty reason still yields an actionable instruction
    assert "could not be applied" in reissue_edit_prompt(call, "")


def test_render_call_echo_matches_each_rungs_wire_form():
    call = _call("read_file", path="x")
    assert render_call_echo(call, "native") == ""  # native echoes via tool_calls
    floor = render_call_echo(call, "text_protocol")
    assert floor.startswith("```ironcall") and '"read_file"' in floor
    guided = render_call_echo(call, "strict_json")
    assert guided.startswith("{") and '"read_file"' in guided
