# Maintained releases

Upstream: dkpnw/ha-mxz-coordinator. License and upstream attribution are retained.
Candidate version: 3.4.1. No automatic deployment or upstream merging.

Merge upstream updates into this maintained source and run both CI workflows.
Test a proposed stock Climate Advisor update by updating its pinned source commit
in the compatibility workflow. Require passing results before tagging a release.
Keep the integration domain and entity unique IDs unchanged.

HACS must download this owned repository rather than the upstream repository.
Updates to Climate Advisor then do not replace MXZ changes. Compatibility with
future versions still requires testing; no design guarantees every update works.

Before installation: back up HA, disable CA automation and MXZ automatic control,
verify physical head states, install the reviewed release, and configure contacts
and valid target ranges before deliberately enabling each zone. Test window pause,
closure with current settings, fan reporting, remote overrides and scheduled writes.
Rollback requires disabling CA or restoring a verified compatible pair.
