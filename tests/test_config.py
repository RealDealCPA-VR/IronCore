"""Layered settings: defaults <- user <- project <- env."""

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
