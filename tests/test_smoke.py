"""Package-level smoke tests: import, version, CLI entry."""

import tomllib
from pathlib import Path

import ironcore
from ironcore.cli import build_parser, cmd_doctor, main

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert ironcore.__version__ == pyproject["project"]["version"]


def test_cli_version(capsys):
    assert main(["--version"]) == 0
    assert ironcore.__version__ in capsys.readouterr().out


def test_cli_banner_points_to_docs(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "docs/SPEC.md" in out
    assert "TODO.md" in out


def test_parser_has_doctor():
    parser = build_parser()
    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"


def _doctor(tmp_path, user_config, env=None):
    """Run doctor fully hermetic: injected config/env, no network probe."""
    return cmd_doctor(
        project_dir=tmp_path,
        user_config=user_config,
        env=env or {},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=False,
    )


def test_doctor_malformed_config_exits_1_with_path_and_line(tmp_path, capsys):
    bad = tmp_path / "user.toml"
    bad.write_text("[provider\n")
    assert _doctor(tmp_path, bad) == 1
    out = capsys.readouterr().out
    assert "[FAIL] config" in out
    assert str(bad) in out
    assert "line" in out


def test_doctor_invalid_mode_exits_1_listing_valid_modes(tmp_path, capsys):
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "yolo"\n')
    assert _doctor(tmp_path, user) == 1
    out = capsys.readouterr().out
    assert "[FAIL] config" in out
    assert "accept-edits" in out


def test_doctor_reports_set_roles_only(tmp_path, capsys):
    user = tmp_path / "user.toml"
    user.write_text('[roles]\nplanner = "big-planner"\ncoder = "fast-coder"\n')
    assert _doctor(tmp_path, user) == 0
    out = capsys.readouterr().out
    assert "role planner: big-planner" in out
    assert "role coder: fast-coder" in out
    assert "role summarizer" not in out
    assert "role verifier" not in out


def test_doctor_warns_when_hosted_endpoint_and_network_tools(tmp_path, capsys):
    user = tmp_path / "user.toml"
    user.write_text(
        '[provider]\nbase_url = "https://hosted.example.com/v1"\n'
        "[safety]\nnetwork_tools = true\n"
    )
    assert _doctor(tmp_path, user) == 0  # a warning, not a failure
    out = capsys.readouterr().out
    assert "[!!]" in out
    assert "leaves this machine" in out


def test_doctor_reports_measured_profile(tmp_path, capsys):
    # a probed profile cached under the injected envelope_dir -> the "measured" line
    from ironcore.envelope.profile import CapabilityProfile

    CapabilityProfile(
        model_id="qwen3-coder:30b",  # the default provider model
        source="probed",
        probed_at="2026-07-15T00:00:00+00:00",
        tool_protocols={"native": 0.97},
        edit_formats={"unified_diff": 0.95},
        honest_context=16384,
    ).save(tmp_path / "envelopes")

    assert _doctor(tmp_path, tmp_path / "nope.toml") == 0
    out = capsys.readouterr().out
    assert "qwen3-coder:30b measured" in out
    assert "ctx: 16384" in out


def test_doctor_unprobed_model_mentions_instant_seed(tmp_path, capsys):
    # nothing cached -> the instant-seed wording, not the old "on floor defaults"
    assert _doctor(tmp_path, tmp_path / "nope.toml") == 0
    out = capsys.readouterr().out
    assert "qwen3-coder:30b unprobed" in out
    assert "instant-seeds" in out
    assert out.isascii()


def test_doctor_no_warning_for_localhost_or_network_tools_off(tmp_path, capsys):
    # localhost endpoint + network_tools on -> no warning
    user = tmp_path / "user.toml"
    user.write_text("[safety]\nnetwork_tools = true\n")
    assert _doctor(tmp_path, user) == 0
    assert "[!!]" not in capsys.readouterr().out

    # hosted endpoint + network_tools off (default) -> no warning
    user.write_text('[provider]\nbase_url = "https://hosted.example.com/v1"\n')
    assert _doctor(tmp_path, user) == 0
    assert "[!!]" not in capsys.readouterr().out
