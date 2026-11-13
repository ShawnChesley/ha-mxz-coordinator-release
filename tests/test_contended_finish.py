"""Contended finish: a tight-demand room another zone is waiting on finishes hard.

Deeper cool bias (2 steps), a fan floor (top stage, capped at High in quiet
hours), a per-engagement latch, and no change at all when nothing is waiting.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import MODE_COOL, MODE_HEAT
from custom_components.mxz_coordinator.fan_control import FanController
from custom_components.mxz_coordinator.fan_policy import (
    FanPolicy,
    FanStage,
    RoomFanPolicy,
)
from custom_components.mxz_coordinator.logic import (
    head_setpoint_target,
    quiet_hours_active,
)

from .test_cool_floor_single_setpoint import FloorRangeHead
from .test_single_setpoint import _enable, _set_temp, _setup

# ── head_setpoint_target ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("contended", "celsius", "expected"),
    [
        (False, False, 62.0),
        (True, False, 61.0),
        (False, True, 62.5),
        (True, True, 62.0),
    ],
)
def test_contended_doubles_the_cool_bias(contended, celsius, expected):
    assert (
        head_setpoint_target(
            target=63.0, mode=MODE_COOL, tight_demand=True, eco=False,
            emergency=False, celsius=celsius, contended=contended,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("mode", "tight", "eco", "emergency"),
    [
        (MODE_HEAT, True, False, False),
        (MODE_COOL, False, False, False),
        (MODE_COOL, True, True, False),
        (MODE_COOL, True, False, True),
    ],
)
def test_contended_respects_existing_exclusions(mode, tight, eco, emergency):
    assert (
        head_setpoint_target(
            target=63.0, mode=mode, tight_demand=tight, eco=eco,
            emergency=emergency, celsius=False, contended=True,
        )
        == 63.0
    )


# ── quiet_hours_active ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "start", "end", "expected"),
    [
        (20 * 60, "19:30", "07:00", True),   # evening, spans midnight
        (2 * 60, "19:30", "07:00", True),    # middle of the night
        (7 * 60, "19:30", "07:00", False),   # end is exclusive
        (12 * 60, "19:30", "07:00", False),  # midday
        (10 * 60, "09:00", "11:00", True),   # same-day window
        (11 * 60, "09:00", "11:00", False),
        (19 * 60 + 30, "19:30:00", "07:00:00", True),  # time-selector seconds
        (0, "19:30", "19:30", False),        # equal bounds: disabled
        (0, "oops", "07:00", False),         # unparsable: disabled
        (0, "25:00", "07:00", False),
    ],
)
def test_quiet_hours_windows(now, start, end, expected):
    assert quiet_hours_active(now, start, end) is expected


# ── fan_policy floor ────────────────────────────────────────────────────


def test_floor_raises_taper_but_never_lowers():
    room = RoomFanPolicy(policy=FanPolicy.ADAPTIVE)
    # 3 F short: adaptive alone picks Very High; a High floor must not lower it.
    d = room.decide(mode="cool", error_f=3.0, now=0.0, floor=FanStage.HIGH)
    assert d.stage == FanStage.VERY_HIGH
    # Tail at 0.5 F short: adaptive would drop to Auto; the floor holds High.
    d = room.decide(mode="cool", error_f=0.5, now=700.0, floor=FanStage.HIGH)
    assert d.stage == FanStage.HIGH
    assert d.diagnostic == "contended_floor"
    # Uncapped contention floors at the top stage.
    d = room.decide(mode="cool", error_f=0.5, now=1400.0, floor=FanStage.VERY_HIGH)
    assert d.stage == FanStage.VERY_HIGH
    # Contention over: the ladder is free to taper again (0.4 clears the
    # 0.75 - 0.3 hysteresis edge and steps all the way down to Auto).
    d = room.decide(mode="cool", error_f=0.4, now=2100.0)
    assert d.stage == FanStage.AUTO


def test_floor_bypasses_step_down_dwell():
    room = RoomFanPolicy(policy=FanPolicy.ADAPTIVE)
    room.decide(mode="cool", error_f=3.0, now=0.0)  # Very High
    # 10 s later the error collapses; dwell would hold Very High, which is fine
    # (floor never lowers) — but from Medium, a floor raise is immediate.
    room2 = RoomFanPolicy(policy=FanPolicy.ADAPTIVE)
    room2.decide(mode="cool", error_f=1.0, now=0.0)  # Medium
    d = room2.decide(mode="cool", error_f=1.0, now=10.0, floor=FanStage.HIGH)
    assert d.stage == FanStage.HIGH


def test_floor_leaves_holds_native_and_satisfied_alone():
    held = RoomFanPolicy(policy=FanPolicy.ADAPTIVE, hold_speed="low")
    assert held.decide(mode="cool", error_f=0.5, now=0.0, floor=FanStage.HIGH).driver == "hold"
    native = RoomFanPolicy(policy=FanPolicy.NATIVE_AUTO)
    assert native.decide(mode="cool", error_f=3.0, now=0.0, floor=FanStage.HIGH).stage == FanStage.AUTO
    sat = RoomFanPolicy(policy=FanPolicy.ADAPTIVE)
    assert sat.decide(mode="cool", error_f=0.5, now=0.0, satisfied=True, floor=FanStage.HIGH).stage == FanStage.AUTO


# ── fan_control passes the floor ───────────────────────────────────────


class _Seen(Exception):
    pass


async def _floor_passed(hass: HomeAssistant, plan_extra: dict[str, Any]):
    cid = "climate.contended_head"
    hass.states.async_set(cid, "cool", {"fan_modes": ["auto", "low", "medium", "high", "Very High"]})
    fc = FanController(hass, SimpleNamespace(celsius=False, zones=[]), "contended")
    head = fc.head(cid)
    head.seeded = True
    head.policy = FanPolicy.ADAPTIVE.value
    head.room.set_policy(FanPolicy.ADAPTIVE)
    fc.protection = lambda zone, act: None
    seen = {}
    real = head.room.decide

    def spy(**kw):
        seen["floor"] = kw.get("floor")
        seen["decision"] = real(**kw)
        raise _Seen

    head.room.decide = spy
    zone = SimpleNamespace(climate_id=cid, slug="primary")
    plan = {"primary_temp": 63.5, "primary_temp_ok": True, **plan_extra}
    with pytest.raises(_Seen):
        await fc.apply(zone, MODE_COOL, plan, eco=False, handback=False, target=63.0)
    return seen


async def test_apply_floors_top_stage_when_contended(hass: HomeAssistant):
    seen = await _floor_passed(hass, {"primary_contended": True})
    assert seen["floor"] == FanStage.VERY_HIGH
    assert seen["decision"].stage == FanStage.VERY_HIGH


async def test_apply_caps_floor_at_high_in_quiet_hours(hass: HomeAssistant):
    seen = await _floor_passed(hass, {"primary_contended": True, "quiet_hours": True})
    assert seen["floor"] == FanStage.HIGH
    assert seen["decision"].stage == FanStage.HIGH


async def test_apply_passes_no_floor_uncontended(hass: HomeAssistant):
    seen = await _floor_passed(hass, {"quiet_hours": True})
    assert seen["floor"] is None


# ── coordinator integration ────────────────────────────────────────────


async def _contended_setup(hass: HomeAssistant):
    """Zone a: tight, cooling (target 63, room 67). Zone b: wants heat."""
    heads = [FloorRangeHead("a"), FloorRangeHead("b")]
    entry = await _setup(hass, heads)
    await _enable(hass, entry)
    coord = entry.runtime_data
    coord.zones[0].tight_demand = True
    coord.zones[0].target = 63.0
    coord.reset_engage_latch(coord.zones[0].slug)
    await _set_temp(hass, "sensor.room_a_temp", 67.0)
    await _set_temp(hass, "sensor.room_b_temp", 70.0)  # near b's target: neutral
    await coord.async_refresh()
    await hass.async_block_till_done()
    return heads, coord


def _cool_sp(head: FloorRangeHead):
    for c in reversed(head.calls):
        v = c.get("temperature", c.get("target_temp_high"))
        if v is not None:
            return v
    return None


async def test_waiting_heat_demand_deepens_bias_and_latches(hass: HomeAssistant):
    heads, coord = await _contended_setup(hass)
    assert coord.data["state"] == MODE_COOL
    assert _cool_sp(heads[0]) == 62  # one-step bias, nobody waiting
    assert not coord._contended
    # Zone b goes cold enough to demand heat -> contention.
    await _set_temp(hass, "sensor.room_b_temp", 60.0)
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert coord.data["state"] == MODE_COOL  # priority hold: a finishes first
    assert coord.data.get("primary_contended") is True
    assert _cool_sp(heads[0]) == 61  # two-step bias
    # The waiter's demand flickers off: the latch holds while a is engaged.
    await _set_temp(hass, "sensor.room_b_temp", 70.0)
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert "primary" in coord._contended
    assert _cool_sp(heads[0]) == 61
    # Zone a reaches target -> releases; contention clears with it.
    await _set_temp(hass, "sensor.room_a_temp", 62.9)
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert "primary" not in coord._contended


async def test_no_competition_keeps_single_step_bias(hass: HomeAssistant):
    heads, coord = await _contended_setup(hass)
    for _ in range(2):
        await coord.async_refresh()
        await hass.async_block_till_done()
    assert not coord._contended
    assert _cool_sp(heads[0]) == 62
