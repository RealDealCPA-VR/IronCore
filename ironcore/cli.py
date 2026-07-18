"""IronCore command-line entry point.

``ironcore`` with no subcommand launches the Textual TUI (phase 7,
ironcore/tui/app.py) **when attached to an interactive terminal**. When stdout
is not a TTY — piped, captured, or under CI — there is no app to drive, so it
prints the banner and exits **non-zero**: silently succeeding while doing
nothing is a lie the caller cannot detect.

``--version``, ``doctor``, ``init`` and ``demo`` are the fast, import-light
paths — the TUI (and Textual) are imported lazily inside ``main`` so none of
them pay for it.

Doctor's contract is *truth*: every line it prints is something it actually
checked, and it exits non-zero when the setup is misconfigured — a bad
``base_url``, an endpoint that is not OpenAI-compatible, a model that is not on
the server, an MCP command that is not on PATH. A server that simply is not
running yet exits 0: that is a thing to start, not a thing to fix. So
``ironcore doctor && ironcore`` is a usable install gate.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ironcore import __version__

if TYPE_CHECKING:  # import-light at runtime: --version must not pull in pydantic
    from ironcore.config.settings import Settings

#: Repository/doc links. The wheel ships no .md files, so the banner must point
#: at URLs, not at relative paths that only exist in a source checkout.
REPO_URL = "https://github.com/RealDealCPA-VR/IronCore"
ISSUES_URL = "https://github.com/RealDealCPA-VR/IronCore/issues"

BANNER = r"""
  IronCore v{version}
  A frontier-grade terminal coding agent for open-source models.

  Run `ironcore` in an interactive terminal to launch the TUI.

  Start here:
    ironcore doctor    check python, config, endpoint, model, git -- exits 1 if misconfigured
    ironcore demo      a real IronCore session, fully offline (no model needed)
    ironcore init      write a commented starter config and print its path

  Docs:   {repo}
  Issues: {issues}
