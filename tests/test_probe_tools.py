"""TOOL-FORM + JSON-STRICT probes (IC-603).

Drives ``ToolFormProbe`` / ``JsonStrictProbe`` with a ``MockProvider``. Because the mock
returns scripted completions in FIFO order regardless of prompt, each test scripts one
``CompletionResult`` per trial and relies on the documented consumption order:
``ToolFormProbe`` runs all ``native`` trials, then all ``strict_json``, then all
``text_protocol`` (``3 * trials`` provider calls); ``JsonStrictProbe`` runs ``trials``
calls. Scoring is mechanical, so results are deterministic on these fixtures.
"""

import asyncio
import json

from ironcore.envelope.probe_tools import (
    DEFAULT_TRIALS,
    JsonStrictProbe,
    ToolFormProbe,
)
from ironcore.envelope.runner import run_probes
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider, RaiseError

# --------------------------------------------------------------------------- #
# Fixtures: one scripted completion == one trial
# --------------------------------------------------------------------------- #

_ARGS = {"city": "Paris", "units": "celsius"}
_STRICT_JSON_OK = json.dumps({"tool": "get_weather", "args": _ARGS})
_IRONCALL_OK = (
    '```ironcall\n{"tool": "get_weather", "args": '
    '{"city": "Paris", "units": "celsius"}}\n```'
)
_JSON_OK = json.dumps({"title": "Ship it", "priority": 2, "done": False, "tags": ["a"]})


def _native_ok(name="get_weather", args=None):
    args = _ARGS if args is None else args
    return CompletionResult(
        message=Message(
            role="assistant", tool_calls=[ToolCall(id="c0", name=name, arguments=args)]
        )
    )


def _text(content):
    return CompletionResult(message=Message(role="assistant", content=content))


_GARBAGE = _text("Sorry, I can't do that.")


def _provider(entries):
    return MockProvider(script=list(entries))


class _RecordingProvider(MockProvider):
    """MockProvider that remembers the ``response_format`` of every ``complete`` call.

    ``MockProvider.last_response_format`` keeps only the most recent value, and the probe
    runs ``text_protocol`` (unconstrained) after ``strict_json`` — so the *sequence* is
    what proves the strict_json trials requested server-side constrained decoding while the
    native/text trials did not. Scoring is untouched: ``super().complete`` still replays the
    FIFO script and ignores the knob, exactly like a server that honors (or drops) it.
    """

    def __init__(self, entries):
        super().__init__(script=list(entries))
        self.response_formats: list[dict | None] = []

    async def complete(
        self,
        messages,
        *,
        tools=None,
        sampling=None,
        response_format=None,
        extra_body=None,
    ):
        self.response_formats.append(response_format)
        return await super().complete(
            messages,
            tools=tools,
            sampling=sampling,
            response_format=response_format,
            extra_body=extra_body,
        )


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_probe_identity_and_targets():
    tf = ToolFormProbe()
    assert tf.id == "TOOL-FORM"
    assert tuple(tf.targets) == (
        "tool_protocols.native",
        "tool_protocols.strict_json",
        "tool_protocols.text_protocol",
    )
    js = JsonStrictProbe()
    assert js.id == "JSON-STRICT"
    assert tuple(js.targets) == ("json_adherence",)
    assert DEFAULT_TRIALS == 10


def test_invalid_trials_rejected():
    for bad in (0, -1):
        try:
            ToolFormProbe(trials=bad)
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("expected ValueError")


# --------------------------------------------------------------------------- #
# ToolFormProbe scoring
# --------------------------------------------------------------------------- #


def test_perfect_native_but_garbage_text_protocol():
    # native perfect, strict_json + text_protocol garbage -> native~1.0, text~0.0
    t = 5
    script = [_native_ok() for _ in range(t)] + [_GARBAGE] * t + [_GARBAGE] * t
    provider = _provider(script)
    result = asyncio.run(ToolFormProbe(trials=t).run(provider))
    assert result.ok is True
    assert result.scores["tool_protocols.native"] == 1.0
    assert result.scores["tool_protocols.strict_json"] == 0.0
    assert result.scores["tool_protocols.text_protocol"] == 0.0
    # one provider call per trial, 3 protocols
    assert len(provider.calls) == 3 * t


