"""IronCoreApp (IC-701..704): headless Textual Pilot tests.

Every case drives the real app with a scripted ``MockProvider``-backed engine
(or a tiny slow provider) on a tmp workspace — zero network, zero model. Async
is driven with ``asyncio.run`` wrapping ``async with app.run_test()`` (Textual's
Pilot is headless); there is no pytest-asyncio dependency, matching the rest of
the suite (tests/test_engine.py). Keys go through ``pilot.press``; input is set
by assigning ``Input.value`` then submitting with Enter.

Headless-testing patterns adopted (documented for IC-705/706):
* A turn runs in a Textual worker; ``app.workers.wait_for_complete()`` awaits a
  turn that finishes on its own, and ``_wait_for`` polls ``pilot.pause`` for a
  condition (e.g. the approval modal appearing, streamed text landing) when the
  turn blocks on the UI.
* The approval modal is driven by the broker's ``on_request`` callback the app
  installs at mount; pressing y/n/a dismisses it into an ``ApprovalAnswer``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ironcore.commands import build_default_registry as build_cmds
from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalBroker
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, Provider, StreamEvent, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import CYCLE, Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tui.app import IronCoreApp, match_commands
from ironcore.tui.screens.approval import ApprovalScreen
from ironcore.tui.widgets import InputBar, StatusBar, Transcript

# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _call(name: str, args: dict, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _engine(
    tmp_path,
    script,
    *,
    mode: Mode = Mode.MANUAL,
    network: bool = False,
    broker: ApprovalBroker | None = None,
) -> TurnEngine:
    settings = Settings.model_validate({"safety": {"network_tools": network}})
    tools = build_tools(settings, tmp_path)
    return TurnEngine(
        MockProvider(list(script)),
        tools,
        settings,
        _profile(),
        mode,
        workspace=tmp_path,
        approvals=broker,
        snapshots=None,
    )


def _app(engine: TurnEngine) -> IronCoreApp:
    return IronCoreApp(engine, build_cmds(), engine.settings)


# --------------------------------------------------------------------------- #
# pilot helpers
# --------------------------------------------------------------------------- #


async def _submit(app: IronCoreApp, pilot, text: str) -> None:
    inp = app.query_one(InputBar)
    inp.value = text
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for(pilot, predicate, tries: int = 120) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause()
    return False


# --------------------------------------------------------------------------- #
# (1) boot
# --------------------------------------------------------------------------- #


def test_app_boots_three_regions(tmp_path):
    app = _app(_engine(tmp_path, [_text("hi")]))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one(Transcript) is not None
            assert app.query_one(InputBar) is not None
            assert app.query_one(StatusBar) is not None
            # status bar carries the mode chip + model name
            assert "[MANUAL]" in app.status_bar._plain
            assert "qwen3-coder:30b" in app.status_bar._plain

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (2) streamed text turn
# --------------------------------------------------------------------------- #


def test_text_turn_streams_into_transcript(tmp_path):
    app = _app(_engine(tmp_path, [_text("Hello from the model.")]))

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "hi there")
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = app.transcript_text()
            assert "hi there" in text  # the user message
            assert "Hello from the model." in text  # streamed assistant text

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (3) tool card reaches a finished state
# --------------------------------------------------------------------------- #


def test_tool_turn_renders_card_to_finished(tmp_path):
    (tmp_path / "hello.txt").write_text("data", encoding="utf-8")
    script = [_text("", [_call("read_file", {"path": "hello.txt"})]), _text("done")]
    app = _app(_engine(tmp_path, script))  # READ auto-allows in MANUAL

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "read it")
            await app.workers.wait_for_complete()
            await pilot.pause()
            card = app.transcript.card("c1")
            assert card is not None
            assert card.state == "done"  # executed, ok result collapsed on the card
            assert "read_file" in app.transcript_text()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (4) Shift+Tab cycles the mode chip through all four modes
# --------------------------------------------------------------------------- #


def test_shift_tab_cycles_all_modes(tmp_path):
    app = _app(_engine(tmp_path, [_text("hi")]))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.engine.mode is Mode.MANUAL
            seen = [app.engine.mode]
            for _ in range(4):
                await pilot.press("shift+tab")
                await pilot.pause()
                seen.append(app.engine.mode)
                # the status chip tracks the engine mode
                assert f"[{app.engine.mode.value.upper()}]" in app.status_bar._plain
            # visited every mode, in CYCLE order, wrapping back to MANUAL
            assert seen[1:] == [CYCLE[1], CYCLE[2], CYCLE[3], CYCLE[0]]
            assert set(seen) == set(CYCLE)
            # mode changes are announced in the transcript
            assert "accept-edits" in app.transcript_text()
            assert "plan" in app.transcript_text()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (5) ASK-gated write: modal shown, deny resolves, tool NOT executed
# --------------------------------------------------------------------------- #


def test_ask_write_modal_deny_blocks_execution(tmp_path):
    target = tmp_path / "new.txt"
    script = [
        _text("", [_call("write_file", {"path": "new.txt", "content": "x"})]),
        _text("understood, leaving it"),
    ]
    broker = ApprovalBroker(timeout=5.0)  # bounded so a bug can't hang the suite
    app = _app(_engine(tmp_path, script, broker=broker))  # WRITE asks in MANUAL

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "write a file")
            shown = await _wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            assert shown, "approval modal was not pushed"
            # the modal shows the exact effect, not a paraphrase (SAFETY §4)
            assert "write_file" in app.screen.request.preview
            await pilot.press("n")  # deny
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not target.exists()  # tool never executed
            card = app.transcript.card("c1")
            assert card is not None and card.state == "denied"
            # the turn continued after the denial
            assert "understood, leaving it" in app.transcript_text()

    asyncio.run(scenario())


def test_ask_write_modal_approve_executes(tmp_path):
    target = tmp_path / "made.txt"
    script = [
        _text("", [_call("write_file", {"path": "made.txt", "content": "hi"})]),
        _text("done"),
    ]
    broker = ApprovalBroker(timeout=5.0)
    app = _app(_engine(tmp_path, script, broker=broker))

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "write it")
            shown = await _wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            assert shown
            await pilot.press("y")  # approve once
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert target.exists()  # tool executed after approval
            card = app.transcript.card("c1")
            assert card is not None and card.state == "done"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (6) slash command dispatched from the input shows output
# --------------------------------------------------------------------------- #


def test_slash_help_dispatches_to_transcript(tmp_path):
    app = _app(_engine(tmp_path, [_text("hi")]))

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/help")
            await _wait_for(pilot, lambda: "Commands:" in app.transcript_text())
            text = app.transcript_text()
            assert "Commands:" in text
            assert "/version" in text
            assert "/probe" in text  # every command is live in v0.1

    asyncio.run(scenario())


def test_unknown_command_suggests_nearest(tmp_path):
    app = _app(_engine(tmp_path, [_text("hi")]))

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "/versoin")  # typo, no args
            await _wait_for(pilot, lambda: "Unknown command" in app.transcript_text())
            text = app.transcript_text()
            assert "Unknown command" in text
            assert "version" in text  # nearest match offered

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (6b) slash palette lists commands and Tab completes
# --------------------------------------------------------------------------- #


def test_slash_palette_lists_and_tab_completes(tmp_path):
    app = _app(_engine(tmp_path, [_text("hi")]))

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one(InputBar)
            inp.value = "/"
            await pilot.pause()
            palette = app.query_one("#palette")
            assert palette.display is True
            assert any(c.name == "help" for c in app._matches)
            expected = app._matches[0].name  # capture before completion clears it
            # Tab completes to the top match
            await pilot.press("tab")
            await pilot.pause()
            assert inp.value == f"/{expected} "
            # completing to a full name (with trailing space) hides the palette
            assert palette.display is False

    asyncio.run(scenario())


def test_match_commands_prefix_first():
    registry = build_cmds()
    matches = match_commands(registry, "he")
    assert matches and matches[0].name == "help"
    # the palette lists the full command set (all implemented in v0.1)
    everything = match_commands(registry, "")
    assert everything and all(c.implemented for c in everything)
    assert {"probe", "envelope", "workflow"} <= {c.name for c in everything}


# --------------------------------------------------------------------------- #
# (7) Esc interrupts a running turn without crashing
# --------------------------------------------------------------------------- #


class _SlowProvider(Provider):
    """Streams one chunk then blocks, so a turn stays RUNNING to be interrupted."""

    name = "slow"

    async def complete(self, messages, *, tools=None, sampling=None):  # pragma: no cover
        raise NotImplementedError

    async def stream(
        self, messages, *, tools=None, sampling=None, response_format=None, extra_body=None
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(kind="text", text="Thinking about it")
        await asyncio.sleep(30)  # long enough to be cancelled first
        yield StreamEvent(kind="done", data={})

    async def list_models(self):
        return ["slow"]


def test_esc_interrupts_running_turn(tmp_path):
    settings = Settings.model_validate({})
    tools = build_tools(settings, tmp_path)
    engine = TurnEngine(
        _SlowProvider(), tools, settings, _profile(), Mode.MANUAL,
        workspace=tmp_path, snapshots=None,
    )
    app = IronCoreApp(engine, build_cmds(), settings)

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "do something slow")
            streamed = await _wait_for(
                pilot, lambda: "Thinking about it" in app.transcript_text()
            )
            assert streamed, "partial output never streamed"
            await pilot.press("escape")
            await pilot.pause()
            interrupted = await _wait_for(
                pilot, lambda: "[interrupted]" in app.transcript_text()
            )
            assert interrupted
            # partial output preserved, app still alive & responsive
            assert "Thinking about it" in app.transcript_text()
            assert not app._turn_running()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (8) instant-on molding: SEED hot-swaps first, DEEP PROBE refines after
# --------------------------------------------------------------------------- #


def test_instant_seed_then_probe_hot_swaps_in_order(tmp_path, monkeypatch):
    """With instant_seed + auto_probe on, mount SEEDs the profile from endpoint
    introspection (hot-swap #1) and THEN deep-probes to measure it (hot-swap #2),
    in that order. Both async calls are stubbed so the test is offline + fast."""
    engine = _engine(tmp_path, [_text("hi")])
    engine.provider.base_url = "http://localhost:11434/v1"  # endpoint-backed → seed runs

    seed = CapabilityProfile(
        model_id="mock",
        source="seeded",
        honest_context=32768,
        tool_protocols={"native": 0.95},
        edit_formats={"search_replace": 0.85},
    )
    measured = CapabilityProfile(
        model_id="mock",
        source="probed",
        probed_at="2026-07-16T00:00:00Z",
        honest_context=16384,
        tool_protocols={"native": 1.0},
    )
    order: list[str] = []
    probe_saw: list[CapabilityProfile] = []

    async def fake_seed(provider, *, model_id, transport=None):
        order.append("seed")
        return seed

    async def fake_probe(eng, *, envelope_dir=None):  # MS-2: additive kwarg threaded
        order.append("probe")
        probe_saw.append(eng.profile)  # the seed must already be hot-swapped in
        eng.profile = measured
        return "Probe complete — mock profile updated."

    monkeypatch.setattr("ironcore.envelope.seed.seed_profile", fake_seed)
    monkeypatch.setattr("ironcore.commands.envelopecmd.probe_and_swap", fake_probe)

    app = IronCoreApp(engine, build_cmds(), engine.settings, instant_seed=True, auto_probe=True)

    async def scenario():
        async with app.run_test() as pilot:
            done = await _wait_for(pilot, lambda: app.engine.profile is measured)
            assert done, "profile never hot-swapped to the measured one"
            await app.workers.wait_for_complete()
            await pilot.pause()
            # order matters: the seed ran and hot-swapped BEFORE the deep probe
            assert order == ["seed", "probe"]
            assert probe_saw == [seed]  # hot-swap #1 was in place when the probe ran
            text = app.transcript_text()
            assert "Seeded" in text and "32768" in text  # seed note (provisional)
            assert "native" in text and "search_replace" in text
            assert "Probe complete" in text  # deep-probe report (measured)

    asyncio.run(scenario())


def test_flags_off_neither_seed_nor_probe_fires(tmp_path, monkeypatch):
    """The default injected-engine path (both flags off) must not seed or probe —
    this is what keeps every other TUI test from touching the network."""
    engine = _engine(tmp_path, [_text("hi")])
    engine.provider.base_url = "http://localhost:11434/v1"  # even so, nothing should fire
    fired: list[str] = []

    async def fake_seed(provider, *, model_id, transport=None):
        fired.append("seed")
        return _profile()

    async def fake_probe(eng, *, envelope_dir=None):  # MS-2: additive kwarg threaded
        fired.append("probe")
        return "probed"

    monkeypatch.setattr("ironcore.envelope.seed.seed_profile", fake_seed)
    monkeypatch.setattr("ironcore.commands.envelopecmd.probe_and_swap", fake_probe)

    app = _app(engine)  # defaults: instant_seed=False, auto_probe=False

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert fired == []
            assert "Seeded" not in app.transcript_text()
            assert "measuring it" not in app.transcript_text()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (10) live model swap updates the status bar (MS-2)
# --------------------------------------------------------------------------- #


def test_model_swap_updates_status_bar_and_repoints(tmp_path):
    """/model <name> with a live registry: the engine is re-pointed (cache-hit
    profile from the injected envelope_dir — no background work) and the status
    bar re-reads the shared settings after dispatch."""
    env = tmp_path / "env"
    CapabilityProfile(
        model_id="other:7b",
        probed_at="2026-07-16T00:00:00Z",
        source="probed",
        tool_protocols={"native": 1.0},
    ).save(env)
    engine = _engine(tmp_path, [_text("hi")])
    swapped = MockProvider([])

    class StubRegistry:
        def for_model(self, model):
            return swapped

    app = IronCoreApp(
        engine,
        build_cmds(),
        engine.settings,
        provider_registry=StubRegistry(),
        envelope_dir=env,
    )

    async def scenario():
        async with app.run_test() as pilot:
            assert "qwen3-coder:30b" in app.status_bar._plain  # the boot model
            await _submit(app, pilot, "/model other:7b")
            done = await _wait_for(pilot, lambda: "other:7b" in app.status_bar._plain)
            assert done, "status bar never showed the new model"
            assert app.settings.provider.model == "other:7b"
            assert engine.provider is swapped  # the live session was re-pointed
            assert engine.profile.probed_at is not None  # measured cache hit
            await _wait_for(pilot, lambda: "cache" in app.transcript_text())
            assert "cache" in app.transcript_text()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (11) boot-time envelope tuning (MS-8)
# --------------------------------------------------------------------------- #


def _seed_tuning_evidence(env, model: str) -> None:
    """A measured cached profile + a threshold-crossing outcome ledger for it."""
    from ironcore.envelope.outcomes import Counter, OutcomeLedger, generation_stamp

    profile = CapabilityProfile(
        model_id=model,
        probed_at="2026-07-16T00:00:00Z",
        source="probed",
        tool_protocols={"native": 1.0, "strict_json": 0.95},
    )
    profile.save(env)
    ledger = OutcomeLedger(model_id=model, profile_stamp=generation_stamp(profile))
    ledger.tool_protocols["native"] = Counter(attempts=20, failures=4)  # live rate 0.8
    ledger.save(env)


def test_from_settings_applies_outcome_tuning_at_boot(tmp_path, monkeypatch):
    """MS-8: a threshold-crossing ledger next to the envelope cache downgrades
    the boot profile (source='tuned', ladder flipped by the frozen recommended_*)
    and wires the ledger + boot notes into the app."""
    env = tmp_path / "env"
    monkeypatch.setattr("ironcore.tui.app.default_envelope_dir", lambda: env)
    settings = Settings()
    _seed_tuning_evidence(env, settings.provider.model)
    app = IronCoreApp.from_settings(settings, tmp_path)
    assert app.engine.profile.source == "tuned"
    assert app.engine.profile.tool_protocols["native"] == 0.8  # lowered, never raised
    assert app.engine.profile.recommended_tool_protocol() == "strict_json"
    assert app.engine.outcomes is not None
    assert app.engine.outcomes.model_id == settings.provider.model
    assert app._boot_notes and any("tuned" in n.lower() for n in app._boot_notes)
    # the CACHED profile keeps the honest measurement — tuning is never persisted
    on_disk = CapabilityProfile.load(env, settings.provider.model)
    assert on_disk is not None and on_disk.source == "probed"
    assert on_disk.tool_protocols["native"] == 1.0


def test_from_settings_auto_tune_off_wires_nothing(tmp_path, monkeypatch):
    env = tmp_path / "env"
    monkeypatch.setattr("ironcore.tui.app.default_envelope_dir", lambda: env)
    settings = Settings.model_validate({"envelope": {"auto_tune": False}})
    _seed_tuning_evidence(env, settings.provider.model)
    app = IronCoreApp.from_settings(settings, tmp_path)
    assert app.engine.profile.source == "probed"  # the raw cache, untouched
    assert app.engine.profile.recommended_tool_protocol() == "native"
    assert app.engine.outcomes is None  # recording disabled with tuning
    assert app._boot_notes == ()


def test_from_settings_without_evidence_boots_unchanged(tmp_path, monkeypatch):
    env = tmp_path / "env"
    monkeypatch.setattr("ironcore.tui.app.default_envelope_dir", lambda: env)
    app = IronCoreApp.from_settings(Settings(), tmp_path)
    assert app.engine.profile.source == "default"  # no cache, no ledger: floor boot
    assert app.engine.outcomes is not None  # ... but recording is armed
    assert app._boot_notes == ()


# --------------------------------------------------------------------------- #
# MCP tool servers (MS-7): from_settings gating + mount worker + unmount close
# --------------------------------------------------------------------------- #


def test_from_settings_mcp_requires_network_tools(tmp_path, monkeypatch):
    """MS-7: with servers configured but NET tools off, no manager is built
    (servers are not even spawned) and one boot note says why; enabling
    safety.network_tools wires the manager."""
    monkeypatch.setattr("ironcore.tui.app.default_envelope_dir", lambda: tmp_path / "env")
    servers = {"mcp": {"servers": {"gh": {"command": "npx.cmd"}}}}

    off = IronCoreApp.from_settings(Settings.model_validate(servers), tmp_path)
    assert off._mcp_manager is None
    assert any("[mcp]" in n and "network_tools" in n for n in off._boot_notes)

    on = IronCoreApp.from_settings(
        Settings.model_validate({**servers, "safety": {"network_tools": True}}), tmp_path
    )
    assert on._mcp_manager is not None
    assert [c.server for c in on._mcp_manager.clients] == ["gh"]
    assert not any("[mcp]" in n for n in on._boot_notes)


def test_t8_clamp_reaches_the_boot_notes(tmp_path, monkeypatch):
    """FIX-3: a clamp the user never sees is a silent downgrade. Booting a
    workspace whose committed .ironcore/config.toml asks for AUTO must land in
    MANUAL *and* say so in the transcript's boot notes."""
    from pathlib import Path as _Path

    monkeypatch.setattr("ironcore.tui.app.default_envelope_dir", lambda: tmp_path / "env")
    home = tmp_path / "home"  # hermetic: never read the developer's real user config
    home.mkdir()
    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("IRONCORE_MODE", raising=False)

    ws = tmp_path / "cloned-repo"
    (ws / ".ironcore").mkdir(parents=True)
    (ws / ".ironcore" / "config.toml").write_text(
        '[safety]\nmode = "auto"\nnetwork_tools = true\n', encoding="utf-8"
    )

    app = IronCoreApp.from_settings(None, ws)
    assert app.settings.safety.mode == "manual"
    assert app.settings.safety.network_tools is False
    assert any("[safety]" in n and "clamped" in n for n in app._boot_notes)


def test_mcp_worker_registers_tools_and_unmount_closes(tmp_path):
    """MS-7: on_mount's background worker registers MCP tools into the LIVE
    registry (note posted to the transcript); leaving the app closes servers."""
    from ironcore.tools.mcp import MCPManager

    class FakeMCPClient:
        server = "fake"
        closed = False

        async def list_tools(self):
            return [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object"}}]

        async def call_tool(self, name, arguments):  # pragma: no cover — not driven here
            return {"content": []}

        async def aclose(self):
            self.closed = True

    client = FakeMCPClient()
    engine = _engine(tmp_path, [_text("hi")])
    app = IronCoreApp(
        engine, build_cmds(), engine.settings, mcp_manager=MCPManager([client])
    )

    async def scenario():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert engine.tools.get("mcp__fake__echo") is not None  # live registry grew
            assert "1 tool(s) registered" in app.transcript_text()

    asyncio.run(scenario())
    assert client.closed  # on_unmount closed the server subprocess
