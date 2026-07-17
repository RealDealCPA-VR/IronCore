"""/compact, /undo, /redo (IC-805): compaction ack + byte-exact snapshot revert."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from ironcore.commands.base import CommandContext
from ironcore.commands.lifecyclecmd import _cmd_compact, _cmd_redo, _cmd_undo
from ironcore.config.settings import Settings
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider
from ironcore.safety.snapshots import SnapshotStore

_GIT_FLAGS = [
    "-c", "user.email=ci@example.invalid",
    "-c", "user.name=IronCore CI",
    "-c", "core.autocrlf=false",
    "-c", "commit.gpgsign=false",
]


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True)
        return True
    except OSError:
        return False


requires_git = pytest.mark.skipif(not _has_git(), reason="git required for snapshot commands")


def _git(ws: Path, *args: str) -> None:
    cmd = ["git", "-C", str(ws), *_GIT_FLAGS, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _sync_schedule():
    captured: list[str] = []

    def schedule(coro):
        captured.append(asyncio.run(coro))

    return schedule, captured


class _Engine:
    def __init__(self, conversation, provider, profile):
        self._conversation = conversation
        self.provider = provider
        self.profile = profile


# -- /compact -----------------------------------------------------------------


def _summary_result() -> CompletionResult:
    text = (
        "Context: x\nChanged: y\nVerified: not verified\nNext: z\nGotchas: none\n"
    )
    return CompletionResult(message=Message(role="assistant", content=text))


def test_compact_schedules_and_replaces_conversation():
    schedule, captured = _sync_schedule()
    conversation = [Message(role="user", content=f"message {i}") for i in range(12)]
    engine = _Engine(
        conversation,
        MockProvider([_summary_result()]),
        CapabilityProfile(model_id="m", honest_context=4096),
    )
    ctx = CommandContext(settings=Settings())
    ctx.extra.update(engine=engine, schedule=schedule)
    ack = _cmd_compact(ctx, "")
    assert "Compacting 12" in ack
    assert captured and "Compacted 12" in captured[0]
    assert len(engine._conversation) == 7  # 1 summary + 6 recent
    assert engine._conversation[0].content.startswith("# Compacted history")


def test_compact_routes_to_the_summarizer_role():
    # MS-3: when the engine carries a RoleRouter that routes the summarizer,
    # /compact's model call goes to THAT provider, not the engine's primary.
    from ironcore.core.roles import RoleRouter

    schedule, captured = _sync_schedule()
    conversation = [Message(role="user", content=f"message {i}") for i in range(12)]
    primary = MockProvider()  # must receive nothing
    summarizer = MockProvider([_summary_result()])
    settings = Settings.model_validate(
        {"provider": {"model": "big"}, "roles": {"summarizer": "small-8b"}}
    )
    engine = _Engine(
        conversation, primary, CapabilityProfile(model_id="big", honest_context=4096)
    )
    engine.roles = RoleRouter(
        settings,
        providers={"summarizer": summarizer},
        profiles={"small-8b": CapabilityProfile(model_id="small-8b")},
    )
    ctx = CommandContext(settings=settings)
    ctx.extra.update(engine=engine, schedule=schedule)
    assert "Compacting 12" in _cmd_compact(ctx, "")
    assert captured and "Compacted 12" in captured[0]
    assert len(summarizer.calls) == 1 and primary.calls == []
    assert engine._conversation[0].content.startswith("# Compacted history")


def test_compact_without_engine():
    ctx = CommandContext(settings=Settings())
    assert "no engine" in _cmd_compact(ctx, "").lower()


def test_compact_empty_conversation():
    engine = _Engine([], MockProvider(), CapabilityProfile(model_id="m"))
    ctx = CommandContext(settings=Settings())
    ctx.extra.update(engine=engine, schedule=lambda coro: None)
    assert "Nothing to compact" in _cmd_compact(ctx, "")


# -- /undo and /redo ----------------------------------------------------------


def _ws_ctx(ws: Path) -> CommandContext:
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = str(ws)
    return ctx


@requires_git
def test_undo_then_redo_round_trips(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(ws, "init", "-q")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "base")

    store = SnapshotStore(ws)
    store.snapshot("s1")  # captures v1
    (ws / "a.txt").write_text("v2\n", encoding="utf-8")
    store.snapshot("s2")  # captures v2

    ctx = _ws_ctx(ws)
    out = _cmd_undo(ctx, "")
    assert "Reverted to" in out and "s1" in out
    assert (ws / "a.txt").read_text(encoding="utf-8") == "v1\n"

    out2 = _cmd_redo(ctx, "")
    assert "Reapplied" in out2 and "s2" in out2
    assert (ws / "a.txt").read_text(encoding="utf-8") == "v2\n"


@requires_git
def test_undo_with_no_snapshots(tmp_path):
    ws = tmp_path / "empty"
    ws.mkdir()
    (ws / "f.txt").write_text("x\n", encoding="utf-8")
    _git(ws, "init", "-q")
    assert "No snapshots" in _cmd_undo(_ws_ctx(ws), "")


@requires_git
def test_redo_with_nothing_to_redo(tmp_path):
    ws = tmp_path / "repo2"
    ws.mkdir()
    (ws / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(ws, "init", "-q")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "base")
    store = SnapshotStore(ws)
    store.snapshot("s1")
    assert "Nothing to redo" in _cmd_redo(_ws_ctx(ws), "")


def test_undo_without_workspace():
    ctx = CommandContext(settings=Settings())
    assert "No workspace" in _cmd_undo(ctx, "")
