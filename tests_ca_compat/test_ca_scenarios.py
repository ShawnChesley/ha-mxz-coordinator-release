"""Stock Climate Advisor 0.7.106 -> forked MXZ Coordinator, scenario assertions.

Real HA test instance: forked MXZ (real coordinator, two mock heads) + CA's real
AutomationEngine aimed at the MXZ room thermostat. Room B is kept
cold so the shared outdoor unit is in HEAT for every scenario -- the season in
which tonight's incident happened. Clock is pinned per scenario.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from homeassistant.core import HomeAssistant
from tests.test_ca_against_mxz import MASTER_CFG, WINDOW

from custom_components.mxz_coordinator.const import (
    CONF_CLAMP_MIN,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CONTACTS,
    CONF_WINDOW_PAUSE_DELAY,
)
from tests.test_climate import SENSOR_A, SENSOR_B, _eid, _enable, _recompute, _set_temp
from tests.test_climate import _setup as _climate_setup

DAY = "2026-10-18 14:00:00-07:00"
BEDTIME = "2026-10-18 21:00:05-07:00"
NIGHT = "2026-10-18 23:30:00-07:00"
MORNING = "2026-10-19 07:00:05-07:00"


@pytest.fixture(autouse=True)
async def cleanup_direct_engines(hass, freezer):
    """Direct engines have no config-entry unload hook; release their timers."""
    engines = hass.data.setdefault("mxz_ca_test_engines", [])
    yield
    # Let all ten-second write acknowledgements settle before unloading. CA
    # stores only the latest cancellation handle when writes arrive in a burst;
    # earlier acknowledgements are sequence-guarded but still scheduled.
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    for engine in engines:
        engine.cleanup()
    await hass.async_block_till_done()


def _mild() -> DayClassification:
    return DayClassification(
        day_type="mild", trend_direction="cooling", trend_magnitude=5.0,
        today_high=66.0, today_low=46.0, tomorrow_high=63.0, tomorrow_low=39.0,
        hvac_mode="off",
    )


class Rig:
    def __init__(self, hass, entry, engine, therm, head_a):
        self.hass, self.entry, self.engine = hass, entry, engine
        self.therm, self.head_a = therm, head_a

    async def settle(self):
        await self.hass.async_block_till_done()
        await _recompute(self.hass, self.entry)

    async def tick(self, temp):
        # Production order: the sensor changes, MXZ re-publishes the room
        # thermostat, then CA's temperature-tick handler reads it.
        await _set_temp(self.hass, SENSOR_A, temp)
        await self.settle()
        await self.engine.comfort_family_temperature_check(temp, predicted_indoor=None)
        await self.settle()

    @property
    def zone(self):
        st = self.hass.states.get(self.therm)
        return st.state, st.attributes.get("temperature")

    @property
    def heating(self):
        plan = self.entry.runtime_data.data
        return plan["primary_engage"] == "heat" and plan["state"] == "heat"

    @property
    def head(self):
        return self.hass.states.get(self.head_a).state


async def _rig(hass: HomeAssistant, freezer, when: str, **mxz) -> Rig:
    freezer.move_to(when)
    entry, head_a, _ = await _climate_setup(hass, **{CONF_MODE_HYSTERESIS: 0, **mxz})
    await _enable(hass, entry, "_coordinator_enable", "_primary_enable", "_secondary_enable")
    hass.states.async_set(WINDOW, "off")
    therm = _eid(hass, entry, "_primary_thermostat")
    engine = AutomationEngine(
        hass=hass, climate_entity=therm, weather_entity="weather.home",
        door_window_sensors=[WINDOW], notify_service="notify.none",
        config={"climate_entity": therm, "weather_entity": "weather.home", **MASTER_CFG},
    )
    engine.dry_run = False
    hass.data["mxz_ca_test_engines"].append(engine)
    engine._notify = AsyncMock()
    engine._current_classification = _mild()
    await _set_temp(hass, SENSOR_B, 60)  # other room cold -> shared mode HEAT
    await _set_temp(hass, SENSOR_A, 70)
    rig = Rig(hass, entry, engine, therm, head_a)
    await rig.settle()
    return rig


async def test_mild_day_cool_intent_does_not_heat_inside_band(hass, freezer) -> None:
    rig = await _rig(hass, freezer, DAY)
    await rig.engine.apply_classification(_mild(), indoor_temp=70)
    await rig.settle()
    assert rig.entry.runtime_data.data["state"] == "heat"  # heat season
    assert rig.zone[0] in ("cool", "heat")  # one-sided, never two-sided heat_cool
    # Inside the 69-71 comfort band nothing heats (tonight's 70-target bug can't happen).
    for t in (70.5, 70.0, 69.5, 69.1):
        await rig.tick(t)
        assert not rig.heating, (t, rig.zone)


async def test_mild_day_floor_breach_heats_to_floor(hass, freezer) -> None:
    rig = await _rig(hass, freezer, DAY)
    await rig.engine.apply_classification(_mild(), indoor_temp=70)
    await rig.settle()
    await rig.tick(66.0)  # 3F under the 69 floor: a breach
    assert rig.zone == ("heat", 69)
    assert rig.heating and rig.head == "heat"


async def test_bedtime_moves_room_to_sleep_band(hass, freezer) -> None:
    rig = await _rig(hass, freezer, BEDTIME)
    await rig.engine.handle_bedtime()
    await rig.settle()
    # Sleep band [60/69] on a mild day: ceiling edge, never heats inside the band.
    assert rig.zone == ("cool", 69)
    freezer.move_to(NIGHT)
    for t in (68.0, 66.0, 64.0, 62.0, 61.0):
        await rig.tick(t)
        assert not rig.heating, (t, rig.zone)


async def test_night_floor_breach_heats_to_sleep_floor(hass, freezer) -> None:
    rig = await _rig(hass, freezer, BEDTIME)
    await rig.engine.handle_bedtime()
    await rig.settle()
    freezer.move_to(NIGHT)
    await rig.tick(57.5)  # well past the 60 sleep floor
    assert rig.zone == ("heat", 60)
    assert rig.heating


async def test_sleep_floor_below_mxz_minimum_is_rejected(hass, freezer) -> None:
    """Live MXZ min is 61; CA's Master sleep_heat is 60.

    HA rejects the 60 write, CA logs a warning, and the room is left on the
    previous edge -- heat at 69 all night. CA's sleep_heat must be >= 61.
    """
    rig = await _rig(hass, freezer, BEDTIME, **{CONF_CLAMP_MIN: 61})
    assert hass.states.get(rig.therm).attributes["min_temp"] == 61
    await rig.engine.handle_bedtime()
    await rig.settle()
    freezer.move_to(NIGHT)
    await rig.tick(57.5)
    # Either HA rejects 60 (room never switches to heat) or something clamps it.
    assert rig.zone == ("heat", 69)  # stale daytime floor, NOT the sleep floor
    assert rig.heating


async def test_morning_wakeup_returns_to_comfort(hass, freezer) -> None:
    rig = await _rig(hass, freezer, MORNING)
    await _set_temp(hass, SENSOR_A, 64)
    await rig.settle()
    await rig.engine.handle_morning_wakeup(indoor_temp=64.0)
    await rig.settle()
    assert rig.zone == ("heat", 69)
    assert rig.heating


@pytest.mark.xfail(
    strict=True,
    reason="CA ALONE (no MXZ contacts configured) neither pauses nor blocks heat during its "
    "planned window hours / nat-vent; a below-floor room heats with the window open. "
    "Kept as the documented gap; the MXZ window interlock test below closes it.",
)
async def test_window_open_during_planned_window_hours_does_not_heat(hass, freezer) -> None:
    rig = await _rig(hass, freezer, DAY)
    await rig.engine.apply_classification(_mild(), indoor_temp=70)
    await rig.settle()
    rig.engine._last_outdoor_temp = 46.0
    hass.states.async_set(WINDOW, "on")
    await rig.engine.handle_door_window_open(WINDOW)
    await rig.settle()
    print("\nNATVENT", rig.zone, "natvent", rig.engine._natural_vent_active,
          "paused", rig.engine._paused_by_door)
    for t in (68.0, 66.0, 64.0, 62.0):
        await rig.tick(t)
        print("  tick", t, rig.zone, "heating", rig.heating)
    assert not rig.heating


async def test_window_open_pause_turns_zone_off(hass, freezer) -> None:
    rig = await _rig(hass, freezer, NIGHT)  # outside CA's planned window hours
    await rig.engine.apply_classification(_mild(), indoor_temp=70)
    await rig.settle()
    rig.engine._last_outdoor_temp = None  # nat-vent cannot start -> pause path
    hass.states.async_set(WINDOW, "on")
    await rig.engine.handle_door_window_open(WINDOW)
    await rig.settle()
    assert rig.engine._paused_by_door
    assert rig.zone[0] == "off"
    await rig.tick(62.0)
    assert not rig.heating


async def test_mxz_window_interlock_closes_the_planned_window_gap(hass, freezer) -> None:
    """Same scenario as the xfail above, with the room's contact wired into MXZ."""
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass.states.async_set(WINDOW, "off")
    rig = await _rig(
        hass, freezer, DAY,
        **{CONF_PRIMARY_CONTACTS: [WINDOW], CONF_WINDOW_PAUSE_DELAY: 600},
    )
    await rig.engine.apply_classification(_mild(), indoor_temp=70)
    await rig.settle()
    rig.engine._last_outdoor_temp = 46.0
    hass.states.async_set(WINDOW, "on")
    await hass.async_block_till_done()
    await rig.engine.handle_door_window_open(WINDOW)
    await rig.settle()  # MXZ stamps open_since
    freezer.tick(timedelta(seconds=601))
    async_fire_time_changed(hass)
    await rig.settle()
    assert rig.entry.runtime_data.data["primary_window"] == "paused"
    for t in (68.0, 66.0, 64.0, 62.0):
        await rig.tick(t)
        assert not rig.heating, (t, rig.zone)
    # Window closes: the room follows CA's current band again (floor breach -> heat).
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()
    await rig.tick(62.0)
    assert rig.entry.runtime_data.data["primary_window"] == "closed"
    assert rig.heating


