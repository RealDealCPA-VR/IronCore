"""Slash command registry and the live built-ins."""

import pytest
from rich.text import Text

from ironcore import __version__
from ironcore.commands import CommandContext, UnknownCommand, build_default_registry, plain
from ironcore.commands.base import CommandRegistry, SlashCommand
from ironcore.config.settings import Settings
from ironcore.safety.modes import Mode


@pytest.fixture()
def ctx():
    registry = build_default_registry()
    context = CommandContext(settings=Settings())
    context.extra["registry"] = registry
    return registry, context


def test_core_commands_declared(ctx):
    registry, _ = ctx
    names = {c.name for c in registry.all()}
    assert {"help", "goal", "loop", "workflow", "mode", "model", "init", "undo"} <= names


def test_help_lists_every_command_all_implemented(ctx):
    registry, context = ctx
    out = registry.dispatch("/help", context)
    assert "/goal" in out and "/envelope" in out and "/probe" in out
    assert "[planned]" not in out  # every declared command is now live


def test_version_command(ctx):
    registry, context = ctx
    assert __version__ in registry.dispatch("/version", context)


def test_mode_cycles_and_sets(ctx):
    registry, context = ctx
    assert context.mode == Mode.MANUAL
    registry.dispatch("/mode", context)
    assert context.mode == Mode.ACCEPT_EDITS  # one step along CYCLE
    registry.dispatch("/mode plan", context)
    assert context.mode == Mode.PLAN
    out = registry.dispatch("/mode bogus", context)
    assert "Unknown mode" in out
    assert context.mode == Mode.PLAN  # unchanged on bad input


def test_goal_set_show_clear(ctx):
    registry, context = ctx
    assert "No goal set" in registry.dispatch("/goal", context)
    registry.dispatch("/goal ship v0.1", context)
    assert context.goal == "ship v0.1"
    assert "ship v0.1" in registry.dispatch("/goal", context)
    registry.dispatch("/goal clear", context)
    assert context.goal is None


def test_unknown_command_raises(ctx):
    registry, context = ctx
    with pytest.raises(UnknownCommand):
        registry.dispatch("/frobnicate", context)


def test_envelope_and_probe_are_live(ctx):
    # IC-608: /envelope + /probe are real; with no engine they degrade gracefully.
    registry, context = ctx
    env = registry.dispatch("/envelope", context)
    assert "IC-608" not in env and "profile" in env.lower()
    prb = registry.dispatch("/probe", context)
    assert "IC-608" not in prb and "session" in prb.lower()


def test_phase8_commands_are_implemented(ctx):
    registry, _ = ctx
    by_name = {c.name: c for c in registry.all()}
    for name in ("model", "init", "goal", "loop", "compact", "undo", "redo", "review", "memory"):
        assert by_name[name].implemented, f"/{name} must be implemented after phase 8"


def test_no_commands_remain_planned(ctx):
    registry, _ = ctx
    planned = {c.name for c in registry.all() if not c.implemented}
    assert planned == set()  # v0.1: every command is implemented


def test_redo_is_registered(ctx):
    # /redo is new in phase 8 (SPEC §3.3) — it was not in the scaffold's stub table.
    registry, _ = ctx
    assert registry.get("redo") is not None


# -- handler result type (CONTRACTS.md §6) --------------------------------------


def _registry_of(handler) -> CommandRegistry:
    registry = CommandRegistry()
    registry.register(SlashCommand("x", "s", "/x", handler))
    return registry


def test_a_handler_may_return_plain_text():
    """``str`` stays the default and the common case: nothing about the
    additive Text option may change what a plain handler does."""
    out = _registry_of(lambda ctx, args: "just words").dispatch("/x", CommandContext(Settings()))
    assert out == "just words"
    assert plain(out) == "just words"


def test_a_handler_may_return_styled_text():
    """The additive half: a command whose output contains a VERDICT can style
    it, and dispatch hands the styling through untouched."""
    styled = Text()
    styled.append("MET", style="bold green")
    out = _registry_of(lambda ctx, args: styled).dispatch("/x", CommandContext(Settings()))
    assert isinstance(out, Text)
    assert out.spans  # the styling survived dispatch
    assert plain(out) == "MET"  # ...and a str-assuming caller still gets characters


def test_envelope_returns_a_styled_card_whose_plain_text_is_unchanged():
    """/envelope is the flagship consumer: its characters must be exactly what
    the plain-text renderer produces, so a pipe or a pasted issue loses only
    colour."""
    from ironcore.envelope.profile import CapabilityProfile
    from ironcore.envelope.runner import render_report_card

    class _Engine:
        profile = CapabilityProfile(model_id="m", source="probed", probed_at="t")

    registry = build_default_registry()
    ctx = CommandContext(settings=Settings(), extra={"engine": _Engine()})
    out = registry.dispatch("/envelope", ctx)
    assert isinstance(out, Text)
    assert out.spans
    assert plain(out) == render_report_card(_Engine.profile)
