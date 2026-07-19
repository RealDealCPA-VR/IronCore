"""CommandVerifier (IC-504): discovery priority, honest run/report, engine wiring.

Hermetic and cross-OS: the run path is driven with portable ``sys.executable -c``
command lines (exit 0 vs ``sys.exit(1)``) so nothing depends on pytest/npm being
invocable in weird ways; auto-detect is driven by planting marker files and
asserting the DISCOVERED command string (we stop before running a real pytest).
Async is driven with ``asyncio.run`` (no pytest-asyncio); workspaces are tmp dirs.
"""

from __future__ import annotations

import asyncio
import sys

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import TurnCompleted
from ironcore.core.state import SessionState
from ironcore.core.verify import CommandVerifier
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry

# --------------------------------------------------------------------------- #
# portable, hermetic verify commands
# --------------------------------------------------------------------------- #

#: Python source run via `-c`; single quotes inside keep it cmd.exe-safe, and the
#: command never both starts AND ends with a quote (so cmd won't strip quotes).
_PASS_SRC = "import sys; sys.exit(0)"
_FAIL_SRC = "import sys; print('VERIFY_MARKER_TAIL'); sys.exit(1)"


def _py(source: str) -> str:
    return f'{sys.executable} -c "{source}"'


def _run(verifier: CommandVerifier, workspace, *, touched: bool = True) -> object:
    return asyncio.run(verifier.verify(workspace, Settings(), SessionState(), touched))


# --------------------------------------------------------------------------- #
# (1) discovery priority: configured > IRONCORE.md > auto-detect
# --------------------------------------------------------------------------- #


def test_configured_beats_md_and_markers(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "IRONCORE.md").write_text("verify: echo from-md\n", encoding="utf-8")
    commands, source = CommandVerifier(commands=["echo configured"]).discover(tmp_path)
    assert commands == ["echo configured"]
    assert source == "configured"


