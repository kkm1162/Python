#!/usr/bin/env bash
# O-RAN M-Plane 3.1.4.2 — Retrieval of O-RU's information elements (with filter)
set -u
set -o pipefail

TESTID="3142"
CONFIG=""
while [ $# -gt 0 ]; do
	case "$1" in
	--config)
		CONFIG="${2:-}"
		shift 2
		;;
	--)
		shift
		break
		;;
	*)
		echo "[ERROR] unknown argument: $1"
		exit 2
		;;
	esac
done

if [[ -z "${CONFIG}" ]]; then
	echo "[ERROR] --config <path> required"
	exit 2
fi
if [[ ! -f "$CONFIG" ]]; then
	echo "[ERROR] config file not found: $CONFIG"
	exit 2
fi

USER=$(jq -r '.["management-configurations"]["NETCONF-ID"] // empty' "$CONFIG")
PASSWORD=$(jq -r '.["management-configurations"]["NETCONF-PW"] // empty' "$CONFIG")
ALLOWED_IP=$(jq -r '.["management-configurations"]["SERVER-IP"] // empty' "$CONFIG")
LOCAL_IP=$(jq -r '.["management-configurations"]["LOCAL-IP"] // empty' "$CONFIG")
NETCONF_PORT=$(jq -r '.["management-configurations"]["PORT"] // empty' "$CONFIG")
PRODUCT=$(jq -r '.["management-configurations"]["PRODUCT-CODE"] // empty' "$CONFIG")

LISTEN_PORT="${CALLHOME_PORT:-4334}"
NETCONF_TMP="${NETCONF_TMP:-/var/tmp/netconf_tmp}"

echo "[INFO] USER=$USER, ALLOWED_IP=$ALLOWED_IP, LOCAL_IP=$LOCAL_IP, LISTEN_PORT=$LISTEN_PORT (Call Home), NETCONF_PORT=$NETCONF_PORT (JSON PORT), PRODUCT=$PRODUCT"

LOG_BASE="${LOG_PATH:-${CONFORMANCE_REMOTE_DIR:-/var/tmp/conformance}/logs}"
LOG_BASE="${LOG_BASE%/}"
LOG_DIR="${LOG_BASE}/${PRODUCT:-_unknown_}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/CONF_${TESTID}_$(date +'%y%m%d_%H-%M-%S').log"
: >"$LOG"
chmod 0644 "$LOG" 2>/dev/null || true

send_cmd() {
	local cmd="$*"
	echo "Client SENT : $cmd" >>"$LOG" 2>&1
	set +u
	local _wfd="${NP2[1]:-}"
	set -u
	[[ -n "${_wfd}" ]] || return 0
	echo "$cmd" >&"${_wfd}" 2>/dev/null || true
}

test_fail() {
	echo "[FAIL] $*"
}

COPROC_READY=0
NETOPEER_COPROC_PID=""
cleanup() {
	if [[ "$COPROC_READY" == "1" ]]; then
		send_cmd "disconnect" 2>/dev/null || true
		sleep 1 || true
		exec 3>&- 2>/dev/null || true
	fi
	if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
		sudo kill -15 "$NETOPEER_COPROC_PID" 2>/dev/null || true
		sleep 1 || true
		sudo kill -9 "$NETOPEER_COPROC_PID" 2>/dev/null || true
	fi
	sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -j DROP >/dev/null 2>&1 || true
	sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT >/dev/null 2>&1 || true
	return 0
}
trap cleanup EXIT INT TERM HUP

sudo fuser -k "${LISTEN_PORT}/tcp" 2>/dev/null || true
sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -j DROP 2>/dev/null || true
sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT 2>/dev/null || true
sleep 1

sudo iptables -A INPUT -p tcp --dport "$LISTEN_PORT" -j DROP
sudo iptables -I INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT
sleep 3

coproc NP2 {
	setsid stdbuf -oL sshpass -p "$PASSWORD" netopeer2-cli 2>&1
} >>"$LOG" 2>&1
NETOPEER_COPROC_PID="${NP2_PID:-$!}"
exec 3>&"${NP2[1]}"
COPROC_READY=1

