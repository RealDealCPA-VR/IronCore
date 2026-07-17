"""The runtime that makes IronCore mold to the model: /probe, /envelope,
probe_model, edit-format steering, and first-use auto-probe.

Without this wiring the probes exist but never run, so every model gets the
floor profile. These tests pin that the loop actually closes: measure ->
cache -> hot-swap -> the engine adapts.
"""

from __future__ import annotations

import asyncio

from ironcore.commands import build_default_registry as build_cmds
from ironcore.commands.base import CommandContext
from ironcore.commands.envelopecmd import probe_and_swap
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.probe_tools import ToolFormProbe
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.suite import probe_model
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools

WEATHER = {"city": "Paris", "units": "celsius"}


def _native_reply() -> CompletionResult:
    return CompletionResult(
        message=Message(
            role="assistant", tool_calls=[ToolCall(id="n", name="get_weather", arguments=WEATHER)]
        )
    )


def _text(s: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=s))


def _one_trial_suite() -> list:
    """A deterministic single-probe suite (native tool-form, 1 trial) so the
    shared MockProvider script is order-predictable: native, strict, text."""
    return [ToolFormProbe(trials=1)]


def _capable_provider() -> MockProvider:
    # ToolFormProbe(trials=1) issues exactly 3 calls, in this order:
    return MockProvider([_native_reply(), _text("no json"), _text("no block")])


def _profile(**kw) -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, **kw)


def _engine(tmp_path, provider, profile):
    settings = Settings()
    return TurnEngine(
        provider, build_tools(settings, tmp_path), settings, profile, Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )


# --- probe_model: measure + cache ------------------------------------------


def test_probe_model_measures_and_caches(tmp_path):
    envelope_dir = tmp_path / "env"
    profile = asyncio.run(
        probe_model(
            _capable_provider(),
            model_id="mock",
            envelope_dir=envelope_dir,
            probed_at="2026-07-16T00:00:00Z",
            probes=_one_trial_suite(),
        )
    )
    assert profile.probed_at == "2026-07-16T00:00:00Z"
    # native tool-calling was measured as reliable -> the ladder picks it up
    assert profile.tool_protocols.get("native", 0) >= 0.95
    assert profile.recommended_tool_protocol() == "native"
    # and it was persisted, reloadable
    reloaded = CapabilityProfile.load(envelope_dir, "mock")
    assert reloaded is not None and reloaded.probed_at == profile.probed_at


# --- /probe hot-swaps the engine profile -> adaptation ---------------------


def test_probe_command_hot_swaps_the_engine_profile(tmp_path, monkeypatch):
    # keep the probe cache out of the real home dir + use the deterministic suite
    monkeypatch.setattr("ironcore.envelope.suite.default_envelope_dir", lambda: tmp_path / "env")
    monkeypatch.setattr("ironcore.envelope.suite.default_probe_suite", _one_trial_suite)
    engine = _engine(tmp_path, _capable_provider(), _profile())  # unprobed floor
    assert engine.profile.recommended_tool_protocol() == "text_protocol"  # floor

    report = asyncio.run(probe_and_swap(engine))
    assert "profile updated" in report.lower()
    # the engine now carries the MEASURED profile — the next turn adapts
    assert engine.profile.probed_at is not None
    assert engine.profile.recommended_tool_protocol() == "native"


def test_dead_endpoint_degrades_to_floor_without_crashing(tmp_path, monkeypatch):
    # a totally unreachable endpoint: every probe fails -> the run DEGRADES each
    # score to the floor (never aborts), so we still get a usable floor profile
    monkeypatch.setattr("ironcore.envelope.suite.default_envelope_dir", lambda: tmp_path / "env")
    monkeypatch.setattr("ironcore.envelope.suite.default_probe_suite", _one_trial_suite)
    from ironcore.providers.mock import RaiseError

    engine = _engine(tmp_path, MockProvider([RaiseError(message="endpoint down")] * 5), _profile())
    report = asyncio.run(probe_and_swap(engine))  # must not raise
    assert isinstance(report, str)
    assert engine.profile.recommended_tool_protocol() == "text_protocol"  # floor, honest


# --- /envelope + /probe through the registry -------------------------------


