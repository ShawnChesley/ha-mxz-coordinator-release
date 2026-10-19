"""Priority hold (local fork, 2026-10-18).

Requirement (Master Bedroom): at bedtime Master cools to 64 F. If Master wants
cold, every other room waits until Master is satisfied; once Master is cold
enough, the others get heat or cold. Emergency heat (a room below its floor)
still overrides, for safety.

Before this, a room only counted as "calling" while it was more than the demand
threshold (3 F) off target. A higher-priority room still cooling from 66 to 64
was back inside that threshold, so a lower-priority room's heat demand flipped
the shared outdoor unit and stranded the bedroom around 66-67.
"""

from __future__ import annotations

from custom_components.mxz_coordinator.const import (
    DEMAND_NEUTRAL,
    ENGAGE_SATISFIED,
    MODE_COOL,
    MODE_HEAT,
)
from custom_components.mxz_coordinator.logic import priority_hold, shared_mode

N, S = DEMAND_NEUTRAL, ENGAGE_SATISFIED


# --- pure logic --------------------------------------------------------------
def test_running_top_room_holds_against_lower_room_heat_demand() -> None:
    # Master (0) still cooling (engaged) but back inside the demand threshold;
    # Kitchen (2) demands heat.
    assert shared_mode(
        demands=[N, N, MODE_HEAT, N], current=MODE_COOL, allowed=True,
        engaged=[MODE_COOL, S, MODE_HEAT, S],
    ) == MODE_COOL


def test_flip_happens_once_top_room_is_satisfied() -> None:
    assert shared_mode(
        demands=[N, N, MODE_HEAT, N], current=MODE_COOL, allowed=True,
        engaged=[S, S, MODE_HEAT, S],
    ) == MODE_HEAT


def test_higher_priority_opposite_demand_still_wins() -> None:
    # Ducted (3) running cool cannot hold off Master (0) demanding heat.
    assert shared_mode(
        demands=[MODE_HEAT, N, N, N], current=MODE_COOL, allowed=True,
        engaged=[MODE_HEAT, S, S, MODE_COOL],
    ) == MODE_HEAT


def test_emergency_heat_overrides_the_hold() -> None:
    assert shared_mode(
        demands=[N, N, N, N, MODE_HEAT], current=MODE_COOL, allowed=True,
        engaged=[MODE_COOL, S, S, S, MODE_HEAT], emergency_heat=True,
    ) == MODE_HEAT


def test_resting_bias_never_interrupts_a_running_room() -> None:
    assert shared_mode(
        demands=[N, N], current=MODE_COOL, allowed=True, resting=MODE_HEAT,
        engaged=[MODE_COOL, S],
    ) == MODE_COOL
    assert shared_mode(
        demands=[N, N], current=MODE_COOL, allowed=True, resting=MODE_HEAT,
        engaged=[S, S],
    ) == MODE_HEAT


def test_without_engaged_behaviour_is_unchanged() -> None:
    assert shared_mode(
        demands=[N, N, MODE_HEAT], current=MODE_COOL, allowed=True,
    ) == MODE_HEAT


def test_hysteresis_still_gates() -> None:
    assert shared_mode(
        demands=[N, MODE_HEAT], current=MODE_COOL, allowed=False, engaged=[S, MODE_HEAT],
    ) == MODE_COOL


def test_priority_hold_helper() -> None:
    assert priority_hold(
        demands=[N, MODE_HEAT], engaged=[MODE_COOL, MODE_HEAT],
        current=MODE_COOL, proposed=MODE_HEAT,
    )
    assert not priority_hold(
        demands=[MODE_HEAT, N], engaged=[MODE_HEAT, MODE_COOL],
        current=MODE_COOL, proposed=MODE_HEAT,
    )


# --- integration (requires the HA test harness) -------------------------------
import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import CONF_MODE_HYSTERESIS
from tests.test_climate import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _enable,
    _recompute,
    _set_temp,
)
from tests.test_climate import _setup as _climate_setup
from tests.test_direction import _set_mode


