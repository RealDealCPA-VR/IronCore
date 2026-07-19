"""Headless exec (PKG-5): the `ironcore exec` event-stream renderer + wiring.

Drives the real TurnEngine against a scripted MockProvider (like the demo), so
these prove the whole headless arc — stream text to stdout, status to stderr,
--json per-line events, and fail-closed approvals — with no network and no real
model. Async is handled by run_exec's own asyncio.run; tests capture via
StringIO.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from ironcore import headless
from ironcore.cli import build_parser, cmd_exec
from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalBroker
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools import build_default_registry


def _assistant(content: str = "", calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=list(calls or []))
    )


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _engine(script: list[CompletionResult], ws: Path, mode: Mode) -> TurnEngine:
    settings = Settings()
    registry = build_default_registry(settings, ws)
    return TurnEngine(
        MockProvider(script),
        registry,
        settings,
        _profile(),
        mode,
        workspace=ws,
        approvals=ApprovalBroker(timeout=0.0),  # asks fail closed, immediately
        snapshots=None,
        handoff_path=None,
    )


def _run(script, ws, mode, *, json_output=False):
    out, err = io.StringIO(), io.StringIO()
    code = headless.run_exec(
        _engine(script, ws, mode), "do the thing", json_output=json_output, out=out, err=err
    )
    return code, out.getvalue(), err.getvalue()


# --- end-to-end: prose to stdout, status to stderr -----------------------------


def test_exec_streams_the_model_text_to_stdout(tmp_path):
    code, out, err = _run([_assistant("Hello from the model.")], tmp_path, Mode.PLAN)
    assert code == 0
    assert "Hello from the model." in out
    assert "[turn t0] mode=plan" in err  # status is on stderr, not stdout
    assert "[done] stop_reason=done" in err


def test_exec_json_emits_one_valid_event_per_line(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    code = headless.run_exec(
        _engine([_assistant("Streamed answer.")], tmp_path, Mode.PLAN),
        "q",
        json_output=True,
        out=out,
        err=err,
    )
    assert code == 0
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]  # every line is valid JSON
    types = [e["type"] for e in events]
    assert types[0] == "turn_started"
    assert "text_delta" in types
    assert types[-1] == "turn_completed"
    assert events[-1]["stop_reason"] == "done"
    assert err.getvalue() == ""  # --json writes only to stdout


# --- plan mode is read-only: a mutating call auto-denies, nothing changes -------


def test_plan_mode_cannot_mutate(tmp_path):
    target = "out.txt"
    script = [
        _assistant(calls=[ToolCall(id="c1", name="write_file",
                                   arguments={"path": target, "content": "leaked"})]),
        _assistant("I was blocked from writing."),
    ]
    code, out, err = _run(script, tmp_path, Mode.PLAN)
    assert code == 0  # the turn completed; the mutation did not
    assert not (tmp_path / target).exists()  # PLAN denies WRITE by policy
    assert "write_file risk=write -> deny" in err


def test_plan_mode_denied_mutation_reports_no_write_in_json(tmp_path):
    script = [
        _assistant(calls=[ToolCall(id="c1", name="write_file",
                                   arguments={"path": "x.txt", "content": "leaked"})]),
        _assistant("blocked"),
    ]
    out, err = io.StringIO(), io.StringIO()
    code = headless.run_exec(
        _engine(script, tmp_path, Mode.PLAN), "q", json_output=True, out=out, err=err
    )
    assert code == 0
    events = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    gated = [e for e in events if e["type"] == "tool_call_requested"]
    assert gated and gated[0]["decision"] == "deny" and gated[0]["risk"] == "write"
    # no tool ever ran -> the completion is honest about it
    assert not any(e["type"] == "tool_call_finished" for e in events)
    assert events[-1]["stop_reason"] == "denied"
    assert not (tmp_path / "x.txt").exists()


def test_manual_mode_ask_auto_denies_with_a_hint(tmp_path):
    """In a non-auto mode a WRITE ASKs; headless has no human, so the broker's
    fail-closed timeout DENIES it and a hint is printed (no new decision path)."""
    script = [
        _assistant(calls=[ToolCall(id="c1", name="write_file",
                                   arguments={"path": "y.txt", "content": "nope"})]),
        _assistant("done"),
    ]
    code, out, err = _run(script, tmp_path, Mode.MANUAL)
    assert code == 0
    assert not (tmp_path / "y.txt").exists()
    assert "[approval]" in err and "auto-denies" in err


# --- trailing newline is prose-conditional (PKG-5 round 1) ---------------------


def test_no_trailing_blank_line_when_turn_streams_no_prose(tmp_path):
    # A tool-only / denied / error turn writes NO prose to stdout, so
    # `ironcore exec "…" > answer.txt` must not be left with a lone blank line.
    # The terminating newline is emitted only when prose was actually streamed.
    script = [
        _assistant(calls=[ToolCall(id="c1", name="write_file",
                                   arguments={"path": "z.txt", "content": "x"})]),
        _assistant(""),  # write denied in PLAN, model yields no prose
    ]
    code, out, _ = _run(script, tmp_path, Mode.PLAN)
    assert code == 0
    assert out == ""  # not "\n": no prose means no stdout at all
    # and a prose turn still ends with exactly one trailing newline
    code2, out2, _ = _run([_assistant("The answer.")], tmp_path, Mode.PLAN)
    assert code2 == 0
    assert out2 == "The answer.\n"


# --- CLI wiring ----------------------------------------------------------------


def test_parser_has_exec_with_json_and_mode():
    args = build_parser().parse_args(["exec", "fix the bug", "--json", "--mode", "manual"])
    assert args.command == "exec"
    assert args.prompt == "fix the bug"
    assert args.json_output is True
    assert args.mode == "manual"
    default = build_parser().parse_args(["exec", "hi"])
    assert default.mode == "plan" and default.json_output is False  # CI-safe default


def test_cmd_exec_maps_config_error_to_exit_2(tmp_path, capsys):
    """A malformed project config is a setup problem (exit 2), distinct from a
    failed turn (1) — caught in cmd_exec before the generic main() backstop."""
    cfg = tmp_path / ".ironcore" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[provider\n", encoding="utf-8")  # unterminated table header
    code = cmd_exec("anything", project_dir=tmp_path)
    assert code == 2
    assert "ironcore:" in capsys.readouterr().err


# --- event serialization -------------------------------------------------------


def test_serialize_event_is_json_safe_for_every_event_type():
    from ironcore.core.events import (
        ApprovalRequired,
        ResampleProgress,
        TextDelta,
        ToolCallFinished,
        ToolCallRequested,
        TurnCompleted,
        TurnError,
        TurnStarted,
    )
    from ironcore.tools.base import ToolResult

    call = ToolCall(id="c1", name="shell", arguments={"command": "ls"})
    result = ToolResult(ok=True, output="files", error=None, data={"k": object()})
    events = [
        TurnStarted(turn_id="t0", mode="plan"),
        TextDelta(turn_id="t0", text="hi"),
        ToolCallRequested(turn_id="t0", call=call, risk="exec", decision="ask"),
        ApprovalRequired(turn_id="t0", call=call, risk="exec", preview="$ ls"),
        ToolCallFinished(turn_id="t0", call=call, result=result),
        ResampleProgress(turn_id="t0", seam="parse", attempt=1, total=2),
        TurnCompleted(turn_id="t0", usage={"total_tokens": 5}, stop_reason="done"),
        TurnError(turn_id="t0", message="boom", data={"reason": "x"}),
    ]
    for event in events:
        line = json.dumps(headless.serialize_event(event), default=str)
        back = json.loads(line)
        assert back["type"] and back["turn_id"] == "t0"
