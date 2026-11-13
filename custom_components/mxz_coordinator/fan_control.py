"""Per-room fan control: policy, holds, protections and verification for HA.

Wires the pure ``fan_policy`` engine and ``field_journal`` matcher to one
coordinator. A room is either:

* ``legacy``       -- the pre-existing boost ladder and manual-fan latch in
                      ``coordinator.py``, untouched. This is the migration
                      default for EVERY room, so installing this release does
                      not change anyone's airflow until a policy is chosen.
* ``native_auto``  -- the head's own Auto while conditioning.
* ``adaptive``     -- Medium/High recovery stages on unrounded room error,
                      Auto near target (``fan_policy.RoomFanPolicy``).

A manual hold is separate from the policy and persists across restarts. Only a
person's gesture (classified ``external``), the service path, or the Fan-auto
switch OFF creates one; Release (button, service ``auto``, switch ON, remote
Auto outside the own-command window) ends it. Policy changes never release a
hold.

Writes are gated by ONE guard (``FanController.guard``) evaluated against live
state immediately before every dispatch -- policy decisions, service holds and
verification re-sends alike: none for a legacy room, while the coordinator,
room or head is unavailable or disabled, while the head is Off or in
``fan_only`` (coil dry) or any non-conditioning mode, during a standby hold, a
vane kick or a window pause, during defrost, preheat or standby, or when any
of those protection sensors is unavailable. A head is only driven while
actively conditioning; at the moment conditioning ends it is handed back to
Auto (if awake and not held). A service hold that is blocked is kept as a
pending hold and written once, when the guard permits.

Every new intent -- a command, a hold (service, gesture, switch), Release or a
policy change -- advances the head's command ``generation``. A verification
re-send carries the generation it was scheduled under and is dropped at
dispatch if anything newer happened, even when its task was already queued.

Bounded verification: one re-send after 30 s if the head has not reported the
commanded selected mode, then ``give_up`` after another 30 s, which turns a
report of the unchanged old speed into a ``mismatch`` diagnostic. Never retried
against a hold or a protection. Only SELECTED fan mode is classified, never the
actual blower speed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import MODE_COOL, MODE_FAN_ONLY, MODE_HEAT
from .fan_policy import FanPolicy, FanStage, RoomFanPolicy, supported_fan
from .field_journal import FieldJournal

if TYPE_CHECKING:
    from .coordinator import MXZCoordinator, Zone

_LOGGER = logging.getLogger(__name__)

POLICY_LEGACY = "legacy"
POLICY_OPTIONS = [POLICY_LEGACY, FanPolicy.NATIVE_AUTO.value, FanPolicy.ADAPTIVE.value]
FAN_FIELD = "fan_mode"
FAN_AUTO = "auto"
VERIFY_S = 30.0
PROTECTION_SUFFIXES = ("defrost", "preheat", "standby")
_UNAVAILABLE = ("unavailable", "unknown")
_STORE_VERSION = 1


@dataclass
class _Verify:
    token: str
    generation: int
    resent: bool = False
    reported: str | None = None  # what the head showed when the re-send was decided
    cancel: Callable[[], None] | None = None


@dataclass
class HeadFan:
    """Everything one managed head's fan control knows."""

    policy: str = POLICY_LEGACY
    room: RoomFanPolicy | None = None
    journal: FieldJournal | None = None
    verify: _Verify | None = None
    last_input: str | None = None
    protected: str | None = None
    driver: str | None = None
    stage: str | None = None
    diagnostic: str | None = None
    seeded: bool = False
    pending_hold: bool = False  # a requested hold not yet written to the head
    generation: int = 0  # advanced by every new intent; stale retries are dropped

    def __post_init__(self) -> None:
        if self.room is None:
            self.room = RoomFanPolicy()
        if self.journal is None:
            self.journal = FieldJournal()


