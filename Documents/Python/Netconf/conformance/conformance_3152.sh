#!/usr/bin/env bash
# O-RAN M-Plane 3.1.5.2 — Retrieval of Active Alarm List
# Connects to L2SW via SSH, executes OFF commands to trigger alarm,
# verifies notifications, retrieves active-alarm-list, then restores.
set -u
set -o pipefail

TESTID="3152"
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

L2SW_IP="${L2SW_IP:-}"
L2SW_ID="${L2SW_ID:-}"
L2SW_PW="${L2SW_PW:-}"
ALARM_OFF_CMDS="${ALARM_OFF_CMDS:-}"
ALARM_ON_CMDS="${ALARM_ON_CMDS:-}"
ALARM_TIMEOUT_SEC="${ALARM_TIMEOUT_SEC:-300}"

if [[ -z "$L2SW_IP" || -z "$L2SW_ID" || -z "$L2SW_PW" ]]; then
	echo "[ERROR] L2SW connection info required (L2SW_IP, L2SW_ID, L2SW_PW)"
	exit 2
fi
if [[ -z "$ALARM_OFF_CMDS" ]]; then
	echo "[ERROR] ALARM_OFF_CMDS required (comma-separated commands)"
	exit 2
fi

WATCHDOG_RPC="${NETCONF_TMP}/edit/watchdog_reset.xml"
mkdir -p "${NETCONF_TMP}/edit" "${NETCONF_TMP}/get"
cat > "${WATCHDOG_RPC}" <<'EORPC'
<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>
EORPC

GET_RPC="${NETCONF_TMP}/get/get_active_alarm_list.xml"
cat > "${GET_RPC}" <<'XMLEOF'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><filter type="subtree"><active-alarm-list xmlns="urn:o-ran:fm:1.0"/></filter></get>
XMLEOF
GET_OUT="${NETCONF_TMP}/get/get_active_alarm_list_out.xml"

echo "[INFO] USER=$USER, ALLOWED_IP=$ALLOWED_IP, LOCAL_IP=$LOCAL_IP, LISTEN_PORT=$LISTEN_PORT (Call Home), PRODUCT=$PRODUCT"
echo "[INFO] L2SW=$L2SW_IP, TIMEOUT=${ALARM_TIMEOUT_SEC}s"
echo "[INFO] OFF_CMDS=$ALARM_OFF_CMDS"
echo "[INFO] ON_CMDS=$ALARM_ON_CMDS"

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

l2sw_send() {
	echo "[L2SW] >>> $*"
	echo "$*" >&20 2>/dev/null || true
	sleep 1
}

test_fail() {
	echo "[FAIL] $*"
}

COPROC_READY=0
NETOPEER_COPROC_PID=""
WATCHDOG_PID=""
CLI_PID=""
CLI_FIFO="${NETCONF_TMP}/to_ssh_l2sw"

