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

    def command(self, mode: str, value: float, *equivalents: float) -> None:
        """Remember a successful/intended write, including known native rounding."""
        self.commanded[mode] = (value, *equivalents)
        self.candidate = None

    def expect(self, mode: str, value: float, *equivalents: float) -> None:
        """Refresh what we expect a head to report for ``mode`` without a write.

        Unlike :meth:`command`, a pending candidate is kept: reconciling our own
        expectation must not erase a genuine remote press that is still settling.
        """
        self.commanded[mode] = (value, *equivalents)

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
