"""EDIT-FORMAT + CODE-SMOKE probes (IC-604).

Drives ``EditFormatProbe`` and ``CodeSmokeProbe`` with a ``MockProvider`` that
replays scripted completions in order. The probes must:
  * score each edit format by "the IC-302 applier applies it AND the result parses"
    — a valid unified diff scores 1.0 while a broken search/replace scores 0.0;
  * treat a whole-file no-op (unchanged file) as a FAILURE, and a valid whole-file
    rewrite that parses as a pass;
  * run CODE-SMOKE by exec-ing the returned code in an isolated namespace: a correct
    function passes, a buggy one fails, syntactically-invalid code fails without
    crashing the probe;
  * be deterministic on scripted outputs and degrade to ``ok=False`` + a note on a
    provider error.
"""

import asyncio

from ironcore.envelope.probe_edits import (
    CodeSmokeProbe,
    EditFormatProbe,
    EditTrial,
    SmokeTask,
)
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import run_probes
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider, RaiseError

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

FIXTURE = "def add(a, b):\n    return a + b\n"

# A valid unified diff that applies to FIXTURE and yields parseable Python.
GOOD_DIFF = "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b + 1\n"

# A SEARCH/REPLACE block whose SEARCH text is not in FIXTURE -> applier fails.
BROKEN_SR = "<<<<<<< SEARCH\n    return a * b\n=======\n    return a - b\n>>>>>>> REPLACE\n"

# A whole-file rewrite that is valid Python and actually changes the file.
GOOD_WHOLE = "def add(a, b):\n    return a + b + 2\n"

# A whole-file reply that is syntactically-invalid Python.
BROKEN_WHOLE = "def add(a, b):\n    return a +\n"


def _text(content: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=content))


def _provider(*contents: str) -> MockProvider:
    return MockProvider(script=[_text(c) for c in contents])


def _one_trial() -> dict[str, list[EditTrial]]:
    """One trial per format, all against FIXTURE (3 provider calls, ladder order)."""
    return {
        "unified_diff": [EditTrial(FIXTURE, "add 1 to the sum")],
        "search_replace": [EditTrial(FIXTURE, "subtract instead")],
        "whole_file": [EditTrial(FIXTURE, "add 2 to the sum")],
    }


# --------------------------------------------------------------------------- #
# EDIT-FORMAT: apply-and-parse scoring
# --------------------------------------------------------------------------- #


def test_edit_probe_targets_and_id():
    probe = EditFormatProbe()
    assert probe.id == "EDIT-FORMAT"
    assert probe.targets == (
        "edit_formats.unified_diff",
        "edit_formats.search_replace",
        "edit_formats.whole_file",
    )


def test_valid_diff_scores_one_broken_sr_scores_zero_noop_whole_fails():
    # ladder order: unified_diff, search_replace, whole_file
    provider = _provider(GOOD_DIFF, BROKEN_SR, FIXTURE)  # whole_file echoes fixture -> no-op
    result = asyncio.run(EditFormatProbe(_one_trial()).run(provider))
    assert result.ok is True
    assert result.scores["edit_formats.unified_diff"] == 1.0
    assert result.scores["edit_formats.search_replace"] == 0.0
    assert result.scores["edit_formats.whole_file"] == 0.0  # no-op == failure
    # one provider call per trial
    assert len(provider.calls) == 3


def test_valid_whole_file_rewrite_scores_one():
    provider = _provider(GOOD_DIFF, BROKEN_SR, GOOD_WHOLE)
    result = asyncio.run(EditFormatProbe(_one_trial()).run(provider))
    assert result.scores["edit_formats.whole_file"] == 1.0


def test_apply_success_but_unparseable_result_fails():
    # whole_file applies fine (text differs) but the new text is not valid Python.
    provider = _provider(GOOD_DIFF, BROKEN_SR, BROKEN_WHOLE)
    result = asyncio.run(EditFormatProbe(_one_trial()).run(provider))
    assert result.scores["edit_formats.whole_file"] == 0.0


def test_fraction_over_multiple_trials():
    # unified_diff: 1 good + 1 broken -> 0.5. Other formats: single trials.
    trials = {
        "unified_diff": [EditTrial(FIXTURE, "a"), EditTrial(FIXTURE, "b")],
        "search_replace": [EditTrial(FIXTURE, "c")],
        "whole_file": [EditTrial(FIXTURE, "d")],
    }
    broken_diff = "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return NOPE\n+    return a + b + 9\n"
    provider = _provider(GOOD_DIFF, broken_diff, BROKEN_SR, GOOD_WHOLE)
    result = asyncio.run(EditFormatProbe(trials).run(provider))
    assert result.scores["edit_formats.unified_diff"] == 0.5
    assert result.scores["edit_formats.search_replace"] == 0.0
    assert result.scores["edit_formats.whole_file"] == 1.0


def test_edit_probe_deterministic():
    scripts = [GOOD_DIFF, BROKEN_SR, GOOD_WHOLE]
    first = asyncio.run(EditFormatProbe(_one_trial()).run(_provider(*scripts)))
    second = asyncio.run(EditFormatProbe(_one_trial()).run(_provider(*scripts)))
    assert first.scores == second.scores


def test_edit_probe_no_trials_scores_zero():
    result = asyncio.run(EditFormatProbe({}).run(_provider()))
    assert result.scores["edit_formats.unified_diff"] == 0.0
    assert result.scores["edit_formats.whole_file"] == 0.0
    assert "no trials" in result.notes


