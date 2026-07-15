"""MockProvider: scripted completions for offline tests and demos.

Every subsystem must be testable with zero network and zero model
(SPEC.md #14). MockProvider replays a queue of CompletionResults; stream()
re-emits the same content as character chunks so streaming consumers get
exercised too. IC-104 extends this with transcript fixtures and
failure-injection (malformed tool calls, truncation, timeouts).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ironcore.providers.base import (
    CompletionResult,
    Message,
    Provider,
    SamplingPolicy,
    StreamEvent,
)


class MockProvider(Provider):
    name = "mock"

    def __init__(self, script: list[CompletionResult] | None = None) -> None:
        self.script: list[CompletionResult] = list(script or [])
        self.calls: list[list[Message]] = []  # what the engine sent, for assertions

    def push(self, result: CompletionResult) -> None:
        self.script.append(result)

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> CompletionResult:
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("MockProvider script exhausted")
        return self.script.pop(0)

    async def stream(  # type: ignore[override]
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> AsyncIterator[StreamEvent]:
        result = await self.complete(messages, tools=tools, sampling=sampling)
        text = result.message.content
        # chunk in small pieces to exercise incremental rendering
        for i in range(0, len(text), 8):
            yield StreamEvent(kind="text", text=text[i : i + 8])
        for call in result.message.tool_calls:
            yield StreamEvent(kind="tool_call", tool_call=call)
        yield StreamEvent(kind="done", data={"finish_reason": result.finish_reason})

    async def list_models(self) -> list[str]:
        return ["mock-model"]
