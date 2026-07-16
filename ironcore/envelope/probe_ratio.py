"""TOKEN-RATIO probe (MS-1): measure the model's real chars-per-token ratio.

One member of the probe suite (declared in ``probes.py``, run by the IC-601 runner in
``runner.py``). Follows the ``Probe`` protocol: ``id`` / ``title`` / ``targets`` and an
``async run(provider) -> ProbeResult``.

Why: the composer's ``estimate_tokens`` defaults to the universal chars/4 guess. Real
tokenizers vary (dense code ~3 chars/token, prose ~4-5), so the budget math can misjudge
the very window CTX-HONESTY measured. This probe sends filler documents whose exact
character count the harness knows and reads back the server-reported ``prompt_tokens``
(fallback ``input_tokens``) from ``CompletionResult.usage`` — the same real count
``Budget.record_call`` already trusts. ratio = total chars sent / total prompt tokens,
clamped to [1.0, 8.0].

Honesty rules (SPEC §4.1 spirit):
  * Mechanical scoring only — arithmetic over server-reported usage; no LLM judge.
  * Deterministic + offline-testable. Filler is the same fixed-vocab style as
    ``probe_ctx``; no randomness anywhere.
  * ``chars_per_token`` is NOT a reliability: many OpenAI-compatible servers simply
    omit usage on completions. When no call reports usage > 0 the probe returns
    ``ok=True`` with EMPTY scores — an omitted non-reliability score keeps the profile's
    base value (4.0), which is the designed-for outcome, not a failure. A provider
    exception follows the existing ``ok=False`` degrade path, which also leaves the
    field at base because it is not in ``_RELIABILITY_ROOTS``.
"""

from __future__ import annotations

from collections.abc import Sequence

from ironcore.envelope.runner import ProbeResult
from ironcore.providers.base import Message, Provider

#: Filler-document sizes in vocab words — small/medium/large so tokenizer behavior
#: over repeated short tokens averages out. Injectable for fast tests.
_DEFAULT_SIZES: tuple[int, ...] = (512, 1024, 2048)

#: Ratio clamp: below 1 char/token or above 8 chars/token means the server's usage
#: numbers are nonsense for budget math — refuse to store them.
_RATIO_MIN = 1.0
_RATIO_MAX = 8.0

_RATIO_SYSTEM = "Reply with the single word OK. Do not repeat the document."


def _filler(n: int) -> str:
    """``n`` deterministic filler words (same fixed cycling vocab as probe_ctx —
    no randomness, so a given size always produces the same document)."""
    return " ".join(f"tok{i % 128:03d}" for i in range(max(0, n)))


def _prompt_tokens(usage: dict[str, int]) -> int:
    """Server-reported prompt-side token count; 0 when the server omits usage."""
    for key in ("prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


class TokenRatioProbe:
    """Known-char filler docs vs server-reported prompt tokens -> ``chars_per_token``."""

    id = "TOKEN-RATIO"
    title = "Measured chars-per-token from server-reported prompt usage"
    targets: tuple[str, ...] = ("chars_per_token",)

    def __init__(self, *, sizes: Sequence[int] | None = None) -> None:
        self.sizes: tuple[int, ...] = tuple(sizes if sizes is not None else _DEFAULT_SIZES)

    async def run(self, provider: Provider) -> ProbeResult:
        total_chars = 0
        total_tokens = 0
        reported = 0
        try:
            for size in self.sizes:
                messages = [
                    Message(role="system", content=_RATIO_SYSTEM),
                    Message(role="user", content=_filler(size)),
                ]
                result = await provider.complete(messages)
                tokens = _prompt_tokens(result.usage)
                if tokens <= 0:
                    continue  # this server omitted usage on this call — skip honestly
                total_chars += sum(len(m.content) for m in messages)
                total_tokens += tokens
                reported += 1
        except Exception as exc:
            return ProbeResult(
                self.id,
                {},
                notes=f"provider failed during TOKEN-RATIO: {type(exc).__name__}: {exc}",
                ok=False,
            )

        if total_tokens <= 0:
            # No usage anywhere: keep the 4.0 base. ok=True with empty scores is the
            # honest "measured nothing" result — NOT a degrade (non-reliability field).
            return ProbeResult(
                self.id,
                {},
                notes="no usage reported by the server; keeping default 4.0 chars/token",
                ok=True,
            )

        ratio = max(_RATIO_MIN, min(_RATIO_MAX, total_chars / total_tokens))
        return ProbeResult(
            self.id,
            {"chars_per_token": ratio},
            notes=(
                f"{reported}/{len(self.sizes)} trials reported usage; "
                f"{total_chars} chars / {total_tokens} prompt tokens "
                f"-> {ratio:.2f} chars/token (clamped to [1.0, 8.0])"
            ),
            ok=True,
        )
