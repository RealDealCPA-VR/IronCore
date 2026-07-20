"""The update notifier: fail-silent, cached, offline-tested.

Every test injects ``fetch`` (and ``now``) so nothing here dials PyPI — the
whole point of the notifier is that it can be exercised without a network, and
a test that needed one would violate the suite's offline contract (AGENTS.md).

The notifier's non-negotiables, each pinned below:
* the DISTRIBUTION is ``ironcore-cli`` (not the import name ``ironcore``), so a
  rename cannot silently point the URL at the wrong project;
* any failure — network, DNS, non-JSON, missing key — reads as "no update",
  never a raised exception;
* a fresh cache is used WITHOUT dialing; a stale one refetches and rewrites; a
  corrupt one is tolerated; the write is atomic;
* ``ironcore doctor`` prints the right line, never FAILs on an offline check, and
  a non-interactive doctor never dials.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

import ironcore
from ironcore import term
from ironcore.cli import cmd_doctor
from ironcore.update import (
    DIST_NAME,
    PYPI_JSON_URL,
    UpdateInfo,
    check_for_update,
    default_cache_path,
    is_newer,
    latest_version,
)

CURRENT = ironcore.__version__


@pytest.fixture(autouse=True)
def _fresh_term_console():
    """Guarantee ``capsys`` captures ``doctor`` output in this file too.

    ``tests/test_term.py`` monkeypatches the process-wide ``term`` console's
    ``file`` and — because ``file`` is a property whose setter writes
    ``_file`` — its teardown restores a STALE ``sys.stdout`` rather than the
    ``None`` the console shipped with, so the console stops resolving the live
    stream and ``capsys`` sees nothing. That file sorts before this one, so its
    residue lands here. Clearing the cache rebuilds a ``_file=None`` console that
    follows ``sys.stdout`` per write, exactly as ``term``'s contract promises.
    """
    term._cached_console.cache_clear()
    yield
    term._cached_console.cache_clear()


def _pypi_json(version: str) -> str:
    """A minimal but PyPI-shaped ``/pypi/<name>/json`` body."""
    return json.dumps({"info": {"name": DIST_NAME, "version": version}, "releases": {}})


def _returns(text: str):
    """A fetch that ignores its args and returns ``text``."""
    return lambda url, timeout: text


# --------------------------------------------------------------------------
# the distribution name / URL (guards a future rename regressing the URL)
# --------------------------------------------------------------------------


def test_dist_name_is_the_pypi_project_not_the_import_name():
    assert DIST_NAME == "ironcore-cli"
    assert PYPI_JSON_URL == "https://pypi.org/pypi/ironcore-cli/json"


def test_default_cache_path_lives_under_home_dot_ironcore():
    path = default_cache_path()
    assert path.name == "update-check.json"
    assert path.parent.name == ".ironcore"


def test_packaging_is_a_declared_runtime_dependency():
    """Regression (validator round 1): ``is_newer`` imports ``packaging`` on both
    the ``ironcore doctor`` and the TUI-boot paths, so it MUST be a declared
    runtime dependency — not silently inherited from the dev-only ``pytest``.

    The dev suite hid the gap: ``pytest`` pulls ``packaging`` into the dev env, so
    every test had it. But a stock install (``pip``/``pipx``/``uv tool install
    ironcore-cli`` into a clean env — the README's recommended methods) omitted
    it, and ``ironcore doctor`` crashed with ``ModuleNotFoundError`` + exit 1.
    Pin the declaration so a future dependency prune cannot drop it again.
    """
    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    names = {re.split(r"[<>=!~ ]", d, maxsplit=1)[0].strip().lower() for d in deps}
    assert "packaging" in names, f"packaging must be a runtime dep; saw {sorted(names)}"


# --------------------------------------------------------------------------
# latest_version: parses the body, fail-silent on every bad input
# --------------------------------------------------------------------------


def test_latest_version_parses_info_version():
    assert latest_version(fetch=_returns(_pypi_json("1.2.3"))) == "1.2.3"


def test_latest_version_requests_the_dist_json_url_with_the_timeout():
    seen: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> str:
        seen.append((url, timeout))
        return _pypi_json("1.0.0")

    latest_version(fetch=fetch, timeout=1.5)
    assert seen == [(PYPI_JSON_URL, 1.5)]


def test_latest_version_is_none_when_fetch_raises():
    def boom(url: str, timeout: float) -> str:
        raise RuntimeError("network down")  # any exception -> None, never propagates

    assert latest_version(fetch=boom) is None


def test_latest_version_is_none_on_non_json_body():
    assert latest_version(fetch=_returns("<html>not json</html>")) is None


def test_latest_version_is_none_when_info_version_is_missing_or_bad():
    assert latest_version(fetch=_returns(json.dumps({"info": {}}))) is None
    assert latest_version(fetch=_returns(json.dumps({}))) is None
    assert latest_version(fetch=_returns(json.dumps({"info": {"version": ""}}))) is None
    assert latest_version(fetch=_returns(json.dumps({"info": {"version": 123}}))) is None


# --------------------------------------------------------------------------
# is_newer: PEP 440 comparison, pre-release aware, fail-silent
# --------------------------------------------------------------------------


def test_is_newer_true_when_latest_is_ahead():
    assert is_newer("0.3.1", "0.3.2") is True
    assert is_newer("0.3.1", "0.4.0") is True
    assert is_newer("0.3.2", "0.3.10") is True  # numeric ordering, not lexical


def test_is_newer_false_when_equal_or_behind():
    assert is_newer("0.3.1", "0.3.1") is False  # equal is nothing to nudge
    assert is_newer("0.3.1", "0.3.0") is False
    assert is_newer("0.3.1", "0.2.9") is False


def test_is_newer_orders_prereleases_sensibly():
    assert is_newer("0.3.1", "0.3.2rc1") is True  # a newer pre-release exists
    assert is_newer("0.3.2rc1", "0.3.2") is True  # the final beats its own rc
    assert is_newer("0.3.2", "0.3.2rc1") is False  # an rc does not beat the final


def test_is_newer_is_false_on_an_unparseable_version():
    assert is_newer("0.3.1", "not-a-version") is False
    assert is_newer("garbage", "0.3.2") is False


def test_is_newer_is_false_when_packaging_is_missing(monkeypatch):
    """Regression (validator round 1, defense in depth): ``packaging`` is a
    declared runtime dep now, but ``is_newer`` must ALSO fail-silent if it ever
    goes missing again. Its import lives inside the ``try``, so a pruned
    dependency reads as "not newer" — never a ``ModuleNotFoundError`` that would
    crash a launch or turn ``ironcore doctor`` into a traceback + exit 1.

    Simulated by poisoning ``sys.modules`` so the lazy ``from packaging.version
    import parse`` raises ``ImportError`` exactly as a stock install would.
    """
    monkeypatch.setitem(sys.modules, "packaging", None)
    monkeypatch.setitem(sys.modules, "packaging.version", None)
    assert is_newer("0.3.1", "9999.0.0") is False  # would be True if packaging loaded


# --------------------------------------------------------------------------
# check_for_update: the cache-aware verdict
# --------------------------------------------------------------------------


def test_reports_a_newer_release_as_update_info(tmp_path):
    cache = tmp_path / "update-check.json"
    info = check_for_update("0.3.1", cache_path=cache, fetch=_returns(_pypi_json("0.3.2")), now=1.0)
    assert info == UpdateInfo(current="0.3.1", latest="0.3.2", newer=True)


def test_none_when_already_up_to_date_but_cache_is_still_written(tmp_path):
    cache = tmp_path / "update-check.json"
    info = check_for_update("0.3.1", cache_path=cache, fetch=_returns(_pypi_json("0.3.1")), now=5.0)
    assert info is None  # nothing to nudge
    # ...but the check still primed the cache so the next launch is a cache hit.
    assert json.loads(cache.read_text(encoding="utf-8")) == {"checked_at": 5.0, "latest": "0.3.1"}


def test_none_when_offline_and_no_cache_exists(tmp_path):
    cache = tmp_path / "update-check.json"

    def boom(url: str, timeout: float) -> str:
        raise RuntimeError("offline")

    assert check_for_update("0.3.1", cache_path=cache, fetch=boom, now=1.0) is None
    assert not cache.exists()  # nothing usable to write


def test_a_fresh_cache_is_used_without_calling_fetch(tmp_path):
    cache = tmp_path / "update-check.json"
    cache.write_text(json.dumps({"checked_at": 1000.0, "latest": "9.9.9"}), encoding="utf-8")
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> str:
        calls.append(url)
        return _pypi_json("0.0.1")

    info = check_for_update(
        "0.3.1", cache_path=cache, fetch=fetch, now=1000.0 + 100, ttl_seconds=86400
    )
    assert calls == []  # inside the TTL: never dialed
    assert info is not None and info.latest == "9.9.9"


def test_a_stale_cache_triggers_a_fetch_and_rewrite(tmp_path):
    cache = tmp_path / "update-check.json"
    cache.write_text(json.dumps({"checked_at": 0.0, "latest": "0.1.0"}), encoding="utf-8")
    now = 100_000.0  # far beyond the ttl from checked_at=0

    info = check_for_update(
        "0.3.1", cache_path=cache, fetch=_returns(_pypi_json("0.4.0")), now=now, ttl_seconds=86400
    )
    assert info is not None and info.latest == "0.4.0"
    assert json.loads(cache.read_text(encoding="utf-8")) == {"checked_at": now, "latest": "0.4.0"}


def test_a_corrupt_cache_is_tolerated_and_refetched(tmp_path):
    cache = tmp_path / "update-check.json"
    cache.write_bytes(b"{truncated not json")

    info = check_for_update("0.3.1", cache_path=cache, fetch=_returns(_pypi_json("0.5.0")), now=1.0)
    assert info is not None and info.latest == "0.5.0"
    # it heals: the corrupt bytes are replaced with a valid cache.
    assert json.loads(cache.read_text(encoding="utf-8"))["latest"] == "0.5.0"


def test_a_non_utf8_cache_is_tolerated(tmp_path):
    cache = tmp_path / "update-check.json"
    cache.write_bytes(b"\xff\xfe not even utf-8")

    info = check_for_update("0.3.1", cache_path=cache, fetch=_returns(_pypi_json("0.6.0")), now=1.0)
    assert info is not None and info.latest == "0.6.0"


def test_a_cache_missing_a_field_is_treated_as_no_cache(tmp_path):
    cache = tmp_path / "update-check.json"
    cache.write_text(json.dumps({"latest": "9.9.9"}), encoding="utf-8")  # no checked_at
    info = check_for_update("0.3.1", cache_path=cache, fetch=_returns(_pypi_json("0.7.0")), now=1.0)
    assert info is not None and info.latest == "0.7.0"  # refetched, not the cached 9.9.9


def test_cache_write_is_atomic_leaving_no_partial_file_on_failure(tmp_path, monkeypatch):
    """A simulated publish failure (os.replace raising) must leave neither a
    partial file at the live path nor an orphan staging file — the write-then-
    rename shape core/state.py and envelope/profile.py share."""
    cache = tmp_path / "update-check.json"

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    info = check_for_update("0.3.1", cache_path=cache, fetch=_returns(_pypi_json("0.8.0")), now=1.0)

    # a cache we cannot write is not a crash: the verdict is still correct.
    assert info is not None and info.latest == "0.8.0"
    # ...and nothing partial was left behind.
    assert not cache.exists()
    assert list(tmp_path.glob(".update-check.json.*")) == []


def test_check_for_update_never_raises_even_with_a_wild_fetch(tmp_path):
    """Belt and braces: the whole surface is fail-silent."""
    cache = tmp_path / "update-check.json"
    for bad in (_returns("]["), _returns("null"), _returns("42")):
        assert check_for_update("0.3.1", cache_path=cache, fetch=bad, now=1.0) is None


# --------------------------------------------------------------------------
# ironcore doctor: the one-line nudge, offline-safe, TTY-gated
# --------------------------------------------------------------------------


def _doctor(tmp_path, *, config="", update_fetch=None) -> int:
    """Hermetic doctor: injected config, no endpoint probe, injected update fetch."""
    user = tmp_path / "user.toml"
    user.write_text(config, encoding="utf-8")
    return cmd_doctor(
        project_dir=tmp_path,
        user_config=user if config else tmp_path / "absent.toml",
        env={},
        envelope_dir=tmp_path / "envelopes",
        check_endpoint=False,
        update_fetch=update_fetch,
    )


def test_doctor_prints_the_update_line_when_behind(tmp_path, capsys):
    rc = _doctor(tmp_path, update_fetch=_returns(_pypi_json("9999.0.0")))
    out = capsys.readouterr().out
    assert rc == 0  # an available update is not a broken setup
    assert "[--] update: 9999.0.0 available -- pip install -U ironcore-cli" in out


def test_doctor_prints_up_to_date_when_current(tmp_path, capsys):
    rc = _doctor(tmp_path, update_fetch=_returns(_pypi_json(CURRENT)))
    out = capsys.readouterr().out
    assert rc == 0
    assert f"[ok] up to date ({CURRENT})" in out


def test_doctor_update_check_offline_is_silent_and_never_a_failure(tmp_path, capsys):
    def boom(url: str, timeout: float) -> str:
        raise RuntimeError("no network")

    rc = _doctor(tmp_path, update_fetch=boom)
    out = capsys.readouterr().out
    assert rc == 0  # being offline is not a doctor failure
    assert "update:" not in out
    assert "up to date" not in out
    assert "Traceback" not in out


def test_doctor_skips_the_check_entirely_when_disabled(tmp_path, capsys):
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> str:
        calls.append(url)
        return _pypi_json("9999.0.0")

    rc = _doctor(tmp_path, config="[update]\ncheck = false\n", update_fetch=fetch)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []  # [update] check = false means the fetch is never invoked
    assert "update:" not in out
    assert "up to date" not in out


def test_doctor_survives_missing_packaging(tmp_path, capsys, monkeypatch):
    """Regression (validator round 1): in a stock install ``packaging`` was
    absent from the runtime closure, so the notifier's ``is_newer`` raised
    ``ModuleNotFoundError`` and turned an ``ironcore doctor`` run into
    ``ironcore: ModuleNotFoundError: No module named 'packaging'`` + exit 1 — a
    doctor FAILURE caused solely by the notifier. Even with ``packaging`` gone,
    doctor must stay green and traceback-free (offline/degraded is not a FAIL).
    """
    monkeypatch.setitem(sys.modules, "packaging", None)
    monkeypatch.setitem(sys.modules, "packaging.version", None)
    rc = _doctor(tmp_path, update_fetch=_returns(_pypi_json("9999.0.0")))
    out = capsys.readouterr().out
    assert rc == 0  # the notifier is never the reason doctor fails
    assert "Traceback" not in out
    assert "ModuleNotFoundError" not in out


def test_doctor_does_not_dial_from_a_non_tty(tmp_path, monkeypatch):
    """The real path (no injected fetch) must never dial PyPI from a
    non-interactive stdout (a pipe, a redirect, CI). Asserted by counting httpx
    calls — an exception would be swallowed by latest_version's fail-silent
    guard, so count the invocation rather than raise from it."""
    import httpx

    from ironcore import cli

    calls: list[object] = []

    def _get(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("should never be reached from a non-TTY")

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: False)  # force the non-TTY branch
    rc = _doctor(tmp_path)  # update_fetch=None -> real, TTY-gated path
    assert rc == 0
    assert calls == []  # the notifier never dialed


def test_doctor_dials_the_real_fetch_when_interactive(tmp_path, monkeypatch, capsys):
    """The mirror image: force an interactive stdout and prove the real httpx
    fetch IS reached (and its result rendered), so the TTY gate is a gate, not a
    silent off-switch that never dials."""
    import httpx

    from ironcore import cli

    class _FakeResp:
        text = json.dumps({"info": {"version": "9999.0.0"}})

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    rc = _doctor(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "update: 9999.0.0 available -- pip install -U ironcore-cli" in out
