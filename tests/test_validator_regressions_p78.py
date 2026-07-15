"""Regression pins for the phase-7/8 adversarial validation round (2026-07-16)."""

from __future__ import annotations

import asyncio

from ironcore.commands import build_default_registry as build_cmds
from ironcore.commands.base import CommandContext
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.memory.sessions import SessionStore
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tui.app import IronCoreApp
from ironcore.tui.widgets import InputBar

# --- MAJOR 1: /help must not crash on an empty ctx.extra (keys are optional)


def test_finding1_help_survives_empty_context():
    registry = build_cmds()
    out = registry.dispatch("/help", CommandContext(settings=Settings(), extra={}))
    assert "/goal" in out and "/help" in out  # listed via the fallback registry


# --- MAJOR 2: --resume with a nonexistent id must not create an orphan session


def _engine(tmp_path, script):
    settings = Settings()
    return TurnEngine(
        MockProvider(list(script)),
        build_tools(settings, tmp_path),
        settings,
        CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0}),
        Mode.AUTO,
        workspace=tmp_path,
        snapshots=None,
    )


def test_finding2_resume_bad_id_starts_fresh_not_an_orphan(tmp_path):
    store = SessionStore(tmp_path)
    engine = _engine(tmp_path, [CompletionResult(message=Message(role="assistant", content="hi"))])
    app = IronCoreApp(
        engine, build_cmds(), engine.settings, session_store=store, resume_id="does-not-exist-123"
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            inp = app.query_one(InputBar)
            inp.value = "first real turn"
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(scenario())

    # the fresh turn created a PROPER, listable session (header first) — not a
    # headerless orphan under the typo'd id
    sessions = store.list_sessions()
    assert all(s.id != "does-not-exist-123" for s in sessions)
    # whatever session was created is well-formed and rehydrates the real turn
    if sessions:
        messages, _ = store.rehydrate(sessions[0].id)
        assert any(m.role == "user" and "first real turn" in m.content for m in messages)
    # the bogus id never became a file
    assert not store.path_for("does-not-exist-123").exists()
