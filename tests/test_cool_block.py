"""Per-room cool block (local fork, woodstove lockout)."""
from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.logic import engage_with_latch, room_call

_BASE = {"enabled": True, "eco": False, "sensor_ok": True, "band": 1.0,
         "eco_cool_max": 78.0, "eco_heat_min": 50.0, "neutral": "neutral"}


def test_room_call_cool_block_suppresses_cool_only() -> None:
    assert room_call(temp=76, target=72, **_BASE) == "cool"
    assert room_call(temp=76, target=72, cool_block=True, **_BASE) == "neutral"
    # Heat is untouched.
    assert room_call(temp=68, target=72, cool_block=True, **_BASE) == "heat"
    # No ceiling: still blocked far above target (unlike cool_lockout).
    assert room_call(temp=77.9, target=72, cool_block=True, **_BASE) == "neutral"


def test_eco_protection_still_cools() -> None:
    eco = {**_BASE, "eco": True}
    assert room_call(temp=80, target=72, cool_block=True, **eco) == "cool"


def test_engaged_cool_run_coasts_when_blocked() -> None:
    kw = {"temp": 75, "target": 72, "enabled": True, "eco": False,
          "sensor_ok": True, "eco_cool_max": 78.0, "eco_heat_min": 50.0}
    assert engage_with_latch(prior="cool", band=1.0, neutral="satisfied", **kw) == "cool"
    assert engage_with_latch(prior="cool", band=1.0, neutral="satisfied",
                             cool_block=True, **kw) == "satisfied"


pytest.importorskip("homeassistant")
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


async def test_blocked_room_does_not_cool_and_others_can(hass: HomeAssistant) -> None:
    entry, head_a, head_b = await _climate_setup(hass, **{CONF_MODE_HYSTERESIS: 0})
    await _enable(hass, entry, "_coordinator_enable", "_secondary_enable")
    await _set_mode(hass, _eid(hass, entry, "_secondary_thermostat"),
                    hvac_mode="cool", temperature=72)
    await _set_mode(hass, _eid(hass, entry, "_primary_thermostat"),
                    hvac_mode="cool", temperature=72)
    await _set_temp(hass, SENSOR_A, 72)
    await _set_temp(hass, SENSOR_B, 76)
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["secondary_demand"] == "cool"
    assert hass.states.get(head_b).state == "cool"

    # Woodstove: block the secondary room. It must stop cooling.
    await _enable(hass, entry, "_secondary_cool_block")
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["secondary_demand"] != "cool"
    assert plan["secondary_engage"] != "cool"
    assert hass.states.get(head_b).state != "cool"
    zones = hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"]
    assert zones[1]["cool_block"] is True and zones[0]["cool_block"] is False

    # An unblocked room can still cool; the blocked head stays out of it.
    await _set_temp(hass, SENSOR_A, 76)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    assert hass.states.get(head_a).state == "cool"
    assert hass.states.get(head_b).state != "cool"