@pytest.mark.parametrize("phase,expected_target", [("bedtime", 64), ("morning", 69)])
async def test_schedule_changes_during_window_pause_use_current_target_on_close(
    hass, freezer, phase, expected_target
) -> None:
    """An afternoon target must not be restored after an overnight window pause."""
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass.states.async_set(WINDOW, "off")
    rig = await _rig(hass, freezer, DAY, **{CONF_PRIMARY_CONTACTS: [WINDOW]})
    rig.engine.config["sleep_heat"] = 64  # valid fixture value, not a live setting
    await rig.engine.apply_classification(_mild(), indoor_temp=70)
    await rig.settle()
    rig.engine._last_outdoor_temp = 46.0
    hass.states.async_set(WINDOW, "on")
    await rig.engine.handle_door_window_open(WINDOW)
    await rig.settle()
    freezer.tick(timedelta(seconds=601))
    async_fire_time_changed(hass)
    await rig.settle()
    assert rig.entry.runtime_data.data["primary_window"] == "paused"

    freezer.move_to(BEDTIME)
    await rig.engine.handle_bedtime()
    if phase == "morning":
        freezer.move_to(MORNING)
        await rig.engine.handle_morning_wakeup(indoor_temp=61.0)
    await rig.tick(61.0)
    assert not rig.heating
    hass.states.async_set(WINDOW, "off")
    await rig.tick(61.0)
    assert rig.entry.runtime_data.data["primary_window"] == "closed"
    assert rig.zone == ("heat", expected_target)
    assert rig.heating

