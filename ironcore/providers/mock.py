"""MockProvider: scripted completions for offline tests and demos.

Every subsystem must be testable with zero network and zero model
(SPEC.md #14). MockProvider replays a queue of script entries; stream()
re-emits the same content as character chunks so streaming consumers get
exercised too.

IC-104: a script entry is either a CompletionResult (happy path, unchanged)
or a failure marker — MalformedToolJSON, Truncate, TimeoutFailure,
RaiseError — so tests can interleave failures with normal results. Streamed
failures honor the provider contract (docs/CONTRACTS.md #2): stream() still
terminates with a "done" or "error" event for every mode, and malformed
model output surfaces as a repairable error event, never an exception.
from_fixture() loads a JSONL transcript (see tests/fixtures/) into a script.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ironcore.providers.base import (
    CompletionResult,
    Message,
    Provider,
    SamplingPolicy,
    StreamEvent,
    ToolCall,
)
from ironcore.providers.openai_compat import ProviderError, ProviderTimeout


@dataclass
class MalformedToolJSON:
    """The model "emitted" a tool call whose JSON does not parse.

    stream(): yields text_prefix as text chunks, then a terminal
    StreamEvent(kind="error", data={"repairable": True,
    "reason": "malformed_tool_json", "raw": raw_fragment}).
    complete(): returns a normal result whose content carries the raw
    malformed text (text_prefix + raw_fragment) for the repair loop.
    """

    text_prefix: str = ""
    raw_fragment: str = '{"name": "broken", "arguments": {'


@dataclass
class Truncate:
    """The completion was cut off mid-output (max_tokens / context limit).

    stream(): yields content[:after_chars] as text chunks, then a terminal
    StreamEvent(kind="error", data={"repairable": True, "reason": "truncated"}).
    complete(): returns the partial content with finish_reason="length".
    """

    content: str
    after_chars: int


@dataclass
class TimeoutFailure:
    """The transport timed out, post-retries.

    complete(): raises ProviderTimeout(message) — the same ProviderError
    subclass the real client raises. stream(): yields a terminal
    StreamEvent(kind="error", data={"repairable": False, "reason": "timeout",
    "message": message}) — the terminate-with-done-or-error contract holds
    even for transport failures.
    """

    message: str = "request timed out"


@dataclass
class RaiseError:
    """A non-repairable provider failure with a caller-chosen message.

    complete(): raises ProviderError(message). stream(): yields a terminal
    StreamEvent(kind="error", data={"repairable": False,
    "reason": "provider_error", "message": message}).
    """

    message: str


ScriptEntry = CompletionResult | MalformedToolJSON | Truncate | TimeoutFailure | RaiseError


def _chunked(text: str) -> Iterator[StreamEvent]:
    # chunk in small pieces to exercise incremental rendering
    for i in range(0, len(text), 8):
        yield StreamEvent(kind="text", text=text[i : i + 8])


def _entry_from_record(record: dict[str, Any], *, path: Path, lineno: int) -> ScriptEntry:
    """One JSONL fixture line -> one script entry. See from_fixture() for the schema."""
    kind = record.get("type")
    try:
        if kind == "text":
            return CompletionResult(
                message=Message(role="assistant", content=record.get("content", "")),
                finish_reason=record.get("finish_reason", "stop"),
            )
        if kind == "tool_call":
            call = ToolCall(
                id=record.get("id", f"call_{lineno}"),
                name=record["name"],
                arguments=record.get("arguments", {}),
            )
            return CompletionResult(
                message=Message(
                    role="assistant", content=record.get("content", ""), tool_calls=[call]
                ),
                finish_reason=record.get("finish_reason", "tool_calls"),
            )
        if kind == "malformed_tool_json":
            kwargs = {k: record[k] for k in ("text_prefix", "raw_fragment") if k in record}
            return MalformedToolJSON(**kwargs)
        if kind == "truncate":
            return Truncate(content=record["content"], after_chars=int(record["after_chars"]))
        if kind == "timeout":
            kwargs = {k: record[k] for k in ("message",) if k in record}
            return TimeoutFailure(**kwargs)
        if kind == "error":
            return RaiseError(message=record["message"])
    except KeyError as exc:
        raise ValueError(f"{path}:{lineno}: entry type {kind!r} missing key {exc}") from exc
    raise ValueError(f"{path}:{lineno}: unknown entry type {kind!r}")


class MockProvider(Provider):
    name = "mock"

    def __init__(self, script: list[ScriptEntry] | None = None) -> None:
        self.script: list[ScriptEntry] = list(script or [])
        self.calls: list[list[Message]] = []  # what the engine sent, for assertions

    def push(self, result: ScriptEntry) -> None:
        self.script.append(result)

    @classmethod
    def from_fixture(cls, path: str | Path) -> MockProvider:
        """Load a JSONL transcript fixture; each non-blank line is one script entry.

        Line schema (``type`` selects the entry; brackets = optional):
          {"type": "text", "content": "...", ["finish_reason": "stop"]}
          {"type": "tool_call", "name": "...", "arguments": {...},
           ["content": "...", "id": "...", "finish_reason": "tool_calls"]}
          {"type": "malformed_tool_json", ["text_prefix": "...", "raw_fragment": "..."]}
          {"type": "truncate", "content": "...", "after_chars": N}
          {"type": "timeout", ["message": "..."]}
          {"type": "error", "message": "..."}
        """
        fixture = Path(path)
        script: list[ScriptEntry] = []
        # splitlines() (not naive \n split) keeps CRLF fixtures working on Windows
        for lineno, line in enumerate(fixture.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{fixture}:{lineno}: invalid JSON: {exc}") from exc
            script.append(_entry_from_record(record, path=fixture, lineno=lineno))
        return cls(script=script)

    def _pop_entry(self, messages: list[Message]) -> ScriptEntry:
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("MockProvider script exhausted")
        return self.script.pop(0)

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> CompletionResult:
        entry = self._pop_entry(messages)
        if isinstance(entry, CompletionResult):
            return entry
        if isinstance(entry, MalformedToolJSON):
            return CompletionResult(
                message=Message(role="assistant", content=entry.text_prefix + entry.raw_fragment)
            )
        if isinstance(entry, Truncate):
            return CompletionResult(
                message=Message(role="assistant", content=entry.content[: entry.after_chars]),
                finish_reason="length",
            )
        if isinstance(entry, TimeoutFailure):
            raise ProviderTimeout(entry.message)  # same subclass the real client raises
        # RaiseError
        raise ProviderError(entry.message)

    async def stream(  # type: ignore[override]
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        sampling: SamplingPolicy | None = None,
    ) -> AsyncIterator[StreamEvent]:
        entry = self._pop_entry(messages)
        if isinstance(entry, CompletionResult):
            for event in _chunked(entry.message.content):
                yield event
            for call in entry.message.tool_calls:
                yield StreamEvent(kind="tool_call", tool_call=call)
            if entry.usage:  # parity with the real provider's usage events
                yield StreamEvent(kind="usage", data=dict(entry.usage))
            yield StreamEvent(kind="done", data={"finish_reason": entry.finish_reason})
        elif isinstance(entry, MalformedToolJSON):
            for event in _chunked(entry.text_prefix):
                yield event
            yield StreamEvent(
                kind="error",
                data={
                    "repairable": True,
                    "reason": "malformed_tool_json",
                    "raw": entry.raw_fragment,
                },
            )
        elif isinstance(entry, Truncate):
            for event in _chunked(entry.content[: entry.after_chars]):
                yield event
            yield StreamEvent(kind="error", data={"repairable": True, "reason": "truncated"})
        elif isinstance(entry, TimeoutFailure):
            yield StreamEvent(
                kind="error",
                data={"repairable": False, "reason": "timeout", "message": entry.message},
            )
        else:  # RaiseError
            yield StreamEvent(
                kind="error",
                data={"repairable": False, "reason": "provider_error", "message": entry.message},
            )

    async def list_models(self) -> list[str]:
        return ["mock-model"]
