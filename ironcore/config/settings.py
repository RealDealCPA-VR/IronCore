"""Settings: defaults <- user config <- project config <- environment.

Files (TOML):
  user:    ~/.ironcore/config.toml
  project: <workspace>/.ironcore/config.toml   (committable)

Environment overrides (highest precedence, IC-101 extends the set):
  IRONCORE_BASE_URL, IRONCORE_MODEL, IRONCORE_API_KEY, IRONCORE_MODE
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ProviderSettings(BaseModel):
    base_url: str = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible port
    api_key: str = "ironcore-local"  # local servers ignore it; never ship a real key
    model: str = "qwen3-coder:30b"


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


class Settings(BaseModel):
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    roles: RoleModels = Field(default_factory=RoleModels)
    safety: SafetySettings = Field(default_factory=SafetySettings)

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
                    _deep_merge(data, tomllib.load(f))

        _apply_env(data, env)
        return cls.model_validate(data)


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
    }
    for var, (section, key) in mapping.items():
        if var in env and env[var]:
            data.setdefault(section, {})[key] = env[var]
