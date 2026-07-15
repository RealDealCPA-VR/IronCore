"""Layered settings: defaults <- user <- project <- env."""

from pathlib import Path

from ironcore.config.settings import Settings


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