class FanController:
    """Owns per-room fan policies and holds for one coordinator."""

    def __init__(self, hass: HomeAssistant, coord: MXZCoordinator, entry_id: str) -> None:
        self.hass = hass
        self.coord = coord
        self.heads: dict[str, HeadFan] = {}
        self._store: Store = Store(hass, _STORE_VERSION, f"mxz_coordinator.fan.{entry_id}")
        self._protection: dict[str, list[str]] = {}

    # -- persistence -----------------------------------------------------
    async def async_load(self) -> None:
        try:
            stored = await self._store.async_load() or {}
        except Exception:  # noqa: BLE001 - a corrupt store must not block setup
            _LOGGER.warning("MXZ: fan policy store unreadable; all rooms legacy")
            stored = {}
        for zone in self.coord.zones:
            head = self.head(zone.climate_id)
            rec = (stored.get("heads") or {}).get(zone.climate_id) or {}
            policy = rec.get("policy", POLICY_LEGACY)
            head.policy = policy if policy in POLICY_OPTIONS else POLICY_LEGACY
            if head.policy != POLICY_LEGACY:
                head.room.set_policy(FanPolicy(head.policy))
            hold = rec.get("hold")
            head.room.hold_speed = hold if isinstance(hold, str) and hold else None
            head.pending_hold = bool(rec.get("pending")) and head.room.hold_speed is not None

    def _payload(self) -> dict[str, Any]:
        return {
            "heads": {
                cid: {"policy": h.policy, "hold": h.room.hold_speed, "pending": h.pending_hold}
                for cid, h in self.heads.items()
            }
        }

    def save(self) -> None:
        self._store.async_delay_save(self._payload, 1)

    async def async_shutdown(self) -> None:
        for head in self.heads.values():
            self._cancel_verify(head)
        if self.heads:
            await self._store.async_save(self._payload())

    # -- queries -----------------------------------------------------------
    def head(self, climate_id: str) -> HeadFan:
        return self.heads.setdefault(climate_id, HeadFan())

    def managed(self, climate_id: str) -> bool:
        return self.head(climate_id).policy != POLICY_LEGACY

    def policy(self, climate_id: str) -> str:
        return self.head(climate_id).policy

    def held(self, climate_id: str) -> bool:
        return self.head(climate_id).room.hold_speed is not None

    def requested(self, climate_id: str) -> str:
        """What a room thermostat shows as its fan setting: the hold, else auto."""
        return self.head(climate_id).room.hold_speed or FAN_AUTO

    def diagnostics(self, climate_id: str) -> dict[str, Any]:
        head = self.head(climate_id)
        status = head.journal.status(FAN_FIELD)
        state = self.hass.states.get(climate_id)
        return {
            "fan_policy": head.policy,
            "fan_driver": head.driver,
            "fan_stage": head.stage,
            "fan_hold_speed": head.room.hold_speed,
            "fan_commanded": status["commanded"],
            "fan_reported": state.attributes.get("fan_mode") if state else None,
            "fan_mismatch": status["mismatch"],
            "fan_input": head.last_input,
            "fan_protected": head.protected,
            "fan_diagnostic": head.diagnostic,
            "fan_hold_pending": head.pending_hold,
        }

    # -- configuration -----------------------------------------------------
    async def async_set_policy(self, climate_id: str, policy: str) -> None:
        """Change a room's normal policy. Never releases a hold."""
        if policy not in POLICY_OPTIONS:
            raise HomeAssistantError(f"Unknown fan policy {policy}")
        head = self.head(climate_id)
        if policy == head.policy:
            return
        coord = self.coord
        now = time.monotonic()
        self._invalidate(head)
        if head.policy == POLICY_LEGACY:
            # Carry a legacy manual latch over as a hold; treat the legacy
            # ladder's recent tokens as OUR commands so a late echo of them is
            # ambiguous, never a person.
            if coord._fan_latched.get(climate_id):
                state = self.hass.states.get(climate_id)
                observed = state.attributes.get("fan_mode") if state else None
                if observed and observed != FAN_AUTO:
                    head.room.hold_speed = observed
            for token in (coord._fan_prev.get(climate_id), coord._fan_cmd.get(climate_id)):
                if token:
                    head.journal.note_own(FAN_FIELD, token, now)
        if policy == POLICY_LEGACY:
            hold = head.room.hold_speed
            # Hand the hold to the legacy latch so it is honored there too.
            coord._fan_idx.pop(climate_id, None)
            if hold:
                coord._fan_cmd[climate_id] = hold
                coord._fan_prev[climate_id] = hold
                coord._fan_latched[climate_id] = True
            else:
                coord._fan_latched[climate_id] = False
                coord._fan_cmd.pop(climate_id, None)
                coord._fan_prev.pop(climate_id, None)
            head.room.hold_speed = None
            head.pending_hold = False
        else:
            head.room.set_policy(FanPolicy(policy))
        head.policy = policy
        head.seeded = False
        self.save()
        coord.async_update_listeners()
        await coord.async_request_refresh()

    async def async_release(self, climate_id: str) -> None:
        """Release a manual hold back to the configured normal policy."""
        head = self.head(climate_id)
        if head.policy == POLICY_LEGACY:
            await self.coord.async_set_fan_auto(climate_id, True)
            return
        head.room.release()
        head.pending_hold = False
        self._invalidate(head)
        head.last_input = "release"
        self.save()
        self.coord.async_update_listeners()
        await self.coord.async_request_refresh()

    async def async_hold_current(self, climate_id: str) -> None:
        """Fan-auto switch OFF: hold whatever selected speed the head reports."""
        head = self.head(climate_id)
        state = self.hass.states.get(climate_id)
        observed = state.attributes.get("fan_mode") if state else None
        if observed is None or observed == FAN_AUTO:
            return  # nothing to hold (matches the legacy switch)
        head.room.external_fan(observed)
        head.pending_hold = False
        head.journal.reconcile(FAN_FIELD, observed, now=time.monotonic())
        self._invalidate(head)
        head.last_input = "switch_hold"
        self.save()
        self.coord.async_update_listeners()

    async def async_service_fan(self, climate_id: str, fan_mode: str) -> None:
        """Room thermostat ``set_fan_mode`` (CA, dashboard, HomeKit).

        ``auto`` releases to the configured policy; a named speed is an
        indefinite hold and is written to the head.
        """
        head = self.head(climate_id)
        if fan_mode.strip().casefold() == FAN_AUTO:
            await self.async_release(climate_id)
            return
        state = self.hass.states.get(climate_id)
        modes = (state.attributes.get("fan_modes") if state else None) or []
        token = next((m for m in modes if m.casefold() == fan_mode.strip().casefold()), None)
        if token is None:
            raise HomeAssistantError(f"Fan mode {fan_mode} is not supported by {climate_id}")
        head.room.external_fan(token)
        head.last_input = "service_hold"
        self._invalidate(head)
        if state is not None and state.attributes.get("fan_mode") == token:
            head.pending_hold = False
            head.journal.reconcile(FAN_FIELD, token, now=time.monotonic())
        else:
            # Kept as the room's hold even if the guard blocks the write now;
            # the actuator writes it once when permitted.
            head.pending_hold = True
            if await self._write(climate_id, head, token, verify=False):
                head.pending_hold = False
            elif head.pending_hold and head.room.hold_speed == token:
                # Blocked (not superseded): this request is still the hold.
                head.diagnostic = "hold_pending"
        self.save()
        self.coord.async_update_listeners()

    # -- head events -------------------------------------------------------
    @callback
    def observe(self, climate_id: str, old: State | None, new: State | None) -> None:
        """Classify a selected-fan change on a managed head (person vs ours)."""
        head = self.head(climate_id)
        if head.policy == POLICY_LEGACY or old is None or new is None:
            return
        if old.state in _UNAVAILABLE or new.state in _UNAVAILABLE:
            return
        before, after = old.attributes.get("fan_mode"), new.attributes.get("fan_mode")
        if after is None or before == after:
            return
        kind = head.journal.classify({FAN_FIELD: after}, now=time.monotonic())[FAN_FIELD]
        head.last_input = kind
        if kind == "echo" and head.verify is not None and head.verify.token == after:
            self._cancel_verify(head)
        if kind == "mismatch":
            head.diagnostic = "fan_mode_mismatch"
            _LOGGER.warning(
                "MXZ: %s kept fan %s after verification of %s gave up",
                climate_id, after, head.journal.status(FAN_FIELD)["commanded"],
            )
        if (
            kind == "echo" and head.pending_hold and head.room.hold_speed is not None
            and after.casefold() == head.room.hold_speed.casefold()
        ):
            head.pending_hold = False
            self.save()
        if head.room.observe_selected(kind, after):
            head.pending_hold = False
            self._invalidate(head)
            self.save()
            _LOGGER.info("MXZ: %s fan %s adopted as a manual gesture", climate_id, after)
        self.coord.async_update_listeners()

    # -- protections -------------------------------------------------------
    def _protection_entities(self, climate_id: str) -> list[str]:
        if climate_id not in self._protection:
            found: list[str] = []
            reg = er.async_get(self.hass)
            entry = reg.async_get(climate_id)
            if entry is not None and entry.device_id:
                for ent in er.async_entries_for_device(reg, entry.device_id):
                    if ent.domain == "binary_sensor" and ent.entity_id.endswith(
                        tuple(f"_{s}" for s in PROTECTION_SUFFIXES)
                    ):
                        found.append(ent.entity_id)
            self._protection[climate_id] = sorted(found)
        return self._protection[climate_id]

    def guard(self, climate_id: str) -> str | None:
        """Why a fan write to this head must not be dispatched NOW, or None.

        Evaluated from live state immediately before every dispatch (policy
        writes, service holds, verification re-sends), so a protection that
        appears between scheduling and dispatch still blocks the write.
        """
        coord = self.coord
        cid = climate_id
        if self.head(cid).policy == POLICY_LEGACY:
            return "legacy_policy"
        zone = coord.zone_for_head(cid)
        if zone is None:
            return "unknown_room"
        state = self.hass.states.get(cid)
        if not coord.coordinator_enable:
            return "coordinator_disabled"
        if state is None or state.state in _UNAVAILABLE:
            return "head_unavailable"
        if coord.inhibited:
            return "standby_hold"
        if cid in coord._vane_kicks:
            return "vane_kick"
        if not zone.enable:
            return "room_disabled"
        if coord.zone_window_paused(zone):
            return "window_pause"
        if state.state == MODE_FAN_ONLY:
            return "coil_dry_or_fan_only"
        if state.state not in (MODE_HEAT, MODE_COOL):
            return f"head_{state.state}"
        return self._sensor_protection(cid)

    def protection(self, zone: Zone, act: str) -> str | None:
        """Why this cycle's fan writes are blocked, or None (plan act + guard)."""
        if act == MODE_FAN_ONLY:
            return "coil_dry_or_fan_only"
        return self.guard(zone.climate_id)

    def _sensor_protection(self, cid: str) -> str | None:
        for ent in self._protection_entities(cid):
            st = self.hass.states.get(ent)
            name = ent.rsplit("_", 1)[-1]
            if st is None or st.state in _UNAVAILABLE:
                return f"{name}_unavailable"
            if st.state == "on":
                return name
        return None

    # -- the actuator ------------------------------------------------------
    async def apply(
        self, zone: Zone, act: str, plan: dict[str, Any], *, eco: bool, handback: bool,
        target: float,
    ) -> None:
        """Drive one managed head's selected fan mode for this cycle."""
        cid = zone.climate_id
        head = self.head(cid)
        state = self.hass.states.get(cid)
        modes = (state.attributes.get("fan_modes") if state else None) or []
        # At the handback edge the head is still awake and conditioning just
        # ended: judge protections as for a running head.
        head.protected = self.protection(zone, MODE_HEAT if handback else act)
        if head.protected is not None or FAN_AUTO not in [m.casefold() for m in modes]:
            head.room.reset_recovery()
            head.driver, head.stage = ("hold" if head.room.hold_speed else "head"), None
            return
        if not head.seeded:
            self._seed(cid, head, state)
        if head.room.hold_speed is not None:
            head.driver, head.stage = "hold", None
            if head.pending_hold and not handback:
                hold = head.room.hold_speed
                current = state.attributes.get("fan_mode")
                if current is not None and current.casefold() == hold.casefold():
                    head.pending_hold = False
                    head.journal.reconcile(FAN_FIELD, hold, now=time.monotonic())
                elif await self._write(cid, head, hold, verify=False):
                    head.pending_hold = False
                self.save()
            return
        if handback:
            desired = FAN_AUTO
            head.driver, head.stage = "head", None
            head.room.reset_recovery()
        elif act in (MODE_HEAT, MODE_COOL):
            temp = plan.get(f"{zone.slug}_temp")
            ok = plan.get(f"{zone.slug}_temp_ok", True)
            error = None
            if ok and isinstance(temp, (int, float)):
                error = (target - temp) if act == MODE_HEAT else (temp - target)
                # The policy thresholds are Fahrenheit deltas (High at 1.5 F short,
                # ...); a Celsius installation's error is converted, not reused.
                if self.coord.celsius:
                    error *= 1.8
            floor = None
            if plan.get(f"{zone.slug}_contended") and not eco:
                # Contended finish (local fork): another room is waiting for the
                # opposite shared mode — hold the fan up through the tail. Quiet
                # hours cap the floor at High so a bedroom never gets the top
                # stage mid-night; the floor never lowers the adaptive choice.
                floor = (
                    FanStage.HIGH if plan.get("quiet_hours") else FanStage.VERY_HIGH
                )
            decision = head.room.decide(
                mode=act, error_f=error, now=time.monotonic(), satisfied=eco,
                floor=floor,
            )
            decision = supported_fan(decision, list(modes))
            head.driver = decision.driver
            head.stage = decision.stage.value if decision.stage else None
            head.diagnostic = decision.diagnostic
            desired = decision.selected
        else:
            head.room.reset_recovery()
            head.driver, head.stage = "head", None
            return  # off / parked: never written (Garage stays untouched)
        if desired is None:
            return
        current = state.attributes.get("fan_mode")
        if current is not None and current.casefold() == desired.casefold():
            head.journal.reconcile(FAN_FIELD, desired, now=time.monotonic())
            return
        if head.verify is not None and head.verify.token.casefold() == desired.casefold():
            return  # bounded verification owns re-sends of this command
        status = head.journal.status(FAN_FIELD)
        if status["commanded"] == desired.casefold() and (status["gave_up"] or status["mismatch"]):
            # Verification already gave up on this value: report, never hammer.
            # A different intended value or a person's gesture starts afresh.
            head.diagnostic = "fan_mode_mismatch" if status["mismatch"] else "fan_mode_unverified"
            return
        await self._write(cid, head, desired, verify=True)

    def _seed(self, cid: str, head: HeadFan, state: State | None) -> None:
        """First cycle after start or policy change: learn nothing from reports.

        A restored hold stays a hold. Only our own intent is reconciled.
        """
        head.seeded = True
        if head.room.hold_speed is not None:
            head.journal.reconcile(FAN_FIELD, head.room.hold_speed, now=time.monotonic())
            observed = state.attributes.get("fan_mode") if state else None
            if observed and observed.casefold() != head.room.hold_speed.casefold():
                head.diagnostic = "hold_differs_from_head"

    async def _write(
        self, cid: str, head: HeadFan, token: str, *, verify: bool,
        retry_of: int | None = None,
    ) -> bool:
        """Dispatch one fan write if it is still permitted and still current.

        Checked synchronously right before dispatch: the live protection guard,
        and for a verification re-send (``retry_of`` = its generation) that no
        newer intent has happened since it was scheduled. A new command
        advances the generation itself, invalidating any queued older retry.

        The generation is captured BEFORE awaiting the service call. A call
        that returns after a newer intent (command, hold, Release, policy
        change) creates no verification and returns False, so a late older
        write can never replace or revive anything newer.

        Returns True only if the write was dispatched AND is still the current
        intent when its call returns; False when refused (nothing recorded,
        nothing sent) or superseded during the call.
        """
        if (reason := self.guard(cid)) is not None:
            head.protected = reason
            _LOGGER.debug("MXZ: fan %s to %s blocked: %s", token, cid, reason)
            return False
        if retry_of is not None:
            ver = head.verify
            live = self.hass.states.get(cid)
            live_fan = live.attributes.get("fan_mode") if live else None
            if (
                head.generation != retry_of or ver is None or ver.generation != retry_of
                or ver.token != token or head.room.hold_speed is not None
                # The head's selected fan moved since the re-send was decided
                # (a gesture whose event has not been processed yet): stand down.
                or live_fan != ver.reported
            ):
                _LOGGER.debug("MXZ: stale fan re-send %s to %s dropped", token, cid)
                return False
        else:
            self._invalidate(head)
        generation = head.generation  # captured before the await below
        state = self.hass.states.get(cid)
        previous = state.attributes.get("fan_mode") if state else None
        staged = head.journal.record(FAN_FIELD, token, previous=previous, now=time.monotonic())
        try:
            await self.coord.async_head_call("set_fan_mode", {"entity_id": cid, "fan_mode": token})
        except HomeAssistantError:
            head.journal.rollback(staged)
            raise
        if head.generation != generation:
            _LOGGER.debug("MXZ: fan %s to %s superseded during its call", token, cid)
            return False
        if verify:
            head.verify = _Verify(token, generation)
            head.verify.cancel = async_call_later(
                self.hass, VERIFY_S, partial(self._on_verify, cid, generation)
            )
        return True

    def _invalidate(self, head: HeadFan) -> None:
        """A new intent: older verification and any queued re-send are void."""
        head.generation += 1
        self._cancel_verify(head)

    def _cancel_verify(self, head: HeadFan) -> None:
        if head.verify is not None and head.verify.cancel is not None:
            head.verify.cancel()
        head.verify = None

    @callback
    def _on_verify(self, cid: str, generation: int, _now: Any) -> None:
        head = self.head(cid)
        ver = head.verify
        if ver is None or ver.generation != generation or head.generation != generation:
            return
        ver.cancel = None
        state = self.hass.states.get(cid)
        reported = state.attributes.get("fan_mode") if state else None
        zone = self.coord.zone_for_head(cid)
        if reported is not None and reported.casefold() == ver.token.casefold():
            head.verify = None
            return
        if zone is None or head.room.hold_speed is not None or self.guard(cid) is not None:
            head.verify = None  # never retried against a hold, Off head or protection
            return
        if not ver.resent:
            ver.resent = True
            ver.reported = reported
            self.hass.async_create_task(self._resend(cid, ver.token, generation))
            ver.cancel = async_call_later(
                self.hass, VERIFY_S, partial(self._on_verify, cid, generation)
            )
            return
        head.verify = None
        head.journal.give_up(FAN_FIELD)
        head.diagnostic = "fan_mode_unverified"
        _LOGGER.warning(
            "MXZ: %s did not take fan %s after one re-send (reports %s)",
            cid, ver.token, reported,
        )
        self.coord.async_update_listeners()

    async def _resend(self, cid: str, token: str, generation: int) -> None:
        head = self.head(cid)
        try:
            await self._write(cid, head, token, verify=False, retry_of=generation)
        except HomeAssistantError as err:
            _LOGGER.warning("MXZ: fan re-send to %s failed: %s", cid, err)