"""


#: ``--resume`` given with no id: open the session picker at launch. Mirrors
#: ``ironcore.tui.app.RESUME_PICK`` (kept as a literal so this module stays
#: import-light — the TUI is imported lazily inside ``main``).
RESUME_PICK = "__pick__"


#: Role names doctor reports, in the order they appear in RoleModels.
_ROLE_NAMES = ("planner", "coder", "summarizer", "verifier")

#: How long doctor waits on the endpoint probe. Short: doctor must stay snappy,
#: and "slow" and "down" are the same answer for a local server.
_PROBE_TIMEOUT_S = 3.0


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
    sub.add_parser(
        "doctor",
        help="check the local setup (python, config, endpoint, model, git); "
        "exit 1 if misconfigured",
    )
    demo = sub.add_parser(
        "demo", help="run a real IronCore session fully offline -- no model, no network"
    )
    demo.add_argument(
        "--smoke",
        action="store_true",
        help="non-interactive: print one PASS/FAIL line instead of the narration",
    )
    init = sub.add_parser("init", help="write a commented starter config file")
    scope = init.add_mutually_exclusive_group()
    scope.add_argument(
        "--user",
        dest="scope",
        action="store_const",
        const="user",
        help="write ~/.ironcore/config.toml (the default)",
    )
    scope.add_argument(
        "--project",
        dest="scope",
        action="store_const",
        const="project",
        help="write ./.ironcore/config.toml (committable, overrides the user file)",
    )
    init.set_defaults(scope="user")
    init.add_argument("--force", action="store_true", help="overwrite an existing config file")
    return parser


def _is_localhost(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _which(command: str) -> str | None:
    """Indirection over :func:`shutil.which` so tests can make PATH deterministic."""
    return shutil.which(command)


# --------------------------------------------------------------------------
# endpoint probe
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointProbe:
    """The result of ONE request to ``{base_url}/models``.

    ``/models`` is the OpenAI-compatible listing path that every backend
    IronCore advertises serves (Ollama, vLLM, llama.cpp's server, LM Studio) —
    unlike Ollama's proprietary ``/api/version``, which the others 404, making
    a probe of it "pass" against a server that cannot talk to us at all.

    One request answers both questions doctor needs: is the endpoint really an
    OpenAI-compatible server, and does it have the configured model?

    status:
      ``ok``           -- 2xx and an intelligible model list (``models`` filled)
      ``bad_url``      -- not a usable URL at all (missing scheme, bad syntax)
      ``unreachable``  -- nothing answered (connection refused, DNS, timeout)
      ``unauthorized`` -- 401/403: OpenAI-shaped, but rejected our api_key
      ``http_error``   -- something else answered, but not with success
      ``bad_payload``  -- 2xx whose body is not an OpenAI model list
    """

    status: str
    url: str
    detail: str = ""
    code: int | None = None
    models: tuple[str, ...] = field(default=())


def _model_ids(payload: object) -> tuple[str, ...] | None:
    """Model ids out of an OpenAI ``/models`` body, or None if it isn't one.

    Tolerant on purpose: local servers are sloppy. ``{"data": [...]}`` is the
    OpenAI shape; a bare list is accepted too; entries may be dicts (``id`` or
    ``name``) or plain strings. An empty list is a valid answer ("no models
    installed") and must not be confused with "not a model list".
    """
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return None
    ids: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("id") or entry.get("name")
            if isinstance(name, str):
                ids.append(name)
    if entries and not ids:
        return None  # a list of somethings, but not of models
    return tuple(ids)


def probe_endpoint(
    base_url: str, api_key: str = "", timeout: float = _PROBE_TIMEOUT_S
) -> EndpointProbe:
    """GET ``{base_url}/models`` once and classify the outcome. Never raises.

    Sends the same ``Authorization: Bearer`` header the real client does
    (providers/openai_compat.py, envelope/detect.py). Without it doctor asks a
    different question than the app: vLLM or llama.cpp's server started with
    ``--api-key``, and every hosted OpenAI-compatible provider, answer 401 to an
    anonymous probe — so doctor would fail the gate on a setup that works, and
    blame ``base_url``, the one field that is correct.

    The key is never echoed: it is redacted out of any detail we carry back, so
    it cannot reach the terminal (or a pasted bug report) through an exception
    message.
    """
    import httpx

    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _redact(text: str) -> str:
        return text.replace(api_key, "[redacted]") if api_key else text

    try:
        resp = httpx.get(url, timeout=timeout, headers=headers)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        # A scheme typo used to print "fine if no local server is running" --
        # actively telling the user their broken config was OK.
        return EndpointProbe("bad_url", url, detail=_redact(str(exc)))
    except httpx.HTTPError as exc:
        return EndpointProbe("unreachable", url, detail=_redact(str(exc)))
    except Exception as exc:  # pragma: no cover -- defensive; doctor never crashes
        return EndpointProbe("unreachable", url, detail=_redact(str(exc)))

    if resp.status_code in (401, 403):
        # Distinct from a generic http_error: the endpoint IS OpenAI-shaped and
        # is talking to us, it just rejected the key we sent. Pointing this user
        # at base_url would send them to edit the one field that is correct.
        return EndpointProbe("unauthorized", url, code=resp.status_code)
    if not 200 <= resp.status_code < 300:
        return EndpointProbe("http_error", url, code=resp.status_code)
    try:
        ids = _model_ids(resp.json())
    except Exception:
        ids = None
    if ids is None:
        return EndpointProbe("bad_payload", url, code=resp.status_code)
    return EndpointProbe("ok", url, code=resp.status_code, models=ids)


def _model_available(model: str, available: tuple[str, ...]) -> bool:
    """Is ``model`` in the endpoint's list? Exact match, modulo Ollama's
    implicit ``:latest`` tag (``llama3`` and ``llama3:latest`` are one model)."""
    candidates = {model, model.removesuffix(":latest"), f"{model}:latest"}
    for have in available:
        if have in candidates or have.removesuffix(":latest") in candidates:
            return True
    return False


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def cmd_doctor(
    project_dir: Path | None = None,
    user_config: Path | None = None,
    env: dict[str, str] | None = None,
    envelope_dir: Path | None = None,
    check_endpoint: bool = True,
    probe: Callable[[str], EndpointProbe] | None = None,
) -> int:
    """Environment checks; 0 only if the setup would actually work.

    Parameters are injectable for tests (they mirror Settings.load); real runs
    pass nothing. ``check_endpoint=False`` skips the network probe entirely so
    tests stay offline; ``probe`` substitutes a scripted probe for the same
    reason while still exercising the reporting branches.
    """
    ok = True

    version_ok = sys.version_info >= (3, 11)
    print(f"[{'ok' if version_ok else 'FAIL'}] python {sys.version.split()[0]} (need >= 3.11)")
    ok = ok and version_ok

    from ironcore.config.settings import Settings

    resolved_project = project_dir if project_dir is not None else Path.cwd()
    # Path.home() only when we actually need it: it raises on a machine with no
    # resolvable home, and doctor must still run when the path was injected.
    user_path = (
        user_config if user_config is not None else Path.home() / ".ironcore" / "config.toml"
    )
    project_path = resolved_project / ".ironcore" / "config.toml"

    try:
        settings = Settings.load(
            project_dir=resolved_project,
            user_config=user_path,
            env=env,
        )
    except Exception as exc:  # ConfigError included: both need the same answer
        # Not every loader error names its file -- a non-UTF8 byte surfaces as a
        # bare codec message. "Which of my two config files do I open?" is the
        # only question that matters here, and doctor already knows both.
        print(f"[FAIL] config: {exc}")
        present = [p for p in (user_path, project_path) if p.exists()]
        if present and not any(str(p) in str(exc) for p in present):
            print(f"       config file(s) doctor read: {', '.join(str(p) for p in present)}")
        return 1

    # Name the files. "config loaded" printed when no config file existed at all
    # was the single most misleading line doctor emitted.
    def _state(path: Path) -> str:
        return "loaded" if path.exists() else "absent"

    if user_path.exists() or project_path.exists():
        print(
            f"[ok] config: {user_path} ({_state(user_path)}) "
            f"+ {project_path} ({_state(project_path)})"
        )
    else:
        print(f"[--] no config file -- using defaults (model: {settings.provider.model})")
        print(f"     `ironcore init` writes a commented starter config at {user_path}")
    print(f"[ok] effective: model {settings.provider.model}, mode {settings.safety.mode}")
    for role in _ROLE_NAMES:
        model = getattr(settings.roles, role)
        if model:
            print(f"[ok] role {role}: {model}")

    endpoint = settings.provider.base_url
    if settings.safety.network_tools and not _is_localhost(endpoint):
        # SAFETY.md section 6: hosted endpoint + network tools = code leaves this machine.
        print(f"[!!] endpoint {endpoint} is not localhost and safety.network_tools is on:")
        print("     your code leaves this machine -- make sure that is what you want")

    if probe is not None or check_endpoint:
        # The real probe authenticates exactly like the client does; an injected
        # test probe keeps the one-argument signature.
        result = (
            probe(endpoint)
            if probe is not None
            else probe_endpoint(endpoint, api_key=settings.provider.api_key)
        )
        ok = _report_endpoint(result, settings, endpoint, user_path, project_path) and ok

    if envelope_dir is None:
        envelope_dir = Path.home() / ".ironcore" / "envelopes"
    try:
        envelope_dir.mkdir(parents=True, exist_ok=True)
        print(f"[ok] envelope cache writable: {envelope_dir}")
    except OSError as exc:  # pragma: no cover — defensive
        print(f"[FAIL] envelope cache: {exc}")
        ok = False

    # git backs /undo, /redo and change-set snapshots -- a headline feature that
    # silently degrades to nothing when git is missing.
    if _which("git"):
        print("[ok] git found (undo/redo and change-set snapshots available)")
    else:
        print("[!!] git not found -- /undo, /redo and change-set snapshots are disabled")

    # whether the configured model has been measured — the molds-to-the-model status
    from ironcore.envelope.profile import CapabilityProfile

    # load_with_note, not load: a quarantined (corrupt) cache is announced at TUI
    # boot, and doctor must not be the one surface that stays quiet about it.
    profile, quarantine_note = CapabilityProfile.load_with_note(
        envelope_dir, settings.provider.model
    )
    if quarantine_note:
        # same sentence the boot note uses, minus its "[envelope]" tag -- doctor's
        # own marker column already carries the severity.
        print(f"[!!] {quarantine_note.removeprefix('[envelope] ')}")
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

    ok = _report_mcp(settings) and ok

    # Entry-point plugins (MS-5) -- what discovery would load at boot. This
    # RUNS installed plugin factories (installation is the consent moment,
    # SAFETY.md T9); load_plugins never raises, skips are listed with reasons.
    from ironcore.plugins import load_plugins

    plugin_provider_types: set[str] = set()
    if not settings.plugins.enabled:
        print("[--] plugins: disabled ([plugins] enabled = false)")
    else:
        loaded = load_plugins(settings, resolved_project)
        print(f"[ok] plugins: {loaded.summary()}")
        for skip in loaded.skipped:
            print(f"[--] plugin skipped: {skip.group}:{skip.name} -- {skip.reason}")
        plugin_provider_types = set(loaded.provider_factories)
    ptype = settings.provider.type
    if ptype not in ("auto", "ollama", "openai") and ptype not in plugin_provider_types:
        # boot keeps the pinned unknown-type -> auto fallthrough; say so here
        # instead of silently building the wrong client.
        print(
            f"[!!] provider.type {ptype!r} matches no built-in or plugin provider "
            "factory; boot falls back to auto-selection"
        )

    return 0 if ok else 1


def _config_hint(user_path: Path, project_path: Path) -> str:
    """Which file to edit: the one that already exists wins (project last)."""
    for path in (project_path, user_path):
        if path.exists():
            return str(path)
    return f"{user_path} (run `ironcore init` to create it)"


def _report_endpoint(
    result: EndpointProbe,
    settings: Settings,
    endpoint: str,
    user_path: Path,
    project_path: Path,
) -> bool:
    """Print the endpoint + model-availability verdict; return False if broken.

    Split out of cmd_doctor because it is the part that has to be exactly right:
    the old code captured ``resp.status_code``, printed it, and never looked at
    it, so ``[ok] endpoint reachable (404)`` was a literal output.
    """
    ok = True
    where = _config_hint(user_path, project_path)
    if result.status == "bad_url":
        print(f"[FAIL] provider.base_url is not a usable URL: {endpoint}")
        print("       expected something like http://localhost:11434/v1")
        print(f"       set [provider] base_url in {where}")
        ok = False
    elif result.status == "unreachable":
        print(f"[--] endpoint not reachable: {result.url}")
        print("     start your local server (e.g. `ollama serve`), then re-run `ironcore doctor`")
    elif result.status in ("unauthorized", "http_error"):
        # Something is listening but it is not talking OpenAI at this path. That
        # is a config error the user must fix -- same class as bad_url, NOT the
        # same as "nothing is running yet" -- so it fails the gate.
        if result.status == "unauthorized":
            # We DID send the configured key, so base_url is not the suspect
            # here and telling them to change it sends them the wrong way.
            print(f"[FAIL] endpoint rejected our API key: HTTP {result.code} from {result.url}")
            print(f"       set [provider] api_key in {where} (or the IRONCORE_API_KEY env var)")
        else:
            print(
                f"[FAIL] got HTTP {result.code} from {result.url} "
                "-- is this an OpenAI-compatible endpoint?"
            )
            if not endpoint.rstrip("/").endswith("/v1"):
                print(f"       base_url usually ends with /v1 (yours is {endpoint})")
            print(f"       set [provider] base_url in {where}")
        ok = False
    elif result.status == "bad_payload":
        print(f"[FAIL] {result.url} answered {result.code} but not with an OpenAI model list")
        print(f"       point [provider] base_url at an OpenAI-compatible server ({where})")
        ok = False
    else:
        print(f"[ok] endpoint reachable: {result.url} ({len(result.models)} model(s) listed)")
        ok = _report_models(result, settings, where) and ok

    if not ok or result.status != "ok":
        print("     no model ready yet? `ironcore demo` runs a real session fully offline")
    return ok


def _report_models(result: EndpointProbe, settings: Settings, where: str) -> bool:
    """Is every configured model actually available at the endpoint?

    The shipped default is an ~18GB model almost nobody has pulled, and until
    now doctor never asked — so a clean bill of health was compatible with the
    very first turn failing.
    """
    wanted: list[tuple[str, str]] = [("provider.model", settings.provider.model)]
    for role in _ROLE_NAMES:
        model = getattr(settings.roles, role)
        if model:
            wanted.append((f"roles.{role}", model))

    if not result.models:
        print(f"[FAIL] {result.url} answered, but lists no models at all")
        print(f"       pull one first (e.g. `ollama pull {settings.provider.model}`)")
        return False

    shown = ", ".join(result.models[:5])
    if len(result.models) > 5:
        shown += f", ... ({len(result.models)} total)"
    ok = True
    for label, model in wanted:
        if _model_available(model, result.models):
            print(f"[ok] {label} {model} is available at the endpoint")
        else:
            print(f"[FAIL] model {model} is not available at {result.url} (from {label})")
            print(f"       models you have: {shown}")
            print(f"       fix: `ollama pull {model}`, or set [provider] model in {where}")
            ok = False
    return ok


def _report_mcp(settings: Settings) -> bool:
    """MCP servers: configured, registerable, and actually launchable?

    A missing command only fails the gate when the server would actually be
    launched — i.e. when ``safety.network_tools`` is on. With it off, doctor has
    just said these servers stay unregistered, so failing on one would be doctor
    contradicting itself one line later and refusing an install that runs fine.
    """
    servers = sorted(
        ((name, srv) for name, srv in settings.mcp.servers.items() if srv.enabled),
        key=lambda item: item[0],
    )
    if not servers:
        return True
    names = ", ".join(name for name, _ in servers)
    registered = settings.safety.network_tools
    if registered:
        print(f"[ok] mcp: {len(servers)} server(s) configured ({names})")
    else:
        print(
            f"[--] mcp: {len(servers)} server(s) configured ({names}) but MCP tools "
            "are NET-risk and stay unregistered until safety.network_tools = true"
        )
    ok = True
    for name, srv in servers:
        if not srv.command:
            # the exact wording MCPManager.from_settings emits, so doctor and
            # the TUI never disagree about why an entry was dropped.
            print(
                f"[--] mcp {name}: url-only entries are not supported yet "
                "(stdio only -- set 'command') -- will be skipped"
            )
        elif _which(srv.command) is None:
            missing = f"mcp {name}: command {srv.command!r} not found on PATH"
            if registered:
                print(f"[FAIL] {missing}")
                ok = False
            else:
                print(f"[!!] {missing}")
                print("     harmless today (this server is not registered); fix it before")
                print("     turning safety.network_tools on")
    return ok


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

#: A starter config. Everything a user is likely to change is live; everything
#: else is commented with its real default, so the file doubles as the settings
#: reference and never changes behaviour just by existing.
STARTER_CONFIG = """\
# IronCore configuration.
#   user file:    ~/.ironcore/config.toml
#   project file: <repo>/.ironcore/config.toml   (committable; wins over the user file)
#   environment:  IRONCORE_BASE_URL / _MODEL / _API_KEY / _MODE / _ROLE_* (win over both)
# Commented `key = value` lines show IronCore's actual default, so uncommenting
# one changes nothing -- except where the comment says the setting is UNSET by
# default, which no value can express.
# The two blocks marked EXAMPLE below are the exception: their values are made
# up, so uncommenting those DOES change behaviour.

[provider]
# Any OpenAI-compatible server: Ollama, vLLM, llama.cpp's server, LM Studio.
# The path almost always ends in /v1.
base_url = "http://localhost:11434/v1"
# The model must already exist on that server -- `ironcore doctor` checks.
model = "qwen3-coder:30b"
# api_key = "ironcore-local"   # local servers ignore it; never put a real key in a repo
# type = "auto"                # "auto" | "ollama" | "openai"

[safety]
# Boot mode: "manual" (approve every action) | "accept-edits" | "plan".
mode = "manual"
# workspace_only = true        # path jail: writes cannot escape the workspace
# network_tools = false        # NET-risk tools are not even registered unless true

# [roles]                      # EXAMPLE names -- optional per-role routing.
#                              # Unset (the default) = every role uses provider.model.
# planner = "big-model"
# coder = "fast-model"
# summarizer = "fast-model"
# verifier = "fast-model"

# [envelope]                   # how IronCore molds itself to your model
# auto_probe = true            # false = never measure in the background; stay on floor defaults
# instant_seed = true          # false = no ~1s seed from endpoint introspection at boot
# auto_tune = true             # false = never record outcomes or downgrade a ladder rung
# vision = true                # force image support on/off; UNSET by default =
#                              # trust the measured profile (so setting it false
#                              # disables vision on a model that really has it)

# [engine]
# best_of_n = 1                # 1 = off; N resamples up to N-1 extra candidates per turn

# [plugins]
# enabled = true               # false = never consult entry points at all

# [mcp.servers.example]        # EXAMPLE server -- none configured by default.
#                              # Uncommenting registers one. stdio transport only in v0.x.
# command = "npx.cmd"          # on Windows use the real launcher name
# args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
# enabled = true
"""


def cmd_init(
    scope: str = "user",
    project_dir: Path | None = None,
    user_config: Path | None = None,
    force: bool = False,
) -> int:
    """Write a commented starter config and print where it went."""
    if scope == "project":
        base = project_dir if project_dir is not None else Path.cwd()
        target = base / ".ironcore" / "config.toml"
    else:
        target = (
            user_config if user_config is not None else Path.home() / ".ironcore" / "config.toml"
        )

    if target.is_dir():
        # --force would not help here (the write fails with an OSError either
        # way), so do not suggest a remedy that cannot work.
        print(f"ironcore: {target} is a directory, not a config file")
        print("ironcore: remove or rename it, then re-run `ironcore init`")
        return 1
    if target.exists() and not force:
        print(f"ironcore: {target} already exists; pass --force to overwrite it")
        return 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(STARTER_CONFIG, encoding="utf-8")
    except OSError as exc:
        print(f"ironcore: could not write {target}: {exc}")
        return 1
    print(f"wrote {target}")
    print("edit [provider] base_url + model, then run `ironcore doctor`")
    return 0


# --------------------------------------------------------------------------
# demo
# --------------------------------------------------------------------------


def cmd_demo(smoke: bool = False) -> int:
    """Run the offline demo session (ironcore/demo). No model, no network.

    ``--smoke`` collapses the narration to one PASS/FAIL line so a release gate
    can assert on it; the transcript is still printed on failure, because a
    silent failure is the thing a gate exists to prevent.
    """
    from ironcore.demo.scenario import run_demo

    if not smoke:
        return run_demo(emit=print)

    captured: list[str] = []
    code = run_demo(emit=captured.append)
    if code == 0:
        print("demo: PASS -- offline session completed (read -> edit -> verify -> done)")
        return 0
    print("\n".join(captured))
    print("demo: FAIL -- the offline session did not complete")
    return 1


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

#: Printed under a config-file error out of the dispatch. Every path that can
#: raise (a stray quote in TOML, a half-written state file) used to traceback
#: straight out of the primary entry point.
_CONFIG_HINT = (
    "Fix the file or delete it to fall back to defaults; run `ironcore doctor` to re-check."
)

#: Printed under anything else. Deliberately says nothing about a file: this
#: backstop catches errors that have no file behind them (a missing HOME, a
#: broken terminal), and pointing those at "fix the file" is advice about a
#: file that has nothing to do with the failure.
_ERROR_HINT = (
    f"Run `ironcore doctor` to check your setup; if it persists, report it at {ISSUES_URL}"
)


def _dispatch(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"ironcore {__version__}")
        return 0
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "demo":
        return cmd_demo(smoke=args.smoke)
    if args.command == "init":
        return cmd_init(scope=args.scope, force=args.force)
    # No subcommand: launch the interactive TUI only when we own a real
    # terminal. Non-TTY (pipes, CI, captured tests) gets the banner and a
    # non-zero exit — driving a full-screen app into a pipe would hang, and
    # exiting 0 would tell the caller a session ran. Imported lazily so
    # --version and doctor stay import-light.
    if sys.stdout.isatty():
        from ironcore.tui.app import run_app

        return run_app(resume=args.resume)
    print(BANNER.format(version=__version__, repo=REPO_URL, issues=ISSUES_URL))
    print("no interactive terminal; try `ironcore doctor` or `ironcore demo`", file=sys.stderr)
    return 1


def _is_config_error(exc: BaseException) -> bool:
    """Is this a ConfigError, without importing the module that defines it?

    ``from ironcore.config.settings import ConfigError`` at the top of ``main``
    would pull pydantic into every invocation, breaking the import-light
    guarantee ``--version`` is built on. It is also unnecessary: a ConfigError
    instance cannot exist unless something already imported that module, so if
    it is absent from sys.modules the answer is simply no.
    """
    module = sys.modules.get("ironcore.config.settings")
    return module is not None and isinstance(exc, module.ConfigError)


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:  # pragma: no cover -- interactive only
        print("ironcore: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        # Last line of defence: a stranger gets a sentence, not a traceback.
        if _is_config_error(exc):
            print(f"ironcore: {exc}", file=sys.stderr)
            print(_CONFIG_HINT, file=sys.stderr)
        else:
            print(f"ironcore: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(_ERROR_HINT, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