def test_ironcore_md_beats_markers(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "IRONCORE.md").write_text("# Title\n\nverify: pytest -q tests/unit\n", "utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == ["pytest -q tests/unit"]
    assert source == "ironcore.md"


def test_ironcore_md_verify_section(tmp_path):
    md = "# Project\n\n## Verify\n\n- ruff check .\n- pytest -q\n\n## Other\n\nignore me\n"
    (tmp_path / "IRONCORE.md").write_text(md, encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == ["ruff check .", "pytest -q"]
    assert source == "ironcore.md"


# --------------------------------------------------------------------------- #
# SECURITY (PKG-3 / parity review): `verify:` is sourced from the PROJECT
# IRONCORE.md ALONE. PKG-3 widened DISPLAY-memory to AGENTS.md/CLAUDE.md and a
# user-global file — the verify-command file set must NOT widen with it, because
# a verify command executes unattended after the first edit. These pins fail
# closed if a future change ever teaches discovery to read those files.
# --------------------------------------------------------------------------- #


def test_verify_not_sourced_from_agents_md(tmp_path):
    # An AGENTS.md carrying a verify: directive, no IRONCORE.md, no markers.
    (tmp_path / "AGENTS.md").write_text("verify: echo pwned-by-agents-md\n", encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == []  # the AGENTS.md verify: is NOT honored
    assert source == "none"
    assert not any("pwned" in c for c in commands)


def test_verify_not_sourced_from_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("verify: echo pwned-by-claude-md\n", encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == []
    assert source == "none"


def test_ironcore_md_verify_wins_over_agents_md(tmp_path):
    # With BOTH present, the real IRONCORE.md is the sole verify source; a cloned
    # AGENTS.md cannot inject or override the command that will run.
    (tmp_path / "IRONCORE.md").write_text("verify: ruff check .\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("verify: echo pwned-by-agents-md\n", encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == ["ruff check ."]
    assert source == "ironcore.md"
    assert not any("pwned" in c for c in commands)


# --------------------------------------------------------------------------- #
# (2) auto-detect by workspace markers (assert the discovered command string)
# --------------------------------------------------------------------------- #


def test_autodetect_pytest_via_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == ["pytest -q"]
    assert source == "auto:pytest"


def test_autodetect_pytest_via_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    commands, _ = CommandVerifier().discover(tmp_path)
    assert commands == ["pytest -q"]


def test_autodetect_pytest_via_pytest_ini(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    commands, _ = CommandVerifier().discover(tmp_path)
    assert commands == ["pytest -q"]


def test_autodetect_npm_test(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == ["npm test"]
    assert source == "auto:npm"


def test_npm_without_test_script_is_not_detected(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "tsc"}}', encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == []
    assert source == "none"


def test_autodetect_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n", encoding="utf-8")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == ["cargo test"]
    assert source == "auto:cargo"


# --------------------------------------------------------------------------- #
# (3) run path — configured portable commands
# --------------------------------------------------------------------------- #


def test_configured_passing_command_ok(tmp_path):
    result = _run(CommandVerifier(commands=[_py(_PASS_SRC)]), tmp_path)
    assert result.ok is True
    assert result.ran == [_py(_PASS_SRC)]


def test_configured_failing_command_reports_tail(tmp_path):
    command = _py(_FAIL_SRC)
    result = _run(CommandVerifier(commands=[command]), tmp_path)
    assert result.ok is False
    assert command in result.summary  # the failing command line is named
    assert "VERIFY_MARKER_TAIL" in result.summary  # its output tail surfaced
    assert result.ran == [command]


def test_stops_at_first_failing_command(tmp_path):
    ok_cmd = _py(_PASS_SRC)
    bad_cmd = _py(_FAIL_SRC)
    never = _py("import sys; print('SHOULD_NOT_RUN'); sys.exit(0)")
    result = _run(CommandVerifier(commands=[ok_cmd, bad_cmd, never]), tmp_path)
    assert result.ok is False
    assert result.ran == [ok_cmd, bad_cmd]  # the third command never ran
    assert "SHOULD_NOT_RUN" not in result.summary


# --------------------------------------------------------------------------- #
# (4) skip / no-command honesty
# --------------------------------------------------------------------------- #


def test_touched_files_false_skips(tmp_path):
    result = _run(CommandVerifier(commands=[_py(_FAIL_SRC)]), tmp_path, touched=False)
    assert result.ok is True  # skipped: verification only runs after mutations
    assert result.ran == []


def test_no_command_found_is_ok_but_honest(tmp_path):
    result = _run(CommandVerifier(), tmp_path)  # empty workspace, no config
    assert result.ok is True
    assert result.ran == []
    assert "no verify command" in result.summary


# --------------------------------------------------------------------------- #
# (5) engine integration — a failing verify surfaces as a [verify] TextDelta
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def test_engine_surfaces_verify_failure(tmp_path):
    settings = Settings()
    tools = build_default_registry(settings, tmp_path)
    write = ToolCall(id="c1", name="write_file", arguments={"path": "out.txt", "content": "x\n"})
    script = [
        # turn 1: write a file, then stop -> engine runs verify, it FAILS
        CompletionResult(
            message=Message(role="assistant", content="", tool_calls=[write])
        ),
        CompletionResult(message=Message(role="assistant", content="wrote it")),
        # SPEC §5.5: the engine feeds the failure back once; the model responds,
        # stops again, and the engine surfaces the still-failing verify honestly
        CompletionResult(message=Message(role="assistant", content="acknowledged")),
    ]
    engine = TurnEngine(
        MockProvider(script),
        tools,
        settings,
        _profile(),
        Mode.ACCEPT_EDITS,
        workspace=tmp_path,
        verifier=CommandVerifier(commands=[_py(_FAIL_SRC)]),
        snapshots=None,
    )

    events: list = []

    async def _drive():
        async for ev in engine.run_turn("write out.txt"):
            events.append(ev)

    asyncio.run(_drive())

    text = "".join(getattr(ev, "text", "") for ev in events)
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x\n"  # the write happened
    assert "[verify]" in text  # engine surfaced the failure honestly
    assert "VERIFY_MARKER_TAIL" in text  # the failing command's tail rode along
    # the failure was fed back to the model once (SPEC §5.5): the corrective
    # prompt is in the conversation, and verify was surfaced on both stops
    assert any("Verification failed" in m.content for m in engine._conversation)
    assert text.count("[verify]") == 2
    # SAFETY T7: the engine must NOT report success on still-failing verification
    completed = [ev for ev in events if isinstance(ev, TurnCompleted)][-1]
    assert completed.stop_reason == "goal-unmet"


# --------------------------------------------------------------------------- #
# (6) the deny-list gate — a repo-borne verify command cannot auto-execute
# --------------------------------------------------------------------------- #


def _verify(verifier: CommandVerifier, workspace, state: SessionState) -> object:
    return asyncio.run(verifier.verify(workspace, Settings(), state, touched_files=True))


def test_deny_listed_verify_command_is_refused_not_executed(tmp_path):
    # A deny-listed token makes the WHOLE line refused. If the gate were missing,
    # the python prefix would create `proof` before the (failing) rm — so the
    # file's ABSENCE proves the command never ran.
    proof = tmp_path / "PROOF_IT_RAN.txt"
    command = f"{sys.executable} -c \"open(r'{proof}','w').write('x')\" && rm -rf /"
    result = _verify(CommandVerifier(commands=[command]), tmp_path, SessionState())
    assert result.ok is False  # fail-closed: unverifiable work is not reported done
    assert "deny-listed" in result.summary or "refused" in result.summary
    assert command not in result.ran  # excluded from ran → never executed
    assert not proof.exists()  # the side effect never happened


def test_risky_verify_command_skipped_unattended_in_auto(tmp_path):
    # `git push` is not deny-listed but matches a risky pattern; in AUTO there is
    # no human to approve it, so it is skipped rather than auto-run.
    result = _verify(
        CommandVerifier(commands=["git push origin main"]),
        tmp_path,
        SessionState(mode=Mode.AUTO),
    )
    assert result.ok is False
    assert "risky" in result.summary and "auto" in result.summary
    assert result.ran == []  # never executed


def test_clean_verify_command_still_runs_in_accept_edits(tmp_path):
    # regression guard: an ordinary (clean) verify command is NOT gated out in
    # the non-AUTO modes — the security fix must not break normal verification.
    result = _verify(
        CommandVerifier(commands=[_py(_PASS_SRC)]),
        tmp_path,
        SessionState(mode=Mode.ACCEPT_EDITS),
    )
    assert result.ok is True
    assert result.ran == [_py(_PASS_SRC)]


def test_engine_refuses_deny_listed_ironcore_md_verify(tmp_path):
    # THE THREAT: a cloned repo's IRONCORE.md carries a malicious verify: line
    # that would fire automatically after the first edit in accept-edits/auto.
    proof = tmp_path / "PWNED.txt"
    md_cmd = f"{sys.executable} -c \"open(r'{proof}','w').write('x')\" && rm -rf /"
    (tmp_path / "IRONCORE.md").write_text(f"verify: {md_cmd}\n", encoding="utf-8")

    settings = Settings()
    tools = build_default_registry(settings, tmp_path)
    write = ToolCall(id="c1", name="write_file", arguments={"path": "out.txt", "content": "x\n"})
    script = [
        CompletionResult(message=Message(role="assistant", content="", tool_calls=[write])),
        CompletionResult(message=Message(role="assistant", content="done")),
        CompletionResult(message=Message(role="assistant", content="acknowledged")),
    ]
    engine = TurnEngine(
        MockProvider(script),
        tools,
        settings,
        _profile(),
        Mode.ACCEPT_EDITS,
        workspace=tmp_path,
        snapshots=None,  # default CommandVerifier() → discovers the IRONCORE.md line
    )
    events: list = []

    async def _drive():
        async for ev in engine.run_turn("write out.txt"):
            events.append(ev)

    asyncio.run(_drive())

    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x\n"  # the edit happened
    assert not proof.exists()  # the malicious verify line NEVER executed
    text = "".join(getattr(ev, "text", "") for ev in events)
    assert "[verify]" in text and ("deny-listed" in text or "refused" in text)
    completed = [ev for ev in events if isinstance(ev, TurnCompleted)][-1]
    assert completed.stop_reason == "goal-unmet"  # not reported done on an unverifiable turn


# --------------------------------------------------------------------------- #
# (7) /goal verify: arms the engine's in-turn stop-condition (goal-attached)
# --------------------------------------------------------------------------- #


def test_goal_attached_verify_beats_markers(tmp_path):
    # a workspace whose markers would auto-detect `pytest -q`…
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    # …but a goal-attached check takes priority and is what actually runs.
    state = SessionState(goal="be green", goal_verify=[_py(_PASS_SRC)])
    result = _verify(CommandVerifier(), tmp_path, state)  # engine's default verifier
    assert result.ok is True
    assert result.ran == [_py(_PASS_SRC)]  # the goal command ran, not pytest
    assert "(goal)" in result.summary


def test_engine_arms_goal_stop_condition_without_markers(tmp_path):
    # Empty workspace → no verify markers; without a goal check the engine would
    # report "done". An attached FAILING check must instead hold the turn open.
    settings = Settings()
    tools = build_default_registry(settings, tmp_path)
    write = ToolCall(id="c1", name="write_file", arguments={"path": "out.txt", "content": "x\n"})
    script = [
        CompletionResult(message=Message(role="assistant", content="", tool_calls=[write])),
        CompletionResult(message=Message(role="assistant", content="done")),
        CompletionResult(message=Message(role="assistant", content="acknowledged")),
    ]
    session = SessionState(goal="ship it", goal_verify=[_py(_FAIL_SRC)])
    engine = TurnEngine(
        MockProvider(script),
        tools,
        settings,
        _profile(),
        Mode.ACCEPT_EDITS,
        workspace=tmp_path,
        session=session,
        snapshots=None,
    )
    events: list = []

    async def _drive():
        async for ev in engine.run_turn("write out.txt"):
            events.append(ev)

    asyncio.run(_drive())

    text = "".join(getattr(ev, "text", "") for ev in events)
    assert "[verify]" in text and "VERIFY_MARKER_TAIL" in text  # the goal check ran + failed
    completed = [ev for ev in events if isinstance(ev, TurnCompleted)][-1]
    assert completed.stop_reason == "goal-unmet"  # armed: won't call itself done
