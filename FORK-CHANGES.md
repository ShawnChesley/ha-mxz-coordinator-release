# Fork changes for review

Released as 3.5.1 (tag v3.5.1) from ShawnChesley/ha-mxz-coordinator-release for HACS.

* Room thermostats support off, heat, cool and the original single-target heat_cool.
  Direction constrains normal demand; it never gives a room authority to bypass
  arbitration for the shared outdoor unit. Existing eco/emergency exceptions remain.
* A configured room contact pauses heating/cooling after 600 seconds by default.
  New commands are recorded during the pause and evaluated on closure, rather than
  restoring an afternoon target. Closure resumes immediately. Open time and pause
  survive restart; unknown contacts retain the prior room state. With no prior
  state, unknown contacts are treated as closed and reported as unknown internally.
* Heating through an open window requires the explicit per-room freeze-protection
  option, off by default, and produces a notification.
* The thermostat's fan_mode is the requested setting; the physical blower speed is
  exposed as fan_actual. Automatic ramps must not create manual fan overrides.
* IR temperature/ON input reopens heat_cool and is adopted as manual input. Stock
  CA's override confirmation and hold behavior are covered by compatibility tests.
* Direction and freeze-protection helper registry entries are retained on reload.
* Per-room "cool block" switch (default off): no comfort cooling for that room
  while on; heat and eco protection unaffected; independent of the seasonal
  cool lockout. Intended for automation (woodstove lockout).
* Adaptive fan recovery thresholds lowered: Medium at 0.75 °F short (was 1.0),
  High at 1.5 °F (was 3.0), new Very High stage at 2.5 °F; hysteresis and
  dwell unchanged.
* Per-room "tight demand" switch (default off). When on, the room votes on the
  shared heat/cool mode at its own drift instead of max(demand_threshold, drift),
  so a priority room can claim the shared compressor at target + drift rather than
  waiting for the global threshold. Expect more changeovers while other rooms heat;
  widening that room's drift trades band width for fewer changeovers.
  While a tight room cools (not eco/emergency) its head is aimed one step below
  the room target (1 °F / 0.5 °C) so it reaches the release point.
  When another room is waiting for the opposite shared mode ("contended"),
  the bias deepens to two steps and the adaptive fan is floored at the top
  stage (High during quiet hours) until the room releases.
* Cool target near a two-setpoint head's floor is sent as a single setpoint when
  the firmware's minimum gap would otherwise raise it.
* Adaptive fan error is converted to °F on Celsius installations.
* A failed head write rolls back only its own target expectation (per-mode
  revision), never a newer write's; if overlapping writes all fail, the
  expectation returns to the last one that did not fail.

Temperature semantics are unchanged: target 64 with drift 1 starts heat below 63
and stops at 64. CA's floor is therefore a target, not an exact compressor start
temperature. All CA bands must fit the thermostat minimum and maximum.
Invalid scheduled targets can leave the previous target active.

See RELEASE-PROCESS.md for update ownership, test/release gates, rollout order,
and the correction to rollback. The private staging repository is ShawnChesley/ha-mxz-coordinator-release.
