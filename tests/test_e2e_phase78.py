"""End-to-end proof of outcome for phases 7 (TUI) and 8 (slash commands).

Two angles, both against real subsystems:
  * the actual Textual app driven headless through a realistic session
    (stream -> tool card -> Shift+Tab mode -> approval deny -> session record);
  * the phase-8 slash commands dispatched through the REAL registry against
    real subsystems — the /init -> CommandVerifier round-trip proves a command
    actually configures what the engine later runs, not just prints text.
No network, no model beyond a scripted MockProvider.
"""

from __future__ import annotations

import asyncio
import subprocess

from ironcore.commands import build_default_registry as build_cmds
from ironcore.commands.base import CommandContext
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.verify import CommandVerifier
from ironcore.envelope.profile import CapabilityProfile
from ironcore.memory.sessions import SessionStore
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import CYCLE, Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tui.app import IronCoreApp
from ironcore.tui.screens.approval import ApprovalScreen
from ironcore.tui.widgets import InputBar, StatusBar, Transcript


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str, calls=None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _engine(tmp_path, script, *, mode=Mode.MANUAL):
    settings = Settings()
    tools = build_tools(settings, tmp_path)
    return TurnEngine(
        MockProvider(list(script)), tools, settings, _profile(), mode,
        workspace=tmp_path, snapshots=None,
    )


async def _submit(app, pilot, text):
    inp = app.query_one(InputBar)
    inp.value = text
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for(pilot, predicate, tries=120):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause()
    return False


# --------------------------------------------------------------------------- #
# 1. A realistic TUI session driven end to end
# --------------------------------------------------------------------------- #


def test_tui_session_streams_records_and_cycles_modes(tmp_path):
    store = SessionStore(tmp_path)
    engine = _engine(tmp_path, [_text("Here is my answer.")])
    app = IronCoreApp(engine, build_cmds(), engine.settings, session_store=store)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            # three regions mounted
            assert app.query_one(Transcript) is not None
            assert app.query_one(InputBar) is not None
            assert app.query_one(StatusBar) is not None
            assert "[MANUAL]" in app.status_bar._plain

            # a streamed text turn lands in the transcript
            await _submit(app, pilot, "what is 2+2")
            await app.workers.wait_for_complete()
            await pilot.pause()
            transcript = app.transcript_text()
            assert "what is 2+2" in transcript
            assert "Here is my answer." in transcript

            # Shift+Tab cycles the mode chip to the next mode
            await pilot.press("shift+tab")
            await pilot.pause()
            nxt = CYCLE[(CYCLE.index(Mode.MANUAL) + 1) % len(CYCLE)]
            assert f"[{nxt.value.upper()}]" in app.status_bar._plain

    asyncio.run(scenario())

    # the session was recorded to disk and rehydrates the user turn
    sessions = store.list_sessions()
    assert len(sessions) == 1
    messages, _tail = store.rehydrate(sessions[0].id)
    assert any(m.role == "user" and "what is 2+2" in m.content for m in messages)


def test_tui_approval_modal_denies_a_write(tmp_path):
    write = ToolCall(id="c1", name="write_file", arguments={"path": "x.txt", "content": "hi"})
    engine = _engine(tmp_path, [_text("", [write]), _text("cannot then")], mode=Mode.MANUAL)
    app = IronCoreApp(engine, build_cmds(), engine.settings)

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "write x.txt")
            # the write is ASK-gated in MANUAL -> the approval modal appears
            assert await _wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("n")  # deny
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())
    assert not (tmp_path / "x.txt").exists()  # denial really prevented the write


def test_tui_slash_help_lists_commands(tmp_path):
    engine = _engine(tmp_path, [_text("unused")])
    app = IronCoreApp(engine, build_cmds(), engine.settings)

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/help")
            await pilot.pause()
            text = app.transcript_text()
            assert "/goal" in text and "/undo" in text  # dispatched through the registry

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 2. Phase-8 commands through the real registry + real subsystems
# --------------------------------------------------------------------------- #


