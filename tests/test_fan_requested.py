"""Room thermostat fan_mode = REQUESTED setting (local fork, 2026-10-17).

Incident: Climate Advisor saw the room thermostat's fan go auto -> low on every
MXZ boost step / firmware ramp and opened a manual-override grace (30 min to
4 h) each time, deferring its own schedule. The room thermostat now reports
``auto`` unless a person is genuinely holding a speed; the momentary speed is
the ``fan_actual`` attribute.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from tests.test_drive import (
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_temp,
    _setup_fan_boost,
    _setup_mock_heads,
    _user_set_fan,
)


async def _rig(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM
    head_a, head_b = await _setup_mock_heads(hass)
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = await _setup_fan_boost(hass, head_a, head_b)
    return entry, head_a, _eid(hass, entry, "_primary_thermostat")


async def test_boost_ladder_speeds_never_show_as_a_fan_change(hass: HomeAssistant) -> None:
    entry, head_a, prim = await _rig(hass)
    seen: list = []
    hass.bus.async_listen(
        "state_changed",
        lambda e: seen.append(e.data["new_state"].attributes.get("fan_mode"))
        if e.data["entity_id"] == prim and e.data.get("new_state") else None,
    )
    for t in (67.0, 66.0, 64.0, 63.0, 62.5, 62.0, 61.5):
        await _set_temp(hass, SENSOR_A, t)
        await _recompute(hass, entry)
    speeds = {hass.states.get(head_a).attributes["fan_mode"]}
    await _set_temp(hass, SENSOR_A, 67.0)
    await _recompute(hass, entry)
    speeds.add(hass.states.get(head_a).attributes["fan_mode"])
    assert speeds - {"auto"}, "boost should have driven a real speed on the head"
    assert set(seen) == {"auto"}, seen  # requested setting never moved
    assert hass.states.get(prim).attributes["fan_actual"] in speeds


async def test_genuine_manual_hold_is_still_reported(hass: HomeAssistant) -> None:
    entry, head_a, prim = await _rig(hass)
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "quiet")
    await _set_temp(hass, SENSOR_A, 67.1)
    await _recompute(hass, entry)
    assert hass.states.get(head_a).attributes["fan_mode"] == "quiet"
    assert hass.states.get(prim).attributes["fan_mode"] == "quiet"


async def test_releasing_the_hold_reports_auto_again(hass: HomeAssistant) -> None:
    entry, head_a, prim = await _rig(hass)
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    await _user_set_fan(hass, head_a, "quiet")
    await _recompute(hass, entry)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, entry, "_primary_fan_auto")},
        blocking=True,
    )
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert hass.states.get(prim).attributes["fan_mode"] == "auto"


async def test_boost_step_in_flight_is_not_a_fan_change(hass: HomeAssistant) -> None:
    """2026-10-19 live: auto -> low -> auto within one second on each boost step.

    _write_fan records the new ladder token before the head applies it. A
    render inside that window (head still on the previous ladder token) must
    keep reporting ``auto``; Climate Advisor read the blip as a person turning
    the fan off and paused the room for 30 minutes each time.
    """
    entry, head_a, prim = await _rig(hass)
    await _set_temp(hass, SENSOR_A, 67)
    await _recompute(hass, entry)
    coord = entry.runtime_data
    old = hass.states.get(head_a).attributes["fan_mode"]
    assert old != "auto"
    assert hass.states.get(prim).attributes["fan_mode"] == "auto"
    # Boost decides on a different rung; the head has not applied it yet.
    new = next(t for t in ("quiet", "low", "medium", "high") if t != old)
    coord._fan_prev[head_a] = old
    coord._fan_cmd[head_a] = new
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(prim).attributes["fan_mode"] == "auto"
    assert hass.states.get(prim).attributes["fan_actual"] == old
