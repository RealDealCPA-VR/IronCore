"""Doctor tells the truth (FIX-2).

Every test here fails against the pre-FIX-2 CLI. The v0.2.0 doctor printed
``[ok] endpoint reachable (404)`` because it captured ``resp.status_code`` and
never inspected it; it probed Ollama's proprietary ``/api/version`` for a README
advertising vLLM/llama.cpp/LM Studio (which 404 that path, which is exactly why
it "passed"); it never asked whether the configured model existed; it printed
``[ok] config loaded`` when no config file existed at all; and ``main()`` had no
exception guard, so a ConfigError tracebacked out of the primary entry point.

Fully offline: the endpoint probe is injected (``probe=``), never dialled.
"""

from __future__ import annotations

import httpx
import pytest

from ironcore.cli import (
    EndpointProbe,
    _model_available,
    build_parser,
    cmd_doctor,
    cmd_init,
    main,
    probe_endpoint,
)
from ironcore.config.settings import ConfigError, Settings

DEFAULT_MODEL = Settings().provider.model


def _doctor(tmp_path, *, config="", probe=None, monkeypatch=None):
    """Hermetic doctor: injected config file, injected probe, no network."""
    user = tmp_path / "user.toml"
    user.write_text(config, encoding="utf-8")
    return cmd_doctor(
        project_dir=tmp_path,
        user_config=user if config else tmp_path / "absent.toml",
        env={},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=False,
        probe=probe,
    )


def _ok_probe(*models: str):
    return lambda url: EndpointProbe("ok", f"{url.rstrip('/')}/models", code=200, models=models)


# --------------------------------------------------------------------------
# the headline lie: a non-2xx response reported as [ok]
# --------------------------------------------------------------------------


def test_http_error_is_never_reported_as_ok(tmp_path, capsys):
    """v0.2.0 printed '[ok] endpoint reachable: ... (404)'."""
    probe = lambda url: EndpointProbe("http_error", f"{url}/models", code=404)  # noqa: E731
    _doctor(tmp_path, probe=probe)
    out = capsys.readouterr().out

    assert "[ok] endpoint reachable" not in out
    assert "got HTTP 404" in out
    assert "OpenAI-compatible" in out


def _fake_httpx_get(monkeypatch, handler):
    """Replace httpx.get with an in-memory handler -- still zero network."""
    seen: list[str] = []

    def _get(url, **kwargs):
        seen.append(str(url))
        return handler(str(url))

    monkeypatch.setattr(httpx, "get", _get)
    return seen


def test_probe_targets_the_openai_models_path_not_ollamas_api_version(monkeypatch):
    """/api/version is Ollama-proprietary; vLLM, llama.cpp and LM Studio 404 it,
    which is precisely why the old probe 'passed' against servers that could not
    serve us. /models is the path all four actually implement."""
    seen = _fake_httpx_get(
        monkeypatch,
        lambda url: httpx.Response(200, json={"data": [{"id": "llama3"}]}),
    )
    result = probe_endpoint("http://localhost:11434/v1")

    assert seen == ["http://localhost:11434/v1/models"]
    assert "/api/version" not in seen[0]
    assert result.status == "ok"
    assert result.models == ("llama3",)


def test_probe_reads_the_status_code_it_prints(monkeypatch):
    _fake_httpx_get(monkeypatch, lambda url: httpx.Response(404, text="not found"))
    result = probe_endpoint("http://localhost:11434/v1")

    assert result.status == "http_error"
    assert result.code == 404


def test_probe_tolerates_sloppy_local_server_bodies(monkeypatch):
    _fake_httpx_get(monkeypatch, lambda url: httpx.Response(200, json=["a", {"name": "b"}]))
    assert probe_endpoint("http://x/v1").models == ("a", "b")

    _fake_httpx_get(monkeypatch, lambda url: httpx.Response(200, json={"data": []}))
    empty = probe_endpoint("http://x/v1")
    assert empty.status == "ok" and empty.models == ()  # "no models" != "not a list"

    _fake_httpx_get(monkeypatch, lambda url: httpx.Response(200, text="<html>hi</html>"))
    assert probe_endpoint("http://x/v1").status == "bad_payload"


