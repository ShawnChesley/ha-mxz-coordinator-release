"""Window/door pause (local fork, 2026-10-17).

Requirements (from the 2026-10-17 Master Bedroom incident review):
* an open window past the delay pauses the room against EVERY input: Climate
  Advisor-style service calls, the IR remote, schedules/dashboards;
* emergency heat through an open window only with the explicit per-room switch,
  and it notifies;
* closing re-evaluates the room's CURRENT settings (no restore of an old target);
* survives a restart; unavailable contacts hold the last known state.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.mxz_coordinator.window_pause import (
    EVENT_OPENED,
    EVENT_PAUSED,
    EVENT_RESUMED,
    pause_deadline,
    window_label,
    window_step,
)

D = 600.0


# --- pure logic --------------------------------------------------------------
def test_open_then_pause_after_delay_then_resume_on_close() -> None:
    st, ev = window_step(state=None, contacts=["on"], now=1000.0, delay=D)
    assert ev == EVENT_OPENED and st["open_since"] == 1000.0 and not st["paused"]
    assert pause_deadline(st, D) == 1600.0
    st2, ev = window_step(state=st, contacts=["on"], now=1599.0, delay=D)
    assert ev is None and st2 is st
    st3, ev = window_step(state=st2, contacts=["on"], now=1600.0, delay=D)
    assert ev == EVENT_PAUSED and st3["paused"]
    assert window_label(st3, True) == "paused"
    st4, ev = window_step(state=st3, contacts=["off"], now=2000.0, delay=D)
    assert ev == EVENT_RESUMED and not st4["paused"] and st4["open_since"] is None


def test_closed_before_delay_never_pauses() -> None:
    st, _ = window_step(state=None, contacts=["on"], now=0.0, delay=D)
    st, ev = window_step(state=st, contacts=["off"], now=300.0, delay=D)
    assert ev == EVENT_RESUMED and not st["paused"]
    st, ev = window_step(state=st, contacts=["off"], now=10_000.0, delay=D)
    assert not st["paused"]


def test_any_of_several_contacts_open_counts() -> None:
    st, _ = window_step(state=None, contacts=["off", "on"], now=0.0, delay=D)
    st, ev = window_step(state=st, contacts=["off", "on"], now=D, delay=D)
    assert st["paused"] and ev == EVENT_PAUSED
    st, _ = window_step(state=st, contacts=["off", "off"], now=D + 1, delay=D)
    assert not st["paused"]


@pytest.mark.parametrize("bad", ["unavailable", "unknown", None])
def test_unknown_holds_last_known(bad) -> None:
    # paused stays paused
    st, _ = window_step(state=None, contacts=["on"], now=0.0, delay=D)
    st, _ = window_step(state=st, contacts=["on"], now=D, delay=D)
    st, _ = window_step(state=st, contacts=[bad], now=D + 5, delay=D)
    assert st["paused"] and st["sensor_unknown"]
    # closed stays closed
    st, _ = window_step(state=None, contacts=["off"], now=0.0, delay=D)
    st, _ = window_step(state=st, contacts=[bad], now=10_000.0, delay=D)
    assert not st["paused"]
    # open (delay running) keeps counting and pauses on time
    st, _ = window_step(state=None, contacts=["on"], now=0.0, delay=D)
    st, _ = window_step(state=st, contacts=[bad], now=D - 1, delay=D)
    assert not st["paused"]
    st, _ = window_step(state=st, contacts=[bad], now=D, delay=D)
    assert st["paused"]


def test_unknown_plus_open_is_open() -> None:
    st, _ = window_step(state=None, contacts=["unavailable", "on"], now=0.0, delay=D)
    assert st["open_since"] == 0.0


# --- integration (HA test harness) --------------------------------------------
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
)

from custom_components.mxz_coordinator.const import (
    CONF_MODE_HYSTERESIS,
    CONF_NOTIFY_SERVICE,
    CONF_PRIMARY_CONTACTS,
    CONF_WINDOW_PAUSE_DELAY,
)
from tests.test_climate import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _enable,
    _recompute,
    _set_temp,
)
from tests.test_climate import _setup as _climate_setup

WIN = "binary_sensor.room_a_window"


async def _setup(hass, **extra):
    hass.states.async_set(WIN, "off")
    return await _climate_setup(
        hass,
        **{
            CONF_MODE_HYSTERESIS: 0,
            CONF_PRIMARY_CONTACTS: [WIN],
            CONF_WINDOW_PAUSE_DELAY: 600,
            **extra,
        },
    )


async def _advance(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _heat_room(hass, entry, prim, target=70):
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": prim, "hvac_mode": "heat", "temperature": target}, blocking=True,
    )
    await hass.async_block_till_done()


def _heating(hass, entry, head_a) -> bool:
    plan = entry.runtime_data.data
    return plan["primary_engage"] == "heat" or hass.states.get(head_a).state == "heat"


async def _open_window_and_pause(hass, entry, freezer):
    hass.states.async_set(WIN, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)  # the compute that stamps open_since
    freezer.tick(timedelta(seconds=601))
    await _advance(hass, 0)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_window"] == "paused"


async def test_window_pause_stops_active_heating(hass: HomeAssistant, freezer) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_temp(hass, SENSOR_A, 62)
    await _heat_room(hass, entry, prim)
    await _recompute(hass, entry)
    assert _heating(hass, entry, head_a)

    # Window opens: keeps heating until the delay passes...
    hass.states.async_set(WIN, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_window"] == "open"
    assert _heating(hass, entry, head_a)
    # ...then the delay timer itself pauses it (no other trigger needed).
    freezer.tick(timedelta(seconds=601))
    await _advance(hass, 0)
    await hass.async_block_till_done()
    assert entry.runtime_data.data["primary_window"] == "paused"
    assert not _heating(hass, entry, head_a)
    st = hass.states.get(prim)
    assert st.attributes["hvac_action"] == "off"
    assert st.attributes["window"] == "paused"
    assert st.state == "heat"  # the room's own settings are untouched


async def test_inputs_during_pause_are_recorded_not_acted_on(
    hass: HomeAssistant, freezer
) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_temp(hass, SENSOR_A, 60)
    await _heat_room(hass, entry, prim, 70)
    await _open_window_and_pause(hass, entry, freezer)
    coord = entry.runtime_data
    zone = coord.zones[0]
    # CA-style command, dashboard number, and a person at the head -- all ignored.
    await _heat_room(hass, entry, prim, 66)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 65}, blocking=True,
    )
    coord.set_zone_input(zone, source="head_manual", target=64, enable=True)
    await coord.async_user_changed()
    await _recompute(hass, entry)
    assert zone.target == 64 and zone.enable  # recorded
    assert not _heating(hass, entry, head_a)  # not acted on
    # Close: re-evaluates CURRENT settings (64 via heat_cool from the remote),
    # not the 70 that was active when the window opened.
    hass.states.async_set(WIN, "off")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert coord.data["primary_window"] == "closed"
    assert _heating(hass, entry, head_a)
    head = hass.states.get(head_a)
    assert head.attributes.get("target_temp_low") == 64  # heat edge = current target


async def test_ir_remote_press_cannot_restart_a_paused_room(
    hass: HomeAssistant, freezer
) -> None:
    from tests.test_remote_input import _age_commands
    from tests.test_remote_input import _setup as _remote_setup

    hass.states.async_set(WIN, "off")
    entry, coord, heads = await _remote_setup(
        hass, **{CONF_PRIMARY_CONTACTS: [WIN], CONF_WINDOW_PAUSE_DELAY: 600}
    )
    head = heads[0]
    await _set_temp(hass, SENSOR_A, 60)
    await _recompute(hass, entry)
    await _open_window_and_pause(hass, entry, freezer)
    # (frozen clock: step past HA's 5 s service-context reuse window, or the
    # packets would carry our park command's context and read as our echo)
    freezer.tick(timedelta(seconds=6))
    _age_commands(coord)
    head.remote("heat", 72.0, "auto")  # first packet: recorded
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=6))
    _age_commands(coord)
    head.remote("heat", 74.0, "auto")  # a real temperature press -> adopted
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert coord.zones[0].target == 74.0  # adopted into the room...
    assert coord.data["primary_engage"] != "heat"  # ...but the room stays paused
    assert hass.states.get(head.entity_id).state != "heat"


async def test_emergency_heat_through_open_window_requires_explicit_switch(
    hass: HomeAssistant, freezer
) -> None:
    calls: list = []
    hass.services.async_register("notify", "test", lambda c: calls.append(c.data))
    entry, head_a, _ = await _setup(hass, **{CONF_NOTIFY_SERVICE: "notify.test"})
    await _enable(hass, entry, "_coordinator_enable", "_primary_enable",
                  "_primary_emergency_protect")
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    await _open_window_and_pause(hass, entry, freezer)
    await _set_temp(hass, SENSOR_A, 55)  # below the 60 emergency floor
    await _recompute(hass, entry)
    assert not _heating(hass, entry, head_a)  # default: the open window wins
    assert entry.runtime_data.data["primary_emergency"] is False

    await _enable(hass, entry, "_primary_window_freeze_protect")
    await _recompute(hass, entry)
    await hass.async_block_till_done()
    assert entry.runtime_data.data["primary_emergency"] is True
    assert entry.runtime_data.data["state"] == "heat"
    assert hass.states.get(head_a).state == "heat"
    assert any("window/door open - heating anyway" in c.get("message", "") for c in calls)


async def test_pause_and_delay_clock_survive_restart(hass: HomeAssistant, freezer) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_temp(hass, SENSOR_A, 60)
    await _heat_room(hass, entry, prim)
    await _open_window_and_pause(hass, entry, freezer)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_window"] == "paused"  # no new delay
    assert not _heating(hass, entry, head_a)

    # Closed while HA was down -> resumes on the first compute after restart.
    hass.states.async_set(WIN, "off")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_window"] == "closed"
    assert _heating(hass, entry, head_a)


async def test_delay_clock_is_not_restarted_by_a_restart(hass: HomeAssistant, freezer) -> None:
    entry, _head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_temp(hass, SENSOR_A, 60)
    await _heat_room(hass, entry, prim)
    hass.states.async_set(WIN, "on")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    freezer.tick(timedelta(seconds=400))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=201))  # 601 s since the window opened
    await _advance(hass, 0)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_window"] == "paused"


async def test_unavailable_contact_holds_pause(hass: HomeAssistant, freezer) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_temp(hass, SENSOR_A, 60)
    await _heat_room(hass, entry, prim)
    await _open_window_and_pause(hass, entry, freezer)
    hass.states.async_set(WIN, "unavailable")
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_window"] == "paused"
    assert not _heating(hass, entry, head_a)


async def test_unavailable_contact_does_not_pause_a_closed_room(
    hass: HomeAssistant, freezer
) -> None:
    entry, head_a, _ = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable")
    prim = _eid(hass, entry, "_primary_thermostat")
    await _set_temp(hass, SENSOR_A, 60)
    await _heat_room(hass, entry, prim)
    await _recompute(hass, entry)
    hass.states.async_set(WIN, "unavailable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(hours=2))
    await _advance(hass, 0)
    await _recompute(hass, entry)
    assert entry.runtime_data.data["primary_window"] == "closed"
    assert _heating(hass, entry, head_a)


async def test_other_rooms_unaffected_and_no_contacts_means_no_pause(
    hass: HomeAssistant, freezer
) -> None:
    entry, _head_a, head_b = await _setup(hass)
    await _enable(hass, entry, "_coordinator_enable", "_secondary_enable")
    await _set_temp(hass, SENSOR_B, 60)
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": _eid(hass, entry, "_secondary_thermostat"),
         "hvac_mode": "heat", "temperature": 70}, blocking=True,
    )
    await _recompute(hass, entry)
    await _open_window_and_pause(hass, entry, freezer)
    assert entry.runtime_data.data["secondary_window"] == "none"
    assert hass.states.get(head_b).state == "heat"
