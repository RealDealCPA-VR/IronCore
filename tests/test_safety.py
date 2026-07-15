"""The policy table is the safety contract — pin it hard."""

import pytest

from ironcore.safety import CYCLE, Decision, Mode, ToolRisk, decide, next_mode
from ironcore.safety.policy import DENYLIST_SEED, POLICY


def test_cycle_covers_all_modes_once():
    assert sorted(CYCLE) == sorted(Mode)
    assert len(CYCLE) == len(set(CYCLE))


def test_cycle_wraps():
    current = Mode.MANUAL
    seen = [current]
    for _ in range(len(CYCLE) - 1):
        current = next_mode(current)
        seen.append(current)
    assert next_mode(current) == Mode.MANUAL  # full loop
    assert seen == CYCLE


def test_policy_total_over_both_enums():
    for mode in Mode:
        for risk in ToolRisk:
            assert isinstance(decide(mode, risk), Decision)


def test_plan_mode_is_read_only():
    assert decide(Mode.PLAN, ToolRisk.READ) == Decision.ALLOW
    for risk in (ToolRisk.WRITE, ToolRisk.EXEC, ToolRisk.NET):
        assert decide(Mode.PLAN, risk) == Decision.DENY


def test_manual_asks_for_everything_mutating():
    for risk in (ToolRisk.WRITE, ToolRisk.EXEC, ToolRisk.NET):
        assert decide(Mode.MANUAL, risk) == Decision.ASK


def test_accept_edits_only_frees_writes():
    assert decide(Mode.ACCEPT_EDITS, ToolRisk.WRITE) == Decision.ALLOW
    assert decide(Mode.ACCEPT_EDITS, ToolRisk.EXEC) == Decision.ASK


@pytest.mark.parametrize("mode", list(Mode))
def test_network_is_never_auto_allowed(mode):
    assert decide(mode, ToolRisk.NET) != Decision.ALLOW


def test_reads_always_allowed():
    for mode in Mode:
        assert decide(mode, ToolRisk.READ) == Decision.ALLOW


def test_denylist_seed_has_teeth():
    assert "rm -rf /" in DENYLIST_SEED
    assert any("push --force" in entry or "push -f" in entry for entry in DENYLIST_SEED)


def test_policy_table_is_explicit_not_derived():
    # every cell written out by hand: no mode may silently inherit another's row
    assert set(POLICY) == set(Mode)
    for row in POLICY.values():
        assert set(row) == set(ToolRisk)
