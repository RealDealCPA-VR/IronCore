"""Settings: defaults <- user config <- project config <- environment.

Files (TOML):
  user:    ~/.ironcore/config.toml
  project: <workspace>/.ironcore/config.toml   (committable)

Environment overrides (highest precedence):
  IRONCORE_BASE_URL, IRONCORE_MODEL, IRONCORE_API_KEY, IRONCORE_MODE,
  IRONCORE_ROLE_PLANNER, IRONCORE_ROLE_CODER, IRONCORE_ROLE_SUMMARIZER,
  IRONCORE_ROLE_VERIFIER

THE AUTONOMY CEILING (docs/SAFETY.md T8). The project file is the ONLY layer
that arrives with a `git clone`, so it is the only untrusted one. It may LOWER
autonomy freely; it may never RAISE `safety.mode`, turn `safety.network_tools`
on, turn `plugins.enabled` back on, or ADD an `[mcp.servers.*]` entry above the
ceiling the user layer set (defaults included -- an absent user config means the
built-in `manual` / network-off floor IS the ceiling). Env is NOT clamped:
`IRONCORE_MODE` comes from the user's own shell, not from the repo.
Every clamp emits a note (load_with_notes) -- silent downgrades would leave both
the user and an honest repo author guessing.

A ceiling value IronCore cannot use is a ConfigError naming the user's file, not
a skipped clamp: skipping leaves the untrusted layer's value standing (fails
OPEN) and masks the user's own error behind the repo they cloned. For the same
reason ceilings are compared COERCED, never raw: `enabled = "false"` is False to
pydantic, so a raw comparison would disagree with the value that actually ships.

Malformed files and invalid values raise ConfigError with a human message
(file path + line for TOML errors) -- callers never see a raw traceback.
"""

from __future__ import annotations

import codecs
import copy
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from ironcore.safety.modes import Mode  # config may import safety; safety imports stdlib only


class ConfigError(Exception):
    """A config file is malformed or contains an invalid value.

    The message is user-facing: it names the offending file (and line, for
    TOML syntax errors) or lists the valid values. cli/doctor catches this
    and exits 1 with the message instead of a traceback.
    """


class ProviderSettings(BaseModel):
    base_url: str = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible port
    api_key: str = "ironcore-local"  # local servers ignore it; never ship a real key
    model: str = "qwen3-coder:30b"
    #: which client to build: "auto" picks OllamaProvider for an Ollama-looking
    #: endpoint (unlocking keep_alive + /api introspection) and the generic
    #: OpenAI-compatible client otherwise; "ollama"/"openai" force one.
    type: str = "auto"


class RoleModels(BaseModel):
    """Optional per-role model routing (docs/MODELS.md #5): a big model can
    plan while a small fast one executes, or the reverse. None = use
    provider.model for everything."""

    planner: str | None = None
    coder: str | None = None
    summarizer: str | None = None
    verifier: str | None = None


class SafetySettings(BaseModel):
    mode: str = "manual"  # boot mode; must be a safety.modes.Mode value
    #: prompt-level statement of the write jail, NOT a switch that arms it:
    #: `ironcore/tools/fs_write.py` calls `resolve_jailed()` unconditionally, so
    #: turning this off only drops the sentence from the system prompt
    #: (`core/composer.py`) -- it cannot let a write escape. That is why it needs
    #: no T8 clamp: an untrusted project layer setting it false gains nothing.
    workspace_only: bool = True  # path jail on writes (IC-401)
    network_tools: bool = False  # NET-risk tools not even registered unless true


class EnvelopeSettings(BaseModel):
    """How IronCore molds itself to the model (docs/MODELS.md)."""

    #: measure an UNPROBED model in the background on first launch, so the
    #: engine adapts automatically. Off = stay on floor defaults until /probe.
    auto_probe: bool = True

    #: seed a usable profile from endpoint introspection in ~1s before the full probe runs
    instant_seed: bool = True

    #: self-improvement loop (MS-8): record live-session outcomes per model and
    #: conservatively LOWER any ladder score the evidence contradicts at session
    #: start (downgrade-only; /probe re-measures). Off = no recording, no tuning.
    auto_tune: bool = True

    #: vision override (MS-6): force image attachment on/off for endpoints
    #: without introspection (e.g. vLLM serving a VL model). None (the default)
    #: trusts the CapabilityProfile's seeded/measured ``vision`` flag.
    vision: bool | None = None


