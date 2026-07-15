"""Provider wire types and the MockProvider used by all offline tests."""

import asyncio
from pathlib import Path

import pytest

from ironcore.providers import (
    CompletionResult,
    MalformedToolJSON,
    Message,
    MockProvider,
    RaiseError,
    TimeoutFailure,
    ToolCall,
    Truncate,
)
from ironcore.providers.openai_compat import OpenAICompatProvider, ProviderError

FIXTURES = Path(__file__).parent / "fixtures"


def complete_one(mock: MockProvider, prompt: str = "go") -> CompletionResult:
    return asyncio.run(mock.complete([Message(role="user", content=prompt)]))


def collect_stream(mock: MockProvider, prompt: str = "go") -> list:
    async def go():
        return [event async for event in mock.stream([Message(role="user", content=prompt)])]

    return asyncio.run(go())


def test_mock_replays_script_and_records_calls():
    mock = MockProvider(
        script=[CompletionResult(message=Message(role="assistant", content="hello world"))]
    )

    async def go():
        return await mock.complete([Message(role="user", content="hi")])

    result = asyncio.run(go())
    assert result.message.content == "hello world"
    assert mock.calls[0][0].content == "hi"


def test_mock_stream_chunks_text_and_emits_tool_calls_then_done():
    call = ToolCall(id="1", name="echo", arguments={"text": "x"})
    mock = MockProvider(
        script=[
            CompletionResult(
                message=Message(role="assistant", content="0123456789abcdef", tool_calls=[call])
            )
        ]
    )

    async def collect():
        return [event async for event in mock.stream([Message(role="user", content="go")])]

    events = asyncio.run(collect())
    text = "".join(e.text for e in events if e.kind == "text")
    assert text == "0123456789abcdef"
    assert len([e for e in events if e.kind == "text"]) > 1  # actually chunked
    assert [e.tool_call.name for e in events if e.kind == "tool_call"] == ["echo"]
    assert events[-1].kind == "done"


def test_mock_exhaustion_is_loud():
    mock = MockProvider()
    with pytest.raises(AssertionError, match="exhausted"):
        asyncio.run(mock.complete([Message(role="user", content="hi")]))


def test_openai_compat_normalizes_base_url():
    # full client behavior is pinned in tests/providers/ (IC-201/IC-202)
    provider = OpenAICompatProvider(base_url="http://localhost:11434/v1/")
    assert provider.base_url == "http://localhost:11434/v1"  # trailing slash stripped


# --- IC-104: failure injection ---------------------------------------------


def test_malformed_tool_json_stream_is_a_repairable_error_event():
    mock = MockProvider(
        script=[MalformedToolJSON(text_prefix="Calling: ", raw_fragment='{"name": "grep", "arg')]
    )
    events = collect_stream(mock)
    assert "".join(e.text for e in events if e.kind == "text") == "Calling: "
    assert events[-1].kind == "error"
    assert events[-1].data == {
        "repairable": True,
        "reason": "malformed_tool_json",
        "raw": '{"name": "grep", "arg',
    }


def test_malformed_tool_json_complete_carries_the_raw_text():
    mock = MockProvider(script=[MalformedToolJSON(text_prefix="hm ", raw_fragment='{"broken": ')])
    result = complete_one(mock)
    assert result.message.content == 'hm {"broken": '
    assert result.message.tool_calls == []  # nothing parsed — repair loop's job


def test_truncate_stream_emits_partial_text_then_repairable_error():
    mock = MockProvider(script=[Truncate(content="0123456789abcdef", after_chars=10)])
    events = collect_stream(mock)
    assert "".join(e.text for e in events if e.kind == "text") == "0123456789"
    assert events[-1].kind == "error"
    assert events[-1].data == {"repairable": True, "reason": "truncated"}


def test_truncate_complete_returns_partial_with_finish_reason_length():
    mock = MockProvider(script=[Truncate(content="0123456789abcdef", after_chars=10)])
    result = complete_one(mock)
    assert result.message.content == "0123456789"
    assert result.finish_reason == "length"


def test_timeout_failure_complete_raises_provider_error():
    mock = MockProvider(script=[TimeoutFailure()])
    with pytest.raises(ProviderError, match="timed out"):
        complete_one(mock)


def test_timeout_failure_stream_still_terminates_with_an_error_event():
    mock = MockProvider(script=[TimeoutFailure(message="read timeout after 30s")])
    events = collect_stream(mock)
    assert [e.kind for e in events] == ["error"]
    assert events[0].data == {
        "repairable": False,
        "reason": "timeout",
        "message": "read timeout after 30s",
    }


