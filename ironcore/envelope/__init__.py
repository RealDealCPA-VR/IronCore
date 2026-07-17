"""The Capability Envelope: measured model profiles and adapter selection.

This is IronCore's differentiator. Instead of assuming what a model can
do, IronCore probes it (envelope/probes.py) and stores a CapabilityProfile
(envelope/profile.py). The turn engine reads the profile to choose wire
protocols, edit formats, context budgets, and anchor cadence — the
"downgrade ladders" of docs/MODELS.md.
"""

from ironcore.envelope.outcomes import OutcomeLedger, TuningResult, apply_tuning
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import render_report_card
from ironcore.envelope.suite import default_envelope_dir, default_probe_suite, probe_model

__all__ = [
    "CapabilityProfile",
    "OutcomeLedger",
    "TuningResult",
    "apply_tuning",
    "default_envelope_dir",
    "default_probe_suite",
    "probe_model",
    "render_report_card",
]
