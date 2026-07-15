"""Safety kernel: modes, risk taxonomy, policy engine, and (soon) sandbox.

Dependency rule (docs/ARCHITECTURE.md): this package imports ONLY the
standard library. Every other package may import safety; safety imports
no one. The policy tables here are the single source of truth for what
IronCore is allowed to do in each mode.
"""

from ironcore.safety.modes import CYCLE, Mode, next_mode
from ironcore.safety.policy import POLICY, Decision, decide
from ironcore.safety.risk import ToolRisk

__all__ = ["CYCLE", "Mode", "next_mode", "POLICY", "Decision", "decide", "ToolRisk"]
