"""IronCore command-line entry point.

The full Textual TUI ships in phase 7 (TODO.md IC-701..706). Until then
the CLI provides --version, `doctor`, and an honest scaffold banner so the
package is installable and verifiable end to end from day one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ironcore import __version__

BANNER = r"""
  IronCore v{version}
  A frontier-grade terminal coding agent for open-source models.

  The TUI is not built yet -- this is the scaffold release.
  Start here:
    README.md        what this is and why
    docs/SPEC.md     the full specification
    TODO.md          the build plan (one-pass tasks)
    AGENTS.md        pickup protocol for agents working on this repo

  Try: ironcore doctor
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ironcore",
        description="A frontier-grade terminal coding agent for open-source models.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="check the local environment (python, config, endpoint)")
    return parser


def cmd_doctor() -> int:
    ok = True

    version_ok = sys.version_info >= (3, 11)
    print(f"[{'ok' if version_ok else 'FAIL'}] python {sys.version.split()[0]} (need >= 3.11)")
    ok = ok and version_ok

    try:
        from ironcore.config.settings import Settings

        settings = Settings.load(project_dir=Path.cwd())
        print(f"[ok] config loaded (model: {settings.provider.model})")
        endpoint = settings.provider.base_url
    except Exception as exc:  # pragma: no cover — defensive
        print(f"[FAIL] config: {exc}")
        return 1

    try:
        import httpx

        resp = httpx.get(f"{endpoint.rstrip('/').removesuffix('/v1')}/api/version", timeout=2.0)
        print(f"[ok] endpoint reachable: {endpoint} ({resp.status_code})")
    except Exception:
        print(f"[--] endpoint not reachable: {endpoint} (fine if no local server is running)")

    envelope_dir = Path.home() / ".ironcore" / "envelopes"
    try:
        envelope_dir.mkdir(parents=True, exist_ok=True)
        print(f"[ok] envelope cache writable: {envelope_dir}")
    except OSError as exc:  # pragma: no cover — defensive
        print(f"[FAIL] envelope cache: {exc}")
        ok = False

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"ironcore {__version__}")
        return 0
    if args.command == "doctor":
        return cmd_doctor()
    print(BANNER.format(version=__version__))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
