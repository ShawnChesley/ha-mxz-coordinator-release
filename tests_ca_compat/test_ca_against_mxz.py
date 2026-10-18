"""Stock Climate Advisor 0.7.106 control code driving the forked MXZ Coordinator.

One real Home Assistant test instance: the forked MXZ integration (two mock heads,
real coordinator) and CA's real AutomationEngine pointed at the MXZ room
thermostat, configured with a representative regression fixture. Nothing is mocked
between them: CA calls climate.* services, MXZ receives them.
"""

from __future__ import annotations

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from homeassistant.core import HomeAssistant

from custom_components.mxz_coordinator.const import CONF_MODE_HYSTERESIS
from tests.test_climate import SENSOR_A, SENSOR_B, _eid, _enable, _recompute, _set_temp
from tests.test_climate import _setup as _climate_setup

WINDOW = "binary_sensor.test_window"

# Regression fixture with intentionally invalid targets to reproduce rejected writes.
MASTER_CFG = {
    "comfort_heat": 69,
    "comfort_cool": 71,
    "setback_heat": 59,
    "setback_cool": 75,
    "sleep_heat": 60,
    "sleep_cool": 69,
    "sleep_time": "21:00:00",
    "wake_time": "07:00:00",
    "fan_mode": "disabled",
    "temp_unit": "fahrenheit",
    "sensor_debounce_seconds": 600,
    "manual_grace_seconds": 14400,
    "automation_grace_seconds": 300,
    "indoor_temp_source": "climate_fallback",
    "nat_vent_soft_start_enabled": True,
}


def _classification(day_type="mild", hvac_mode="off", trend="cooling") -> DayClassification:
    return DayClassification(
        day_type=day_type,
        trend_direction=trend,
        trend_magnitude=5.0,
        today_high=66.0,
        today_low=46.0,
        tomorrow_high=63.0,
        tomorrow_low=39.0,
        hvac_mode=hvac_mode,
    )


async def _stand_up(hass: HomeAssistant):
    entry, head_a, head_b = await _climate_setup(hass, **{CONF_MODE_HYSTERESIS: 0})
    await _enable(hass, entry, "_coordinator_enable", "_primary_enable", "_secondary_enable")
    hass.states.async_set(WINDOW, "off")
    hass.states.async_set("weather.home", "cloudy", {"temperature": 46})
    therm = _eid(hass, entry, "_primary_thermostat")
    engine = AutomationEngine(
        hass=hass,
        climate_entity=therm,
        weather_entity="weather.home",
        door_window_sensors=[WINDOW],
        notify_service="notify.none",
        config={"climate_entity": therm, "weather_entity": "weather.home", **MASTER_CFG},
    )
    engine.dry_run = False
    await hass.async_block_till_done()
    return entry, engine, therm, head_a, head_b


def _snap(hass, entry, therm, head_a):
    st = hass.states.get(therm)
    plan = entry.runtime_data.data
    return {
        "zone_mode": st.state,
        "target": st.attributes.get("temperature"),
        "shared": plan.get("state"),
        "engage": plan.get("primary_engage"),
        "head": hass.states.get(head_a).state,
    }


async def test_probe(hass: HomeAssistant) -> None:
    entry, engine, therm, head_a, _ = await _stand_up(hass)
    caps = engine._get_thermostat_capabilities()
    print("\nCAPS", caps)
    # Put the house in heat season: secondary room cold and calling heat.
    await _set_temp(hass, SENSOR_B, 60)
    await _set_temp(hass, SENSOR_A, 70)
    await _recompute(hass, entry)

    from custom_components.climate_advisor.automation import select_comfort_band

    c = _classification()
    engine._current_classification = c
    band = select_comfort_band(c, engine.config, occupancy_mode="home",
                               in_sleep_window=False, aggressive_savings=False)
    print("BAND", band)
    res = await engine._apply_comfort_band(band, reason="probe comfort")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    print("COMFORT result", res, _snap(hass, entry, therm, head_a))

    # Room falls to 64 (tonight's shape) -> must not heat on a cool edge.
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    print("ROOM64 before CA re-eval", _snap(hass, entry, therm, head_a))
    res = await engine._apply_comfort_band(band, reason="probe re-eval")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    print("ROOM64 after CA re-eval", res, _snap(hass, entry, therm, head_a))

    # Bedtime
    sleep = select_comfort_band(c, engine.config, occupancy_mode="home",
                                in_sleep_window=True, aggressive_savings=False)
    print("SLEEP BAND", sleep)
    res = await engine._apply_comfort_band(sleep, reason="probe bedtime")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    print("BEDTIME", res, _snap(hass, entry, therm, head_a))

