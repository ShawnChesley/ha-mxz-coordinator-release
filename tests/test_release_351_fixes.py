"""3.5.1 release-review fixes.

1. Adaptive fan thresholds are Fahrenheit deltas: a Celsius installation's
   room error must be converted before the policy decides (1 C short == 1.8 F).
2. A failed head write must not roll back a NEWER write's target expectation.
3. Tight-cool offset coverage: Celsius step, eco/emergency/heat exclusion,
   and a tight-cool target near the head floor.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.mxz_coordinator.const import MODE_COOL, MODE_HEAT
from custom_components.mxz_coordinator.fan_control import FanController
from custom_components.mxz_coordinator.fan_policy import FanPolicy

POLICY = FanPolicy.ADAPTIVE
from custom_components.mxz_coordinator.logic import head_setpoint_target
from custom_components.mxz_coordinator.target_input import TargetInput

from .test_cool_floor_single_setpoint import FloorRangeHead
from .test_single_setpoint import _enable, _set_temp, _setup
from .test_target_input_runtime import _heat

# ── 1. Fan error units ──────────────────────────────────────────────────


class _Stop(Exception):
    pass


async def _decided_stage(hass: HomeAssistant, *, celsius: bool, target: float, temp: float, mode: str):
    """Run FanController.apply up to the policy decision; return (error_f, stage)."""
    cid = "climate.fan_units_head"
    hass.states.async_set(cid, mode, {"fan_modes": ["auto", "low", "medium", "high"]})
    fc = FanController(hass, SimpleNamespace(celsius=celsius, zones=[]), "units")
    head = fc.head(cid)
    head.seeded = True
    head.policy = POLICY.value
    head.room.set_policy(POLICY)
    fc.protection = lambda zone, act: None
    seen: dict[str, Any] = {}
    real = head.room.decide

    def spy(**kw):
        seen["error_f"] = kw["error_f"]
        seen["decision"] = real(**kw)
        raise _Stop

    head.room.decide = spy
    zone = SimpleNamespace(climate_id=cid, slug="primary")
    plan = {"primary_temp": temp, "primary_temp_ok": True}
    with pytest.raises(_Stop):
        await fc.apply(zone, mode, plan, eco=False, handback=False, target=target)
    d = seen["decision"]
    return seen["error_f"], (d.stage.value if d.stage else None), d.selected


@pytest.mark.parametrize("mode", [MODE_HEAT, MODE_COOL])
async def test_celsius_error_is_converted_to_fahrenheit(hass: HomeAssistant, mode: str) -> None:
    short_c = 1.0
    temp_c = 21.0 - short_c if mode == MODE_HEAT else 21.0 + short_c
    err_c, stage_c, sel_c = await _decided_stage(hass, celsius=True, target=21.0, temp=temp_c, mode=mode)
    short_f = 1.8
    temp_f = 70.0 - short_f if mode == MODE_HEAT else 70.0 + short_f
    err_f, stage_f, sel_f = await _decided_stage(hass, celsius=False, target=70.0, temp=temp_f, mode=mode)
    assert err_c == pytest.approx(1.8)
    assert err_f == pytest.approx(1.8)
    assert (stage_c, sel_c) == (stage_f, sel_f)


async def test_fahrenheit_error_is_unchanged(hass: HomeAssistant) -> None:
    err, _stage, _sel = await _decided_stage(hass, celsius=False, target=70.0, temp=69.0, mode=MODE_HEAT)
    assert err == pytest.approx(1.0)


async def test_unconverted_celsius_error_would_pick_a_lower_stage(hass: HomeAssistant) -> None:
    """Guard: 1.0 vs 1.8 really are different stages, so the test above can fail."""
    _e1, s1, _ = await _decided_stage(hass, celsius=False, target=70.0, temp=69.0, mode=MODE_HEAT)
    _e2, s2, _ = await _decided_stage(hass, celsius=False, target=70.0, temp=68.2, mode=MODE_HEAT)
    assert s1 != s2


# ── 2. Rollback must not erase a newer write ────────────────────────────


def _base72() -> TargetInput:
    ti = TargetInput()
    ti.settle(ti.command("heat", 72.0))  # an earlier, successful write
    return ti


def test_rollback_skips_when_newer_write_exists() -> None:
    ti = _base72()
    a = ti.command("heat", 75.0)
    b = ti.command("heat", 73.0)  # newer write while A in flight
    assert ti.rollback("heat", a) is False
    ti.settle(b)  # B succeeds
    assert ti.matches("heat", 73.0)


def test_both_overlapping_writes_fail_a_first() -> None:
    """72 expected; A stages 75; B stages 73; A fails; B fails -> back to 72, not 75."""
    ti = _base72()
    a = ti.command("heat", 75.0)
    b = ti.command("heat", 73.0)
    assert ti.rollback("heat", a) is False
    assert ti.rollback("heat", b) is True
    assert ti.matches("heat", 72.0)
    assert not ti.matches("heat", 75.0)


def test_both_overlapping_writes_fail_b_first() -> None:
    ti = _base72()
    a = ti.command("heat", 75.0)
    b = ti.command("heat", 73.0)
    assert ti.rollback("heat", b) is True  # B's predecessor A is still in flight
    assert ti.matches("heat", 75.0)
    assert ti.rollback("heat", a) is True
    assert ti.matches("heat", 72.0)


def test_older_succeeds_newer_fails_restores_older() -> None:
    ti = _base72()
    a = ti.command("heat", 75.0)
    b = ti.command("heat", 73.0)
    ti.settle(a)
    assert ti.rollback("heat", b) is True
    assert ti.matches("heat", 75.0)


def test_three_writes_middle_and_last_fail() -> None:
    ti = _base72()
    a = ti.command("heat", 74.0)
    b = ti.command("heat", 75.0)
    c = ti.command("heat", 76.0)
    ti.settle(a)
    ti.rollback("heat", b)
    assert ti.rollback("heat", c) is True
    assert ti.matches("heat", 74.0)


def test_first_ever_write_fails_leaves_no_expectation() -> None:
    ti = TargetInput()
    a = ti.command("heat", 75.0)
    assert ti.rollback("heat", a) is True
    assert "heat" not in ti.commanded


def test_history_stays_small_when_nothing_is_in_flight() -> None:
    ti = TargetInput()
    for i in range(500):
        ti.settle(ti.command("heat", 60.0 + (i % 10)))
        ti.expect("cool", 75.0 + (i % 3))
    assert len(ti._stamps) <= 1
    assert not ti._pending and not ti._failed


def test_delayed_failure_keeps_its_ancestry() -> None:
    """A write still in flight is never forgotten, however many writes follow it."""
    ti = _base72()
    a = ti.command("heat", 75.0)
    for i in range(500):
        ti.settle(ti.command("cool", 76.0 + (i % 5)))
        ti.expect("cool", 76.0)
    assert a in ti._stamps
    assert ti.rollback("heat", a) is True
    assert ti.matches("heat", 72.0)


def test_delayed_double_failure_keeps_failed_link() -> None:
    """A fails early, B fails much later: B still walks past A to 72."""
    ti = _base72()
    a = ti.command("heat", 75.0)
    b = ti.command("heat", 73.0)
    assert ti.rollback("heat", a) is False
    for i in range(500):
        ti.settle(ti.command("cool", 76.0 + (i % 5)))
    assert a in ti._stamps  # failed link kept while B is in flight
    assert ti.rollback("heat", b) is True
    assert ti.matches("heat", 72.0)
    assert not ti._stamps and not ti._failed and not ti._pending


async def test_failed_older_write_keeps_newer_expectation(hass: HomeAssistant, monkeypatch) -> None:
    """A starts -> B succeeds -> A fails: B's expectation survives, so B's echo is ours."""
    _entry, coord, head, _zone = await _heat(hass)
    release_a = asyncio.Event()
    real_call = type(hass.services).async_call

    async def call(self, domain, service, data, *args, **kwargs):
        if data.get("temperature") == 75:
            await release_a.wait()
            raise HomeAssistantError("Simulated rejected write A")
        return await real_call(self, domain, service, data, *args, **kwargs)

    monkeypatch.setattr(type(hass.services), "async_call", call)
    task_a = hass.async_create_task(
        coord.async_head_call(
            "set_temperature", {"entity_id": head.entity_id, "hvac_mode": "heat", "temperature": 75}
        )
    )
    await asyncio.sleep(0)
    await coord.async_head_call(
        "set_temperature", {"entity_id": head.entity_id, "hvac_mode": "heat", "temperature": 73}
    )
    assert coord._target_input[head.entity_id].matches("heat", 73)
    release_a.set()
    with pytest.raises(HomeAssistantError):
        await task_a
    ti = coord._target_input[head.entity_id]
    assert ti.matches("heat", 73)  # B kept
    assert not ti.matches("heat", 75)
    assert ti.candidate is None
    # B's echo is recognised as ours, not as a remote press.
    assert ti.observe("heat", 73, 0.0, 0) == "echo"


async def test_both_overlapping_head_writes_fail(hass: HomeAssistant, monkeypatch) -> None:
    """Real head-call path: A (75) and B (73) both rejected -> expectation back to 72."""
    _entry, coord, head, _zone = await _heat(hass)
    assert coord._target_input[head.entity_id].matches("heat", 72)
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    async def call(self, domain, service, data, *args, **kwargs):
        if data.get("temperature") == 75:
            await release_a.wait()
            raise HomeAssistantError("Simulated rejected write A")
        if data.get("temperature") == 73:
            await release_b.wait()
            raise HomeAssistantError("Simulated rejected write B")
        raise AssertionError("unexpected call")

    monkeypatch.setattr(type(hass.services), "async_call", call)
    entity = head.entity_id
    task_a = hass.async_create_task(
        coord.async_head_call("set_temperature", {"entity_id": entity, "hvac_mode": "heat", "temperature": 75})
    )
    await asyncio.sleep(0)
    task_b = hass.async_create_task(
        coord.async_head_call("set_temperature", {"entity_id": entity, "hvac_mode": "heat", "temperature": 73})
    )
    await asyncio.sleep(0)
    release_a.set()
    with pytest.raises(HomeAssistantError):
        await task_a
    release_b.set()
    with pytest.raises(HomeAssistantError):
        await task_b
    ti = coord._target_input[entity]
    assert ti.matches("heat", 72)
    assert not ti.matches("heat", 75)
    assert not ti.matches("heat", 73)


async def test_failed_only_write_restores_previous_expectation(hass: HomeAssistant, monkeypatch) -> None:
    _entry, coord, head, _zone = await _heat(hass)
    previous = dict(coord._target_input[head.entity_id].commanded)

    async def fail(*args, **kwargs):
        raise HomeAssistantError("rejected")

    monkeypatch.setattr(type(hass.services), "async_call", fail)
    with pytest.raises(HomeAssistantError):
        await coord.async_head_call(
            "set_temperature", {"entity_id": head.entity_id, "hvac_mode": "heat", "temperature": 75}
        )
    assert coord._target_input[head.entity_id].commanded == previous


# ── 3. Tight-cool offset coverage ───────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "tight", "eco", "emergency", "celsius", "expected"),
    [
        (MODE_COOL, True, False, False, False, 62.0),  # F: one step = 1 F
        (MODE_COOL, True, False, False, True, 62.5),  # C: one step = 0.5 C
        (MODE_COOL, False, False, False, False, 63.0),  # switch off
        (MODE_COOL, True, True, False, False, 63.0),  # eco protection edges untouched
        (MODE_COOL, True, False, True, False, 63.0),  # emergency untouched
        (MODE_HEAT, True, False, False, False, 63.0),  # heat untouched
        (MODE_HEAT, True, False, True, True, 63.0),
    ],
)
def test_head_setpoint_target_rules(mode, tight, eco, emergency, celsius, expected) -> None:
    assert (
        head_setpoint_target(
            target=63.0, mode=mode, tight_demand=tight, eco=eco, emergency=emergency, celsius=celsius
        )
        == expected
    )


async def _tight_cool_to(hass: HomeAssistant, target: float, *, eco: bool = False, tight: bool = True):
    heads = [FloorRangeHead("a"), FloorRangeHead("b")]
    entry = await _setup(hass, heads)
    await _enable(hass, entry)
    coord = entry.runtime_data
    coord.zones[0].tight_demand = tight
    if eco:
        coord._eco_active = lambda: True
    coord.zones[0].target = target
    coord.reset_engage_latch(coord.zones[0].slug)
    await _set_temp(hass, "sensor.room_a_temp", target + 4)
    await coord.async_refresh()
    await hass.async_block_till_done()
    return heads, coord


async def test_tight_cool_near_head_floor_sends_single_setpoint_one_below(hass: HomeAssistant) -> None:
    """Target 63 on a 61 F-floor head: tight aims 62, sent as a single setpoint."""
    heads, coord = await _tight_cool_to(hass, 63.0)
    a = hass.states.get(heads[0].entity_id)
    assert a.state == "cool"
    assert a.attributes["target_temp_high"] == 62
    last = heads[0].calls[-1]
    assert last.get("temperature") == 62
    assert "target_temp_low" not in last
    assert coord.zones[0].target == 63.0  # room target unchanged


async def test_tight_cool_at_head_floor_not_below_it(hass: HomeAssistant) -> None:
    """Target 61 (the head floor): the biased 60 is limited by the head, never sent below 61."""
    heads, _coord = await _tight_cool_to(hass, 61.0)
    sent = [
        c.get("temperature") if c.get("temperature") is not None else c.get("target_temp_high")
        for c in heads[0].calls
    ]
    sent = [v for v in sent if v is not None]
    assert sent, "no cool setpoint was written"
    assert min(sent) >= 61
    assert hass.states.get(heads[0].entity_id).attributes["target_temp_high"] >= 61


async def test_tight_cool_under_eco_matches_non_tight(hass: HomeAssistant) -> None:
    """Under eco the tight switch changes nothing: toggling it off causes no new write."""
    heads, coord = await _tight_cool_to(hass, 70.0, eco=True, tight=True)
    assert all(c.get("target_temp_high") != 69 and c.get("temperature") != 69 for c in heads[0].calls)
    n = len(heads[0].calls)
    before = dict(hass.states.get(heads[0].entity_id).attributes)
    coord.zones[0].tight_demand = False
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert len(heads[0].calls) == n
    after = hass.states.get(heads[0].entity_id).attributes
    assert (after.get("target_temp_low"), after.get("target_temp_high")) == (
        before.get("target_temp_low"),
        before.get("target_temp_high"),
    )


async def test_tight_cool_without_eco_differs_from_non_tight(hass: HomeAssistant) -> None:
    """Control for the eco test: without eco, turning tight off DOES move the head 69 -> 70."""
    heads, coord = await _tight_cool_to(hass, 70.0, eco=False, tight=True)
    assert hass.states.get(heads[0].entity_id).attributes["target_temp_high"] == 69
    coord.zones[0].tight_demand = False
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(heads[0].entity_id).attributes["target_temp_high"] == 70
