"""Layered settings: defaults <- user <- project <- env."""

import codecs
from pathlib import Path

import pytest

from ironcore.config.settings import ConfigError, Settings


def test_defaults_without_any_files(tmp_path: Path):
    settings = Settings.load(project_dir=tmp_path, user_config=tmp_path / "nope.toml", env={})
    assert settings.provider.base_url == "http://localhost:11434/v1"
    assert settings.safety.mode == "manual"
    assert settings.safety.workspace_only is True
    assert settings.safety.network_tools is False


def test_project_overrides_user(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[provider]\nmodel = "user-model"\nbase_url = "http://user:1/v1"\n')
    project = tmp_path / "proj"
    (project / ".ironcore").mkdir(parents=True)
    (project / ".ironcore" / "config.toml").write_text('[provider]\nmodel = "project-model"\n')

    settings = Settings.load(project_dir=project, user_config=user, env={})
    assert settings.provider.model == "project-model"  # project wins
    assert settings.provider.base_url == "http://user:1/v1"  # user survives deep merge


def test_env_wins_over_everything(tmp_path: Path):
    project = tmp_path / "proj"
    (project / ".ironcore").mkdir(parents=True)
    (project / ".ironcore" / "config.toml").write_text('[provider]\nmodel = "project-model"\n')

    settings = Settings.load(
        project_dir=project,
        user_config=tmp_path / "nope.toml",
        env={"IRONCORE_MODEL": "env-model", "IRONCORE_MODE": "plan"},
    )
    assert settings.provider.model == "env-model"
    assert settings.safety.mode == "plan"


def test_role_env_overrides(tmp_path: Path):
    settings = Settings.load(
        project_dir=tmp_path,
        user_config=tmp_path / "nope.toml",
        env={
            "IRONCORE_ROLE_PLANNER": "planner-model",
            "IRONCORE_ROLE_CODER": "coder-model",
            "IRONCORE_ROLE_SUMMARIZER": "summarizer-model",
            "IRONCORE_ROLE_VERIFIER": "verifier-model",
        },
    )
    assert settings.roles.planner == "planner-model"
    assert settings.roles.coder == "coder-model"
    assert settings.roles.summarizer == "summarizer-model"
    assert settings.roles.verifier == "verifier-model"


def test_role_env_wins_over_file(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[roles]\nplanner = "file-planner"\ncoder = "file-coder"\n')
    settings = Settings.load(
        project_dir=tmp_path,
        user_config=user,
        env={"IRONCORE_ROLE_PLANNER": "env-planner"},
    )
    assert settings.roles.planner == "env-planner"  # env wins
    assert settings.roles.coder == "file-coder"  # untouched keys survive


def test_malformed_toml_reports_path_and_line(tmp_path: Path):
    bad = tmp_path / "user.toml"
    bad.write_text("[provider\nmodel = broken\n")  # missing ] -> syntax error on line 1
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=tmp_path, user_config=bad, env={})
    message = str(excinfo.value)
    assert str(bad) in message
    assert "line" in message


def test_malformed_project_toml_names_project_file(tmp_path: Path):
    project = tmp_path / "proj"
    (project / ".ironcore").mkdir(parents=True)
    bad = project / ".ironcore" / "config.toml"
    bad.write_text("this is not = = toml\n")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=project, user_config=tmp_path / "nope.toml", env={})
    assert str(bad) in str(excinfo.value)


def test_invalid_mode_rejected_with_valid_list(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "yolo"\n')
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=tmp_path, user_config=user, env={})
    message = str(excinfo.value)
    assert "yolo" in message
    for valid in ("plan", "manual", "accept-edits", "auto"):
        assert valid in message


def test_invalid_mode_from_env_rejected(tmp_path: Path):
    with pytest.raises(ConfigError):
        Settings.load(
            project_dir=tmp_path,
            user_config=tmp_path / "nope.toml",
            env={"IRONCORE_MODE": "yolo"},
        )


def test_engine_best_of_n_defaults_to_one_disabled(tmp_path: Path):
    settings = Settings.load(project_dir=tmp_path, user_config=tmp_path / "nope.toml", env={})
    assert settings.engine.best_of_n == 1  # resampling off by default


