"""IC-202 pins: native tool-call parsing across SSE chunk fragments.

Fragmented arguments reassemble; parallel calls emit by index; garbage
arguments become a repairable error event (MockProvider's exact shape);
usage chunks pass through; CRLF/comment framing is tolerated; a mid-stream
transport failure is a terminal non-repairable error event, never a raise.
"""

import asyncio
import json

import httpx

from ironcore.providers.base import Message, StreamEvent, ToolCall
from ironcore.providers.openai_compat import OpenAICompatProvider

BASE = "http://testserver/v1"


def collect(handler):
    provider = OpenAICompatProvider(
        BASE,
        api_key="sk-unit-test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    async def go():
        try:
            events = []
            async for event in provider.stream([Message(role="user", content="go")]):
                events.append(event)
            return events
        finally:
            await provider.close()

    return asyncio.run(go())


def sse_handler(raw: bytes):
    def handler(request):
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    return handler


def sse_bytes(*chunks, done=True):
    parts = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


def tool_fragment(index, *, call_id=None, name=None, args=None):
    call = {"index": index}
    if call_id is not None:
        call["id"] = call_id
        call["type"] = "function"
    function = {}
    if name is not None:
        function["name"] = name
    if args is not None:
        function["arguments"] = args
    if function:
        call["function"] = function
    return {"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]}


def text_chunk(text):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def finish_chunk(reason="tool_calls"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def test_arguments_fragmented_across_chunks_reassemble_into_one_call():
    raw = sse_bytes(
        tool_fragment(0, call_id="call_abc", name="read_file", args=""),
        tool_fragment(0, args='{"pa'),
        tool_fragment(0, args='th": "src/'),
        tool_fragment(0, args='main.py"}'),
        finish_chunk("tool_calls"),
    )
    events = collect(sse_handler(raw))
    calls = [e for e in events if e.kind == "tool_call"]
    assert len(calls) == 1  # exactly one event per call, only once JSON is complete
    assert calls[0].tool_call == ToolCall(
        id="call_abc", name="read_file", arguments={"path": "src/main.py"}
    )
    assert events[-1] == StreamEvent(kind="done", data={"finish_reason": "tool_calls"})


def test_two_parallel_tool_calls_interleaved_both_emit_in_index_order():
    raw = sse_bytes(
        tool_fragment(0, call_id="call_0", name="grep", args='{"pattern":'),
        tool_fragment(1, call_id="call_1", name="read_file", args='{"path": "a.py"'),
        tool_fragment(1, args="}"),
        tool_fragment(0, args=' "TODO"}'),
        finish_chunk("tool_calls"),
    )
    events = collect(sse_handler(raw))
    calls = [e.tool_call for e in events if e.kind == "tool_call"]
    assert calls == [
        ToolCall(id="call_0", name="grep", arguments={"pattern": "TODO"}),
        ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"}),
    ]
    assert events[-1].kind == "done"


def test_garbage_arguments_become_a_repairable_error_event_not_an_exception():
    raw = sse_bytes(
        tool_fragment(0, call_id="call_x", name="broken", args='{"path": not-json'),
        finish_chunk("tool_calls"),
    )
    events = collect(sse_handler(raw))  # collecting must not raise
    assert events[-1].kind == "error"
    assert events[-1].data == {
        "repairable": True,
        "reason": "malformed_tool_json",
        "raw": '{"path": not-json',
    }
    assert not any(e.kind == "tool_call" for e in events)
    assert not any(e.kind == "done" for e in events)  # the error event is terminal


def test_parseable_sibling_still_emits_before_the_malformed_one_terminates():
    raw = sse_bytes(
        tool_fragment(0, call_id="call_ok", name="list_dir", args='{"path": "."}'),
        tool_fragment(1, call_id="call_bad", name="broken", args="{{{"),
        finish_chunk("tool_calls"),
    )
    events = collect(sse_handler(raw))
    assert [e.kind for e in events] == ["tool_call", "error"]
    assert events[0].tool_call.id == "call_ok"
    assert events[1].data["repairable"] is True
    assert events[1].data["raw"] == "{{{"


def test_arguments_that_parse_to_non_object_json_are_repairable_too():
    raw = sse_bytes(
        tool_fragment(0, call_id="call_1", name="odd", args='"just a string"'),
        finish_chunk("tool_calls"),
    )
    events = collect(sse_handler(raw))
    assert events[-1].kind == "error"
    assert events[-1].data["reason"] == "malformed_tool_json"
    assert events[-1].data["raw"] == '"just a string"'


def test_text_and_tool_calls_mix_in_one_stream():
    raw = sse_bytes(
        text_chunk("Let me "),
        text_chunk("check."),
        tool_fragment(0, call_id="call_1", name="list_dir", args='{"path": "."}'),
        finish_chunk("tool_calls"),
    )
    events = collect(sse_handler(raw))
    assert [e.kind for e in events] == ["text", "text", "tool_call", "done"]
    assert "".join(e.text for e in events[:2]) == "Let me check."


def test_call_with_no_arguments_emits_an_empty_dict():
    raw = sse_bytes(
        tool_fragment(0, call_id="call_1", name="list_models"),
        finish_chunk("tool_calls"),
    )
    events = collect(sse_handler(raw))
    calls = [e.tool_call for e in events if e.kind == "tool_call"]
    assert calls == [ToolCall(id="call_1", name="list_models", arguments={})]


def test_usage_chunk_becomes_a_usage_event():
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    raw = sse_bytes(text_chunk("hi"), finish_chunk("stop"), {"choices": [], "usage": usage})
    events = collect(sse_handler(raw))
    assert StreamEvent(kind="usage", data=usage) in events
    assert events[-1] == StreamEvent(kind="done", data={"finish_reason": "stop"})


def test_sse_tolerates_crlf_comments_and_blank_lines():
    body = sse_bytes(text_chunk("hi"), finish_chunk("stop")).replace(b"\n", b"\r\n")
    raw = b": keep-alive\r\n\r\n" + body
    events = collect(sse_handler(raw))
    assert [e.kind for e in events] == ["text", "done"]
    assert events[0].text == "hi"


def test_stream_without_done_marker_still_flushes_and_terminates():
    # robustness: some servers close the connection without a [DONE] line
    raw = sse_bytes(
        tool_fragment(0, call_id="call_1", name="noop", args="{}"),
        finish_chunk("tool_calls"),
        done=False,
    )
    events = collect(sse_handler(raw))
    assert [e.kind for e in events] == ["tool_call", "done"]


def test_midstream_transport_failure_is_a_terminal_error_event():
    # retries cover the initial connection only: a half-consumed stream
    # cannot be replayed, so the failure surfaces as the terminal event
    async def exploding_body():
        yield sse_bytes(text_chunk("par"), done=False)
        raise httpx.ReadError("connection reset by peer")

    def handler(request):
        return httpx.Response(
            200, content=exploding_body(), headers={"content-type": "text/event-stream"}
        )

    events = collect(handler)  # must not raise
    assert [e.kind for e in events] == ["text", "error"]
    assert events[-1].data["repairable"] is False
    assert events[-1].data["reason"] == "provider_error"
    assert "connection reset" in events[-1].data["message"]
