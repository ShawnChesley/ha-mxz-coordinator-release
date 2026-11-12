"""Remote (IR) input adoption inside the coordinator.

Pure-logic replays of the Kitchen head captures (2026-10-07), then integration
tests against the real coordinator with a Mahtanar-like head (supported_features
395: single ``temperature`` in heat/cool plus firmware-synthesized low/high).
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from custom_components.mxz_coordinator.remote_input import (
    HeadObs,
    RemoteRecord,
    classify_external,
    obs_from_state,
    relevant_change,
)


class _S:  # minimal State stand-in for the pure tests
    def __init__(self, state: str, **attrs: Any) -> None:
        self.state = state
        self.attributes = attrs


# --------------------------------------------------------------------------
# Pure classifier — replays of the captured Kitchen traces
# --------------------------------------------------------------------------


def test_obs_ignores_synthesized_low_high_and_off_temperature() -> None:
    off = obs_from_state(
        _S(
            "off",
            temperature=None,
            target_temp_low=68.5,
            target_temp_high=71.5,
            fan_mode="auto",
        )
    )
    fan = obs_from_state(
        _S(
            "fan_only",
            temperature=None,
            target_temp_low=68.5,
            target_temp_high=72.0,
            fan_mode="auto",
        )
    )
    assert off == HeadObs("off", None, "auto")
    assert fan == HeadObs("fan_only", None, "auto")
    # The 12:02:27 FAN_ONLY entry changed only the synthesized high (70 -> 72).
    a = obs_from_state(
        _S("fan_only", temperature=None, target_temp_high=70.0, fan_mode="auto")
    )
    b = obs_from_state(
        _S("fan_only", temperature=None, target_temp_high=72.0, fan_mode="auto")
    )
    assert not relevant_change(a, b)


def test_current_temperature_and_action_are_not_relevant() -> None:
    a = obs_from_state(
        _S("off", current_temperature=68.0, hvac_action="idle", fan_mode="auto")
    )
    b = obs_from_state(
        _S("off", current_temperature=67.0, hvac_action="idle", fan_mode="auto")
    )
    assert not relevant_change(a, b)


def test_unavailable_is_never_an_observation() -> None:
    assert obs_from_state(_S("unavailable")) is None
    assert obs_from_state(None) is None


def test_trace_b_first_press_unknown_record_turns_on_but_adopts_no_temperature() -> (
    None
):
    """12:00:31 OFF -> COOL 70 with nothing recorded yet: power on only."""
    rec = RemoteRecord()
    intent = classify_external(
        HeadObs("off", None, "auto"), HeadObs("cool", 70.0, "auto"), rec, step=1.0
    )
    assert intent.manual_on and intent.adopt_target is None
    assert rec.cool == 70.0  # recorded for next time


def test_down_press_on_parked_head_adopts_new_temperature() -> None:
    """12:22:19 remote COOL 69 -> 68 (▼ once) on an OFF-parked head."""
    rec = RemoteRecord(cool=69.0)
    intent = classify_external(
        HeadObs("off", None, "auto"), HeadObs("cool", 68.0, "auto"), rec, step=1.0
    )
    assert intent.adopt_target == 68.0
    assert intent.manual_on
    assert rec.cool == 68.0


def test_trace_c_fan_only_press_resends_stale_temperature_no_adoption() -> None:
    """12:28:49 FAN button: OFF -> COOL 68 QUIET; remote's cool was already 68."""
    rec = RemoteRecord(cool=68.0)
    intent = classify_external(
        HeadObs("off", None, "auto"), HeadObs("cool", 68.0, "quiet"), rec, step=1.0
    )
    assert intent.adopt_target is None
    assert intent.manual_on  # the head did wake (power info from the transition)


def test_trace_f_remote_off_while_running_is_manual_off() -> None:
    rec = RemoteRecord(cool=68.0)
    intent = classify_external(
        HeadObs("cool", 68.0, "quiet"), HeadObs("off", None, "quiet"), rec, step=1.0
    )
    assert intent.manual_off and intent.adopt_target is None


