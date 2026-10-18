"""Per-room window/door pause (local fork, 2026-10-17) -- pure logic, no HA imports.

A room with contact sensors is PAUSED once any of them has been open for
``delay`` seconds. While paused the room makes no comfort call at all -- no
heat, no cool -- whatever its target, direction or enable say, and whoever set
them (Climate Advisor, a schedule, the IR remote, a dashboard). Those inputs are
still recorded on the room as usual; the pause only suppresses ACTING on them.
When every contact is closed again the pause ends immediately and the room is
simply re-evaluated from its CURRENT settings on the next compute -- nothing is
"restored" from before the window opened.

Unavailable/unknown contacts are handled deliberately -- HOLD LAST KNOWN:

* any contact reads open                -> open (start / continue the delay);
* every contact reads closed            -> closed (pause ends);
* none open but some unknown/unavailable -> keep the previous state: a room that
  was open (or paused) stays open (paused), a room that was closed stays
  closed. A flaky sensor therefore can neither wedge a room off forever from a
  closed state nor silently un-pause a room whose window was last seen open.

The state is a small dict persisted across restarts by the caller, so the delay
clock survives a restart (a window open since before the restart pauses on time,
not ``delay`` seconds after HA comes back).
"""

from __future__ import annotations

from typing import Any

CONTACT_OPEN = "on"
CONTACT_CLOSED = "off"

WINDOW_NONE = "none"  # no contacts configured
WINDOW_CLOSED = "closed"
WINDOW_OPEN = "open"  # open, delay still running
WINDOW_PAUSED = "paused"

EVENT_OPENED = "opened"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"  # closed again (was open or paused)
EVENT_UNKNOWN = "unknown"  # a contact became unknown; holding last known


def classify_contacts(states: list[str | None]) -> str:
    """Reduce raw contact states to ``open`` | ``closed`` | ``unknown``."""
    if any(s == CONTACT_OPEN for s in states):
        return "open"
    if states and all(s == CONTACT_CLOSED for s in states):
        return "closed"
    return "unknown"


def window_step(
    *,
    state: dict[str, Any] | None,
    contacts: list[str | None],
    now: float,
    delay: float,
) -> tuple[dict[str, Any], str | None]:
    """Advance one room's window state. Pure; returns ``(new_state, event)``.

    ``state`` is ``{"open_since": float | None, "paused": bool,
    "sensor_unknown": bool}`` (None = fresh: treated as closed). ``contacts`` are
    the raw HA states of the room's contacts (``"on"`` = open, ``"off"`` =
    closed, anything else = unknown). The returned dict is a NEW object whenever
    anything changed so the caller can persist on ``new is not old``.
    """
    old = state or {"open_since": None, "paused": False, "sensor_unknown": False}
    reading = classify_contacts(contacts)
    open_since = old.get("open_since")
    paused = bool(old.get("paused"))
    unknown = reading == "unknown"

    if reading == "open":
        if open_since is None:
            open_since = now
        new_paused = paused or (now - float(open_since)) >= delay
    elif reading == "closed":
        open_since = None
        new_paused = False
    else:  # unknown: hold last known
        new_paused = paused
        if open_since is not None and not paused:
            # Last seen open and the delay is still running: keep counting.
            new_paused = (now - float(open_since)) >= delay

    new = {"open_since": open_since, "paused": new_paused, "sensor_unknown": unknown}
    if new == old:
        return old, None

    event: str | None = None
    if new_paused and not paused:
        event = EVENT_PAUSED
    elif paused and not new_paused or (
        old.get("open_since") is not None and open_since is None
    ):
        event = EVENT_RESUMED
    elif open_since is not None and old.get("open_since") is None:
        event = EVENT_OPENED
    elif unknown and not old.get("sensor_unknown"):
        event = EVENT_UNKNOWN
    return new, event


def window_label(state: dict[str, Any] | None, has_contacts: bool) -> str:
    """Display label for a room's window state."""
    if not has_contacts:
        return WINDOW_NONE
    if not state:
        return WINDOW_CLOSED
    if state.get("paused"):
        return WINDOW_PAUSED
    if state.get("open_since") is not None:
        return WINDOW_OPEN
    return WINDOW_CLOSED


def pause_deadline(state: dict[str, Any] | None, delay: float) -> float | None:
    """Epoch seconds when a running delay will pause the room, else None."""
    if not state or state.get("paused") or state.get("open_since") is None:
        return None
    return float(state["open_since"]) + delay
