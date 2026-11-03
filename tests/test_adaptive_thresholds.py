"""Adaptive fan recovery thresholds (local fork, 2026-11-03)."""
from __future__ import annotations

from custom_components.mxz_coordinator.fan_policy import (
    DEFAULT_ADAPTIVE_SETTINGS,
    FanPolicy,
    FanStage,
    RoomFanPolicy,
)


def _room() -> RoomFanPolicy:
    return RoomFanPolicy(policy=FanPolicy.ADAPTIVE)


def test_defaults() -> None:
    assert DEFAULT_ADAPTIVE_SETTINGS.medium_at == 0.75
    assert DEFAULT_ADAPTIVE_SETTINGS.high_at == 1.5
    assert DEFAULT_ADAPTIVE_SETTINGS.hysteresis == 0.3


def test_fresh_recovery_stage_by_error() -> None:
    for err, stage in ((0.5, FanStage.AUTO), (0.75, FanStage.MEDIUM),
                       (1.5, FanStage.MEDIUM), (1.6, FanStage.HIGH),
                       (2.6, FanStage.HIGH)):
        assert _room().decide(mode="heat", error_f=err, now=0.0).stage == stage


def test_room_2_6_short_stays_high_until_close() -> None:
    """Yesterday's shape: 2.6 F short must run High, not Medium."""
    room = _room()
    assert room.decide(mode="heat", error_f=2.6, now=0.0).stage == FanStage.HIGH
    # Still 1.3 F short after the dwell: inside hysteresis, stays High.
    assert room.decide(mode="heat", error_f=1.3, now=700.0).stage == FanStage.HIGH
    # Below high_at - hysteresis (1.2) after the dwell: steps to Medium.
    assert room.decide(mode="heat", error_f=1.1, now=1400.0).stage == FanStage.MEDIUM
    # Near target: hands back to native Auto immediately (no dwell).
    d = room.decide(mode="heat", error_f=0.4, now=1450.0)
    assert d.stage == FanStage.AUTO and d.driver == "head"


def test_native_auto_rooms_unaffected() -> None:
    room = RoomFanPolicy(policy=FanPolicy.NATIVE_AUTO)
    assert room.decide(mode="heat", error_f=5.0, now=0.0).stage == FanStage.AUTO