def test_per_mode_memory_mode_switch_is_not_a_temperature_change() -> None:
    """Remote COOL 68 -> MODE -> HEAT 70: different modes are never compared."""
    rec = RemoteRecord(cool=68.0)
    intent = classify_external(
        HeadObs("cool", 68.0, "auto"), HeadObs("heat", 70.0, "auto"), rec, step=1.0
    )
    assert intent.adopt_target is None  # no heat record yet -> record only
    assert rec.heat == 70.0 and rec.cool == 68.0
    back = classify_external(
        HeadObs("heat", 70.0, "auto"), HeadObs("cool", 68.0, "auto"), rec, step=1.0
    )
    assert back.adopt_target is None


def test_half_step_resolution() -> None:
    rec = RemoteRecord(cool=21.0)
    assert (
        classify_external(
            HeadObs("cool", 21.0, "auto"), HeadObs("cool", 21.5, "auto"), rec, step=0.5
        ).adopt_target
        == 21.5
    )


@pytest.mark.parametrize("mode", ["heat", "cool"])
@pytest.mark.parametrize("remembered", [None, 68.0])
def test_fan_only_update_does_not_adopt_or_record_temperature(mode, remembered):
    """A stale IR record is not evidence of a new temperature button press."""
    record = RemoteRecord()
    if remembered is not None:
        record.set(mode, remembered)
    intent = classify_external(
        HeadObs(mode, 72.0, "low"), HeadObs(mode, 72.0, "auto"),
        record, step=0.5,
    )
    assert not intent.any
    assert not intent.record_changed
    assert record.get(mode) == remembered


def test_record_round_trip() -> None:
    rec = RemoteRecord.from_dict({"cool": "68", "heat": None, "junk": 1})
    assert rec.as_dict() == {"cool": 68.0, "heat": None}
    assert RemoteRecord.from_dict("garbage").as_dict() == {"cool": None, "heat": None}


# --------------------------------------------------------------------------
# Integration — real coordinator, Mahtanar-like head
# --------------------------------------------------------------------------

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.mxz_coordinator.const import (
    CONF_FAN_BOOST_ENABLE,
    CONF_IDLE_ACTION,
    CONF_MODE_HYSTERESIS,
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
    IDLE_ACTION_OFF,
)
from tests.test_drive import (
    EVENT_CALL_SERVICE,
    SENSOR_A,
    SENSOR_B,
    _eid,
    _recompute,
    _set_temp,
)


