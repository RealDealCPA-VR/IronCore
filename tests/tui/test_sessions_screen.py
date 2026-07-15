"""IC-706: session picker + record/resume — pure helpers + headless Pilot tests.

Covers the pure age/label helpers, the picker modal (lists newest-first, returns
the chosen id, empty-state cancels), live recording of a turn's user+assistant
lines into an injected store, the ``--resume`` rehydrate flow, and the CLI flag.
Async is driven with ``asyncio.run`` wrapping ``async with app.run_test()``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.widgets import ListItem, ListView

from ironcore.commands import build_default_registry as build_cmds
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.memory.sessions import SessionRecord, SessionStore
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tui.app import IronCoreApp
from ironcore.tui.screens.sessions import SessionPicker, _row_label, relative_age
from ironcore.tui.widgets import InputBar

FIXED_NOW = datetime(2026, 7, 15, 12, 0, 0)


# --------------------------------------------------------------------------- #
# builders (mirrors tests/tui/test_app.py)
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _engine(tmp_path, script) -> TurnEngine:
    settings = Settings.model_validate({})
    tools = build_tools(settings, tmp_path)
    return TurnEngine(
        MockProvider(list(script)),
        tools,
        settings,
        _profile(),
        Mode.MANUAL,
        workspace=tmp_path,
        snapshots=None,
    )


def _app(engine: TurnEngine, **kwargs) -> IronCoreApp:
    return IronCoreApp(engine, build_cmds(), engine.settings, **kwargs)


async def _submit(app: IronCoreApp, pilot, text: str) -> None:
    inp = app.query_one(InputBar)
    inp.value = text
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for(pilot, predicate, tries: int = 120) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause()
    return False


# --------------------------------------------------------------------------- #
# (1) pure helpers: relative age + row label
# --------------------------------------------------------------------------- #


def test_relative_age_buckets():
    assert relative_age("2026-07-15T11:59:30", FIXED_NOW) == "just now"
    assert relative_age("2026-07-15T11:30:00", FIXED_NOW) == "30m ago"
    assert relative_age("2026-07-15T09:00:00", FIXED_NOW) == "3h ago"
    assert relative_age("2026-07-13T12:00:00", FIXED_NOW) == "2d ago"
    assert relative_age("not-a-date", FIXED_NOW) == "?"


def test_row_label_shows_age_prompt_and_turns(tmp_path):
    rec = SessionRecord(
        id="x",
        created_at="2026-07-15T11:00:00",
        turn_count=3,
        first_prompt="fix the parser bug",
        path=tmp_path / "x.jsonl",
    )
    label = _row_label(rec, now=FIXED_NOW)
    assert "1h ago" in label
    assert "fix the parser bug" in label
    assert "3 turn(s)" in label


def test_row_label_handles_empty_prompt(tmp_path):
    rec = SessionRecord(
        id="x", created_at="2026-07-15T11:00:00", turn_count=0, first_prompt="", path=tmp_path
    )
    assert "(no prompt)" in _row_label(rec, now=FIXED_NOW)


# --------------------------------------------------------------------------- #
# (2) picker: lists newest-first, selection returns the id
# --------------------------------------------------------------------------- #


def test_picker_lists_newest_first_and_selects(tmp_path):
    store = SessionStore(tmp_path)
    store.create("aaa", "2026-07-15T10:00:00", "older prompt")
    store.create("bbb", "2026-07-15T11:00:00", "newer prompt")
    store.append_user("bbb", "newer prompt")  # gives bbb turn_count 1
    app = _app(_engine(tmp_path, [_text("hi")]))

    async def scenario():
        async with app.run_test() as pilot:
            picked: list[str | None] = []
            app.push_screen(SessionPicker(store), picked.append)
            await _wait_for(pilot, lambda: app.screen.query(ListItem))
            names = [item.name for item in app.screen.query(ListItem)]
            assert names == ["bbb", "aaa"]  # newest-first
            # the newest row is highlighted; Enter selects it
            await pilot.press("enter")
            await _wait_for(pilot, lambda: picked)
            assert picked[0] == "bbb"

    asyncio.run(scenario())


def test_picker_empty_state_cancels(tmp_path):
    store = SessionStore(tmp_path)  # nothing created
    app = _app(_engine(tmp_path, [_text("hi")]))

    async def scenario():
        async with app.run_test() as pilot:
            picked: list[str | None] = []
            app.push_screen(SessionPicker(store), picked.append)
            await _wait_for(pilot, lambda: app.screen.query("#session-empty"))
            assert app.screen.query("#session-empty")  # empty-state shown
            assert not app.screen.query(ListView)  # and no list
            await pilot.press("escape")  # cancels cleanly
            await _wait_for(pilot, lambda: picked)
            assert picked[0] is None

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (3) recording: a live turn's user + assistant lines land in the store
# --------------------------------------------------------------------------- #


def test_app_records_live_turn(tmp_path):
    store = SessionStore(tmp_path)
    engine = _engine(tmp_path, [_text("Hello from the model.")])
    app = _app(engine, session_store=store)

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "hi there")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())

    records = store.list_sessions()
    assert len(records) == 1
    assert records[0].first_prompt == "hi there"  # header label from first turn
    messages, _ = store.rehydrate(records[0].id)
    pairs = [(m.role, m.content) for m in messages]
    assert ("user", "hi there") in pairs
    assert ("assistant", "Hello from the model.") in pairs


def test_no_store_disables_recording(tmp_path):
    # Default (no store injected) must not create any session files.
    app = _app(_engine(tmp_path, [_text("ok")]))

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "no recording please")
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert not SessionStore(tmp_path).list_sessions()


# --------------------------------------------------------------------------- #
# (4) resume: rehydrated tail seeds the transcript, then writing continues
# --------------------------------------------------------------------------- #


def test_resume_seeds_transcript_and_continues(tmp_path):
    store = SessionStore(tmp_path)
    store.create("sess", "2026-07-15T10:00:00", "original question")
    store.append_user("sess", "original question")
    store.append_assistant("sess", "the original answer")
    engine = _engine(tmp_path, [_text("continued reply")])
    app = _app(engine, session_store=store, resume_id="sess")

    async def scenario():
        async with app.run_test() as pilot:
            seeded = await _wait_for(
                pilot, lambda: "the original answer" in app.transcript_text()
            )
            assert seeded, "rehydrated assistant tail never appeared"
            assert "original question" in app.transcript_text()
            assert "resumed session sess" in app.transcript_text()  # tail summary note
            # a new turn continues the SAME session file
            await _submit(app, pilot, "next question")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())

    assert [r.id for r in store.list_sessions()] == ["sess"]  # no new session created
    contents = [m.content for m in store.rehydrate("sess")[0]]
    assert contents == [
        "original question",
        "the original answer",
        "next question",
        "continued reply",
    ]


def test_resume_pick_opens_picker_at_launch(tmp_path):
    from ironcore.tui.app import RESUME_PICK

    store = SessionStore(tmp_path)
    store.create("sess", "2026-07-15T10:00:00", "prior work")
    app = _app(_engine(tmp_path, [_text("hi")]), session_store=store, resume_id=RESUME_PICK)

    async def scenario():
        async with app.run_test() as pilot:
            shown = await _wait_for(pilot, lambda: isinstance(app.screen, SessionPicker))
            assert shown, "picker did not open at launch"
            await pilot.press("escape")  # cancel → drop to a fresh session
            gone = await _wait_for(pilot, lambda: not isinstance(app.screen, SessionPicker))
            assert gone

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (5) --resume is parsed by build_parser (TTY-gating lives in main())
# --------------------------------------------------------------------------- #


def test_resume_flag_parsed():
    from ironcore.cli import RESUME_PICK, build_parser

    assert build_parser().parse_args([]).resume is None
    assert build_parser().parse_args(["--resume"]).resume == RESUME_PICK
    assert build_parser().parse_args(["--resume", "abc123"]).resume == "abc123"
    # additive: --version still parses alongside the new flag
    assert build_parser().parse_args(["--version"]).version is True
