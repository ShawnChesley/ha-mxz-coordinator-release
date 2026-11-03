# Unreleased (on 3.5.1-rc.1)

**Per-room "tight demand" (local fork).** New per-zone config switch
`<room> tight demand`, default off. When on, the room's vote on the shared
heat/cool mode uses its own drift instead of `max(demand_threshold, drift)`, so a
priority room can claim the shared compressor at `target + drift` rather than
waiting for the global threshold. Intended for a bedroom held to a narrow
overnight band. Expect more changeovers while other rooms are heating; widen that
room's drift to trade band width for fewer changeovers.

Verification: 711 MXZ tests passed on HA 2026.2.3, ruff clean. The 3.5.1-rc.1 base
was recovered from the copy installed on Home Assistant (it was never committed);
rc.1's own new tests were not recoverable.

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
