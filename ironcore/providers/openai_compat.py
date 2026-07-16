"""OpenAI-compatible provider (IC-201/IC-202).

One client for every local and hosted server that speaks the
/v1/chat/completions dialect: Ollama (:11434/v1), vLLM, llama.cpp server,
LM Studio, OpenRouter, Together, Groq.

Behavior (pinned by tests/providers/):

* One httpx.AsyncClient per instance; connect/read timeouts are constructor
  keywords, and ``transport=`` / ``sleep=`` are injection seams so tests run
  against httpx.MockTransport without real backoff waits.
* 429/5xx responses and httpx transport errors retry with exponential
  backoff + jitter (SamplingPolicy.retries times, default 2), honoring a
  seconds-form Retry-After header (capped) when present. Other 4xx never retry.
* stream() parses SSE ``data:`` lines until ``data: [DONE]``, tolerating
  comments, blank lines, and CRLF framing. Text deltas and usage chunks yield
  as they arrive; tool_calls deltas accumulate by index (id/name from the
  first fragment, arguments string concatenated) and each call yields exactly
  one StreamEvent(kind="tool_call") at stream end, once its accumulated
  arguments parse as complete JSON. Streams always terminate with a "done"
  or "error" event (CONTRACTS.md #2).
* Malformed tool-call JSON is NOT an exception: stream() surfaces it as
  StreamEvent(kind="error", data={"repairable": True, ...}) so the turn
  engine can run the repair loop (IC-503); complete() mirrors MockProvider
  and hands the raw fragment back in message.content.
* Transport failure post-retries: complete() raises ProviderError; stream()
  yields a terminal non-repairable "error" event instead — a half-consumed
  stream cannot be replayed, so retries cover the initial connection only.
* Never log or echo the api_key. Redact it in exception messages and error
  events.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import httpx

from ironcore.providers.base import (
    CompletionResult,
    Message,
    Provider,
    SamplingPolicy,
    StreamEvent,
    ToolCall,
)

#: retry-worthy statuses: rate limiting and transient server failures
_RETRY_STATUSES = frozenset({429, *range(500, 600)})
#: cap a server-supplied Retry-After so a buggy header cannot stall a session
_RETRY_AFTER_CAP = 30.0
#: httpx failures we translate into ProviderError / error events
_TRANSPORT_ERRORS = (httpx.HTTPError, httpx.InvalidURL)


class ProviderError(RuntimeError):
    """Transport or protocol failure, post-retries. Message must be safe to
    show the user (no secrets, no full request bodies)."""


class ProviderTimeout(ProviderError):
    """The failure that exhausted retries was a transport timeout. Distinct
    so stream() can map it to reason "timeout" — keeping the real provider
    and MockProvider.TimeoutFailure drop-in identical (CONTRACTS #2)."""


def _wire_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Message list -> OpenAI chat schema. tool_calls carry their arguments
    as a JSON *string* on the wire; tool results carry tool_call_id/name."""
    wire: list[dict[str, Any]] = []
    for msg in messages:
        entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in msg.tool_calls
            ]
        if msg.tool_call_id is not None:
            entry["tool_call_id"] = msg.tool_call_id
        if msg.name is not None:
            entry["name"] = msg.name
        wire.append(entry)
    return wire


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], bool]:
    """A tool call's wire-form arguments -> (parsed, ok).

    OpenAI encodes arguments as a JSON string; empty/absent means "no
    arguments". ok=False when the string is not one complete JSON object —
    callers turn that into repairable data, never an exception.
    """
    if raw is None:
        return {}, True
    if isinstance(raw, dict):  # some servers skip the string encoding
        return raw, True
    if isinstance(raw, str) and not raw.strip():
        return {}, True
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}, False
    if not isinstance(parsed, dict):
        return {}, False
    return parsed, True


def _backoff_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retry `attempt` (0-based). A parseable
    Retry-After wins (capped); otherwise exponential backoff + jitter."""
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), _RETRY_AFTER_CAP)
        except ValueError:
            pass  # HTTP-date form: fall through to computed backoff
    return 0.5 * (2**attempt) + random.uniform(0.0, 0.25)


