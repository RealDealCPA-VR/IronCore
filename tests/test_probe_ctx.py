"""CTX-HONESTY + RETENTION probe tests (IC-602).

Drive ``CtxHonestyProbe`` / ``RetentionProbe`` with a ``MockProvider`` whose scripted
completions decide, mechanically, what each turn "remembers". Coverage:
  * CTX-HONESTY stops at the last size that retrieves (>=0.9), tops out when all retrieve,
    and falls back to the floor when nothing retrieves.
  * RETENTION scores the right fraction and reports the correct coherence horizon when a mock
    drops the prefix mid-conversation, and 1.0 / top-checkpoint when it never drops.
  * Scoring is deterministic on identical scripts.
  * An exhausted / erroring provider yields ok=False + a note (no crash), and the runner then
    degrades reliability targets while leaving context/horizon at base.
"""

import asyncio

from ironcore.envelope.probe_ctx import (
    CtxHonestyProbe,
    RetentionProbe,
    _filler_tokens,
    _haystack,
)
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import run_probes
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider


def _reply(content: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=content))


def _ctx_script(passcode: str, per_size_pass: list[bool], n_depths: int) -> list[CompletionResult]:
    """One completion per (size, depth); a size 'passes' => every depth retrieves the passcode."""
    script: list[CompletionResult] = []
    for passes in per_size_pass:
        for _ in range(n_depths):
            body = f"The passcode is {passcode}" if passes else "I could not find a passcode."
            script.append(_reply(body))
    return script


def _ret_script(prefix: str, per_turn_pass: list[bool]) -> list[CompletionResult]:
    return [_reply(f"{prefix} ok" if p else "sorry, here is the answer") for p in per_turn_pass]


# --------------------------------------------------------------------------- #
# Probe contract sanity
# --------------------------------------------------------------------------- #


def test_probe_ids_and_targets():
    assert CtxHonestyProbe().id == "CTX-HONESTY"
    assert CtxHonestyProbe().targets == ("honest_context",)
    assert RetentionProbe().id == "RETENTION"
    assert RetentionProbe().targets == ("instruction_retention", "coherence_horizon")


# --------------------------------------------------------------------------- #
# CTX-HONESTY
# --------------------------------------------------------------------------- #


def test_ctx_honesty_stops_at_8k():
    sizes = (4096, 8192, 16384)
    depths = (0.25, 0.5, 0.75, 0.9)
    passcode = "ZZ-1"
    # remembers up to 8k, collapses at 16k
    script = _ctx_script(passcode, [True, True, False], n_depths=len(depths))
    probe = CtxHonestyProbe(sizes=sizes, depths=depths, passcode=passcode)
    result = asyncio.run(probe.run(MockProvider(script=script)))
    assert result.ok is True
    assert result.scores == {"honest_context": 8192}
    assert isinstance(result.scores["honest_context"], int)


def test_ctx_honesty_all_retrieved_tops_out():
    sizes = (4096, 8192, 16384)
    depths = (0.5, 0.9)
    passcode = "ZZ-2"
    script = _ctx_script(passcode, [True, True, True], n_depths=len(depths))
    probe = CtxHonestyProbe(sizes=sizes, depths=depths, passcode=passcode)
    result = asyncio.run(probe.run(MockProvider(script=script)))
    assert result.scores["honest_context"] == 16384  # the top rung


def test_ctx_honesty_none_retrieved_falls_to_floor():
    sizes = (8192, 16384)  # note: above the floor, to prove the floor is what's returned
    depths = (0.5, 0.9)
    passcode = "ZZ-3"
    script = _ctx_script(passcode, [False, False], n_depths=len(depths))
    probe = CtxHonestyProbe(sizes=sizes, depths=depths, passcode=passcode, floor=4096)
    result = asyncio.run(probe.run(MockProvider(script=script)))
    assert result.scores["honest_context"] == 4096


def test_ctx_honesty_partial_depth_fail_below_threshold():
    # 3/4 depths retrieve at 8k -> 0.75 < 0.9 -> 8k does NOT count as honest.
    sizes = (4096, 8192)
    passcode = "ZZ-4"
    script = [
        # 4k: all four depths retrieve
        _reply(f"The passcode is {passcode}"),
        _reply(f"The passcode is {passcode}"),
        _reply(f"The passcode is {passcode}"),
        _reply(f"The passcode is {passcode}"),
        # 8k: only three of four retrieve
        _reply(f"The passcode is {passcode}"),
        _reply("no idea"),
        _reply(f"The passcode is {passcode}"),
        _reply(f"The passcode is {passcode}"),
    ]
    probe = CtxHonestyProbe(sizes=sizes, depths=(0.25, 0.5, 0.75, 0.9), passcode=passcode)
    result = asyncio.run(probe.run(MockProvider(script=script)))
    assert result.scores["honest_context"] == 4096


def test_ctx_honesty_deterministic():
    sizes = (4096, 8192)
    depths = (0.5, 0.9)
    passcode = "ZZ-5"
    per = [True, False]
    a = asyncio.run(
        CtxHonestyProbe(sizes=sizes, depths=depths, passcode=passcode).run(
            MockProvider(script=_ctx_script(passcode, per, len(depths)))
        )
    )
    b = asyncio.run(
        CtxHonestyProbe(sizes=sizes, depths=depths, passcode=passcode).run(
            MockProvider(script=_ctx_script(passcode, per, len(depths)))
        )
    )
    assert a.scores == b.scores == {"honest_context": 4096}


