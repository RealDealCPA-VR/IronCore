"""/goal (IC-803): objective anchoring + attached verify stop-condition checks."""

import asyncio

from ironcore.commands.base import CommandContext
from ironcore.commands.goalcmd import _VERIFY_CHECKS, _cmd_goal
from ironcore.config.settings import Settings


class _State:
    def __init__(self):
        self.goal = None


class _Engine:
    def __init__(self):
        self.state = _State()


def _ctx(tmp_path, *, engine=None, schedule=None):
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = str(tmp_path)
    if engine is not None:
        ctx.extra["engine"] = engine
    if schedule is not None:
        ctx.extra["schedule"] = schedule
    return ctx


def _sync_schedule():
    captured: list[str] = []

    def schedule(coro):
        captured.append(asyncio.run(coro))

    return schedule, captured


def test_set_updates_ctx_and_engine_state(tmp_path):
    engine = _Engine()
    ctx = _ctx(tmp_path, engine=engine)
    out = _cmd_goal(ctx, "ship v0.1")
    assert ctx.goal == "ship v0.1"
    assert engine.state.goal == "ship v0.1"
    assert "Goal set" in out
    _cmd_goal(ctx, "clear")
    assert engine.state.goal is None


def test_attach_verify_and_show(tmp_path):
    ctx = _ctx(tmp_path)
    _cmd_goal(ctx, "make tests pass")
    assert "Attached" in _cmd_goal(ctx, "verify: pytest -q")
    show = _cmd_goal(ctx, "show")
    assert "pytest -q" in show
    assert "make tests pass" in show
    _cmd_goal(ctx, "clear")
    assert _VERIFY_CHECKS.get(str(tmp_path)) is None


def test_check_met(tmp_path):
    schedule, captured = _sync_schedule()
    ctx = _ctx(tmp_path, schedule=schedule)
    _cmd_goal(ctx, "be green")
    _cmd_goal(ctx, "verify: exit 0")
    ack = _cmd_goal(ctx, "check")
    assert "Checking" in ack
    assert captured and "MET" in captured[0]
    _cmd_goal(ctx, "clear")


def test_check_unmet(tmp_path):
    schedule, captured = _sync_schedule()
    ctx = _ctx(tmp_path, schedule=schedule)
    _cmd_goal(ctx, "be green")
    _cmd_goal(ctx, "verify: exit 1")
    _cmd_goal(ctx, "check")
    assert captured and "UNMET" in captured[0]
    _cmd_goal(ctx, "clear")


def test_check_without_attached_commands(tmp_path):
    schedule, _ = _sync_schedule()
    ctx = _ctx(tmp_path, schedule=schedule)
    _cmd_goal(ctx, "do the thing")
    assert "No verify commands attached" in _cmd_goal(ctx, "check")
    _cmd_goal(ctx, "clear")


def test_show_without_goal(tmp_path):
    assert "No goal set" in _cmd_goal(_ctx(tmp_path), "")


def test_verify_mirrors_onto_engine_state_to_arm_the_stop_condition(tmp_path):
    from ironcore.core.state import SessionState

    class _Eng:
        def __init__(self):
            self.state = SessionState()

    eng = _Eng()
    ctx = _ctx(tmp_path, engine=eng)
    _cmd_goal(ctx, "ship it")
    _cmd_goal(ctx, "verify: pytest -q")
    # the engine's durable list is what its in-turn verifier reads
    assert eng.state.goal_verify == ["pytest -q"]
    _cmd_goal(ctx, "clear")
    assert eng.state.goal_verify == []  # clearing the goal disarms the engine too
