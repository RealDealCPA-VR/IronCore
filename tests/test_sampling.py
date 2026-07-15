"""Sampling policy resolution + best-of-n harness (IC-607, MODELS §6).

No pytest-asyncio: each async assertion drives one loop via ``asyncio.run``.
Everything here is deterministic — no clocks, no RNG.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from ironcore.core.sampling import BestOf, best_of, resolve_sampling
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import SamplingPolicy

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _seq_generate(items: list) -> Callable[[], Awaitable]:
    """Async ``generate`` that yields ``items`` one per call."""
    it = iter(items)

    async def gen():
        return next(it)

    return gen


def _counting_generate() -> tuple[Callable[[], Awaitable[int]], dict]:
    """Async ``generate`` returning 1, 2, 3, ... plus a call counter to prove
    short-circuit actually skips the remaining calls."""
    calls = {"n": 0}

    async def gen():
        calls["n"] += 1
        return calls["n"]

    return gen, calls


# --------------------------------------------------------------------------- #
# resolve_sampling — bands per kind
# --------------------------------------------------------------------------- #


def test_tool_and_edit_are_cold():
    profile = CapabilityProfile(model_id="m")  # default sampling temp 0.2
    assert resolve_sampling(profile, kind="tool").temperature == pytest.approx(0.2)
    assert resolve_sampling(profile, kind="edit").temperature == pytest.approx(0.2)


def test_brainstorm_is_warmer_than_tool_and_plan_sits_between():
    profile = CapabilityProfile(model_id="m")
    tool = resolve_sampling(profile, kind="tool").temperature
    plan = resolve_sampling(profile, kind="plan").temperature
    brainstorm = resolve_sampling(profile, kind="brainstorm").temperature
    assert tool < plan < brainstorm
    assert brainstorm == pytest.approx(0.5)  # clamp(0.2, 0.5, 0.7)
    assert brainstorm <= 0.7


def test_hot_model_default_is_clamped_down_for_cold_kinds():
    profile = CapabilityProfile(model_id="m", sampling={"temperature": 0.9, "top_p": 0.8})
    pol = resolve_sampling(profile, kind="tool")
    assert pol.temperature == pytest.approx(0.2)  # clamped into (0.0, 0.2)
    assert pol.top_p == pytest.approx(0.8)  # carried through from the profile


def test_unknown_kind_and_negative_attempt_raise():
    profile = CapabilityProfile(model_id="m")
    with pytest.raises(ValueError):
        resolve_sampling(profile, kind="chatty")
    with pytest.raises(ValueError):
        resolve_sampling(profile, kind="tool", attempt=-1)


# --------------------------------------------------------------------------- #
# resolve_sampling — retry bump + cap
# --------------------------------------------------------------------------- #


def test_retry_bumps_by_point_two_per_attempt():
    profile = CapabilityProfile(model_id="m")  # tool base 0.2
    assert resolve_sampling(profile, kind="tool", attempt=0).temperature == pytest.approx(0.2)
    assert resolve_sampling(profile, kind="tool", attempt=1).temperature == pytest.approx(0.4)
    assert resolve_sampling(profile, kind="tool", attempt=2).temperature == pytest.approx(0.6)
    assert resolve_sampling(profile, kind="tool", attempt=3).temperature == pytest.approx(0.8)


def test_retry_temperature_is_capped():
    profile = CapabilityProfile(model_id="m")
    # tool base 0.2 + 0.2*4 = 1.0 -> capped at 0.9
    assert resolve_sampling(profile, kind="tool", attempt=4).temperature == pytest.approx(0.9)
    # brainstorm base 0.5 + 0.2*3 = 1.1 -> capped at 0.9
    assert resolve_sampling(profile, kind="brainstorm", attempt=3).temperature == pytest.approx(0.9)
    # never exceeds the cap no matter how many attempts
    assert resolve_sampling(profile, kind="tool", attempt=99).temperature <= 0.9


def test_returns_fresh_policy_and_never_mutates_profile():
    profile = CapabilityProfile(model_id="m")
    before = dict(profile.sampling)
    p1 = resolve_sampling(profile, kind="brainstorm", attempt=2)
    p2 = resolve_sampling(profile, kind="tool")
    assert isinstance(p1, SamplingPolicy)
    assert profile.sampling == before  # profile untouched
    assert p1 is not p2  # a new object each call
    assert p1.temperature == pytest.approx(0.9)  # clamp(0.2,0.5,0.7)=0.5 -> +0.4 -> 0.9


# --------------------------------------------------------------------------- #
# best_of — short-circuit on first pass
# --------------------------------------------------------------------------- #


def test_first_verified_winner_short_circuits():
    gen, calls = _counting_generate()
    result = asyncio.run(best_of(5, gen, lambda c: c == 2))
    assert isinstance(result, BestOf)
    assert result.winner == 2
    assert result.verified is True
    assert result.attempts_used == 2
    assert result.attempts_used < 5
    assert calls["n"] == 2  # remaining generate calls were skipped


def test_bool_true_wins_over_later_candidates():
    gen = _seq_generate(["a", "b", "c"])
    result = asyncio.run(best_of(3, gen, lambda c: c == "a"))
    assert result.winner == "a"
    assert result.verified is True
    assert result.attempts_used == 1


def test_numeric_score_at_one_counts_as_full_pass():
    gen = _seq_generate([10, 20, 30])
    # score 1.0 on the second candidate -> full pass, short-circuit
    result = asyncio.run(best_of(3, gen, lambda c: 1.0 if c == 20 else 0.4))
    assert result.winner == 20
    assert result.verified is True
    assert result.attempts_used == 2


# --------------------------------------------------------------------------- #
# best_of — nobody passes -> best-scoring, verified=False
# --------------------------------------------------------------------------- #


def test_no_pass_returns_best_scoring_unverified():
    gen = _seq_generate([1, 2, 3])
    scores = {1: 0.3, 2: 0.9, 3: 0.5}
    result = asyncio.run(best_of(3, gen, lambda c: scores[c]))
    assert result.winner == 2  # highest score, still < 1.0
    assert result.verified is False
    assert result.attempts_used == 3


def test_all_bool_false_returns_a_candidate_unverified():
    gen = _seq_generate(["x", "y"])
    result = asyncio.run(best_of(2, gen, lambda c: False))
    assert result.winner == "x"  # first seen, all tie at score 0.0
    assert result.verified is False
    assert result.attempts_used == 2


# --------------------------------------------------------------------------- #
# best_of — budget duck-typing
# --------------------------------------------------------------------------- #


class _ShouldContinueBudget:
    """Allows exactly ``allow`` attempts via ``should_continue()``."""

    def __init__(self, allow: int) -> None:
        self.allow = allow
        self.checks = 0

    def should_continue(self) -> bool:
        cont = self.checks < self.allow
        self.checks += 1
        return cont


class _RemainingBudget:
    """Self-decrementing ``remaining()`` budget; > 0 means continue."""

    def __init__(self, start: int) -> None:
        self.left = start

    def remaining(self) -> int:
        v = self.left
        self.left -= 1
        return v


def test_budget_should_continue_stops_early():
    gen = _seq_generate([1, 2, 3, 4, 5])
    result = asyncio.run(
        best_of(5, gen, lambda c: c * 0.1, budget=_ShouldContinueBudget(allow=2))
    )
    assert result.attempts_used == 2  # early stop reflected
    assert result.verified is False
    assert result.winner == 2  # best score among the two it managed


def test_budget_remaining_stops_early():
    gen = _seq_generate([1, 2, 3, 4, 5])
    result = asyncio.run(best_of(5, gen, lambda c: False, budget=_RemainingBudget(start=2)))
    assert result.attempts_used == 2


def test_budget_plain_callable_stops_early():
    gen, calls = _counting_generate()
    counter = {"n": 0}

    def budget() -> bool:
        counter["n"] += 1
        return counter["n"] <= 3

    result = asyncio.run(best_of(10, gen, lambda c: False, budget=budget))
    assert result.attempts_used == 3
    assert calls["n"] == 3


def test_budget_that_refuses_immediately_yields_no_winner():
    gen, calls = _counting_generate()
    result = asyncio.run(best_of(5, gen, lambda c: True, budget=lambda: False))
    assert result.attempts_used == 0
    assert result.winner is None
    assert result.verified is False
    assert calls["n"] == 0


def test_bad_budget_shape_raises_type_error():
    gen = _seq_generate([1])
    with pytest.raises(TypeError):
        asyncio.run(best_of(1, gen, lambda c: True, budget=object()))


# --------------------------------------------------------------------------- #
# best_of — edges
# --------------------------------------------------------------------------- #


def test_n_one_pass():
    gen = _seq_generate(["only"])
    result = asyncio.run(best_of(1, gen, lambda c: True))
    assert result == BestOf(winner="only", attempts_used=1, verified=True)


def test_n_one_no_pass():
    gen = _seq_generate(["only"])
    result = asyncio.run(best_of(1, gen, lambda c: 0.4))
    assert result.winner == "only"
    assert result.attempts_used == 1
    assert result.verified is False


def test_n_below_one_raises():
    gen = _seq_generate([1])
    with pytest.raises(ValueError):
        asyncio.run(best_of(0, gen, lambda c: True))
