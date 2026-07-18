"""Regenerate the README screenshots in ``docs/img/`` from real renders.

    uv run python tools/make_screenshots.py

Pipeline, per shot:

1. A real :class:`~ironcore.tui.app.IronCoreApp` is driven headlessly through
   Textual's ``App.run_test`` pilot with a scripted ``MockProvider``-backed
   ``TurnEngine`` — the same construction the TUI suite uses (tests/tui/test_app.py).
2. ``App.export_screenshot`` exports what the terminal ACTUALLY rendered as SVG.
3. Headless Edge/Chrome rasterizes that SVG to a 2x PNG; the SVG's ``viewBox``
   fixes the window size so no scaling guesswork is involved.

``ironcore doctor`` is a CLI, not a TUI: it is executed for real as a subprocess
and its captured stdout is replayed through a recording ``rich.Console`` (rich
ships with textual) into the same SVG -> PNG rasterizer. Only the ``$ ironcore
doctor`` prompt line is added as a label; every other character is the command's
own output.

Constraints this script must keep:

* No pixel may be authored by hand — everything is a render of the shipping UI.
* Hermetic: no network, no model, no user config. Each shot gets a fresh
  ``tempfile`` workspace, an injected envelope dir, and (for doctor) a fake HOME.
* ``docs/img/`` is the ONLY path written inside the repo; SVGs stay in the temp
  dir, which is removed on success.
* Lives outside the ``ironcore`` package, so it is absent from the built wheel
  (``[tool.hatch.build.targets.wheel] packages = ["ironcore"]``) and outside the
  coverage gate's measured surface.

Browser lookup order: ``$IRONCORE_SHOT_BROWSER``, then ``PATH``, then the
standard install locations. With no browser found the SVGs are LEFT on disk and
their location is printed, so a maintainer can convert them another way.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ironcore.commands import build_default_registry as build_command_registry
from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalBroker
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.memory.sessions import SessionStore
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tool_registry
from ironcore.tui.app import RESUME_PICK, IronCoreApp
from ironcore.tui.screens.approval import ApprovalScreen
from ironcore.tui.widgets import InputBar

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "docs" / "img"

#: One terminal geometry for every TUI shot — a consistent frame across the
#: README reads as one product, not eight screenshots.
TERM_SIZE = (100, 30)
#: Console width for the doctor capture; matches TERM_SIZE's width so the CLI
#: shot sits at the same page width as the TUI shots.
DOCTOR_WIDTH = 100
#: Rasterize at 2x so the PNGs stay sharp on high-density displays.
SCALE = 2

BROWSER_ENV = "IRONCORE_SHOT_BROWSER"
#: Standard Chromium install locations, checked after $IRONCORE_SHOT_BROWSER and PATH.
BROWSER_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
BROWSER_NAMES = ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium")

# --------------------------------------------------------------------------- #
# engine / app builders (mirrors tests/tui/test_app.py)
# --------------------------------------------------------------------------- #


def _measured_profile() -> CapabilityProfile:
    """A profile shaped like a real probe run of a local 30B coder model.

    Values are the ones the probe battery writes (see envelope/runner.py's
    dotted-path merge); the ladders below are computed by the shipping
    ``recommended_*`` functions, never hardcoded here.
    """
    return CapabilityProfile(
        model_id="qwen3-coder:30b",
        source="probed",
        probed_at="2026-07-16T09:41:07+00:00",
        context_window=262144,
        honest_context=49152,
        chars_per_token=3.6,
        vision=False,
        tool_protocols={"native": 0.98, "strict_json": 0.94, "text_protocol": 1.0},
        edit_formats={"unified_diff": 0.71, "search_replace": 0.93, "whole_file": 1.0},
        json_adherence=0.96,
        instruction_retention=0.88,
        coherence_horizon=9,
    )


def _floor_profile() -> CapabilityProfile:
    """The unprobed boot profile used by shots that are not about the envelope."""
    return CapabilityProfile(
        model_id="qwen3-coder:30b", honest_context=32768, tool_protocols={"native": 1.0}
    )


def _text(
    content: str, calls: Sequence[ToolCall] = (), *, tokens: int = 0
) -> CompletionResult:
    usage = {"total_tokens": tokens} if tokens else {}
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=list(calls)),
        usage=usage,
    )


def _call(name: str, args: dict, cid: str) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _build_app(
    workspace: Path,
    script: Sequence[object] = (),
    *,
    mode: Mode = Mode.MANUAL,
    profile: CapabilityProfile | None = None,
    broker: ApprovalBroker | None = None,
    session_store: SessionStore | None = None,
    resume_id: str | None = None,
) -> IronCoreApp:
    """A real app over a real engine, with the model and the disk swapped out.

    ``snapshots=None`` keeps the workspace free of an ``.ironcore`` sidecar and
    ``envelope_dir`` is pinned inside the temp tree, so nothing under the user's
    home is read or written.
    """
    settings = Settings.model_validate({"safety": {"network_tools": False}})
    settings.provider.model = "qwen3-coder:30b"
    engine = TurnEngine(
        MockProvider(list(script)),
        build_tool_registry(settings, workspace),
        settings,
        profile if profile is not None else _floor_profile(),
        mode,
        workspace=workspace,
        approvals=broker,
        snapshots=None,
    )
    return IronCoreApp(
        engine,
        build_command_registry(),
        settings,
        envelope_dir=workspace / ".envelopes",
        session_store=session_store,
        resume_id=resume_id,
    )


async def _submit(app: IronCoreApp, pilot, line: str) -> None:
    """Type ``line`` into the input bar and press Enter, as a user would."""
    inp = app.query_one(InputBar)
    inp.value = line
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for(pilot, predicate: Callable[[], bool], tries: int = 400) -> bool:
    """Poll ``predicate`` across message-pump ticks (turns run in a worker)."""
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause()
    return False


async def _settle(app: IronCoreApp, pilot) -> None:
    """Let every worker (turn, scheduled command) finish and the DOM redraw."""
    await app.workers.wait_for_complete()
    for _ in range(4):
        await pilot.pause()


def _write(workspace: Path, name: str, body: str) -> None:
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


#: The broken module the first shots investigate; the goal shot uses the fixed one.
BROKEN_FIB = '''"""Fibonacci helpers for the report generator."""


def fib(n: int) -> int:
    """Return the nth Fibonacci number (0-indexed)."""
    # TODO: this stub was never finished
    return 0
'''

FIXED_FIB = '''"""Fibonacci helpers for the report generator."""


def fib(n: int) -> int:
    """Return the nth Fibonacci number (0-indexed)."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
