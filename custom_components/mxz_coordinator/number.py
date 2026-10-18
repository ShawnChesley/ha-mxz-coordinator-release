"""Per-zone setpoint target numbers (replaces input_number.hvac_*_target)."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import INPUT_SOURCE_SERVICE, MXZCoordinator, Zone
from .entity import MXZEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one target number and one drift number per zone."""
    coordinator: MXZCoordinator = entry.runtime_data
    entities: list[RestoreNumber] = [
        MXZTargetNumber(coordinator, zone) for zone in coordinator.zones
    ]
    entities.extend(
        MXZDriftNumber(coordinator, zone) for zone in coordinator.zones
    )
    entities.extend(
        MXZEmergencyFloorNumber(coordinator, zone) for zone in coordinator.zones
    )
    async_add_entities(entities)


class MXZTargetNumber(MXZEntity, RestoreNumber):
    """A restorable comfort-target setpoint, bounded to the firmware band."""

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_target")
        self._zone = zone
        # Every zone uses the generic translated name with its own name
        # substituted in ("Bedroom target").
        self._attr_translation_key = "zone_target"
        self._attr_translation_placeholders = {"zone": zone.name}
        # Track the HA system temperature unit + resolution (°F: whole degrees;
        # °C: 0.5° steps). Match the climate tile: bound the target to the
        # firmware operating band [clamp_min, clamp_max].
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        self._attr_native_step = coordinator.target_step
        lo, hi = coordinator.head_target_bounds(zone.climate_id)
        self._attr_native_value = min(max(coordinator.target_default, lo), hi)

    # [clamp_min, clamp_max] narrowed to what THIS head will actually accept
    # (its native operating band), so a rejectable target can't be entered from
    # the UI / HomeKit / voice in the first place (#10). LIVE properties, not
    # frozen at init: a head whose integration loads after ours would otherwise
    # keep the wide fallback bounds until a reload. Validation reads these at
    # set-time, so it is always against the head's real band; the UI slider
    # refreshes its cached bounds on the entity's next state write.
    @property
    def native_min_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[0]

    @property
    def native_max_value(self) -> float:
        return self.coordinator.head_target_bounds(self._zone.climate_id)[1]

    async def async_added_to_hass(self) -> None:
        """Restore the last setpoint and seed the coordinator's zone.

        On a FRESH install (nothing to restore) the target seeds from the
        head's current setpoint instead of a hard default, so enabling the
        coordinator never plans against a temperature nobody chose (#6 —
        a 70 °F default vs a 66 °F room planned heat in July).
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if (
            not self._restored_state_is_stale(last_state)
            and (last := await self.async_get_last_number_data())
            and last.native_value is not None
        ):
            # Clamp the restored value into the current bounds: a pre-narrowing
            # install can hand back a target the head would reject (a restored
            # 79 °F on a 26.0 °C-native head, #10), and it would otherwise sit
            # in zone.target steering the plan until the user touches the
            # slider. Best-effort — if the head's integration hasn't loaded
            # yet the bounds are still the wide clamp band, and the apply-time
            # head-band clamp in _apply_head backstops whatever gets through.
            self._attr_native_value = min(
                max(last.native_value, self.native_min_value), self.native_max_value
            )
        elif (seed := self._head_setpoint()) is not None:
            self._attr_native_value = seed
        self._zone.target = self._attr_native_value
        self.coordinator.target_entities[self._zone.slug] = self

    async def async_will_remove_from_hass(self) -> None:
        if self.coordinator.target_entities.get(self._zone.slug) is self:
            self.coordinator.target_entities.pop(self._zone.slug, None)
        await super().async_will_remove_from_hass()

    def apply_target(self, value: float) -> None:
        """Coordinator-driven write (provenance recorded by the caller)."""
        self._attr_native_value = value
        self._zone.target = value
        self.async_write_ha_state()

    def _head_setpoint(self) -> float | None:
        """The head's current setpoint, clamped and snapped to our resolution."""
        state = self.hass.states.get(self._zone.climate_id)
        if state is None:
            return None
        attrs = state.attributes
        raw = attrs.get("temperature")
        if raw is None and attrs.get("target_temp_low") is not None:
            try:
                raw = (
                    float(attrs["target_temp_low"])
                    + float(attrs.get("target_temp_high", attrs["target_temp_low"]))
                ) / 2
            except (TypeError, ValueError):
                raw = None
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        step = self.coordinator.target_step or 1.0
        value = round(value / step) * step
        return min(max(value, self.native_min_value), self.native_max_value)

    async def async_set_native_value(self, value: float) -> None:
        """User changed the target -> persist, reset the latch, recompute."""
        changed = self.coordinator.set_zone_input(
            self._zone, source=INPUT_SOURCE_SERVICE, target=value
        )
        if not changed:
            self.coordinator.reset_engage_latch(self._zone.slug)
            self.apply_target(value)
        await self.coordinator.async_user_changed()


