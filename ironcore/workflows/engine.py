"""Workflow engine — TODO IC-901..IC-903.

Workflows are YAML files in .ironcore/workflows/ describing deterministic
orchestration over subagents. The MODEL never controls the orchestration;
the harness does — this matters twice as much for small models, whose
long-horizon planning is exactly the thing we do not trust.

Schema sketch (finalized in IC-902, then frozen in docs/CONTRACTS.md):

    name: review
    description: review the working diff across dimensions
    inputs: [diff_ref]
    phases:
      - id: find
        fanout:
          items: [bugs, security, performance]
          agent:
            role: reviewer
            prompt: |
              Review {{diff_ref}} for {{item}} issues only. ...
            output_schema: findings.v1
      - id: verify
        foreach: "{{find.findings}}"
        agent:
          role: verifier
          prompt: "Adversarially verify: {{item.title}} ..."
      - id: report
        reduce: markdown_table

Every subagent runs with a FRESH context composed for the task and sized
to the model's envelope (this is the whole point: many small well-framed
contexts beat one long drifting one).
"""

from __future__ import annotations


class WorkflowRunner:
    """Ships in IC-903."""

    def __init__(self) -> None:
        raise NotImplementedError("IC-903: workflow orchestrator (see TODO.md)")
