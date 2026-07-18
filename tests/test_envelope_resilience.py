"""FIX-1 regressions: the envelope cache must never brick a boot.

The envelope cache is the one piece of state a stranger's first launch writes
automatically. Before this package it was written non-atomically (a truncated
JSON survived a quit mid-write) with a loader that RAISED on it — so an
interrupted first-run probe bricked ``ironcore`` permanently, and ``doctor``
crashed at the same line before reaching any diagnostic.

Every case here is offline: no provider, no network, no model. The two TUI
cases drive the real app headlessly with a ``MockProvider``-backed engine,
matching tests/tui/test_app.py.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from ironcore.commands import build_default_registry as build_cmds
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.outcomes import OutcomeLedger
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tui.app import IronCoreApp
from ironcore.tui.widgets import StatusBar

# --------------------------------------------------------------------------- #
# builders (mirrors tests/tui/test_app.py — offline engine, no network)
# --------------------------------------------------------------------------- #


def _engine(tmp_path: Path) -> TurnEngine:
    settings = Settings.model_validate({"safety": {"network_tools": False}})
    tools = build_tools(settings, tmp_path)
    profile = CapabilityProfile(model_id="mock", tool_protocols={"native": 1.0})
    return TurnEngine(
        MockProvider([CompletionResult(message=Message(role="assistant", content="hi"))]),
        tools,
        settings,
        profile,
        Mode.MANUAL,
        workspace=tmp_path,
        snapshots=None,
    )


def _app(tmp_path: Path, **kwargs) -> IronCoreApp:
    engine = _engine(tmp_path)
    return IronCoreApp(engine, build_cmds(), engine.settings, **kwargs)


def _truncate(path: Path) -> None:
    """Simulate a quit mid-write: half a JSON document on disk."""
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw[: len(raw) // 2], encoding="utf-8")


# --------------------------------------------------------------------------- #
# (1) tolerant load — a corrupt cache reads as "unprobed", never raises
# --------------------------------------------------------------------------- #


def test_truncated_profile_loads_as_none_not_raises(tmp_path):
    """The original blocker: json.loads on a half-written cache raised
    ValueError out of load(), bricking every boot AND `ironcore doctor`."""
    saved = CapabilityProfile(model_id="mock").save(tmp_path)
    _truncate(saved)

    assert CapabilityProfile.load(tmp_path, "mock") is None


@pytest.mark.parametrize(
    "payload",
    [
        "",  # empty file (interrupted before any bytes landed)
        "{",  # truncated object
        "null",  # valid JSON, not an object
        '{"model_id": 17}',  # valid JSON, schema violation -> pydantic ValidationError
        '["not", "a", "profile"]',
    ],
)
def test_corrupt_profile_payloads_all_load_as_none(tmp_path, payload):
    path = tmp_path / f"{CapabilityProfile.slug('mock')}.json"
    path.write_text(payload, encoding="utf-8")

    assert CapabilityProfile.load(tmp_path, "mock") is None


def test_corrupt_profile_is_quarantined_and_reprobes_cleanly(tmp_path):
    """A bad cache is renamed aside (so the path is nameable in a boot note)
    and the slot is free for the next save — i.e. the model re-probes."""
    saved = CapabilityProfile(model_id="mock").save(tmp_path)
    _truncate(saved)

    assert CapabilityProfile.load(tmp_path, "mock") is None
    quarantined = tmp_path / f"{CapabilityProfile.slug('mock')}.json.corrupt"
    assert quarantined.exists(), "corrupt cache must be renamed aside, not silently deleted"
    assert not saved.exists(), "the live cache path must be cleared for a re-probe"

    # and the slot genuinely re-probes: a fresh save round-trips.
    CapabilityProfile(model_id="mock", probed_at="2026-07-18T00:00:00", source="probed").save(
        tmp_path
    )
    reloaded = CapabilityProfile.load(tmp_path, "mock")
    assert reloaded is not None and reloaded.source == "probed"


def test_quarantine_failure_still_loads_as_none(tmp_path, monkeypatch):
    """Even if the rename itself fails (locked file on Windows, read-only dir),
    load must still return None rather than propagate."""
    saved = CapabilityProfile(model_id="mock").save(tmp_path)
    _truncate(saved)

    def _boom(*_args, **_kwargs):
        raise OSError("rename denied")

    monkeypatch.setattr(os, "replace", _boom)
    assert CapabilityProfile.load(tmp_path, "mock") is None


def test_load_reports_corruption_when_asked(tmp_path):
    """`load_with_note` is the additive surface the boot note uses: it names
    the quarantined path so the user knows what happened."""
    saved = CapabilityProfile(model_id="mock").save(tmp_path)
    _truncate(saved)

    profile, note = CapabilityProfile.load_with_note(tmp_path, "mock")
    assert profile is None
    assert note is not None and ".json.corrupt" in note

    # the clean path stays quiet
    CapabilityProfile(model_id="mock").save(tmp_path)
    profile, note = CapabilityProfile.load_with_note(tmp_path, "mock")
    assert profile is not None and note is None


def test_missing_cache_is_not_a_corruption(tmp_path):
    profile, note = CapabilityProfile.load_with_note(tmp_path, "never-seen")
    assert profile is None and note is None


# --------------------------------------------------------------------------- #
# (2) atomic save — an interrupted write never destroys a good cache
# --------------------------------------------------------------------------- #


def test_profile_save_is_atomic_interrupted_write_keeps_old_cache(tmp_path, monkeypatch):
    """The write-then-rename invariant: if the process dies at the moment of
    publication, the PREVIOUS good cache is still intact and loadable. With a
    plain write_text this truncated the file in place."""
    good = CapabilityProfile(model_id="mock", honest_context=8192, source="probed")
    path = good.save(tmp_path)
    before = path.read_text(encoding="utf-8")

    def _die(*_args, **_kwargs):
        raise KeyboardInterrupt("user quit during the first-run probe")

    monkeypatch.setattr(os, "replace", _die)
    with pytest.raises(KeyboardInterrupt):
        CapabilityProfile(model_id="mock", honest_context=1).save(tmp_path)

    assert path.read_text(encoding="utf-8") == before
    reloaded = CapabilityProfile.load(tmp_path, "mock")
    assert reloaded is not None and reloaded.honest_context == 8192


def test_profile_save_never_publishes_a_partial_file(tmp_path, monkeypatch):
    """At the instant of os.replace the source must already be complete —
    no observer can ever see a half-written cache at the live path."""
    seen: list[str] = []
    real_replace = os.replace

    def _spy(src, dst, *args, **kwargs):
        seen.append(Path(src).read_text(encoding="utf-8"))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _spy)
    CapabilityProfile(model_id="mock", source="probed").save(tmp_path)

    assert len(seen) == 1
    assert json.loads(seen[0])["source"] == "probed", "staged file must be complete JSON"


def test_profile_save_leaves_no_temp_droppings(tmp_path):
    for _ in range(3):
        CapabilityProfile(model_id="mock").save(tmp_path)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"{CapabilityProfile.slug('mock')}.json"]


def test_outcome_ledger_save_is_atomic(tmp_path, monkeypatch):
    """OutcomeLedger.save was the second non-atomic outlier."""
    ledger = OutcomeLedger(model_id="mock")
    path = ledger.save(tmp_path)
    assert path is not None
    before = path.read_text(encoding="utf-8")

    def _die(*_args, **_kwargs):
        raise KeyboardInterrupt("quit")

    monkeypatch.setattr(os, "replace", _die)
    with pytest.raises(KeyboardInterrupt):
        OutcomeLedger(model_id="mock").save(tmp_path)

    assert path.read_text(encoding="utf-8") == before
    assert OutcomeLedger.load(tmp_path, "mock").model_id == "mock"


def test_outcome_ledger_save_leaves_no_temp_droppings(tmp_path):
    OutcomeLedger(model_id="mock").save(tmp_path)
    OutcomeLedger(model_id="mock").save(tmp_path)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"{CapabilityProfile.slug('mock')}.outcomes.json"]


# --------------------------------------------------------------------------- #
# (3) boot never tracebacks — from_settings survives a corrupt cache
# --------------------------------------------------------------------------- #


def test_from_settings_boots_with_a_corrupt_cache(tmp_path, monkeypatch):
    """End to end: the bricked-forever scenario. A truncated cache must boot
    into a working app and SAY what happened, not raise."""
    envelope_dir = tmp_path / "envelopes"
    envelope_dir.mkdir(parents=True)
    settings = Settings.model_validate(
        {"provider": {"model": "mock"}, "envelope": {"auto_probe": False, "instant_seed": False}}
    )
    saved = CapabilityProfile(model_id="mock", source="probed").save(envelope_dir)
    _truncate(saved)

    monkeypatch.setattr("ironcore.tui.app.default_envelope_dir", lambda: envelope_dir)
    app = IronCoreApp.from_settings(settings=settings, workspace=tmp_path)

    assert app.engine.profile.model_id == "mock"
    assert app.engine.profile.probed_at is None, "a corrupt cache must read as unprobed"
    assert any(".json.corrupt" in note for note in app._boot_notes), app._boot_notes


def test_corrupt_ledger_sidecar_also_boots(tmp_path, monkeypatch):
    envelope_dir = tmp_path / "envelopes"
    envelope_dir.mkdir(parents=True)
    (envelope_dir / f"{CapabilityProfile.slug('mock')}.outcomes.json").write_text(
        "{not json", encoding="utf-8"
    )
    settings = Settings.model_validate(
        {"provider": {"model": "mock"}, "envelope": {"auto_probe": False, "instant_seed": False}}
    )

    monkeypatch.setattr("ironcore.tui.app.default_envelope_dir", lambda: envelope_dir)
    app = IronCoreApp.from_settings(settings=settings, workspace=tmp_path)
    assert app.engine.profile.model_id == "mock"


# --------------------------------------------------------------------------- #
# (4) StatusBar._running collided with Textual's MessagePump._running
# --------------------------------------------------------------------------- #


def test_status_bar_does_not_shadow_textual_is_running(tmp_path):
    """MessagePump.__init__ owns ``self._running`` and ``is_running`` returns
    it. The bar reusing that name meant set_running(False) reported a LIVE
    widget as not-running, which gates check_idle's Idle posting."""
    app = _app(tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = app.query_one(StatusBar)
            assert bar.is_running is True

            bar.set_running(True)
            assert bar.is_running is True
            bar.set_running(False)
            assert bar.is_running is True, "the bar must not write Textual's pump flag"

    asyncio.run(scenario())


def test_idle_status_bar_shows_no_working_marker_after_slash_command(tmp_path):
    """The visible half of the same bug: once the pump set _running=True, ANY
    re-render (slash dispatch calls set_model unconditionally) painted a
    spurious 'working…' while the app sat idle."""
    app = _app(tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = app.query_one(StatusBar)
            assert "working" not in bar._plain

            inp = app.query_one("InputBar")
            inp.value = "/help"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert "working" not in bar._plain, bar._plain

    asyncio.run(scenario())


def test_status_bar_still_shows_working_during_a_turn(tmp_path):
    """The rename must not cost the real signal."""
    bar = StatusBar(mode=Mode.MANUAL, model="mock")
    assert "working" not in bar._plain
    bar.set_running(True)
    assert "working…" in bar._plain
    bar.set_running(False)
    assert "working" not in bar._plain


def test_mode_cycle_does_not_paint_working(tmp_path):
    app = _app(tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = app.query_one(StatusBar)
            await pilot.press("shift+tab")
            await pilot.pause()
            assert "working" not in bar._plain, bar._plain

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (5) discoverability — the first-run note is honest, keys are always visible
# --------------------------------------------------------------------------- #


def test_first_run_note_states_cost_and_the_off_switch(tmp_path):
    """A stranger quitting mid-probe is WHY the cache got corrupted. The note
    must state the count, the duration and how to turn it off."""
    app = _app(tmp_path, auto_probe=True, instant_seed=False, envelope_dir=tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            text = app.transcript_text()
            assert "auto_probe = false" in text, text
            assert "80" in text and "min" in text, text

    asyncio.run(scenario())


def test_keybindings_are_always_visible_not_just_in_the_scrollback(tmp_path):
    """compose() yielded no Footer, so the only key hint scrolled away."""
    app = _app(tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            hint = app.query_one(StatusBar).keys_hint()
            assert "shift+tab" in hint and "esc" in hint
            assert "ctrl+c" in hint, "a stranger must always be told how to leave"

    asyncio.run(scenario())


def test_help_lists_the_keybindings(tmp_path):
    from ironcore.commands.base import CommandContext

    registry = build_cmds()
    ctx = CommandContext(settings=Settings(), mode=Mode.MANUAL, extra={"registry": registry})
    out = registry.get("help").handler(ctx, "")

    assert "Keys:" in out
    assert "shift+tab" in out and "ctrl+c" in out


def test_help_keys_stay_in_sync_with_the_app_bindings():
    """/help duplicates the key list as literals (commands/ must not import
    tui/). This is the guard that keeps the duplication honest."""
    from ironcore.commands.builtins import _KEYS

    bound = {b.key for b in IronCoreApp.BINDINGS}
    documented = {key for key, _ in _KEYS}
    assert bound <= documented, f"undocumented keybindings: {bound - documented}"
