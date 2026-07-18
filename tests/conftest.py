"""Suite-wide hermeticity guards.

Two invariants live here because no individual test can be relied on to
remember them:

1. **The contributor's real home directory is off limits.** IronCore resolves
   its user config and its probe cache through ``Path.home()``
   (``envelope/suite.py``, ``config/settings.py``, ``cli.py``). A test that
   forgot to redirect ``default_envelope_dir`` overwrote the developer's own
   ``~/.ironcore/envelopes/<model>.json`` on every run. Redirecting HOME for
   every test makes that structurally impossible instead of something each new
   test has to remember.
2. **git is a soft dependency.** Snapshots shell out to the git binary, so the
   undo/redo tests genuinely cannot run without it. On a machine or a minimal
   container with no git they must SKIP, not error — CI runners always ship
   git, so only a contributor or a packager ever sees the difference.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

#: The contributor's ACTUAL home, captured at import time — before any fixture
#: can redirect it. Tests assert against this to prove the sandbox holds.
REAL_HOME = Path(os.path.expanduser("~"))


def _has_git() -> bool:
    """True when a git binary is reachable on PATH.

    Only ``OSError`` means "absent": a non-zero exit still proves the binary
    exists, and swallowing that would skip tests that ought to run.
    """
    try:
        subprocess.run(["git", "--version"], capture_output=True)
    except OSError:
        return False
    return True


#: Resolved once per session — PATH does not change mid-run, and probing the
#: binary per test would cost a process spawn each time.
HAS_GIT = _has_git()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_git: needs the git binary on PATH (snapshots shell out to it)",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Turn ``@pytest.mark.requires_git`` into a skip when git is missing.

    Done as a hook rather than a module-level ``skipif`` so the decision lives
    in one place and a test file only has to declare its dependency.
    """
    if HAS_GIT:
        return
    skip_no_git = pytest.mark.skip(reason="git binary not on PATH")
    for item in items:
        if "requires_git" in item.keywords:
            item.add_marker(skip_no_git)


@pytest.fixture(scope="session")
def real_home() -> Path:
    """The developer's true home dir, for tests that assert we never wrote there."""
    return REAL_HOME


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every home-directory lookup at a per-test throwaway directory.

    ``Path.home()`` reads USERPROFILE on Windows and HOME on POSIX, and git
    (msys) additionally consults HOMEDRIVE/HOMEPATH, so all four move together.

    This is a backstop, not a licence: a test that knows where it writes should
    still pass an explicit directory. Patching an attribute such as
    ``ironcore.envelope.suite.default_envelope_dir`` keeps working unchanged —
    an attribute patch never consults the environment, so this fixture cannot
    mask it.
    """
    home = tmp_path_factory.mktemp("home")
    drive, tail = os.path.splitdrive(str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", drive)  # "" off Windows — harmless, splitdrive says so
    monkeypatch.setenv("HOMEPATH", tail)
    return home
