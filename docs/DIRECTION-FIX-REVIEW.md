# Direction and target-echo fix — review candidate

Prepared October 22, 2026 on `fix/head-fan-direction`, based on `v3.4.2`
(`d5e1332`). Local changes only: no push, tag, release, or HA installation.

## Resulting behavior

- A fan-only report never consults or overwrites the remembered IR temperature.
- A temperature-only adjustment preserves Heat-only/Cool-only direction.
- Remote ON in an already-allowed mode preserves direction. An explicit opposite
  heat/cool or Auto transition reopens Heat/Cool, without setting shared mode.
- A report matching the last commanded target is a target-field echo at any age.
  It changes neither the remote record nor input provenance.
- The journal also recognizes the head's advertised native-step/display
  representation of that command, calculated from entity metadata. It does not
  learn aliases from arbitrary observations or accept a broad tolerance band.
- A different same-mode target becomes a candidate. It must remain reported for
  15 seconds before adoption. The clock starts at the differing report, not the
  last command. Another value restarts the clock; returning to the commanded
  value cancels it. Repeated identical reports do not restart it.
- A genuine service override cancels a candidate, even if it repeats the existing
  canonical target. Disconnects, sensor loss, pause/Off/mode/protection commands,
  canonical input changes and shutdown also cancel pending work.
- Ordinary same-mode target rewrites wait during a candidate so the Coordinator
  cannot erase a real remote press before judging it. Fan control continues.
  New emergency plans, window pauses, Off and shared-mode changes never wait.
- Successful adoption updates the target and remembered remote value once,
  records `head_manual` / `target`, and preserves direction.

Explicit remote **power/mode transitions retain their existing intent path**.
They are not delayed by the same-mode setpoint filter. This preserves the existing
window behavior: remember an explicit new remote request while immediately
parking a paused head again. It does not grant the head direct control of the
shared outdoor mode. The manual-fan latch remains independent.

Command expectations and candidates are runtime state, not restored timers.
When no target has yet been commanded for a head/mode, the existing startup/IR
classification remains in use. Failed target writes restore the prior expectation.

## Source of 71.5 F — live diagnostic capture

At approximately 4:46 PM Pacific on October 22, the existing diagnostic firmware
captured a direct Auto write followed by the Coordinator's Low write.

Both outgoing set requests contained active-target byte `AC`; subsequent head
settings replies also contained `AC`. The pinned packet library decodes this as
`(0xAC - 128) / 2 = 22.0 C`. Its encoder rounds to native 0.5 C steps:

`72 F -> 22.222... C -> 22.0 C on the wire -> 71.6 F -> HA display 71.5 F`.

The adapter first publishes the unquantized 22.22 C requested value optimistically.
The wire value is already 22.0 C before the head receives it. This is evidence of
native quantization/display differences, not evidence that an old IR setting is
being replayed. Correcting optimistic firmware reporting could remove the display
oscillation, but cannot make the native protocol represent exactly 72 F.

Sources inspected: `muart-group/itp-packet` commit
`3438d83b7087a6f028ce20af654eda66f9a060bc`, `src/itp_utils.h` and
`src/packets/set.cpp`; component `v2.0.1` advertises a native 0.5 C step.

After the diagnostic fan write reproduced the existing direction bug, Great Room
was restored to Heat-only. Great Room fan auto and Coordinator enable were both
verified On. The diagnostic log stream was stopped. Candidate code remains local.

## Validation

- Original ten regressions fail against unmodified v3.4.2; they pass with the fix.
- Return-to-72 sweep: every integer second from +6 to +60, plus +10.1; known native
  71.5 F reports do not change target, direction, remote record or input generation.
- Four-second non-equivalent blip sweep: start offsets +1 through +60 seconds.
- Real changes: offsets 0 through 24 hours in pure tests, selected offsets +1
  through +60 in real HA/head/timer tests; adoption waits a full 15 seconds.
- Service overrides, candidate replacement and fan changes during a candidate;
  both stale and current remembered remote temperatures.
- Off, mode changes, unavailable head, missing room sensor, window pauses and
  emergency commands; a NEW emergency plan cancels a hold even if its target
  happens to equal the prior normal target.
- Native Celsius quantization/display alias tested using the real entity metadata.
- Complete final MXZ suite: 708 passes on both Python 3.13 / HA 2026.2.3 and
  Python 3.14.7 / HA 2026.10.2. This includes 186 additional exact-boundary
  cases at 14.999, 15.000 and 15.001 seconds, at offsets 0–60 and 24 hours.
- Stock Climate Advisor 0.7.106 on HA 2026.10.2: 11 passes, one documented
  expected failure. Ruff, whitespace and installed-dependency checks pass.

The live About page confirms Core **2026.10.2**, Supervisor 2026.10.3, OS 18.3.
HA 2026.10.2 and stock Climate Advisor validation now pass locally. Cryptography
50.0.1 was rebuilt against matching local OpenSSL 3.5.7 to resolve the import
failure; dependency pins remain unchanged. No live acceptance test of the
candidate has occurred because it has not been installed.

## Scope and remaining gates

No fan-policy entities, migration, Adaptive stages, dwell/restart changes,
schedules, idle policy, shared arbitration or priority changes are included.
The full per-field command journal remains mandatory for the fan-policy release;
this target-only journal is its reusable starting point. Existing mode/fan
context/timeout matching has not been redesigned.

Before release: review the expanded target-filter scope, pass required CI, then
approve publication/installation. Installed-version and CA compatibility tests
have passed locally.
After installation approval, use the existing backup and rollout procedure.

Live acceptance:

1. Fan change in Heat-only: target, direction, enable, shared mode and remembered
   remote heat value stay unchanged through subsequent reports.
2. Remote setpoint change in Heat-only: adopt a persistent change after 15 seconds
   without changing direction; a brief/cancelled change produces no input.
3. Remote mode change: preserve existing behavior without directly changing shared
   mode. Window pause still wins.
4. Restore Great Room fan auto as in v3.4.2 after testing.

## Follow-up review resolution

The early return in `_apply_head` defers same-mode setpoint rewrites only.
Fan control is called afterward by `_apply`; vane control uses `async_apply_vane`.
The pending-target test verifies real fan and vane service writes continue.
Off/mode/safety actions fail `can_wait`, cancel the candidate and proceed.

Each plan sensor zone now exposes `target_equivalents` with `target` and
`equivalents`, computed from current entity metadata even before the first
command. `target_commands` separately records per-mode command expectations.
For Great Room at 72 F, verify `target_equivalents` shows target 72 and equivalents
[71.5] BEFORE any live fan test; inspect the heat command expectation too once a
command has occurred. Missing metadata produces an empty list, never a learned
alias. A persistent unrecognized 71.5 F report reaches adoption after 15 seconds;
the existing 1 F rounding policy maps it to 72, so the canonical target stays put.
The missing-metadata test verifies the remote record changes only after settling.
No automatic retry of a previously recorded command's equivalents was added.

Adoption retains Python's existing half-to-even rounding: with a 1 F step,
71.5 rounds to 72 and 70.5 to 70. This change does not alter that policy.

All changes are local. Hosted CI, release publication, HA backup/installation and
live acceptance remain outstanding. No live fan write or firmware change occurred.
