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

import os
import sys
from pathlib import Path

import httpx
import pytest

from ironcore.cli import (
    EndpointProbe,
    _display_path,
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


@pytest.mark.parametrize("code", [404, 500, 503])
def test_http_error_is_never_reported_as_ok(tmp_path, capsys, code):
    """v0.2.0 printed '[ok] endpoint reachable: ... (404)'.

    The exit code matters as much as the text: the round-1 fix printed an honest
    ``[!!]`` here but left ok untouched, so ``ironcore doctor && ironcore`` still
    green-lit a server IronCore cannot talk to. This is the exact
    vLLM/LM-Studio-shaped case -- /api/version answers, /v1/models 404s.
    """
    probe = lambda url: EndpointProbe("http_error", f"{url}/models", code=code)  # noqa: E731
    rc = _doctor(tmp_path, probe=probe)
    out = capsys.readouterr().out

    assert rc == 1  # a server that answers but is not OpenAI-compatible is broken config
    assert "[ok] endpoint reachable" not in out
    assert f"got HTTP {code}" in out
    assert "OpenAI-compatible" in out
    assert "[FAIL]" in out  # not a survivable warning


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


def _capture_get(monkeypatch, handler):
    """Like _fake_httpx_get, but records the kwargs (headers) too."""
    calls: list[dict] = []

    def _get(url, **kwargs):
        calls.append({"url": str(url), **kwargs})
        return handler(str(url))

    monkeypatch.setattr(httpx, "get", _get)
    return calls


def test_probe_authenticates_like_the_real_client_does(monkeypatch):
    """Round 1 sent NO Authorization header, while openai_compat.py and
    detect.py both send `Bearer <api_key>`. Against vLLM or llama.cpp started
    with --api-key (or any hosted OpenAI-compatible provider) the anonymous
    probe gets 401, so doctor failed the gate on a setup that works."""
    calls = _capture_get(monkeypatch, lambda url: httpx.Response(200, json={"data": []}))
    probe_endpoint("http://localhost:11434/v1", api_key="sk-secret-123")

    assert calls[0]["headers"] == {"Authorization": "Bearer sk-secret-123"}


def test_doctor_probes_with_the_configured_key(tmp_path, monkeypatch, capsys):
    """End to end: the key from [provider] reaches the wire, and an endpoint
    that requires it comes back [ok] rather than failing the install gate."""
    key = "sk-live-abc"

    def _handler(url):
        return httpx.Response(200, json={"data": [{"id": "llama3"}]})

    calls = _capture_get(monkeypatch, _handler)
    user = tmp_path / "user.toml"
    user.write_text(
        f'[provider]\napi_key = "{key}"\nmodel = "llama3"\n',
        encoding="utf-8",
    )
    rc = cmd_doctor(
        project_dir=tmp_path,
        user_config=user,
        env={},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=True,
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert calls[0]["headers"] == {"Authorization": f"Bearer {key}"}
    assert "[ok] endpoint reachable" in out
    assert key not in out  # never echo the key


def test_rejected_key_blames_api_key_not_base_url(tmp_path, capsys):
    """401/403 means the endpoint IS OpenAI-shaped and talking to us -- it just
    refused the key we sent. Sending the user to edit base_url would send them
    to change the one field that is correct."""
    probe = lambda url: EndpointProbe("unauthorized", f"{url}/models", code=401)  # noqa: E731
    rc = _doctor(tmp_path, probe=probe)
    out = capsys.readouterr().out

    assert rc == 1
    assert "endpoint rejected our API key" in out
    assert "set [provider] api_key" in out
    assert "IRONCORE_API_KEY" in out
    assert "set [provider] base_url" not in out
    assert "is this an OpenAI-compatible endpoint?" not in out


@pytest.mark.parametrize("code", [401, 403])
def test_probe_classifies_auth_rejection_apart_from_other_http_errors(monkeypatch, code):
    _fake_httpx_get(monkeypatch, lambda url: httpx.Response(code, text="nope"))
    result = probe_endpoint("http://x/v1", api_key="k")

    assert result.status == "unauthorized"
    assert result.code == code


def test_probe_never_carries_the_api_key_back_in_its_detail(monkeypatch):
    """Redaction discipline (openai_compat.py's _redact): the key must not be
    able to reach the terminal -- or a pasted bug report -- via an exception."""
    key = "sk-secret-123"

    def _boom(url, **kwargs):
        raise httpx.ConnectError(f"failed to connect using header Bearer {key}")

    monkeypatch.setattr(httpx, "get", _boom)
    result = probe_endpoint("http://x/v1", api_key=key)

    assert key not in result.detail
    assert "[redacted]" in result.detail


def test_probe_sends_no_header_when_no_key_is_configured(monkeypatch):
    calls = _capture_get(monkeypatch, lambda url: httpx.Response(200, json={"data": []}))
    probe_endpoint("http://x/v1", api_key="")
    assert calls[0]["headers"] == {}


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
    """200 from something that is not a model server is misconfiguration, so it
    must fail the gate too -- round 1 printed the truth but still exited 0."""
    probe = lambda url: EndpointProbe("bad_payload", url, code=200)  # noqa: E731
    rc = _doctor(tmp_path, probe=probe)
    out = capsys.readouterr().out

    assert rc == 1
    assert "not with an OpenAI model list" in out
    assert "[ok] endpoint reachable" not in out
    assert "[FAIL]" in out


def test_only_a_stopped_server_survives_the_gate(tmp_path, capsys):
    """The one endpoint status that must NOT fail: nothing is listening yet.
    Pins the boundary so a future 'make doctor stricter' cannot blur it."""
    failing = ("bad_url", "http_error", "bad_payload")
    for status in failing:
        assert _doctor(tmp_path, probe=lambda url, s=status: EndpointProbe(s, url, code=500)) == 1
    assert _doctor(tmp_path, probe=lambda url: EndpointProbe("unreachable", url)) == 0
    capsys.readouterr()


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


def test_a_config_error_that_names_no_file_gets_one_named_for_it(tmp_path, capsys):
    """An invalid VALUE is reported by key, not by file ("invalid safety.mode
    'yolo'"), so a user with both a user and a project config cannot tell which
    to open -- doctor knows both and says so."""
    user = tmp_path / "user.toml"
    user.write_text('[safety]\nmode = "yolo"\n', encoding="utf-8")

    rc = cmd_doctor(
        project_dir=tmp_path,
        user_config=user,
        env={},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=False,
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "yolo" in out
    assert str(user) in out  # the question that actually matters: which file?


def test_an_unreadable_config_file_names_itself_without_a_traceback(tmp_path, capsys):
    """FIX-3 round 1: a non-UTF8 byte used to escape as a raw UnicodeDecodeError
    with no path in it; it is now a ConfigError that names the file itself."""
    user = tmp_path / "user.toml"
    user.write_bytes(b'[provider]\nmodel = "caf\xe9"\n')

    rc = cmd_doctor(
        project_dir=tmp_path,
        user_config=user,
        env={},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=False,
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "UTF-8" in out
    assert str(user) in out
    assert "Traceback" not in out
    assert "config file(s) doctor read" not in out  # already named, not told twice


def test_a_config_error_that_already_names_the_file_is_not_told_twice(tmp_path, capsys):
    user = tmp_path / "user.toml"
    user.write_text('[provider]\nmodel = "oops\n', encoding="utf-8")

    assert (
        cmd_doctor(
            project_dir=tmp_path,
            user_config=user,
            env={},
            envelope_dir=tmp_path / "envelopes",
            check_endpoint=False,
        )
        == 1
    )
    out = capsys.readouterr().out
    assert str(user) in out
    assert "config file(s) doctor read" not in out


def test_doctor_runs_when_home_cannot_be_resolved(tmp_path, capsys, monkeypatch):
    """Every path doctor needs was injected, so an unresolvable home is not its
    problem -- it used to compute Path.home() eagerly and discard the value."""

    def _no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr("pathlib.Path.home", staticmethod(_no_home))

    assert (
        cmd_doctor(
            project_dir=tmp_path,
            user_config=tmp_path / "absent.toml",
            env={},
            envelope_dir=tmp_path / "envelopes",
            check_endpoint=False,
        )
        == 0
    )
    capsys.readouterr()


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


def test_doctor_prints_the_t8_clamp_it_applied(tmp_path, capsys):
    """FIX-3: doctor reports the EFFECTIVE setup. A project config that asked
    for AUTO and got MANUAL is exactly the kind of gap doctor exists to name."""
    user = tmp_path / "absent.toml"
    (tmp_path / ".ironcore").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ironcore" / "config.toml").write_text(
        '[safety]\nmode = "auto"\nnetwork_tools = true\n', encoding="utf-8"
    )
    code = cmd_doctor(
        project_dir=tmp_path,
        user_config=user,
        env={},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=False,
    )
    out = capsys.readouterr().out

    assert code == 0  # a clamp is a control working, not a broken install
    assert "mode manual" in out  # the effective line tells the truth
    assert "clamped to your ceiling 'manual'" in out
    assert "network_tools" in out


def test_present_git_is_reported(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("ironcore.cli._which", lambda cmd: "C:/git/git.exe")
    _doctor(tmp_path)
    assert "[ok] git found" in capsys.readouterr().out


def test_mcp_command_missing_from_path_is_a_failure_when_it_would_be_launched(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("ironcore.cli._which", lambda cmd: None if cmd == "nope.cmd" else "C:/x")
    code = _doctor(
        tmp_path,
        config='[safety]\nnetwork_tools = true\n[mcp.servers.gh]\ncommand = "nope.cmd"\n',
    )
    out = capsys.readouterr().out

    assert code == 1
    assert "[FAIL] mcp gh: command 'nope.cmd' not found on PATH" in out


def test_mcp_command_missing_is_only_a_warning_while_the_server_stays_unregistered(
    tmp_path, capsys, monkeypatch
):
    """Round 1 printed '...stay unregistered until safety.network_tools = true'
    and then FAILED the gate on one of those very servers -- doctor refusing an
    install over a component it had just declared inert, one line apart.
    Nothing will ever launch that command, so it cannot break anything today."""
    monkeypatch.setattr("ironcore.cli._which", lambda cmd: None if cmd == "nope.cmd" else "C:/x")
    code = _doctor(tmp_path, config='[mcp.servers.gh]\ncommand = "nope.cmd"\n')
    out = capsys.readouterr().out

    assert code == 0  # `ironcore doctor && ironcore` must still run
    assert "stay unregistered until safety.network_tools = true" in out
    assert "[!!] mcp gh: command 'nope.cmd' not found on PATH" in out
    assert "[FAIL]" not in out
    assert "before" in out and "turning safety.network_tools on" in out


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


def test_a_non_file_error_is_not_given_file_advice(monkeypatch, capsys):
    """The backstop catches errors with no file behind them (a missing HOME, a
    broken terminal). Telling those users to 'fix the file or delete it' is
    advice about a file that has nothing to do with the failure."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _boom(**kwargs):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr("ironcore.tui.app.run_app", _boom)

    assert main([]) == 1
    err = capsys.readouterr().err
    assert "Could not determine home directory" in err
    assert "delete it to fall back to defaults" not in err
    assert "ironcore doctor" in err  # still tells them what to do next


def test_version_stays_import_light(monkeypatch, capsys):
    """`ironcore --version` must not pay for pydantic. main()'s guard used to
    import ConfigError unconditionally, which pulled the whole settings module
    into every invocation -- contradicting the invariant stated in cli.py."""
    import subprocess

    probe = (
        "import sys; from ironcore.cli import main; main(['--version']);"
        " sys.exit(1 if 'pydantic' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", probe], capture_output=True).returncode == 0


def test_config_errors_are_still_recognised_through_the_lazy_guard(monkeypatch, capsys):
    """The import-light guard identifies ConfigError via sys.modules rather than
    importing it; make sure that still classifies a real one correctly (file
    advice, and no 'ConfigError:' type prefix)."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _boom(**kwargs):
        raise ConfigError("malformed config file: bad (at line 3)")

    monkeypatch.setattr("ironcore.tui.app.run_app", _boom)

    assert main([]) == 1
    err = capsys.readouterr().err
    assert "ironcore: malformed config file" in err
    assert "ConfigError" not in err  # the message, not the exception type
    assert "delete it to fall back to defaults" in err


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


def test_starter_config_commented_defaults_are_really_the_defaults():
    """The file promises a commented line shows the actual default, so
    uncommenting it changes nothing. It said `# vision = false`, which is NOT
    the default (unset = trust the probed profile), so uncommenting it would
    silently disable vision on a model that measured as having it.

    Checks every documented default against the live Settings model, so a future
    default change that forgets this file fails here.
    """
    import re
    import tomllib

    from ironcore.cli import STARTER_CONFIG

    defaults = Settings()
    section: str | None = None
    checked = 0
    for line in STARTER_CONFIG.splitlines():
        header = re.match(r"^#?\s*\[([a-z.]+)\]", line)
        if header:
            section = header.group(1)
            continue
        entry = re.match(r"^#\s*([a-z_]+)\s*=\s*(.+?)(?:\s+#.*)?$", line)
        # roles.* and mcp.servers.* are examples, not defaults; UNSET keys have
        # no value that could express their default.
        if not entry or section in (None, "roles") or "UNSET by default" in line:
            continue
        if section.startswith("mcp"):
            continue
        key, raw = entry.group(1), entry.group(2)
        documented = tomllib.loads(f"v = {raw}")["v"]
        assert documented == getattr(getattr(defaults, section), key), (
            f"STARTER_CONFIG documents [{section}] {key} = {raw}, "
            f"but the default is {getattr(getattr(defaults, section), key)!r}"
        )
        checked += 1
    assert checked >= 8, f"parsed only {checked} documented defaults -- the parser drifted"


def test_starter_config_does_not_claim_a_default_for_vision(tmp_path):
    from ironcore.cli import STARTER_CONFIG

    assert "# vision = false" not in STARTER_CONFIG
    assert "UNSET by default" in STARTER_CONFIG


def test_starter_config_does_not_call_its_examples_defaults():
    """The header promised 'uncommenting one changes nothing', but [roles] and
    [mcp.servers.example] hold invented values -- uncommenting the mcp block
    genuinely registers a server. The pinning test above only compares scalar
    lines, so it cannot catch an over-broad claim in the prose."""
    from ironcore.cli import STARTER_CONFIG

    lines = STARTER_CONFIG.splitlines()
    for header in ("# [roles]", "# [mcp.servers.example]"):
        marked = [ln for ln in lines if ln.startswith(header)]
        assert marked, f"{header} vanished from STARTER_CONFIG"
        assert "EXAMPLE" in marked[0], f"{header} is not marked as an example"
    assert "DOES change behaviour" in STARTER_CONFIG


def test_init_on_a_directory_does_not_suggest_a_remedy_that_cannot_work(tmp_path, capsys):
    """--force would just hit the OSError branch, so do not offer it."""
    target = tmp_path / "config.toml"
    target.mkdir()

    assert cmd_init(scope="user", user_config=target) == 1
    out = capsys.readouterr().out
    assert "is a directory" in out
    assert "--force" not in out
    assert "remove or rename it" in out

    assert cmd_init(scope="user", user_config=target, force=True) == 1  # still no crash
    assert "is a directory" in capsys.readouterr().out


def test_home_paths_print_as_tilde_so_output_carries_no_username(tmp_path, capsys, monkeypatch):
    """Doctor output gets pasted into issues and screenshots -- it must not leak $HOME."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert (
        cmd_doctor(
            project_dir=tmp_path / "proj",
            user_config=None,  # resolves under the patched home
            env={},
            envelope_dir=None,  # ditto
            check_endpoint=False,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert f"~{os.sep}.ironcore" in out
    assert str(tmp_path) not in out


def test_display_path_degrades_instead_of_raising(tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere" / "config.toml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert _display_path(outside) == str(outside)  # not under home -> unchanged

    def _no_home(cls):
        raise RuntimeError("home directory can't be determined")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))
    assert _display_path(outside) == str(outside)  # unresolvable home -> no crash


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
