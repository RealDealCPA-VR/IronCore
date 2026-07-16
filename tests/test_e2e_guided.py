"""End-to-end proof for guided decoding (the real strict_json rung).

Drives a full strict_json engine turn: the envelope routes a mid-tier model to
strict_json, so the engine constrains the server with `response_format` and the
model emits guaranteed-well-formed `{"tool","args"}` objects (read -> edit ->
done), each executed and gated through the normal machinery — and the raw JSON
scaffold never leaks to the transcript. Plus: the probe now measures guided
reliability, so the ladder routes here only when constrained decoding works.
"""

from __future__ import annotations

import asyncio
import json

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import ToolCallFinished, TurnCompleted
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools


def _strict_json_profile() -> CapabilityProfile:
    # native 0 (< 0.95) but strict_json 0.95 (>= 0.90) -> the ladder picks strict_json
    return CapabilityProfile(model_id="mid-tier", honest_context=8192,
                             tool_protocols={"strict_json": 0.95})


def _json(obj: dict) -> CompletionResult:
    # the server, constrained by response_format, emits exactly this JSON object
    return CompletionResult(message=Message(role="assistant", content=json.dumps(obj)))


class _RecordingMock(MockProvider):
    """Captures the response_format of every call (MockProvider keeps only the last)."""

    def __init__(self, script):
        super().__init__(script)
        self.formats: list = []

    async def stream(
        self, messages, *, tools=None, sampling=None, response_format=None, extra_body=None
    ):
        self.formats.append(response_format)
        async for ev in super().stream(
            messages, tools=tools, sampling=sampling,
            response_format=response_format, extra_body=extra_body,
        ):
            yield ev

    async def complete(
        self, messages, *, tools=None, sampling=None, response_format=None, extra_body=None
    ):
        self.formats.append(response_format)
        return await super().complete(
            messages, tools=tools, sampling=sampling,
            response_format=response_format, extra_body=extra_body,
        )


def test_guided_strict_json_full_session(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", newline="")
    registry = build_tools(Settings(), tmp_path)

    provider = _RecordingMock([
        _json({"tool": "read_file", "args": {"path": "app.py"}}),
        _json({
            "tool": "edit_file",
            "args": {
                "path": "app.py", "format": "search_replace",
                "edit": "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n",
            },
        }),
        _json({"tool": "done", "args": {"message": "Bumped x to 2."}}),
    ])
    engine = TurnEngine(
        provider, registry, Settings(), _strict_json_profile(), Mode.ACCEPT_EDITS,
        workspace=tmp_path, snapshots=None,
    )

    events = []

    async def go():
        async for ev in engine.run_turn("bump x to 2"):
            events.append(ev)

    asyncio.run(go())

    # every call constrained the server with the tool-call json_schema
    assert all(f is not None for f in provider.formats)
    assert provider.formats[0]["json_schema"]["name"] == "ironcore_tool_call"

    # both tools executed via the guided JSON path
    executed = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.call.name for e in executed] == ["read_file", "edit_file"]
    assert (tmp_path / "app.py").read_text() == "x = 2\n"  # the edit really applied

    text = "".join(getattr(e, "text", "") for e in events)
    assert '{"tool"' not in text  # the raw JSON scaffold never reached the transcript
    assert "Bumped x to 2." in text  # the done message surfaced as prose
    assert [e for e in events if isinstance(e, TurnCompleted)][-1].stop_reason == "done"


def test_probe_measures_guided_strict_json():
    # the strict_json rung the ladder routes on now reflects server-CONSTRAINED
    # reliability: the probe asks the server to constrain, then scores conformance
    from ironcore.envelope.probe_tools import ToolFormProbe

    weather = {"city": "Paris", "units": "celsius"}
    good = _json({"tool": "get_weather", "args": weather})
    # native ok, strict_json ok, text garbage -> strict_json still measurable
    provider = _RecordingMock([
        # ToolFormProbe(trials=1): native, strict_json, text (in that order)
        CompletionResult(message=Message(
            role="assistant",
            tool_calls=[ToolCall(id="n", name="get_weather", arguments=weather)],
        )),
        good,
        CompletionResult(message=Message(role="assistant", content="no block")),
    ])
    result = asyncio.run(ToolFormProbe(trials=1).run(provider))  # a ProbeResult
    # the strict_json trial was measured with a real constraint request
    # (formats: [native=None, strict_json=json_schema, text=None])
    assert provider.formats[1] is not None
    assert provider.formats[0] is None and provider.formats[2] is None
    assert result.scores.get("tool_protocols.strict_json", 0) >= 0.99
