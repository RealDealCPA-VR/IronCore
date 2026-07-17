"""Entry-point plugin loading (MS-5): the five groups, fail-safe skips,
builtins-win duplicate handling, safety gating, and registry wiring.

All offline: entry points are REAL ``importlib.metadata.EntryPoint`` objects
whose value targets a fake module inserted into ``sys.modules`` — zero
installed distributions, zero network. Async via ``asyncio.run`` (no
pytest-asyncio). 🔒 pins CONTRACTS §11.
"""

from __future__ import annotations

import asyncio
import sys
import types
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from ironcore.commands import build_default_registry as build_cmds
from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.commands.envelopecmd import probe_and_swap
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import ToolCallFinished, ToolCallRequested
from ironcore.envelope.probe_tools import ToolFormProbe
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import ProbeResult
from ironcore.plugins import (
    GROUP_COMMANDS,
    GROUP_EDIT_FORMATS,
    GROUP_PROBES,
    GROUP_PROVIDERS,
    GROUP_TOOLS,
    LoadedPlugins,
    load_plugins,
)
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.providers.registry import ProviderRegistry, select_provider_factory
from ironcore.safety.modes import Mode
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolResult
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tools.patch import PatchResult

MODULE = "ic_fake_plugin"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_module(monkeypatch):
    """A real module in sys.modules for EntryPoint.load() to import from."""
    mod = types.ModuleType(MODULE)
    monkeypatch.setitem(sys.modules, MODULE, mod)
    return mod


def _ep(name: str, attr: str, group: str) -> EntryPoint:
    # constructed directly: stable positional signature on 3.11–3.13. Keeping
    # construction in this one helper makes any future stdlib change a
    # one-line fix (plan risk note).
    return EntryPoint(name, f"{MODULE}:{attr}", group)


def _fn(*eps: EntryPoint):
    def entry_points_fn(*, group: str):
        return [e for e in eps if e.group == group]

    return entry_points_fn


def _named_tool(tool_name: str, tool_risk: ToolRisk = ToolRisk.READ) -> Tool:
    class _T(Tool):
        name = tool_name
        description = f"plugin tool. Example: {tool_name}()"
        risk = tool_risk
        parameters = {"type": "object", "properties": {}, "required": []}

        async def run(self, **kwargs):
            return ToolResult(ok=True, output="plugin ran")

    return _T()


def _reasons(lp: LoadedPlugins) -> str:
    return " | ".join(s.reason for s in lp.skipped)


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _native_weather_reply() -> CompletionResult:
    return CompletionResult(
        message=Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="n",
                    name="get_weather",
                    arguments={"city": "Paris", "units": "celsius"},
                )
            ],
        )
    )


# --------------------------------------------------------------------------- #
# tools group
# --------------------------------------------------------------------------- #


def test_tool_plugin_loads_and_registers_behind_builtins(fake_module, tmp_path):
    fake_module.tools = lambda settings, workspace: _named_tool("hello_tool", ToolRisk.WRITE)
    lp = load_plugins(
        Settings(), tmp_path, entry_points_fn=_fn(_ep("hello", "tools", GROUP_TOOLS))
    )
    assert [t.name for t in lp.tools] == ["hello_tool"]
    assert lp.skipped == []
    registry = build_tools(Settings(), tmp_path, plugins=lp)
    assert registry.get("hello_tool") is lp.tools[0]
    assert any(s["function"]["name"] == "hello_tool" for s in registry.specs())


def test_tool_factory_receives_settings_and_workspace(fake_module, tmp_path):
    seen = {}

    def factory(settings, workspace):
        seen["settings"] = settings
        seen["workspace"] = workspace
        return _named_tool("probe_args")

    fake_module.factory = factory
    settings = Settings()
    load_plugins(settings, tmp_path, entry_points_fn=_fn(_ep("x", "factory", GROUP_TOOLS)))
    assert seen["settings"] is settings
    assert seen["workspace"] == tmp_path


def test_broken_tool_factory_is_skipped_and_others_still_load(fake_module, tmp_path):
    def bad(settings, workspace):
        raise ZeroDivisionError("boom")

    fake_module.bad = bad
    fake_module.good = lambda s, w: [_named_tool("survivor")]
    lp = load_plugins(
        Settings(),
        tmp_path,
        entry_points_fn=_fn(
            _ep("aaa_bad", "bad", GROUP_TOOLS), _ep("bbb_good", "good", GROUP_TOOLS)
        ),
    )
    assert [t.name for t in lp.tools] == ["survivor"]
    assert len(lp.skipped) == 1
    assert lp.skipped[0].name == "aaa_bad"
    assert "ZeroDivisionError" in lp.skipped[0].reason


