"""Per-room fan Release button (local fork).

Ends a manual fan hold and returns the room to its configured fan policy. On a
room still using the legacy policy it is the same as turning Fan auto ON.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MXZCoordinator, Zone
from .entity import MXZEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MXZCoordinator = entry.runtime_data
    async_add_entities(MXZZoneFanReleaseButton(coordinator, z) for z in coordinator.zones)


class MXZZoneFanReleaseButton(MXZEntity, ButtonEntity):
    """Release the room's manual fan hold."""

    _attr_icon = "mdi:fan-auto"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_fan_release")
        self._zone = zone
        self._attr_translation_key = "zone_fan_release"
        self._attr_translation_placeholders = {"zone": zone.name}

    async def async_press(self) -> None:
        await self.coordinator.fan.async_release(self._zone.climate_id)