def test_engine_best_of_n_parses_from_toml(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text("[engine]\nbest_of_n = 3\n")
    settings = Settings.load(project_dir=tmp_path, user_config=user, env={})
    assert settings.engine.best_of_n == 3


def test_engine_best_of_n_rejects_out_of_range_values(tmp_path: Path):
    for bad in (0, 6):
        user = tmp_path / "user.toml"
        user.write_text(f"[engine]\nbest_of_n = {bad}\n")
        with pytest.raises(ConfigError) as excinfo:
            Settings.load(project_dir=tmp_path, user_config=user, env={})
        assert "engine.best_of_n" in str(excinfo.value)


def test_instant_seed_defaults_true(tmp_path: Path):
    settings = Settings.load(project_dir=tmp_path, user_config=tmp_path / "nope.toml", env={})
    assert settings.envelope.instant_seed is True
    assert settings.envelope.auto_probe is True  # unchanged


def test_project_config_can_disable_instant_seed(tmp_path: Path):
    project = tmp_path / "proj"
    (project / ".ironcore").mkdir(parents=True)
    (project / ".ironcore" / "config.toml").write_text("[envelope]\ninstant_seed = false\n")

    settings = Settings.load(project_dir=project, user_config=tmp_path / "nope.toml", env={})
    assert settings.envelope.instant_seed is False
    assert settings.envelope.auto_probe is True  # sibling key untouched


def test_mcp_defaults_to_no_servers(tmp_path: Path):
    settings = Settings.load(project_dir=tmp_path, user_config=tmp_path / "nope.toml", env={})
    assert settings.mcp.servers == {}


def test_mcp_server_parses_from_toml(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        "[mcp.servers.gh]\n"
        'command = "npx.cmd"\n'
        'args = ["-y", "@modelcontextprotocol/server-github"]\n'
        "timeout_s = 12.5\n"
        "[mcp.servers.gh.env]\n"
        'GITHUB_TOKEN = "placeholder"\n'
    )
    settings = Settings.load(project_dir=tmp_path, user_config=user, env={})
    server = settings.mcp.servers["gh"]
    assert server.command == "npx.cmd"
    assert server.args == ["-y", "@modelcontextprotocol/server-github"]
    assert server.env == {"GITHUB_TOKEN": "placeholder"}
    assert server.timeout_s == 12.5
    assert server.enabled is True  # default
    assert server.url is None


def test_mcp_url_only_entry_parses(tmp_path: Path):
    # the schema accepts url-only entries (skipped at wiring time: stdio-only v1)
    user = tmp_path / "user.toml"
    user.write_text('[mcp.servers.remote]\nurl = "https://example.com/mcp"\n')
    settings = Settings.load(project_dir=tmp_path, user_config=user, env={})
    assert settings.mcp.servers["remote"].url == "https://example.com/mcp"
    assert settings.mcp.servers["remote"].command is None


def test_mcp_server_requires_command_or_url(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text("[mcp.servers.gh]\nenabled = true\n")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=tmp_path, user_config=user, env={})
    message = str(excinfo.value)
    assert "mcp.servers.gh" in message  # names the offending entry
    assert "command" in message


def test_auto_tune_defaults_true(tmp_path: Path):
    settings = Settings.load(project_dir=tmp_path, user_config=tmp_path / "nope.toml", env={})
    assert settings.envelope.auto_tune is True


def test_auto_tune_parses_from_toml(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text("[envelope]\nauto_tune = false\n")
    settings = Settings.load(project_dir=tmp_path, user_config=user, env={})
    assert settings.envelope.auto_tune is False
    assert settings.envelope.auto_probe is True  # sibling keys untouched


def test_plugins_enabled_defaults_true(tmp_path: Path):
    settings = Settings.load(project_dir=tmp_path, user_config=tmp_path / "nope.toml", env={})
    assert settings.plugins.enabled is True


def test_plugins_enabled_parses_from_toml(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text("[plugins]\nenabled = false\n")
    settings = Settings.load(project_dir=tmp_path, user_config=user, env={})
    assert settings.plugins.enabled is False
    assert settings.safety.network_tools is False  # sibling sections untouched


# --------------------------------------------------------------------------- #
# T8: the autonomy ceiling (FIX-3)
#
# Every test below FAILS before FIX-3: `Settings.load` deep-merged the project
# file over the user file with no clamp, so `git clone`-ing any repo carrying
# `.ironcore/config.toml` booted a manual/no-network user straight into AUTO
# with network tools registered -- while docs/SAFETY.md T8 and CONTRACTS §7
# both asserted the control existed.
# --------------------------------------------------------------------------- #


def _project_with(tmp_path: Path, toml: str) -> Path:
    project = tmp_path / "proj"
    (project / ".ironcore").mkdir(parents=True)
    (project / ".ironcore" / "config.toml").write_text(toml, encoding="utf-8")
    return project


def test_project_config_cannot_raise_mode_above_the_user_ceiling(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "manual"\nnetwork_tools = false\n')
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\nnetwork_tools = true\n')

    settings = Settings.load(project_dir=project, user_config=user, env={})
    assert settings.safety.mode == "manual"
    assert settings.safety.network_tools is False


def test_ceiling_holds_for_a_user_with_no_config_file_at_all(tmp_path: Path):
    """The fresh install is the exposed case: no ~/.ironcore/config.toml means
    the built-in floor IS the ceiling, not an exemption."""
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\nnetwork_tools = true\n')

    settings = Settings.load(project_dir=project, user_config=tmp_path / "absent.toml", env={})
    assert settings.safety.mode == "manual"
    assert settings.safety.network_tools is False


def test_project_config_may_still_lower_autonomy(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "auto"\nnetwork_tools = true\n')
    project = _project_with(tmp_path, '[safety]\nmode = "plan"\nnetwork_tools = false\n')

    settings = Settings.load(project_dir=project, user_config=user, env={})
    assert settings.safety.mode == "plan"  # a repo asking for LESS is honoured
    assert settings.safety.network_tools is False


def test_project_config_may_raise_up_to_but_not_past_the_ceiling(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "accept-edits"\nnetwork_tools = true\n')
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\nnetwork_tools = true\n')

    settings = Settings.load(project_dir=project, user_config=user, env={})
    assert settings.safety.mode == "accept-edits"  # clamped down one rung
    assert settings.safety.network_tools is True  # user opted in: project may use it

    project2 = _project_with(tmp_path / "b", '[safety]\nmode = "accept-edits"\n')
    settings2 = Settings.load(project_dir=project2, user_config=user, env={})
    assert settings2.safety.mode == "accept-edits"  # equal rank: untouched


def test_ceiling_clamp_is_never_silent(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "manual"\n')
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\nnetwork_tools = true\n')

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.safety.mode == "manual"
    blob = "\n".join(notes)
    assert "'auto'" in blob and "'manual'" in blob  # what was asked, what was granted
    assert "network_tools" in blob
    assert str(user) in blob  # where to raise the ceiling
    assert "Shift+Tab" in blob  # the per-session escape hatch


def test_notes_are_ascii_so_a_cp1252_console_can_print_them(tmp_path: Path):
    """`ironcore doctor` prints these; an em-dash renders as a replacement
    character on a stock Windows console."""
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\nnetwork_tools = true\n')
    _, notes = Settings.load_with_notes(
        project_dir=project, user_config=tmp_path / "absent.toml", env={}
    )
    assert notes and all(note.isascii() for note in notes)


def test_no_notes_when_nothing_was_clamped(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "auto"\n')
    project = _project_with(tmp_path, '[provider]\nmodel = "project-model"\n')

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.safety.mode == "auto"
    assert notes == []


def test_env_mode_is_not_clamped_it_is_the_human_at_the_keyboard(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "plan"\n')
    project = _project_with(tmp_path, '[provider]\nmodel = "m"\n')

    settings = Settings.load(project_dir=project, user_config=user, env={"IRONCORE_MODE": "auto"})
    assert settings.safety.mode == "auto"  # env still wins (CONTRACTS §7 precedence)


def test_user_config_alone_may_set_any_mode(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "auto"\nnetwork_tools = true\n')

    settings, notes = Settings.load_with_notes(project_dir=tmp_path, user_config=user, env={})
    assert settings.safety.mode == "auto"
    assert settings.safety.network_tools is True
    assert notes == []


def test_load_is_exactly_load_with_notes_first_element(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[provider]\nmodel = "m"\n')
    a = Settings.load(project_dir=tmp_path, user_config=user, env={})
    b, _ = Settings.load_with_notes(project_dir=tmp_path, user_config=user, env={})
    assert a == b


def test_invalid_project_mode_still_reaches_the_loud_validator(tmp_path: Path):
    """The clamp must not swallow garbage into a silently-valid value."""
    project = _project_with(tmp_path, '[safety]\nmode = "yolo"\n')
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=project, user_config=tmp_path / "absent.toml", env={})
    assert "yolo" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# FIX-3 round 1: the ceiling must not FAIL OPEN on a malformed user layer, and
# the plugin kill switch is under it too.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("typo", ["Manual", "acceptedits", "yolo"])
def test_unrankable_user_mode_raises_instead_of_letting_the_project_win(
    tmp_path: Path, typo: str
):
    """Round-1 major: a user-layer mode that was a STRING but not a valid mode
    skipped the clamp entirely, so the project layer's `auto` survived -- the
    untrusted layer both escalated autonomy AND masked the user's own typo
    (which raises loudly when no project file is present)."""
    user = tmp_path / "user.toml"
    user.write_text(f'[safety]\nmode = "{typo}"\n')
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\n')

    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=project, user_config=user, env={})
    message = str(excinfo.value)
    assert typo in message
    assert str(user) in message  # which file to fix
    assert "accept-edits" in message  # the valid values


def test_non_string_user_mode_also_raises(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text("[safety]\nmode = 3\n")
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\n')

    with pytest.raises(ConfigError):
        Settings.load(project_dir=project, user_config=user, env={})


def test_project_config_cannot_re_enable_plugins_the_user_killed(tmp_path: Path):
    """Round-1 major: `[plugins] enabled = false` is the hardened-setup switch
    (SAFETY.md §8), and plugin code runs at boot and in `doctor`. A clone must
    not re-arm it."""
    user = tmp_path / "user.toml"
    user.write_text("[plugins]\nenabled = false\n")
    project = _project_with(tmp_path, "[plugins]\nenabled = true\n")

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.plugins.enabled is False
    blob = "\n".join(notes)
    assert "[plugins]" in blob and str(user) in blob
    assert all(note.isascii() for note in notes)


def test_plugin_clamp_survives_a_config_with_no_safety_section(tmp_path: Path):
    """Regression on the fix itself: the early `[safety]`-shaped bail-out used to
    return before the plugin clamp ever ran."""
    user = tmp_path / "user.toml"
    user.write_text("[plugins]\nenabled = false\n")  # no [safety] anywhere
    project = _project_with(tmp_path, "[plugins]\nenabled = true\n")

    settings = Settings.load(project_dir=project, user_config=user, env={})
    assert settings.plugins.enabled is False


def test_project_config_may_still_turn_plugins_off(tmp_path: Path):
    """Lowering is always allowed -- the clamp is one-directional."""
    user = tmp_path / "user.toml"
    user.write_text("[plugins]\nenabled = true\n")
    project = _project_with(tmp_path, "[plugins]\nenabled = false\n")

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.plugins.enabled is False
    assert notes == []


def test_plugins_left_on_when_the_user_never_disabled_them(tmp_path: Path):
    """The default is ON (installation was the consent moment), so a project file
    agreeing with the default escalates nothing and earns no note."""
    project = _project_with(tmp_path, "[plugins]\nenabled = true\n")

    settings, notes = Settings.load_with_notes(
        project_dir=project, user_config=tmp_path / "absent.toml", env={}
    )
    assert settings.plugins.enabled is True
    assert notes == []


def test_unreadable_and_non_utf8_config_files_raise_configerror_not_a_traceback(
    tmp_path: Path,
):
    """The module docstring promises callers never see a raw traceback; a non-UTF8
    file raised UnicodeDecodeError and a directory path raised PermissionError."""
    non_utf8 = tmp_path / "latin1.toml"
    non_utf8.write_bytes(b'[safety]\nmode = "manual"  # caf\xe9\n')
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=tmp_path, user_config=non_utf8, env={})
    assert "UTF-8" in str(excinfo.value)

    a_directory = tmp_path / "dir.toml"
    a_directory.mkdir()
    with pytest.raises(ConfigError):
        Settings.load(project_dir=tmp_path, user_config=a_directory, env={})


# --------------------------------------------------------------------------- #
# FIX-3 round 2: the ceiling must read the user layer the way pydantic does, and
# `[mcp.servers.*]` is under the ceiling too because a configured server is
# spawned AT LAUNCH (register_into -> tools/list), not at the first tool call.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ['"false"', "0", '"no"'])
def test_a_coercible_user_plugins_switch_is_still_a_ceiling(tmp_path: Path, raw: str):
    """Round-2 major: the clamp read RAW TOML while the effective value is
    pydantic-COERCED, so `enabled = "false"` (which pydantic honours, and which
    disables plugins when the user file is alone) fell back to the PERMISSIVE
    default as the ceiling -- and a cloned repo re-armed entry-point plugin code
    at boot for the one user who explicitly disarmed it."""
    user = tmp_path / "user.toml"
    user.write_text(f"[plugins]\nenabled = {raw}\n")
    assert Settings.load(project_dir=None, user_config=user, env={}).plugins.enabled is False

    project = _project_with(tmp_path / raw.strip('"'), "[plugins]\nenabled = true\n")
    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.plugins.enabled is False
    assert notes and str(user) in "\n".join(notes)


@pytest.mark.parametrize("section", ["plugins", "safety", "mcp"])
def test_a_non_table_user_section_raises_instead_of_being_skipped(tmp_path: Path, section: str):
    """Round-2 major, worse variant: `plugins = 5` alone raised loudly, but adding
    a project `[plugins] enabled = true` made the untrusted layer both escalate
    AND mask the user's own config error."""
    user = tmp_path / "user.toml"
    user.write_text(f"{section} = 5\n")
    project = _project_with(
        tmp_path,
        "[plugins]\nenabled = true\n[safety]\nmode = 'auto'\n"
        '[mcp.servers.evil]\ncommand = "calc"\n',
    )

    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=project, user_config=user, env={})
    assert section in str(excinfo.value) and str(user) in str(excinfo.value)


def test_a_non_boolean_user_ceiling_raises_rather_than_falling_back(tmp_path: Path):
    """Same defect class as the round-1 mode fix: a ceiling IronCore cannot read
    must be loud, never a skipped clamp."""
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nnetwork_tools = "sometimes"\n')
    project = _project_with(tmp_path, "[safety]\nnetwork_tools = true\n")

    with pytest.raises(ConfigError) as excinfo:
        Settings.load(project_dir=project, user_config=user, env={})
    assert "network_tools" in str(excinfo.value) and str(user) in str(excinfo.value)


def test_a_coercible_user_network_ceiling_is_not_misattributed(tmp_path: Path):
    """Round-2 minor: user `network_tools = 1` is True to pydantic (and alone it
    turns NET on), but the raw read clamped it OFF while the note blamed the
    user's own ceiling for a decision the user never made."""
    user = tmp_path / "user.toml"
    user.write_text("[safety]\nnetwork_tools = 1\n")
    project = _project_with(tmp_path, "[safety]\nnetwork_tools = true\n")

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.safety.network_tools is True
    assert notes == []


def test_a_project_config_cannot_add_an_mcp_server(tmp_path: Path):
    """Round-2 major (T8 x T10): every configured server is spawned at LAUNCH to
    enumerate its tools, so a `[mcp.servers.*]` table arriving with a `git clone`
    is boot-time code execution for anyone who legitimately turned NET on."""
    user = tmp_path / "user.toml"
    user.write_text("[safety]\nnetwork_tools = true\n")
    project = _project_with(tmp_path, '[mcp.servers.evil]\ncommand = "calc"\n')

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.mcp.servers == {}
    blob = "\n".join(notes)
    assert "evil" in blob and str(user) in blob
    assert all(note.isascii() for note in notes)


def test_a_project_config_cannot_redefine_a_user_declared_server(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[mcp.servers.gh]\ncommand = "real"\nargs = ["--safe"]\n')
    project = _project_with(
        tmp_path, '[mcp.servers.gh]\ncommand = "evil"\nargs = ["--pwn"]\n'
    )

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.mcp.servers["gh"].command == "real"
    assert settings.mcp.servers["gh"].args == ["--safe"]
    assert notes and "gh" in notes[0]


def test_a_project_config_may_still_disable_a_server(tmp_path: Path):
    """Lowering stays allowed -- the clamp is one-directional here too."""
    user = tmp_path / "user.toml"
    user.write_text('[mcp.servers.gh]\ncommand = "real"\n')
    project = _project_with(tmp_path, "[mcp.servers.gh]\nenabled = false\n")

    settings, notes = Settings.load_with_notes(project_dir=project, user_config=user, env={})
    assert settings.mcp.servers["gh"].enabled is False
    assert settings.mcp.servers["gh"].command == "real"
    assert notes == []


def test_the_mode_note_does_not_claim_a_clamp_the_env_overrode(tmp_path: Path):
    """Round-2 minor: the clamp runs before `_apply_env`, so with IRONCORE_MODE
    set doctor printed `mode auto` and then a note asserting it had been clamped
    to manual -- on the one surface whose whole value is being trustworthy."""
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "manual"\n')
    project = _project_with(tmp_path, '[safety]\nmode = "auto"\n')

    settings, notes = Settings.load_with_notes(
        project_dir=project, user_config=user, env={"IRONCORE_MODE": "auto"}
    )
    assert settings.safety.mode == "auto"  # env is the human at the keyboard
    blob = "\n".join(notes)
    assert "IRONCORE_MODE" in blob
    assert "clamped to your ceiling" not in blob  # it plainly was not


def test_a_utf8_bom_config_still_parses(tmp_path: Path):
    """Round-2 minor: a BOM is a routine Windows editor artifact; it decoded fine
    and died as a bogus TOML syntax error at line 1, column 1."""
    user = tmp_path / "bom.toml"
    user.write_bytes(codecs.BOM_UTF8 + b'[safety]\nmode = "plan"\n')

    assert Settings.load(project_dir=None, user_config=user, env={}).safety.mode == "plan"


# --------------------------------------------------------------------------- #
# ${VAR} expansion in MCP env (FIX-3): secrets live in the shell, not in the
# committable project config. Before FIX-3 the four literal characters "${X}"
# were handed to the child, producing an opaque auth failure inside someone
# else's process.
# --------------------------------------------------------------------------- #


def test_mcp_env_expands_placeholders_from_the_environment(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        "[mcp.servers.gh]\n"
        'command = "npx.cmd"\n'
        "[mcp.servers.gh.env]\n"
        'GITHUB_TOKEN = "${GH_PAT}"\n'
        'HEADER = "Bearer ${GH_PAT}!"\n'
        'PLAIN = "$NOT_EXPANDED"\n'
    )
    settings, notes = Settings.load_with_notes(
        project_dir=tmp_path, user_config=user, env={"GH_PAT": "s3cret"}
    )
    env = settings.mcp.servers["gh"].env
    assert env["GITHUB_TOKEN"] == "s3cret"
    assert env["HEADER"] == "Bearer s3cret!"
    assert env["PLAIN"] == "$NOT_EXPANDED"  # bare $VAR stays literal
    assert notes == []


def test_mcp_server_with_an_unset_var_is_skipped_with_a_named_note(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        "[mcp.servers.github]\n"
        'command = "npx.cmd"\n'
        "[mcp.servers.github.env]\n"
        'GITHUB_TOKEN = "${GITHUB_TOKEN}"\n'
        "[mcp.servers.fine]\n"
        'command = "other"\n'
    )
    settings, notes = Settings.load_with_notes(project_dir=tmp_path, user_config=user, env={})
    assert "github" not in settings.mcp.servers  # never spawned with a broken value
    assert "fine" in settings.mcp.servers  # one bad entry does not sink the rest
    assert notes == [
        "[mcp] server 'github' skipped: ${GITHUB_TOKEN} is not set in your environment"
    ]


def test_mcp_env_empty_variable_counts_as_unset(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[mcp.servers.gh]\ncommand = "x"\n[mcp.servers.gh.env]\nT = "${TOK}"\n')
    settings, notes = Settings.load_with_notes(
        project_dir=tmp_path, user_config=user, env={"TOK": ""}
    )
    assert settings.mcp.servers == {}
    assert "${TOK}" in notes[0]


def test_mcp_env_note_lists_every_missing_var_once(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[mcp.servers.gh]\ncommand = "x"\n'
        "[mcp.servers.gh.env]\n"
        'A = "${ONE}/${TWO}"\n'
        'B = "${ONE}"\n'
    )
    _, notes = Settings.load_with_notes(project_dir=tmp_path, user_config=user, env={})
    assert notes == ["[mcp] server 'gh' skipped: ${ONE}, ${TWO} are not set in your environment"]


def test_disabled_mcp_server_is_left_alone(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[mcp.servers.off]\ncommand = "x"\nenabled = false\n'
        "[mcp.servers.off.env]\n"
        'T = "${NEVER_SET}"\n'
    )
    settings, notes = Settings.load_with_notes(project_dir=tmp_path, user_config=user, env={})
    assert settings.mcp.servers["off"].env == {"T": "${NEVER_SET}"}  # never spawned
    assert notes == []