cleanup() {
	if [[ -n "${ALARM_ON_CMDS:-}" ]]; then
		IFS=',' read -ra _on_arr <<< "$ALARM_ON_CMDS"
		for _c in "${_on_arr[@]}"; do
			_c="$(echo "$_c" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
			[[ -n "$_c" ]] && echo "$_c" >&20 2>/dev/null || true
			sleep 1
		done
	fi
	sleep 1

	if [[ -n "${WATCHDOG_PID:-}" ]]; then
		kill "$WATCHDOG_PID" 2>/dev/null || true
		wait "$WATCHDOG_PID" 2>/dev/null || true
	fi

	if [[ -n "${CLI_PID:-}" ]]; then
		kill "$CLI_PID" 2>/dev/null || true
		wait "$CLI_PID" 2>/dev/null || true
	fi
	exec 20>&- 2>/dev/null || true
	rm -f "$CLI_FIFO" 2>/dev/null || true

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

########################################################################################
# STEP 1. Call Home
########################################################################################
RESULT1="NOK"
PAT_ACCEPT="Accepted a connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
for _w in $(seq 1 1500); do
	if grep -a -F "$PAT_ACCEPT" "$LOG" >/dev/null 2>&1; then
		RESULT1="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT1]	STEP 1.	The Netconf Client receive the CallHome from ORU"
if [[ "$RESULT1" != "OK" ]]; then
	test_fail "CallHome"
	exit 1
fi

########################################################################################
# STEP 2. Login
########################################################################################
RESULT2="NOK"
for _w in $(seq 1 150); do
	if grep -a -F "Authentication successful" "$LOG" >/dev/null 2>&1; then
		RESULT2="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT2]	STEP 2.	Successfully login with the correct username and password ($USER / ***)"
if [[ "$RESULT2" != "OK" ]]; then
	test_fail "login"
	exit 1
fi

########################################################################################
# Subscribe
########################################################################################
sleep 5
_ok_before=$(grep -c -a -F "OK" "$LOG" 2>/dev/null) || true
send_cmd "subscribe --stream NETCONF"
for _w in $(seq 1 300); do
	_ok_now=$(grep -c -a -F "OK" "$LOG" 2>/dev/null) || true
	if (( _ok_now > _ok_before )); then
		break
	fi
	sleep 0.2
done

########################################################################################
# Watchdog (background)
########################################################################################
(
	_last_count=0
	while true; do
		sleep 2
		_cur_count=$(grep -acE '^\s*<supervision-notification' "$LOG" 2>/dev/null) || true
		if [[ "${_cur_count:-0}" =~ ^[0-9]+$ ]] && (( _cur_count > _last_count )); then
			while (( _cur_count > _last_count )); do
				_last_count=$((_last_count + 1))
				echo "user-rpc --content ${WATCHDOG_RPC}" >&3 2>/dev/null || true
				echo "Client SENT : user-rpc --content ${WATCHDOG_RPC}" >>"$LOG" 2>&1
			done
		fi
	done
) &
WATCHDOG_PID=$!

########################################################################################
# STEP 3. L2SW SSH + OFF commands
########################################################################################
sleep 3
rm -f "$CLI_FIFO" 2>/dev/null || true
mkfifo "$CLI_FIFO"
exec 20<>"$CLI_FIFO"

{ sshpass -p "$L2SW_PW" ssh -tt -o StrictHostKeyChecking=no "$L2SW_ID@$L2SW_IP" <&20 >>"$LOG_DIR/L2SW-LOG.log" 2>&1 || true; } &
CLI_PID=$!
sleep 3

_sync_before=$(grep -acE 'synchronization-state-change' "$LOG" 2>/dev/null) || true
_alarm_before=$(grep -acE '<fault-id>' "$LOG" 2>/dev/null) || true

TIMEOUT_ITER=$(( ALARM_TIMEOUT_SEC * 5 ))

IFS=',' read -ra OFF_ARR <<< "$ALARM_OFF_CMDS"
for _cmd in "${OFF_ARR[@]}"; do
	_cmd="$(echo "$_cmd" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
	[[ -n "$_cmd" ]] || continue
	l2sw_send "$_cmd"
done
echo "[OK]	STEP 3.	L2SW OFF commands sent (${#OFF_ARR[@]} commands)"

########################################################################################
# STEP 4. sync-state-change notification
########################################################################################
RESULT4="NOK"
for _w in $(seq 1 "$TIMEOUT_ITER"); do
	_sync_now=$(grep -acE 'synchronization-state-change' "$LOG" 2>/dev/null) || true
	if [[ "${_sync_now:-0}" =~ ^[0-9]+$ ]] && (( _sync_now > _sync_before )); then
		RESULT4="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT4]	STEP 4.	The RU transmitted sync-state-change Notification"
if [[ "$RESULT4" != "OK" ]]; then
	test_fail "sync-state-change notification timeout"
	exit 1
fi

########################################################################################
# STEP 5. Alarm occur notification (is-cleared=false)
########################################################################################
RESULT5="NOK"
for _w in $(seq 1 "$TIMEOUT_ITER"); do
	_alarm_now=$(grep -acE '<fault-id>' "$LOG" 2>/dev/null) || true
	if [[ "${_alarm_now:-0}" =~ ^[0-9]+$ ]] && (( _alarm_now > _alarm_before )); then
		if grep -a -E '<is-cleared>false</is-cleared>' "$LOG" >/dev/null 2>&1; then
			RESULT5="OK"
			break
		fi
	fi
	sleep 0.2
done
echo "[$RESULT5]	STEP 5.	The RU transmitted alarm occur Notification"
if [[ "$RESULT5" != "OK" ]]; then
	test_fail "alarm-occur notification timeout"
	exit 1
fi

########################################################################################
# STEP 6. Get active-alarm-list (alarm should exist)
########################################################################################
rm -f "${GET_OUT}" 2>/dev/null || true
send_cmd "user-rpc --content ${GET_RPC} --out ${GET_OUT}"
for _w in $(seq 1 50); do
	if grep -a -q '</data>' "${GET_OUT}" 2>/dev/null; then
		break
	fi
	sleep 0.2
done
_alarm_count=$(xmlstarlet sel -t -v "count(/*[local-name()='data']/*[local-name()='active-alarm-list']/*[local-name()='active-alarms'])" "${GET_OUT}" 2>/dev/null) || _alarm_count=0
if (( _alarm_count > 0 )); then
	RESULT6="OK"
else
	RESULT6="NOK"
fi
echo "[$RESULT6]	STEP 6.	Check active-alarm-list (alarm should exist, count=$_alarm_count)"
if [[ "$RESULT6" != "OK" ]]; then
	test_fail "no alarm in active-alarm-list"
	exit 1
fi

########################################################################################
# STEP 7. L2SW ON commands
########################################################################################
if [[ -n "${ALARM_ON_CMDS:-}" ]]; then
	_alarm_clear_before=$(grep -acE '<is-cleared>true</is-cleared>' "$LOG" 2>/dev/null) || true

	IFS=',' read -ra ON_ARR <<< "$ALARM_ON_CMDS"
	for _cmd in "${ON_ARR[@]}"; do
		_cmd="$(echo "$_cmd" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
		[[ -n "$_cmd" ]] || continue
		l2sw_send "$_cmd"
	done
	echo "[OK]	STEP 7.	L2SW ON commands sent (${#ON_ARR[@]} commands)"

	########################################################################################
	# STEP 8. Alarm clear notification (is-cleared=true)
	########################################################################################
	RESULT8="NOK"
	for _w in $(seq 1 "$TIMEOUT_ITER"); do
		_alarm_clear_now=$(grep -acE '<is-cleared>true</is-cleared>' "$LOG" 2>/dev/null) || true
		if [[ "${_alarm_clear_now:-0}" =~ ^[0-9]+$ ]] && (( _alarm_clear_now > _alarm_clear_before )); then
			RESULT8="OK"
			break
		fi
		sleep 0.2
	done
	echo "[$RESULT8]	STEP 8.	The RU transmitted alarm clear Notification"
	if [[ "$RESULT8" != "OK" ]]; then
		test_fail "alarm-clear notification timeout"
		exit 1
	fi

	########################################################################################
	# STEP 9. Get active-alarm-list (alarm should not exist)
	########################################################################################
	rm -f "${GET_OUT}" 2>/dev/null || true
	send_cmd "user-rpc --content ${GET_RPC} --out ${GET_OUT}"
	for _w in $(seq 1 50); do
		if grep -a -q -E '</data>|<data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"/>' "${GET_OUT}" 2>/dev/null; then
			break
		fi
		sleep 0.2
	done
	_alarm_count2=$(xmlstarlet sel -t -v "count(/*[local-name()='data']/*[local-name()='active-alarm-list']/*[local-name()='active-alarms'])" "${GET_OUT}" 2>/dev/null) || _alarm_count2=0
	if (( _alarm_count2 == 0 )); then
		RESULT9="OK"
	else
		RESULT9="NOK"
	fi
	echo "[$RESULT9]	STEP 9.	Check active-alarm-list (alarm should not exist, count=$_alarm_count2)"
	if [[ "$RESULT9" != "OK" ]]; then
		test_fail "alarm still in active-alarm-list"
		exit 1
	fi
else
	echo "[OK]	STEP 7.	ON commands not configured (skip)"
fi

echo "[PASS]"
echo "[INFO] 3.1.5.2 Retrieval of Active Alarm List completed. Detailed log: $LOG"

trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
