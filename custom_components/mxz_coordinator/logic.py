"""Pure decision math for MXZ Coordinator — no Home Assistant dependency.

Mirrors the Jinja in the YAML package's ``sensor.mxz_plan`` and the setpoint/action
math in ``script.mxz_coordinate``. Kept HA-free so the validated truth table can be
unit-tested directly.
"""

from __future__ import annotations

from .const import (
    DIRECTION_AUTO,
    DIRECTION_COOL,
    DIRECTION_HEAT,
    ECO_COOL_HIGH,
    ECO_COOL_LOW,
    ECO_HEAT_HIGH,
    ECO_HEAT_LOW,
    ENGAGE_SATISFIED,
    MODE_COOL,
    MODE_FAN_ONLY,
    MODE_HEAT,
    MODE_OFF,
)


def room_call(
    *,
    temp: float,
    target: float,
    enabled: bool,
    eco: bool,
    sensor_ok: bool,
    band: float,
    eco_cool_max: float,
    eco_heat_min: float,
    neutral: str,
    heat_lockout: bool = False,
    heat_lockout_floor: float = 0.0,
    cool_lockout: bool = False,
    cool_lockout_ceiling: float = 200.0,
    direction: str = DIRECTION_AUTO,
) -> str:
    """Per-room call for one band (demand uses S/'neutral', engage uses D/'satisfied').

    ``direction`` (local fork): ``heat_cool`` (default) is the original
    two-sided behaviour. ``heat`` never returns a cool call and ``cool`` never
    returns a heat call -- the room returns ``neutral`` instead, so a blocked
    request can never flip into the opposite direction. Eco extremes are
    protection, not comfort, and ignore ``direction``.

    Mirrors the primary/secondary_demand and primary/secondary_engage attributes.

    ``heat_lockout`` holds off the heat call (the room idles instead of heating — e.g.
    to let passive solar warm it in summer) UNLESS the room has dropped below
    ``heat_lockout_floor``, a safety floor so a genuinely cold room still gets heat.
    ``cool_lockout`` is the mirror for the cold season: it holds off the cool call
    (let the room drift down on its own) UNLESS it rises above ``cool_lockout_ceiling``.
    The eco extremes are unaffected; the two lockouts are independent.
    """
    if not enabled:
        return MODE_OFF
    if eco:
        if temp > eco_cool_max:
            return MODE_COOL
        if temp < eco_heat_min:
            return MODE_HEAT
        return neutral
    if not sensor_ok:
        return neutral
    if temp > target + band:
        if direction == DIRECTION_HEAT:
            return neutral
        if cool_lockout and temp <= cool_lockout_ceiling:
            return neutral
        return MODE_COOL
    if temp < target - band:
        if direction == DIRECTION_COOL:
            return neutral
        if heat_lockout and temp >= heat_lockout_floor:
            return neutral
        return MODE_HEAT
    return neutral


def engage_with_latch(
    *,
    prior: str | None,
    band: float,
    neutral: str,
    cool_stop_offset: float = 0.0,
    **call_kwargs,
) -> str:
    """Per-room engage with run-to-target hysteresis.

    The engage deadband exists so a satisfied room COASTS instead of flip-
    flopping — it was never meant to truncate the approach. Without a latch,
    a room cooling toward 63 would stop at 64 (target + band) and never reach
    the number the user actually set.

    * Not engaged (``prior`` is None): a room must drift PAST ``band`` to
      engage — unchanged behavior.
    * Engaged (``prior`` is cool|heat): the room runs all the way TO its
      target — satisfied means exactly the number the user set. Crossing the target — or a lockout / eco / disable /
      sensor dropout, all still evaluated via :func:`room_call` — disengages
      it back to ``neutral`` to coast until it drifts past ``band`` again.
    * A direction flip while engaged (overshoot, opened window) never
      whiplashes: anything other than continuing in ``prior``'s direction
      disengages to ``neutral`` first.

    Eco mode bypasses the latch entirely (protection extremes are already a
    wide band; run-to-extreme would defeat their purpose).

    ``cool_stop_offset`` (>= 0) ends an engaged COOL run early, at
    ``target + cool_stop_offset`` instead of the target. The caller passes the
    already-bounded *effective* offset (see :func:`effective_cool_stop_offset`),
    so the stop always sits below the re-engage start (``target + band``). A
    room below the shifted target reads as ``heat`` from :func:`room_call`,
    which the "anything but prior -> neutral" rule maps to coasting — the
    shifted target can never produce a heat command.
    """
    if prior in (MODE_COOL, MODE_HEAT) and not call_kwargs.get("eco"):
        # Engaged phase: band 0 — the room runs exactly TO its target, then
        # coasts. The re-engage drift (``band``) is the only tunable here.
        if prior == MODE_COOL and cool_stop_offset > 0:
            call_kwargs = {
                **call_kwargs,
                "target": call_kwargs["target"] + cool_stop_offset,
            }
        raw = room_call(band=0.0, neutral=neutral, **call_kwargs)
        if raw == prior or raw == MODE_OFF:
            return raw
        return neutral  # target reached / lockout / direction flip -> coast
    return room_call(band=band, neutral=neutral, **call_kwargs)


