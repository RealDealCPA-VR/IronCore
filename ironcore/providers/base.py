"""Provider contract and wire types.

CONTRACT (docs/CONTRACTS.md #Provider): these signatures are frozen.
The turn engine, envelope probes, and workflows all program against
Provider — never against a concrete client.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """A parsed tool invocation, regardless of the wire protocol it used
    (native function-calling, strict JSON, or the IRONCALL text protocol)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: set on role="tool" messages: which call this result answers
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class SamplingPolicy:
    """Per-call sampling knobs. Defaults come from the model's capability
    profile (envelope), not from here — these are neutral fallbacks."""

    temperature: float = 0.2
    top_p: float = 0.95
    max_tokens: int = 4096
    #: retries on transport errors / malformed output before surfacing failure
    retries: int = 2


@dataclass
class StreamEvent:
    """One increment of a streamed completion."""

    kind: Literal["text", "tool_call", "usage", "done", "error"]
    text: str = ""
    tool_call: ToolCall | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResult:
    message: Message
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"


class Provider(ABC):
    """A chat-completion backend.

    Implementations must be safe to call concurrently and must translate
    transport failures into ProviderError (IC-201) rather than leaking
    httpx exceptions upward.
    """

    name: str = "abstract"

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> CompletionResult:
        """Non-streaming completion."""

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming completion. Must yield a terminal 'done' or 'error' event."""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Model ids available at this endpoint."""

    async def close(self) -> None:  # noqa: B027 — optional hook, default no-op
        """Release transport resources. Default: nothing to do."""
