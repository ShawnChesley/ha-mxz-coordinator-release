"""Per-room tight demand: a room votes on the shared mode at its own drift."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import (
    CONF_EXEMPT_COOL_STOP_OFFSET,
    CONF_MODE_HYSTERESIS,
)
from tests.test_climate import SENSOR_A, SENSOR_B, _eid, _enable, _recompute, _set_temp
from tests.test_climate import _setup as _climate_setup
from tests.test_direction import _set_mode


async def _overnight(hass: HomeAssistant, *, tight: bool):
    """Last night's shape: house heating, Master cool 64 (exempt, lockout on)."""
    entry, head_a, head_b = await _climate_setup(
        hass, **{CONF_MODE_HYSTERESIS: 0, CONF_EXEMPT_COOL_STOP_OFFSET: 0.0}
    )
    switches = ["_coordinator_enable", "_secondary_enable", "_cool_lockout",
                "_primary_cool_exempt"]
    if tight:
        switches.append("_primary_tight_demand")
    await _enable(hass, entry, *switches)
    await _set_mode(hass, _eid(hass, entry, "_secondary_thermostat"),
                    hvac_mode="heat", temperature=68)
    await _set_mode(hass, _eid(hass, entry, "_primary_thermostat"),
                    hvac_mode="cool", temperature=64)
    # Other room cold -> house in heat; Master inside its band.
    await _set_temp(hass, SENSOR_A, 64)
    await _set_temp(hass, SENSOR_B, 63)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["state"] == "heat"
    # Master drifts to 65.5: past its 1 F drift, inside the 3 F global threshold.
    await _set_temp(hass, SENSOR_A, 65.5)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    return entry, head_a, head_b


async def test_default_master_waits_for_global_threshold(hass: HomeAssistant) -> None:
    """Unchanged behaviour with the switch off: 65.5 does not claim the compressor."""
    entry, head_a, _b = await _overnight(hass, tight=False)
    plan = entry.runtime_data.data
    assert plan["primary_demand"] == "neutral"
    assert plan["state"] == "heat"
    assert hass.states.get(head_a).state != "cool"
    zone0 = hass.states.get(_eid(hass, entry, "_plan")).attributes["zones"][0]
    assert zone0["tight_demand"] is False


async def test_tight_master_claims_cool_at_drift_and_releases_at_target(
    hass: HomeAssistant,
) -> None:
    entry, head_a, head_b = await _overnight(hass, tight=True)
    plan = entry.runtime_data.data
    assert plan["primary_demand"] == "cool"
    assert plan["state"] == "cool"
    assert hass.states.get(head_a).state == "cool"

    # Back to 64: Master satisfied, the house gets heat again.
    await _set_temp(hass, SENSOR_A, 64)
    entry.runtime_data._last_mode_change_ts = 0.0
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["primary_engage"] != "cool"
    assert plan["state"] == "heat"
    assert hass.states.get(head_b).state == "heat"


async def test_tight_master_inside_drift_does_not_flip(hass: HomeAssistant) -> None:
    """64.8 is inside the 1 F drift: no flip even with the switch on."""
    entry, _a, _b = await _climate_setup(hass, **{CONF_MODE_HYSTERESIS: 0})
    await _enable(hass, entry, "_coordinator_enable", "_secondary_enable",
                  "_cool_lockout", "_primary_cool_exempt", "_primary_tight_demand")
    await _set_mode(hass, _eid(hass, entry, "_secondary_thermostat"),
                    hvac_mode="heat", temperature=68)
    await _set_mode(hass, _eid(hass, entry, "_primary_thermostat"),
                    hvac_mode="cool", temperature=64)
    await _set_temp(hass, SENSOR_B, 63)
    await _set_temp(hass, SENSOR_A, 64.8)
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["primary_demand"] == "neutral"
    assert plan["state"] == "heat"