def season_lockouts(
    *, outdoor_high: float | None, heat_above: float, cool_below: float
) -> tuple[bool, bool]:
    """Derive (heat_lockout, cool_lockout) from the local outdoor daily-high temp.

    The decision is driven by *local weather*, not the calendar: pass the forecast
    daily high (from a weather entity) or an outdoor temperature reading.

    * high >= ``heat_above`` -> warm season -> heat_lockout on (don't actively heat).
    * high <= ``cool_below`` -> cold season -> cool_lockout on (don't actively cool).
    * in between (shoulder season) -> neither -> normal heat + cool.

    The gap between the two thresholds is the hysteresis band — the forecast has to
    swing across the whole shoulder zone to flip a lockout, so it won't chatter.
    ``None`` (no signal — weather unavailable) yields no lockout (safe: normal auto).
    """
    if outdoor_high is None:
        return (False, False)
    if outdoor_high >= heat_above:
        return (True, False)
    if outdoor_high <= cool_below:
        return (False, True)
    return (False, False)


def effective_cool_stop_offset(
    *, offset: float, drift: float, margin: float
) -> float:
    """Bound the early-stop offset so the stop stays below the start.

    Drift decides when cooling starts (``target + drift``); the offset decides
    when it stops (``target + offset``). ``min(offset, drift - margin)``,
    floored at 0, keeps at least ``margin`` between them; a drift at or below
    ``margin`` yields 0 (today's run-to-target behaviour).
    """
    return max(0.0, min(float(offset), float(drift) - float(margin)))


# Emergency-heat latch events (returned by :func:`emergency_step` for logging).
EMERGENCY_ENTER = "enter"
EMERGENCY_EXIT = "exit"
EMERGENCY_CLEARED = "cleared"  # protection or zone enable turned off
EMERGENCY_SENSOR_RELEASE = "sensor_release"  # sensor invalid past the grace
EMERGENCY_LOSS_START = "loss_start"  # sensor went invalid (grace clock starts)
EMERGENCY_SENSOR_BACK = "sensor_back"  # sensor valid again inside the grace
EMERGENCY_RESTORED = "restored"  # pending (restored) latch confirmed
EMERGENCY_RESTORE_DROPPED = "restore_dropped"  # pending latch: room already recovered