def test_doctor_probes_the_configured_base_url(tmp_path):
    seen: list[str] = []

    def _probe(url: str) -> EndpointProbe:
        seen.append(url)
        return EndpointProbe("unreachable", url)

    cmd_doctor(
        project_dir=tmp_path,
        user_config=tmp_path / "absent.toml",
        env={},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=False,
        probe=_probe,
    )
    assert seen == ["http://localhost:11434/v1"]


# --------------------------------------------------------------------------
# a broken base_url is a FAILURE, not "fine"
# --------------------------------------------------------------------------


def test_unusable_base_url_fails_instead_of_saying_it_is_fine(tmp_path, capsys):
    """v0.2.0 caught every exception and printed 'fine if no local server is
    running' -- telling the user their broken config was OK."""
    probe = lambda url: EndpointProbe("bad_url", url, detail="missing protocol")  # noqa: E731
    code = _doctor(tmp_path, config='[provider]\nbase_url = "localhost:11434/v1"\n', probe=probe)
    out = capsys.readouterr().out

    assert code == 1  # doctor is usable as a scriptable setup gate
    assert "[FAIL] provider.base_url is not a usable URL" in out
    assert "fine if no local server" not in out
    assert "http://localhost:11434/v1" in out  # names the shape of a good one


def test_real_probe_classifies_a_missing_scheme_as_bad_url():
    result = probe_endpoint("localhost:11434/v1")
    assert result.status == "bad_url"


def test_unreachable_names_the_fix_and_is_not_a_failure(tmp_path, capsys):
    probe = lambda url: EndpointProbe("unreachable", url, detail="refused")  # noqa: E731
    code = _doctor(tmp_path, probe=probe)
    out = capsys.readouterr().out

    assert code == 0  # "no server running yet" is not a broken setup
    assert "[--] endpoint not reachable" in out
    assert "ollama serve" in out


def test_success_with_an_unusable_body_is_flagged(tmp_path, capsys):
    probe = lambda url: EndpointProbe("bad_payload", url, code=200)  # noqa: E731
    _doctor(tmp_path, probe=probe)
    out = capsys.readouterr().out
    assert "not with an OpenAI model list" in out
    assert "[ok] endpoint reachable" not in out


# --------------------------------------------------------------------------
# the model-existence check (never asked before)
# --------------------------------------------------------------------------


def test_missing_model_fails_and_says_how_to_get_it(tmp_path, capsys):
    """The shipped default is an ~18GB model almost nobody has pulled."""
    code = _doctor(tmp_path, probe=_ok_probe("llama3:8b", "phi4:latest"))
    out = capsys.readouterr().out

    assert code == 1
    assert f"[FAIL] model {DEFAULT_MODEL} is not available" in out
    assert "models you have: llama3:8b, phi4:latest" in out
    assert f"ollama pull {DEFAULT_MODEL}" in out


def test_present_model_passes(tmp_path, capsys):
    code = _doctor(tmp_path, probe=_ok_probe(DEFAULT_MODEL))
    out = capsys.readouterr().out

    assert code == 0
    assert f"[ok] provider.model {DEFAULT_MODEL} is available" in out


def test_role_models_are_checked_too(tmp_path, capsys):
    code = _doctor(
        tmp_path,
        config='[roles]\nplanner = "big-planner"\n',
        probe=_ok_probe(DEFAULT_MODEL),
    )
    out = capsys.readouterr().out

    assert code == 1
    assert "[FAIL] model big-planner is not available" in out
    assert "roles.planner" in out