'''

TEST_FIB = '''from fib import fib


def test_fib_sequence():
    assert [fib(i) for i in range(8)] == [0, 1, 1, 2, 3, 5, 8, 13]


def test_fib_tenth():
    assert fib(10) == 55
'''


README = """# reportgen

Fibonacci helpers for the weekly report generator.
"""


def _seed_project(workspace: Path, *, fixed: bool = False) -> None:
    _write(workspace, "README.md", README)
    _write(workspace, "fib.py", FIXED_FIB if fixed else BROKEN_FIB)
    _write(workspace, "tests/test_fib.py", TEST_FIB)


def _orientation_script() -> list[CompletionResult]:
    """An opening turn reused by several shots, so no frame is mostly empty."""
    return [
        _text(
            "Getting my bearings first.",
            [_call("list_dir", {"path": "."}, "c0")],
            tokens=310,
        ),
        _text(
            "A single-module Python package: fib.py, plus a pytest suite under tests/.",
            tokens=468,
        ),
    ]


def _session_script() -> list[CompletionResult]:
    """The two-turn investigation the session shots share, so the README's
    frames read as one continuous session rather than eight unrelated ones."""
    return [
        *_orientation_script(),
        _text(
            "Let me look at the implementation before guessing.",
            [_call("read_file", {"path": "fib.py"}, "c1")],
            tokens=812,
        ),
        _text(
            "Now let me see who calls it.",
            [_call("grep", {"pattern": "fib\\(", "glob": "*.py"}, "c2")],
            tokens=1140,
        ),
        _text(
            "Found it: fib() is an unfinished stub that returns 0 for every n, so "
            "test_fib_sequence fails on the very first element. It needs the real "
            "recurrence — I can patch it with a two-variable loop.",
            tokens=1483,
        ),
    ]


async def _drive_orientation(app: IronCoreApp, pilot) -> None:
    await _submit(app, pilot, "what's in this project?")
    await _settle(app, pilot)


async def _drive_session(app: IronCoreApp, pilot) -> None:
    await _drive_orientation(app, pilot)
    await _submit(app, pilot, "tests/test_fib.py is failing — why?")
    await _settle(app, pilot)


# --------------------------------------------------------------------------- #
# shots
# --------------------------------------------------------------------------- #


async def shot_session_tool_cards(tmp: Path) -> str:
    """A working session: streamed reasoning plus live tool cards, across two turns."""
    _seed_project(tmp)
    app = _build_app(tmp, _session_script())
    async with app.run_test(size=TERM_SIZE) as pilot:
        await _drive_session(app, pilot)
        return app.export_screenshot(title="IronCore")


async def shot_approval_diff(tmp: Path) -> str:
    """The approval modal: a WRITE-risk edit held at the gate, diff shown in full."""
    _seed_project(tmp)
    edit = (
        "<<<<<<< SEARCH\n"
        "    # TODO: this stub was never finished\n"
        "    return 0\n"
        "=======\n"
        "    a, b = 0, 1\n"
        "    for _ in range(n):\n"
        "        a, b = b, a + b\n"
        "    return a\n"
        ">>>>>>> REPLACE"
    )
    script = [
        _text(
            "Here is the fix — the stub becomes the real recurrence.",
            [
                _call(
                    "edit_file",
                    {"path": "fib.py", "format": "search_replace", "edit": edit},
                    "c1",
                )
            ],
            tokens=1290,
        ),
        _text("Applied. Re-run the tests when you are ready.", tokens=1402),
    ]
    # A generous timeout so the modal is still up when the frame is exported;
    # the deny below resolves it long before this could fire.
    broker = ApprovalBroker(timeout=120.0)
    app = _build_app(tmp, script, broker=broker)
    async with app.run_test(size=TERM_SIZE) as pilot:
        await _submit(app, pilot, "fix fib() with a two-variable loop")
        shown = await _wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        if not shown:
            raise RuntimeError("approval modal never appeared")
        for _ in range(4):
            await pilot.pause()
        svg = app.export_screenshot(title="IronCore")
        await pilot.press("n")  # resolve the gate so the turn can finish cleanly
        await _settle(app, pilot)
        return svg


async def shot_envelope_report(tmp: Path) -> str:
    """/envelope: the measured capability report card for the live model."""
    app = _build_app(tmp, profile=_measured_profile())
    async with app.run_test(size=TERM_SIZE) as pilot:
        await _submit(app, pilot, "/envelope")
        await _wait_for(pilot, lambda: "Verdict:" in app.transcript_text())
        await _settle(app, pilot)
        return app.export_screenshot(title="IronCore")


async def shot_command_palette(tmp: Path) -> str:
    """The slash palette listing the command registry as ``/`` is typed."""
    _seed_project(tmp)
    app = _build_app(tmp, _session_script())
    async with app.run_test(size=TERM_SIZE) as pilot:
        await _drive_session(app, pilot)
        inp = app.query_one(InputBar)
        inp.value = "/"
        for _ in range(4):
            await pilot.pause()
        return app.export_screenshot(title="IronCore")


async def shot_safety_modes(tmp: Path) -> str:
    """Shift+Tab through the whole safety cycle: four modes, four contracts.

    The cycle runs BEFORE the turn (pick your autonomy level, then work), which
    is both the natural order and what leaves the frame with real content in it.
    """
    _seed_project(tmp)
    app = _build_app(tmp, _orientation_script())
    async with app.run_test(size=TERM_SIZE) as pilot:
        await pilot.pause()
        for _ in range(4):  # manual -> accept-edits -> auto -> plan -> manual
            await pilot.press("shift+tab")
            await pilot.pause()
        await _drive_orientation(app, pilot)
        return app.export_screenshot(title="IronCore")


async def shot_goal_verified(tmp: Path) -> str:
    """/goal: an objective whose stop-condition is proved by running a command."""
    _seed_project(tmp, fixed=True)
    check = 'python -c "import fib; assert fib.fib(30) == 832040"'
    app = _build_app(tmp)
    async with app.run_test(size=TERM_SIZE) as pilot:
        await _submit(app, pilot, "/goal make fib() correct for every n up to 30")
        await _wait_for(pilot, lambda: "Goal set" in app.transcript_text())
        await _submit(app, pilot, f"/goal verify: {check}")
        await _wait_for(pilot, lambda: "Attached verify command" in app.transcript_text())
        await _submit(app, pilot, "/goal check")
        ran = await _wait_for(pilot, lambda: "stop-condition" in app.transcript_text())
        if not ran:
            raise RuntimeError("verify command never reported back")
        await _settle(app, pilot)
        return app.export_screenshot(title="IronCore")


async def shot_session_picker(tmp: Path) -> str:
    """The resume picker over a store of real recorded sessions."""
    store = SessionStore(tmp)
    now = datetime.now()
    seeded = (
        (timedelta(minutes=8), "fix the failing fib tests", 3),
        (timedelta(hours=2), "add a --json flag to the report CLI", 6),
        (timedelta(hours=27), "why does the parser drop trailing commas?", 4),
        (timedelta(days=3), "port the ingest script off requests", 11),
    )
    for index, (age, prompt, turns) in enumerate(seeded):
        stamp = now - age
        session_id = f"{stamp:%Y%m%dT%H%M%S}-seed{index:04x}"
        store.create(session_id, stamp.isoformat(), first_prompt=prompt)
        for turn in range(turns):
            store.append_user(session_id, f"{prompt} ({turn + 1})")
            store.append_assistant(session_id, "…")
    app = _build_app(tmp, session_store=store, resume_id=RESUME_PICK)
    async with app.run_test(size=TERM_SIZE) as pilot:
        for _ in range(6):
            await pilot.pause()
        return app.export_screenshot(title="IronCore")


def _doctor_output(tmp: Path) -> str:
    """Run the REAL ``ironcore doctor`` and return its stdout verbatim.

    The subprocess gets a temp HOME and a temp project dir and has every
    ``IRONCORE_*`` variable stripped, so what it reports is the tool's honest
    view of a clean machine rather than of this maintainer's config. ``tmp``
    itself is the fake HOME (rather than a ``home/`` child) to keep the envelope
    cache path doctor prints short enough not to wrap at ``DOCTOR_WIDTH``.
    """
    home = tmp
    project = tmp / "p"
    project.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("IRONCORE_")}
    env.update({"HOME": str(home), "USERPROFILE": str(home)})
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "ironcore.cli", "doctor"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if not proc.stdout.strip():
        raise RuntimeError(f"ironcore doctor produced no output: {proc.stderr!r}")
    return proc.stdout


def render_doctor(tmp: Path) -> str:
    """Replay captured doctor stdout through a recording Console -> SVG.

    Lines are wrapped in ``Text`` so ``[ok]`` / ``[--]`` tags can never be read
    as Rich markup, and no styling is invented: doctor prints plain text and
    this renders plain text.
    """
    output = _doctor_output(tmp)
    console = Console(
        record=True, width=DOCTOR_WIDTH, file=io.StringIO(), force_terminal=True
    )
    console.print(Text("$ ironcore doctor", style="bold"))
    for line in output.splitlines():
        console.print(Text(line))
    return console.export_svg(title="ironcore doctor")


async def shot_doctor(tmp: Path) -> str:
    """``ironcore doctor``: the pre-flight environment check, really executed."""
    return render_doctor(tmp)


#: Output name -> builder. Order is the order the README author will see them.
SHOTS: tuple[tuple[str, Callable[[Path], Awaitable[str]]], ...] = (
    ("01-session-tool-cards", shot_session_tool_cards),
    ("02-approval-diff", shot_approval_diff),
    ("03-envelope-report-card", shot_envelope_report),
    ("04-command-palette", shot_command_palette),
    ("05-safety-modes", shot_safety_modes),
    ("06-goal-verified", shot_goal_verified),
    ("07-doctor", shot_doctor),
    ("08-session-picker", shot_session_picker),
)


# --------------------------------------------------------------------------- #
# SVG -> PNG
# --------------------------------------------------------------------------- #

_VIEWBOX_RE = re.compile(r'viewBox="0 0 ([\d.]+) ([\d.]+)"')

_WRAPPER = (
    '<!doctype html><meta charset="utf-8">'
    "<style>html,body{{margin:0;padding:0}}"
    "img{{display:block;width:{w}px;height:{h}px}}</style>"
    '<img src="{src}">'
)


def find_browser() -> str | None:
    """A headless-capable Chromium binary, or None."""
    override = os.environ.get(BROWSER_ENV)
    if override:
        return override if Path(override).exists() else None
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in BROWSER_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def _viewbox(svg: str) -> tuple[int, int]:
    match = _VIEWBOX_RE.search(svg)
    if match is None:
        raise RuntimeError("exported SVG has no viewBox — cannot size the raster")
    return math.ceil(float(match.group(1))), math.ceil(float(match.group(2)))


def rasterize(svg_path: Path, png_path: Path, browser: str, profile_dir: Path) -> None:
    """Screenshot ``svg_path`` into ``png_path`` with headless Chromium.

    ABSOLUTE paths and an explicit ``--user-data-dir`` are both required — with
    either missing, headless Chromium on Windows fails with "Access is denied".
    """
    svg = svg_path.read_text(encoding="utf-8")
    width, height = _viewbox(svg)
    wrapper = svg_path.with_suffix(".html")
    wrapper.write_text(
        _WRAPPER.format(w=width, h=height, src=svg_path.name), encoding="utf-8"
    )
    subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--force-device-scale-factor={SCALE}",
            f"--user-data-dir={profile_dir}",
            f"--window-size={width},{height}",
            f"--screenshot={png_path}",
            wrapper.resolve().as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if not png_path.exists():
        raise RuntimeError(f"browser produced no PNG for {svg_path.name}")


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Short prefix / short per-shot dir names: the doctor shot prints its (fake)
    # HOME-derived envelope path, and a deep temp path would wrap the line.
    work = Path(tempfile.mkdtemp(prefix="ic-"))
    svgs: list[tuple[str, Path]] = []
    for name, builder in SHOTS:
        shot_dir = work / name.split("-", 1)[0]
        shot_dir.mkdir(parents=True, exist_ok=True)
        svg_path = work / f"{name}.svg"
        svg_path.write_text(asyncio.run(builder(shot_dir)), encoding="utf-8")
        svgs.append((name, svg_path))
        print(f"[svg] {name}")

    browser = find_browser()
    if browser is None:
        print(
            f"\nNo headless browser found. Set {BROWSER_ENV} to an Edge/Chrome binary "
            f"and re-run.\nThe SVGs are complete and were kept at: {work}",
            file=sys.stderr,
        )
        return 1

    print(f"\n[browser] {browser}")
    profile_dir = work / "browser-profile"
    for name, svg_path in svgs:
        png_path = OUT_DIR / f"{name}.png"
        rasterize(svg_path, png_path, browser, profile_dir)
        print(f"[png] {png_path.relative_to(REPO_ROOT)}  {png_path.stat().st_size:,} bytes")

    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(svgs)} screenshot(s) written to {OUT_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
