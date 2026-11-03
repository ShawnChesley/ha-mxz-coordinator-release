"""Per-field command matching, without suppressing unrelated manual inputs.

This is a primitive for the later HA event adapter. It supplies matching, not
proof of provenance: identical remote gestures and echoes are indistinguishable.
Targets use their separate value/persistence journal (target_input.py).

Classifications returned by :meth:`FieldJournal.classify`:

``echo``        the value the head is expected to hold (our command, our own
                reconciled intent, or a person's already-classified gesture).
                An echo of our command confirms it and clears any mismatch.
``transition``  the value the field held before our latest command, while that
                command is unconfirmed and verification is still running.
``mismatch``    that same previous value after verification gave up (or the
                transition cap passed) without the head taking our command.
                A diagnostic, never a person's gesture: the configured policy
                stays in force and no hold is created.
``ambiguous``   one of our own recent commands, but not the current expectation
                (a delayed or reordered report). Never a person's gesture: an
                adapter must not adopt it, so it cannot release a manual hold.
``external``    anything else: a person's gesture. The journal then expects that
                value, so later reports of it are echoes and a later press of
                our old command is again external.

Each field has one immutable state record; :meth:`FieldJournal.rollback`
restores the whole record (command, confirmation, verification status), never
just the command. Clocks are seconds from a monotonic source; a backward step
is treated as "window over", never as an extension.
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field, replace

# Our own commands remain recognizable for this long (and at most this many),
# so a delayed report of an older command is "ambiguous", not a person.
OWN_HISTORY_S = 120.0
OWN_HISTORY_MAX = 8
# Upper bound on treating the previous value as transitional, even if the
# adapter never confirms or gives up (one re-send plus a 30 s check fits).
TRANSITION_CAP_S = 90.0


def normalize(value: object) -> str:
    """Comparable form of a reported or commanded field value.

    Numbers compare by value ("72" == "72.0"); text ignores case and runs of
    whitespace ("Very High" == "very  high").
    """
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return re.sub(r"\s+", " ", text).casefold()
    if not math.isfinite(number):
        return text.casefold()
    return format(number, "g")


@dataclass(frozen=True)
class FieldCommand:
    value: str
    previous: str | None
    sent_at: float
    context_id: str | None
    origin: str = "command"  # "command" | "reconcile" | "external"


@dataclass(frozen=True)
class FieldState:
    """Everything the journal knows about one field, replaced as a unit."""

    command: FieldCommand
    confirmed: bool = False
    gave_up: bool = False
    mismatch: str | None = None  # the reported value while in mismatch


@dataclass(frozen=True)
class Staged:
    """What :meth:`FieldJournal.record` staged; pass it back to roll back."""

    field: str
    state: FieldState
    prior: FieldState | None

    @property
    def command(self) -> FieldCommand:
        return self.state.command


@dataclass
class FieldJournal:
    states: dict[str, FieldState] = field(default_factory=dict)
    transition_s: float = TRANSITION_CAP_S
    history_s: float = OWN_HISTORY_S
    _own: dict[str, deque[tuple[str, float]]] = field(default_factory=dict)

    @property
    def commands(self) -> dict[str, FieldCommand]:
        """Current expectation per field (read-only view)."""
        return {name: st.command for name, st in self.states.items()}

    # -- commands -----------------------------------------------------------
    def record(
        self, field_name: str, value: object, *, previous: object | None, now: float,
        context_id: str | None = None,
    ) -> Staged:
        """Stage before dispatch; keep the result to roll back a failed write.

        A re-send of the current command keeps its original timestamp and its
        confirmation/verification status (verification is bounded, not renewed).
        """
        norm = normalize(value)
        prior = self.states.get(field_name)
        self._remember_own(field_name, norm, now)
        if prior is not None and prior.command.origin == "command" and prior.command.value == norm:
            return Staged(field_name, prior, prior)
        state = FieldState(FieldCommand(
            norm, None if previous is None else normalize(previous), now, context_id
        ))
        self.states[field_name] = state
        return Staged(field_name, state, prior)

    def rollback(self, staged: Staged) -> bool:
        """Undo a failed write only if its command is still the current one.

        Restores the WHOLE prior state: command, confirmation, give-up and
        mismatch. A newer command staged meanwhile is kept (out-of-order
        failures); a failed re-send leaves the original in place. A report
        classified against the staged command (echo/confirmation) is undone
        with it. Returns True if anything was restored.
        """
        current = self.states.get(staged.field)
        if current is None or current.command is not staged.state.command:
            return False
        if staged.prior is staged.state:
            return False
        if staged.prior is None:
            self.states.pop(staged.field, None)
        else:
            self.states[staged.field] = staged.prior
        return True

    def reconcile(self, field_name: str, intended: object, *, now: float) -> None:
        """Expectation-only refresh from OUR intended value (idempotent skip).

        Never call this with a reported value. A person's gesture is not
        erased: if the current expectation came from a gesture, it stays.
        """
        norm = normalize(intended)
        cur = self.states.get(field_name)
        if cur is not None and (cur.command.value == norm or cur.command.origin == "external"):
            return
        self.states[field_name] = FieldState(
            FieldCommand(norm, cur.command.value if cur else None, now, None, origin="reconcile"),
            confirmed=True,  # nothing was sent: no transition
        )

    def note_own(self, field_name: str, value: object, now: float) -> None:
        """Remember a value WE wrote outside this journal (e.g. the legacy fan
        ladder before a policy change), so a late echo of it is ambiguous."""
        self._remember_own(field_name, normalize(value), now)

    def give_up(self, field_name: str) -> None:
        """Bounded verification ended without the head confirming our command.

        The previous value stops being transitional. A report of it is then a
        ``mismatch`` diagnostic, not a person's gesture.
        """
        cur = self.states.get(field_name)
        if cur is not None and not cur.confirmed:
            self.states[field_name] = replace(cur, gave_up=True)

    # -- diagnostics --------------------------------------------------------
    def status(self, field_name: str) -> dict[str, object]:
        cur = self.states.get(field_name)
        if cur is None:
            return {"commanded": None, "origin": None, "confirmed": False,
                    "gave_up": False, "mismatch": False, "reported": None}
        return {
            "commanded": cur.command.value, "origin": cur.command.origin,
            "confirmed": cur.confirmed, "gave_up": cur.gave_up,
            "mismatch": cur.mismatch is not None, "reported": cur.mismatch,
        }

    def mismatches(self) -> dict[str, tuple[str, str]]:
        """Fields whose head kept the old value after verification ended."""
        return {
            name: (st.command.value, st.mismatch)
            for name, st in self.states.items() if st.mismatch is not None
        }

    # -- reports ------------------------------------------------------------
    def classify(
        self, changes: dict[str, object], *, now: float, context_id: str | None = None,
    ) -> dict[str, str]:
        """Classify each changed field, never the whole packet.

        Context alone cannot hide a different value, or a simultaneous change
        to a field we did not command. Updates the field state as it learns:
        an echo confirms our command; a mismatch is recorded as a diagnostic;
        an external value becomes the expectation.
        """
        result = {}
        for name, raw in changes.items():
            value = normalize(raw)
            st = self.states.get(name)
            cmd = st.command if st else None
            if st is not None and value == cmd.value:
                result[name] = "echo"
                if not st.confirmed or st.mismatch is not None:
                    self.states[name] = replace(st, confirmed=True, mismatch=None)
            elif st is not None and value == cmd.previous and self._awaiting(st):
                if self._transitional(st, now):
                    result[name] = "transition"
                else:
                    result[name] = "mismatch"
                    self.states[name] = replace(st, mismatch=value)
            elif self._is_recent_own(name, value, now):
                result[name] = "ambiguous"
            else:
                result[name] = "external"
                self.states[name] = FieldState(
                    FieldCommand(value, cmd.value if cmd else None, now, None, origin="external"),
                    confirmed=True,
                )
        return result

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _awaiting(st: FieldState) -> bool:
        """Our command was sent and the head has not yet confirmed it."""
        return st.command.origin == "command" and not st.confirmed

    def _transitional(self, st: FieldState, now: float) -> bool:
        if st.gave_up:
            return False
        elapsed = now - st.command.sent_at
        return 0 <= elapsed <= self.transition_s

    def _remember_own(self, name: str, value: str, now: float) -> None:
        own = self._own.setdefault(name, deque(maxlen=OWN_HISTORY_MAX))
        own.append((value, now))

    def _is_recent_own(self, name: str, value: str, now: float) -> bool:
        for own_value, sent_at in self._own.get(name, ()):
            elapsed = now - sent_at
            if own_value == value and 0 <= elapsed <= self.history_s:
                return True
        return False
