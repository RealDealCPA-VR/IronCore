"""CTX-HONESTY and RETENTION probes (IC-602).

Two members of the probe suite (declared in ``probes.py``, run by the IC-601 runner in
``runner.py``). Both follow the ``Probe`` protocol: a ``id`` / ``title`` / ``targets`` and an
``async run(provider) -> ProbeResult``. ``targets`` are the dotted profile paths the probe
fills, so the runner knows what to degrade if the probe fails (SPEC §4.1, MODELS §2).

Design rules honored here (SPEC §4.1):
  * Mechanical scoring ONLY — no LLM judge. CTX-HONESTY does an exact-substring match of the
    planted passcode; RETENTION checks that a reply *starts with* the required prefix.
  * Deterministic + offline. Filler ("haystack") text is generated from a fixed small vocab
    with no randomness, so a given (size, depth) always produces the same document.
  * Graceful degradation. A provider that errors mid-probe (timeout, provider error, exhausted
    mock script, ...) is caught and reported as ``ok=False`` + a note; the runner then degrades
    reliability targets and leaves context/horizon targets at their base. We never crash the run.

Scoring, precisely:

``CtxHonestyProbe`` (``honest_context``)
  Plant "The passcode is <X>." at each depth (25/50/75/90 %) inside filler of increasing SIZE
  (a ladder that steps up toward the advertised window: 4k, 8k, 16k, ...). For each size,
  retrieval accuracy = fraction of depths whose completion contains the passcode as an exact
  substring. ``honest_context`` = the largest size at which retrieval *stays* >= ``threshold``
  (0.9) — i.e. the top of the contiguous passing prefix, since real models degrade by
  collapsing past a point rather than randomly. If even the smallest rung collapses, we report
  the conservative ``floor`` (4096, matching ``CapabilityProfile``'s default).

``RetentionProbe`` (``instruction_retention``, ``coherence_horizon``)
  Turn 1 sets an arbitrary constraint ("prefix every reply with REF-7:"); later turns are
  unrelated filler. Adherence is checked at ``checkpoints`` (turns 3/6/9/12).
  ``instruction_retention`` = fraction of checkpoints where the prefix held.
  ``coherence_horizon`` = the last checkpoint that held before the FIRST drop (how many turns of
  drift-free adherence) — 0 if it drifts before the first checkpoint, the top checkpoint if it
  never drifts.

Both probes take injectable trial parameters (sizes / depths / checkpoints / prefix) with sane
defaults so fast tests can shrink the battery.
"""

from __future__ import annotations

from collections.abc import Sequence

from ironcore.envelope.runner import ProbeResult
from ironcore.providers.base import Message, Provider

# --------------------------------------------------------------------------- #
# CTX-HONESTY defaults
# --------------------------------------------------------------------------- #

#: Size ladder (approx tokens) stepping up toward a typical advertised window.
_DEFAULT_SIZES: tuple[int, ...] = (4096, 8192, 16384, 32768)
#: Needle depths as a fraction of the document.
_DEFAULT_DEPTHS: tuple[float, ...] = (0.25, 0.50, 0.75, 0.90)
#: A distinctive value unlikely to appear in the filler by chance.
_DEFAULT_PASSCODE = "SWORDFISH-7731"
#: Retrieval must stay at/above this to count a size as "honest".
_RETRIEVAL_THRESHOLD = 0.9
#: honest_context floor when even the smallest rung collapses.
_HONEST_FLOOR = 4096

_CTX_SYSTEM = (
    "You are reading a long document. A single passcode is hidden somewhere in it. "
    "Read the whole thing, then report the passcode exactly."
)
_CTX_QUESTION = "\n\nQUESTION: What is the passcode? Reply with only the passcode value."

# --------------------------------------------------------------------------- #
# RETENTION defaults
# --------------------------------------------------------------------------- #

_DEFAULT_CHECKPOINTS: tuple[int, ...] = (3, 6, 9, 12)
_DEFAULT_PREFIX = "REF-7:"

_RET_SYSTEM = "You are a helpful assistant. Follow standing instructions for the whole chat."


# --------------------------------------------------------------------------- #
# Deterministic filler
# --------------------------------------------------------------------------- #


def _filler_tokens(n: int) -> list[str]:
    """``n`` deterministic filler tokens. A small cycling vocab (rather than one repeated
    token) keeps the text from collapsing to a single token under most tokenizers, while
    staying fully reproducible — no randomness anywhere."""
    return [f"tok{i % 128:03d}" for i in range(max(0, n))]


def _haystack(size: int, depth: float, needle: str) -> str:
    """A ``size``-token filler document with ``needle`` planted at ``depth`` (0..1)."""
    tokens = _filler_tokens(size)
    pos = min(len(tokens), max(0, int(depth * size)))
    return " ".join(tokens[:pos] + [needle] + tokens[pos:])


def _filler_turn(turn: int) -> str:
    """A deterministic, unrelated distractor prompt for RETENTION filler turns."""
    return f"Aside {turn}: briefly, what is {turn} plus {turn}?"


# --------------------------------------------------------------------------- #
# CTX-HONESTY
# --------------------------------------------------------------------------- #