class MitpHead(ClimateEntity):
    """Mahtanar/CN105-like head: features 395, single ``temperature`` in
    heat/cool, firmware low/high that do NOT track MXZ's range writes."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = 0.5
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.HEAT_COOL,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]
    _attr_fan_modes: ClassVar[list[str]] = [
        "auto",
        "low",
        "medium",
        "high",
        "quiet",
        "Very High",
    ]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, suffix: str) -> None:
        self._attr_unique_id = f"mitp_head_{suffix}"
        self._attr_name = f"Mitp Head {suffix}"
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_mode = "auto"
        self._cool_sp = 72.0
        self._heat_sp = 68.5
        self._attr_target_temperature_low = 68.5
        self._attr_target_temperature_high = 71.5

    @property
    def target_temperature(self) -> float | None:
        if self._attr_hvac_mode == HVACMode.COOL:
            return self._cool_sp
        if self._attr_hvac_mode == HVACMode.HEAT:
            return self._heat_sp
        return None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        if hvac_mode == HVACMode.FAN_ONLY:
            self._attr_target_temperature_high = 72.0  # firmware noise
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        mode = kwargs.get("hvac_mode")
        if mode is not None:
            self._attr_hvac_mode = mode
        low, high = kwargs.get("target_temp_low"), kwargs.get("target_temp_high")
        if self._attr_hvac_mode == HVACMode.COOL and high is not None:
            self._cool_sp = high
        if self._attr_hvac_mode == HVACMode.HEAT and low is not None:
            self._heat_sp = low
        if (t := kwargs.get("temperature")) is not None:
            if self._attr_hvac_mode == HVACMode.COOL:
                self._cool_sp = t
            elif self._attr_hvac_mode == HVACMode.HEAT:
                self._heat_sp = t
        self.async_write_ha_state()

    def remote(
        self, mode: str, temp: float | None = None, fan: str | None = None
    ) -> None:
        """A full-state IR packet: written with NO coordinator context."""
        self._attr_hvac_mode = HVACMode(mode)
        if temp is not None:
            if mode == "cool":
                self._cool_sp = temp
                self._attr_target_temperature_high = temp
            elif mode == "heat":
                self._heat_sp = temp
        if fan is not None:
            self._attr_fan_mode = fan
        self.async_write_ha_state()


async def _setup(hass: HomeAssistant, **extra: Any):
    hass.config.units = US_CUSTOMARY_SYSTEM
    heads = [MitpHead("a"), MitpHead("b")]

    async def _plat(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(heads)

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.climate", MockPlatform(async_setup_platform=_plat))
    assert await async_setup_component(
        hass, "climate", {"climate": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    await _set_temp(hass, SENSOR_A, 70)
    await _set_temp(hass, SENSOR_B, 70)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="MXZ Coordinator",
        data={
            CONF_PRIMARY_CLIMATE: heads[0].entity_id,
            CONF_SECONDARY_CLIMATE: heads[1].entity_id,
            CONF_PRIMARY_SENSOR: SENSOR_A,
            CONF_SECONDARY_SENSOR: SENSOR_B,
            CONF_FAN_BOOST_ENABLE: True,
            CONF_IDLE_ACTION: IDLE_ACTION_OFF,
            CONF_MODE_HYSTERESIS: 0,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for suffix in ("_primary_enable", "_secondary_enable", "_coordinator_enable"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _eid(hass, entry, suffix)}, blocking=True
        )
    await hass.async_block_till_done()
    coord = entry.runtime_data
    return entry, coord, heads


def _age_commands(coord) -> None:
    """Model wall-clock passing since MXZ's last head commands (beyond the
    fallback echo window) so the next remote press is judged on its merits."""
    for cid, (ts, mode, fan) in list(coord._last_head_cmd.items()):
        coord._last_head_cmd[cid] = (ts - 3600.0, mode, fan)


def _calls(hass: HomeAssistant) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    hass.bus.async_listen(
        EVENT_CALL_SERVICE, callback(lambda e: out.append(dict(e.data)))
    )
    return out


def _inputs(hass: HomeAssistant) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    hass.bus.async_listen(
        "mxz_coordinator_input", callback(lambda e: out.append(dict(e.data)))
    )
    return out


@pytest.mark.parametrize("mode", ["heat", "cool"])
async def test_fan_only_keeps_direction_target_and_provenance(hass, mode):
    """Replay Great Room's LOW -> AUTO with an older IR temperature record."""
    from homeassistant.core import State

    _entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.set_zone_input(zone, source="service", target=72, enable=True, direction=mode)
    coord.remote_record(head.entity_id).set(mode, 68.0)
    before = (zone.direction, zone.target, zone.enable, zone.input_generation,
              zone.input_source, zone.input_kind)
    _age_commands(coord)
    coord._classify_head_input(
        head.entity_id,
        State(head.entity_id, mode, {"temperature": 72.0, "fan_mode": "low"}),
        State(head.entity_id, mode, {"temperature": 72.0, "fan_mode": "auto"}),
    )
    assert (zone.direction, zone.target, zone.enable, zone.input_generation,
            zone.input_source, zone.input_kind) == before


@pytest.mark.parametrize("mode", ["heat", "cool"])
@pytest.mark.parametrize("fan", ["low", "auto"])
async def test_real_target_change_preserves_direction_without_mode_change(hass, mode, fan):
    from homeassistant.core import State

    _entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.set_zone_input(zone, source="service", target=72, enable=True, direction=mode)
    coord.remote_record(head.entity_id).set(mode, 72.0)
    _age_commands(coord)
    coord._classify_head_input(
        head.entity_id,
        State(head.entity_id, mode, {"temperature": 72.0, "fan_mode": "low"}),
        State(head.entity_id, mode, {"temperature": 71.0, "fan_mode": fan}),
    )
    assert zone.target == 71.0
    assert zone.direction == mode
    assert zone.input_source == "head_manual"
    assert zone.input_kind == "target"


