"""IronCore command-line entry point.

``ironcore`` with no subcommand launches the Textual TUI (phase 7,
ironcore/tui/app.py) **when attached to an interactive terminal**. When stdout
is not a TTY — piped, captured, or under CI — it prints the informational
banner and exits 0 instead of trying to drive a full-screen app into a pipe.
``--version`` and ``doctor`` remain the fast, import-light paths — the TUI (and
Textual) are imported lazily inside ``main`` so those two never pay for it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ironcore import __version__

BANNER = r"""
  IronCore v{version}
  A frontier-grade terminal coding agent for open-source models.

  Run `ironcore` in an interactive terminal to launch the TUI.
  Reference:
    README.md        what this is and why
    docs/SPEC.md     the full specification
    TODO.md          the build plan (one-pass tasks)
    AGENTS.md        pickup protocol for agents working on this repo

  Try: ironcore doctor
"""


#: ``--resume`` given with no id: open the session picker at launch. Mirrors
#: ``ironcore.tui.app.RESUME_PICK`` (kept as a literal so this module stays
#: import-light — the TUI is imported lazily inside ``main``).
RESUME_PICK = "__pick__"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ironcore",
        description="A frontier-grade terminal coding agent for open-source models.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--resume",
        nargs="?",
        const=RESUME_PICK,
        default=None,
        metavar="SESSION_ID",
        help="resume a previous session; with no id, pick one from a list at launch",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="check the local environment (python, config, endpoint)")
    return parser


#: Role names doctor reports, in the order they appear in RoleModels.
_ROLE_NAMES = ("planner", "coder", "summarizer", "verifier")


def _is_localhost(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "localhost" or host == "::1" or host.startswith("127.")


def cmd_doctor(
    project_dir: Path | None = None,
    user_config: Path | None = None,
    env: dict[str, str] | None = None,
    envelope_dir: Path | None = None,
    check_endpoint: bool = True,
) -> int:
    """Environment checks. Parameters are injectable for tests (mirrors
    Settings.load); real runs pass nothing. check_endpoint=False skips the
    network probe so tests stay offline."""
    ok = True

    version_ok = sys.version_info >= (3, 11)
    print(f"[{'ok' if version_ok else 'FAIL'}] python {sys.version.split()[0]} (need >= 3.11)")
    ok = ok and version_ok

    from ironcore.config.settings import ConfigError, Settings

    try:
        settings = Settings.load(
            project_dir=project_dir if project_dir is not None else Path.cwd(),
            user_config=user_config,
            env=env,
        )
    except ConfigError as exc:
        print(f"[FAIL] config: {exc}")
        return 1
    except Exception as exc:  # pragma: no cover — defensive
        print(f"[FAIL] config: {exc}")
        return 1

    print(f"[ok] config loaded (model: {settings.provider.model}, mode: {settings.safety.mode})")
    for role in _ROLE_NAMES:
        model = getattr(settings.roles, role)
        if model:
            print(f"[ok] role {role}: {model}")

    endpoint = settings.provider.base_url
    if settings.safety.network_tools and not _is_localhost(endpoint):
        # SAFETY.md section 6: hosted endpoint + network tools = code leaves this machine.
        print(f"[!!] endpoint {endpoint} is not localhost and safety.network_tools is on:")
        print("     your code leaves this machine -- make sure that is what you want")

    if check_endpoint:
        try:
            import httpx

            probe = f"{endpoint.rstrip('/').removesuffix('/v1')}/api/version"
            resp = httpx.get(probe, timeout=2.0)
            print(f"[ok] endpoint reachable: {endpoint} ({resp.status_code})")
        except Exception:
            print(f"[--] endpoint not reachable: {endpoint} (fine if no local server is running)")

    if envelope_dir is None:
        envelope_dir = Path.home() / ".ironcore" / "envelopes"
    try:
        envelope_dir.mkdir(parents=True, exist_ok=True)
        print(f"[ok] envelope cache writable: {envelope_dir}")
    except OSError as exc:  # pragma: no cover — defensive
        print(f"[FAIL] envelope cache: {exc}")
        ok = False

    # whether the configured model has been measured — the molds-to-the-model status
    from ironcore.envelope.profile import CapabilityProfile

    profile = CapabilityProfile.load(envelope_dir, settings.provider.model)
    if profile is not None and (profile.source == "probed" or profile.probed_at is not None):
        print(
            f"[ok] model {settings.provider.model} measured "
            f"(tools: {profile.recommended_tool_protocol()}, edits: "
            f"{profile.recommended_edit_format()}, ctx: {profile.honest_context})"
        )
    else:
        print(
            f"[--] model {settings.provider.model} unprobed -- the app instant-seeds it "
            "from the endpoint in ~1s then measures in the background (or run /probe)"
        )

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"ironcore {__version__}")
        return 0
    if args.command == "doctor":
        return cmd_doctor()
    # No subcommand: launch the interactive TUI only when we own a real
    # terminal. Non-TTY (pipes, CI, captured tests) gets the banner — driving a
    # full-screen app into a pipe would hang. Imported lazily so --version and
    # doctor stay import-light.
    if sys.stdout.isatty():
        from ironcore.tui.app import run_app

        return run_app(resume=args.resume)
    print(BANNER.format(version=__version__))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
