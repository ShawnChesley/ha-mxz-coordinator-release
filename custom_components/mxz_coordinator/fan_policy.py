"""Per-room fan decisions, independent of Home Assistant and actuator timing.

Inputs are ROOM-sensor error in Fahrenheit and selected fan modes. Actual blower
speed is deliberately absent: native Auto may legitimately run its blower Low.
The caller classifies external gestures through the command journal first.
``now`` must come from a monotonic clock; a backward step ends a dwell rather
than extending it. A manual hold is never re-asserted here: if the head drifts
off a held speed, that is left to the person (and to verification diagnostics).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class FanPolicy(StrEnum):
    NATIVE_AUTO = "native_auto"
    ADAPTIVE = "adaptive"


class FanStage(StrEnum):
    AUTO = "auto"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very high"  # matched to the head's "Very High" by supported_fan()


@dataclass(frozen=True)
class AdaptiveSettings:
    # Local fork 2026-11-03: tuned for a capacity-limited room (Great Room).
    # Was medium_at 1.0 / high_at 3.0, which held Medium while the room sat
    # 2-3 F short for hours. High now starts at 1.5 F short.
    medium_at: float = 0.75
    high_at: float = 1.5
    # Local fork 2026-11-03: top stage for a room that is well behind. A head
    # without a "Very High" fan mode falls back via supported_fan().
    very_high_at: float = 2.5
    hysteresis: float = 0.3
    boost_dwell_s: float = 600.0

    def __post_init__(self) -> None:
        values = (
            self.medium_at, self.high_at, self.very_high_at,
            self.hysteresis, self.boost_dwell_s,
        )
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Adaptive settings must be finite")
        if not 0 <= self.hysteresis < self.medium_at < self.high_at < self.very_high_at:
            raise ValueError(
                "Require 0 <= hysteresis < medium < high < very high threshold"
            )
        if (
            self.high_at - self.medium_at <= 2 * self.hysteresis
            or self.very_high_at - self.high_at <= 2 * self.hysteresis
        ):
            raise ValueError("Adaptive hysteresis bands must not overlap")

    @property
    def thresholds(self) -> tuple[float, float, float]:
        """Entry thresholds for MEDIUM, HIGH, VERY_HIGH (index = stage - 1)."""
        return (self.medium_at, self.high_at, self.very_high_at)
        if self.boost_dwell_s < 0:
            raise ValueError("Dwell must be nonnegative")


@dataclass(frozen=True)
class FanDecision:
    selected: str | None
    driver: str
    stage: FanStage | None
    diagnostic: str | None = None


DEFAULT_ADAPTIVE_SETTINGS = AdaptiveSettings()
_LADDER = (FanStage.AUTO, FanStage.MEDIUM, FanStage.HIGH, FanStage.VERY_HIGH)
_BOOST = (FanStage.MEDIUM, FanStage.HIGH, FanStage.VERY_HIGH)


@dataclass
class RoomFanPolicy:
    policy: FanPolicy = FanPolicy.NATIVE_AUTO
    hold_speed: str | None = None
    stage: FanStage | None = None
    stage_since: float | None = None
    recovery_mode: str | None = None

    def reset_recovery(self) -> None:
        self.stage = None
        self.stage_since = None
        self.recovery_mode = None

    def external_fan(self, selected: str) -> None:
        """A classified person gesture, never an unclassified head report."""
        self.hold_speed = None if _token(selected) == "auto" else selected
        self.reset_recovery()

    def observe_selected(self, classification: str, selected: str) -> bool:
        """Apply one journal classification of a selected-fan report.

        Only ``external`` is a person's gesture. ``echo``, ``transition``,
        ``ambiguous`` (a delayed report of one of our own recent commands, such
        as an earlier Auto) and ``mismatch`` (the head kept its old speed after
        verification gave up) never change a hold, the policy or the stage; a
        mismatch is a diagnostic for the adapter. Returns True if a gesture was
        adopted.
        """
        if classification != "external":
            return False
        self.external_fan(selected)
        return True

    def release(self) -> None:
        self.hold_speed = None
        self.reset_recovery()

    def set_policy(self, policy: FanPolicy) -> None:
        self.policy = FanPolicy(policy)
        self.reset_recovery()  # changing normal policy does not release a hold

    def decide(
        self,
        *,
        mode: str,
        error_f: float | None,
        now: float,
        allowed: bool = True,
        satisfied: bool = False,
        settings: AdaptiveSettings = DEFAULT_ADAPTIVE_SETTINGS,
    ) -> FanDecision:
        """Choose an intended selected mode; never starts or enables a head.

        ``allowed`` is false for Off, pauses, unavailable heads and protections.
        Existing on/off, dry, drift and shared-mode logic belongs to the caller.
        Error is target-room in Heat and room-target in Cool, without rounding.
        """
        if not math.isfinite(now):
            raise ValueError("Clock must be finite")
        if not allowed or mode not in ("heat", "cool", "fan_only"):
            self.reset_recovery()
            return FanDecision(None, "hold" if self.hold_speed else "head", None)
        if self.hold_speed is not None:
            self.reset_recovery()
            return FanDecision(None, "hold", None)
        if self.policy == FanPolicy.NATIVE_AUTO or satisfied or mode == "fan_only":
            self.reset_recovery()
            return FanDecision("auto", "head", FanStage.AUTO)
        if error_f is None or not math.isfinite(error_f):
            self.reset_recovery()
            return FanDecision("auto", "head", FanStage.AUTO, "room_sensor_unavailable")

        previous = self.stage
        th = settings.thresholds
        if self.recovery_mode != mode or previous is None:
            # Fresh decision: no hysteresis (MEDIUM entry is inclusive).
            level = 0
            if error_f >= th[0]:
                level = 1
            while level < len(th) and error_f > th[level]:
                level += 1
        else:
            # Step up only past threshold + hysteresis; step down only below
            # threshold - hysteresis. Multi-stage jumps are allowed.
            level = _LADDER.index(previous)
            start = level
            while level < len(th) and error_f > th[level] + settings.hysteresis:
                level += 1
            if level == start:
                while level > 0 and error_f < th[level - 1] - settings.hysteresis:
                    level -= 1
        wanted = _LADDER[level]
        # Dwell only BETWEEN boost stages. Near-target Auto and safety bypass it.
        if (
            self.recovery_mode == mode
            and previous in _BOOST
            and wanted in _BOOST
            and self.stage_since is not None
            and 0 <= now - self.stage_since < settings.boost_dwell_s
        ):
            wanted = previous
        if wanted != previous or self.recovery_mode != mode:
            self.stage_since = now
        self.stage = wanted
        self.recovery_mode = mode
        return FanDecision(wanted.value, "head" if wanted == FanStage.AUTO else "coordinator", wanted)


def _token(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def supported_fan(decision: FanDecision, modes: list[str]) -> FanDecision:
    """Return the head's own advertised spelling; unsupported boost falls back
    to native Auto. Matching ignores case and spacing ("Very High")."""
    if decision.selected is None:
        return decision
    advertised = {_token(m): m for m in modes}
    exact = advertised.get(_token(decision.selected))
    if exact is not None:
        return FanDecision(exact, decision.driver, decision.stage, decision.diagnostic)
    auto = advertised.get("auto")
    return FanDecision(
        auto, "head", FanStage.AUTO if auto is not None else None,
        "fan_mode_unsupported",
    )