def _accumulate(drafts: dict[int, dict[str, str]], fragment: Any) -> None:
    """Fold one tool_calls delta into the per-index draft. id and name arrive
    on a call's first fragment; the arguments string accumulates across chunks."""
    if not isinstance(fragment, dict):
        return
    index = fragment.get("index", 0)
    if not isinstance(index, int):
        return
    draft = drafts.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if isinstance(fragment.get("id"), str) and fragment["id"]:
        draft["id"] = fragment["id"]
    function = fragment.get("function")
    if not isinstance(function, dict):
        return
    if isinstance(function.get("name"), str) and function["name"]:
        draft["name"] = function["name"]
    if isinstance(function.get("arguments"), str):
        draft["arguments"] += function["arguments"]


def _flush_tool_calls(drafts: dict[int, dict[str, str]]) -> Iterator[StreamEvent]:
    """Stream end: each draft becomes one tool_call event, in index order —
    unless its arguments never became complete JSON, in which case the
    repairable error event (MockProvider's exact shape) terminates the stream."""
    for index in sorted(drafts):
        draft = drafts[index]
        arguments, ok = _parse_arguments(draft["arguments"])
        if not ok:
            yield StreamEvent(
                kind="error",
                data={
                    "repairable": True,
                    "reason": "malformed_tool_json",
                    "raw": draft["arguments"],
                },
            )
            return
        yield StreamEvent(
            kind="tool_call",
            tool_call=ToolCall(
                id=draft["id"] or f"call_{index}",
                name=draft["name"],
                arguments=arguments,
            ),
        )


