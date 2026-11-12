# 3.5.1

Everything since 3.4.2. Recovers the 3.5.1-rc.1 base that ran on Home Assistant
(never committed upstream), adds the local-fork features below, and fixes two
issues found in release review.

**Direction and target classification.** Room direction is preserved across
cycles and persistent head targets are classified, so a setting that sticks on
the head is not misread as a remote press.

**Per-room "tight demand"** (switch, default off). The room votes on the shared
heat/cool mode at its own drift instead of `max(demand_threshold, drift)`, so a
priority room claims the shared compressor at `target + drift` instead of waiting
for the global threshold. While cooling (not eco, not emergency) the head is aimed
one step below the room target (1 °F / 0.5 °C): aimed exactly at target the head
eases off just above it, the room never reaches its release point, and the shared
compressor stays in cool for hours. The vote and release still use the real target.
Expect more changeovers while other rooms heat; widen the room's drift to trade
band width for fewer changeovers.

**Per-room "cool block"** (switch, default off). While on, the room never makes a
comfort cool call; heat and eco protection are unaffected. Independent of the
seasonal cool lockout, so an automation (e.g. a woodstove lockout) can own it.

**Adaptive fan recovers harder.** Medium at 0.75 °F short (was 1.0), High at 1.5 °F
(was 3.0), new Very High at 2.5 °F (heads without Very High fall back to native
Auto). Hysteresis and dwell unchanged. Native-auto rooms are unaffected.

**Cool target near the head floor.** Two-setpoint heads whose firmware enforces a
minimum heat/cool gap: when keeping the unused heat edge would raise the cool
target (e.g. cool 63 °F on a 61 °F-floor head was sent as 61/65 and cooled to
65), a single setpoint is sent and the firmware places the other edge. Its
reported edge is expected, so it is not mistaken for remote input.

**Remote (IR) temperature changes take about 15 s to appear.** A temperature set
on a head's own remote reaches the room's virtual thermostat once it has held for
about 15 seconds, not instantly. Since 3.5.1-rc.1 the coordinator records what it
expects every head to report, so a differing report must persist before it is
treated as a person's input (a transient report is not adopted). Climate
Advisor's override detection then proceeds as before.

**Release-review fixes.**
- Adaptive fan on Celsius installations: the room error is converted to °F before
  the policy thresholds (which are °F deltas) apply. Previously 1 °C short was
  treated as 1 °F short and selected a lower fan stage.
- A failed head write no longer rolls back a newer write's target expectation.
  Expectations carry a per-mode revision and the expectation they replaced; a
  failed write restores only its own, and when overlapping writes all fail the
  expectation returns to the last one that did not fail (never a rejected value).
  Ancestry is kept for every write still in flight, however late its result.
  Previously an older failing write could erase a newer one's expectation, and the
  newer write's echo could then be taken for a remote press.

Verification: 774 MXZ tests passed (HA 2026.2.3 harness locally; the CI matrix
and the CA compatibility workflow run on the PR); ruff clean. The compatibility
workflow now runs against both stock CA 0.7.106 and the installed CA fork
(0.7.107 + sleep offset): 11 passed, 1 expected failure locally against the fork.
The stock-CA IR-override scenario had failed in CI since 3.5.1-rc.1: rc.1 records
an expectation for every head write, so a differing remote setpoint is adopted
only after it holds for 15 s (the designed settle). The test now waits for that
settle; Climate Advisor's override detection and resume are unchanged.
Installation: the code through the tight-cool change ran on Home Assistant before
release (installed by hand, verified byte-identical to commit 059a247). The
release-review fixes had not run on Home Assistant before this release. Install
through HACS from ShawnChesley/ha-mxz-coordinator-release (tag v3.5.1) with the
controlled installation steps in RELEASE-PROCESS.md.

# 3.4.1 release candidate

Adds directional room modes for stock Climate Advisor, persistent window/door
pauses, requested fan reporting, remote-input handling and helper registry stability.
Retains the existing integration domain and entity identities.

Verification at the source commit: 318 MXZ tests passed on HA 2026.10.2;
11 CA compatibility tests passed with one deliberate expected failure documenting
CA without the MXZ window interlock. The interlock scenario passes.
Older HA 2024.12.0 and 2026.2.3 matrices, hassfest, lint and YAML checks passed.
Public-repository HACS validation must pass before release.

Window contacts require configuration. Open-contact delay defaults to 600 seconds;
closure reevaluates current settings immediately. Freeze heat during a window
pause is an explicit option, disabled by default. Targets retain drift semantics.

This source snapshot excludes private operational handoffs and household choices.
The full development history is retained separately. No live deployment occurred.