def test_invalid_tools_are_skipped_with_explicit_reasons(fake_module, tmp_path):
    class StringRisk(Tool):
        name = "string_risk"
        description = "x"
        risk = "write"  # a plain string is NOT a ToolRisk member
        parameters = {"type": "object", "properties": {}}

        async def run(self, **kwargs):
            return ToolResult(ok=True, output="")

    fake_module.not_a_tool = lambda s, w: object()
    fake_module.string_risk = lambda s, w: StringRisk()
    lp = load_plugins(
        Settings(),
        tmp_path,
        entry_points_fn=_fn(
            _ep("a", "not_a_tool", GROUP_TOOLS), _ep("b", "string_risk", GROUP_TOOLS)
        ),
    )
    assert lp.tools == []
    assert "not a Tool" in _reasons(lp)
    assert "ToolRisk" in _reasons(lp)


def test_builtin_tool_names_win_including_read_image(fake_module, tmp_path):
    from ironcore.tools.fs_read import ReadFileTool
    from ironcore.tools.image import ReadImageTool

    fake_module.shadow = lambda s, w: [_named_tool("read_file"), _named_tool("read_image")]
    lp = load_plugins(
        Settings(), tmp_path, entry_points_fn=_fn(_ep("shadow", "shadow", GROUP_TOOLS))
    )
    assert len(lp.tools) == 2  # the loader has no builtin knowledge; assembly does
    registry = build_tools(Settings(), tmp_path, plugins=lp)
    assert isinstance(registry.get("read_file"), ReadFileTool)
    assert isinstance(registry.get("read_image"), ReadImageTool)
    assert {s.name for s in lp.skipped} == {"read_file", "read_image"}
    assert all("built-ins win" in s.reason for s in lp.skipped)


def test_net_risk_plugin_tool_requires_network_tools(fake_module, tmp_path):
    fake_module.net = lambda s, w: _named_tool("net_thing", ToolRisk.NET)
    eps = _fn(_ep("net", "net", GROUP_TOOLS))
    off = load_plugins(Settings(), tmp_path, entry_points_fn=eps)
    assert off.tools == []
    assert "network_tools" in off.skipped[0].reason
    on = load_plugins(
        Settings.model_validate({"safety": {"network_tools": True}}),
        tmp_path,
        entry_points_fn=eps,
    )
    assert [t.name for t in on.tools] == ["net_thing"]


def test_load_order_is_deterministic_under_shuffled_input(fake_module, tmp_path):
    fake_module.a = lambda s, w: _named_tool("aaa")
    fake_module.b = lambda s, w: _named_tool("bbb")
    fake_module.c = lambda s, w: _named_tool("ccc")
    lp = load_plugins(
        Settings(),
        tmp_path,
        entry_points_fn=_fn(
            _ep("c", "c", GROUP_TOOLS), _ep("a", "a", GROUP_TOOLS), _ep("b", "b", GROUP_TOOLS)
        ),
    )
    assert [t.name for t in lp.tools] == ["aaa", "bbb", "ccc"]


def test_unimportable_entry_point_is_skipped(fake_module, tmp_path):
    lp = load_plugins(
        Settings(),
        tmp_path,
        entry_points_fn=_fn(_ep("ghost", "missing_attr", GROUP_TOOLS)),
    )
    assert lp.tools == []
    assert len(lp.skipped) == 1 and lp.skipped[0].name == "ghost"


# --------------------------------------------------------------------------- #
# commands group
# --------------------------------------------------------------------------- #


def test_command_plugin_dispatches_and_builtin_help_wins(fake_module):
    def hello_handler(ctx, args):
        return f"hello {args}".strip()

    fake_module.COMMANDS = (
        SlashCommand("hello", "say hello", "/hello", hello_handler),
        SlashCommand("help", "shadow help", "/help", hello_handler),
    )
    lp = load_plugins(
        Settings(), Path("."), entry_points_fn=_fn(_ep("hello", "COMMANDS", GROUP_COMMANDS))
    )
    assert [c.name for c in lp.commands] == ["hello", "help"]
    registry = build_cmds(plugins=lp)
    ctx = CommandContext(settings=Settings())
    assert registry.dispatch("/hello world", ctx) == "hello world"
    assert registry.get("help").summary == "list commands"  # the builtin, not the shadow
    assert any(s.name == "help" and "built-ins win" in s.reason for s in lp.skipped)


def test_non_slashcommand_entry_is_skipped(fake_module):
    fake_module.bogus = "not a command"
    lp = load_plugins(
        Settings(), Path("."), entry_points_fn=_fn(_ep("bogus", "bogus", GROUP_COMMANDS))
    )
    assert lp.commands == []
    assert "not a SlashCommand" in _reasons(lp)


# --------------------------------------------------------------------------- #
# probes group
# --------------------------------------------------------------------------- #


