"""Full stock Climate Advisor integration + forked MXZ: IR-remote override."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.weather import (
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)


class MockWeather(WeatherEntity):
    _attr_name = "Home"
    _attr_unique_id = "mock_weather_home"
    _attr_native_temperature = 50.0
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_condition = "cloudy"
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    async def async_forecast_daily(self) -> list[Forecast]:
        now = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0)
        return [
            Forecast(datetime=(now + timedelta(days=i)).isoformat(),
                     native_temperature=66.0 - 3 * i, native_templow=46.0 - 3 * i,
                     condition="cloudy")
            for i in range(5)
        ]

    async def async_forecast_hourly(self) -> list[Forecast]:
        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        return [
            Forecast(datetime=(now + timedelta(hours=i)).isoformat(),
                     native_temperature=55.0, condition="cloudy")
            for i in range(48)
        ]


async def _setup_weather(hass) -> str:
    w = MockWeather()

    async def _plat(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([w])

    mock_integration(hass, MockModule("wtest"))
    mock_platform(hass, "wtest.weather", MockPlatform(async_setup_platform=_plat))
    assert await async_setup_component(hass, "weather", {"weather": {"platform": "wtest"}})
    await hass.async_block_till_done()
    return w.entity_id

from tests.test_ca_against_mxz import MASTER_CFG

from custom_components.mxz_coordinator.const import CONF_MODE_HYSTERESIS
from tests.test_climate import _eid, _recompute
from tests.test_remote_input import _age_commands
from tests.test_remote_input import _setup as _remote_setup


async def _advance(hass, freezer, minutes):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    step = 1
    for _ in range(int(minutes / step)):
        freezer.tick(timedelta(minutes=step))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_ir_remote_override_is_respected_by_stock_ca(hass: HomeAssistant, freezer) -> None:
    """IR press -> MXZ adopts (target + heat_cool) -> stock CA sees a manual
    override, does not overwrite it for its grace, then resumes its schedule."""
    freezer.move_to("2026-10-18 13:00:00-07:00")
    entry, coord, heads = await _remote_setup(hass, **{CONF_MODE_HYSTERESIS: 0})
    head = heads[0]
    therm = _eid(hass, entry, "_primary_thermostat")
    weather = await _setup_weather(hass)
    notes: list = []
    hass.services.async_register("notify", "test", lambda c: notes.append(c.data))
    # CA also mirrors every notification to notify.send_email (email channel).
    hass.services.async_register("notify", "send_email", lambda c: notes.append(c.data))
    ca = MockConfigEntry(
        domain="climate_advisor", version=20, title="Test room",
        data={**MASTER_CFG, "climate_entity": therm, "weather_entity": weather,
              "door_window_sensors": [], "notify_service": "notify.test",
              "outdoor_temp_source": "weather_service"},
    )
    ca.add_to_hass(hass)
    assert await hass.config_entries.async_setup(ca.entry_id)
    await hass.async_block_till_done()
    ca_coord = hass.data["climate_advisor"][ca.entry_id]
    engine = ca_coord.automation_engine

    await _advance(hass, freezer, 8)  # CA startup coalescing -> takes control
    st = hass.states.get(therm)
    assert (st.state, st.attributes["temperature"]) == ("cool", 71)
    assert not engine._manual_override_active

    # A person uses the IR remote: HEAT, then up to 74.
    freezer.tick(timedelta(seconds=10)); _age_commands(coord)
    head.remote("heat", 72.0, "auto")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=10)); _age_commands(coord)
    head.remote("heat", 74.0, "auto")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    st = hass.states.get(therm)
    assert st.attributes["temperature"] == 74
    assert st.state == "heat_cool"  # remote input reopened both directions
    assert st.attributes["last_input_source"] == "head_manual"
    await _advance(hass, freezer, 1)
    assert engine._override_confirm_pending or engine._manual_override_active, (
        "stock CA did not detect the override")
    # CA confirms an override after override_confirm_seconds (600 s in this fixture).
    await _advance(hass, freezer, 11)
    assert engine._manual_override_active, "stock CA did not confirm the override"
    st = hass.states.get(therm)
    assert (st.state, st.attributes["temperature"]) == ("heat_cool", 74)
    grace_s = engine.config.get("manual_grace_seconds")

    # For (almost) the whole grace CA must leave the person's setting alone.
    await _advance(hass, freezer, grace_s / 60 - 15)
    st = hass.states.get(therm)
    assert (st.state, st.attributes["temperature"]) == ("heat_cool", 74), (
        st.state, st.attributes.get("temperature"))

    # After the grace CA resumes its plan for the time of day.
    await _advance(hass, freezer, 60)
    st = hass.states.get(therm)
    print("\nAFTER GRACE", st.state, st.attributes.get("temperature"),
          "override", engine._manual_override_active)
    assert not engine._manual_override_active
    assert st.state in ("heat", "cool")
    assert st.attributes["last_input_source"] == "service"

    await hass.config_entries.async_unload(ca.entry_id)
    await hass.async_block_till_done()
