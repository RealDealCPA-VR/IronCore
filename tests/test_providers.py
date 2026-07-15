"""Provider wire types and the MockProvider used by all offline tests."""

import asyncio

import pytest

from ironcore.providers import CompletionResult, Message, MockProvider, ToolCall
from ironcore.providers.openai_compat import OpenAICompatProvider


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


def test_openai_compat_is_an_honest_stub():
    provider = OpenAICompatProvider(base_url="http://localhost:11434/v1/")
    assert provider.base_url == "http://localhost:11434/v1"  # trailing slash stripped
    with pytest.raises(NotImplementedError, match="IC-201"):
        asyncio.run(provider.complete([]))
