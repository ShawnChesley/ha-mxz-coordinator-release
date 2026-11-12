"""Cool target near the head's floor must not be raised by the firmware-gap logic.

Real case (Master FH09, 2026-11-09): sleep cool 63 F, head floor 61 F, Mahtanar
firmware needs cool - heat >= 2.0 C. HA range-checks BOTH edges, so the unused
heat edge could not go below 61 and the coordinator commanded 61/65 -- the head
cooled to 65, not 63. The head also accepts a single setpoint (features 395),
and given ``temperature: 63`` in cool its firmware places the heat edge at 59.5
itself. The coordinator must send that single setpoint in this case.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.core import HomeAssistant

from .test_drive import MockHead
from .test_single_setpoint import _enable, _set_temp, _setup


class FloorRangeHead(MockHead):
    """Two-setpoint head that also takes a single setpoint, floor 61 F.

    Mirrors the Mahtanar firmware: a single ``temperature`` sets the active edge
    and the firmware moves the other edge to keep a 3.6 F (2.0 C) gap.
    """

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = 61.0
    _attr_max_temp = 88.0

    def __init__(self, suffix: str) -> None:
        super().__init__(suffix)
        self._attr_target_temperature = None
        self.calls: list[dict[str, Any]] = []

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = mode
        if (temp := kwargs.get("temperature")) is not None:
            mode = self._attr_hvac_mode
            if mode == "cool":
                self._attr_target_temperature_high = temp
                self._attr_target_temperature_low = round((temp - 3.6) * 2) / 2
            else:
                self._attr_target_temperature_low = temp
                self._attr_target_temperature_high = round((temp + 3.6) * 2) / 2
            self._attr_target_temperature = temp
        if (low := kwargs.get("target_temp_low")) is not None:
            self._attr_target_temperature_low = low
        if (high := kwargs.get("target_temp_high")) is not None:
            self._attr_target_temperature_high = high
        if "target_temp_high" in kwargs or "target_temp_low" in kwargs:
            self._attr_target_temperature = (
                self._attr_target_temperature_high
                if self._attr_hvac_mode == "cool"
                else self._attr_target_temperature_low
            )
        self.async_write_ha_state()


async def _cool_to(hass: HomeAssistant, target: float):
    heads = [FloorRangeHead("a"), FloorRangeHead("b")]
    entry = await _setup(hass, heads)
    await _enable(hass, entry)
    coord = entry.runtime_data
    coord.zones[0].target = target
    coord.reset_engage_latch(coord.zones[0].slug)
    await _set_temp(hass, "sensor.room_a_temp", target + 4)
    await coord.async_refresh()
    await hass.async_block_till_done()
    return heads, coord


async def test_cool_63_under_61_floor_holds_63(hass: HomeAssistant) -> None:
    heads, coord = await _cool_to(hass, 63.0)
    a = hass.states.get(heads[0].entity_id)
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 63  # was 65 before the fix
    last = heads[0].calls[-1]
    assert last.get("temperature") == 63
    assert "target_temp_low" not in last
    assert coord.last_update_success is True


async def test_cool_63_is_idempotent(hass: HomeAssistant) -> None:
    heads, coord = await _cool_to(hass, 63.0)
    n = len(heads[0].calls)
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert len(heads[0].calls) == n  # no rewrite once the head holds 63


async def test_cool_well_above_floor_still_uses_range(hass: HomeAssistant) -> None:
    """Nothing changes when the unused edge fits: 70 -> 66/70 range write."""
    heads, _ = await _cool_to(hass, 70.0)
    last = heads[0].calls[-1]
    assert last.get("target_temp_high") == 70
    assert last.get("target_temp_low") == 66
