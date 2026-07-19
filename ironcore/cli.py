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
import os
import re
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


#: Matches one ``Start here:`` row of BANNER above: four spaces, the command,
#: a run of padding, then what it does. Anchored to that exact shape so a line
#: it does not describe falls through to the plain branch instead of being
#: mis-split.
_BANNER_COMMAND_RE = re.compile(r"^(    )(ironcore \w+)(\s{2,})(.+)$")


def _print_banner() -> None:
    """Print BANNER with the hierarchy it already implies made visible.

    Text in, same text out: this styles the template above line by line and
    invents nothing, so the ``test_cli_banner_*`` pins keep reading exactly what
    they read before. It matters less than doctor or demo — the banner only
    prints when stdout is NOT a terminal, which is also when colour is dropped —
    but a terminal that has been told to keep colour (``FORCE_COLOR``) should
    see the same product as every other surface, not a grey block.
    """
    from rich.text import Text

    from ironcore import term

    for raw in BANNER.format(version=__version__, repo=REPO_URL, issues=ISSUES_URL).splitlines():
        command = _BANNER_COMMAND_RE.match(raw)
        if raw.startswith("  IronCore v"):
            term.line(Text(raw, style=f"bold {term.ACCENT}"))
        elif command is not None:
            indent, name, pad, description = command.groups()
            out = Text(indent)
            out.append(name, style="bold")
            out.append(pad)
            out.append(description, style=term.STYLE_MUTED)
            term.line(out)
        elif raw.strip().endswith(":") or raw.startswith(("  Docs:", "  Issues:")):
            term.line(Text(raw, style=term.STYLE_LABEL))
        else:
            term.line(Text(raw, style=term.STYLE_MUTED))
    term.line()


#: ``--resume`` given with no id: open the session picker at launch. Mirrors
#: ``ironcore.tui.app.RESUME_PICK`` (kept as a literal so this module stays
#: import-light — the TUI is imported lazily inside ``main``).
RESUME_PICK = "__pick__"

#: ``ironcore exec --mode`` choices, in autonomy order. Literal (not
#: ``[m.value for m in Mode]``) so ``build_parser`` stays import-light — the
#: real ``Mode`` is resolved lazily inside ``cmd_exec``. Mirrors
#: ``ironcore.safety.modes.Mode``; PLAN is the headless default (read-only, CI-safe).
_EXEC_MODES = ("plan", "manual", "accept-edits", "auto")


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
    ex = sub.add_parser(
        "exec",
        help="run one turn headlessly from a prompt; stream the reply to stdout "
        "(read-only by default)",
    )
    ex.add_argument("prompt", help="the instruction to run this turn")
    ex.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="emit one serialized event per line to stdout instead of prose",
    )
    ex.add_argument(
        "--mode",
        choices=_EXEC_MODES,
        default="plan",
        help="autonomy mode (default: plan -- read-only, CI-safe). Higher modes "
        "still auto-DENY any approval prompt (no human to ask).",
    )
    return parser


