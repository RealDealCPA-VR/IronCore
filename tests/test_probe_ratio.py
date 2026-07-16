"""TOKEN-RATIO probe (MS-1): measured chars-per-token from server-reported usage.

All offline (MockProvider only). Pins: the ratio is recomputed from exactly what the
provider RECEIVED (MockProvider.calls) vs the scripted usage; the clamp bounds; the
no-usage path keeps the 4.0 default with ok=True (non-reliability, honestly unmeasured);
and a provider failure also leaves the base value — never a degraded 0.0 ratio.
"""

import asyncio

import pytest

from ironcore.envelope.probe_ratio import TokenRatioProbe
from ironcore.envelope.runner import evaluate_probes, run_probes
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider, RaiseError


def _ok(usage: dict | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content="OK"), usage=dict(usage or {})
    )


def _run(probe: TokenRatioProbe, provider: MockProvider):
    return asyncio.run(probe.run(provider))


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


def test_measures_ratio_from_reported_usage():
    provider = MockProvider([_ok({"prompt_tokens": 2000}) for _ in range(3)])
    probe = TokenRatioProbe()
    result = _run(probe, provider)

    assert result.ok is True
    assert len(provider.calls) == 3
    # recompute the expectation from what the provider actually received
    sent_chars = sum(len(m.content) for call in provider.calls for m in call)
    expected = sent_chars / 6000
    assert 1.0 < expected < 8.0  # the fixture must not sit on a clamp edge
    assert result.scores["chars_per_token"] == pytest.approx(expected)


def test_input_tokens_fallback_key_is_honored():
    provider = MockProvider([_ok({"input_tokens": 2000}) for _ in range(3)])
    result = _run(TokenRatioProbe(), provider)
    sent_chars = sum(len(m.content) for call in provider.calls for m in call)
    assert result.scores["chars_per_token"] == pytest.approx(sent_chars / 6000)


def test_probe_declares_its_target():
    probe = TokenRatioProbe()
    assert probe.id == "TOKEN-RATIO"
    assert tuple(probe.targets) == ("chars_per_token",)


# --------------------------------------------------------------------------- #
# Clamping
# --------------------------------------------------------------------------- #


def test_ratio_clamped_to_upper_bound():
    # 1 token per huge document -> absurd ratio -> clamped to 8.0
    provider = MockProvider([_ok({"prompt_tokens": 1}) for _ in range(3)])
    result = _run(TokenRatioProbe(), provider)
    assert result.scores["chars_per_token"] == 8.0


def test_ratio_clamped_to_lower_bound():
    # more tokens than characters -> nonsense ratio -> clamped to 1.0
    provider = MockProvider([_ok({"prompt_tokens": 10_000_000}) for _ in range(3)])
    result = _run(TokenRatioProbe(), provider)
    assert result.scores["chars_per_token"] == 1.0


# --------------------------------------------------------------------------- #
# No usage / partial usage
# --------------------------------------------------------------------------- #


def test_no_usage_reported_keeps_default_honestly():
    provider = MockProvider([_ok({}) for _ in range(3)])
    result = _run(TokenRatioProbe(), provider)
    assert result.ok is True  # not a failure — the server just doesn't report usage
    assert result.scores == {}  # omitted non-reliability score keeps the base value
    assert "keeping default 4.0" in result.notes

    # and through the runner: the profile field stays at the 4.0 default
    profile = asyncio.run(
        run_probes(
            MockProvider([_ok({}) for _ in range(3)]),
            [TokenRatioProbe()],
            model_id="m",
            probed_at="t",
        )
    )
    assert profile.chars_per_token == 4.0


def test_partial_usage_uses_only_reporting_trials():
    # middle trial omits usage: ratio comes from trials 1+3 only
    provider = MockProvider(
        [_ok({"prompt_tokens": 500}), _ok({}), _ok({"prompt_tokens": 2500})]
    )
    result = _run(TokenRatioProbe(), provider)
    reporting = [provider.calls[0], provider.calls[2]]
    sent_chars = sum(len(m.content) for call in reporting for m in call)
    assert result.scores["chars_per_token"] == pytest.approx(sent_chars / 3000)
    assert "2/3 trials reported usage" in result.notes


# --------------------------------------------------------------------------- #
# Provider failure: base kept, never degraded to 0.0
# --------------------------------------------------------------------------- #


def test_provider_failure_reports_ok_false():
    results = asyncio.run(
        evaluate_probes(MockProvider([RaiseError(message="endpoint down")]), [TokenRatioProbe()])
    )
    assert len(results) == 1
    assert results[0].ok is False
    # chars_per_token is NOT a reliability: the degrade path must not zero it
    assert results[0].scores == {}


def test_provider_failure_leaves_profile_at_base():
    profile = asyncio.run(
        run_probes(
            MockProvider([RaiseError(message="endpoint down")]),
            [TokenRatioProbe()],
            model_id="m",
            probed_at="t",
        )
    )
    assert profile.chars_per_token == 4.0


def test_measured_ratio_lands_on_the_profile_via_runner():
    provider = MockProvider([_ok({"prompt_tokens": 2000}) for _ in range(3)])
    profile = asyncio.run(
        run_probes(provider, [TokenRatioProbe()], model_id="m", probed_at="t")
    )
    sent_chars = sum(len(m.content) for call in provider.calls for m in call)
    assert profile.chars_per_token == pytest.approx(sent_chars / 6000)
    assert profile.chars_per_token != 4.0  # the measurement actually moved the field
    assert profile.source == "probed"


def test_injectable_sizes_shrink_the_battery():
    provider = MockProvider([_ok({"prompt_tokens": 100})])
    probe = TokenRatioProbe(sizes=(64,))
    result = _run(probe, provider)
    assert len(provider.calls) == 1
    sent_chars = sum(len(m.content) for m in provider.calls[0])
    assert result.scores["chars_per_token"] == pytest.approx(
        max(1.0, min(8.0, sent_chars / 100))
    )
