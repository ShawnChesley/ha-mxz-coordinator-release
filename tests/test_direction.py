"""Per-room direction (local fork, 2026-10-17): heat / cool / heat_cool.

Motivating incident (2026-10-17, Master Bedroom): an upstream controller wanted
"no heat" (cool-leaning band, window open) but could only express a two-sided
single target, so the room heated toward it. With a one-sided direction a room
told ``cool`` can never produce a heat call, and vice versa, while shared-mode
arbitration and the protections (eco extremes, emergency floor) are unchanged.
"""

from __future__ import annotations

import pytest

from custom_components.mxz_coordinator.const import (
    DEMAND_NEUTRAL,
    DIRECTION_AUTO,
    DIRECTION_COOL,
    DIRECTION_HEAT,
    MODE_COOL,
    MODE_HEAT,
)
from custom_components.mxz_coordinator.logic import engage_with_latch, room_call

_BASE = {
    "enabled": True,
    "eco": False,
    "sensor_ok": True,
    "band": 1.0,
    "eco_cool_max": 78.0,
    "eco_heat_min": 59.0,
    "neutral": DEMAND_NEUTRAL,
}


# --- pure logic --------------------------------------------------------------
@pytest.mark.parametrize(
    ("direction", "temp", "expected"),
    [
        (DIRECTION_AUTO, 60.0, MODE_HEAT),
        (DIRECTION_AUTO, 75.0, MODE_COOL),
        (DIRECTION_AUTO, 70.0, DEMAND_NEUTRAL),
        (DIRECTION_HEAT, 60.0, MODE_HEAT),
        (DIRECTION_HEAT, 75.0, DEMAND_NEUTRAL),  # never cools
        (DIRECTION_COOL, 75.0, MODE_COOL),
        (DIRECTION_COOL, 60.0, DEMAND_NEUTRAL),  # never heats
    ],
)
def test_room_call_respects_direction(direction, temp, expected) -> None:
    assert room_call(temp=temp, target=70.0, direction=direction, **_BASE) == expected


def test_default_direction_is_original_behaviour() -> None:
    for temp in (55.0, 68.9, 69.0, 70.0, 71.0, 71.1, 80.0):
        assert room_call(temp=temp, target=70.0, **_BASE) == room_call(
            temp=temp, target=70.0, direction=DIRECTION_AUTO, **_BASE
        )


def test_blocked_cool_with_cool_lockout_never_becomes_heat() -> None:
    """Tonight's shape: cool intent, cool locked out, room well below target."""
    for temp in (50.0, 60.0, 68.0, 69.5, 72.0, 80.0):
        call = room_call(
            temp=temp, target=70.0, direction=DIRECTION_COOL,
            cool_lockout=True, cool_lockout_ceiling=82.0, **_BASE,
        )
        assert call != MODE_HEAT, temp


def test_eco_extremes_ignore_direction() -> None:
    eco = {**_BASE, "eco": True}
    assert room_call(temp=80.0, target=70.0, direction=DIRECTION_HEAT, **eco) == MODE_COOL
    assert room_call(temp=55.0, target=70.0, direction=DIRECTION_COOL, **eco) == MODE_HEAT


def test_engaged_heat_run_stops_when_direction_turns_cool() -> None:
    """A latched heat run must coast (not keep heating) once direction is cool."""
    kw = {k: v for k, v in _BASE.items() if k not in ("band", "neutral")}
    out = engage_with_latch(
        prior=MODE_HEAT, band=1.0, neutral="satisfied",
        temp=66.0, target=70.0, direction=DIRECTION_COOL, **kw,
    )
    assert out == "satisfied"
    # and still runs to target under heat / heat_cool
    for d in (DIRECTION_HEAT, DIRECTION_AUTO):
        assert engage_with_latch(
            prior=MODE_HEAT, band=1.0, neutral="satisfied",
            temp=66.0, target=70.0, direction=d, **kw,
        ) == MODE_HEAT


def test_heat_direction_start_stop_points() -> None:
    """Target 64, drift 1 -> heat starts below 63 and runs to 64 (unchanged meaning)."""
    kw = {k: v for k, v in _BASE.items() if k not in ("band", "neutral")}
    idle = {"prior": None, "band": 1.0, "neutral": "satisfied", "target": 64.0,
            "direction": DIRECTION_HEAT, **kw}
    assert engage_with_latch(temp=63.0, **idle) == "satisfied"
    assert engage_with_latch(temp=62.9, **idle) == MODE_HEAT
    running = {**idle, "prior": MODE_HEAT}
    assert engage_with_latch(temp=63.9, **running) == MODE_HEAT
    assert engage_with_latch(temp=64.0, **running) == "satisfied"
    assert engage_with_latch(temp=64.1, **running) == "satisfied"