class EngineSettings(BaseModel):
    """Turn-engine knobs (the additive ``[engine]`` TOML section, MS-4)."""

    #: Best-of-N escape hatches: the TOTAL candidate budget per turn at the
    #: mechanically-verified seams (a tool call that will not parse, an edit
    #: that will not apply). 1 = disabled — no extra provider calls, the
    #: default; N races up to N-1 resampled candidates per turn, each still
    #: passing the safety gate and charged to the turn budget.
    best_of_n: int = Field(default=1, ge=1, le=5)


class MCPServerSettings(BaseModel):
    """One MCP tool server (an additive ``[mcp.servers.<name>]`` TOML table, MS-7).

    v1 speaks the stdio transport only: ``command`` (+ ``args``/``env``) spawns
    the server as a child process, resolved via PATH but never through a shell —
    so on Windows launcher shims need their real name (``command = "npx.cmd"``).
    ``url`` is accepted so http-transport configs parse, but such entries are
    skipped with a note until an http client ships.

    ``env`` values support ``${VAR}`` placeholders, expanded from IronCore's own
    environment at load time (see ``_expand_mcp_env``): secrets belong in your
    shell, never in the committable project config."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    timeout_s: float = Field(default=30.0, gt=0)
    enabled: bool = True

    @model_validator(mode="after")
    def _require_transport(self) -> MCPServerSettings:
        if not self.command and not self.url:
            raise ValueError("an MCP server needs 'command' (stdio) or 'url'")
        return self


class MCPSettings(BaseModel):
    """MCP integration (the additive ``[mcp]`` TOML section, MS-7). Tools from
    these servers are NET-risk: they are never even registered unless
    ``safety.network_tools`` is true."""

    servers: dict[str, MCPServerSettings] = Field(default_factory=dict)


class PluginSettings(BaseModel):
    """Entry-point plugin discovery (the additive ``[plugins]`` TOML section, MS-5).

    Default ON: installing a plugin distribution into IronCore's environment
    already executed arbitrary code (pip install), so installation — not
    discovery — is the consent moment (docs/SAFETY.md T9). ``enabled = false``
    is the hardened-setup switch: discovery is skipped entirely."""

    enabled: bool = True


class ToolsSettings(BaseModel):
    """Optional tool configuration (the additive ``[tools]`` section, PKG-5).

    ``search_url`` is the endpoint the NET-risk ``web_search`` tool queries — an
    HTML search page that takes a ``?q=`` query and returns result anchors (a
    SearXNG instance, or the DuckDuckGo HTML endpoint, the default). Like
    ``fetch_url``, ``web_search`` is a NET tool: it is never registered unless
    ``safety.network_tools`` is true, and every call ASKS (NET is never
    auto-allowed) — and the approval preview names the configured endpoint plus
    the query, so a repointed ``search_url`` is as visible on approval as any
    ``fetch_url`` destination (``core/engine.py`` ``_preview``). So this is NOT
    an autonomy control and is NOT under the T8 ceiling — a cloned project
    pointing it elsewhere escalates nothing (``fetch_url`` already reaches any
    host) and cannot exfil more quietly than ``fetch_url``, because the human
    sees where each search goes. An empty ``search_url`` leaves ``web_search``
    unregistered while ``fetch_url`` stays."""

    #: HTML search endpoint for web_search. Empty string = no web_search tool.
    search_url: str = "https://html.duckduckgo.com/html/"


class SkillSettings(BaseModel):
    """Skills — the SKILL.md open standard (the additive ``[skills]`` section, PKG-4).

    A skill is a ``<dir>/SKILL.md`` file (docs/SKILLS.md). Discovery reads the
    user's ``~/.ironcore/skills`` and the workspace's ``.ironcore/skills``.
    Skills are INERT Markdown until invoked and carry no autonomy: their scripts
    run through the model's own ``run_command`` under the EXEC gate, so this
    section is NOT under the T8 autonomy ceiling — a project layer may set it
    (project skills are still first-use gated, and an unconfirmed one never
    reaches the model catalog)."""

    #: Discover skills at all. Off = no catalog, no ``use_skill`` tool, ``/skill``
    #: reports it is disabled — the hardened-setup switch.
    enabled: bool = True

    #: Also read ``.claude`` / ``.codex`` / ``.grok`` ``/skills`` dirs (at both the
    #: user-home and workspace level), so skills authored for those tools work
    #: unchanged. Off by default; opt in for zero-setup ecosystem compatibility.
    compat_dirs: bool = False


class Settings(BaseModel):
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    roles: RoleModels = Field(default_factory=RoleModels)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    envelope: EnvelopeSettings = Field(default_factory=EnvelopeSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    skills: SkillSettings = Field(default_factory=SkillSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)

    @classmethod
    def load(
        cls,
        project_dir: Path | None = None,
        user_config: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Settings:
        """Layered load. `user_config` and `env` are injectable for tests.

        Exactly ``load_with_notes(...)[0]`` — use that when you can surface the
        notes (a clamped project config, a skipped MCP server) to the user."""
        return cls.load_with_notes(
            project_dir=project_dir, user_config=user_config, env=env
        )[0]

    @classmethod
    def load_with_notes(
        cls,
        project_dir: Path | None = None,
        user_config: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[Settings, list[str]]:
        """Layered load plus the user-facing notes the load produced.

        Notes are plain lines ready to print (`ironcore doctor`) or post as boot
        notes (the TUI): autonomy clamps (§T8) and MCP servers dropped for an
        unset ``${VAR}``. Empty list = nothing was silently changed.
        """
        if user_config is None:
            user_config = Path.home() / ".ironcore" / "config.toml"
        if env is None:
            env = dict(os.environ)

        project_config = (project_dir / ".ironcore" / "config.toml") if project_dir else None
        # Layers stay SEPARATE through the merge: the ceiling has to know what
        # the user asked for vs. what the (untrusted, cloned) project file asked
        # for, and that is unrecoverable from the merged dict afterwards.
        layers: dict[str, dict[str, Any]] = {"user": {}, "project": {}}
        for layer, path in (("user", user_config), ("project", project_config)):
            if path is not None and path.exists():
                try:
                    raw = path.read_bytes()
                    # A UTF-8 BOM is a routine Windows editor artifact and decodes
                    # fine, so tomllib would blame it on TOML syntax at line 1.
                    raw = raw.removeprefix(codecs.BOM_UTF8)
                    layers[layer] = tomllib.loads(raw.decode("utf-8"))
                except tomllib.TOMLDecodeError as exc:
                    # exc's message already carries "(at line N, column M)".
                    raise ConfigError(f"malformed config file {path}: {exc}") from exc
                except UnicodeDecodeError as exc:
                    raise ConfigError(
                        f"malformed config file {path}: not valid UTF-8 at byte "
                        f"{exc.start} -- TOML must be UTF-8 (re-save the file as UTF-8)"
                    ) from exc
                except OSError as exc:
                    # unreadable, a directory, a dead junction: still ours to
                    # report, not a raw traceback (module docstring).
                    raise ConfigError(
                        f"cannot read config file {path}: {exc.strerror or exc}"
                    ) from exc

        # deepcopy: _deep_merge grafts sub-dicts by REFERENCE, so merging into
        # a raw layer would let the project file overwrite the very user values
        # the ceiling is about to compare against.
        data: dict[str, Any] = copy.deepcopy(layers["user"])
        _deep_merge(data, copy.deepcopy(layers["project"]))
        notes = _clamp_autonomy(
            data, layers["user"], layers["project"], user_config, env.get("IRONCORE_MODE")
        )

        _apply_env(data, env)
        try:
            settings = cls.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "(top level)"
            raise ConfigError(f"invalid config value at {where}: {first['msg']}") from None
        try:
            Mode(settings.safety.mode)
        except ValueError:
            valid = ", ".join(m.value for m in Mode)
            raise ConfigError(
                f"invalid safety.mode {settings.safety.mode!r}; valid modes: {valid}"
            ) from None
        notes.extend(_expand_mcp_env(settings, env))
        return settings, notes


#: Autonomy ranking for the T8 ceiling. Ordering is the mode gate's own
#: (docs/SAFETY.md §3): each rung allows strictly more than the one below it.
_MODE_RANK: dict[str, int] = {
    Mode.PLAN.value: 0,
    Mode.MANUAL.value: 1,
    Mode.ACCEPT_EDITS.value: 2,
    Mode.AUTO.value: 3,
}


def _clamp_autonomy(
    data: dict[str, Any],
    user_layer: dict[str, Any],
    project_layer: dict[str, Any],
    user_config: Path,
    env_mode: str | None = None,
) -> list[str]:
    """T8: the project file may lower autonomy, never raise it (module docstring).

    The ceiling is the EFFECTIVE user layer — the user's TOML if it speaks, the
    built-in defaults if it does not — so a fresh install with no
    ``~/.ironcore/config.toml`` is protected too, which is the whole point:
    that user is the one most likely to `git clone` and press Enter.
    Mutates ``data`` in place; returns one note per clamp.
    """
    notes: list[str] = []
    notes.extend(_clamp_safety(data, user_layer, project_layer, user_config, env_mode))
    notes.extend(_clamp_plugins(data, user_layer, project_layer, user_config))
    notes.extend(_clamp_mcp(data, user_layer, project_layer, user_config))
    return notes


def _clamp_safety(
    data: dict[str, Any],
    user_layer: dict[str, Any],
    project_layer: dict[str, Any],
    user_config: Path,
    env_mode: str | None = None,
) -> list[str]:
    """The `[safety]` half of the ceiling: `mode` and `network_tools`."""
    notes: list[str] = []
    user_safety = _user_section(user_layer, "safety", user_config)
    project_safety = _section(project_layer, "safety")
    merged = data.get("safety")
    if not isinstance(merged, dict):
        return notes  # absent (nothing to clamp) or garbage (model_validate is loud)

    # A ceiling we cannot RANK is a ceiling we cannot enforce, so an unusable
    # user-layer mode is an error, never a skipped clamp: silently falling
    # through here would leave the (untrusted) project layer's value standing --
    # the exact escalation this function exists to stop -- and would also mask
    # the user's own typo, which raises loudly when no project file is present.
    ceiling_mode = user_safety.get("mode")
    if ceiling_mode is None:
        ceiling_mode = SafetySettings.model_fields["mode"].default
    elif not isinstance(ceiling_mode, str) or ceiling_mode not in _MODE_RANK:
        valid = ", ".join(_MODE_RANK)
        raise ConfigError(
            f"invalid safety.mode {ceiling_mode!r} in {user_config}; valid modes: {valid}"
        )
    wanted_mode = project_safety.get("mode")
    if (
        isinstance(wanted_mode, str)
        and wanted_mode in _MODE_RANK
        and _MODE_RANK[wanted_mode] > _MODE_RANK[ceiling_mode]
    ):
        merged["mode"] = ceiling_mode
        if env_mode:
            # _apply_env runs AFTER the clamp and is deliberately not clamped, so
            # this clamp will not survive the load. Reporting it as though it did
            # would make the one surface whose entire value is trustworthiness lie.
            notes.append(
                f"[safety] project config requested mode {wanted_mode!r}, above your "
                f"ceiling {ceiling_mode!r} (docs/SAFETY.md T8) -- but IRONCORE_MODE="
                f"{env_mode} from your own shell wins; env is never clamped."
            )
        else:
            notes.append(
                f"[safety] project config requested mode {wanted_mode!r}; clamped to your "
                f"ceiling {ceiling_mode!r} (docs/SAFETY.md T8). Grant it for this session "
                f"with Shift+Tab, or raise the ceiling in {user_config}."
            )

    # network_tools has no per-session keystroke, so an unset user layer means
    # the default (off) is the ceiling and a cloned repo cannot switch NET on.
    ceiling_net = _user_bool(
        user_safety,
        "network_tools",
        SafetySettings.model_fields["network_tools"].default,
        "safety.network_tools",
        user_config,
    )
    if _project_bool(project_safety, "network_tools") is True and ceiling_net is not True:
        merged["network_tools"] = False
        notes.append(
            "[safety] project config requested network_tools = true; kept OFF by your "
            # ASCII only: these lines print to the Windows console, where a
            # cp1252 code page turns an em-dash into a replacement char.
            "ceiling (docs/SAFETY.md T8). NET tools -- including MCP -- stay unregistered "
            f"until [safety] network_tools = true in {user_config}."
        )
    return notes


def _clamp_plugins(
    data: dict[str, Any],
    user_layer: dict[str, Any],
    project_layer: dict[str, Any],
    user_config: Path,
) -> list[str]:
    """The plugin kill switch is an autonomy control too (docs/SAFETY.md §8/T9).

    ``[plugins] enabled = false`` is THE hardened-setup switch, and entry-point
    plugin code runs at boot and during ``doctor``. So the project layer may turn
    discovery off, never back on -- otherwise a clone silently re-arms code
    execution for the one user who explicitly disarmed it.
    """
    notes: list[str] = []
    ceiling = _user_bool(
        _user_section(user_layer, "plugins", user_config),
        "enabled",
        PluginSettings.model_fields["enabled"].default,
        "plugins.enabled",
        user_config,
    )
    if _project_bool(_section(project_layer, "plugins"), "enabled") is not True or ceiling is True:
        return notes
    merged = data.get("plugins")
    if not isinstance(merged, dict):
        return notes  # garbage section — model_validate reports it loudly
    merged["enabled"] = False
    notes.append(
        "[plugins] project config requested plugins enabled = true; kept OFF by your "
        "ceiling (docs/SAFETY.md T9). Entry-point plugin discovery stays skipped until "
        f"[plugins] enabled = true in {user_config}."
    )
    return notes


def _clamp_mcp(
    data: dict[str, Any],
    user_layer: dict[str, Any],
    project_layer: dict[str, Any],
    user_config: Path,
) -> list[str]:
    """T8 x T10: a cloned config may not put an executable on the launch path.

    Every configured server is SPAWNED AT LAUNCH, not at the first tool call: the
    TUI's mount worker runs ``MCPManager.register_into``, which calls
    ``tools/list`` on each server to enumerate its tools -- before any prompt,
    tool call or approval, with IronCore's environment inherited. So an
    ``[mcp.servers.*]`` table that arrived with a `git clone` is boot-time code
    execution for every user who legitimately turned NET on, and the NET switch
    alone does not contain it. The project layer may DISABLE a server the user
    declared (a lowering, always allowed); it may never introduce one, nor
    redefine the command of one.
    """
    notes: list[str] = []
    project_servers = _section(_section(project_layer, "mcp"), "servers")
    if not project_servers:
        return notes
    user_servers = _section(_user_section(user_layer, "mcp", user_config), "servers")
    merged = _section(data, "mcp").get("servers")
    if not isinstance(merged, dict):
        return notes  # garbage section — model_validate reports it loudly
    for name, entry in project_servers.items():
        declared = user_servers.get(name)
        if not isinstance(declared, dict):
            merged.pop(name, None)
            notes.append(
                f"[mcp] project config declares server {name!r}; ignored (docs/SAFETY.md "
                "T8/T10) -- every configured server is spawned at launch to list its "
                f"tools, so a cloned config cannot add one. Declare it in {user_config} "
                "to run it."
            )
            continue
        restored = copy.deepcopy(declared)
        if _project_bool(entry if isinstance(entry, dict) else {}, "enabled") is False:
            restored["enabled"] = False  # turning a server OFF is a lowering
        if merged.get(name) != restored:
            notes.append(
                f"[mcp] project config overrides server {name!r}; ignored except "
                f"'enabled = false' -- your definition in {user_config} stands."
            )
        merged[name] = restored
    return notes


#: pydantic's own bool rules, so a ceiling read agrees with the value that ships
#: (`"false"`, `0` and `"no"` are all False to the model, and to a TOML author).
_BOOL = TypeAdapter(bool)


def _user_bool(
    section: dict[str, Any], key: str, default: bool, where: str, user_config: Path
) -> bool:
    """The user's effective value for a boolean ceiling key, coerced.

    A value pydantic would reject is a ConfigError naming the user's file. The
    tempting fall back to the field default fails OPEN on every permissive
    default (``plugins.enabled``): the untrusted project layer would both
    escalate past a disarmed switch AND mask the user's own config error.
    """
    if key not in section:
        return default
    try:
        return _BOOL.validate_python(section[key])
    except ValidationError:
        raise ConfigError(
            f"invalid config value at {where} in {user_config}: "
            f"{section[key]!r} is not a boolean (true or false)"
        ) from None


def _project_bool(section: dict[str, Any], key: str) -> bool | None:
    """What the project layer asked for, coerced the same way -- ``None`` when it
    did not ask, or asked with garbage (which ``model_validate`` reports loudly
    on the merged data; the clamp must not pre-empt that with a wrong answer)."""
    if key not in section:
        return None
    try:
        return _BOOL.validate_python(section[key])
    except ValidationError:
        return None


#: ``${VAR}`` in an MCP env value. Bare ``$VAR`` is left literal on purpose:
#: an env value like a JSON blob or a Windows path must survive untouched.
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_mcp_env(settings: Settings, env: dict[str, str]) -> list[str]:
    """Resolve ``${VAR}`` in every enabled MCP server's ``env`` values.

    An unset (or empty) variable DROPS that server with a note instead of
    passing the four literal characters to the child, where it would surface as
    an opaque auth failure from someone else's process. Disabled entries are
    left untouched — they are never spawned, so an unset var is not news.
    Mutates ``settings.mcp.servers``; returns one note per dropped server.
    """
    notes: list[str] = []
    kept: dict[str, MCPServerSettings] = {}
    for name, server in settings.mcp.servers.items():
        if not server.enabled or not server.env:
            kept[name] = server
            continue
        missing: list[str] = []
        resolved: dict[str, str] = {}
        for key, value in server.env.items():

            def _sub(match: re.Match[str], _missing: list[str] = missing) -> str:
                var = match.group(1)
                filled = env.get(var)
                if not filled:  # unset or empty — an empty token is as broken as none
                    _missing.append(var)
                    return ""
                return filled

            resolved[key] = _ENV_PLACEHOLDER.sub(_sub, value)
        if missing:
            unique = list(dict.fromkeys(missing))
            names = ", ".join(f"${{{var}}}" for var in unique)
            verb = "is" if len(unique) == 1 else "are"
            notes.append(
                f"[mcp] server {name!r} skipped: {names} {verb} not set in your environment"
            )
            continue
        server.env = resolved
        kept[name] = server
    settings.mcp.servers = kept
    return notes


def _section(layer: dict[str, Any], name: str) -> dict[str, Any]:
    value = layer.get(name)
    return value if isinstance(value, dict) else {}


def _user_section(layer: dict[str, Any], name: str, user_config: Path) -> dict[str, Any]:
    """The USER layer's ``[name]`` table -- loudly, if it is not a table.

    Falling through to ``{}`` here would take the permissive built-in default as
    the ceiling, so the untrusted project layer would escalate past the user's
    own (broken) intent and hide the error that fires when no project file is
    present. Fail closed and loud, never open.
    """
    value = layer.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"invalid config value at {name} in {user_config}: expected a [{name}] "
            f"table, got {type(value).__name__}"
        )
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _apply_env(data: dict[str, Any], env: dict[str, str]) -> None:
    mapping = {
        "IRONCORE_BASE_URL": ("provider", "base_url"),
        "IRONCORE_MODEL": ("provider", "model"),
        "IRONCORE_API_KEY": ("provider", "api_key"),
        "IRONCORE_MODE": ("safety", "mode"),
        "IRONCORE_ROLE_PLANNER": ("roles", "planner"),
        "IRONCORE_ROLE_CODER": ("roles", "coder"),
        "IRONCORE_ROLE_SUMMARIZER": ("roles", "summarizer"),
        "IRONCORE_ROLE_VERIFIER": ("roles", "verifier"),
    }
    for var, (section, key) in mapping.items():
        if var in env and env[var]:
            section_data = data.setdefault(section, {})
            if not isinstance(section_data, dict):
                continue  # garbage section in a file — validation reports it loudly
            section_data[key] = env[var]