def test_ctx_honesty_exhausted_provider_ok_false():
    # empty script -> MockProvider raises "script exhausted" on the first complete().
    probe = CtxHonestyProbe(sizes=(4096,), depths=(0.5,), passcode="ZZ-6")
    result = asyncio.run(probe.run(MockProvider(script=[])))
    assert result.ok is False
    assert result.scores == {}
    assert "CTX-HONESTY" in result.notes and result.notes


# --------------------------------------------------------------------------- #
# RETENTION
# --------------------------------------------------------------------------- #


def test_retention_drops_after_turn_6():
    prefix = "REF-7:"
    # turns 1..6 comply, 7..12 drift -> checkpoints 3,6 hold; 9,12 drop.
    per_turn = [t <= 6 for t in range(1, 13)]
    probe = RetentionProbe(prefix=prefix)  # default checkpoints (3,6,9,12), 12 turns
    result = asyncio.run(probe.run(MockProvider(script=_ret_script(prefix, per_turn))))
    assert result.ok is True
    assert result.scores["coherence_horizon"] == 6
    assert result.scores["instruction_retention"] == 0.5
    assert isinstance(result.scores["coherence_horizon"], int)


def test_retention_always_complies():
    prefix = "REF-7:"
    per_turn = [True] * 12
    result = asyncio.run(
        RetentionProbe(prefix=prefix).run(MockProvider(script=_ret_script(prefix, per_turn)))
    )
    assert result.scores["instruction_retention"] == 1.0
    assert result.scores["coherence_horizon"] == 12


def test_retention_drops_before_first_checkpoint():
    prefix = "REF-7:"
    per_turn = [False] * 12  # never adheres
    result = asyncio.run(
        RetentionProbe(prefix=prefix).run(MockProvider(script=_ret_script(prefix, per_turn)))
    )
    assert result.scores["instruction_retention"] == 0.0
    assert result.scores["coherence_horizon"] == 0


def test_retention_custom_checkpoints_and_turns():
    prefix = "TAG:"
    # small battery for speed: checkpoints 2 and 4, drop after turn 2
    per_turn = [True, True, False, False]
    probe = RetentionProbe(prefix=prefix, checkpoints=(2, 4))
    result = asyncio.run(probe.run(MockProvider(script=_ret_script(prefix, per_turn))))
    assert probe.total_turns == 4
    assert result.scores["instruction_retention"] == 0.5
    assert result.scores["coherence_horizon"] == 2


def test_retention_tolerates_leading_whitespace():
    prefix = "REF-7:"
    per_turn = [_reply(f"  {prefix} indented reply") for _ in range(12)]
    result = asyncio.run(RetentionProbe(prefix=prefix).run(MockProvider(script=per_turn)))
    assert result.scores["instruction_retention"] == 1.0


def test_retention_exhausted_provider_ok_false():
    result = asyncio.run(
        RetentionProbe(prefix="REF-7:", checkpoints=(2,)).run(MockProvider(script=[]))
    )
    assert result.ok is False
    assert result.scores == {}
    assert "RETENTION" in result.notes


# --------------------------------------------------------------------------- #
# Integration with the runner (degradation semantics IC-608 relies on)
# --------------------------------------------------------------------------- #


def test_run_probes_merges_ctx_and_retention():
    prefix = "REF-7:"
    ctx = CtxHonestyProbe(sizes=(4096, 8192), depths=(0.5, 0.9), passcode="ZZ-7")
    ret = RetentionProbe(prefix=prefix, checkpoints=(2, 4))
    script = (
        _ctx_script("ZZ-7", [True, True], n_depths=2)  # ctx: both rungs retrieve -> 8192
        + _ret_script(prefix, [True, True, True, True])  # ret: all hold -> 1.0 / horizon 4
    )
    profile = asyncio.run(
        run_probes(MockProvider(script=script), [ctx, ret], model_id="m", probed_at="t")
    )
    assert profile.honest_context == 8192
    assert profile.instruction_retention == 1.0
    assert profile.coherence_horizon == 4


def test_run_probes_erroring_ctx_leaves_honest_context_at_base():
    # A failed context measurement must NOT invent a smaller honest_context (runner rule).
    base = CapabilityProfile(model_id="m", honest_context=16384)
    ctx = CtxHonestyProbe(sizes=(4096,), depths=(0.5,))
    profile = asyncio.run(
        run_probes(MockProvider(script=[]), [ctx], model_id="m", base=base, probed_at="t")
    )
    assert profile.honest_context == 16384  # untouched, not degraded to 0


def test_run_probes_erroring_retention_degrades_reliability_only():
    base = CapabilityProfile(model_id="m", coherence_horizon=6, instruction_retention=0.9)
    ret = RetentionProbe(checkpoints=(2,))
    profile = asyncio.run(
        run_probes(MockProvider(script=[]), [ret], model_id="m", base=base, probed_at="t")
    )
    assert profile.instruction_retention == 0.0  # reliability degraded
    assert profile.coherence_horizon == 6  # context/horizon left at base


# --------------------------------------------------------------------------- #
# Deterministic filler helpers
# --------------------------------------------------------------------------- #


def test_filler_is_deterministic_and_sized():
    assert _filler_tokens(5) == _filler_tokens(5)
    assert len(_filler_tokens(2000)) == 2000
    needle = "The passcode is ABC."
    doc = _haystack(100, 0.5, needle)
    assert doc == _haystack(100, 0.5, needle)  # deterministic
    assert needle in doc