class CtxHonestyProbe:
    """Needle-in-haystack retrieval at increasing context sizes -> ``honest_context``."""

    id = "CTX-HONESTY"
    title = "Needle retrieval at increasing context depths"
    targets: tuple[str, ...] = ("honest_context",)

    def __init__(
        self,
        *,
        sizes: Sequence[int] | None = None,
        depths: Sequence[float] | None = None,
        passcode: str = _DEFAULT_PASSCODE,
        threshold: float = _RETRIEVAL_THRESHOLD,
        floor: int = _HONEST_FLOOR,
    ) -> None:
        # Ascending, de-duplicated size ladder — the "stays >= threshold" walk assumes order.
        ladder = sizes if sizes is not None else _DEFAULT_SIZES
        self.sizes: tuple[int, ...] = tuple(sorted(set(ladder)))
        self.depths: tuple[float, ...] = tuple(depths if depths is not None else _DEFAULT_DEPTHS)
        self.passcode = passcode
        self.threshold = threshold
        self.floor = floor

    async def run(self, provider: Provider) -> ProbeResult:
        needle = f"The passcode is {self.passcode}."
        per_size: dict[int, float] = {}
        try:
            for size in self.sizes:
                hits = 0
                for depth in self.depths:
                    prompt = _haystack(size, depth, needle) + _CTX_QUESTION
                    messages = [
                        Message(role="system", content=_CTX_SYSTEM),
                        Message(role="user", content=prompt),
                    ]
                    result = await provider.complete(messages)
                    if self.passcode in (result.message.content or ""):
                        hits += 1
                per_size[size] = hits / len(self.depths) if self.depths else 0.0
        except Exception as exc:
            return ProbeResult(
                self.id,
                {},
                notes=f"provider failed during CTX-HONESTY: {type(exc).__name__}: {exc}",
                ok=False,
            )

        # honest_context = top of the contiguous passing prefix; floor if the first rung fails.
        honest = self.floor
        for size in self.sizes:
            if per_size[size] >= self.threshold:
                honest = size
            else:
                break

        detail = ", ".join(f"{s}={per_size[s]:.2f}" for s in self.sizes)
        return ProbeResult(
            self.id,
            {"honest_context": honest},
            notes=f"retrieval by size [{detail}]; honest_context={honest}",
            ok=True,
        )


# --------------------------------------------------------------------------- #
# RETENTION
# --------------------------------------------------------------------------- #


class RetentionProbe:
    """Constraint set at turn 1, adherence checked later -> retention + coherence horizon."""

    id = "RETENTION"
    title = "Constraint set at turn 1, adherence checked at later turns"
    targets: tuple[str, ...] = ("instruction_retention", "coherence_horizon")

    def __init__(
        self,
        *,
        checkpoints: Sequence[int] | None = None,
        prefix: str = _DEFAULT_PREFIX,
        total_turns: int | None = None,
    ) -> None:
        cps = checkpoints if checkpoints is not None else _DEFAULT_CHECKPOINTS
        # Ascending, de-duplicated, positive checkpoints; the horizon walk assumes order.
        self.checkpoints: tuple[int, ...] = tuple(sorted({c for c in cps if c >= 1}))
        self.prefix = prefix
        top = self.checkpoints[-1] if self.checkpoints else 0
        # Always run at least far enough to reach every checkpoint.
        self.total_turns = max(total_turns or 0, top)

    def _constraint(self) -> str:
        return (
            f"Standing instruction for the rest of this conversation: begin EVERY reply with "
            f"the exact prefix {self.prefix!r} and put nothing before it. Acknowledge and "
            f"follow it from now on."
        )

    async def run(self, provider: Provider) -> ProbeResult:
        messages: list[Message] = [Message(role="system", content=_RET_SYSTEM)]
        adherence: dict[int, bool] = {}
        try:
            for turn in range(1, self.total_turns + 1):
                prompt = self._constraint() if turn == 1 else _filler_turn(turn)
                messages.append(Message(role="user", content=prompt))
                result = await provider.complete(messages)
                content = result.message.content or ""
                # "starts with" tolerant only of leading whitespace the model may prepend.
                adherence[turn] = content.lstrip().startswith(self.prefix)
                messages.append(Message(role="assistant", content=content))
        except Exception as exc:
            return ProbeResult(
                self.id,
                {},
                notes=f"provider failed during RETENTION: {type(exc).__name__}: {exc}",
                ok=False,
            )

        held = [adherence.get(c, False) for c in self.checkpoints]
        retention = sum(held) / len(self.checkpoints) if self.checkpoints else 0.0

        # coherence_horizon = last checkpoint that held before the first drop.
        horizon = 0
        for c in self.checkpoints:
            if adherence.get(c, False):
                horizon = c
            else:
                break

        detail = ", ".join(f"t{c}={'ok' if adherence.get(c) else 'drop'}" for c in self.checkpoints)
        return ProbeResult(
            self.id,
            {"instruction_retention": retention, "coherence_horizon": horizon},
            notes=f"checkpoints [{detail}]; retention={retention:.2f}, horizon={horizon}",
            ok=True,
        )
