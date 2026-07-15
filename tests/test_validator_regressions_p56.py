"""Regression pins for the phase-5/6 adversarial validation round (2026-07-16).

Each test reproduces one validator finding and pins its fix.
"""

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.budgets import Budget
from ironcore.core.compact import compact
from ironcore.core.engine import TurnEngine
from ironcore.core.events import ToolCallFinished, TurnCompleted
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools import build_default_registry

SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


# --- BLOCKER 1: compaction must not send unredacted secrets to the provider


def test_finding1_compaction_redacts_before_the_provider():
    captured = MockProvider(
        [CompletionResult(message=Message(role="assistant", content="Context: ...\nGotchas: none"))]
    )
    history = [
        Message(role="user", content="deploy it"),
        Message(role="tool", content=f'API_KEY = "{SECRET}"', name="read_file"),
    ]
    asyncio.run(compact(history, provider=captured, model="summarizer"))
    # the transcript the summarizer received must be scrubbed
    sent = "".join(m.content for call in captured.calls for m in call)
    assert SECRET not in sent
    assert "[redacted:openai-key]" in sent


def test_finding1_compaction_fallback_digest_is_also_redacted():
    # provider errors -> mechanical digest; the tail must still be scrubbed
    from ironcore.providers.mock import RaiseError

    failing = MockProvider([RaiseError(message="boom")])
    history = [Message(role="tool", content=f'token={SECRET}', name="read_file")]
    summary = asyncio.run(compact(history, provider=failing, model=""))
    assert SECRET not in summary.content


# --- BLOCKER 2: still-failing verification must never report stop_reason "done"


def _fail_cmd() -> str:
    import sys

    return f'{sys.executable} -c "import sys; sys.exit(1)"'


def test_finding2_failing_verify_reports_goal_unmet_not_done(tmp_path):
    from ironcore.core.verify import CommandVerifier

    registry = build_default_registry(Settings(), tmp_path)
    write = ToolCall(id="c1", name="write_file", arguments={"path": "out.txt", "content": "x"})
    provider = MockProvider(
        [
            CompletionResult(message=Message(role="assistant", content="", tool_calls=[write])),
            CompletionResult(message=Message(role="assistant", content="All done! Tests pass.")),
            CompletionResult(message=Message(role="assistant", content="Yep, complete.")),
        ]
    )
    engine = TurnEngine(
        provider, registry, Settings(), _profile(), Mode.ACCEPT_EDITS,
        workspace=tmp_path, verifier=CommandVerifier(commands=[_fail_cmd()]), snapshots=None,
    )

    events = []

    async def go():
        async for ev in engine.run_turn("write it"):
            events.append(ev)

    asyncio.run(go())
    completed = [e for e in events if isinstance(e, TurnCompleted)][-1]
    # the model claimed success; the engine reports the evidence, not the claim
    assert completed.stop_reason == "goal-unmet"


# --- MAJOR 3: compaction fires at most once per turn and is budget-counted


def test_finding3_compaction_is_once_per_turn_and_counted(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", newline="")
    registry = build_default_registry(Settings(), tmp_path)
    read = ToolCall(id="c1", name="read_file", arguments={"path": "app.py"})
    # long user input guarantees should_compact stays True across iterations
    big = "please read the file. " * 4000
    # one compaction summary + reads; a per-iteration compaction storm would
    # blow far past this script. Budget cap also counts the compaction call.
    script = [
        CompletionResult(message=Message(role="assistant", content="summary")),  # compaction
    ] + [CompletionResult(message=Message(role="assistant", content="", tool_calls=[read]))] * 6
    provider = MockProvider(script)
    engine = TurnEngine(
        provider, registry, Settings(), _profile(), Mode.AUTO,
        workspace=tmp_path, budget=Budget(max_provider_calls=4), snapshots=None,
    )

    events = []

    async def go():
        async for ev in engine.run_turn(big):
            events.append(ev)

    asyncio.run(go())
    completed = [e for e in events if isinstance(e, TurnCompleted)][-1]
    assert completed.stop_reason == "budget"  # bounded, not a runaway
    # the compaction call counted toward the cap: fewer than the raw call budget
    # of reads got through before the cap tripped
    assert len([e for e in events if isinstance(e, ToolCallFinished)]) <= 4