def test_envelope_command_renders_the_profile(tmp_path):
    engine = _engine(tmp_path, MockProvider([]), _profile(tool_protocols={"native": 1.0}))
    registry = build_cmds()
    captured: list = []

    def schedule(coro):
        captured.append(asyncio.run(coro))

    ctx = CommandContext(settings=Settings(), extra={"engine": engine, "schedule": schedule})
    out = registry.dispatch("/envelope", ctx)
    assert "mock" in out  # the report card names the model
    assert "UNPROBED" in out  # honestly flags it hasn't been measured

    ack = registry.dispatch("/probe", ctx)
    assert "Probing" in ack  # returns an ack, schedules the real work


def test_envelope_command_appends_the_roles_tail(tmp_path):
    """MS-3: /envelope lists each ROUTED role's model + measured status after the
    primary card, and points at /model as the way a role model gets measured."""
    from ironcore.core.roles import RoleRouter

    settings = Settings.model_validate(
        {"roles": {"planner": "deep-70b", "coder": "tiny-7b"}}
    )
    router = RoleRouter(
        settings,
        providers={"planner": MockProvider(), "coder": MockProvider()},
        profiles={
            "deep-70b": CapabilityProfile(
                model_id="deep-70b",
                honest_context=16384,
                tool_protocols={"native": 1.0},
                probed_at="2026-07-16T00:00:00Z",
                source="probed",
            ),
            "tiny-7b": CapabilityProfile(model_id="tiny-7b"),  # unmeasured floor
        },
    )
    engine = _engine(tmp_path, MockProvider([]), _profile(tool_protocols={"native": 1.0}))
    engine.roles = router
    ctx = CommandContext(settings=settings, extra={"engine": engine})
    out = build_cmds().dispatch("/envelope", ctx)
    assert "mock" in out  # the primary card still leads
    assert "Roles (routed models):" in out
    assert "planner" in out and "deep-70b" in out and "[measured]" in out
    assert "coder" in out and "tiny-7b" in out and "unprobed — floor defaults" in out
    assert "/model" in out  # how a role model gets measured into the shared cache


def test_envelope_command_appends_the_tuned_footer_last(tmp_path):
    """MS-8: a tuned profile gets an honest footer AFTER the roles tail — both
    suffix-only, so the primary card and MS-3's tail survive verbatim."""
    from ironcore.core.roles import RoleRouter

    settings = Settings.model_validate({"roles": {"planner": "deep-70b"}})
    router = RoleRouter(
        settings,
        providers={"planner": MockProvider()},
        profiles={"deep-70b": CapabilityProfile(model_id="deep-70b")},
    )
    engine = _engine(
        tmp_path,
        MockProvider([]),
        CapabilityProfile(
            model_id="mock",
            source="tuned",
            probed_at="2026-07-16T00:00:00Z",
            tool_protocols={"strict_json": 0.95},
        ),
    )
    engine.roles = router
    ctx = CommandContext(settings=settings, extra={"engine": engine})
    out = build_cmds().dispatch("/envelope", ctx)
    assert "Roles (routed models):" in out
    assert "Tuned:" in out and "/probe" in out
    assert out.index("Tuned:") > out.index("Roles (routed models):")  # footer is LAST


def test_envelope_command_has_no_tuned_footer_for_untuned_profiles(tmp_path):
    engine = _engine(tmp_path, MockProvider([]), _profile(tool_protocols={"native": 1.0}))
    ctx = CommandContext(settings=Settings(), extra={"engine": engine})
    out = build_cmds().dispatch("/envelope", ctx)
    assert "Tuned:" not in out


# --- edit-format steering: the engine tells the model the measured format ---


def test_engine_steers_the_recommended_edit_format(tmp_path):
    settings = Settings()
    tools = build_tools(settings, tmp_path)
    # a profile that measured unified_diff as reliable
    profile = _profile(edit_formats={"unified_diff": 0.95})
    engine = TurnEngine(
        MockProvider([]), tools, settings, profile, Mode.AUTO, workspace=tmp_path, snapshots=None
    )
    prompt = engine._system_prompt(text_protocol=False)
    assert "unified_diff" in prompt  # steered to the measured format

    # a weak profile falls to the whole_file floor, and the prompt says so
    floor = TurnEngine(
        MockProvider([]), tools, settings, _profile(), Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )
    assert "whole_file" in floor._system_prompt(text_protocol=False)


# --- config: auto_probe defaults on ----------------------------------------


def test_auto_probe_defaults_on():
    assert Settings().envelope.auto_probe is True
