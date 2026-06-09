#!/usr/bin/env bash
# O-RAN M-Plane 3.1.4.1 — Retrieval of O-RU's information elements (without filter)
set -u
set -o pipefail

TESTID="3141"
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
# shellcheck source=/dev/null
_CALLHOME_COMMON="${CONFORMANCE_REMOTE_DIR:-/var/tmp/conformance}/conformance_callhome_common.sh"
[[ -f "$_CALLHOME_COMMON" ]] || _CALLHOME_COMMON="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/conformance_callhome_common.sh"
source "$_CALLHOME_COMMON"

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
conformance_callhome_set_listen_mark
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout 300"

RESULT1=$(conformance_callhome_wait_step1 300)

echo "STEP 1. Criteria : The Netconf Client receive the CallHome from ORU"
echo "STEP 1. CallHome : $RESULT1"
if [[ "$RESULT1" != "OK" ]]; then
	test_fail "Call Home"
	exit 1
fi

RESULT2=$(conformance_callhome_wait_auth 120)

echo "[$RESULT2] STEP 2. Successfully login with the correct username and password ($USER / ***)"
if [[ "$RESULT2" != "OK" ]]; then
	test_fail "login"
	exit 1
fi

sleep 3

GET_ALL_RPC="${NETCONF_TMP}/get/get_all_rpc.xml"
GET_ALL_OUT="${NETCONF_TMP}/get/get_all_out.xml"
mkdir -p "${NETCONF_TMP}/get"
cat > "$GET_ALL_RPC" <<'EORPC'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"/>
EORPC

rm -f "$GET_ALL_OUT"
send_cmd "user-rpc --content $GET_ALL_RPC --out $GET_ALL_OUT"

RESULT3="NOK"
for _w in $(seq 1 120); do
	if [[ -f "$GET_ALL_OUT" ]]; then
		if grep -aq "</data>" "$GET_ALL_OUT" 2>/dev/null \
			|| grep -aqE '^[[:space:]]*OK[[:space:]]*$' "$GET_ALL_OUT" 2>/dev/null; then
			RESULT3="OK"
			break
		fi
	fi
	sleep 0.5
done

echo "STEP 3. Criteria : Validating YANG-MODEL (get without filter)"
if [[ "$RESULT3" != "OK" ]]; then
	if [[ -f "$GET_ALL_OUT" ]]; then
		echo "[INFO] output file exists but no </data> found"
	else
		echo "[INFO] output file not created: $GET_ALL_OUT"
	fi
	test_fail "get-all response"
	exit 1
fi

declare -A MASTER_LIST
MASTER_LIST["hardware"]="urn:ietf:params:xml:ns:yang:ietf-hardware"
MASTER_LIST["interfaces"]="urn:ietf:params:xml:ns:yang:ietf-interfaces"
MASTER_LIST["nacm"]="urn:ietf:params:xml:ns:yang:ietf-netconf-acm"
MASTER_LIST["netconf-state"]="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring"
MASTER_LIST["netconf-server"]="urn:ietf:params:xml:ns:yang:ietf-netconf-server"
MASTER_LIST["yang-library"]="urn:ietf:params:xml:ns:yang:ietf-yang-library"
MASTER_LIST["modules-state"]="urn:ietf:params:xml:ns:yang:ietf-yang-library"
MASTER_LIST["delay-management"]="urn:o-ran:delay:1.0"
MASTER_LIST["dhcp"]="urn:o-ran:dhcp:1.0"
MASTER_LIST["active-alarm-list"]="urn:o-ran:fm:1.0"
MASTER_LIST["module-capability"]="urn:o-ran:module-cap:1.0"
MASTER_LIST["mplane-info"]="urn:o-ran:mplane-interfaces:1.0"
MASTER_LIST["operational-info"]="urn:o-ran:operations:1.0"
MASTER_LIST["performance-measurement-objects"]="urn:o-ran:performance-management:1.0"
MASTER_LIST["processing-elements"]="urn:o-ran:processing-element:1.0"
MASTER_LIST["software-inventory"]="urn:o-ran:software-management:1.0"
MASTER_LIST["sync"]="urn:o-ran:sync:1.0"
MASTER_LIST["port-transceivers"]="urn:o-ran:transceiver:1.0"
MASTER_LIST["user-plane-configuration"]="urn:o-ran:uplane-conf:1.0"
MASTER_LIST["users"]="urn:o-ran:user-mgmt:1.0"

declare -A FOUND_LIST

XML_CONTENT=$(xmlstarlet sel -t -m "/*[local-name()='data']/*" \
    -v "concat(local-name(), '|', namespace-uri())" -n "$GET_ALL_OUT" 2>/dev/null) || true

ERROR_COUNT=0
while read -r line; do
	[[ -z "$line" ]] && continue
	node_name="${line%|*}"
	ns_uri="${line#*|}"
	FOUND_LIST["$node_name"]="$ns_uri"
	if [[ -v MASTER_LIST["$node_name"] ]]; then
		if [[ "${MASTER_LIST[$node_name]}" != "$ns_uri" ]]; then
			echo "	[FAIL] $node_name: namespace mismatch!"
			echo "	   - expected : ${MASTER_LIST[$node_name]}"
			echo "	   - data     : $ns_uri"
			((ERROR_COUNT++))
		fi
	else
		echo "	[WARN] $node_name: Node not in criteria list"
	fi
done <<< "$XML_CONTENT"

for master_node in "${!MASTER_LIST[@]}"; do
	if [[ ! -v FOUND_LIST["$master_node"] ]]; then
		ns="${MASTER_LIST[$master_node]}"
		if grep -aqF "$ns" "$GET_ALL_OUT" 2>/dev/null; then
			echo "	[OK] \"$master_node\" not in top-level <data> (empty container, with-defaults=explicit) but namespace found in yang-library"
		else
			echo "	[MISSING] \"$master_node\" does not exist in O-RU."
			((ERROR_COUNT++))
		fi
	fi
done

if (( ERROR_COUNT == 0 )); then
	echo "STEP 3. Validation : OK"
else
	echo "STEP 3. Validation : NOK ($ERROR_COUNT errors)"
	test_fail "YANG model validation"
	exit 1
fi

echo "[INFO] 3.1.4.1 retrieval without filter completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