send_cmd "verb 3"
send_cmd "knownhosts --mode skip"
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout 300"

RESULT1="NOK"
PAT_ACCEPT="Accepted a connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
for _w in $(seq 1 300); do
	if grep -a -F "$PAT_ACCEPT" "$LOG" >/dev/null 2>&1; then
		RESULT1="OK"
		break
	fi
	sleep 0.2
done

echo "STEP 1. Criteria : The Netconf Client receive the CallHome from ORU"
echo "STEP 1. CallHome : $RESULT1"
if [[ "$RESULT1" != "OK" ]]; then
	test_fail "Call Home"
	exit 1
fi

RESULT2="NOK"
for _w in $(seq 1 120); do
	if grep -a -F "Authentication successful" "$LOG" >/dev/null 2>&1; then
		RESULT2="OK"
		break
	fi
	sleep 0.2
done

echo "[$RESULT2] STEP 2. Successfully login with the correct username and password ($USER / ***)"
if [[ "$RESULT2" != "OK" ]]; then
	test_fail "login"
	exit 1
fi

sleep 3

GET_FILTER_RPC="${NETCONF_TMP}/get/get_filter_rpc.xml"
GET_FILTER_OUT="${NETCONF_TMP}/get/get_filter_out.xml"
mkdir -p "${NETCONF_TMP}/get"
cat > "$GET_FILTER_RPC" <<'EORPC'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <filter type="subtree">
    <user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0">
      <static-low-level-rx-endpoints/>
    </user-plane-configuration>
  </filter>
</get>
EORPC

rm -f "$GET_FILTER_OUT"
send_cmd "user-rpc --content $GET_FILTER_RPC --out $GET_FILTER_OUT"

RESULT3="NOK"
for _w in $(seq 1 120); do
	if [[ -f "$GET_FILTER_OUT" ]]; then
		if grep -aq "</data>" "$GET_FILTER_OUT" 2>/dev/null \
			|| grep -aqE '^[[:space:]]*OK[[:space:]]*$' "$GET_FILTER_OUT" 2>/dev/null; then
			RESULT3="OK"
			break
		fi
	fi
	sleep 0.5
done

echo "STEP 3. Criteria : Check filter option normality"
if [[ "$RESULT3" != "OK" ]]; then
	if [[ -f "$GET_FILTER_OUT" ]]; then
		echo "[INFO] output file exists but no </data> found"
	else
		echo "[INFO] output file not created: $GET_FILTER_OUT"
	fi
	test_fail "get-with-filter response"
	exit 1
fi

mapfile -t MATCHED < <(xmlstarlet sel -t \
	-m "/*[local-name()='data']/*[local-name()='user-plane-configuration']/*[local-name() = 'static-low-level-rx-endpoints']" \
	-v "local-name()" -n "$GET_FILTER_OUT" 2>/dev/null) || true

if (( ${#MATCHED[@]} < 1 )); then
	echo "STEP 3. Filter : NOK"
	echo "	--- target node (static-low-level-rx-endpoints) not detected."
	test_fail "filter target node"
	exit 1
fi

mapfile -t OTHERS < <(xmlstarlet sel -t \
	-m "/*[local-name()='data']/*[local-name()='user-plane-configuration']/*[local-name() != 'static-low-level-rx-endpoints']" \
	-v "local-name()" -n "$GET_FILTER_OUT" 2>/dev/null) || true

if (( ${#OTHERS[@]} > 0 )); then
	echo "	--- Unwanted Nodes Detected ---"
	for check in "${!OTHERS[@]}"; do
		echo "	- ${OTHERS[$check]}"
	done
	echo "	-------------------------------"
	echo "STEP 3. Filter : NOK"
	test_fail "filter unwanted nodes"
	exit 1
fi

echo "STEP 3. Filter : OK"

echo "[INFO] 3.1.4.2 retrieval with filter completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