def test_valid_ironcall_scores_text_protocol_high():
    t = 4
    # native + strict garbage, text_protocol all valid ironcall blocks
    script = [_GARBAGE] * t + [_GARBAGE] * t + [_text(_IRONCALL_OK) for _ in range(t)]
    result = asyncio.run(ToolFormProbe(trials=t).run(_provider(script)))
    assert result.scores["tool_protocols.text_protocol"] == 1.0
    assert result.scores["tool_protocols.native"] == 0.0


def test_all_protocols_perfect():
    t = 3
    script = (
        [_native_ok() for _ in range(t)]
        + [_text(_STRICT_JSON_OK) for _ in range(t)]
        + [_text(_IRONCALL_OK) for _ in range(t)]
    )
    result = asyncio.run(ToolFormProbe(trials=t).run(_provider(script)))
    assert result.scores == {
        "tool_protocols.native": 1.0,
        "tool_protocols.strict_json": 1.0,
        "tool_protocols.text_protocol": 1.0,
    }


def test_native_wrong_name_or_args_scores_zero():
    t = 4
    # wrong tool name, and wrong args -> none correct
    bad = [_native_ok(name="get_forecast") for _ in range(2)] + [
        _native_ok(args={"city": "London", "units": "celsius"}) for _ in range(2)
    ]
    script = bad + [_GARBAGE] * t + [_GARBAGE] * t
    result = asyncio.run(ToolFormProbe(trials=t).run(_provider(script)))
    assert result.scores["tool_protocols.native"] == 0.0


def test_native_two_calls_is_not_correct():
    # exactly one call required; two calls -> not correct
    t = 2
    two = CompletionResult(
        message=Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="a", name="get_weather", arguments=_ARGS),
                ToolCall(id="b", name="get_weather", arguments=_ARGS),
            ],
        )
    )
    script = [two, two] + [_GARBAGE] * t + [_GARBAGE] * t
    result = asyncio.run(ToolFormProbe(trials=t).run(_provider(script)))
    assert result.scores["tool_protocols.native"] == 0.0


def test_strict_json_half():
    t = 4
    strict = [_text(_STRICT_JSON_OK), _GARBAGE, _text(_STRICT_JSON_OK), _GARBAGE]
    script = [_GARBAGE] * t + strict + [_GARBAGE] * t
    result = asyncio.run(ToolFormProbe(trials=t).run(_provider(script)))
    assert result.scores["tool_protocols.strict_json"] == 0.5


def test_strict_json_trials_request_guided_decoding():
    # The strict_json rung is GUIDED: its trials must ask the server to CONSTRAIN output
    # (response_format), while native + text_protocol stay unconstrained. Scoring unchanged.
    t = 3
    script = (
        [_native_ok() for _ in range(t)]
        + [_text(_STRICT_JSON_OK) for _ in range(t)]
        + [_text(_IRONCALL_OK) for _ in range(t)]
    )
    provider = _RecordingProvider(script)
    result = asyncio.run(ToolFormProbe(trials=t).run(provider))
    # constrained-and-valid strict_json still scores perfect (mechanical scoring intact)
    assert result.scores["tool_protocols.strict_json"] == 1.0
    seen = provider.response_formats
    assert len(seen) == 3 * t
    native, strict, text = seen[:t], seen[t : 2 * t], seen[2 * t :]
    # native trials requested NO server-side constraint
    assert native == [None] * t
    # after the strict_json trials ran, response_format was requested on every one
    assert all(rf is not None for rf in strict)
    assert all(rf["type"] == "json_schema" for rf in strict)
    # the json-schema pins the tool NAME (get_weather) + the done finisher
    enum = strict[0]["json_schema"]["schema"]["properties"]["tool"]["enum"]
    assert "get_weather" in enum and "done" in enum
    # text_protocol (IRONCALL) trials requested NO constraint
    assert text == [None] * t
    # text_protocol ran last, so the latest recorded knob is None again
    assert provider.last_response_format is None


def test_strict_json_guided_but_server_ignores_scores_low():
    # A server that IGNORES response_format returns best-effort text: the probe still
    # asked for the constraint, but scores by the same criterion -> low (no regression).
    t = 4
    script = [_native_ok() for _ in range(t)] + [_GARBAGE] * t + [_GARBAGE] * t
    provider = _RecordingProvider(script)
    result = asyncio.run(ToolFormProbe(trials=t).run(provider))
    assert result.scores["tool_protocols.strict_json"] == 0.0
    strict = provider.response_formats[t : 2 * t]
    assert all(rf is not None for rf in strict)  # constraint requested regardless