def emergency_step(
    *,
    state: dict | None,
    protect: bool,
    enabled: bool,
    sensor_ok: bool,
    temp: float,
    floor: float,
    recovery: float,
    grace_s: float,
    now: float,
) -> tuple[dict | None, str | None]:
    """Advance one zone's emergency-heat latch. Pure; returns (state, event).

    ``state`` is None (not in emergency) or a dict with ``entered_at`` (epoch
    s), ``loss_since`` (epoch s the sensor went invalid, or None) and
    ``pending`` (True for a latch restored after a restart that has not yet
    seen a valid reading). The returned dict is a NEW object whenever anything
    changed, so the caller can persist on ``new is not old``.

    * Protection off or zone disabled -> cleared immediately.
    * Enter only on a VALID reading below ``floor``.
    * Exit at ``temp >= floor + recovery`` (valid reading).
    * Invalid sensor while latched: hold up to ``grace_s`` measured from the
      FIRST invalid reading (``loss_since``, persisted — so a restart cannot
      extend it; a restored latch with no stamp gets one now), then release.
    * A pending latch is confirmed by its first valid reading only if the room
      is still below ``floor + recovery``; otherwise it is dropped.
    """
    if not (protect and enabled):
        return (None, EMERGENCY_CLEARED if state is not None else None)
    if state is None:
        if sensor_ok and temp < floor:
            return (
                {"entered_at": now, "loss_since": None, "pending": False},
                EMERGENCY_ENTER,
            )
        return (None, None)
    if not sensor_ok:
        loss = state.get("loss_since")
        if loss is None:
            return ({**state, "loss_since": now}, EMERGENCY_LOSS_START)
        if now - float(loss) >= grace_s:
            return (None, EMERGENCY_SENSOR_RELEASE)
        return (state, None)
    # Valid reading.
    if temp >= floor + recovery:
        return (
            None,
            EMERGENCY_RESTORE_DROPPED if state.get("pending") else EMERGENCY_EXIT,
        )
    if state.get("pending"):
        return ({**state, "pending": False, "loss_since": None}, EMERGENCY_RESTORED)
    if state.get("loss_since") is not None:
        return ({**state, "loss_since": None}, EMERGENCY_SENSOR_BACK)
    return (state, None)


def emergency_target(*, target: float, floor: float, recovery: float) -> float:
    """Heat setpoint while in emergency: never below the recovery threshold."""
    return max(float(target), float(floor) + float(recovery))


def shared_mode(
    *,
    demands: list[str],
    current: str,
    allowed: bool,
    resting: str | None = None,
    emergency_heat: bool = False,
) -> str:
    """Choose the shared mode (cool|heat). Mirrors the mxz_plan state template.

    ``demands`` is one demand per zone in PRIORITY ORDER (index 0 = highest).
    ``current`` must already be normalized to cool|heat (cold start -> cool).
    ``allowed`` is the hysteresis gate. In a standoff (some zones calling cool
    while others call heat) the HIGHEST-PRIORITY calling zone wins — the N-zone
    generalization of "the primary room wins".

    ``resting`` biases the mode used when NO room is calling. ``None`` (or any
    non cool|heat value) keeps the original behavior — rest at ``current`` (the
    last called mode). Set it to cool|heat to always settle there when idle; a
    genuine opposite demand still flips the mode (the bias only changes the
    neutral fallback, gated by the same hysteresis as any other flip).

    ``emergency_heat`` (some room is below its emergency floor) returns HEAT
    outright: it overrides standoff priority and the hysteresis gate.
    """
    if emergency_heat:
        return MODE_HEAT
    rest = resting if resting in (MODE_COOL, MODE_HEAT) else current
    any_cool = MODE_COOL in demands
    any_heat = MODE_HEAT in demands
    standoff = any_cool and any_heat

    if standoff:
        proposed = next(
            (d for d in demands if d in (MODE_COOL, MODE_HEAT)), current
        )
    elif any_cool:
        proposed = MODE_COOL
    elif any_heat:
        proposed = MODE_HEAT
    else:
        proposed = rest

    still = (current == MODE_COOL and any_cool) or (current == MODE_HEAT and any_heat)
    if proposed != current and allowed and (standoff or not still):
        return proposed
    return current