def test_empty_model_list_is_called_out_explicitly(tmp_path, capsys):
    code = _doctor(tmp_path, probe=_ok_probe())
    out = capsys.readouterr().out

    assert code == 1
    assert "lists no models at all" in out


def test_long_model_lists_are_truncated(tmp_path, capsys):
    code = _doctor(tmp_path, probe=_ok_probe(*[f"m{i}" for i in range(9)]))
    out = capsys.readouterr().out
    assert code == 1
    assert "m0, m1, m2, m3, m4, ... (9 total)" in out


def test_latest_tag_is_the_same_model():
    assert _model_available("llama3", ("llama3:latest",))
    assert _model_available("llama3:latest", ("llama3",))
    assert not _model_available("llama3", ("llama3.1",))


def test_endpoint_or_model_trouble_points_at_the_offline_demo(tmp_path, capsys):
    _doctor(tmp_path, probe=_ok_probe("something-else"))
    assert "`ironcore demo` runs a real session fully offline" in capsys.readouterr().out


# --------------------------------------------------------------------------
# config discoverability
# --------------------------------------------------------------------------


def test_no_config_file_is_not_reported_as_config_loaded(tmp_path, capsys):
    """v0.2.0 printed '[ok] config loaded' with no config file anywhere."""
    _doctor(tmp_path)
    out = capsys.readouterr().out

    assert "config loaded" not in out
    assert "[--] no config file -- using defaults" in out
    assert "ironcore init" in out
    assert f"model {DEFAULT_MODEL}" in out


def test_a_real_config_file_is_named_by_path(tmp_path, capsys):
    _doctor(tmp_path, config='[safety]\nmode = "plan"\n')
    out = capsys.readouterr().out

    assert str(tmp_path / "user.toml") in out
    assert "(loaded)" in out
    assert "(absent)" in out  # the project file, named even though it is missing
    assert "mode plan" in out


# --------------------------------------------------------------------------
# git + mcp resolution
# --------------------------------------------------------------------------


def test_missing_git_is_reported_because_undo_silently_needs_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("ironcore.cli._which", lambda cmd: None)
    code = _doctor(tmp_path)
    out = capsys.readouterr().out

    assert code == 0  # a warning: IronCore still runs, undo just does not
    assert "[!!] git not found" in out
    assert "/undo" in out and "snapshots are disabled" in out