def test_edit_probe_provider_error_is_graceful():
    provider = MockProvider(script=[RaiseError("endpoint down")])
    result = asyncio.run(EditFormatProbe(_one_trial()).run(provider))
    assert result.ok is False
    assert "provider error" in result.notes
    assert "endpoint down" in result.notes


def test_edit_probe_default_trials_run():
    # Default fixtures: the built-in change (sum -> product) via whole_file.
    default_whole = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a * b\n'
    provider = _provider(GOOD_DIFF, BROKEN_SR, default_whole)
    result = asyncio.run(EditFormatProbe().run(provider))
    assert result.scores["edit_formats.whole_file"] == 1.0


# --------------------------------------------------------------------------- #
# EDIT-FORMAT: integration with the runner (fills the profile + drives the ladder)
# --------------------------------------------------------------------------- #


def test_edit_probe_feeds_profile_and_ladder():
    provider = _provider(GOOD_DIFF, BROKEN_SR, FIXTURE)
    profile = asyncio.run(
        run_probes(provider, [EditFormatProbe(_one_trial())], model_id="m", probed_at="t")
    )
    assert profile.edit_formats["unified_diff"] == 1.0
    assert profile.edit_formats["search_replace"] == 0.0
    # unified_diff clears its 0.90 threshold -> recommended
    assert profile.recommended_edit_format() == "unified_diff"


def test_edit_probe_error_degrades_via_runner():
    provider = MockProvider(script=[RaiseError("boom")])
    profile = asyncio.run(
        run_probes(provider, [EditFormatProbe(_one_trial())], model_id="m", probed_at="t")
    )
    # ok=False -> reliability targets degraded to 0.0 by the runner
    assert profile.edit_formats["unified_diff"] == 0.0
    assert profile.edit_formats["search_replace"] == 0.0
    assert profile.edit_formats["whole_file"] == 0.0
    assert profile.recommended_edit_format() == "whole_file"


# --------------------------------------------------------------------------- #
# CODE-SMOKE: floor gate
# --------------------------------------------------------------------------- #

_SMOKE = SmokeTask(
    docstring="Return n doubled.",
    func_name="double",
    signature="double(n)",
    checks=(((5,), 10), ((0,), 0), ((-3,), -6)),
)

GOOD_FUNC = "def double(n):\n    return n * 2\n"
BUGGY_FUNC = "def double(n):\n    return n + 2\n"
INVALID_FUNC = "def double(n)\n    return n * 2\n"  # missing colon
WRONG_NAME_FUNC = "def triple(n):\n    return n * 3\n"


def test_code_smoke_id_and_targets():
    probe = CodeSmokeProbe()
    assert probe.id == "CODE-SMOKE"
    assert probe.targets == ()  # fills no profile field


def test_code_smoke_correct_function_passes():
    result = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(GOOD_FUNC)))
    assert result.ok is True
    assert "PASS" in result.notes
    assert result.scores == {"code_smoke": 1.0}


def test_code_smoke_buggy_function_fails():
    result = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(BUGGY_FUNC)))
    assert result.ok is True  # the probe ran fine; the model failed the gate
    assert "FAIL" in result.notes
    assert result.scores == {"code_smoke": 0.0}


def test_code_smoke_invalid_syntax_fails_without_crashing():
    result = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(INVALID_FUNC)))
    assert result.ok is True
    assert "FAIL" in result.notes
    assert "did not exec" in result.notes


def test_code_smoke_missing_function_fails():
    result = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(WRONG_NAME_FUNC)))
    assert "FAIL" in result.notes
    assert "not defined" in result.notes


def test_code_smoke_runtime_error_fails():
    boom = "def double(n):\n    raise ValueError('nope')\n"
    result = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(boom)))
    assert "FAIL" in result.notes
    assert "ValueError" in result.notes


def test_code_smoke_provider_error_is_graceful():
    provider = MockProvider(script=[RaiseError("model offline")])
    result = asyncio.run(CodeSmokeProbe(_SMOKE).run(provider))
    assert result.ok is False
    assert "provider error" in result.notes
    assert "model offline" in result.notes


def test_code_smoke_deterministic():
    first = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(GOOD_FUNC)))
    second = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(GOOD_FUNC)))
    assert first.notes == second.notes
    assert first.scores == second.scores


def test_code_smoke_exec_is_isolated_from_probe_module():
    # Code that references a name only present here must NOT see it — fresh globals.
    sneaky = "def double(n):\n    return FIXTURE\n"  # FIXTURE undefined in the exec ns
    result = asyncio.run(CodeSmokeProbe(_SMOKE).run(_provider(sneaky)))
    assert "FAIL" in result.notes


def test_code_smoke_default_task_passes_with_factorial():
    fac = (
        "def factorial(n):\n    r = 1\n    for i in range(2, n + 1):\n"
        "        r *= i\n    return r\n"
    )
    result = asyncio.run(CodeSmokeProbe().run(_provider(fac)))
    assert "PASS" in result.notes


def test_code_smoke_score_not_merged_into_profile():
    # The synthetic code_smoke score must not land on any real profile field.
    provider = _provider(GOOD_FUNC)
    profile = asyncio.run(
        run_probes(provider, [CodeSmokeProbe(_SMOKE)], model_id="m", probed_at="t")
    )
    baseline = CapabilityProfile(model_id="m")
    assert profile.tool_protocols == baseline.tool_protocols
    assert profile.edit_formats == baseline.edit_formats
    assert profile.json_adherence == baseline.json_adherence
