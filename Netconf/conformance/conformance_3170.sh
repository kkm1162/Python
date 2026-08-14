#!/usr/bin/env bash
# O-RAN M-Plane 3.1.7.1 — Software Activation without Reset
set -u
set -o pipefail

TESTID="3170"
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
CLI_ID=$(jq -r '.["management-configurations"]["CLI-ID"] // empty' "$CONFIG")
CLI_PW=$(jq -r '.["management-configurations"]["CLI-PW"] // empty' "$CONFIG")

SWM_PATH="${SW_PKG_REMOTE_PATH:-$(jq -r '.["software-management"]["path"] // empty' "$CONFIG")}"
SWM_PASSWORD="$(jq -r '.["software-management"]["password"] // empty' "$CONFIG")"

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
WATCHDOG_PID=""
cleanup() {
	if [[ -n "${WATCHDOG_PID:-}" ]]; then
		kill "$WATCHDOG_PID" 2>/dev/null || true
		wait "$WATCHDOG_PID" 2>/dev/null || true
	fi
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

mkdir -p "${NETCONF_TMP}/edit" "${NETCONF_TMP}/get"

# --- Generate inline XML RPCs ---
WT_XML="${NETCONF_TMP}/supervision_reset.xml"
cat > "$WT_XML" <<'EORPC'
<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>
EORPC

GET_RUNNING_SLOT_RPC="${NETCONF_TMP}/get/get_running_slot.xml"
cat > "$GET_RUNNING_SLOT_RPC" <<'EORPC'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <filter type="subtree">
    <software-inventory xmlns="urn:o-ran:software-management:1.0">
      <software-slot>
        <active>true</active>
      </software-slot>
    </software-inventory>
  </filter>
</get>
EORPC

RESET_RPC="${NETCONF_TMP}/edit/reset.xml"
cat > "$RESET_RPC" <<'EORPC'
<reset xmlns="urn:o-ran:operations:1.0"/>
EORPC

coproc NP2 {
	setsid stdbuf -oL sshpass -p "$PASSWORD" netopeer2-cli 2>&1
} >>"$LOG" 2>&1
NETOPEER_COPROC_PID="${NP2_PID:-$!}"
exec 3>&"${NP2[1]}"
COPROC_READY=1

send_cmd "verb 3"
send_cmd "knownhosts --mode skip"
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout 300"

########################################################################
# STEP 1. Call Home
########################################################################
RESULT1="NOK"
PAT_ACCEPT_OLD="Accepted a connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
PAT_ACCEPT_NEW="Accepted a new connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
PAT_ACCEPT="$PAT_ACCEPT_OLD"
for _w in $(seq 1 1500); do
	if grep -a -F -e "$PAT_ACCEPT_OLD" -e "$PAT_ACCEPT_NEW" "$LOG" >/dev/null 2>&1; then
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

########################################################################
# STEP 2. Authentication
########################################################################
RESULT2="NOK"
for _w in $(seq 1 150); do
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

########################################################################
# Subscribe + Watchdog
########################################################################
send_cmd "subscribe --stream NETCONF"

RESULT_SUB="NOK"
for _w in $(seq 1 300); do
	if grep -a "<ok/>" "$LOG" >/dev/null 2>&1; then
		RESULT_SUB="OK"
		break
	fi
	sleep 0.2
done
if [[ "$RESULT_SUB" != "OK" ]]; then
	test_fail "subscribe"
	exit 1
fi

_watchdog_notif_seen=0
(
	while true; do
		_cnt=$(grep -acE '<supervision-notification' "$LOG" 2>/dev/null)
		if [[ "${_cnt:-0}" =~ ^[0-9]+$ ]] && (( _cnt > _watchdog_notif_seen )); then
			_watchdog_notif_seen=$_cnt
			echo "user-rpc --content $WT_XML" >&3 2>/dev/null || true
			echo "Client SENT : user-rpc --content $WT_XML" >>"$LOG" 2>&1
		fi
		sleep 0.5
	done
) &
WATCHDOG_PID=$!

########################################################################
# STEP 3. Get Active Slot
########################################################################
GET_RUNNING_OUT="${NETCONF_TMP}/get/get_running_slot_out.xml"
rm -f "$GET_RUNNING_OUT"
send_cmd "user-rpc --content $GET_RUNNING_SLOT_RPC --out $GET_RUNNING_OUT"

RESULT3="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$GET_RUNNING_OUT" ]]; then
		if grep -aq "</data>" "$GET_RUNNING_OUT" 2>/dev/null; then
			RESULT3="OK"
			break
		fi
	fi
	sleep 0.5
done

RUNNINGSLOT=""
if [[ "$RESULT3" == "OK" ]]; then
	RUNNINGSLOT=$(xmlstarlet sel -N x="urn:o-ran:software-management:1.0" -t -v "//x:software-slot/x:name" "$GET_RUNNING_OUT" 2>/dev/null | head -n 1 | tr -d '\r\n ') || true
