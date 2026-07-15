"""Regression pins for the phase-1/2 adversarial validation round (2026-07-15).

Each test reproduces one finding from the validator's report and pins its
fix. Numbering follows the report: 1 blocker, 2-3 major, 4-7 minor.
"""

import asyncio
import json
import threading
from datetime import UTC, datetime

import httpx
import pytest

from ironcore.config.settings import ConfigError, Settings
from ironcore.providers import CompletionResult, Message, MockProvider, TimeoutFailure
from ironcore.providers.ollama import OllamaProvider
from ironcore.providers.openai_compat import (
    OpenAICompatProvider,
    ProviderError,
    ProviderTimeout,
)
from ironcore.providers.registry import ProviderRegistry
from ironcore.safety.audit import AuditWriter

TS = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


async def _no_sleep(_delay: float) -> None:
    return None


# --- 1 BLOCKER: non-string tool arguments are repairable data, not a TypeError


def test_finding1_nonstring_tool_arguments_never_raise():
    payload = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "hm ",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "f", "arguments": [1, 2]}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    provider = OpenAICompatProvider("http://t/v1", model="m", transport=transport)

    async def go():
        try:
            return await provider.complete([Message(role="user", content="hi")])
        finally:
            await provider.close()

    result = asyncio.run(go())
    assert result.message.tool_calls == []  # nothing parsed — repair loop's job
    assert "[1, 2]" in result.message.content  # raw shape preserved for the repair loop


# --- 2 MAJOR: concurrent appends to one audit day-file lose nothing (Windows "a"
# mode is seek-then-write, not atomic — the pre-fix probe lost records every trial)


def test_finding2_concurrent_audit_writers_lose_no_records(tmp_path):
    def clock() -> datetime:
        return TS  # both writers share one day file, deterministically

    writers = [AuditWriter(tmp_path, f"sess-{i}", clock=clock) for i in range(2)]
    per_writer = 300

    def spam(writer: AuditWriter, tag: int) -> None:
        for i in range(per_writer):
            writer.tool_call(i, f"tool-{tag}", {"i": i}, "ok")

    threads = [
        threading.Thread(target=spam, args=(writer, tag))
        for tag, writer in enumerate(writers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = writers[0].path_for(TS).read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]  # every line valid JSON...
    assert len(records) == per_writer * len(writers)  # ...and none lost


# --- 3 MAJOR: post-retries timeouts keep reason "timeout" in stream mode,
# matching MockProvider.TimeoutFailure (drop-in contract)


def test_finding3_stream_connect_timeout_reason_is_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("connect timed out")

    provider = OpenAICompatProvider(
        "http://t/v1", model="m", transport=httpx.MockTransport(handler), sleep=_no_sleep
    )

    async def go():
        try:
            return [e async for e in provider.stream([Message(role="user", content="hi")])]
        finally:
            await provider.close()

    events = asyncio.run(go())
    assert [e.kind for e in events] == ["error"]
    assert events[0].data["reason"] == "timeout"
    assert events[0].data["repairable"] is False


def test_finding3_mock_timeout_raises_the_same_subclass():
    mock = MockProvider(script=[TimeoutFailure()])
    with pytest.raises(ProviderTimeout):
        asyncio.run(mock.complete([Message(role="user", content="hi")]))
    assert issubclass(ProviderTimeout, ProviderError)  # consumers may catch either


# --- 4 MINOR: a closed registry refuses to hand out providers (cached or default)


def test_finding4_closed_registry_refuses_cached_and_default():
    settings = Settings()
    settings.roles.planner = "planner-model"
    registry = ProviderRegistry(settings)
    registry.for_role("planner")  # cache it while open

    asyncio.run(registry.close_all())
    with pytest.raises(RuntimeError, match="closed"):
        registry.for_role("planner")
    with pytest.raises(RuntimeError, match="closed"):
        _ = registry.default


# --- 5 MINOR: OllamaProvider completes a bare root URL to the /v1 dialect
# (before: discovery worked, every chat call 404ed)


def test_finding5_ollama_bare_root_url_reaches_v1_chat():
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = OllamaProvider(
        "http://localhost:11434", model="m", transport=httpx.MockTransport(handler)
    )
    assert provider.base_url == "http://localhost:11434/v1"
    assert provider.api_root == "http://localhost:11434"

    async def go():
        try:
            return await provider.complete([Message(role="user", content="hi")])
        finally:
            await provider.close()

    result = asyncio.run(go())
    assert result.message.content == "ok"
    assert seen == ["/v1/chat/completions"]


# --- 6 MINOR: every bad config value surfaces as ConfigError, never a raw traceback


def test_finding6_wrong_typed_value_raises_configerror(tmp_path):
    user = tmp_path / "user.toml"
    user.write_text("[provider]\nbase_url = 123\n")
    with pytest.raises(ConfigError, match="provider.base_url"):
        Settings.load(project_dir=tmp_path, user_config=user, env={})


def test_finding6_env_override_on_garbage_section_raises_configerror(tmp_path):
    user = tmp_path / "user.toml"
    user.write_text('provider = "just-a-string"\n')
    with pytest.raises(ConfigError):
        Settings.load(project_dir=tmp_path, user_config=user, env={"IRONCORE_MODEL": "m"})


# --- 7 MINOR: MockProvider streams usage events (budget tracking, IC-506,
# must be exercisable offline)


def test_finding7_mock_stream_emits_usage_for_budget_tracking():
    mock = MockProvider(
        script=[
            CompletionResult(
                message=Message(role="assistant", content="hi"),
                usage={"total_tokens": 7},
            )
        ]
    )

    async def go():
        return [e async for e in mock.stream([Message(role="user", content="x")])]

    events = asyncio.run(go())
    usage_events = [e for e in events if e.kind == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0].data == {"total_tokens": 7}
    assert events[-1].kind == "done"
