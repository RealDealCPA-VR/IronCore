"""History compaction (IC-505): handoff-shaped summary via the summarizer,
deterministic mechanical fallback on provider failure / bypass, and the
should_compact budget predicate (SPEC §11.2). Async is driven with asyncio.run."""

import asyncio

from ironcore.core.compact import compact, should_compact
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider, RaiseError, TimeoutFailure

HANDOFF_FIELDS = ("Context:", "Changed:", "Verified:", "Next:", "Gotchas:")


def _profile(honest_context: int = 4096) -> CapabilityProfile:
    return CapabilityProfile(model_id="test-model", honest_context=honest_context)


def _history() -> list[Message]:
    return [
        Message(role="user", content="add a --json flag to the report command"),
        Message(role="assistant", content="reading report.py"),
        Message(role="tool", content="def report(): ...", tool_call_id="c1", name="read_file"),
        Message(role="assistant", content="editing report.py to add the flag"),
    ]


def _summary_result() -> CompletionResult:
    text = (
        "Context: adding a --json flag to the report command\n"
        "Changed: edited report.py to accept --json and emit JSON\n"
        "Verified: pytest tests/test_report.py -> 4 passed\n"
        "Next: document the flag in the README\n"
        "Gotchas: none\n"
    )
    return CompletionResult(message=Message(role="assistant", content=text))


# -- model path: handoff-shaped summary ---------------------------------------


def test_compact_returns_handoff_shaped_summary():
    mock = MockProvider([_summary_result()])
    msg = asyncio.run(compact(_history(), provider=mock, model="qwen3:8b"))
    assert msg.role == "user"
    for field in HANDOFF_FIELDS:
        assert field in msg.content
    assert "edited report.py" in msg.content
    assert "qwen3:8b" in msg.content  # model recorded as provenance in the header
    assert len(mock.calls) == 1  # the summarizer was actually called


def test_compact_sends_the_five_field_instruction_to_the_summarizer():
    mock = MockProvider([_summary_result()])
    asyncio.run(compact(_history(), provider=mock))
    sent = mock.calls[0]
    assert sent[0].role == "system"
    for field in HANDOFF_FIELDS:
        assert field in sent[0].content  # prompt asks for exactly the handoff fields
    # the transcript to summarize is passed as user content
    assert "add a --json flag" in sent[1].content


def test_empty_model_summary_falls_back_to_mechanical():
    mock = MockProvider([CompletionResult(message=Message(role="assistant", content="   "))])
    msg = asyncio.run(compact(_history(), provider=mock))
    assert "mechanical digest" in msg.content


# -- mechanical fallback: never hard-fails ------------------------------------


def test_provider_error_falls_back_without_raising():
    mock = MockProvider([RaiseError(message="502 from endpoint")])
    msg = asyncio.run(compact(_history(), provider=mock))  # must not raise
    assert msg.role == "user"
    assert "mechanical digest" in msg.content
    assert "userx1" in msg.content  # role histogram
    assert "assistantx2" in msg.content
    assert "Recent tail" in msg.content


def test_provider_timeout_also_falls_back():
    mock = MockProvider([TimeoutFailure(message="timed out")])
    msg = asyncio.run(compact(_history(), provider=mock))  # ProviderTimeout is a ProviderError
    assert "mechanical digest" in msg.content


def test_fallback_only_bypasses_the_provider():
    mock = MockProvider([_summary_result()])
    msg = asyncio.run(compact(_history(), provider=mock, fallback_only=True))
    assert "mechanical digest" in msg.content
    assert mock.calls == []  # provider was never touched
    assert mock.script  # scripted entry left unconsumed


def test_empty_history_compacts_mechanically_without_a_call():
    mock = MockProvider([_summary_result()])
    msg = asyncio.run(compact([], provider=mock))
    assert "0 earlier message(s)" in msg.content
    assert mock.calls == []


def test_mechanical_digest_is_deterministic():
    hist = _history()
    a = asyncio.run(compact(hist, provider=MockProvider(), fallback_only=True))
    b = asyncio.run(compact(hist, provider=MockProvider(), fallback_only=True))
    assert a.content == b.content  # no clocks, no randomness


# -- should_compact predicate --------------------------------------------------


def test_should_compact_false_when_history_small():
    history = [Message(role="user", content="hi")]
    assert should_compact(history, profile=_profile()) is False


def test_should_compact_true_when_history_exceeds_budget():
    # budget = 4096 * HISTORY_SHARE (0.25) = 1024 tokens; ~1500 tokens here.
    big = Message(role="assistant", content="x" * 6000)
    assert should_compact([big], profile=_profile()) is True


def test_should_compact_honors_headroom_ratio():
    big = Message(role="assistant", content="x" * 6000)  # ~1500 tokens
    # A generous ratio (0.9 -> 3686-token budget) means the same history still fits.
    assert should_compact([big], profile=_profile(), headroom_ratio=0.9) is False
    # A tight ratio (0.05 -> 204-token budget) trips on even small history.
    small = [Message(role="user", content="a short line of text here")]
    assert should_compact(small, profile=_profile(), headroom_ratio=0.001) is True