@pytest.mark.parametrize("restore_at", [*range(6, 61), 10.1])
async def test_oct22_fan_and_setpoint_blip_replay(hass, monkeypatch, restore_at):
    """The captured replay, including slight jitter at the echo-window boundary.

    Feed contextless head reports through the real coordinator classifier.
    Keep timestamps deterministic without advancing HA's background timers.
    """
    from types import SimpleNamespace

    from homeassistant.core import State

    from custom_components.mxz_coordinator import coordinator as coordinator_module
    from custom_components.mxz_coordinator.target_input import TargetInput

    _entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.target_step = 0.5  # expose half-degree adoption instead of rounding it away
    coord.set_zone_input(
        zone, source="service", target=72, enable=True, direction="heat"
    )
    record = coord.remote_record(head.entity_id)
    record.heat = 71.5
    generation = zone.input_generation
    t0 = 1000.0
    # A Coordinator-issued Low fan write. No mode/target was commanded in it.
    coord._last_head_cmd[head.entity_id] = (t0, None, "low")
    journal = coord._target_input.setdefault(head.entity_id, TargetInput())
    journal.command("heat", 72.0, 71.5)  # native 22.0 C / HA display equivalent
    old = State(head.entity_id, "heat", {"temperature": 72.0, "fan_mode": "auto"})
    for elapsed, temperature in ((0.12, 72.0), (6.0, 71.5), (restore_at, 72.0)):
        new = State(
            head.entity_id, "heat", {"temperature": temperature, "fan_mode": "low"}
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                coordinator_module, "time",
                SimpleNamespace(monotonic=lambda elapsed=elapsed: t0 + elapsed),
            )
            coord._classify_head_input(head.entity_id, old, new)
        assert zone.direction == "heat", f"direction changed at +{elapsed}s"
        assert zone.target == 72, f"target changed at +{elapsed}s"
        assert record.heat == 71.5, f"remote record changed at +{elapsed}s"
        assert zone.input_generation == generation, f"input adopted at +{elapsed}s"
        old = new


async def test_own_commands_are_never_adopted(hass: HomeAssistant) -> None:
    entry, coord, heads = await _setup(hass)
    inputs = _inputs(hass)
    zone = coord.zones[0]
    gen0 = zone.input_generation
    await _set_temp(hass, SENSOR_A, 75)  # plan drives head A to cool
    await _recompute(hass, entry)
    assert hass.states.get(heads[0].entity_id).state == "cool"
    await _set_temp(hass, SENSOR_A, 70)  # satisfied -> parked
    await _recompute(hass, entry)
    assert zone.input_generation == gen0
    assert not [i for i in inputs if i["source"] == "head_manual"]


async def test_remote_down_press_on_parked_head_adopts_target(
    hass: HomeAssistant,
) -> None:
    entry, coord, heads = await _setup(hass)
    head = heads[0]
    zone = coord.zones[0]
    zone_target_before = zone.target
    inputs = _inputs(hass)
    _age_commands(coord)
    head.remote("cool", 69.0, "auto")  # first packet: recorded only
    await hass.async_block_till_done()
    assert zone.target == zone_target_before
    _age_commands(coord)
    head.remote("off")  # remote OFF while awake -> room off (authoritative)
    await hass.async_block_till_done()
    assert zone.enable is False and zone.input_kind == "off"
    _age_commands(coord)
    head.remote("cool", 68.0, "auto")  # ▼ then ON: OFF -> COOL 68
    await hass.async_block_till_done()
    assert zone.target == 68.0
    assert zone.enable is True
    assert zone.input_source == "head_manual" and zone.input_kind == "target_on"
    fac = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert fac.attributes["temperature"] == 68.0
    assert fac.attributes["last_input_source"] == "head_manual"
    assert fac.attributes["last_input_kind"] == "target_on"
    assert fac.attributes["remote_last_cool"] == 68.0
    assert inputs[-1]["source"] == "head_manual" and inputs[-1]["target"] == 68.0
    # The number entity (restore owner) agrees.
    assert float(hass.states.get(_eid(hass, entry, "_primary_target")).state) == 68.0


