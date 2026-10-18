# Fork changes for review

This candidate is not installed and is not yet a published HACS release.

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

Temperature semantics are unchanged: target 64 with drift 1 starts heat below 63
and stops at 64. CA's floor is therefore a target, not an exact compressor start
temperature. All CA bands must fit the thermostat minimum and maximum.
Invalid scheduled targets can leave the previous target active.

See RELEASE-PROCESS.md for update ownership, test/release gates, rollout order,
and the correction to rollback. The private staging repository is ShawnChesley/ha-mxz-coordinator-release.