def setpoints(
    *,
    mode: str,
    target: float,
    eco: bool,
    clamp_min: float,
    clamp_max: float,
    band: float = 2.0,
    step: float = 1.0,
    eco_cool: tuple[float, float] = (float(ECO_COOL_LOW), float(ECO_COOL_HIGH)),
    eco_heat: tuple[float, float] = (float(ECO_HEAT_LOW), float(ECO_HEAT_HIGH)),
) -> tuple[float, float]:
    """Return (low, high) setpoint edges, clamped. Mirrors the actuator's edge math.

    Unit-agnostic: ``band``/``step``/``eco_*`` carry the temperature unit. The
    defaults reproduce the original °F behavior (2° band, whole-degree steps,
    76/78 & 59/61 eco edges); a °C caller passes a 1° band, 0.5° step, and metric
    eco edges. Edges are rounded to ``step`` and clamped to [clamp_min, clamp_max].
    """
    if eco:
        return eco_cool if mode == MODE_COOL else eco_heat
    tc = max(min(target, clamp_max), clamp_min)
    if mode == MODE_COOL:
        low, high = max(tc - band, clamp_min), tc
    else:
        low, high = tc, min(tc + band, clamp_max)
    return (_round_to(low, step), _round_to(high, step))


def _round_to(value: float, step: float) -> float:
    """Round ``value`` to the nearest multiple of ``step`` (step<=0 -> unchanged)."""
    if step <= 0:
        return value
    return round(value / step) * step


def fan_for_delta(
    *,
    delta: float,
    cur_idx: int,
    up_at: tuple[float, ...],
    down_at: tuple[float, ...],
    max_idx: int,
) -> int:
    """Pick a fan-ladder INDEX (0..) from how far the room is off-target.

    Tesla-style: a big ``delta`` steps the fan up toward ``max_idx``; as the room
    closes on target the fan eases down. ``up_at``/``down_at`` carry a 0.5 °F
    hysteresis band so the speed doesn't chatter on the boundary. The caller owns
    persisting ``cur_idx`` between calls (per head).
    """
    # step UP as far as delta allows, never above max_idx
    while cur_idx < len(up_at) and cur_idx < max_idx and delta >= up_at[cur_idx]:
        cur_idx += 1
    # step DOWN only once delta clears the lower rung's down-threshold
    while cur_idx > 0 and delta < down_at[cur_idx - 1]:
        cur_idx -= 1
    return min(cur_idx, max_idx)


def head_action(*, engage: str, mode: str, eco: bool, idle: str = MODE_FAN_ONLY) -> str:
    """Map a head's engage state to the mode to command. Mirrors p_act/s_act.

    ``idle`` is the pre-resolved parking mode (fan_only or off) for a head that
    is neither disabled nor running — satisfied rooms and standoff losers both
    land there. Precedence: disabled > eco-satisfied > running > idle.
    """
    if engage == MODE_OFF:
        return MODE_OFF
    if eco and engage == ENGAGE_SATISFIED:
        return MODE_OFF
    if engage == mode:
        return mode
    return idle


def widen_unused_edge(
    *,
    mode: str,
    low: float,
    high: float,
    min_gap: float,
    band: tuple[float, float] | None = None,
) -> tuple[float, float, bool]:
    """Make ``high - low >= min_gap`` by moving only the edge ``mode`` does not use.

    Two-setpoint heads on the Mahtanar ``mitsubishi_itp`` firmware (v2.0.1)
    force cool - heat >= 2.0 C and, when a command is narrower, move an edge
    themselves -- sometimes the ACTIVE one, which then alternates with our
    rewrite. Heat keeps ``low`` (the heat edge) and raises ``high``; cool keeps
    ``high`` and lowers ``low``. ``band`` is the head's accept-able (lo, hi) in
    the same units; the unused edge is clamped into it. Only if the band is too
    tight to fit the gap there is the active edge pulled inward -- the minimum
    move that satisfies the firmware deterministically (it would move it
    anyway) -- and the third return value is True so the caller can log it.
    """
    if high - low >= min_gap:
        return (low, high, False)
    b_lo, b_hi = band if band is not None else (float("-inf"), float("inf"))
    moved = False
    if mode == MODE_COOL:
        low = max(high - min_gap, b_lo)
        if high - low < min_gap:
            high = min(low + min_gap, b_hi)
            moved = True
    else:  # MODE_HEAT
        high = min(low + min_gap, b_hi)
        if high - low < min_gap:
            low = max(high - min_gap, b_lo)
            moved = True
    return (low, high, moved)