# --- integration (requires the HA test harness) -------------------------------
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache,
)

from custom_components.mxz_coordinator.const import (
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)
from tests.test_climate import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _enable,
    _recompute,
    _set_temp,
    _setup_mock_heads,
)
from tests.test_climate import _setup as _climate_setup


async def _setup(hass, **extra):
    """test_climate's setup with mode hysteresis 0 (flips are immediate)."""
    return await _climate_setup(hass, **{CONF_MODE_HYSTERESIS: 0, **extra})


async def _set_mode(hass, entity_id, *, hvac_mode=None, temperature=None):
    data = {"entity_id": entity_id}
    if temperature is not None:
        data["temperature"] = temperature
    if hvac_mode is not None and temperature is not None:
        data["hvac_mode"] = hvac_mode
        await hass.services.async_call("climate", "set_temperature", data, blocking=True)
    elif hvac_mode is not None:
        data["hvac_mode"] = hvac_mode
        await hass.services.async_call("climate", "set_hvac_mode", data, blocking=True)
    else:
        await hass.services.async_call("climate", "set_temperature", data, blocking=True)
    await hass.async_block_till_done()


async def test_thermostat_offers_and_reports_directions(hass: HomeAssistant) -> None:
    entry, _, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    sel = _eid(hass, entry, "_primary_direction")
    assert set(hass.states.get(prim).attributes["hvac_modes"]) == {
        "off", "heat", "cool", "heat_cool",
    }
    # Climate Advisor's exact call shape: set_temperature(hvac_mode=..., temperature=...)
    await _set_mode(hass, prim, hvac_mode="heat", temperature=64)
    st = hass.states.get(prim)
    assert st.state == "heat" and st.attributes["temperature"] == 64
    assert hass.states.get(sel).state == "heat"
    await _set_mode(hass, prim, hvac_mode="cool", temperature=71)
    assert hass.states.get(prim).state == "cool"
    assert hass.states.get(sel).state == "cool"
    await _set_mode(hass, prim, hvac_mode="off")
    assert hass.states.get(prim).state == "off"
    assert hass.states.get(sel).state == "cool"  # direction kept while off
    await _set_mode(hass, prim, hvac_mode="heat_cool")
    assert hass.states.get(prim).state == "heat_cool"


async def test_cool_only_room_never_heats_in_heat_season(hass: HomeAssistant) -> None:
    """The incident: cool intent at 71, house in heat, room cooling off to 64."""
    entry, head_a, _head_b = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable", "_secondary_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_mode(hass, prim, hvac_mode="cool", temperature=71)
    # Secondary room is cold and genuinely calling heat -> shared mode heat.
    await _set_temp(hass, SENSOR_B, 60)
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["state"] == "heat"
    assert plan["primary_demand"] != "heat"
    assert plan["primary_engage"] != "heat"
    assert hass.states.get(head_a).state != "heat"
    assert hass.states.get(prim).attributes["hvac_action"] != "heating"
    # Same room under the original two-sided mode WOULD heat (control case).
    await _set_mode(hass, prim, hvac_mode="heat_cool")
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] == "heat"


async def test_heat_only_room_heats_and_never_cools(hass: HomeAssistant) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_mode(hass, prim, hvac_mode="heat", temperature=64)
    # 60 is past the shared-mode demand threshold (2F), so the house flips to heat.
    await _set_temp(hass, SENSOR_A, 60)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] == "heat"
    assert hass.states.get(head_a).state == "heat"
    await _set_temp(hass, SENSOR_A, 80)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_demand"] != "cool"
    assert entry.runtime_data.data["primary_engage"] != "cool"


async def test_emergency_floor_still_heats_a_cool_only_room(hass: HomeAssistant) -> None:
    entry, _, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable", "_primary_emergency_protect")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_mode(hass, prim, hvac_mode="cool", temperature=71)
    await _set_temp(hass, SENSOR_A, 55)  # below the 60 default floor
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_emergency"] is True
    assert entry.runtime_data.data["primary_engage"] == "heat"