def _is_localhost(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _which(command: str) -> str | None:
    """Indirection over :func:`shutil.which` so tests can make PATH deterministic."""
    return shutil.which(command)


def _display_path(path: Path) -> str:
    """Render ``path`` with the home directory collapsed to ``~``.

    Doctor output gets pasted into issues and screenshots, so an absolute path
    carries the operator's username off this machine for no benefit. Falls back
    to the full path when home is unresolvable (``Path.home()`` raises) or when
    the path lies outside it.
    """
    try:
        return f"~{os.sep}{path.relative_to(Path.home())}"
    except (ValueError, RuntimeError, OSError):
        return str(path)


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


def _say(text: str) -> None:
    """Print one doctor line, coloured by the marker it already carries.

    Every doctor string in this module is composed exactly as it always was and
    handed here whole: ``ironcore.term.doctor_line`` derives the styling from
    the leading ``[ok]`` / ``[--]`` / ``[!!]`` / ``[FAIL]`` marker (or from the
    indent of a follow-up line), so painting cannot change a single character of
    what is printed — which is the property the doctor tests and
    docs/TROUBLESHOOTING.md both lean on. Colour is dropped entirely when stdout
    is not a terminal.

    Imported inside the call so ``ironcore --version`` stays import-light: it
    returns before any of this runs and must not pay for rich.
    """
    from ironcore.term import doctor_line

    doctor_line(text)


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
    _say(f"[{'ok' if version_ok else 'FAIL'}] python {sys.version.split()[0]} (need >= 3.11)")
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
        settings, config_notes = Settings.load_with_notes(
            project_dir=resolved_project,
            user_config=user_path,
            env=env,
        )
    except Exception as exc:  # ConfigError included: both need the same answer
        # Not every loader error names its file -- a non-UTF8 byte surfaces as a
        # bare codec message. "Which of my two config files do I open?" is the
        # only question that matters here, and doctor already knows both.
        _say(f"[FAIL] config: {exc}")
        present = [p for p in (user_path, project_path) if p.exists()]
        if present and not any(str(p) in str(exc) for p in present):
            names = ", ".join(_display_path(p) for p in present)
            _say(f"       config file(s) doctor read: {names}")
        return 1

    # Name the files. "config loaded" printed when no config file existed at all
    # was the single most misleading line doctor emitted.
    def _state(path: Path) -> str:
        return "loaded" if path.exists() else "absent"

    if user_path.exists() or project_path.exists():
        _say(
            f"[ok] config: {_display_path(user_path)} ({_state(user_path)}) "
            f"+ {_display_path(project_path)} ({_state(project_path)})"
        )
    else:
        _say(f"[--] no config file -- using defaults (model: {settings.provider.model})")
        starter = _display_path(user_path)
        _say(f"     `ironcore init` writes a commented starter config at {starter}")
    _say(f"[ok] effective: model {settings.provider.model}, mode {settings.safety.mode}")
    # T8 clamps / skipped MCP servers: doctor reports the EFFECTIVE setup, so a
    # project config that asked for more than it got has to show up right here.
    for note in config_notes:
        _say(f"     {note}")
    for role in _ROLE_NAMES:
        model = getattr(settings.roles, role)
        if model:
            _say(f"[ok] role {role}: {model}")

    endpoint = settings.provider.base_url
    if settings.safety.network_tools and not _is_localhost(endpoint):
        # SAFETY.md section 6: hosted endpoint + network tools = code leaves this machine.
        _say(f"[!!] endpoint {endpoint} is not localhost and safety.network_tools is on:")
        _say("     your code leaves this machine -- make sure that is what you want")

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
        _say(f"[ok] envelope cache writable: {_display_path(envelope_dir)}")
    except OSError as exc:  # pragma: no cover — defensive
        _say(f"[FAIL] envelope cache: {exc}")
        ok = False

    # git backs /undo, /redo and change-set snapshots -- a headline feature that
    # silently degrades to nothing when git is missing.
    if _which("git"):
        _say("[ok] git found (undo/redo and change-set snapshots available)")
    else:
        _say("[!!] git not found -- /undo, /redo and change-set snapshots are disabled")

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
        _say(f"[!!] {quarantine_note.removeprefix('[envelope] ')}")
    if profile is not None and (profile.source == "probed" or profile.probed_at is not None):
        _say(
            f"[ok] model {settings.provider.model} measured "
            f"(tools: {profile.recommended_tool_protocol()}, edits: "
            f"{profile.recommended_edit_format()}, ctx: {profile.honest_context})"
        )
    else:
        # Finding on the marker line, remedy indented under it -- the same shape
        # every other multi-line check uses. As one 133-character sentence it
        # wrapped to column 0 in any normal terminal, which broke the marker
        # column exactly where a reader is scanning it.
        _say(f"[--] model {settings.provider.model} unprobed")
        _say("     the app instant-seeds it in ~1s from the endpoint, then measures it")
        _say("     in the background -- or run /probe to measure it now")

    ok = _report_mcp(settings) and ok

    # Entry-point plugins (MS-5) -- what discovery would load at boot. This
    # RUNS installed plugin factories (installation is the consent moment,
    # SAFETY.md T9); load_plugins never raises, skips are listed with reasons.
    from ironcore.plugins import load_plugins

    plugin_provider_types: set[str] = set()
    if not settings.plugins.enabled:
        _say("[--] plugins: disabled ([plugins] enabled = false)")
    else:
        loaded = load_plugins(settings, resolved_project)
        _say(f"[ok] plugins: {loaded.summary()}")
        for skip in loaded.skipped:
            _say(f"[--] plugin skipped: {skip.group}:{skip.name} -- {skip.reason}")
        plugin_provider_types = set(loaded.provider_factories)
    ptype = settings.provider.type
    if ptype not in ("auto", "ollama", "openai") and ptype not in plugin_provider_types:
        # boot keeps the pinned unknown-type -> auto fallthrough; say so here
        # instead of silently building the wrong client.
        _say(
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
        _say(f"[FAIL] provider.base_url is not a usable URL: {endpoint}")
        _say("       expected something like http://localhost:11434/v1")
        _say(f"       set [provider] base_url in {where}")
        ok = False
    elif result.status == "unreachable":
        _say(f"[--] endpoint not reachable: {result.url}")
        _say("     start your local server (e.g. `ollama serve`), then re-run `ironcore doctor`")
    elif result.status in ("unauthorized", "http_error"):
        # Something is listening but it is not talking OpenAI at this path. That
        # is a config error the user must fix -- same class as bad_url, NOT the
        # same as "nothing is running yet" -- so it fails the gate.
        if result.status == "unauthorized":
            # We DID send the configured key, so base_url is not the suspect
            # here and telling them to change it sends them the wrong way.
            _say(f"[FAIL] endpoint rejected our API key: HTTP {result.code} from {result.url}")
            _say(f"       set [provider] api_key in {where} (or the IRONCORE_API_KEY env var)")
        else:
            _say(
                f"[FAIL] got HTTP {result.code} from {result.url} "
                "-- is this an OpenAI-compatible endpoint?"
            )
            if not endpoint.rstrip("/").endswith("/v1"):
                _say(f"       base_url usually ends with /v1 (yours is {endpoint})")
            _say(f"       set [provider] base_url in {where}")
        ok = False
    elif result.status == "bad_payload":
        _say(f"[FAIL] {result.url} answered {result.code} but not with an OpenAI model list")
        _say(f"       point [provider] base_url at an OpenAI-compatible server ({where})")
        ok = False
    else:
        _say(f"[ok] endpoint reachable: {result.url} ({len(result.models)} model(s) listed)")
        ok = _report_models(result, settings, where) and ok

    if not ok or result.status != "ok":
        _say("     no model ready yet? `ironcore demo` runs a real session fully offline")
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
        _say(f"[FAIL] {result.url} answered, but lists no models at all")
        _say(f"       pull one first (e.g. `ollama pull {settings.provider.model}`)")
        return False

    shown = ", ".join(result.models[:5])
    if len(result.models) > 5:
        shown += f", ... ({len(result.models)} total)"
    ok = True
    for label, model in wanted:
        if _model_available(model, result.models):
            _say(f"[ok] {label} {model} is available at the endpoint")
        else:
            _say(f"[FAIL] model {model} is not available at {result.url} (from {label})")
            _say(f"       models you have: {shown}")
            _say(f"       fix: `ollama pull {model}`, or set [provider] model in {where}")
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
        _say(f"[ok] mcp: {len(servers)} server(s) configured ({names})")
    else:
        _say(
            f"[--] mcp: {len(servers)} server(s) configured ({names}) but MCP tools "
            "are NET-risk and stay unregistered until safety.network_tools = true"
        )
    ok = True
    for name, srv in servers:
        if not srv.command:
            # the exact wording MCPManager.from_settings emits, so doctor and
            # the TUI never disagree about why an entry was dropped.
            _say(
                f"[--] mcp {name}: url-only entries are not supported yet "
                "(stdio only -- set 'command') -- will be skipped"
            )
        elif _which(srv.command) is None:
            missing = f"mcp {name}: command {srv.command!r} not found on PATH"
            if registered:
                _say(f"[FAIL] {missing}")
                ok = False
            else:
                _say(f"[!!] {missing}")
                _say("     harmless today (this server is not registered); fix it before")
                _say("     turning safety.network_tools on")
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
# Boot mode, least to most autonomous:
#   "plan"         read-only: explore and propose; nothing is changed
#   "manual"       approve every file edit, command, and network call  (the default)
#   "accept-edits" file edits apply automatically; commands still ask
#   "auto"         full auto inside the workspace sandbox; network still asks
# Shift+Tab cycles it live in the TUI.
mode = "manual"
# workspace_only = true        # states the write jail in the system prompt; the jail
#                              # itself is always enforced, so false only drops the
#                              # sentence -- it cannot let a write escape
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

# [tools]
# search_url = "https://html.duckduckgo.com/html/"  # web_search endpoint (needs network_tools);
#                              # "" leaves web_search unregistered, fetch_url stays

# [mcp.servers.example]        # EXAMPLE server -- none configured by default.
#                              # Uncommenting registers one. stdio transport only in v0.x.
# command = "npx.cmd"          # on Windows use the real launcher name
# args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
# enabled = true
"""


def _init_say(text: str) -> None:
    """Print one ``init`` line. Like :func:`_say`, the styling is derived from
    the text and cannot alter it: an ``ironcore:`` prefix means init refused to
    do something (so the prefix carries the error colour), ``wrote …`` is the
    result the user came for, and everything else is supporting detail."""
    from rich.text import Text

    from ironcore import term

    if text.startswith("ironcore: "):
        out = Text()
        out.append("ironcore:", style=term.STYLE_FAIL)
        out.append(text[len("ironcore:") :])
        term.line(out)
    elif text.startswith("wrote "):
        term.line(Text(text, style="bold"))
    else:
        term.line(Text(text, style=term.STYLE_MUTED))


def _backup_before_overwrite(target: Path) -> int:
    """Preserve ``target``'s current bytes beside it before ``--force`` writes over them.

    Returns 0 when the overwrite may proceed (a backup was taken, or there was
    nothing to lose). Non-zero means REFUSE: a backup that could not be written
    is not a licence to destroy the original anyway -- silently losing a
    hand-typed `model =` line is the exact failure this exists to prevent.
    """
    try:
        current = target.read_bytes()
    except OSError as exc:
        _init_say(f"ironcore: could not read {_display_path(target)} to back it up: {exc}")
        _init_say("ironcore: refusing to overwrite a file whose contents cannot be preserved")
        return 1

    # Unchanged since init wrote it: nothing is at risk, so do not litter the
    # directory with a .bak that only duplicates the template.
    #
    # Compared as decoded text with newlines normalised, NOT as raw bytes: on
    # Windows `write_text` translates \n -> \r\n, so init's own output never
    # equals STARTER_CONFIG byte for byte there. A byte compare would therefore
    # always miss, and the .bak policy below leans on this check to guarantee a
    # real backup is never replaced by a copy of the template. Bytes that are
    # not valid UTF-8 are, by definition, not the template -- back them up.
    try:
        unchanged = current.decode("utf-8").replace("\r\n", "\n") == STARTER_CONFIG
    except UnicodeDecodeError:
        unchanged = False
    if unchanged:
        return 0

    backup = target.with_name(target.name + ".bak")
    # Policy: ONE backup path, overwritten each time -- not config.toml.bak.1,
    # .bak.2, ... A numbered chain leaves a pile nobody can tell apart, whereas
    # this single path always means "the config as it was before the last
    # --force". Overwriting is safe *because* of the identical-content check
    # above: a second `init --force` finds the template already in place and
    # returns before reaching here, so a real backup can never be replaced by a
    # copy of the template. The only way to lose a .bak is edit-force-edit-force,
    # where the newer edit is the one worth keeping.
    if backup.is_dir():
        _init_say(f"ironcore: {_display_path(backup)} is a directory, not a backup file")
        _init_say("ironcore: remove or rename it, then re-run `ironcore init --force`")
        _init_say(f"ironcore: leaving {_display_path(target)} untouched")
        return 1
    try:
        backup.write_bytes(current)
    except OSError as exc:
        _init_say(f"ironcore: could not write the backup {_display_path(backup)}: {exc}")
        _init_say(f"ironcore: leaving {_display_path(target)} untouched")
        return 1
    _init_say(f"backed up the config that was there to {_display_path(backup)}")
    return 0


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
        _init_say(f"ironcore: {target} is a directory, not a config file")
        _init_say("ironcore: remove or rename it, then re-run `ironcore init`")
        return 1
    if target.exists() and not force:
        _init_say(f"ironcore: {target} already exists; pass --force to overwrite it")
        return 1
    # --force is the only path that can destroy something a user typed by hand,
    # so the old contents are preserved first -- and if they cannot be, the
    # overwrite does not happen at all.
    if target.exists() and force:
        backup_code = _backup_before_overwrite(target)
        if backup_code != 0:
            return backup_code
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(STARTER_CONFIG, encoding="utf-8")
    except OSError as exc:
        _init_say(f"ironcore: could not write {target}: {exc}")
        return 1
    _init_say(f"wrote {target}")
    _init_say("edit [provider] base_url + model, then run `ironcore doctor`")
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
        return run_demo()  # narrated to the shared console, styled when on a TTY

    captured: list[str] = []
    code = run_demo(emit=captured.append)
    if code == 0:
        print("demo: PASS -- offline session completed (read -> edit -> verify -> done)")
        return 0
    print("\n".join(captured))
    print("demo: FAIL -- the offline session did not complete")
    return 1


# --------------------------------------------------------------------------
# exec (headless)
# --------------------------------------------------------------------------


def cmd_exec(
    prompt: str,
    *,
    mode: str = "plan",
    json_output: bool = False,
    project_dir: Path | None = None,
) -> int:
    """Run one headless turn (``ironcore exec``); return its exit code.

    Import-light like doctor/demo/init: the engine is imported lazily inside
    ``ironcore.headless``, invoked only here. Exit codes: 0 on ``TurnCompleted``,
    1 on ``TurnError``, **2 on ``ConfigError``** — a broken config is caught here
    (before the generic ``main`` backstop, which would return 1) so a scripted
    caller can tell "my setup is wrong" (2) apart from "the turn failed" (1).
    """
    from ironcore import headless
    from ironcore.config.settings import ConfigError, Settings
    from ironcore.safety.modes import Mode

    ws = project_dir if project_dir is not None else Path.cwd()
    try:
        settings, notes = Settings.load_with_notes(project_dir=ws)
    except ConfigError as exc:
        print(f"ironcore: {exc}", file=sys.stderr)
        print(_CONFIG_HINT, file=sys.stderr)
        return 2
    for note in notes:  # T8 clamps / skipped MCP servers ride stderr, never stdout
        print(note, file=sys.stderr)

    engine, registry = headless.build_engine(settings, ws, Mode(mode))
    return headless.run_exec(engine, prompt, json_output=json_output, registry=registry)


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
    if args.command == "exec":
        return cmd_exec(args.prompt, mode=args.mode, json_output=args.json_output)
    # No subcommand: launch the interactive TUI only when we own a real
    # terminal. Non-TTY (pipes, CI, captured tests) gets the banner and a
    # non-zero exit — driving a full-screen app into a pipe would hang, and
    # exiting 0 would tell the caller a session ran. Imported lazily so
    # --version and doctor stay import-light.
    if sys.stdout.isatty():
        from ironcore.tui.app import run_app

        return run_app(resume=args.resume)
    _print_banner()
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
