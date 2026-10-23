"""Real coordinator/head/timer tests for settled remote target adoption."""

from datetime import timedelta

import pytest
from homeassistant.const import UnitOfTemperature
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mxz_coordinator.target_input import TARGET_SETTLE_S
from tests.test_remote_input import SENSOR_A, _recompute, _set_temp, _setup


async def _heat(hass):
    entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.set_zone_input(zone, source="service", target=72, enable=True, direction="heat")
    await _set_temp(hass, SENSOR_A, 64)
    await _recompute(hass, entry)
    assert hass.states.get(head.entity_id).state == "heat"
    assert coord._target_input[head.entity_id].matches("heat", 72)
    coord.remote_record(head.entity_id).heat = 71.5
    return entry, coord, head, zone


async def _advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


@pytest.mark.parametrize("offset", [1, 6, 10.1, 15.1, 30, 60])
async def test_real_remote_target_holds_for_15s_and_preserves_direction(hass, freezer, offset):
    entry, coord, head, zone = await _heat(hass)
    await _advance(hass, freezer, offset)
    generation = zone.input_generation
    head.remote("heat", 70)
    await hass.async_block_till_done()
    assert head.entity_id in coord._target_timers
    assert zone.target == 72
    assert coord.remote_record(head.entity_id).heat == 71.5
    # An unrelated refresh must not overwrite the physical candidate first.
    await _recompute(hass, entry)
    assert hass.states.get(head.entity_id).attributes["temperature"] == 70
    await _advance(hass, freezer, TARGET_SETTLE_S - 0.1)
    assert zone.target == 72
    await _advance(hass, freezer, 0.2)
    assert zone.target == 70
    assert zone.direction == "heat"
    assert zone.input_generation == generation + 1
    assert zone.input_source == "head_manual"
    assert zone.input_kind == "target"
    assert coord.remote_record(head.entity_id).heat == 70


@pytest.mark.parametrize("offset", [1, 6, 10.1, 30, 60])
@pytest.mark.parametrize("remembered", [71.5, 72.0])
async def test_transient_target_does_not_touch_record_or_provenance(hass, freezer, offset, remembered):
    _entry, coord, head, zone = await _heat(hass)
    coord.remote_record(head.entity_id).heat = remembered
    await _advance(hass, freezer, offset)
    generation = zone.input_generation
    head.remote("heat", 71.5)
    await hass.async_block_till_done()
    await _advance(hass, freezer, 4)
    head.remote("heat", 72)
    await hass.async_block_till_done()
    await _advance(hass, freezer, 20)
    assert zone.target == 72
    assert zone.direction == "heat"
    assert zone.input_generation == generation
    assert coord.remote_record(head.entity_id).heat == remembered
    assert head.entity_id not in coord._target_timers


@pytest.mark.parametrize("offset", [1, 10.1, 30, 60])
async def test_service_override_cancels_candidate_even_if_same_canonical_target(hass, freezer, offset):
    entry, coord, head, zone = await _heat(hass)
    generation = zone.input_generation
    await _advance(hass, freezer, offset)
    head.remote("heat", 70)
    await hass.async_block_till_done()
    await _advance(hass, freezer, 4)
    coord.set_zone_input(zone, source="service", target=72)
    await _recompute(hass, entry)
    assert hass.states.get(head.entity_id).attributes["temperature"] == 72
    await _advance(hass, freezer, 20)
    assert zone.target == 72
    assert zone.direction == "heat"
    assert coord.remote_record(head.entity_id).heat == 71.5
    assert zone.input_generation == generation
    assert head.entity_id not in coord._target_timers


@pytest.mark.parametrize("event", ["off", "cool", "unavailable", "window", "emergency", "sensor"])
async def test_protection_or_mode_change_cancels_pending_target(hass, freezer, event):
    _entry, coord, head, zone = await _heat(hass)
    head.remote("heat", 70)
    await hass.async_block_till_done()
    assert coord._target_input[head.entity_id].candidate is not None
    if event == "unavailable":
        st = hass.states.get(head.entity_id)
        hass.states.async_set(head.entity_id, "unavailable", dict(st.attributes))
    elif event == "window":
        coord._window[zone.slug] = {"paused": True}
        await coord._apply_head(head.entity_id, "off", 0, 0)
    elif event == "emergency":
        coord.data[f"{zone.slug}_emergency"] = True
        await coord._apply_head(head.entity_id, "heat", 74, 78)
    elif event == "sensor":
        hass.states.async_set(zone.sensor_id, "unavailable")
    else:
        await coord._apply_head(head.entity_id, event, 70, 74)
    await hass.async_block_till_done()
    assert coord._target_input[head.entity_id].candidate is None
    await _advance(hass, freezer, 20)
    assert coord.remote_record(head.entity_id).heat == 71.5


