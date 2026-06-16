#!/usr/bin/env bash
# RU / NETCONF smoke: uses ONLY paths under /var/tmp (conformance + netconf_tmp).
# Reads merged GUI JSON (--config). LOG_PATH from env or JSON must stay under /var/tmp if set.
set -euo pipefail

CONF_ROOT="/var/tmp/conformance"
NETCONF_TMP="/var/tmp/netconf_tmp"
CFG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CFG="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

log_cli() { printf '%s\n' "$*" >> "${NETCONF_TMP}/CLI-LOG.log"; }

if [[ -z "${CFG}" || ! -f "${CFG}" ]]; then
  echo "[NOK] STEP 0. Config : NOK (missing --config or file)"
  exit 1
fi

mkdir -p "${NETCONF_TMP}/edit" "${NETCONF_TMP}/get" "${CONF_ROOT}"

MC="$(jq -c '.["management-configurations"] // {}' "${CFG}")" || { echo "[NOK] STEP 0. JSON : NOK"; exit 1; }

NETCONF_ID="$(jq -r '.["NETCONF-ID"] // empty' <<<"${MC}")"
SERVER_IP="$(jq -r '.["SERVER-IP"] // empty' <<<"${MC}")"
PORT="$(jq -r '.["PORT"] // empty' <<<"${MC}")"
PRODUCT="$(jq -r '.["PRODUCT-CODE"] // empty' <<<"${MC}")"

echo "[RUN_CTX] NETCONF_TMP=${NETCONF_TMP} CONF_ROOT=${CONF_ROOT}"
echo "[RUN_CTX] NETCONF_ID=${NETCONF_ID} SERVER_IP=${SERVER_IP} PORT=${PORT} PRODUCT=${PRODUCT}"

log_cli "==== $(date -Iseconds) conformance_ru_netconf_tmp ===="
log_cli "NETCONF_TMP=${NETCONF_TMP} CONF_ROOT=${CONF_ROOT}"
log_cli "management-configurations (passwords omitted): $(jq -c '.["management-configurations"] | if . then .["NETCONF-PW"]=null | .["CLI-PW"]=null else . end' "${CFG}")"

# Align supervision reset XML with miniDU_callhome (same path family as /var/tmp/netconf_tmp)
SUP_RESET="${NETCONF_TMP}/supervision_reset.xml"
cat > "${SUP_RESET}" <<'XMLEOF'
<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>
XMLEOF
cp -f "${SUP_RESET}" "${NETCONF_TMP}/watchdog.xml" 2>/dev/null || cat "${SUP_RESET}" > "${NETCONF_TMP}/watchdog.xml"

if [[ -p "${NETCONF_TMP}/netconf_control.fifo" ]]; then
  echo "[OK] STEP 1. netconf_control.fifo : OK (miniDU_callhome session likely active)"
  log_cli "FIFO present: ${NETCONF_TMP}/netconf_control.fifo"
else
  echo "[INFO] STEP 1. netconf_control.fifo : missing (Start miniDU_callhome on this host for live RPC)"
  log_cli "FIFO missing: ${NETCONF_TMP}/netconf_control.fifo"
fi

# Optional: last lines of netopeer log under LOG_PATH (must be under /var/tmp)
LP="${LOG_PATH:-}"
if [[ -n "${LP}" && "${LP}" == /var/tmp/* ]]; then
  latest="$(ls -1t "${LP}/${PRODUCT}"_*.log 2>/dev/null | head -n 1 || true)"
  if [[ -n "${latest}" && -f "${latest}" ]]; then
    echo "[INFO] tail ${latest} (last 5 lines)"
    tail -n 5 "${latest}" || true
    log_cli "log tail ${latest}"
  fi
fi

echo "[OK] STEP 2. Paths under /var/tmp : OK"
echo "[OK] STEP 3. Config snapshot : OK"
exit 0
