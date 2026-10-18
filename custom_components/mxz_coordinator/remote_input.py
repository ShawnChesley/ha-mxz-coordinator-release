"""Remote (IR / wall) input classification for coordinated heads — pure logic.

The physical heads are actuators: the coordinator is their only intended writer.
A person can still change a head directly with its factory IR remote. This module
decides, from one observed head state change, what (if anything) that person asked
for, so the coordinator can adopt it into the room's canonical target/enable
instead of silently overwriting it on the next apply.

Field facts this design is built on (Mitsubishi IR remote + CN105 head, captured
2026-10-07 on the Kitchen head):

* Every remote button press transmits the remote's WHOLE displayed state (power,
  mode, temperature, fan). A fan-only press re-sent ON + COOL + 68 + QUIET.
* A transmission is invisible when it changes nothing the head exposes (remote OFF
  sent to a head the coordinator had already parked OFF produced no state change).
  So power knowledge derived from "what the remote last sent" can silently go stale
  -> power intent is taken ONLY from the head's own observed transitions.
* The remote cannot change its temperature while it is OFF (blank display).
* The remote keeps independent COOL and HEAT temperatures -> temperature evidence
  is tracked per remote mode and never compared across modes.
* The head's ``target_temp_low``/``target_temp_high`` are firmware-synthesized and
  move on mode changes alone (e.g. low 68.5 above high 68.0) -> never used. Only
  ``temperature`` counts, and only while the head is in heat or cool.

Rules (for a change already judged NOT to be the coordinator's own echo):

* power: head awake -> off            => manual OFF (authoritative for the room)
         head off   -> awake          => manual ON
* temperature: head in heat/cool reporting ``temperature`` T, and the remote record
  holds a previous externally observed temperature P for that SAME mode with
  |T - P| >= step/2                   => adopt T as the room target.
  (No record for that mode yet => record T, adopt nothing.)
  The record is updated with T on every external heat/cool observation, so a
  fan/power/mode/vane press that merely re-sends an unchanged remote display
  never moves the target.
* mode: never adopted (shared-mode arbitration stays with the coordinator).
* fan: not handled here — the existing manual-fan latch observes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODE_OFF = "off"
MODE_COOL = "cool"
MODE_HEAT = "heat"
_UNAVAILABLE = ("unavailable", "unknown")
TEMP_MODES = (MODE_COOL, MODE_HEAT)


@dataclass(frozen=True)
class HeadObs:
    """The parts of a head state that can carry human intent."""

    mode: str
    temperature: float | None  # only meaningful in heat/cool; else None
    fan: str | None

    @property
    def awake(self) -> bool:
        return self.mode != MODE_OFF


def obs_from_state(state: Any) -> HeadObs | None:
    """Reduce a HA State to a HeadObs; None if missing/unavailable."""
    if state is None or state.state in _UNAVAILABLE:
        return None
    attrs = state.attributes or {}
    temp: float | None = None
    if state.state in TEMP_MODES:
        raw = attrs.get("temperature")
        try:
            temp = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            temp = None
    return HeadObs(mode=state.state, temperature=temp, fan=attrs.get("fan_mode"))


@dataclass
class RemoteRecord:
    """Last EXTERNALLY observed remote temperature, per remote mode."""

    cool: float | None = None
    heat: float | None = None

    def get(self, mode: str) -> float | None:
        return (
            self.cool if mode == MODE_COOL else self.heat if mode == MODE_HEAT else None
        )

    def set(self, mode: str, value: float) -> None:
        if mode == MODE_COOL:
            self.cool = value
        elif mode == MODE_HEAT:
            self.heat = value

    def as_dict(self) -> dict[str, float | None]:
        return {"cool": self.cool, "heat": self.heat}

    @classmethod
    def from_dict(cls, data: Any) -> RemoteRecord:
        rec = cls()
        if isinstance(data, dict):
            for key in ("cool", "heat"):
                try:
                    val = data.get(key)
                    setattr(rec, key, float(val) if val is not None else None)
                except (TypeError, ValueError):
                    pass
        return rec


@dataclass
class RemoteIntent:
    """What one external head change asks of the room."""

    manual_off: bool = False
    manual_on: bool = False
    adopt_target: float | None = None
    record_changed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return self.manual_off or self.manual_on or self.adopt_target is not None


def relevant_change(old: HeadObs | None, new: HeadObs | None) -> bool:
    """True if mode, (heat/cool) temperature or fan changed.

    current_temperature, hvac_action and the synthesized low/high never count.
    """
    if old is None or new is None:
        return False
    return (old.mode, old.temperature, old.fan) != (new.mode, new.temperature, new.fan)


def classify_external(
    old: HeadObs,
    new: HeadObs,
    record: RemoteRecord,
    *,
    step: float,
) -> RemoteIntent:
    """Intent of an external (non-echo) head change. Mutates ``record``."""
    intent = RemoteIntent()
    if old.awake and not new.awake:
        intent.manual_off = True
        intent.notes.append("power: awake->off")
    elif not old.awake and new.awake:
        intent.manual_on = True
        intent.notes.append(f"power: off->{new.mode}")

    if new.mode in TEMP_MODES and new.temperature is not None:
        prev = record.get(new.mode)
        tol = (step or 1.0) / 2.0
        if prev is None:
            intent.notes.append(
                f"{new.mode} {new.temperature:g}: first observation, recorded only"
            )
        elif abs(new.temperature - prev) >= tol - 1e-9:
            intent.adopt_target = new.temperature
            intent.notes.append(
                f"{new.mode} {prev:g}->{new.temperature:g}: temperature adjusted"
            )
        else:
            intent.notes.append(
                f"{new.mode} {new.temperature:g}: unchanged on remote (resend)"
            )
        if prev is None or abs(new.temperature - prev) > 1e-9:
            record.set(new.mode, new.temperature)
            intent.record_changed = True
    return intent
