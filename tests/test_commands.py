"""Slash command registry and the live built-ins."""

import pytest

from ironcore import __version__
from ironcore.commands import CommandContext, UnknownCommand, build_default_registry
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


def test_help_lists_everything_and_labels_planned(ctx):
    registry, context = ctx
    out = registry.dispatch("/help", context)
    assert "/goal" in out
    assert "[planned]" in out  # honesty: stubs are labeled


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


def test_stubs_point_at_todo(ctx):
    registry, context = ctx
    out = registry.dispatch("/workflow review", context)
    assert "IC-904" in out


def test_phase8_commands_are_implemented(ctx):
    registry, _ = ctx
    by_name = {c.name: c for c in registry.all()}
    for name in ("model", "init", "goal", "loop", "compact", "undo", "redo", "review", "memory"):
        assert by_name[name].implemented, f"/{name} must be implemented after phase 8"


def test_only_deferred_commands_remain_planned(ctx):
    registry, _ = ctx
    planned = {c.name for c in registry.all() if not c.implemented}
    assert planned == {"workflow", "envelope", "probe"}


def test_redo_is_registered(ctx):
    # /redo is new in phase 8 (SPEC §3.3) — it was not in the scaffold's stub table.
    registry, _ = ctx
    assert registry.get("redo") is not None
