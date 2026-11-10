"""Review regressions for the single-setpoint floor/ceiling fallback.

Uses the Mahtanar-like ``MitpHead`` (features 395) with a real 61-88 F band and
firmware that moves the OTHER edge itself on a single-setpoint write, keeping a
3.6 F (2.0 C) gap and reporting in half degrees (measured: cool 63 -> 59.5/63).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import HVACMode
from homeassistant.core import State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from tests.test_remote_input import (
    SENSOR_A,
    MitpHead,
    _age_commands,
    _recompute,
    _set_temp,
    _setup,
)


def _half(x: float) -> float:
    return round(x * 2) / 2


async def _fw_set_temperature(self, **kwargs: Any) -> None:
    """MitpHead + firmware gap enforcement on the edge it was NOT given."""
    self.calls.append(dict(kwargs))
    mode = kwargs.get("hvac_mode")
    if mode is not None:
        self._attr_hvac_mode = mode
    low, high = kwargs.get("target_temp_low"), kwargs.get("target_temp_high")
    t = kwargs.get("temperature")
    m = self._attr_hvac_mode
    if m == HVACMode.COOL:
        if high is not None:
            self._cool_sp = high
        if t is not None:
            self._cool_sp = t
        if self._heat_sp > self._cool_sp - 3.6:
            self._heat_sp = _half(self._cool_sp - 3.6)
    elif m == HVACMode.HEAT:
        if low is not None:
            self._heat_sp = low
        if t is not None:
            self._heat_sp = t
        if self._cool_sp < self._heat_sp + 3.6:
            self._cool_sp = _half(self._heat_sp + 3.6)
    self._attr_target_temperature_low = self._heat_sp
    self._attr_target_temperature_high = self._cool_sp
    self.async_write_ha_state()


@pytest.fixture
def fw_head(monkeypatch):
    monkeypatch.setattr(MitpHead, "_attr_min_temp", 61.0, raising=False)
    monkeypatch.setattr(MitpHead, "_attr_max_temp", 88.0, raising=False)
    monkeypatch.setattr(MitpHead, "_attr_precision", 0.5, raising=False)  # as the real head
    monkeypatch.setattr(MitpHead, "async_set_temperature", _fw_set_temperature)
    orig_init = MitpHead.__init__

    def _init(self, suffix):
        orig_init(self, suffix)
        self.calls = []

    monkeypatch.setattr(MitpHead, "__init__", _init)


async def _advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _cool63(hass):
    entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.set_zone_input(zone, source="service", target=63, enable=True, direction="cool")
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    return entry, coord, head, zone


async def test_cool_floor_single_write_and_firmware_edge(hass, fw_head):
    _entry, _coord, head, _zone = await _cool63(hass)
    st = hass.states.get(head.entity_id)
    assert st.state == "cool"
    assert st.attributes["temperature"] == 63
    assert st.attributes["target_temp_low"] == 59.5  # firmware-placed, below floor
    last = head.calls[-1]
    assert last["temperature"] == 63 and last["hvac_mode"] == "cool"
    assert "target_temp_low" not in last and "target_temp_high" not in last


async def test_rounded_firmware_edge_then_mode_only_change_is_not_a_remote_press(
    hass, freezer, fw_head
):
    """59.5 reported (59.4 predicted) + a mode-only switch to heat: no adoption."""
    _entry, coord, head, zone = await _cool63(hass)
    _age_commands(coord)
    generation = zone.input_generation
    assert coord._target_input[head.entity_id].matches("heat", 59.5)
    # Real state-event path: an uncommanded full-state report, heat at 59.5.
    head.remote("heat")
    await hass.async_block_till_done()
    assert hass.states.get(head.entity_id).attributes["temperature"] == 59.5
    await _advance(hass, freezer, 60)
    assert zone.target == 63
    assert zone.input_generation == generation
    assert coord.remote_record(head.entity_id).heat != 59.5


async def test_genuine_remote_press_after_fallback_is_still_adopted(hass, freezer, fw_head):
    _entry, coord, head, zone = await _cool63(hass)
    _age_commands(coord)
    head.remote("cool", 66)
    await hass.async_block_till_done()
    await _advance(hass, freezer, 60)
    assert zone.target == 66
    assert zone.input_source == "head_manual"


async def test_fallback_idempotent_no_rewrites(hass, fw_head):
    entry, _coord, head, _zone = await _cool63(hass)
    n = len(head.calls)
    await _recompute(hass, entry)
    await _recompute(hass, entry)
    assert len(head.calls) == n


async def test_heat_ceiling_fallback_and_idempotency(hass, fw_head):
    entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.set_zone_input(zone, source="service", target=86, enable=True, direction="heat")
    await _set_temp(hass, SENSOR_A, 80)
    await _recompute(hass, entry)
    st = hass.states.get(head.entity_id)
    assert st.state == "heat"
    assert st.attributes["temperature"] == 86  # not pulled down to 84 by the gap
    assert st.attributes["target_temp_high"] == 89.5  # firmware, above the 88 max
    assert head.calls[-1]["temperature"] == 86
    assert coord._target_input[head.entity_id].matches("cool", 89.5)
    n = len(head.calls)
    await _recompute(hass, entry)
    assert len(head.calls) == n


async def test_heat_to_cool_is_one_combined_write_and_transient_old_edge_ignored(
    hass, freezer, fw_head
):
    """Mode change carries the temperature in the SAME call; a separate
    intermediate report of the old cool edge never becomes a target."""
    entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.set_zone_input(zone, source="service", target=72, enable=True, direction="heat_cool")
    await _set_temp(hass, SENSOR_A, 66)
    await _recompute(hass, entry)
    assert hass.states.get(head.entity_id).state == "heat"
    old_cool = hass.states.get(head.entity_id).attributes["target_temp_high"]
    head.calls.clear()
    coord.set_zone_input(zone, source="service", target=63)
    await _set_temp(hass, SENSOR_A, 67)
    # Separate reports: firmware shows the mode first with the OLD cool edge...
    coord._classify_head_input(
        head.entity_id,
        State(head.entity_id, "heat", {"temperature": 72, "fan_mode": "auto"}),
        State(head.entity_id, "cool", {"temperature": old_cool, "fan_mode": "auto"}),
    )
    await _recompute(hass, entry)
    temp_calls = [c for c in head.calls if "hvac_mode" in c]
    assert temp_calls and all("temperature" in c for c in temp_calls)
    assert temp_calls[-1] == {"entity_id": head.entity_id, "hvac_mode": "cool", "temperature": 63} or (
        temp_calls[-1]["hvac_mode"] == "cool" and temp_calls[-1]["temperature"] == 63
    )
    await _advance(hass, freezer, 60)
    assert zone.target == 63


async def test_startup_with_unused_edge_outside_band(hass, fw_head, monkeypatch):
    """Head already in cool 63 with a firmware heat edge of 59.5 at startup."""
    orig_init = MitpHead.__init__

    def _pre(self, suffix):
        orig_init(self, suffix)
        self._attr_hvac_mode = HVACMode.COOL
        self._cool_sp, self._heat_sp = 63.0, 59.5
        self._attr_target_temperature_low = 59.5
        self._attr_target_temperature_high = 63.0

    monkeypatch.setattr(MitpHead, "__init__", _pre)
    entry, coord, heads = await _setup(hass)
    zone = coord.zones[0]
    assert coord.last_update_success is True
    assert zone.target != 59.5
    coord.set_zone_input(zone, source="service", target=63, enable=True, direction="cool")
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    assert hass.states.get(heads[0].entity_id).attributes["temperature"] == 63


async def test_celsius_native_head_real_report_matches(hass, fw_head, monkeypatch):
    """The real FH09 is C-native (0.5 C steps, half-degree display) and reported
    59.5 F for the firmware edge after cool 63. Plain 59.4 -> 15.0 C -> 59.0 F
    would miss it; the half-step tolerance must cover the real report."""
    from homeassistant.const import UnitOfTemperature

    _entry, coord, head, _zone = await _cool63(hass)
    monkeypatch.setattr(
        coord, "_native_metadata",
        lambda cid: (UnitOfTemperature.CELSIUS, 0.5, 0.5),
    )
    coord._expect_firmware_edge(head.entity_id, "cool", 63 - 3.6)
    journal = coord._target_input[head.entity_id]
    assert journal.matches("heat", 59.5)  # observed on the real head
    assert journal.matches("heat", 59.0)  # 15.0 C native representation
    assert not journal.matches("heat", 61.0)  # the floor: a real remote value


async def test_celsius_system_fallback(hass):
    """Metric install: cool 17 C under a 16 C floor -> single 17, not 16/18."""
    from homeassistant.components.climate import ClimateEntityFeature
    from homeassistant.util.unit_system import METRIC_SYSTEM

    from tests.test_drive import MockHeadC
    from tests.test_single_setpoint import _enable

    class CRange(MockHeadC):
        _attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE_RANGE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF) | ClimateEntityFeature.TARGET_TEMPERATURE
        _attr_min_temp = 16.0
        _attr_max_temp = 31.0

        def __init__(self, s):
            super().__init__(s)
            self._attr_target_temperature = None
            self.calls = []

        async def async_set_temperature(self, **kw):
            self.calls.append(dict(kw))
            if kw.get("hvac_mode"):
                self._attr_hvac_mode = kw["hvac_mode"]
            if kw.get("temperature") is not None:
                self._attr_target_temperature = kw["temperature"]
                self._attr_target_temperature_high = kw["temperature"]
                self._attr_target_temperature_low = kw["temperature"] - 2.0
            await super().async_set_temperature(**{k: v for k, v in kw.items() if k != "temperature"})

    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        MockModule,
        MockPlatform,
        mock_integration,
        mock_platform,
    )

    from custom_components.mxz_coordinator.const import (
        CONF_PRIMARY_CLIMATE,
        CONF_PRIMARY_SENSOR,
        CONF_SECONDARY_CLIMATE,
        CONF_SECONDARY_SENSOR,
        DOMAIN,
    )

    hass.config.units = METRIC_SYSTEM
    heads = [CRange("a"), CRange("b")]

    async def _climate(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_climate))
    assert await async_setup_component(hass, "climate", {"climate": {"platform": "test"}})
    await hass.async_block_till_done()
    hass.states.async_set("sensor.room_a_temp", "21")
    hass.states.async_set("sensor.room_b_temp", "20")
    entry = MockConfigEntry(domain=DOMAIN, title="MXZ", data={
        CONF_PRIMARY_CLIMATE: heads[0].entity_id, CONF_SECONDARY_CLIMATE: heads[1].entity_id,
        CONF_PRIMARY_SENSOR: "sensor.room_a_temp", CONF_SECONDARY_SENSOR: "sensor.room_b_temp",
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _enable(hass, entry)
    coord = entry.runtime_data
    coord.zones[0].target = 17.0
    coord.reset_engage_latch(coord.zones[0].slug)
    hass.states.async_set("sensor.room_a_temp", "21")
    await coord.async_refresh()
    await hass.async_block_till_done()
    st = hass.states.get(heads[0].entity_id)
    assert st.state == "cool"
    assert heads[0].calls[-1].get("temperature") == 17.0
    assert "target_temp_low" not in heads[0].calls[-1]


async def test_range_only_head_keeps_existing_behavior(hass):
    """No TARGET_TEMPERATURE feature -> unchanged 61/65 range write."""
    from homeassistant.components.climate import ClimateEntityFeature

    from tests.test_cool_floor_single_setpoint import FloorRangeHead
    from tests.test_single_setpoint import _enable
    from tests.test_single_setpoint import _setup as _s_setup

    class RangeOnly(FloorRangeHead):
        _attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE_RANGE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF)

    heads = [RangeOnly("a"), RangeOnly("b")]
    entry = await _s_setup(hass, heads)
    await _enable(hass, entry)
    coord = entry.runtime_data
    coord.zones[0].target = 63.0
    coord.reset_engage_latch(coord.zones[0].slug)
    await _set_temp(hass, "sensor.room_a_temp", 67)
    await coord.async_refresh()
    await hass.async_block_till_done()
    last = heads[0].calls[-1]
    assert last.get("target_temp_low") == 61 and last.get("target_temp_high") == 65
