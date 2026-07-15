"""Sampling policy resolution + best-of-n verifier harness (MODELS.md §6).

Two pure-ish helpers the turn engine leans on. Both are deterministic given
their inputs — no clocks, no RNG in this module; temperature moves are plain
arithmetic.

Sampling policy (``resolve_sampling``)
--------------------------------------
Harness policy from MODELS §6: cold for mechanical work, warm only for
divergent thinking, hotter on a retry to escape a deterministic dead end.

* ``kind`` picks a temperature band the model's *discovered* default
  (``profile.sampling``) is clamped into — never trusted blindly, never
  mutated:

    | kind         | band (floor, ceil) | intent                         |
    |--------------|--------------------|--------------------------------|
    | ``tool``     | (0.0, 0.2)         | COLD — deterministic calls     |
    | ``edit``     | (0.0, 0.2)         | COLD — deterministic patches   |
    | ``plan``     | (0.3, 0.5)         | a bit warmer                   |
    | ``brainstorm`` | (0.5, 0.7)       | divergent, up to ~0.7          |

* On a retry (``attempt`` > 0) temperature is bumped ``+0.2`` per attempt and
  capped at ``0.9``. The bump is allowed to exceed the kind ceiling on purpose:
  the whole point is to break out of a repeat failure.

A fresh :class:`~ironcore.providers.base.SamplingPolicy` is returned each call;
``profile.sampling`` is read, never written.

Best-of-n (``best_of``)
-----------------------
For a step that has a MECHANICAL verifier — a patch that applies, a test that
passes — generate up to ``n`` candidates, check each, take the first that
verifies (short-circuit; don't burn the remaining calls), else the best-scoring
one with ``verified=False``. It is NOT for open-ended asks with no checkable
outcome; without a real verifier there is nothing to be "best" at.

``verify`` returns either a ``bool`` (True = a full pass) or a numeric score
normalized so that ``>= 1.0`` counts as a full pass; scores rank candidates
when none fully pass. ``generate`` is an async callable; ``verify`` is sync.

``budget`` (optional, loosely coupled) is checked *before* each attempt so
``attempts_used`` reflects an early stop. Accepted shapes, in precedence order:
``None`` (unbounded) · an object with ``should_continue() -> bool`` · an object
with ``remaining() -> int`` (``> 0`` means continue) · a plain callable
``() -> bool``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import SamplingPolicy

#: Per-kind temperature band the profile's discovered default is clamped into.
_TEMPERATURE_BANDS: dict[str, tuple[float, float]] = {
    "tool": (0.0, 0.2),
    "edit": (0.0, 0.2),
    "plan": (0.3, 0.5),
    "brainstorm": (0.5, 0.7),
}

#: Retry resampling (MODELS §6): +0.2 per attempt, hard-capped.
_RETRY_BUMP = 0.2
_RETRY_CAP = 0.9

#: Score at/above which a numeric ``verify`` result is a full pass.
_FULL_PASS = 1.0

#: Neutral fallbacks if a profile omits a sampling key (kept in sync with
#: SamplingPolicy so defaults never drift between the two).
_DEFAULTS = SamplingPolicy()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def resolve_sampling(
    profile: CapabilityProfile,
    *,
    kind: str,
    attempt: int = 0,
) -> SamplingPolicy:
    """Resolve a per-turn :class:`SamplingPolicy` for one envelope.

    ``kind`` is one of ``tool``/``edit``/``plan``/``brainstorm``. The model's
    discovered temperature is clamped into that kind's band, then bumped ``+0.2``
    per retry ``attempt`` (capped at ``0.9``). Returns a fresh policy; the
    profile is never mutated.

    Raises ``ValueError`` on an unknown ``kind`` or a negative ``attempt``.
    """
    if kind not in _TEMPERATURE_BANDS:
        raise ValueError(
            f"unknown sampling kind {kind!r}; expected one of {sorted(_TEMPERATURE_BANDS)}"
        )
    if attempt < 0:
        raise ValueError(f"attempt must be >= 0, got {attempt}")

    defaults = getattr(profile, "sampling", None) or {}
    base_temp = float(defaults.get("temperature", _DEFAULTS.temperature))
    top_p = float(defaults.get("top_p", _DEFAULTS.top_p))

    low, high = _TEMPERATURE_BANDS[kind]
    temperature = _clamp(base_temp, low, high)
    if attempt > 0:
        temperature = min(temperature + _RETRY_BUMP * attempt, _RETRY_CAP)

    # Round away binary-float noise (0.5 + 0.2 != 0.7 in IEEE-754) so the policy
    # is clean and exactly reproducible.
    return SamplingPolicy(temperature=round(temperature, 4), top_p=round(top_p, 4))


@dataclass
class BestOf:
    """Outcome of a :func:`best_of` run.

    * ``winner`` — the chosen candidate (the first to fully pass, else the
      best-scoring). ``None`` only if no candidate was ever generated (a budget
      that refused before the first attempt).
    * ``attempts_used`` — how many times ``generate`` actually ran; ``< n`` when
      short-circuited or stopped by budget.
    * ``verified`` — did the winner fully pass its mechanical check?
    """

    winner: Any
    attempts_used: int
    verified: bool


def _grade(result: bool | float | int) -> tuple[bool, float]:
    """Map a ``verify`` return to ``(passed, score)``.

    ``bool`` -> pass iff True (score 1.0 / 0.0). A number is a score; it is a
    full pass at/above :data:`_FULL_PASS`. bools are checked first because
    ``bool`` is a subclass of ``int``.
    """
    if isinstance(result, bool):
        return result, (1.0 if result else 0.0)
    score = float(result)
    return score >= _FULL_PASS, score


def _budget_allows(budget: Any) -> bool:
    """Loose budget duck-typing (see module docstring). ``None`` -> unbounded."""
    if budget is None:
        return True
    if callable(getattr(budget, "should_continue", None)):
        return bool(budget.should_continue())
    if callable(getattr(budget, "remaining", None)):
        return budget.remaining() > 0
    if callable(budget):
        return bool(budget())
    raise TypeError(
        "budget must be None, expose should_continue()/remaining(), or be callable; "
        f"got {type(budget).__name__}"
    )


async def best_of(
    n: int,
    generate: Callable[[], Awaitable[Any]],
    verify: Callable[[Any], bool | float | int],
    *,
    budget: Any = None,
) -> BestOf:
    """Generate up to ``n`` candidates and return the first that verifies.

    ``generate`` is awaited for each candidate; ``verify`` (sync) grades it via
    :func:`_grade`. The first full pass wins immediately (remaining calls are
    skipped). If none fully pass, the highest-scoring candidate is returned with
    ``verified=False``. A ``budget`` is consulted before each attempt so a
    stop is reflected in ``attempts_used``.

    Only meaningful where a real mechanical verifier exists (patch applies /
    test passes). Raises ``ValueError`` if ``n < 1``.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    winner: Any = None
    best_score: float | None = None
    attempts = 0

    for _ in range(n):
        if not _budget_allows(budget):
            break
        candidate = await generate()
        attempts += 1
        passed, score = _grade(verify(candidate))
        if best_score is None or score > best_score:
            winner, best_score = candidate, score
        if passed:
            return BestOf(winner=candidate, attempts_used=attempts, verified=True)

    return BestOf(winner=winner, attempts_used=attempts, verified=False)