class MXZDriftNumber(MXZEntity, RestoreNumber):
    """A room's re-engage drift band — per-room, automatable (#18).

    How far this room may wander past its target before conditioning resumes.
    Defaults to the global drift; write it from automations to widen an empty
    room's tolerance (presence tiers) and tighten it on arrival — tightening
    re-engages on the next compute. The zone's demand vote respects this band
    too, so a wide-tolerance room never steers the shared compressor inside
    its own comfort window. CONFIG category: on the device page and writable
    by automations, out of auto-populated dashboards and voice.
    """

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_drift")
        self._zone = zone
        self._attr_translation_key = "zone_drift"
        self._attr_translation_placeholders = {"zone": zone.name}
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        lo, hi = coordinator._profile["engage_bounds"]
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        # Same grain as the global drift's options selector, so any value the
        # global can hold can be copied into a room verbatim.
        self._attr_native_step = lo / 2

    # LIVE, not a frozen _attr: an untouched room (zone.drift is None) must
    # DISPLAY the global drift it actually follows — including a new global
    # after an options change — not the value seeded at construction.
    @property
    def native_value(self) -> float:
        return self.coordinator.zone_drift(self._zone)

    # Persisted alongside the state so restore can tell a room the user set
    # apart from one merely displaying the global (RestoreNumber saves the
    # displayed value either way).
    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        return {"override": self._zone.drift is not None}

    async def async_added_to_hass(self) -> None:
        """Restore the last per-room drift; absent/stale/not set -> the global.

        Only a state marked ``override`` restores: an untouched room's saved
        state is just yesterday's global, and writing it back would freeze the
        room on a copy. A restored value is clamped into the profile bounds
        (hand-edited or pre-upgrade values bypass the UI). zone.drift stays
        None otherwise, so the room follows the GLOBAL drift live.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if (
            not self._restored_state_is_stale(last_state)
            and last_state is not None
            and last_state.attributes.get("override")
            and (last := await self.async_get_last_number_data())
            and last.native_value is not None
        ):
            self._zone.drift = min(
                max(last.native_value, self._attr_native_min_value),
                self._attr_native_max_value,
            )

    async def async_set_native_value(self, value: float) -> None:
        """An automation (or user) set this room's drift -> recompute.

        Tightening below the room's current wander re-engages on this very
        compute — the walk-in snap-back. The engage latch is deliberately NOT
        reset: a band change redraws the coast window, it does not invalidate
        a run already headed for the (unchanged) target.
        """
        self._zone.drift = value
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()


class MXZEmergencyFloorNumber(MXZEntity, RestoreNumber):
    """A room's emergency-heat floor (v3.4.0).

    Below this the room forces heat (overriding heat lockout, eco, standoff
    priority and hysteresis) until it reaches floor + emergency_recovery —
    only while the zone's emergency-protect switch is ON (that switch, not a
    sentinel value here, is how protection is disabled). CONFIG category.
    """

    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer-alert"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MXZCoordinator, zone: Zone) -> None:
        super().__init__(coordinator, f"{zone.slug}_emergency_floor")
        self._zone = zone
        self._attr_translation_key = "zone_emergency_floor"
        self._attr_translation_placeholders = {"zone": zone.name}
        self._attr_native_unit_of_measurement = coordinator.temp_unit
        lo, hi = coordinator._profile["emergency_floor_bounds"]
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = coordinator._profile["emergency_floor_step"]
        self._attr_native_value = float(
            coordinator._profile["emergency_floor_default"]
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last floor (clamped into bounds) and seed the zone."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if (
            not self._restored_state_is_stale(last_state)
            and (last := await self.async_get_last_number_data())
            and last.native_value is not None
        ):
            self._attr_native_value = min(
                max(float(last.native_value), self._attr_native_min_value),
                self._attr_native_max_value,
            )
        self._zone.emergency_floor = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = float(value)
        self._zone.emergency_floor = float(value)
        self.async_write_ha_state()
        await self.coordinator.async_user_changed()