def _ctx(tmp_path, *, engine=None, captured=None):
    def schedule(coro):
        (captured if captured is not None else []).append(asyncio.run(coro))

    return CommandContext(
        settings=Settings(),
        extra={
            "workspace": tmp_path,
            "engine": engine,
            "registry": build_cmds(),
            "settings": Settings(),
            "schedule": schedule,
        },
    )


def test_init_writes_ironcore_md_that_the_verifier_actually_reads(tmp_path):
    # a python project -> /init should detect pytest and write a Verify section
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (tmp_path / "tests").mkdir()
    registry = build_cmds()

    out = registry.dispatch("/init", _ctx(tmp_path))
    assert "IRONCORE.md" in out or "wrote" in out.lower()
    md = (tmp_path / "IRONCORE.md").read_text()
    assert "## Verify" in md

    # the cross-subsystem proof: the engine's verifier discovers what /init wrote
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands  # a real command was discovered
    assert source == "ironcore.md"
    assert any("pytest" in c for c in commands)


def test_init_preserves_the_user_section_across_reruns(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    registry = build_cmds()
    registry.dispatch("/init", _ctx(tmp_path))
    # a user edits their section, then re-runs /init
    md_path = tmp_path / "IRONCORE.md"
    md = md_path.read_text()
    marked = md.replace(
        "<!-- IRONCORE:USER start -->",
        "<!-- IRONCORE:USER start -->\nMY OWN NOTE: deploy with make ship\n",
        1,
    )
    md_path.write_text(marked)
    registry.dispatch("/init", _ctx(tmp_path))
    assert "MY OWN NOTE: deploy with make ship" in md_path.read_text()  # preserved


def test_goal_sets_the_engine_anchor(tmp_path):
    engine = _engine(tmp_path, [_text("noted")])
    registry = build_cmds()
    registry.dispatch("/goal ship the release", _ctx(tmp_path, engine=engine))
    assert engine.state.goal == "ship the release"  # the composer will anchor this


def test_memory_add_appends_to_the_user_section(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    registry = build_cmds()
    registry.dispatch("/init", _ctx(tmp_path))
    registry.dispatch("/memory add remember the staging URL", _ctx(tmp_path))
    md = (tmp_path / "IRONCORE.md").read_text()
    assert "remember the staging URL" in md


def test_undo_restores_a_real_git_edit(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "d@e.com")
    git("config", "user.name", "D")
    git("config", "core.autocrlf", "false")
    (tmp_path / "f.txt").write_text("v1\n", newline="")
    git("add", "-A")
    git("commit", "-m", "init")

    from ironcore.safety.snapshots import SnapshotStore

    SnapshotStore(tmp_path).snapshot("before edit")
    (tmp_path / "f.txt").write_text("v2 changed\n", newline="")

    out = build_cmds().dispatch("/undo", _ctx(tmp_path))
    assert isinstance(out, str) and out  # a human-readable result, no crash
    assert (tmp_path / "f.txt").read_text() == "v1\n"  # the edit was undone


def test_every_command_survives_a_thin_context(tmp_path):
    # a command handler must never crash on a missing dependency — it returns
    # a clear message instead. Give it only a safe tmp workspace.
    registry = build_cmds()
    for cmd in registry.all():
        if not cmd.implemented:
            continue
        extra = {"workspace": tmp_path, "registry": registry}
        ctx = CommandContext(settings=Settings(), extra=extra)
        result = registry.dispatch(f"/{cmd.name}", ctx)  # no args, minimal extra
        assert isinstance(result, str) and result  # graceful, non-empty


# --------------------------------------------------------------------------- #
# 3. Production wiring: the app builds from settings with no network
# --------------------------------------------------------------------------- #


def test_app_constructs_from_settings(tmp_path):
    # from_settings wires provider registry + tools + profile + engine for the
    # real `ironcore` launch — prove it constructs without a model or network.
    app = IronCoreApp.from_settings(Settings(), tmp_path)
    assert app is not None
    assert app.query_one is not None  # a real Textual App instance
