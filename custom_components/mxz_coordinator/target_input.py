"""Value-based target echoes and stable external setpoint candidates.

Command expectations do not expire. The timer measures persistence of a DIFFERENT
reported value, never time since the last command. State is local to one head.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

TARGET_SETTLE_S = 15.0


@dataclass(frozen=True)
class TargetCandidate:
    mode: str
    value: float
    since: float
    generation: int


@dataclass
class TargetInput:
    commanded: dict[str, tuple[float, ...]] = field(default_factory=dict)
    candidate: TargetCandidate | None = None
    # Per-mode revision of ``commanded``: every command()/expect() bumps it, so a
    # failed write can tell whether a newer write has replaced its expectation.
    revisions: dict[str, int] = field(default_factory=dict)
    _next_revision: int = 0

    def _stamp(self, mode: str) -> int:
        self._next_revision += 1
        self.revisions[mode] = self._next_revision
        return self._next_revision

    def command(self, mode: str, value: float, *equivalents: float) -> int:
        """Remember a successful/intended write, including known native rounding."""
        self.commanded[mode] = (value, *equivalents)
        self.candidate = None
        return self._stamp(mode)

    def expect(self, mode: str, value: float, *equivalents: float) -> int:
        """Refresh what we expect a head to report for ``mode`` without a write.

        Unlike :meth:`command`, a pending candidate is kept: reconciling our own
        expectation must not erase a genuine remote press that is still settling.
        """
        self.commanded[mode] = (value, *equivalents)
        return self._stamp(mode)

    def rollback(self, mode: str, revision: int, prior: tuple[float, ...] | None) -> bool:
        """Undo a failed write's expectation for ``mode`` — only if it is still ours.

        A newer command/expectation recorded while the failed write was in flight
        has a different revision and is kept. Returns True when rolled back.
        """
        if self.revisions.get(mode) != revision:
            return False
        if prior is None:
            self.commanded.pop(mode, None)
            self.revisions.pop(mode, None)
        else:
            self.commanded[mode] = prior
            self._stamp(mode)
        return True

    def matches(self, mode: str, value: float | None) -> bool:
        return value is not None and any(
            math.isclose(value, expected, abs_tol=1e-6, rel_tol=0)
            for expected in self.commanded.get(mode, ())
        )

    def observe(self, mode: str, value: float, now: float, generation: int) -> str:
        if not math.isfinite(value):
            self.candidate = None
            return "invalid"
        if self.matches(mode, value):
            self.candidate = None
            return "echo"
        if mode not in self.commanded:
            self.candidate = None
            return "untracked"
        candidate = self.candidate
        if (
            candidate is None or candidate.mode != mode
            or candidate.value != value or candidate.generation != generation
        ):
            self.candidate = TargetCandidate(mode, value, now, generation)
            return "candidate"
        return "stable" if now - candidate.since >= TARGET_SETTLE_S else "candidate"
