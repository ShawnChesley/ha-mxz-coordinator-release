"""Setup / coordinator behavior tests (requires pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mxz_coordinator.const import (
    CONF_PRIMARY_CLIMATE,
    CONF_PRIMARY_SENSOR,
    CONF_SECONDARY_CLIMATE,
    CONF_SECONDARY_SENSOR,
    DOMAIN,
)

_DATA = {
    CONF_PRIMARY_CLIMATE: "climate.primary",
    CONF_SECONDARY_CLIMATE: "climate.secondary",
    CONF_PRIMARY_SENSOR: "sensor.primary_temp",
    CONF_SECONDARY_SENSOR: "sensor.secondary_temp",
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set("sensor.primary_temp", "72")
    hass.states.async_set("sensor.secondary_temp", "70")
    attrs = {"target_temp_low": 0, "target_temp_high": 0}
    hass.states.async_set("climate.primary", "off", attrs)
    hass.states.async_set("climate.secondary", "off", attrs)
    entry = MockConfigEntry(domain=DOMAIN, data=_DATA, title="MXZ Coordinator")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_created_and_plan_computes(hass: HomeAssistant) -> None:
    await _setup(hass)

    assert hass.states.async_entity_ids_count("switch") >= 4
    assert hass.states.async_entity_ids_count("number") >= 2
    assert hass.states.async_entity_ids_count("select") >= 1

    plan = next(
        s
        for s in hass.states.async_all("sensor")
        if s.attributes.get("standoff") is not None
    )
    assert plan.state in ("cool", "heat")
    assert "primary_engage" in plan.attributes


async def test_kill_switch_blocks_writes(hass: HomeAssistant) -> None:
    """With the coordinator disabled (default), the heads are never commanded."""
    calls: list = []

    async def _record(call) -> None:
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", _record)
    hass.services.async_register("climate", "set_hvac_mode", _record)

    await _setup(hass)
    # coordinator_enable defaults OFF -> apply is a no-op
    assert calls == []


async def test_reload_preserves_direction_and_window_switch_registry(hass: HomeAssistant) -> None:
    """New persistent helpers must not be pruned as stale on the next setup."""
    entry = await _setup(hass)
    registry = er.async_get(hass)
    tracked = {}
    for suffix, domain in (("direction", "select"), ("window_freeze_protect", "switch")):
        unique_id = f"{entry.entry_id}_primary_{suffix}"
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        assert entity_id is not None
        renamed = f"{domain}.retained_{suffix}"
        registry.async_update_entity(entity_id, new_entity_id=renamed, name="Keep my name")
        tracked[(domain, unique_id)] = (renamed, registry.async_get(renamed).id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    for (domain, unique_id), (entity_id, registry_id) in tracked.items():
        assert registry.async_get_entity_id(domain, DOMAIN, unique_id) == entity_id
        restored = registry.async_get(entity_id)
        assert restored.id == registry_id
        assert restored.name == "Keep my name"


async def test_device_carries_docs_link(hass: HomeAssistant) -> None:
    """The service device links to the docs (incl. Removing) via its Visit button."""
    entry = await _setup(hass)
    # Look the device up through its config entry: the identifier-set lookup
    # (async_get_device) is deprecated and raises in observed HA 2026.9.0.
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert (DOMAIN, entry.entry_id) in device.identifiers
    assert (
        device.configuration_url
        == "https://github.com/ShawnChesley/ha-mxz-coordinator-release#removing"
    )