async def test_fan_change_during_candidate_does_not_reset_its_clock(hass, freezer):
    entry, coord, head, zone = await _heat(hass)
    head.remote("heat", 70)
    await hass.async_block_till_done()
    since = coord._target_input[head.entity_id].candidate.since
    await _advance(hass, freezer, 10)
    # A supported speed outside boost history avoids the existing last/prior
    # token echo guard, which intentionally absorbs reselected boost speeds.
    assert head._attr_fan_mode != "Very High"
    head.remote("heat", 70, "Very High")
    await hass.async_block_till_done()
    # Exercise the apply cycle explicitly; frozen clock jumps do not drain
    # the newer HA coordinator's event-loop debouncer automatically.
    await _recompute(hass, entry)
    assert coord._target_input[head.entity_id].candidate.since == since
    assert coord.data["zones"][0]["fan_hold"] is True
    await _advance(hass, freezer, 5.1)
    assert zone.target == 70
    assert zone.direction == "heat"
    assert coord.remote_record(head.entity_id).heat == 70
    assert coord.data["zones"][0]["fan_hold"] is True


async def test_new_report_restarts_candidate_deadline(hass, freezer):
    _entry, _coord, head, zone = await _heat(hass)
    head.remote("heat", 70)
    await hass.async_block_till_done()
    await _advance(hass, freezer, 10)
    head.remote("heat", 69)
    await hass.async_block_till_done()
    await _advance(hass, freezer, 5.1)  # old deadline; new value has held only 5 s
    assert zone.target == 72
    await _advance(hass, freezer, 10)
    assert zone.target == 69
    assert zone.direction == "heat"


