"""/loop (IC-804): interval parsing, loop-spec registration, status, and stop."""

from ironcore.commands.base import CommandContext
from ironcore.commands.loopcmd import _LOOPS, LoopSpec, _cmd_loop, parse_interval
from ironcore.config.settings import Settings


def _ctx(tmp_path) -> CommandContext:
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = str(tmp_path)
    return ctx


def test_parse_interval_units():
    assert parse_interval("30s") == 30
    assert parse_interval("5m") == 300
    assert parse_interval("1h") == 3600
    assert parse_interval("2d") == 172800
    assert parse_interval("45") == 45


def test_parse_interval_rejects_non_intervals():
    assert parse_interval("build the app") is None
    assert parse_interval("") is None
    assert parse_interval("0s") is None
    assert parse_interval("-3m") is None


def test_loopspec_describe():
    assert LoopSpec("ping", 300).describe() == "every 5m: ping"
    assert LoopSpec("keep going", None).describe() == "self-paced: keep going"


def test_register_with_interval(tmp_path):
    ctx = _ctx(tmp_path)
    out = _cmd_loop(ctx, "5m check the build")
    assert "every 5m" in out
    spec = _LOOPS[str(tmp_path)]
    assert spec.interval_s == 300
    assert spec.prompt == "check the build"
    _cmd_loop(ctx, "stop")


def test_register_self_paced(tmp_path):
    ctx = _ctx(tmp_path)
    assert "self-paced" in _cmd_loop(ctx, "keep refactoring")
    assert _LOOPS[str(tmp_path)].interval_s is None
    _cmd_loop(ctx, "stop")


def test_interval_token_without_prompt_is_self_paced(tmp_path):
    ctx = _ctx(tmp_path)
    assert "self-paced" in _cmd_loop(ctx, "5m")
    assert _LOOPS[str(tmp_path)].prompt == "5m"
    _cmd_loop(ctx, "stop")


def test_status_and_stop(tmp_path):
    ctx = _ctx(tmp_path)
    assert "No loop" in _cmd_loop(ctx, "status")
    _cmd_loop(ctx, "30s ping")
    assert "every 30s" in _cmd_loop(ctx, "status")
    assert "stopped" in _cmd_loop(ctx, "stop").lower()
    assert "No loop" in _cmd_loop(ctx, "status")
    assert "No loop to stop" in _cmd_loop(ctx, "stop")