def test_raise_error_complete_raises_provider_error_with_message():
    mock = MockProvider(script=[RaiseError(message="server exploded")])
    with pytest.raises(ProviderError, match="server exploded"):
        complete_one(mock)


def test_raise_error_stream_still_terminates_with_an_error_event():
    mock = MockProvider(script=[RaiseError(message="server exploded")])
    events = collect_stream(mock)
    assert [e.kind for e in events] == ["error"]
    assert events[0].data == {
        "repairable": False,
        "reason": "provider_error",
        "message": "server exploded",
    }


def test_failures_interleave_with_normal_results():
    mock = MockProvider(
        script=[
            CompletionResult(message=Message(role="assistant", content="ok before")),
            MalformedToolJSON(raw_fragment="{oops"),
            CompletionResult(message=Message(role="assistant", content="ok after")),
        ]
    )
    assert complete_one(mock).message.content == "ok before"
    events = collect_stream(mock)
    assert events[-1].kind == "error" and events[-1].data["repairable"] is True
    assert complete_one(mock).message.content == "ok after"
    assert len(mock.calls) == 3  # every call recorded, failures included


def test_stream_terminates_with_done_or_error_for_every_script_entry_kind():
    # CONTRACTS.md #2: stream must terminate with "done" or "error", no exceptions
    entries = [
        CompletionResult(message=Message(role="assistant", content="fine")),
        MalformedToolJSON(),
        Truncate(content="partial", after_chars=3),
        TimeoutFailure(),
        RaiseError(message="boom"),
    ]
    for entry in entries:
        events = collect_stream(MockProvider(script=[entry]))
        assert events and events[-1].kind in ("done", "error"), entry


# --- IC-104: JSONL transcript fixtures --------------------------------------


def test_from_fixture_basic_session_replays_three_happy_entries():
    mock = MockProvider.from_fixture(FIXTURES / "basic_session.jsonl")
    assert len(mock.script) == 3

    first = complete_one(mock)
    assert first.message.content == "Let me look at the failing test first."
    assert first.finish_reason == "stop"

    second = complete_one(mock)
    assert [c.name for c in second.message.tool_calls] == ["read_file"]
    assert second.message.tool_calls[0].arguments == {"path": "tests/test_math.py"}
    assert second.message.tool_calls[0].id  # loader assigns a stable non-empty id
    assert second.finish_reason == "tool_calls"

    third = complete_one(mock)
    assert "Fixing add()" in third.message.content


def test_from_fixture_basic_session_streams_tool_call_then_done():
    mock = MockProvider.from_fixture(FIXTURES / "basic_session.jsonl")
    complete_one(mock)  # skip the first text entry
    events = collect_stream(mock)
    assert [e.tool_call.name for e in events if e.kind == "tool_call"] == ["read_file"]
    assert events[-1].kind == "done"
    assert events[-1].data == {"finish_reason": "tool_calls"}


def test_from_fixture_failure_session_mixes_happy_and_failures():
    mock = MockProvider.from_fixture(FIXTURES / "failure_session.jsonl")
    assert len(mock.script) == 5

    happy = collect_stream(mock)
    assert happy[-1].kind == "done"

    malformed = collect_stream(mock)
    assert malformed[-1].kind == "error"
    assert malformed[-1].data["repairable"] is True
    assert malformed[-1].data["raw"] == '{"name": "read_file", "arguments": {"path": '

    truncated = collect_stream(mock)
    assert "".join(e.text for e in truncated if e.kind == "text") == "Here is the "
    assert truncated[-1].data == {"repairable": True, "reason": "truncated"}

    with pytest.raises(ProviderError, match="timed out"):
        complete_one(mock)

    with pytest.raises(ProviderError, match="server exploded"):
        complete_one(mock)


def test_from_fixture_rejects_unknown_entry_type(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type": "telepathy"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown entry type 'telepathy'"):
        MockProvider.from_fixture(bad)


def test_from_fixture_reports_line_number_on_invalid_json(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type": "text", "content": "ok"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r":2: invalid JSON"):
        MockProvider.from_fixture(bad)


def test_from_fixture_tolerates_crlf_and_blank_lines(tmp_path):
    fixture = tmp_path / "crlf.jsonl"
    fixture.write_bytes(b'{"type": "text", "content": "one"}\r\n\r\n{"type": "timeout"}\r\n')
    mock = MockProvider.from_fixture(fixture)
    assert len(mock.script) == 2
    assert complete_one(mock).message.content == "one"