async def test_fan_only_press_with_stale_display_keeps_target(
    hass: HomeAssistant,
) -> None:
    _entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.remote_record(head.entity_id).cool = 68.0  # remote shows COOL 68
    target = zone.target
    _age_commands(coord)
    head.remote("cool", 68.0, "quiet")  # FAN press resends ON + COOL 68 + QUIET
    await hass.async_block_till_done()
    assert zone.target == target
    assert zone.input_source != "head_manual"


async def test_remote_on_wakes_disabled_room_without_stale_temperature(
    hass: HomeAssistant,
) -> None:
    entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_primary_enable")},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert zone.enable is False
    coord.remote_record(head.entity_id).cool = 68.0
    target = zone.target
    _age_commands(coord)
    head.remote("cool", 68.0, "auto")  # POWER press: OFF -> COOL 68 (unchanged temp)
    await hass.async_block_till_done()
    assert zone.enable is True and zone.target == target
    assert zone.input_kind == "on"


async def test_mode_is_never_adopted_plan_reparks_head(hass: HomeAssistant) -> None:
    entry, coord, heads = await _setup(hass)
    head = heads[0]
    coord.remote_record(head.entity_id).heat = 70.0
    _age_commands(coord)
    head.remote("heat", 70.0, "auto")  # MODE press -> HEAT 70 (room satisfied)
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert hass.states.get(head.entity_id).state in ("off", "fan_only")
    assert (
        coord.current_shared_mode != "heat"
        or coord.data["state"] == coord.current_shared_mode
    )


async def test_target_written_before_enable_no_stale_target_command(
    hass: HomeAssistant,
) -> None:
    """A room OFF holding a stale 87°F target, then CA sends heat_cool + 72:
    no head command may ever be computed from 87."""
    entry, coord, heads = await _setup(hass)
    zone = coord.zones[0]
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _eid(hass, entry, "_primary_enable")},
        blocking=True,
    )
    await hass.async_block_till_done()
    zone.target = 87.0  # stale value restored from long ago
    await _set_temp(hass, SENSOR_A, 75)
    calls = _calls(hass)
    targets_seen: list[float] = []
    orig = coord._compute

    def _spy():
        if zone.enable:
            targets_seen.append(zone.target)
        return orig()

    coord._compute = _spy
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {
            "entity_id": _eid(hass, entry, "_primary_thermostat"),
            "hvac_mode": "heat_cool",
            "temperature": 72,
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    await _recompute(hass, entry)
    assert targets_seen and all(t == 72.0 for t in targets_seen)
    heads_cmds = [
        c
        for c in calls
        if c["domain"] == "climate"
        and c["service_data"].get("entity_id") == heads[0].entity_id
    ]
    for c in heads_cmds:
        d = c["service_data"]
        assert 87.0 not in (
            d.get("temperature"),
            d.get("target_temp_low"),
            d.get("target_temp_high"),
        )
    assert zone.input_source == "service" and zone.input_kind == "target_on"


async def test_service_writes_publish_service_provenance(hass: HomeAssistant) -> None:
    entry, coord, _heads = await _setup(hass)
    zone = coord.zones[0]
    g = zone.input_generation
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _eid(hass, entry, "_primary_target"), "value": 71},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert (
        zone.input_generation == g + 1
        and zone.input_source == "service"
        and zone.input_kind == "target"
    )
    fac = hass.states.get(_eid(hass, entry, "_primary_thermostat"))
    assert fac.attributes["last_input_generation"] == g + 1


async def test_reconnect_is_not_a_person(hass: HomeAssistant) -> None:
    _entry, coord, heads = await _setup(hass)
    head, zone = heads[0], coord.zones[0]
    coord.remote_record(head.entity_id).cool = 75.0
    attrs = dict(hass.states.get(head.entity_id).attributes)
    hass.states.async_set(head.entity_id, "unavailable", attrs)
    await hass.async_block_till_done()
    _age_commands(coord)
    head.remote("cool", 66.0, "auto")
    await hass.async_block_till_done()
    assert zone.input_source != "head_manual"


async def test_remote_record_persists_across_reload(hass: HomeAssistant) -> None:
    entry, coord, heads = await _setup(hass)
    head = heads[0]
    _age_commands(coord)
    head.remote("cool", 69.0, "auto")
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.remote_record(head.entity_id).cool == 69.0
