"""Update notifier: a gentle "a newer version is available" nudge.

This is the harness's own maintenance ping, not a model tool — so it obeys the
same trust posture as the rest of IronCore: it is **opt-out** ([update] check),
**cached** (a normal launch does not dial PyPI), **fail-silent** (any error is
swallowed and reads as "no update"), **short-timeout**, and **never
auto-installs** — the upgrade command is printed for the human to run.

Rules of this module (docs/ARCHITECTURE.md §4): stdlib + httpx + packaging only.
It imports nothing from ``tui/`` or ``core/`` — the surfaces (``cli.py`` doctor,
``tui/app.py`` boot note) call in, never the reverse.

The DISTRIBUTION on PyPI is ``ironcore-cli`` (the bare name was refused as too
similar to the unrelated ``iron-core``); the import package and the command are
still ``ironcore``. So the JSON metadata URL is keyed to ``DIST_NAME``, and a
guard test pins it against a future rename regressing the URL.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ironcore import __version__

#: The PyPI project name — NOT the import package ``ironcore``. Load-bearing:
#: the JSON URL and the printed upgrade command both derive from it.
DIST_NAME = "ironcore-cli"

#: PyPI's JSON metadata endpoint for the distribution. ``info.version`` is the
#: latest non-yanked release.
PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"

#: Default TTL for the on-disk check: one day. A normal launch inside this
#: window reads the cache and never dials.
DEFAULT_TTL_SECONDS = 86400.0

#: How long the network check waits. Short on purpose — a maintenance ping must
#: never make a launch feel slow, and "slow" and "down" are the same answer.
DEFAULT_TIMEOUT_S = 2.0

#: A callable ``(url, timeout) -> text`` — the injectable fetch seam so tests
#: never touch the network.
Fetch = Callable[[str, float], str]


def default_cache_path() -> Path:
    """Where the cached check lives: ``~/.ironcore/update-check.json``.

    Resolved through ``Path.home()`` like the envelope cache and the user
    config; the suite sandboxes HOME so a test can never write the real one.
    """
    return Path.home() / ".ironcore" / "update-check.json"


def _default_fetch(url: str, timeout: float) -> str:
    """The production fetch: one short GET, no secrets to redact (PyPI is a
    public endpoint). Imported lazily so ``--version`` stays import-light and a
    surface that never checks never pays for httpx here."""
    import httpx

    resp = httpx.get(url, timeout=timeout, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.text


def latest_version(*, fetch: Fetch | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> str | None:
    """The latest released version string from PyPI, or ``None``.

    FAIL SILENT by contract: any exception — a network error, DNS failure,
    timeout, an HTTP error, a body that is not JSON, or a payload missing
    ``info.version`` — returns ``None``. It never raises and never logs a
    traceback, because a maintenance ping that can crash a launch is worse than
    no ping at all. ``fetch`` is the injectable ``(url, timeout) -> text`` seam;
    the default dials PyPI with httpx (already a dependency).
    """
    do_fetch = fetch if fetch is not None else _default_fetch
    try:
        text = do_fetch(PYPI_JSON_URL, timeout)
        version = json.loads(text)["info"]["version"]
    except Exception:
        return None
    if not isinstance(version, str) or not version.strip():
        return None
    return version


def is_newer(current: str, latest: str) -> bool:
    """Is ``latest`` a strictly newer release than ``current``?

    Compared with ``packaging.version`` (PEP 440), so ``0.3.10 > 0.3.2`` and
    pre-releases order sensibly (``0.4.0rc1 > 0.3.1``; a final ``0.3.2`` beats
    its own ``0.3.2rc1``). Fail-silent: an unparseable version on either side —
    or ``packaging`` somehow missing at runtime — is treated as "not newer"
    rather than raising. A garbage tag on PyPI must not nag a working install,
    and a maintenance ping must never crash a launch or turn ``ironcore doctor``
    into a traceback. The import lives INSIDE the ``try`` so a future dependency
    prune degrades to "no nudge", not a ``ModuleNotFoundError`` (``packaging`` is
    a declared runtime dependency now — this is defense in depth).
    """
    try:
        from packaging.version import parse

        return parse(latest) > parse(current)
    except Exception:
        return False


@dataclass(frozen=True)
class UpdateInfo:
    """The verdict of a cache-aware check. ``newer`` is always ``True`` when an
    ``UpdateInfo`` is returned (a no-update check returns ``None``); it is a
    field so a consumer reading the object need not re-derive the comparison."""

    current: str
    latest: str
    newer: bool


def check_for_update(
    current: str = __version__,
    *,
    cache_path: Path | None = None,
    fetch: Fetch | None = None,
    now: float | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> UpdateInfo | None:
    """Cache-aware check: ``UpdateInfo`` when a newer version exists, else ``None``.

    A fresh cache (younger than ``ttl_seconds``) is used WITHOUT dialing PyPI, so
    a normal launch inside the window costs no network. Otherwise the latest is
    fetched and the cache is rewritten atomically. Returns ``None`` when the
    latest is unknown (the network failed and no usable cache exists) or when
    ``current`` is already at or ahead of ``latest`` — there is nothing to nudge
    in either case.

    ``cache_path`` defaults to :func:`default_cache_path`. ``now`` (epoch
    seconds) and ``fetch`` are injectable for tests. A corrupt or partial cache
    file is tolerated — treated as "no cache", never a crash.
    """
    path = cache_path if cache_path is not None else default_cache_path()
    stamp = time.time() if now is None else now

    latest = _read_fresh_cache(path, stamp, ttl_seconds)
    if latest is None:
        latest = latest_version(fetch=fetch)
        if latest is not None:
            _write_cache(path, stamp, latest)
    if latest is None or not is_newer(current, latest):
        return None
    return UpdateInfo(current=current, latest=latest, newer=True)


def _read_fresh_cache(path: Path, now: float, ttl_seconds: float) -> str | None:
    """The cached latest version if the cache is present, valid, and fresh.

    Any of "missing", "unreadable", "not JSON", "wrong shape", or "stale"
    returns ``None`` — the caller then dials. Never raises: a half-written or
    hand-corrupted cache must read as "no cache", not brick a launch.
    """
    try:
        data = json.loads(Path(path).read_bytes())
        checked_at = data["checked_at"]
        latest = data["latest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
        return None
    if not isinstance(latest, str) or not latest:
        return None
    if now - checked_at > ttl_seconds:
        return None  # stale — re-check
    return latest


def _write_cache(path: Path, now: float, latest: str) -> None:
    """Publish ``{checked_at, latest}`` at ``path`` atomically, fail-silent.

    Stage under a unique name in the target's own directory (same volume, so
    ``os.replace`` is atomic), fsync, then rename — the write-then-rename shape
    ``core/state.py`` and ``envelope/profile.py`` use, so an interrupted or
    failed write can never leave a partial file at the live path. A cache we
    cannot write is not worth a crash, so every OSError is swallowed.
    """
    p = Path(path)
    payload = json.dumps({"checked_at": now, "latest": latest}, indent=2)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, p)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return
