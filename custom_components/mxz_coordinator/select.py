"""Shared-mode select (replaces input_select.hvac_shared_mode) and, in the local
fork, one per-room direction select (heat_cool | heat | cool).

Restores the last value across restarts (no default) so the shared mode "rests" at
whatever was last called; cold start falls back to cool. The coordinator updates this
when it flips the mode, and the user can override it here.
"""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DIRECTION_AUTO,
    DIRECTIONS,
    KEY_SHARED_MODE,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)
from .coordinator import INPUT_SOURCE_SERVICE, MXZCoordinator, Zone
from .entity import MXZEntity

_OPTIONS = [MODE_COOL, MODE_HEAT, MODE_FAN_ONLY, MODE_OFF]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the shared-mode select."""
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(
        [
            MXZSharedModeSelect(coordinator),
            *(MXZZoneDirectionSelect(coordinator, zone) for zone in coordinator.zones),
        ]
    )


class MXZSharedModeSelect(MXZEntity, SelectEntity, RestoreEntity):
    """The current shared mode, persisted across restarts."""

    _attr_options = _OPTIONS
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, coordinator: MXZCoordinator) -> None:
        super().__init__(coordinator, KEY_SHARED_MODE)
        self._attr_current_option = MODE_COOL

    async def async_added_to_hass(self) -> None:
        """Restore last mode (resting mode) and seed the coordinator."""
        await super().async_added_to_hass()
        if (
            (last := await self.async_get_last_state()) is not None
            and last.state in _OPTIONS
            and not self._restored_state_is_stale(last)
        ):
            self._attr_current_option = last.state
        self.coordinator.current_shared_mode = self._attr_current_option
        self.async_on_remove(self.coordinator.async_add_listener(self._sync))

    @callback
    def _sync(self) -> None:
        """Reflect a coordinator-driven mode flip."""
        if self._attr_current_option != self.coordinator.current_shared_mode:
            self._attr_current_option = self.coordinator.current_shared_mode
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Manual override."""
        self._attr_current_option = option
        self.async_write_ha_state()
        await self.coordinator.async_select_shared_mode(option)


class MXZZoneDirectionSelect(MXZEntity, SelectEntity, RestoreEntity):
    """One room's direction (local fork): heat_cool | heat | cool.

    Seeds ``Zone.direction`` on restore (default heat_cool -- the original
    two-sided behaviour, so an upgraded install changes nothing until
    something chooses a direction). Writes go through the coordinator's
    ``set_zone_input`` so provenance and the engage-latch reset stay in one
    place, exactly like the zone's target number and enable switch.
    """

    _attr_options: ClassVar[list[str]] = list(DIRECTIONS)
    _attr_icon = "mdi:thermostat-auto"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_direction")
        self._zone = zone
        self._attr_translation_key = "zone_direction"
        self._attr_translation_placeholders = {"zone": zone.name}
        self._attr_current_option = DIRECTION_AUTO

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (
            (last := await self.async_get_last_state()) is not None
            and last.state in DIRECTIONS
            and not self._restored_state_is_stale(last)
        ):
            self._attr_current_option = last.state
        self._zone.direction = self._attr_current_option
        self.coordinator.direction_entities[self._zone.slug] = self

    async def async_will_remove_from_hass(self) -> None:
        if self.coordinator.direction_entities.get(self._zone.slug) is self:
            self.coordinator.direction_entities.pop(self._zone.slug, None)
        await super().async_will_remove_from_hass()

    def apply_direction(self, value: str) -> None:
        """Coordinator-driven write (provenance recorded by the caller)."""
        self._attr_current_option = value
        self._zone.direction = value
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if not self.coordinator.set_zone_input(
            self._zone, source=INPUT_SOURCE_SERVICE, direction=option
        ):
            self.apply_direction(option)
        await self.coordinator.async_user_changed()
