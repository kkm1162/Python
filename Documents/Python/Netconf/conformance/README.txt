Local conformance shell scripts (*.sh) are uploaded to the Linux host under:
  /var/tmp/conformance/

The GUI merges Settings ORU fields into JSON and uploads it as:
  /var/tmp/conformance/_gui_management_config.json

Scripts must use only paths under /var/tmp/ for on-host work files.
Recommended layout (matches miniDU_callhome + your explorer view):
  /var/tmp/netconf_tmp/edit
  /var/tmp/netconf_tmp/get
  /var/tmp/netconf_tmp/*.xml
  /var/tmp/netconf_tmp/netconf_control.fifo
  /var/tmp/netconf_tmp/netconf_cmd.lock
  /var/tmp/netconf_tmp/CLI-LOG.log

Optional netopeer logs: keep LOG_PATH under /var/tmp (e.g. /var/tmp/log/<PRODUCT>) so smoke scripts can tail safely.

The GUI lists O-RAN M-Plane 3.1.x tests in spec order (see CONFORMANCE_SPEC_ROWS in conformance_manifest.py); only scripts present locally are shown.
3.1.8.0 is not listed — before any 3.1.8.1–3.1.8.6 run, conformance_3180_init_user.sh runs automatically as the prep step.
Use CONFORMANCE_TESTS for extra scripts (e.g. RU-smoke) still uploaded on sync but not part of the 3.1 table list.
