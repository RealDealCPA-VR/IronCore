"""/review (IC-806): working-diff collection + rubric review + finding parsing."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from ironcore.commands.base import CommandContext
from ironcore.commands.reviewcmd import _NO_FINDINGS, _cmd_review, _format_findings
from ironcore.config.settings import Settings
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider

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


requires_git = pytest.mark.skipif(not _has_git(), reason="git required for /review")


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
    def __init__(self, provider):
        self.provider = provider


def _ctx(ws: Path, provider, schedule) -> CommandContext:
    ctx = CommandContext(settings=Settings())
    ctx.extra.update(workspace=str(ws), engine=_Engine(provider), schedule=schedule)
    return ctx


# -- end to end (needs git) ---------------------------------------------------


@requires_git
def test_review_reports_findings(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(ws, "init", "-q")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "base")
    (ws / "a.py").write_text("def f():\n    return 1 / 0\n", encoding="utf-8")  # working change

    schedule, captured = _sync_schedule()
    findings = "a.py:2 high — division by zero\n"
    provider = MockProvider([CompletionResult(message=Message(role="assistant", content=findings))])
    ack = _cmd_review(_ctx(ws, provider, schedule), "")
    assert "Reviewing" in ack
    assert captured and "1 finding" in captured[0]
    assert "a.py:2" in captured[0]
    assert provider.calls  # the model was actually consulted


@requires_git
def test_review_clean_tree_needs_no_model(tmp_path):
    ws = tmp_path / "clean"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(ws, "init", "-q")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "base")

    schedule, captured = _sync_schedule()
    provider = MockProvider()  # empty script: must not be called
    _cmd_review(_ctx(ws, provider, schedule), "")
    assert captured and "nothing to review" in captured[0].lower()
    assert not provider.calls


def test_review_outside_git_repo(tmp_path):
    schedule, captured = _sync_schedule()
    provider = MockProvider()
    _cmd_review(_ctx(tmp_path, provider, schedule), "")
    assert captured and "Not a git repository" in captured[0]
    assert not provider.calls


# -- finding formatting (pure) ------------------------------------------------


def test_format_no_findings():
    assert "no bug findings" in _format_findings(_NO_FINDINGS).lower()


def test_format_parses_finding_lines():
    out = _format_findings("- a.py:10 high — leak\nb.py:3 low — typo\nsome prose")
    assert "2 findings" in out
    assert "a.py:10" in out and "b.py:3" in out


def test_format_degrades_to_raw_text():
    out = _format_findings("The code looks fine but consider clearer naming.")
    assert "unstructured" in out
    assert "clearer naming" in out


def test_review_without_scheduler_reports():
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = "/tmp/nope"
    assert "scheduler" in _cmd_review(ctx, "")