class OpenAICompatProvider(Provider):
    """See module docstring."""

    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str = "ironcore-local",
        model: str = "",
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    # ------------------------------------------------------------ transport

    def _redact(self, text: str) -> str:
        """The never-log-the-api-key contract, applied to every outbound message."""
        if self.api_key:
            text = text.replace(self.api_key, "[redacted]")
        return text

    def _describe(self, exc: Exception) -> str:
        return self._redact(f"{type(exc).__name__}: {exc}")

    async def _send_with_retries(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        retries: int,
        stream: bool = False,
    ) -> httpx.Response:
        """Send with backoff retries on 429/5xx/transport errors; raise
        ProviderError (redacted) post-retries or on a non-retryable 4xx.
        stream=True returns the response un-read — the caller must aclose()."""
        attempts = max(retries, 0) + 1
        failure = "no attempts made"
        timed_out = False
        for attempt in range(attempts):
            retry_after: str | None = None
            try:
                request = self._client.build_request(method, self.base_url + path, json=json_body)
                response = await self._client.send(request, stream=stream)
            except _TRANSPORT_ERRORS as exc:
                failure = self._describe(exc)
                timed_out = isinstance(exc, httpx.TimeoutException)
            else:
                if response.status_code < 400:
                    return response
                body = (await response.aread()).decode("utf-8", "replace")
                await response.aclose()
                snippet = self._redact(" ".join(body.split())[:200])
                failure = f"HTTP {response.status_code} from {path}: {snippet}"
                timed_out = False
                if response.status_code not in _RETRY_STATUSES:
                    raise ProviderError(failure)
                retry_after = response.headers.get("Retry-After")
            if attempt + 1 < attempts:
                await self._sleep(_backoff_delay(attempt, retry_after))
        error_type = ProviderTimeout if timed_out else ProviderError
        raise error_type(f"request failed after {attempts} attempt(s): {failure}")

    # -------------------------------------------------------------- parsing

    def _request_body(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        sampling: SamplingPolicy,
        *,
        stream: bool,
        response_format: dict | None = None,
        extra_body: dict | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _wire_messages(messages),
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "max_tokens": sampling.max_tokens,
        }
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
        # Guided-decoding knobs (IC guided decoding): response_format is the
        # portable OpenAI form; extra_body carries server-specific keys
        # (vLLM guided_json/guided_grammar, llama.cpp grammar) and is applied
        # last, so it wins any key clash.
        if response_format is not None:
            body["response_format"] = response_format
        if extra_body:
            body.update(extra_body)
        return body

    def _parse_completion(self, payload: Any) -> CompletionResult:
        try:
            choice = payload["choices"][0]
            raw_message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("response missing choices[0].message") from exc
        content = raw_message.get("content") or ""
        tool_calls: list[ToolCall] = []
        for position, raw_call in enumerate(raw_message.get("tool_calls") or []):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments, ok = _parse_arguments(function.get("arguments"))
            if not ok:
                # repairable data, not an exception: mirror MockProvider.complete()
                # — the raw fragment rides back in content for the repair loop.
                # Non-string wire shapes (list/number) are re-serialized: content
                # concatenation must never raise (CONTRACTS #2).
                raw = function.get("arguments")
                content += raw if isinstance(raw, str) else json.dumps(raw)
                continue
            tool_calls.append(
                ToolCall(
                    id=raw_call.get("id") or f"call_{position}",
                    name=function.get("name") or "",
                    arguments=arguments,
                )
            )
        usage = payload.get("usage")
        return CompletionResult(
            message=Message(role="assistant", content=content, tool_calls=tool_calls),
            usage=usage if isinstance(usage, dict) else {},
            finish_reason=choice.get("finish_reason") or "stop",
        )

    async def _parse_sse(self, response: httpx.Response) -> AsyncIterator[StreamEvent]:
        """Open SSE response -> StreamEvents; always ends with 'done' or 'error'."""
        drafts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        try:
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()  # tolerate CRLF framing
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue  # blank separators, comments, and SSE fields we don't use
                data = line[5:].strip()  # len("data:") == 5
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue  # one corrupt server line must not kill the stream
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                    yield StreamEvent(kind="usage", data=chunk["usage"])
                choices = chunk.get("choices") or []
                choice = choices[0] if isinstance(choices, list) and choices else None
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                if isinstance(delta.get("content"), str) and delta["content"]:
                    yield StreamEvent(kind="text", text=delta["content"])
                for fragment in delta.get("tool_calls") or []:
                    _accumulate(drafts, fragment)
        except _TRANSPORT_ERRORS as exc:
            # a half-consumed stream cannot be retried: surface it and terminate
            reason = "timeout" if isinstance(exc, httpx.TimeoutException) else "provider_error"
            yield StreamEvent(
                kind="error",
                data={"repairable": False, "reason": reason, "message": self._describe(exc)},
            )
            return
        malformed = False
        for event in _flush_tool_calls(drafts):
            malformed = event.kind == "error"
            yield event
        if not malformed:
            yield StreamEvent(kind="done", data={"finish_reason": finish_reason or "stop"})

    # ------------------------------------------------------------ Provider

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
        response_format: dict | None = None,
        extra_body: dict | None = None,
    ) -> CompletionResult:
        sampling = sampling or SamplingPolicy()
        body = self._request_body(
            messages,
            tools,
            sampling,
            stream=False,
            response_format=response_format,
            extra_body=extra_body,
        )
        response = await self._send_with_retries(
            "POST", "/chat/completions", json_body=body, retries=sampling.retries
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"malformed JSON from /chat/completions: {self._describe(exc)}"
            ) from exc
        return self._parse_completion(payload)

    async def stream(  # type: ignore[override]
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
        response_format: dict | None = None,
        extra_body: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        sampling = sampling or SamplingPolicy()
        body = self._request_body(
            messages,
            tools,
            sampling,
            stream=True,
            response_format=response_format,
            extra_body=extra_body,
        )
        try:
            response = await self._send_with_retries(
                "POST", "/chat/completions", json_body=body, retries=sampling.retries, stream=True
            )
        except ProviderError as exc:
            # stream mode: transport failure is a terminal error EVENT, not a raise
            reason = "timeout" if isinstance(exc, ProviderTimeout) else "provider_error"
            yield StreamEvent(
                kind="error",
                data={"repairable": False, "reason": reason, "message": str(exc)},
            )
            return
        try:
            async for event in self._parse_sse(response):
                yield event
        finally:
            await response.aclose()

    async def list_models(self) -> list[str]:
        response = await self._send_with_retries("GET", "/models", retries=SamplingPolicy().retries)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"malformed JSON from /models: {self._describe(exc)}") from exc
        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ProviderError("/models response carries no model list")
        return [
            entry["id"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]

    async def close(self) -> None:
        await self._client.aclose()
