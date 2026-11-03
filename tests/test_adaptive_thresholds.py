"""Adaptive fan recovery thresholds and Very High stage (local fork, 2026-11-03)."""
from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.fan_policy import (
    DEFAULT_ADAPTIVE_SETTINGS,
    AdaptiveSettings,
    FanDecision,
    FanPolicy,
    FanStage,
    RoomFanPolicy,
    supported_fan,
)

HEAD_MODES = ["auto", "low", "medium", "high", "quiet", "Very High"]


def _room() -> RoomFanPolicy:
    return RoomFanPolicy(policy=FanPolicy.ADAPTIVE)


def test_defaults() -> None:
    s = DEFAULT_ADAPTIVE_SETTINGS
    assert (s.medium_at, s.high_at, s.very_high_at, s.hysteresis) == (0.75, 1.5, 2.5, 0.3)


@pytest.mark.parametrize(
    ("err", "stage"),
    [(0.5, FanStage.AUTO), (0.75, FanStage.MEDIUM), (1.5, FanStage.MEDIUM),
     (1.6, FanStage.HIGH), (2.5, FanStage.HIGH), (2.6, FanStage.VERY_HIGH),
     (5.0, FanStage.VERY_HIGH)],
)
def test_fresh_recovery_stage_by_error(err: float, stage: FanStage) -> None:
    assert _room().decide(mode="heat", error_f=err, now=0.0).stage == stage


def test_cold_morning_ladder_down() -> None:
    """2.6 F short -> Very High; eases down through High and Medium to Auto."""
    room = _room()
    assert room.decide(mode="heat", error_f=2.6, now=0.0).stage == FanStage.VERY_HIGH
    # 2.3: inside Very High hysteresis (2.2) -> stays.
    assert room.decide(mode="heat", error_f=2.3, now=700.0).stage == FanStage.VERY_HIGH
    assert room.decide(mode="heat", error_f=2.1, now=1400.0).stage == FanStage.HIGH
    assert room.decide(mode="heat", error_f=1.3, now=2100.0).stage == FanStage.HIGH
    assert room.decide(mode="heat", error_f=1.1, now=2800.0).stage == FanStage.MEDIUM
    d = room.decide(mode="heat", error_f=0.4, now=2850.0)  # Auto bypasses dwell
    assert d.stage == FanStage.AUTO and d.driver == "head"


def test_step_up_needs_hysteresis_and_dwell() -> None:
    room = _room()
    assert room.decide(mode="heat", error_f=1.6, now=0.0).stage == FanStage.HIGH
    # 2.7 > 2.5 but not > 2.8: stays High.
    assert room.decide(mode="heat", error_f=2.7, now=700.0).stage == FanStage.HIGH
    # 3.0 > 2.8 but inside the 600 s dwell from the last change -> held.
    room2 = _room()
    room2.decide(mode="heat", error_f=1.6, now=0.0)
    assert room2.decide(mode="heat", error_f=3.0, now=100.0).stage == FanStage.HIGH
    assert room2.decide(mode="heat", error_f=3.0, now=700.0).stage == FanStage.VERY_HIGH


def test_very_high_maps_to_head_spelling() -> None:
    d = _room().decide(mode="heat", error_f=3.0, now=0.0)
    out = supported_fan(d, HEAD_MODES)
    assert out.selected == "Very High" and out.driver == "coordinator"


def test_head_without_very_high_falls_back_to_auto() -> None:
    d = FanDecision("very high", "coordinator", FanStage.VERY_HIGH)
    out = supported_fan(d, ["auto", "low", "medium", "high"])
    assert out.selected == "auto" and out.diagnostic == "fan_mode_unsupported"


def test_native_auto_rooms_unaffected() -> None:
    room = RoomFanPolicy(policy=FanPolicy.NATIVE_AUTO)
    assert room.decide(mode="heat", error_f=5.0, now=0.0).stage == FanStage.AUTO


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        AdaptiveSettings(very_high_at=1.6)  # band overlaps High
