"""On/off helpers: per-zone enables, eco-idle, lockouts, and the kill-switch.

Replaces the input_boolean.* helpers from the YAML package. All default OFF on a
fresh install (matching the package, where `initial` was omitted) and restore their
last state across restarts. The per-zone Fan auto switches render live from the
coordinator's latch, but restore one bool of their own — held or not — which is how
the seed tells boost residue from a deliberate hold after a restart.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    KEY_COOL_LOCKOUT,
    KEY_COORDINATOR_ENABLE,
    KEY_ECO_IDLE,
    KEY_HEAT_LOCKOUT,
)
from .coordinator import INPUT_SOURCE_SERVICE, MXZCoordinator, Zone
from .entity import MXZEntity

_GLOBAL_ICONS = {
    KEY_COORDINATOR_ENABLE: "mdi:hvac",
    KEY_ECO_IDLE: "mdi:leaf",
    KEY_HEAT_LOCKOUT: "mdi:fire-off",
    KEY_COOL_LOCKOUT: "mdi:snowflake-off",
}
_ZONE_ICONS = ("mdi:bed", "mdi:sofa")  # legacy zone-0/1 icons; generic beyond


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-zone enables and the coordinator-level switches."""
    coordinator: MXZCoordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        MXZZoneEnableSwitch(coordinator, zone) for zone in coordinator.zones
    ]
    entities.extend(MXZZoneFanAutoSwitch(coordinator, zone) for zone in coordinator.zones)
    for zone in coordinator.zones:
        entities.append(
            MXZZoneFlagSwitch(
                coordinator, zone, "cool_exempt", "mdi:snowflake-check"
            )
        )
        entities.append(
            MXZZoneFlagSwitch(
                coordinator, zone, "emergency_protect", "mdi:snowflake-thermometer"
            )
        )
        # Local fork: lets the emergency floor heat THROUGH an active window
        # pause. Default OFF -- an open window wins unless you opt in here.
        entities.append(
            MXZZoneFlagSwitch(
                coordinator, zone, "window_freeze_protect", "mdi:window-open-variant"
            )
        )
    entities.extend(MXZSwitch(coordinator, key) for key in _GLOBAL_ICONS)
    async_add_entities(entities)


