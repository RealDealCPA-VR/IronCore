"""The Capability Envelope: measured model profiles and adapter selection.

This is IronCore's differentiator. Instead of assuming what a model can
do, IronCore probes it (envelope/probes.py) and stores a CapabilityProfile
(envelope/profile.py). The turn engine reads the profile to choose wire
protocols, edit formats, context budgets, and anchor cadence — the
"downgrade ladders" of docs/MODELS.md.
"""

from ironcore.envelope.profile import CapabilityProfile

__all__ = ["CapabilityProfile"]