class PlugFmtProbe:
    """A plugin probe that FILLS edit_formats.plugfmt (dotted-path merge)."""

    id = "PLUG-FMT"
    title = "plugin edit-format reliability"
    targets = ("edit_formats.plugfmt",)

    async def run(self, provider):
        return ProbeResult(probe_id=self.id, scores={"edit_formats.plugfmt": 0.5})


def test_probe_and_swap_appends_plugin_probes_selection_stays_closed(
    fake_module, tmp_path, monkeypatch
):
    # tests monkeypatch the 1-trial suite exactly like test_envelope_wiring:92
    monkeypatch.setattr(
        "ironcore.envelope.suite.default_envelope_dir", lambda: tmp_path / "env"
    )
    monkeypatch.setattr(
        "ironcore.envelope.suite.default_probe_suite", lambda: [ToolFormProbe(trials=1)]
    )
    fake_module.probes = lambda: PlugFmtProbe()
    lp = load_plugins(
        Settings(), tmp_path, entry_points_fn=_fn(_ep("plugfmt", "probes", GROUP_PROBES))
    )
    assert [p.id for p in lp.probes] == ["PLUG-FMT"]

    settings = Settings()
    # ToolFormProbe(trials=1) issues exactly 3 calls; PlugFmtProbe issues none.
    provider = MockProvider([_native_weather_reply(), _text("no json"), _text("no block")])
    engine = TurnEngine(
        provider,
        build_tools(settings, tmp_path),
        settings,
        CapabilityProfile(model_id="mock", honest_context=8192),
        Mode.AUTO,
        workspace=tmp_path,
        snapshots=None,
    )
    report = asyncio.run(probe_and_swap(engine, extra_probes=lp.probes))
    assert "profile updated" in report.lower()
    # the plugin probe FILLED its field…
    assert engine.profile.edit_formats.get("plugfmt") == 0.5
    # …but selection stays closed: only ladder rungs are ever recommended
    assert engine.profile.recommended_edit_format() in (
        "unified_diff",
        "search_replace",
        "whole_file",
    )
    assert engine.profile.recommended_tool_protocol() == "native"  # measured, unharmed


def test_reserved_and_malformed_probes_are_skipped(fake_module, tmp_path):
    class TokenRatioClone:
        id = "TOKEN-RATIO"  # collides with the MS-1 builtin probe id
        title = "impostor"
        targets = ("chars_per_token",)

        async def run(self, provider):  # pragma: no cover — never runs
            return ProbeResult(probe_id=self.id)

    class NoRun:
        id = "NORUN"
        title = "no run method"
        targets = ()
        run = "not callable"

    fake_module.clone = lambda: TokenRatioClone()
    fake_module.norun = lambda: NoRun()
    lp = load_plugins(
        Settings(),
        tmp_path,
        entry_points_fn=_fn(
            _ep("clone", "clone", GROUP_PROBES), _ep("norun", "norun", GROUP_PROBES)
        ),
    )
    assert lp.probes == []
    assert "reserved" in _reasons(lp)
    assert "async" in _reasons(lp)


# --------------------------------------------------------------------------- #
# providers group
# --------------------------------------------------------------------------- #


def test_plugin_provider_factory_selected_and_built_via_registry(fake_module):
    calls: dict = {}

    def myprov(**kwargs):
        calls.update(kwargs)
        return MockProvider([])

    fake_module.myprov = myprov
    lp = load_plugins(
        Settings(), Path("."), entry_points_fn=_fn(_ep("myprov", "myprov", GROUP_PROVIDERS))
    )
    assert set(lp.provider_factories) == {"myprov"}

    settings = Settings.model_validate(
        {
            "provider": {
                "type": "myprov",
                "base_url": "http://plug/v1",
                "api_key": "sk-plug",
                "model": "plug-model",
            }
        }
    )
    factory = select_provider_factory(settings, plugin_factories=lp.provider_factories)
    assert factory is myprov
    registry = ProviderRegistry.from_settings(settings, provider_factory=factory)
    assert isinstance(registry.default, MockProvider)
    assert calls == {
        "base_url": "http://plug/v1",
        "api_key": "sk-plug",
        "model": "plug-model",
    }
    # the ONE _build path: for_model constructs plugin providers too (MS-2/MS-3)
    other = registry.for_model("other-model")
    assert isinstance(other, MockProvider) and other is not registry.default
    assert calls["model"] == "other-model"


def test_reserved_provider_type_names_are_refused(fake_module):
    fake_module.f = lambda **kwargs: None
    lp = load_plugins(
        Settings(),
        Path("."),
        entry_points_fn=_fn(
            _ep("auto", "f", GROUP_PROVIDERS),
            _ep("ollama", "f", GROUP_PROVIDERS),
            _ep("openai", "f", GROUP_PROVIDERS),
        ),
    )
    assert lp.provider_factories == {}
    assert {s.name for s in lp.skipped} == {"auto", "ollama", "openai"}
    assert all("reserved" in s.reason for s in lp.skipped)