fi
echo "[$RESULT3] STEP 3. Check Active Slot, ${RUNNINGSLOT:-unknown} is Active now."
if [[ "$RESULT3" != "OK" ]]; then
	test_fail "get active slot"
	exit 1
fi

########################################################################
# STEP 4. Software Activate
########################################################################
if [[ "$RUNNINGSLOT" == "first" ]]; then
	NONRUNNINGSLOT="second"
else
	NONRUNNINGSLOT="first"
fi

SW_ACTIVATE_RPC="${NETCONF_TMP}/edit/software_activate.xml"
cat > "$SW_ACTIVATE_RPC" <<EORPC
<software-activate xmlns="urn:o-ran:software-management:1.0">
  <slot-name>${NONRUNNINGSLOT}</slot-name>
</software-activate>
EORPC

send_cmd "user-rpc --content $SW_ACTIVATE_RPC"
echo "		Software Activation Started	.slot = $NONRUNNINGSLOT"

RESULT_ACT="NOK"
for _w in $(seq 1 100); do
	if grep -a -F "<slot-name>${NONRUNNINGSLOT}</slot-name>" "$LOG" >/dev/null 2>&1 \
		&& grep -a -F "<status>COMPLETED</status>" "$LOG" >/dev/null 2>&1; then
		RESULT_ACT="OK"
		break
	fi
	sleep 0.5
done

if [[ "$RESULT_ACT" != "OK" ]]; then
	test_fail "software activation event not completed"
	exit 1
fi
echo "[$RESULT_ACT] STEP 4. Software Activation Event Completed."

########################################################################
# STEP 5. Verify Active Slot Changed
########################################################################
_activate_guard_raw="${ACTIVATE_GET_GUARD_SEC:-$(jq -r '.["software-management"]["activate-get-guard-sec"] // empty' "$CONFIG")}"
_activate_guard_raw="${_activate_guard_raw//[!0-9]/}"
ACTIVATE_GET_GUARD_SEC="${_activate_guard_raw:-5}"
ACTIVATE_GET_POLL_SEC=2

GET_ACTIVE_RPC="${NETCONF_TMP}/get/get_active_slot.xml"
cat > "$GET_ACTIVE_RPC" <<EORPC
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <filter type="subtree">
    <software-inventory xmlns="urn:o-ran:software-management:1.0">
      <software-slot>
        <name>${NONRUNNINGSLOT}</name>
      </software-slot>
    </software-inventory>
  </filter>
</get>
EORPC

GET_ACTIVE_OUT="${NETCONF_TMP}/get/get_active_slot_out.xml"

echo "[INFO] STEP 5: activate→GET guard ${ACTIVATE_GET_GUARD_SEC}s (some RUs delay active state)"
if (( ACTIVATE_GET_GUARD_SEC > 0 )); then
	sleep "$ACTIVATE_GET_GUARD_SEC"
fi

RESULT5="NOK"
ACTIVESLOT_STATUS=""
_step5_deadline=$(($(date +%s) + ACTIVATE_GET_GUARD_SEC))
_step5_try=0
while :; do
	_step5_try=$((_step5_try + 1))
	rm -f "$GET_ACTIVE_OUT"
	send_cmd "user-rpc --content $GET_ACTIVE_RPC --out $GET_ACTIVE_OUT"

	for _w in $(seq 1 60); do
		if [[ -f "$GET_ACTIVE_OUT" ]]; then
			if grep -aq "</data>" "$GET_ACTIVE_OUT" 2>/dev/null; then
				break
			fi
		fi
		sleep 0.5
	done

	ACTIVESLOT_STATUS=""
	if [[ -f "$GET_ACTIVE_OUT" ]]; then
		ACTIVESLOT_STATUS=$(xmlstarlet sel -N x="urn:o-ran:software-management:1.0" -t -v "//x:active" "$GET_ACTIVE_OUT" 2>/dev/null) || true
	fi

	if [[ "$ACTIVESLOT_STATUS" == "true" ]]; then
		RESULT5="OK"
		break
	fi

	if (( $(date +%s) >= _step5_deadline )); then
		break
	fi
	echo "[INFO] STEP 5: active not true yet (${ACTIVESLOT_STATUS:-empty}), retry GET in ${ACTIVATE_GET_POLL_SEC}s (try ${_step5_try})"
	sleep "$ACTIVATE_GET_POLL_SEC"
done

if [[ "$RESULT5" == "OK" ]]; then
	echo "[$RESULT5] STEP 5. Check Active Status, $NONRUNNINGSLOT is Active now (try ${_step5_try}, guard ${ACTIVATE_GET_GUARD_SEC}s)."
else
	echo "[$RESULT5] STEP 5. Check Active Status, $NONRUNNINGSLOT active is ${ACTIVESLOT_STATUS:-unknown} (try ${_step5_try}, guard ${ACTIVATE_GET_GUARD_SEC}s)."
	test_fail "active slot did not change to true after activation"
	exit 1
fi

echo "[PASS]"
echo "[INFO] 3.1.7.1 Software Activation completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