def test_present_git_is_reported(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("ironcore.cli._which", lambda cmd: "C:/git/git.exe")
    _doctor(tmp_path)
    assert "[ok] git found" in capsys.readouterr().out


def test_mcp_command_missing_from_path_is_a_failure(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("ironcore.cli._which", lambda cmd: None if cmd == "nope.cmd" else "C:/x")
    code = _doctor(tmp_path, config='[mcp.servers.gh]\ncommand = "nope.cmd"\n')
    out = capsys.readouterr().out

    assert code == 1
    assert "[FAIL] mcp gh: command 'nope.cmd' not found on PATH" in out


def test_mcp_url_only_entry_matches_the_managers_wording(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("ironcore.cli._which", lambda cmd: "C:/x")
    code = _doctor(tmp_path, config='[mcp.servers.remote]\nurl = "http://example.com/mcp"\n')
    out = capsys.readouterr().out

    assert code == 0  # not shipped yet is not a broken setup
    assert "[--] mcp remote: url-only entries are not supported yet" in out
    assert "will be skipped" in out
    # doctor and MCPManager must agree about why an entry is dropped
    assert "stdio only -- set 'command'" in out


def test_doctor_announces_a_quarantined_envelope_cache(tmp_path, capsys):
    """FIX-1's handoff left this for FIX-2: a corrupt profile cache is announced
    at TUI boot, so doctor must not be the one surface that stays quiet."""
    envelopes = tmp_path / "envelopes"
    envelopes.mkdir()
    from ironcore.envelope.profile import CapabilityProfile

    (envelopes / f"{CapabilityProfile.slug(DEFAULT_MODEL)}.json").write_bytes(b"{truncated")

    assert (
        cmd_doctor(
            project_dir=tmp_path,
            user_config=tmp_path / "absent.toml",
            env={},
            envelope_dir=envelopes,
            check_endpoint=False,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "[!!]" in out and "corrupt" in out
    assert ".corrupt" in out  # names where the evidence went


# --------------------------------------------------------------------------
# main(): a stranger gets a sentence, never a traceback
# --------------------------------------------------------------------------


def test_main_reports_config_errors_without_a_traceback(monkeypatch, capsys):
    """v0.2.0's main() had no exception guard: a stray quote in TOML tracebacked
    out of the primary entry point."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _boom(**kwargs):
        raise ConfigError("malformed config file C:/x/.ironcore/config.toml: bad (at line 3)")

    monkeypatch.setattr("ironcore.tui.app.run_app", _boom)

    assert main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("ironcore: malformed config file")
    assert "at line 3" in err
    assert "ironcore doctor" in err
    assert "Traceback" not in err


def test_main_backstops_any_unexpected_exception(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _boom(**kwargs):
        raise RuntimeError("state.json is half written")

    monkeypatch.setattr("ironcore.tui.app.run_app", _boom)

    assert main([]) == 1
    err = capsys.readouterr().err
    assert "ironcore: RuntimeError: state.json is half written" in err
    assert "Traceback" not in err


def test_argparse_errors_still_exit_via_systemexit():
    with pytest.raises(SystemExit):
        main(["--nonsense"])


# --------------------------------------------------------------------------
# ironcore init
# --------------------------------------------------------------------------


def test_parser_has_demo_and_init():
    parser = build_parser()
    assert parser.parse_args(["demo", "--smoke"]).smoke is True
    assert parser.parse_args(["demo"]).smoke is False
    assert parser.parse_args(["init"]).scope == "user"
    assert parser.parse_args(["init", "--project"]).scope == "project"


def test_init_writes_a_config_that_loads_and_names_every_off_switch(tmp_path, capsys):
    target = tmp_path / "config.toml"
    assert cmd_init(scope="user", user_config=target) == 0
    out = capsys.readouterr().out

    assert str(target) in out
    text = target.read_text(encoding="utf-8")
    for switch in ("auto_probe", "instant_seed", "auto_tune", "best_of_n", "network_tools"):
        assert switch in text
    for section in ("[provider]", "[safety]", "[roles]", "[envelope]", "[engine]", "[plugins]"):
        assert section in text
    # it must parse, and must not change behaviour just by existing
    written = Settings.load(project_dir=tmp_path / "nope", user_config=target, env={})
    assert written.model_dump() == Settings().model_dump()


def test_init_project_scope_writes_the_committable_path(tmp_path, capsys):
    assert cmd_init(scope="project", project_dir=tmp_path) == 0
    assert (tmp_path / ".ironcore" / "config.toml").exists()
    assert ".ironcore" in capsys.readouterr().out


def test_init_refuses_to_clobber_without_force(tmp_path, capsys):
    target = tmp_path / "config.toml"
    target.write_text("[provider]\nmodel = 'mine'\n", encoding="utf-8")

    assert cmd_init(scope="user", user_config=target) == 1
    assert "already exists" in capsys.readouterr().out
    assert "mine" in target.read_text(encoding="utf-8")  # untouched

    assert cmd_init(scope="user", user_config=target, force=True) == 0
    assert "mine" not in target.read_text(encoding="utf-8")


def test_doctor_sees_the_config_init_just_wrote(tmp_path, capsys):
    target = tmp_path / "user.toml"
    assert cmd_init(scope="user", user_config=target) == 0
    capsys.readouterr()

    assert (
        cmd_doctor(
            project_dir=tmp_path,
            user_config=target,
            env={},
            envelope_dir=tmp_path / "envelopes",
            check_endpoint=False,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert str(target) in out and "(loaded)" in out
    assert "no config file" not in out
