"""IC-705: the diff viewer — pure renderer unit tests + one modal Pilot test.

The pure ``diff_to_text`` / ``looks_like_diff`` transforms are asserted directly
(they own the coloring contract); one headless Pilot test proves an edit
approval actually shows the colored diff in the modal, and the modal falls back
to plain text for a non-diff (shell) preview. Async is driven with
``asyncio.run`` wrapping ``async with app.run_test()`` (the shell-test pattern).
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.widgets import Static

from ironcore.commands import build_default_registry as build_cmds
from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalBroker, ApprovalRequest
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools
from ironcore.tui.app import IronCoreApp
from ironcore.tui.screens.approval import ApprovalScreen
from ironcore.tui.widgets.diffview import DiffView, diff_to_text, looks_like_diff

UNIFIED = "@@ -1,2 +1,2 @@\n context line\n-old line\n+new line\n"
SEARCH_REPLACE = "<<<<<<< SEARCH\nold body\n=======\nnew body\n>>>>>>> REPLACE"


# --------------------------------------------------------------------------- #
# builders (mirrors tests/tui/test_app.py)
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _call(name: str, args: dict, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _engine(tmp_path, script, *, broker: ApprovalBroker | None = None) -> TurnEngine:
    settings = Settings.model_validate({})
    tools = build_tools(settings, tmp_path)
    return TurnEngine(
        MockProvider(list(script)),
        tools,
        settings,
        _profile(),
        Mode.MANUAL,
        workspace=tmp_path,
        approvals=broker,
        snapshots=None,
    )


def _app(engine: TurnEngine) -> IronCoreApp:
    return IronCoreApp(engine, build_cmds(), engine.settings)


async def _submit(app: IronCoreApp, pilot, text: str) -> None:
    from ironcore.tui.widgets import InputBar

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


def _styled_slices(text: Text, style: str) -> list[str]:
    """Substrings of ``text`` carried by a span whose style equals ``style``."""
    return [text.plain[s.start : s.end] for s in text.spans if s.style == style]


# --------------------------------------------------------------------------- #
# (1) pure renderer: unified diff coloring
# --------------------------------------------------------------------------- #


def test_unified_diff_colors_added_and_removed():
    text = diff_to_text(UNIFIED)
    # a green span covers the added line, a red span covers the removed one
    greens = _styled_slices(text, "green")
    reds = _styled_slices(text, "red")
    assert any(s.startswith("+new line") for s in greens)
    assert any(s.startswith("-old line") for s in reds)
    # the hunk header is styled distinctly (not green/red)
    assert any(s.startswith("@@") for s in _styled_slices(text, "bold cyan"))
    # wide content scrolls rather than wrap-breaks
    assert text.no_wrap is True


def test_search_replace_colors_marker_blocks():
    text = diff_to_text(SEARCH_REPLACE)
    # the search body is removed-red, the replace body is added-green
    assert any("old body" in s for s in _styled_slices(text, "red"))
    assert any("new body" in s for s in _styled_slices(text, "green"))
    # the markers are highlighted
    markers = "".join(_styled_slices(text, "bold yellow"))
    assert "SEARCH" in markers and "REPLACE" in markers


def test_diff_to_text_truncates_with_note():
    payload = "\n".join(f"+line {i}" for i in range(50))
    text = diff_to_text(payload, max_lines=10)
    assert "more line(s)" in text.plain
    # only the capped number of body lines were rendered
    assert text.plain.count("+line") == 10


def test_diff_to_text_never_crashes_on_plain_text():
    text = diff_to_text("write_file notes.txt (12 bytes)")
    assert isinstance(text, Text)
    assert text.plain == "write_file notes.txt (12 bytes)"


# --------------------------------------------------------------------------- #
# (2) looks_like_diff shape check (drives the modal fallback)
# --------------------------------------------------------------------------- #


def test_looks_like_diff_recognizes_diffs_and_rejects_commands():
    assert looks_like_diff(UNIFIED)
    assert looks_like_diff(SEARCH_REPLACE)
    assert looks_like_diff("-removed\n+added")
    # non-diff previews fall through to plain text
    assert not looks_like_diff("$ pytest -q")
    assert not looks_like_diff("GET https://example.com")
    assert not looks_like_diff("write_file notes.txt (12 bytes)")
    assert not looks_like_diff("")


# --------------------------------------------------------------------------- #
# (3) modal wiring: DiffView for write/edit, Static for a shell command
# --------------------------------------------------------------------------- #


def test_preview_widget_routes_edit_to_diffview():
    req = ApprovalRequest(
        id="a1", preview=f"edit_file app.py [unified_diff]\n{UNIFIED}", risk="write", turn=0
    )
    widget = ApprovalScreen(req)._preview_widget()
    assert isinstance(widget, DiffView)


def test_preview_widget_falls_back_to_static_for_shell():
    req = ApprovalRequest(id="a2", preview="$ rm -rf build", risk="exec", turn=0)
    widget = ApprovalScreen(req)._preview_widget()
    assert isinstance(widget, Static) and not isinstance(widget, DiffView)


# --------------------------------------------------------------------------- #
# (4) Pilot: an edit approval turn shows the colored diff in the modal
# --------------------------------------------------------------------------- #


def test_edit_approval_modal_shows_colored_diff(tmp_path):
    (tmp_path / "f.txt").write_text("context line\nold line\n", encoding="utf-8")
    edit_args = {"path": "f.txt", "format": "unified_diff", "edit": UNIFIED}
    script = [
        _text("", [_call("edit_file", edit_args)]),
        _text("left it alone"),
    ]
    broker = ApprovalBroker(timeout=5.0)
    app = _app(_engine(tmp_path, script, broker=broker))

    async def scenario():
        async with app.run_test() as pilot:
            await _submit(app, pilot, "edit the file")
            shown = await _wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            assert shown, "approval modal was not pushed"
            view = app.screen.query_one(DiffView)  # a diff view, not a plain Static
            text = view.diff_text()
            assert any(s.startswith("+new line") for s in _styled_slices(text, "green"))
            assert any(s.startswith("-old line") for s in _styled_slices(text, "red"))
            await pilot.press("n")  # deny → let the turn finish cleanly
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
