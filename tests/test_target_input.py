"""Value matching and candidate persistence, independently of command age."""

from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.target_input import TARGET_SETTLE_S, TargetInput


@pytest.mark.parametrize("offset", range(1, 61))
def test_four_second_blip_is_not_stable_at_any_command_offset(offset):
    journal = TargetInput()
    journal.command("heat", 72)
    assert journal.observe("heat", 71.5, offset, 1) == "candidate"
    assert journal.observe("heat", 71.5, offset + 3.99, 1) == "candidate"
    assert journal.observe("heat", 72, offset + 4, 1) == "echo"
    assert journal.candidate is None


@pytest.mark.parametrize("offset", [0, 1, 6, 10.1, 15, 30, 60, 3600, 86400])
def test_real_press_settles_after_its_own_hold_not_command_age(offset):
    journal = TargetInput()
    journal.command("heat", 72)
    assert journal.observe("heat", 70, offset, 1) == "candidate"
    assert journal.observe("heat", 70, offset + TARGET_SETTLE_S - 0.01, 1) == "candidate"
    assert journal.observe("heat", 70, offset + TARGET_SETTLE_S, 1) == "stable"


@pytest.mark.parametrize("offset", [1, 6, 10.1, 15, 30, 60])
def test_overridden_press_is_never_stable(offset):
    journal = TargetInput()
    journal.command("heat", 72)
    assert journal.observe("heat", 70, offset, 1) == "candidate"
    journal.command("heat", 73)
    assert journal.candidate is None
    assert journal.observe("heat", 73, offset + 2, 2) == "echo"


def test_known_native_equivalent_is_echo_indefinitely():
    journal = TargetInput()
    journal.command("heat", 72, 71.5)
    for now in (1, 10, 10.1, 15.1, 60, 3600, 86400):
        assert journal.observe("heat", 71.5, now, 0) == "echo"
        assert journal.candidate is None


def test_new_candidate_value_and_new_canonical_generation_restart_hold():
    journal = TargetInput()
    journal.command("heat", 72)
    assert journal.observe("heat", 70, 0, 1) == "candidate"
    assert journal.observe("heat", 69, 14, 1) == "candidate"
    assert journal.observe("heat", 69, 15, 1) == "candidate"
    assert journal.observe("heat", 69, 29, 1) == "stable"
    assert journal.observe("heat", 69, 30, 2) == "candidate"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_invalid_target_cancels_candidate(value):
    journal = TargetInput()
    journal.command("heat", 72)
    journal.observe("heat", 70, 0, 1)
    assert journal.observe("heat", value, 1, 1) == "invalid"
    assert journal.candidate is None


@pytest.mark.parametrize("offset", [*range(61), 86400])
@pytest.mark.parametrize("elapsed", [14.999, 15.0, 15.001])
def test_exact_settle_boundary_and_convergence_cancel(offset, elapsed):
    journal = TargetInput()
    journal.command("heat", 72)
    assert journal.observe("heat", 70, offset, 1) == "candidate"
    expected = "stable" if elapsed >= TARGET_SETTLE_S else "candidate"
    assert journal.observe("heat", 70, offset + elapsed, 1) == expected
    assert journal.observe("heat", 72, offset + elapsed, 1) == "echo"
    assert journal.candidate is None