async def test_bedtime_master_cools_to_64_before_others_heat(hass: HomeAssistant) -> None:
    """Tonight's shape: Master cool 64 (exempt, cool lockout on), other room wants heat."""
    entry, head_a, head_b = await _climate_setup(hass, **{CONF_MODE_HYSTERESIS: 0})
    await _enable(
        hass, entry, "_coordinator_enable", "_secondary_enable",
        "_cool_lockout", "_primary_cool_exempt",
    )
    prim = _eid(hass, entry, "_primary_thermostat")
    sec = _eid(hass, entry, "_secondary_thermostat")
    await _set_mode(hass, sec, hvac_mode="heat", temperature=64)
    await _set_temp(hass, SENSOR_B, 64)
    await _set_mode(hass, prim, hvac_mode="cool", temperature=64)

    # Bedtime: Master at 70 calls for cooling and wins.
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["state"] == "cool"
    assert hass.states.get(head_a).state == "cool"

    # Master at 66: inside the 3 F demand threshold but still running. The
    # other room now demands heat (60 < 64 - 3) -- it must wait.
    await _set_temp(hass, SENSOR_A, 66)
    await _set_temp(hass, SENSOR_B, 60)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["primary_demand"] == "neutral"
    assert plan["primary_engage"] == "cool"
    assert plan["secondary_demand"] == "heat"
    assert plan["state"] == "cool"
    assert plan["priority_hold"] is True
    assert hass.states.get(head_a).state == "cool"
    assert hass.states.get(head_b).state != "heat"

    # Master reaches 64 -> satisfied -> the other room gets heat.
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["primary_engage"] != "cool"
    assert plan["state"] == "heat"
    assert plan["priority_hold"] is False
    assert hass.states.get(head_b).state == "heat"
    assert hass.states.get(head_a).state != "cool"


# --- bounded hold (3.4.2 review finding F-1) ---------------------------------
from custom_components.mxz_coordinator.const import (
    CONF_EXEMPT_COOL_STOP_OFFSET,
    PRIORITY_HOLD_MAX_S,
)


async def _bedtime_hold(hass: HomeAssistant, master_temp: float):
    """Master cooling toward 64 (exempt, lockout on, offset 0), other room cold."""
    entry, head_a, head_b = await _climate_setup(
        hass, **{CONF_MODE_HYSTERESIS: 0, CONF_EXEMPT_COOL_STOP_OFFSET: 0.0}
    )
    await _enable(
        hass, entry, "_coordinator_enable", "_secondary_enable",
        "_cool_lockout", "_primary_cool_exempt",
    )
    await _set_mode(hass, _eid(hass, entry, "_secondary_thermostat"), hvac_mode="heat", temperature=68)
    await _set_temp(hass, SENSOR_B, 68)
    await _set_mode(hass, _eid(hass, entry, "_primary_thermostat"), hvac_mode="cool", temperature=64)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["state"] == "cool"
    await _set_temp(hass, SENSOR_A, master_temp)
    await _set_temp(hass, SENSOR_B, 63)  # 5 F below its 68 target
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    return entry, head_a, head_b


async def test_offline_holding_head_does_not_hold_the_house(hass: HomeAssistant) -> None:
    entry, head_a, head_b = await _bedtime_hold(hass, 66)
    assert entry.runtime_data.data["priority_hold"] is True
    # Master's indoor unit drops off the network mid-run.
    hass.states.async_set(head_a, "unavailable", hass.states.get(head_a).attributes)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["primary_engage"] == "cool"  # the latch itself is unchanged
    assert plan["priority_hold"] is False
    assert plan["state"] == "heat"
    assert hass.states.get(head_b).state == "heat"


async def test_hold_that_cannot_finish_expires(hass: HomeAssistant, caplog) -> None:
    # Room sensor parked just above target while the head idles on its own
    # thermistor: the run never completes.
    entry, _head_a, head_b = await _bedtime_hold(hass, 64.3)
    coord = entry.runtime_data
    plan = coord.data
    assert plan["priority_hold"] is True
    assert plan["priority_hold_since"] is not None
    assert plan["priority_hold_expired"] is False

    # Inside the window the hold stands.
    coord._priority_hold_since -= PRIORITY_HOLD_MAX_S - 60
    coord._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    assert coord.data["state"] == "cool"
    assert coord.data["priority_hold"] is True

    # Past the window it releases and the cold room gets heat.
    coord._priority_hold_since -= 120
    coord._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = coord.data
    # (priority_hold_expired marks the releasing compute itself; the follow-up
    # refresh after the flip already sees a plain heat plan.)
    assert "priority hold released after" in caplog.text
    assert plan["priority_hold"] is False
    assert plan["state"] == "heat"
    assert coord._priority_hold_since is None  # next hold gets a fresh window
    await _recompute(hass, entry)
    assert hass.states.get(head_b).state == "heat"


async def test_normal_release_resets_the_window(hass: HomeAssistant) -> None:
    entry, _head_a, _head_b = await _bedtime_hold(hass, 66)
    coord = entry.runtime_data
    assert coord._priority_hold_since is not None
    await _set_temp(hass, SENSOR_A, 64)  # Master satisfied
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    assert coord.data["state"] == "heat"
    assert coord.data["priority_hold"] is False
    assert coord.data["priority_hold_expired"] is False
    assert coord._priority_hold_since is None
