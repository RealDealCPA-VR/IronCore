"""Skills — the SKILL.md open standard (PKG-4).

Discovery from user + project (+ compat) dirs, frontmatter parse + malformed
skip, the trusted catalog + tiny-context budget degrade, /skill injection + the
project first-use confirmation, the use_skill READ tool + its gate, and the hard
boundary that a skill's scripts still hit the EXEC gate. All offline: fixtures
are files under pytest tmp dirs; nothing touches the network or a real model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ironcore.commands.base import CommandContext
from ironcore.commands.skillcmd import COMMANDS as SKILL_COMMANDS
from ironcore.config.settings import Settings
from ironcore.core.composer import (
    RESPONSE_HEADROOM_SHARE,
    SYSTEM_SHARE,
    compose,
    estimate_tokens,
)
from ironcore.core.state import SessionState
from ironcore.envelope.profile import CapabilityProfile
from ironcore.safety.modes import Mode
from ironcore.safety.policy import decide
from ironcore.safety.risk import ToolRisk
from ironcore.skills import (
    MAX_DESCRIPTION_CHARS,
    discover_skills,
    is_confirmed,
    load_skills_catalog,
    mark_confirmed,
)
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tools.shell import ShellTool
from ironcore.tools.skill import UseSkillTool

_SKILL_HANDLER = SKILL_COMMANDS[0].handler


def _write_skill(
    root: Path,
    dirname: str,
    *,
    name: str | None = "a-skill",
    description: str = "does a thing",
    body: str = "Do the thing, step by step.",
    frontmatter: str | None = None,
) -> Path:
    """Create ``root/dirname/SKILL.md`` and return its path. ``frontmatter``
    (raw text) overrides the generated YAML for malformed-input tests."""
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    if frontmatter is not None:
        text = frontmatter
    else:
        lines = ["---"]
        if name is not None:
            lines.append(f"name: {name}")
        lines.append(f"description: {description}")
        lines.append("---")
        lines.append("")
        lines.append(body)
        text = "\n".join(lines)
    path = skill_dir / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def _settings(*, enabled: bool = True, compat: bool = False) -> Settings:
    s = Settings()
    s.skills.enabled = enabled
    s.skills.compat_dirs = compat
    return s


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """(user_home, workspace) — separate trees so sources are unambiguous."""
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    return home, ws


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_discovers_user_and_project_skills(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(home / ".ironcore" / "skills", "mine", name="mine", description="user one")
    _write_skill(ws / ".ironcore" / "skills", "repo", name="repo", description="project one")

    loaded = discover_skills(ws, _settings(), user_home=home)

    by_name = {s.name: s for s in loaded.skills}
    assert set(by_name) == {"mine", "repo"}
    assert by_name["mine"].source == "user"
    assert by_name["repo"].source == "project"
    assert not loaded.skipped


def test_frontmatter_parse_and_body(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(
        ws / ".ironcore" / "skills",
        "pdf",
        name="pdf-tools",
        description="Extract text from PDFs.",
        body="# PDF tools\n\n1. Read the file.\n2. Extract.",
    )
    loaded = discover_skills(ws, _settings(), user_home=home)
    skill = loaded.find("pdf-tools")
    assert skill is not None
    assert skill.name == "pdf-tools"
    assert skill.description == "Extract text from PDFs."
    assert skill.body == "# PDF tools\n\n1. Read the file.\n2. Extract."


def test_compat_dirs_off_by_default_then_on(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(ws / ".claude" / "skills", "cc", name="claude-skill")

    off = discover_skills(ws, _settings(compat=False), user_home=home)
    assert off.find("claude-skill") is None

    on = discover_skills(ws, _settings(compat=True), user_home=home)
    found = on.find("claude-skill")
    assert found is not None and found.source == "project"


def test_malformed_skill_md_is_skipped_not_crash(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    root = ws / ".ironcore" / "skills"
    _write_skill(root, "good", name="good")
    _write_skill(root, "nofm", frontmatter="just a body, no frontmatter at all")
    _write_skill(root, "badyaml", frontmatter="---\nname: [unclosed\n---\nbody")
    _write_skill(root, "unterminated", frontmatter="---\nname: x\nbody with no closing fence")

    loaded = discover_skills(ws, _settings(), user_home=home)

    assert [s.name for s in loaded.skills] == ["good"]  # the valid one still loads
    assert len(loaded.skipped) == 3
    assert all(s.reason for s in loaded.skipped)  # each skip has a reason


def test_description_is_capped(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(ws / ".ironcore" / "skills", "big", name="big", description="x" * 999)
    loaded = discover_skills(ws, _settings(), user_home=home)
    skill = loaded.find("big")
    assert skill is not None
    assert len(skill.description) == MAX_DESCRIPTION_CHARS
    assert skill.description.endswith("...")


def test_name_falls_back_to_directory(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(ws / ".ironcore" / "skills", "folder-name", name=None, description="no name key")
    loaded = discover_skills(ws, _settings(), user_home=home)
    assert loaded.find("folder-name") is not None


def test_duplicate_name_user_wins(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(home / ".ironcore" / "skills", "u", name="dup", description="from user")
    _write_skill(ws / ".ironcore" / "skills", "p", name="dup", description="from project")

    loaded = discover_skills(ws, _settings(), user_home=home)

    matches = [s for s in loaded.skills if s.name == "dup"]
    assert len(matches) == 1
    assert matches[0].source == "user"  # user scanned first, first wins
    assert any("duplicate" in s.reason for s in loaded.skipped)


def test_disabled_returns_empty(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(ws / ".ironcore" / "skills", "x", name="x")
    loaded = discover_skills(ws, _settings(enabled=False), user_home=home)
    assert loaded.skills == []


def test_missing_dirs_never_raise(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)  # no skills dirs created at all
    loaded = discover_skills(ws, _settings(), user_home=home)
    assert loaded.skills == [] and loaded.skipped == []


# --------------------------------------------------------------------------- #
# the trusted catalog + confirmation state
# --------------------------------------------------------------------------- #


def test_catalog_surfaces_only_trusted_skills(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(home / ".ironcore" / "skills", "u", name="user-skill", description="U")
    _write_skill(ws / ".ironcore" / "skills", "p", name="proj-skill", description="P")

    # Unconfirmed project skill is absent from the model-facing catalog.
    catalog = load_skills_catalog(ws, _settings(), user_home=home)
    joined = "\n".join(catalog)
    assert "user-skill" in joined
    assert "proj-skill" not in joined

    # After the user confirms it, it appears.
    mark_confirmed(ws, "proj-skill")
    catalog2 = load_skills_catalog(ws, _settings(), user_home=home)
    assert "proj-skill" in "\n".join(catalog2)


def test_confirmation_is_per_workspace(tmp_path: Path) -> None:
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    assert not is_confirmed(ws_a, "s")
    mark_confirmed(ws_a, "s")
    assert is_confirmed(ws_a, "s")
    assert not is_confirmed(ws_b, "s")  # a different workspace is unaffected


# --------------------------------------------------------------------------- #
# use_skill tool
# --------------------------------------------------------------------------- #


def test_use_skill_is_read_risk() -> None:
    assert UseSkillTool.risk is ToolRisk.READ


def test_use_skill_returns_user_skill_body(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(home / ".ironcore" / "skills", "u", name="greet", body="Say hello nicely.")
    tool = UseSkillTool(ws, _settings(), user_home=home)
    result = asyncio.run(tool.run(name="greet"))
    assert result.ok
    assert "Say hello nicely." in result.output
    assert "greet" in result.output


def test_use_skill_unknown_name(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    tool = UseSkillTool(ws, _settings(), user_home=home)
    result = asyncio.run(tool.run(name="nope"))
    assert not result.ok
    assert "No skill named" in result.output


def test_use_skill_missing_name_arg(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    tool = UseSkillTool(ws, _settings(), user_home=home)
    result = asyncio.run(tool.run())
    assert not result.ok
    assert "name" in result.output.lower()


def test_use_skill_gates_unconfirmed_project_skill(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(ws / ".ironcore" / "skills", "p", name="deploy", body="rm the world")
    tool = UseSkillTool(ws, _settings(), user_home=home)

    blocked = asyncio.run(tool.run(name="deploy"))
    assert not blocked.ok
    assert "approval" in blocked.output.lower() or "approve" in blocked.output.lower()
    assert "rm the world" not in blocked.output  # body withheld until confirmed

    mark_confirmed(ws, "deploy")
    allowed = asyncio.run(tool.run(name="deploy"))
    assert allowed.ok
    assert "rm the world" in allowed.output


def test_use_skill_registered_in_default_tools(tmp_path: Path) -> None:
    registry = build_tools(_settings(), tmp_path)
    assert registry.get("use_skill") is not None


def test_use_skill_absent_when_skills_disabled(tmp_path: Path) -> None:
    registry = build_tools(_settings(enabled=False), tmp_path)
    assert registry.get("use_skill") is None


# --------------------------------------------------------------------------- #
# a skill's scripts still hit the EXEC gate (no execution smuggled in)
# --------------------------------------------------------------------------- #


def test_skill_cannot_bypass_the_exec_gate() -> None:
    # Loading a skill is a READ; there is no new execution path. A skill body
    # telling the model to run something routes through the ordinary shell tool,
    # which is EXEC-risk and denied in PLAN like everything else.
    assert UseSkillTool.risk is ToolRisk.READ
    assert ShellTool.risk is ToolRisk.EXEC
    assert decide(Mode.PLAN, ToolRisk.EXEC).value == "deny"
    assert decide(Mode.MANUAL, ToolRisk.EXEC).value == "ask"


# --------------------------------------------------------------------------- #
# /skill command
# --------------------------------------------------------------------------- #


class _FakeApp:
    """Captures ``inject_context`` calls the way the real TUI would apply them."""

    def __init__(self) -> None:
        self.injected: list[str] = []

    def inject_context(self, text: str) -> None:
        self.injected.append(text)


def _ctx(ws: Path, home: Path, *, settings: Settings | None = None, app: object | None = None):
    ctx = CommandContext(settings=settings or _settings())
    ctx.extra = {"workspace": ws, "user_home": home}
    if app is not None:
        ctx.extra["app"] = app
    return ctx


def test_skill_list(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(home / ".ironcore" / "skills", "u", name="alpha", description="the alpha")
    out = _SKILL_HANDLER(_ctx(ws, home), "")
    assert "alpha" in out and "the alpha" in out


def test_skill_list_empty(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    out = _SKILL_HANDLER(_ctx(ws, home), "")
    assert "No skills found" in out


def test_skill_disabled(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    out = _SKILL_HANDLER(_ctx(ws, home, settings=_settings(enabled=False)), "")
    assert "disabled" in out.lower()


def test_skill_unknown(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    out = _SKILL_HANDLER(_ctx(ws, home), "ghost")
    assert "No skill named" in out


def test_skill_injects_user_skill_via_app_hook(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(home / ".ironcore" / "skills", "u", name="helper", body="Be helpful.")
    app = _FakeApp()
    out = _SKILL_HANDLER(_ctx(ws, home, app=app), "helper")
    assert "Loaded skill" in out
    assert len(app.injected) == 1
    assert "Be helpful." in app.injected[0]


def test_skill_project_first_use_confirmation_then_run(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(ws / ".ironcore" / "skills", "p", name="ritual", body="The steps.")
    app = _FakeApp()
    ctx = _ctx(ws, home, app=app)

    # First use: a confirmation summary, NOT an injection.
    summary = _SKILL_HANDLER(ctx, "ritual")
    assert "approve" in summary.lower() or "First use" in summary
    assert app.injected == []
    assert not is_confirmed(ws, "ritual")

    # Confirm + run: injects and records confirmation.
    ran = _SKILL_HANDLER(ctx, "run ritual")
    assert "Loaded skill" in ran
    assert len(app.injected) == 1 and "The steps." in app.injected[0]
    assert is_confirmed(ws, "ritual")

    # Now a bare /skill injects directly.
    again = _SKILL_HANDLER(ctx, "ritual")
    assert "Loaded skill" in again
    assert len(app.injected) == 2


def test_skill_inject_without_app_returns_body_inline(tmp_path: Path) -> None:
    home, ws = _dirs(tmp_path)
    _write_skill(home / ".ironcore" / "skills", "u", name="solo", body="Inline body here.")
    out = _SKILL_HANDLER(_ctx(ws, home), "solo")  # no app in ctx.extra
    assert "Inline body here." in out


# --------------------------------------------------------------------------- #
# composer: catalog surfacing, budget-fit, tiny-context degrade
# --------------------------------------------------------------------------- #

_SYS = "You are IronCore, a terminal coding agent."


def _profile(honest_context: int = 4096) -> CapabilityProfile:
    return CapabilityProfile(model_id="test-model", honest_context=honest_context)


def _compose(profile: CapabilityProfile, catalog: list[str]):
    return compose(
        SessionState(turn_count=1),
        profile=profile,
        settings=Settings(),
        system_prompt=_SYS,
        working_set={},
        history=[],
        user_input="go",
        memory="",
        skills_catalog=catalog,
    )


def _ceiling(profile: CapabilityProfile) -> int:
    return profile.honest_context - int(profile.honest_context * RESPONSE_HEADROOM_SHARE)


def _content_tokens(messages) -> int:
    return sum(estimate_tokens(m.content) for m in messages)


def test_catalog_appears_in_system_message() -> None:
    msgs = _compose(_profile(), ["- alpha: does A", "- beta: does B"])
    system = msgs[0].content
    assert "# Skills" in system
    assert "- alpha: does A" in system
    assert "- beta: does B" in system


def test_no_catalog_when_empty_is_byte_identical() -> None:
    with_empty = _compose(_profile(), [])
    assert "# Skills" not in with_empty[0].content


def test_catalog_degrades_to_top_n_on_small_context() -> None:
    entries = [f"- skill{i}: description number {i} that takes some room" for i in range(40)]
    profile = _profile(honest_context=320)  # SYSTEM share ~32 tokens
    msgs = _compose(profile, entries)
    system = msgs[0].content
    shown = system.count("- skill")
    # Honest degrade: fewer than all, and never a half-entry (each kept line whole).
    assert 0 <= shown < len(entries)
    if shown:
        assert "# Skills" in system
        assert "more skill" in system  # the dropped-count marker


def test_budget_invariant_holds_with_catalog() -> None:
    entries = [f"- skill{i}: a description with enough text to matter here" for i in range(30)]
    for hc in (128, 256, 512, 1024, 4096):
        profile = _profile(honest_context=hc)
        msgs = _compose(profile, entries)
        assert _content_tokens(msgs) <= _ceiling(profile), f"budget broken at hc={hc}"
        # The SYSTEM message alone must never exceed the SYSTEM share.
        assert estimate_tokens(msgs[0].content) <= int(hc * SYSTEM_SHARE)


def test_memory_takes_priority_over_catalog_in_system_share() -> None:
    # A large memory should crowd the catalog out (it fills the remainder only).
    profile = _profile(honest_context=400)
    msgs = compose(
        SessionState(turn_count=1),
        profile=profile,
        settings=Settings(),
        system_prompt=_SYS,
        working_set={},
        history=[],
        user_input="go",
        memory="M" * 4000,  # oversize memory
        skills_catalog=["- alpha: does A"],
    )
    assert estimate_tokens(msgs[0].content) <= int(profile.honest_context * SYSTEM_SHARE)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_skills_settings_defaults() -> None:
    s = Settings()
    assert s.skills.enabled is True
    assert s.skills.compat_dirs is False


def test_skill_command_registered() -> None:
    from ironcore.commands.builtins import build_default_registry

    assert build_default_registry().get("skill") is not None