async def test_direction_change_publishes_provenance(hass: HomeAssistant) -> None:
    entry, _, _ = await _setup(hass)
    prim = _eid(hass, entry, "_primary_thermostat")
    await _enable(hass, entry, "_primary_enable")
    events = []
    hass.bus.async_listen("mxz_coordinator_input", lambda e: events.append(dict(e.data)))
    await _set_mode(hass, prim, hvac_mode="heat")
    await hass.async_block_till_done()
    assert events and events[-1]["kind"] == "direction"
    assert events[-1]["direction"] == "heat" and events[-1]["source"] == "service"


async def test_direction_restored_after_restart(hass: HomeAssistant) -> None:
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
        },
    )
    entry.add_to_hass(hass)  # created FIRST -> the restore is fresh
    mock_restore_cache(
        hass, [State("select.mxz_coordinator_primary_direction", "cool")]
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _eid(hass, entry, "_primary_direction") == (
        "select.mxz_coordinator_primary_direction"
    )
    assert entry.runtime_data.zones[0].direction == "cool"
    assert hass.states.get("select.mxz_coordinator_primary_direction").state == "cool"
    assert entry.runtime_data.zones[1].direction == "heat_cool"


async def test_upgrade_default_is_original_two_sided_mode(hass: HomeAssistant) -> None:
    entry, _, _ = await _setup(hass)
    await _enable(hass, entry, "_primary_enable")
    assert entry.runtime_data.zones[0].direction == "heat_cool"
    assert hass.states.get(_eid(hass, entry, "_primary_thermostat")).state == "heat_cool"


async def test_direction_change_mid_heat_run_stops_heating(hass: HomeAssistant) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_mode(hass, prim, hvac_mode="heat_cool", temperature=70)
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] == "heat"
    assert hass.states.get(head_a).state == "heat"
    # Mid-run (latched heat, room still below target): switch to cool-only.
    await _set_mode(hass, prim, hvac_mode="cool")
    await _recompute(hass, entry)
    plan = entry.runtime_data.data
    assert plan["primary_engage"] != "heat"
    assert plan["primary_demand"] != "heat"
    assert hass.states.get(head_a).state != "heat"
    # and it stays stopped on later recomputes / colder readings
    await _set_temp(hass, SENSOR_A, 62)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] != "heat"
    assert hass.states.get(head_a).state != "heat"


async def test_direction_change_mid_cool_run_stops_cooling(hass: HomeAssistant) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_mode(hass, prim, hvac_mode="heat_cool", temperature=70)
    await _set_temp(hass, SENSOR_A, 76)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] == "cool"
    assert hass.states.get(head_a).state == "cool"
    await _set_mode(hass, prim, hvac_mode="heat")
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_engage"] != "cool"
    assert hass.states.get(head_a).state != "cool"


async def test_restored_cool_room_never_commanded_heat_during_startup(
    hass: HomeAssistant,
) -> None:
    """Saved cool-only must be in force before the first physical command."""
    from homeassistant.const import EVENT_CALL_SERVICE
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 60)  # cold room: would heat under heat_cool
    await _set_temp(hass, SENSOR_B, 55)  # other room calling heat -> shared heat
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: head_a,
            CONF_SECONDARY_CLIMATE: head_b,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_MODE_HYSTERESIS: 0,
        },
    )
    entry.add_to_hass(hass)
    mock_restore_cache(
        hass,
        [
            State("select.mxz_coordinator_primary_direction", "cool"),
            State("switch.mxz_coordinator_primary_enable", "on"),
            State("switch.mxz_coordinator_secondary_enable", "on"),
            State("switch.mxz_coordinator_coordinator_enable", "on"),
        ],
    )
    calls: list[dict] = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE,
        lambda e: calls.append(dict(e.data)) if e.data.get("domain") == "climate" else None,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    coord = entry.runtime_data
    assert coord.zones[0].direction == "cool" and coord.zones[0].enable
    assert coord.coordinator_enable
    to_a = [
        c for c in calls
        if head_a in str(c.get("service_data", {}).get("entity_id", ""))
    ]
    heat_cmds = [
        c for c in to_a
        if c.get("service_data", {}).get("hvac_mode") == "heat"
    ]
    assert heat_cmds == [], heat_cmds
    assert hass.states.get(head_a).state != "heat"
    # the other room really is heating (the test exercised a heat-season plan)
    assert hass.states.get(head_b).state == "heat"
