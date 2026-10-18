# Climate Advisor compatibility tests

Stock CA's real engine and full integration drive forked MXZ thermostats inside
one Home Assistant test instance. No live HA is contacted.

Install the matching HA test environment, obtain stock CA v0.7.106, then run:

    python tools/run_ca_compat.py /absolute/path/to/ClimateAdvisor

The runner copies all required helpers and integrations into a temporary folder.
Fresh local run: 11 passes and one deliberate expected failure on HA 2026.10.2
using the official 0.13.373 harness. The expected failure documents CA alone heating during planned
window hours; the companion passing test configures MXZ's contact and proves it
pauses heating and resumes from current settings on closure. Registering CA's
notify.send_email service fixes the reported full-integration teardown error.

The full MXZ suite passes 318 tests, including the new registry-persistence test.
The exact-dependency Linux workflow passed on GitHub (run 39140255837), including
HA 2026.10.2 and stock CA 0.7.106. The older HA matrix also passed. HACS validation
requires a public repository and remains pending.

Direct engines settle ten-second acknowledgements and explicitly clean up at
teardown. The runner no longer permits lingering timers globally. It selects
assertion scenarios and the full override test, excluding the old assertion-free
test_probe in the constants/helper module.
