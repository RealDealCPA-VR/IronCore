"""OpenAI-compatible provider — TODO IC-201/IC-202.

One client for every local and hosted server that speaks the
/v1/chat/completions dialect: Ollama (:11434/v1), vLLM, llama.cpp server,
LM Studio, OpenRouter, Together, Groq.

Implementation contract (do not deviate — tests in
tests/providers/test_openai_compat.py will pin this):

* httpx.AsyncClient with connect/read timeouts from settings; retries with
  exponential backoff + jitter on 429/5xx/transport errors, honoring
  Retry-After when present.
* stream() parses SSE `data:` lines; accumulates tool_call fragments by
  index until complete JSON parses; yields StreamEvent(kind="tool_call")
  only for COMPLETE calls; always terminates with "done" or "error".
* Malformed tool-call JSON is NOT an exception: surface it as
  StreamEvent(kind="error", data={"repairable": True, ...}) so the turn
  engine can run the repair loop (IC-503).
* Never log or echo the api_key. Redact it in exception messages.
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


class ProviderError(RuntimeError):
    """Transport or protocol failure, post-retries. Message must be safe to
    show the user (no secrets, no full request bodies)."""


class OpenAICompatProvider(Provider):
    """See module docstring. Ships in IC-201."""

    name = "openai-compat"

    def __init__(self, base_url: str, api_key: str = "ironcore-local", model: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> CompletionResult:
        raise NotImplementedError("IC-201: OpenAI-compatible provider (see TODO.md)")

    async def stream(  # type: ignore[override]
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("IC-201: OpenAI-compatible provider (see TODO.md)")
        yield  # pragma: no cover — marks this as an async generator

    async def list_models(self) -> list[str]:
        raise NotImplementedError("IC-201: OpenAI-compatible provider (see TODO.md)")
