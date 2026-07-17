"""IC-201 pins: request shape, SSE happy path, retries/backoff/Retry-After,
timeout -> ProviderError, api-key redaction, list_models.

Everything runs against httpx.MockTransport; the injected sleep records
backoff delays so retry tests are instant. Async pattern: asyncio.run
(pytest-asyncio is not a dependency of this repo).
"""

import asyncio
import json

import httpx
import pytest

from ironcore.providers.base import ImageData, Message, SamplingPolicy, StreamEvent, ToolCall
from ironcore.providers.openai_compat import OpenAICompatProvider, ProviderError, _wire_messages

BASE = "http://testserver/v1"


def make_provider(handler, *, api_key="sk-unit-test", **kwargs):
    """Provider wired to a MockTransport, plus the list its fake sleep fills."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    provider = OpenAICompatProvider(
        BASE + "/",  # trailing slash on purpose: the constructor must normalize
        api_key=api_key,
        model="test-model",
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
        **kwargs,
    )
    return provider, sleeps


def complete_one(provider, messages=None, **kwargs):
    async def go():
        try:
            return await provider.complete(
                messages or [Message(role="user", content="hi")], **kwargs
            )
        finally:
            await provider.close()

    return asyncio.run(go())


def collect_stream(provider, messages=None, **kwargs):
    async def go():
        try:
            events = []
            async for event in provider.stream(
                messages or [Message(role="user", content="hi")], **kwargs
            ):
                events.append(event)
            return events
        finally:
            await provider.close()

    return asyncio.run(go())


def completion_json(content="hello"):
    return {
        "id": "cmpl-1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


def sse_bytes(*chunks, done=True):
    parts = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


def text_chunk(text):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def finish_chunk(reason):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


# --- happy paths -------------------------------------------------------------


def test_complete_happy_path_parses_choice_and_usage():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json("hi there"))

    provider, _ = make_provider(handler)
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    result = complete_one(provider, tools=tools)

    assert result.message.role == "assistant"
    assert result.message.content == "hi there"
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert seen["url"] == BASE + "/chat/completions"
    assert seen["auth"] == "Bearer sk-unit-test"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["tools"] == tools  # tools pass through untouched
    assert "stream" not in seen["body"]


def test_sampling_policy_maps_to_temperature_top_p_max_tokens():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json())

    provider, _ = make_provider(handler)
    complete_one(provider, sampling=SamplingPolicy(temperature=0.7, top_p=0.5, max_tokens=128))
    assert seen["body"]["temperature"] == 0.7
    assert seen["body"]["top_p"] == 0.5
    assert seen["body"]["max_tokens"] == 128
    assert "tools" not in seen["body"]  # omitted when not supplied


def test_assistant_tool_calls_and_tool_results_serialize_per_openai_schema():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json())

    provider, _ = make_provider(handler)
    messages = [
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})],
        ),
        Message(role="tool", content="ok", tool_call_id="call_1", name="read_file"),
    ]
    complete_one(provider, messages=messages)

    wire = seen["body"]["messages"]
    assert wire[0] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
            }
        ],
    }
    assert wire[1] == {
        "role": "tool",
        "content": "ok",
        "tool_call_id": "call_1",
        "name": "read_file",
    }


def test_complete_parses_tool_calls_from_response():
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,  # null content normalizes to ""
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "grep", "arguments": '{"pattern": "x"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    provider, _ = make_provider(lambda request: httpx.Response(200, json=payload))
    result = complete_one(provider)
    assert result.message.content == ""
    assert result.message.tool_calls == [
        ToolCall(id="call_9", name="grep", arguments={"pattern": "x"})
    ]
    assert result.finish_reason == "tool_calls"
    assert result.usage == {}  # absent usage -> empty dict


def test_stream_happy_path_yields_text_then_done():
    def handler(request):
        body = json.loads(request.content.decode())
        assert body["stream"] is True
        return httpx.Response(
            200,
            content=sse_bytes(text_chunk("Hel"), text_chunk("lo"), finish_chunk("stop")),
            headers={"content-type": "text/event-stream"},
        )

    provider, _ = make_provider(handler)
    events = collect_stream(provider)
    assert [e.kind for e in events] == ["text", "text", "done"]
    assert "".join(e.text for e in events if e.kind == "text") == "Hello"
    assert events[-1] == StreamEvent(kind="done", data={"finish_reason": "stop"})


def test_list_models_returns_ids():
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == BASE + "/models"
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": "llama3:8b"}, {"id": "qwen2.5-coder"}]}
        )

    provider, _ = make_provider(handler)

    async def go():
        try:
            return await provider.list_models()
        finally:
            await provider.close()

    assert asyncio.run(go()) == ["llama3:8b", "qwen2.5-coder"]


# --- retries -----------------------------------------------------------------


def test_429_then_success_retries_and_honors_retry_after():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow down"})
        return httpx.Response(200, json=completion_json("recovered"))

    provider, sleeps = make_provider(handler)
    result = complete_one(provider)
    assert result.message.content == "recovered"
    assert len(attempts) == 2
    assert sleeps == [3.0]  # server's Retry-After wins over computed backoff


def test_500_then_success_retries_with_backoff_jitter():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json=completion_json("recovered"))

    provider, sleeps = make_provider(handler)
    assert complete_one(provider).message.content == "recovered"
    assert len(attempts) == 2
    assert len(sleeps) == 1
    assert 0.5 <= sleeps[0] <= 0.75  # base 0.5 * 2**0 plus jitter in [0, 0.25]


def test_transport_error_then_success_retries():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=completion_json("recovered"))

    provider, _ = make_provider(handler)
    assert complete_one(provider).message.content == "recovered"
    assert len(attempts) == 2


def test_timeout_exhausts_retries_then_raises_provider_error():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ReadTimeout("read timed out")

    provider, sleeps = make_provider(handler)
    with pytest.raises(ProviderError, match="ReadTimeout"):
        complete_one(provider)
    assert len(attempts) == 3  # default SamplingPolicy.retries=2 -> 3 attempts
    assert len(sleeps) == 2
    for i, delay in enumerate(sleeps):
        assert 0.5 * 2**i <= delay <= 0.5 * 2**i + 0.25  # exponential + jitter


def test_retry_count_comes_from_sampling_policy():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ReadTimeout("read timed out")

    provider, sleeps = make_provider(handler)
    with pytest.raises(ProviderError):
        complete_one(provider, sampling=SamplingPolicy(retries=4))
    assert len(attempts) == 5
    assert len(sleeps) == 4


def test_client_error_400_does_not_retry():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    provider, sleeps = make_provider(handler)
    with pytest.raises(ProviderError, match="HTTP 400"):
        complete_one(provider)
    assert len(attempts) == 1
    assert sleeps == []


def test_stream_connect_failure_is_a_terminal_error_event_not_an_exception():
    # the stream-event-vs-raise split: complete() raises, stream() emits
    def handler(request):
        raise httpx.ConnectError("connection refused")

    provider, sleeps = make_provider(handler)
    events = collect_stream(provider)
    assert [e.kind for e in events] == ["error"]
    assert events[0].data["repairable"] is False
    assert events[0].data["reason"] == "provider_error"
    assert "connection refused" in events[0].data["message"]
    assert len(sleeps) == 2  # connect retries still ran before giving up


def test_non_json_success_body_raises_provider_error():
    provider, _ = make_provider(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderError, match="malformed JSON"):
        complete_one(provider)


# --- guided decoding: response_format + extra_body ---------------------------

_RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "t", "schema": {}}}


def _capture_complete_body(**kwargs):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json())

    provider, _ = make_provider(handler)
    complete_one(provider, **kwargs)
    return seen["body"]


def _capture_stream_body(**kwargs):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            content=sse_bytes(text_chunk("hi"), finish_chunk("stop")),
            headers={"content-type": "text/event-stream"},
        )

    provider, _ = make_provider(handler)
    collect_stream(provider, **kwargs)
    return seen["body"]


def test_complete_threads_response_format_into_body():
    body = _capture_complete_body(response_format=_RESPONSE_FORMAT)
    assert body["response_format"] == _RESPONSE_FORMAT
    assert "guided_json" not in body


def test_complete_threads_extra_body_into_body():
    body = _capture_complete_body(extra_body={"guided_json": {"type": "object"}})
    assert body["guided_json"] == {"type": "object"}
    assert "response_format" not in body


def test_complete_omits_guided_keys_when_neither_supplied():
    body = _capture_complete_body()
    assert "response_format" not in body  # no accidental default
    assert "guided_json" not in body


def test_stream_threads_response_format_and_extra_body_into_body():
    body = _capture_stream_body(
        response_format=_RESPONSE_FORMAT, extra_body={"guided_json": {"x": 1}}
    )
    assert body["response_format"] == _RESPONSE_FORMAT
    assert body["guided_json"] == {"x": 1}
    assert body["stream"] is True  # guided knobs don't disturb the stream flag


def test_stream_omits_guided_keys_when_neither_supplied():
    body = _capture_stream_body()
    assert "response_format" not in body
    assert "guided_json" not in body


def test_extra_body_wins_a_clash_with_a_same_named_body_key():
    # extra_body is applied last, so it overrides both a same-named response_format
    # and any base body key (here max_tokens) — the documented clash rule.
    body = _capture_complete_body(
        response_format={"type": "json_object"},
        extra_body={"response_format": {"type": "json_schema"}, "max_tokens": 7},
    )
    assert body["response_format"] == {"type": "json_schema"}
    assert body["max_tokens"] == 7


# --- image wire shape (MS-6) --------------------------------------------------


def test_images_serialize_as_content_parts_with_data_uri():
    parts = _wire_messages(
        [
            Message(
                role="user",
                content="what is this?",
                images=[ImageData(base64="QUJD", media_type="image/jpeg")],
            )
        ]
    )
    assert parts == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,QUJD"},
                },
            ],
        }
    ]


def test_image_with_empty_content_emits_no_text_part():
    (entry,) = _wire_messages([Message(role="user", images=[ImageData(base64="QUJD")])])
    assert entry["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    ]


def test_messages_without_images_keep_the_exact_string_content_shape():
    # byte-identical regression: the images feature must not disturb text turns
    (entry,) = _wire_messages([Message(role="user", content="hi")])
    assert entry == {"role": "user", "content": "hi"}


def test_complete_carries_image_parts_in_the_request_body():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json())

    provider, _ = make_provider(handler)
    complete_one(
        provider,
        messages=[
            Message(role="user", content="look", images=[ImageData(base64="QUJD")])
        ],
    )
    content = seen["body"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


# --- redaction ---------------------------------------------------------------

WEIRD_KEY = "sk-SUPER$ecret.key+123"


def test_api_key_never_appears_in_http_error_messages():
    def handler(request):
        # a hostile/echoing server reflects the auth header into the error body
        return httpx.Response(400, text=f"invalid key: {request.headers['authorization']}")

    provider, _ = make_provider(handler, api_key=WEIRD_KEY)
    with pytest.raises(ProviderError) as excinfo:
        complete_one(provider)
    assert WEIRD_KEY not in str(excinfo.value)
    assert "invalid key" in str(excinfo.value)  # the useful part survives redaction


def test_api_key_never_appears_in_transport_error_messages():
    def handler(request):
        raise httpx.ConnectError(f"proxy rejected header {request.headers['authorization']}")

    provider, _ = make_provider(handler, api_key=WEIRD_KEY)
    with pytest.raises(ProviderError) as excinfo:
        complete_one(provider)
    assert WEIRD_KEY not in str(excinfo.value)


def test_api_key_never_appears_in_stream_error_events():
    def handler(request):
        raise httpx.ConnectError(f"proxy rejected header {request.headers['authorization']}")

    provider, _ = make_provider(handler, api_key=WEIRD_KEY)
    events = collect_stream(provider)
    assert events[-1].kind == "error"
    assert WEIRD_KEY not in events[-1].data["message"]
