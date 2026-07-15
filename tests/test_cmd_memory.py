"""/memory (IC-807): view IRONCORE.md sections and append to the user section."""

from pathlib import Path

from ironcore.commands.base import CommandContext
from ironcore.commands.initcmd import IRONCORE_MD, _cmd_init
from ironcore.commands.memorycmd import _cmd_memory
from ironcore.config.settings import Settings


def _ctx(workspace: Path) -> CommandContext:
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = workspace
    return ctx


def _init(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    _cmd_init(_ctx(tmp_path), "")


def test_memory_view_without_file(tmp_path):
    assert "run /init" in _cmd_memory(_ctx(tmp_path), "")


def test_memory_view_whole_file(tmp_path):
    _init(tmp_path)
    out = _cmd_memory(_ctx(tmp_path), "")
    assert "IronCore project memory" in out


def test_memory_add_appends_and_survives_reinit(tmp_path):
    _init(tmp_path)
    out = _cmd_memory(_ctx(tmp_path), "add prefer ruff over flake8")
    assert "Added" in out
    md = (tmp_path / IRONCORE_MD).read_text(encoding="utf-8")
    assert "prefer ruff over flake8" in md
    # A re-init must preserve the note (it lives in the sentinel-guarded section).
    _cmd_init(_ctx(tmp_path), "")
    assert "prefer ruff over flake8" in (tmp_path / IRONCORE_MD).read_text(encoding="utf-8")


def test_memory_add_requires_text(tmp_path):
    _init(tmp_path)
    assert "Usage" in _cmd_memory(_ctx(tmp_path), "add")


def test_memory_add_without_file(tmp_path):
    assert "run /init" in _cmd_memory(_ctx(tmp_path), "add something")


def test_memory_section_view(tmp_path):
    _init(tmp_path)
    assert "## Build" in _cmd_memory(_ctx(tmp_path), "build")
    assert "## Structure" in _cmd_memory(_ctx(tmp_path), "show structure")


def test_memory_unknown_section_lists_available(tmp_path):
    _init(tmp_path)
    out = _cmd_memory(_ctx(tmp_path), "nonsense")
    assert "Sections:" in out
    assert "Overview" in out
