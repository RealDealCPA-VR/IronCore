"""Package-level smoke tests: import, version, CLI entry."""

import tomllib
from pathlib import Path

import ironcore
from ironcore.cli import build_parser, main

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