# --------------------------------------------------------------------------- #
# edit-formats group
# --------------------------------------------------------------------------- #


def test_edit_format_names_validated_and_builtins_reserved(fake_module):
    def upper(original, edit):
        return PatchResult(ok=True, new_text=original.upper())

    fake_module.upper = upper
    lp = load_plugins(
        Settings(),
        Path("."),
        entry_points_fn=_fn(
            _ep("upper", "upper", GROUP_EDIT_FORMATS),
            _ep("unified_diff", "upper", GROUP_EDIT_FORMATS),  # builtin rung
            _ep("BADFMT", "upper", GROUP_EDIT_FORMATS),  # fails the slug regex
        ),
    )
    assert list(lp.edit_formats) == ["upper"]
    assert "ladder rung" in _reasons(lp)
    assert "must match" in _reasons(lp)


def test_edit_format_plugin_applies_through_edit_file(fake_module, tmp_path):
    def shout(original, edit):
        return PatchResult(ok=True, new_text=original.upper())

    fake_module.shout = shout
    lp = load_plugins(
        Settings(), tmp_path, entry_points_fn=_fn(_ep("shout", "shout", GROUP_EDIT_FORMATS))
    )
    registry = build_tools(Settings(), tmp_path, plugins=lp)
    tool = registry.get("edit_file")
    assert "shout" in tool.parameters["properties"]["format"]["enum"]
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    result = asyncio.run(tool.run(path="a.txt", format="shout", edit="ignored"))
    assert result.ok
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "HELLO\n"


# --------------------------------------------------------------------------- #
# fail-safety, disable switch, summary
# --------------------------------------------------------------------------- #


def test_plugins_disabled_never_touches_entry_points(tmp_path):
    calls: list[str] = []

    def entry_points_fn(*, group):
        calls.append(group)
        return []

    settings = Settings.model_validate({"plugins": {"enabled": False}})
    lp = load_plugins(settings, tmp_path, entry_points_fn=entry_points_fn)
    assert lp.tools == [] and lp.commands == [] and lp.probes == []
    assert lp.provider_factories == {} and lp.edit_formats == {} and lp.skipped == []
    assert calls == []


def test_broken_discovery_backend_never_crashes(tmp_path):
    def entry_points_fn(*, group):
        raise RuntimeError("metadata backend exploded")

    lp = load_plugins(Settings(), tmp_path, entry_points_fn=entry_points_fn)
    assert lp.tools == [] and lp.provider_factories == {}
    assert len(lp.skipped) == 5  # one skip per group, boot survives
    assert all("discovery failed" in s.reason for s in lp.skipped)


def test_summary_counts_and_empty(fake_module, tmp_path):
    assert LoadedPlugins().summary() == "none loaded"
    fake_module.one = lambda s, w: _named_tool("one_tool")
    lp = load_plugins(
        Settings(), tmp_path, entry_points_fn=_fn(_ep("one", "one", GROUP_TOOLS))
    )
    assert lp.summary() == "1 tool"


# --------------------------------------------------------------------------- #
# the safety gate is NOT extensible: PLAN denies a plugin WRITE by execution
# --------------------------------------------------------------------------- #


def test_plan_mode_denies_plugin_write_tool_by_execution(fake_module, tmp_path):
    executed: list[int] = []

    class PluginWrite(Tool):
        name = "plugin_write"
        description = "writes something. Example: plugin_write()"
        risk = ToolRisk.WRITE
        parameters = {"type": "object", "properties": {}, "required": []}

        async def run(self, **kwargs):
            executed.append(1)
            return ToolResult(ok=True, output="wrote")

    fake_module.w = lambda s, w: PluginWrite()
    lp = load_plugins(Settings(), tmp_path, entry_points_fn=_fn(_ep("w", "w", GROUP_TOOLS)))
    settings = Settings()
    tools = build_tools(settings, tmp_path, plugins=lp)
    profile = CapabilityProfile(
        model_id="mock", honest_context=8192, tool_protocols={"native": 1.0}
    )
    script = [
        _text("", [ToolCall(id="c1", name="plugin_write", arguments={})]),
        _text("understood, proposing only"),
    ]
    engine = TurnEngine(
        MockProvider(script), tools, settings, profile, Mode.PLAN,
        workspace=tmp_path, snapshots=None,
    )

    events: list = []

    async def _run():
        async for ev in engine.run_turn("use the plugin"):
            events.append(ev)

    asyncio.run(_run())
    requested = [e for e in events if isinstance(e, ToolCallRequested)]
    assert requested and requested[0].decision == "deny"
    assert not [e for e in events if isinstance(e, ToolCallFinished)]
    assert executed == []  # decide(mode, risk) is the ONLY gate — and it held
