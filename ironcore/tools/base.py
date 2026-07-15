"""Tool contract and registry.

CONTRACT (docs/CONTRACTS.md #Tool): a Tool declares a name, a description
written for the MODEL (it is prompt text — keep it short, concrete, and
example-bearing; small models read these more literally than frontier
models), a JSON-schema parameter spec, and exactly one ToolRisk class.

Tools never print, never prompt, never gate themselves — the turn engine
routes every call through the safety policy first. Tools return
ToolResult; they raise only for programmer errors.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ironcore.safety.risk import ToolRisk


@dataclass
class ToolResult:
    """Outcome of one tool execution.

    `output` is what the model sees (already truncated/redacted upstream).
    `data` carries structured payloads for the harness (never shown raw).
    """

    ok: bool
    output: str
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """Base class for all tools."""

    name: str
    description: str
    risk: ToolRisk
    #: JSON schema for the arguments object (OpenAI function-call format).
    parameters: dict[str, Any]

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Execute. Must be side-effect-free for READ tools."""

    def spec(self) -> dict[str, Any]:
        """OpenAI-compatible function spec for native tool-calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Holds the tools exposed to a session. Names are unique."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self) -> list[dict[str, Any]]:
        """Function specs for providers with native tool-calling."""
        return [t.spec() for t in self._tools.values()]
