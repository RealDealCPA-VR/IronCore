"""IronCore — a frontier-grade terminal coding agent for open-source models.

The core thesis: open models fail at frontier tasks not because they lack
knowledge, but because typical harnesses ask them to do too many things at
once — remember the goal, track state, format tool calls, produce valid
diffs, and self-correct, simultaneously. IronCore moves every job the model
is unreliable at into deterministic code, and re-presents state instead of
trusting recall. What is left for the model is the part it is actually good
at: local reasoning over a well-framed context.
"""

__version__ = "0.3.0"
