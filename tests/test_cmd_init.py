"""/init (IC-802): detect the project and write/refresh IRONCORE.md."""

from pathlib import Path

from ironcore.commands.base import CommandContext
from ironcore.commands.initcmd import (
    IRONCORE_MD,
    USER_END,
    USER_START,
    _cmd_init,
    detect_project,
)
from ironcore.config.settings import Settings
from ironcore.core.verify import CommandVerifier


def _ctx(workspace: Path) -> CommandContext:
    ctx = CommandContext(settings=Settings())
    ctx.extra["workspace"] = workspace
    return ctx


def test_init_python_project_writes_sections(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    out = _cmd_init(_ctx(tmp_path), "")
    md = (tmp_path / IRONCORE_MD).read_text(encoding="utf-8")
    assert "IronCore project memory" in md
    assert "Python" in md
    assert "pytest -q" in md
    assert "## Verify" in md
    assert USER_START in md and USER_END in md
    assert "Created" in out


def test_init_verify_section_is_read_by_the_verifier(tmp_path):
    # The generated ## Verify section is the exact shape CommandVerifier parses,
    # so /init's detected test command is what the engine actually runs.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    _cmd_init(_ctx(tmp_path), "")
    commands, source = CommandVerifier().discover(tmp_path)
    assert commands == ["pytest -q"]
    assert source == "ironcore.md"


def test_init_preserves_user_section_on_rerun(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest", "build": "webpack"}}', encoding="utf-8"
    )
    _cmd_init(_ctx(tmp_path), "")
    md_path = tmp_path / IRONCORE_MD
    text = md_path.read_text(encoding="utf-8")
    text = text.replace(USER_END, "MY CUSTOM NOTE\n" + USER_END)
    md_path.write_text(text, encoding="utf-8")

    out = _cmd_init(_ctx(tmp_path), "")
    md = md_path.read_text(encoding="utf-8")
    assert "MY CUSTOM NOTE" in md
    assert "Refreshed" in out
    assert "npm test" in md
    assert "npm run build" in md


def test_detect_rust_and_go(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n", encoding="utf-8")
    info = detect_project(tmp_path)
    assert "Rust" in info.languages
    assert "cargo build" in info.build_commands
    assert "cargo test" in info.test_commands

    go_ws = tmp_path / "go"
    go_ws.mkdir()
    (go_ws / "go.mod").write_text("module x\n", encoding="utf-8")
    go_info = detect_project(go_ws)
    assert "Go" in go_info.languages
    assert "go test ./..." in go_info.test_commands


def test_init_empty_workspace_still_writes(tmp_path):
    out = _cmd_init(_ctx(tmp_path), "")
    md = (tmp_path / IRONCORE_MD).read_text(encoding="utf-8")
    assert "none recognized" in md
    assert "Created" in out
    # No test command detected -> no ## Verify section overrides the verifier.
    assert "## Verify" not in md


def test_init_structure_map_lists_top_level(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()  # ignored
    _cmd_init(_ctx(tmp_path), "")
    md = (tmp_path / IRONCORE_MD).read_text(encoding="utf-8")
    assert "- src/" in md
    assert "- README.md" in md
    assert "node_modules" not in md