async def test_failed_target_command_retains_previous_expectation(hass, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    _entry, coord, head, _zone = await _heat(hass)
    previous = dict(coord._target_input[head.entity_id].commanded)

    async def fail(*args, **kwargs):
        raise HomeAssistantError("Simulated rejected command")

    with monkeypatch.context() as patch:
        patch.setattr(type(hass.services), "async_call", fail)
        with pytest.raises(HomeAssistantError):
            await coord.async_head_call(
                "set_temperature",
                {"entity_id": head.entity_id, "hvac_mode": "heat", "temperature": 75},
            )
    assert coord._target_input[head.entity_id].commanded == previous


async def test_failed_unrelated_fan_write_does_not_erase_target_candidate(hass, freezer, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    _entry, coord, head, zone = await _heat(hass)
    head.remote("heat", 70)
    await hass.async_block_till_done()
    candidate = coord._target_input[head.entity_id].candidate

    async def fail(*args, **kwargs):
        raise HomeAssistantError("Simulated rejected fan command")

    with monkeypatch.context() as patch:
        patch.setattr(type(hass.services), "async_call", fail)
        with pytest.raises(HomeAssistantError):
            await coord.async_head_call(
                "set_fan_mode", {"entity_id": head.entity_id, "fan_mode": "quiet"}
            )
    assert coord._target_input[head.entity_id].candidate is candidate
    await _advance(hass, freezer, TARGET_SETTLE_S + 0.1)
    assert zone.target == 70


async def test_unload_cancels_candidate_and_timer(hass, freezer):
    _entry, coord, head, zone = await _heat(hass)
    head.remote("heat", 70)
    await hass.async_block_till_done()
    assert head.entity_id in coord._target_timers
    await coord.async_shutdown_listeners()
    assert not coord._target_timers
    assert not coord._target_input
    await _advance(hass, freezer, 30)
    assert zone.target == 72


async def test_new_emergency_plan_cancels_candidate_even_with_same_target(hass):
    entry, coord, head, zone = await _heat(hass)
    head.remote("heat", 70)
    await hass.async_block_till_done()
    assert coord._target_input[head.entity_id].candidate is not None
    assert not coord.data["primary_emergency"]
    zone.emergency_protect = True
    zone.emergency_floor = 60
    await _set_temp(hass, SENSOR_A, 55)
    await _recompute(hass, entry)
    assert coord.data["primary_emergency"]
    assert coord._target_input[head.entity_id].candidate is None
    assert hass.states.get(head.entity_id).attributes["temperature"] == 72


async def test_native_half_celsius_representation_is_not_a_remote_target(hass, freezer):
    _entry, coord, head, zone = await _heat(hass)
    # The running Mahtanar advertises native 0.5 C steps and half-degree display.
    head._attr_temperature_unit = UnitOfTemperature.CELSIUS
    head._attr_target_temperature_step = 0.5
    head._attr_precision = 0.5
    # Entity attributes are cached; clear only the native metadata cached here.
    head.__dict__.pop("temperature_unit", None)
    head.__dict__.pop("target_temperature_step", None)
    assert coord._target_equivalents(head.entity_id, 72) == (71.5,)
    coord._target_input[head.entity_id].command("heat", 72, 71.5)
    generation = zone.input_generation
    # Inject HA's converted state; raw simulator values above were Fahrenheit.
    from homeassistant.core import State

    coord._classify_head_input(
        head.entity_id,
        State(head.entity_id, "heat", {"temperature": 72, "fan_mode": "low"}),
        State(head.entity_id, "heat", {"temperature": 71.5, "fan_mode": "low"}),
    )
    await _advance(hass, freezer, 60)
    assert zone.target == 72
    assert zone.input_generation == generation
    assert coord.remote_record(head.entity_id).heat == 71.5


@pytest.mark.parametrize(
    ("direction", "old_mode", "new_mode", "expected"),
    [("heat", "off", "heat", "heat"), ("cool", "off", "cool", "cool"),
     ("heat", "off", "cool", "heat_cool"), ("cool", "off", "heat", "heat_cool"),
     ("heat", "heat", "cool", "heat_cool"), ("cool", "cool", "heat", "heat_cool"),
     ("heat", "heat", "heat_cool", "heat_cool"),
     ("cool", "cool", "auto", "heat_cool"), ("heat", "heat", "off", "heat")],
)
async def test_remote_mode_only_reopens_disallowed_direction(
    hass, direction, old_mode, new_mode, expected
):
    from homeassistant.core import State

    from tests.test_remote_input import _age_commands

    _entry, coord, head, zone = await _heat(hass)
    coord.set_zone_input(zone, source="service", direction=direction)
    _age_commands(coord)
    shared = coord.current_shared_mode
    coord._classify_head_input(
        head.entity_id,
        State(head.entity_id, old_mode, {"temperature": 72, "fan_mode": "low"}),
        State(head.entity_id, new_mode, {"temperature": 72, "fan_mode": "low"}),
    )
    assert zone.direction == expected
    assert coord.current_shared_mode == shared


async def test_command_diagnostic_uses_live_native_metadata(hass, monkeypatch):
    from unittest.mock import AsyncMock

    _entry, coord, head, _zone = await _heat(hass)
    head._attr_temperature_unit = UnitOfTemperature.CELSIUS
    head._attr_target_temperature_step = 0.5
    head._attr_precision = 0.5
    head.__dict__.pop("temperature_unit", None)
    head.__dict__.pop("target_temperature_step", None)
    monkeypatch.setattr(type(hass.services), "async_call", AsyncMock())
    await coord.async_head_call(
        "set_temperature",
        {"entity_id": head.entity_id, "hvac_mode": "heat", "temperature": 72},
    )
    monkeypatch.setattr(coord, "_apply", AsyncMock())
    plan = await coord._async_update_data()
    assert plan["zones"][0]["target_equivalents"] == {
        "target": 72, "equivalents": [71.5]
    }
    assert plan["zones"][0]["target_commands"]["heat"] == {
        "target": 72, "equivalents": [71.5]
    }
    coord._target_input.pop(head.entity_id)
    plan = await coord._async_update_data()
    assert plan["zones"][0]["target_commands"] == {}
    assert plan["zones"][0]["target_equivalents"] == {
        "target": 72, "equivalents": [71.5]
    }


async def test_missing_metadata_does_not_guess_alias(hass, freezer, monkeypatch):
    from homeassistant.helpers.entity_component import DATA_INSTANCES

    entry, coord, head, zone = await _heat(hass)
    head._attr_precision = 0.5
    component = hass.data[DATA_INSTANCES]["climate"]
    with monkeypatch.context() as patch:
        patch.setattr(component, "get_entity", lambda _entity_id: None)
        assert coord._target_equivalents(head.entity_id, 72) == ()
        await coord.async_head_call(
            "set_temperature",
            {"entity_id": head.entity_id, "hvac_mode": "heat", "temperature": 72},
        )
        await _recompute(hass, entry)
    assert coord.data["zones"][0]["target_commands"]["heat"] == {
        "target": 72, "equivalents": []
    }
    assert coord.data["zones"][0]["target_equivalents"] == {
        "target": 72, "equivalents": []
    }
    generation = zone.input_generation
    # Prove the persistent report reaches adoption, even though rounding makes
    # its canonical target equal to 72 and therefore leaves provenance unchanged.
    coord.remote_record(head.entity_id).heat = 70
    head.remote("heat", 71.5)
    await hass.async_block_till_done()
    assert coord._target_input[head.entity_id].candidate is not None, dict(hass.states.get(head.entity_id).attributes)
    await _advance(hass, freezer, 14.9)
    assert zone.input_generation == generation
    assert coord.remote_record(head.entity_id).heat == 70
    await _advance(hass, freezer, 0.2)
    assert coord._target_input[head.entity_id].candidate is None
    assert coord.remote_record(head.entity_id).heat == 71.5
    assert zone.target == 72


async def test_pending_target_allows_fan_and_vane_writes(hass):
    from tests.test_remote_input import _calls

    entry, coord, head, zone = await _heat(hass)
    head.remote("heat", 70, "auto")
    await hass.async_block_till_done()
    candidate = coord._target_input[head.entity_id].candidate
    assert candidate is not None
    coord._fan_idx[head.entity_id] = 0
    calls = _calls(hass)
    await _recompute(hass, entry)
    assert any(c["service"] == "set_fan_mode" for c in calls)
    assert not any(c["service"] == "set_temperature" for c in calls)

    async def select_option(call):
        pass

    hass.services.async_register("select", "select_option", select_option)
    await coord.async_apply_vane(head.entity_id, "select.test_vane", "SWING")
    await hass.async_block_till_done()
    assert any(c["domain"] == "select" and c["service"] == "select_option" for c in calls)
    assert coord._target_input[head.entity_id].candidate == candidate
    assert zone.target == 72
