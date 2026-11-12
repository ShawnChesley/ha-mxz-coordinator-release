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


@dataclass(frozen=True)
class _Stamp:
    """What one expectation replaced: lets a failed write restore its ancestor."""

    mode: str
    prior: tuple[float, ...] | None
    prior_revision: int | None


@dataclass
class TargetInput:
    commanded: dict[str, tuple[float, ...]] = field(default_factory=dict)
    candidate: TargetCandidate | None = None
    # Per-mode revision of ``commanded``: every command()/expect() bumps it, so a
    # failed write can tell whether a newer write has replaced its expectation.
    revisions: dict[str, int] = field(default_factory=dict)
    _next_revision: int = 0
    # Ancestry, so overlapping writes that BOTH fail roll back past each other to
    # the last expectation that did not fail. Only what a rollback can still read
    # is kept: stamps of in-flight writes (never dropped, however old) and failed
    # stamps that an in-flight newer write may have to walk past.
    _stamps: dict[int, _Stamp] = field(default_factory=dict)
    _pending: set[int] = field(default_factory=set)
    _failed: set[int] = field(default_factory=set)

    def _stamp(self, mode: str, prior: tuple[float, ...] | None) -> int:
        self._next_revision += 1
        rev = self._next_revision
        self._stamps[rev] = _Stamp(mode, prior, self.revisions.get(mode))
        self.revisions[mode] = rev
        self._collect(keep=rev)
        return rev

    def _collect(self, keep: int | None = None) -> None:
        """Drop ancestry no rollback can read: not in flight, and not a failed
        stamp that some newer in-flight write could walk past."""
        newest_pending = max(self._pending, default=None)
        for rev in list(self._stamps):
            if rev == keep or rev in self._pending:
                continue
            if rev in self._failed and newest_pending is not None and newest_pending > rev:
                continue
            self._stamps.pop(rev)
            self._failed.discard(rev)

    def command(self, mode: str, value: float, *equivalents: float) -> int:
        """Record a write's expectation; it stays in flight until settle()/rollback()."""
        prior = self.commanded.get(mode)
        self.commanded[mode] = (value, *equivalents)
        self.candidate = None
        rev = self._stamp(mode, prior)
        self._pending.add(rev)
        return rev

    def expect(self, mode: str, value: float, *equivalents: float) -> int:
        """Refresh what we expect a head to report for ``mode`` without a write.

        Unlike :meth:`command`, a pending candidate is kept: reconciling our own
        expectation must not erase a genuine remote press that is still settling.
        Not in flight unless :meth:`hold` is called (a write that also sets it).
        """
        prior = self.commanded.get(mode)
        self.commanded[mode] = (value, *equivalents)
        return self._stamp(mode, prior)

    def hold(self, revision: int) -> None:
        """Mark an expectation as belonging to an in-flight write."""
        if revision in self._stamps:
            self._pending.add(revision)

    def settle(self, revision: int) -> None:
        """The write that created ``revision`` succeeded: it is a valid ancestor."""
        self._pending.discard(revision)
        self._stamps.pop(revision, None)
        self._failed.discard(revision)
        self._collect()

    def rollback(self, mode: str, revision: int) -> bool:
        """Undo a failed write's expectation for ``mode`` — only if it is still current.

        A newer expectation recorded while this write was in flight is kept; if that
        newer write later fails too, its rollback skips this failed one and restores
        the nearest expectation that did not fail. Returns True when rolled back.
        """
        self._pending.discard(revision)
        if self.revisions.get(mode) != revision:
            if revision in self._stamps:
                self._failed.add(revision)
            self._collect()
            return False
        stamp = self._stamps.get(revision)
        while stamp is not None and stamp.prior_revision in self._failed:
            stamp = self._stamps.get(stamp.prior_revision)
        prior = stamp.prior if stamp is not None else None
        prior_revision = stamp.prior_revision if stamp is not None else None
        if prior is None:
            self.commanded.pop(mode, None)
            self.revisions.pop(mode, None)
        else:
            self.commanded[mode] = prior
            if prior_revision is None:
                self.revisions.pop(mode, None)
            else:
                self.revisions[mode] = prior_revision
        self._stamps.pop(revision, None)
        self._collect()
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
