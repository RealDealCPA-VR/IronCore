"""Settings: defaults <- user config <- project config <- environment.

Files (TOML):
  user:    ~/.ironcore/config.toml
  project: <workspace>/.ironcore/config.toml   (committable)

Environment overrides (highest precedence):
  IRONCORE_BASE_URL, IRONCORE_MODEL, IRONCORE_API_KEY, IRONCORE_MODE,
  IRONCORE_ROLE_PLANNER, IRONCORE_ROLE_CODER, IRONCORE_ROLE_SUMMARIZER,
  IRONCORE_ROLE_VERIFIER

Malformed files and invalid values raise ConfigError with a human message
(file path + line for TOML errors) -- callers never see a raw traceback.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

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
    skipped with a note until an http client ships."""

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


class Settings(BaseModel):
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    roles: RoleModels = Field(default_factory=RoleModels)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    envelope: EnvelopeSettings = Field(default_factory=EnvelopeSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)

    @classmethod
    def load(
        cls,
        project_dir: Path | None = None,
        user_config: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Settings:
        """Layered load. `user_config` and `env` are injectable for tests."""
        if user_config is None:
            user_config = Path.home() / ".ironcore" / "config.toml"
        if env is None:
            env = dict(os.environ)

        project_config = (project_dir / ".ironcore" / "config.toml") if project_dir else None
        data: dict[str, Any] = {}
        for path in (user_config, project_config):
            if path is not None and path.exists():
                with path.open("rb") as f:
                    try:
                        _deep_merge(data, tomllib.load(f))
                    except tomllib.TOMLDecodeError as exc:
                        # exc's message already carries "(at line N, column M)".
                        raise ConfigError(f"malformed config file {path}: {exc}") from exc

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
        return settings


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
