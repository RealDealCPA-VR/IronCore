"""FIX-6: the suite must not touch the developer's machine.

"Fully offline" has to mean more than "no sockets". Running the suite used to
overwrite the contributor's real ``~/.ironcore/envelopes/mock.json``, and a
dozen tests hard-errored with FileNotFoundError on a machine without git
instead of skipping. These tests pin both guards from tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from ironcore.commands import build_default_registry as build_cmds
from ironcore.commands.base import CommandContext
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.probe_tools import ToolFormProbe
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` as {posix relpath: bytes} — {} if absent."""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


# --- guard 1: HOME is sandboxed --------------------------------------------


def test_home_lookups_resolve_inside_the_sandbox(sandbox_home, real_home):
    """``Path.home()`` — the single funnel every ~/.ironcore path goes through
    (envelope/suite.py, config/settings.py, cli.py) — must not be the real one."""
    assert Path.home().resolve() == sandbox_home.resolve()
    assert Path.home().resolve() != real_home.resolve()
    assert Path("~").expanduser().resolve() == sandbox_home.resolve()


def test_probe_through_the_command_registry_never_writes_to_the_real_home(
    tmp_path, sandbox_home, real_home
):
    """The exact leak FIX-6 was filed for.

    ``/envelope``'s sibling test dispatched ``/probe`` with a schedule that runs
    the coroutine immediately, and — unlike its two neighbours — never redirected
    ``default_envelope_dir``. So a plain ``pytest`` clobbered the contributor's
    cached profile for model id "mock". Note this test DELIBERATELY does not
    patch ``default_envelope_dir``: the conftest HOME sandbox is what must hold,
    which is what makes the next such omission harmless.
    """
    before = _tree(real_home / ".ironcore")

    settings = Settings()
    engine = TurnEngine(
        MockProvider([]),
        build_tools(settings, tmp_path),
        settings,
        CapabilityProfile(model_id="mock", honest_context=8192),
        Mode.AUTO,
        workspace=tmp_path,
        snapshots=None,
    )
    ctx = CommandContext(
        settings=settings,
        extra={"engine": engine, "schedule": lambda coro: asyncio.run(coro)},
    )
    build_cmds().dispatch("/probe", ctx)

    assert _tree(real_home / ".ironcore") == before  # the developer's cache is untouched
    # ...and the write really did happen, just inside the sandbox — proving the
    # assertion above passes because HOME moved, not because nothing ran
    assert (sandbox_home / ".ironcore" / "envelopes" / "mock.json").is_file()


def test_an_explicit_envelope_dir_still_beats_the_sandbox(tmp_path, monkeypatch, sandbox_home):
    """The autouse fixture must not mask a test's deliberate patching — an
    attribute patch never consults the environment, so tests keep full control."""
    monkeypatch.setattr("ironcore.envelope.suite.default_envelope_dir", lambda: tmp_path / "env")
    monkeypatch.setattr(
        "ironcore.envelope.suite.default_probe_suite", lambda: [ToolFormProbe(trials=1)]
    )
    from ironcore.envelope.suite import default_envelope_dir

    assert default_envelope_dir() == tmp_path / "env"
    assert not (sandbox_home / ".ironcore").exists()


# --- guard 2: git-dependent tests skip instead of erroring ------------------


def test_git_dependent_tests_skip_when_git_is_absent(tmp_path):
    """Run the snapshot suite in a child process whose PATH has no git.

    Before FIX-6 these nine tests raised FileNotFoundError / SnapshotError.
    CI runners always ship git, so nothing but this test surfaces the
    difference — and a contributor or minimal-container packager is exactly
    who the release is for.
    """
    empty = tmp_path / "no-binaries-here"
    empty.mkdir()
    # Inherit only what the interpreter itself needs: SYSTEMROOT for Windows
    # DLL/socket init, TEMP/TMP so the child's tmp_path does NOT fall back to
    # the repo working directory. PATH is the one thing deliberately emptied.
    env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "TEMP", "TMP", "TMPDIR")}
    env["PATH"] = str(empty)  # no git anywhere on it
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        # no extra -q: pyproject's addopts already supplies one, and -qq would
        # suppress the very summary line this test reads
        [sys.executable, "-m", "pytest", "tests/test_snapshots.py", "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, out  # no errors, no failures
    assert "9 skipped" in out, out  # genuinely skipped, not silently deselected
    # the two that do NOT need the binary must still really run
    assert "2 passed" in out, out