def test_tool_form_deterministic():
    t = 3
    script = (
        [_native_ok() for _ in range(t)]
        + [_text(_STRICT_JSON_OK) for _ in range(t)]
        + [_text(_IRONCALL_OK) for _ in range(t)]
    )
    a = asyncio.run(ToolFormProbe(trials=t).run(_provider(script)))
    b = asyncio.run(ToolFormProbe(trials=t).run(_provider(script)))
    assert a.scores == b.scores


def test_tool_form_provider_error_degrades():
    # first provider call raises -> ok=False, note, no scores, no crash
    provider = _provider([RaiseError("boom")])
    result = asyncio.run(ToolFormProbe(trials=3).run(provider))
    assert result.ok is False
    assert result.scores == {}
    assert "boom" in result.notes


# --------------------------------------------------------------------------- #
# JsonStrictProbe scoring
# --------------------------------------------------------------------------- #


def test_json_strict_half_conforming():
    # 2 conforming + 2 non-conforming -> 0.5
    t = 4
    non_conforming = [
        _text("this is not json"),  # unparseable
        _text(json.dumps({"title": "x", "priority": 1, "done": False})),  # missing tags
    ]
    conforming = [_text(_JSON_OK), _text(_JSON_OK)]
    # interleave to prove order-independence of the count
    script = [conforming[0], non_conforming[0], conforming[1], non_conforming[1]]
    provider = _provider(script)
    result = asyncio.run(JsonStrictProbe(trials=t).run(provider))
    assert result.ok is True
    assert result.scores["json_adherence"] == 0.5
    assert len(provider.calls) == t  # one call per trial


def test_json_strict_all_conforming():
    t = 5
    result = asyncio.run(
        JsonStrictProbe(trials=t).run(_provider([_text(_JSON_OK) for _ in range(t)]))
    )
    assert result.scores["json_adherence"] == 1.0


def test_json_strict_bool_not_accepted_for_int():
    # priority is a bool, which must NOT satisfy the int schema slot
    t = 2
    payload = json.dumps({"title": "x", "priority": True, "done": False, "tags": []})
    result = asyncio.run(JsonStrictProbe(trials=t).run(_provider([_text(payload)] * t)))
    assert result.scores["json_adherence"] == 0.0


def test_json_strict_wrong_type_rejected():
    t = 2
    payload = json.dumps({"title": "x", "priority": "high", "done": False, "tags": []})
    result = asyncio.run(JsonStrictProbe(trials=t).run(_provider([_text(payload)] * t)))
    assert result.scores["json_adherence"] == 0.0


def test_json_strict_non_object_rejected():
    t = 2
    # a bare JSON array parses but is not an object -> non-conforming
    result = asyncio.run(
        JsonStrictProbe(trials=t).run(_provider([_text("[1, 2, 3]")] * t))
    )
    assert result.scores["json_adherence"] == 0.0


def test_json_strict_provider_error_degrades():
    result = asyncio.run(JsonStrictProbe(trials=3).run(_provider([RaiseError("nope")])))
    assert result.ok is False
    assert result.scores == {}
    assert "nope" in result.notes


# --------------------------------------------------------------------------- #
# Integration with the runner (dotted-path merge + ladder selection)
# --------------------------------------------------------------------------- #


def test_tool_form_through_runner_selects_native():
    t = 4
    script = (
        [_native_ok() for _ in range(t)]
        + [_text(_STRICT_JSON_OK) for _ in range(t)]
        + [_text(_IRONCALL_OK) for _ in range(t)]
    )
    profile = asyncio.run(
        run_probes(_provider(script), [ToolFormProbe(trials=t)], model_id="m", probed_at="t")
    )
    assert profile.tool_protocols["native"] == 1.0
    assert profile.recommended_tool_protocol() == "native"


def test_erroring_probe_through_runner_degrades_to_floor():
    # provider raises -> runner degrades all tool_protocols targets to 0.0
    profile = asyncio.run(
        run_probes(
            _provider([RaiseError("x")]),
            [ToolFormProbe(trials=3)],
            model_id="m",
            probed_at="t",
        )
    )
    assert profile.tool_protocols["native"] == 0.0
    assert profile.tool_protocols["text_protocol"] == 0.0
    assert profile.recommended_tool_protocol() == "text_protocol"