class MXZBaseSwitch(MXZEntity, SwitchEntity, RestoreEntity):
    """A restorable on/off helper that seeds coordinator state."""

    def __init__(self, coordinator: MXZCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Restore last state and seed the coordinator.

        Restores from a PREVIOUS entry incarnation are ignored (#7) — the
        dangerous case is a deleted entry's kill-switch ON resurrecting onto a
        freshly re-added entry.
        """
        await super().async_added_to_hass()
        if (
            last := await self.async_get_last_state()
        ) is not None and not self._restored_state_is_stale(last):
            self._attr_is_on = last.state == "on"
        self._seed()

    def _seed(self) -> None:
        raise NotImplementedError

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        self._attr_is_on = value
        self._seed()
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()


class MXZSwitch(MXZBaseSwitch):
    """A coordinator-level flag (kill-switch, eco-idle, lockouts)."""

    def __init__(self, coordinator: MXZCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._key = key
        self._attr_icon = _GLOBAL_ICONS[key]

    def _seed(self) -> None:
        setattr(self.coordinator, self._key, self._attr_is_on)


class MXZZoneEnableSwitch(MXZBaseSwitch):
    """One zone's enable flag (seeds Zone.enable)."""

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_enable")
        self._zone = zone
        self._attr_icon = (
            _ZONE_ICONS[zone.index]
            if zone.index < len(_ZONE_ICONS)
            else "mdi:home-thermometer-outline"
        )
        self._attr_translation_key = "zone_enable"
        self._attr_translation_placeholders = {"zone": zone.name}

    def _seed(self) -> None:
        self._zone.enable = self._attr_is_on

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.enable_entities[self._zone.slug] = self

    async def async_will_remove_from_hass(self) -> None:
        if self.coordinator.enable_entities.get(self._zone.slug) is self:
            self.coordinator.enable_entities.pop(self._zone.slug, None)
        await super().async_will_remove_from_hass()

    def apply_enable(self, value: bool) -> None:
        """Coordinator-driven write (provenance recorded by the caller)."""
        self._attr_is_on = value
        self._seed()
        self.async_write_ha_state()

    async def _set(self, value: bool) -> None:
        if not self.coordinator.set_zone_input(
            self._zone, source=INPUT_SOURCE_SERVICE, enable=value
        ):
            self.apply_enable(value)
        await self.coordinator.async_user_changed()


class MXZZoneFlagSwitch(MXZBaseSwitch):
    """A per-zone policy flag (v3.4.0), restored and seeded onto the Zone.

    ``cool_exempt``: the room may still cool while the global cool lockout is
    on. ``emergency_protect``: force heat when the room drops below its
    emergency floor. Both default OFF; turning one off triggers a recompute,
    which (for protection) clears an active emergency immediately. CONFIG
    category: on the device page and writable by automations, out of
    auto-populated dashboards and voice.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: MXZCoordinator, zone: Zone, attr: str, icon: str
    ) -> None:
        super().__init__(coordinator, f"{zone.slug}_{attr}")
        self._zone = zone
        self._zone_attr = attr
        self._attr_icon = icon
        self._attr_translation_key = f"zone_{attr}"
        self._attr_translation_placeholders = {"zone": zone.name}

    def _seed(self) -> None:
        setattr(self._zone, self._zone_attr, self._attr_is_on)


class MXZZoneFanAutoSwitch(
    MXZEntity, CoordinatorEntity[MXZCoordinator], SwitchEntity, RestoreEntity
):
    """Per-zone "Fan auto" toggle — a live mirror of the manual-fan latch.

    ON  = boost/auto drives this head's fan (zone not held).
    OFF = a manual speed is being held.

    The switch renders live from the latch (CoordinatorEntity re-renders every
    cycle) — but it also RESTORES its last state across restarts, and hands
    that one bool to the coordinator before the first compute. That bool is
    what lets the seed tell boost residue from a deliberate hold: without it,
    a head still carrying boost's last fan token at restart is
    indistinguishable from a hold the user placed while the head idled (the
    token value cannot separate them — four shipped bug shapes proved it).
    Reconciliation always takes the TOKEN from the observed head, so a speed
    changed via wall remote during the outage stays the user's. Turning the
    switch ON hands control back to boost; OFF pins the head's current speed.
    Apple's Home app renders only a climate service's fixed characteristics —
    there's no room for a custom control inside the climate tile — so this
    rides alongside it as a plain toggle and doubles as a visible
    who's-driving-the-fan indicator.
    """

    _attr_icon = "mdi:fan-auto"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        MXZEntity.__init__(self, coordinator, f"{zone.slug}_fan_auto")
        CoordinatorEntity.__init__(self, coordinator)
        self._zone = zone
        self._attr_translation_key = "zone_fan_auto"
        self._attr_translation_placeholders = {"zone": zone.name}

    async def async_added_to_hass(self) -> None:
        """Hand the restored pre-restart hold truth to the coordinator.

        Runs during platform setup, before the coordinator's first compute
        (async_setup_entry awaits the platforms; STARTUP_RECOVER_DELAY adds
        margin on HA start). A stale restore — older than the config entry —
        belongs to a previous incarnation (#7) and is ignored, as is anything
        but a clean on/off (a first restart after upgrading has no stored
        state at all: one clean fallback to observed-state seeding).
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if (
            last is not None
            and not self._restored_state_is_stale(last)
            and last.state in ("on", "off")
        ):
            self.coordinator.restore_fan_hold(
                self._zone.climate_id, held=(last.state == "off")
            )

    @property
    def is_on(self) -> bool:
        """Mirror the latch: ON when boost drives, OFF when a manual hold is active."""
        return self.coordinator.fan_auto_is_on(self._zone.climate_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Release the zone's latch and let boost reassert."""
        await self.coordinator.async_set_fan_auto(self._zone.climate_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Latch the zone at the head's current fan speed (no-op if it's at auto)."""
        await self.coordinator.async_set_fan_auto(self._zone.climate_id, False)
        self.async_write_ha_state()
