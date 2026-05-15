#!/usr/bin/env bash
# O-RAN M-Plane 3.1.12.2 — Trace Test
set -u
set -o pipefail

TESTID="31122"
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
FILE_ID=$(jq -r '.["management-configurations"]["FileServer-ID"] // empty' "$CONFIG")
FILE_PW=$(jq -r '.["management-configurations"]["FileServer-PW"] // empty' "$CONFIG")

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
CLI_PID=""
cleanup() {
	if [[ "$COPROC_READY" == "1" ]]; then
		send_cmd "disconnect" 2>/dev/null || true
		sleep 1 || true
		exec 3>&- 2>/dev/null || true
	fi
	exec 20>&- 2>/dev/null || true
	if [[ -n "${WATCHDOG_PID:-}" ]]; then
		kill "$WATCHDOG_PID" 2>/dev/null || true
		wait "$WATCHDOG_PID" 2>/dev/null || true
	fi
	if [[ -n "${CLI_PID:-}" ]]; then
		sudo kill -15 "$CLI_PID" 2>/dev/null || true
	fi
	if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
		sudo kill -15 "$NETOPEER_COPROC_PID" 2>/dev/null || true
		sleep 1 || true
		sudo kill -9 "$NETOPEER_COPROC_PID" 2>/dev/null || true
	fi
	rm -f "${NETCONF_TMP}/to_ssh_cli" 2>/dev/null || true
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

mkdir -p "${NETCONF_TMP}/edit" "${NETCONF_TMP}/get"

CLI_FIFO="${NETCONF_TMP}/to_ssh_cli"
rm -f "$CLI_FIFO" 2>/dev/null || true
mkfifo "$CLI_FIFO"
exec 20<> "$CLI_FIFO"

sshpass -p "$CLI_PW" ssh -tt -o StrictHostKeyChecking=no "$CLI_ID@$ALLOWED_IP" <&20 >> "${NETCONF_TMP}/CLI-LOG.log" 2>&1 &
CLI_PID=$!

send_cmd "verb 3"
send_cmd "knownhosts --mode skip"
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout 300"

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
	test_fail "Call Home"
	exit 1
fi

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

sleep 3

send_cmd "subscribe --stream NETCONF"
sleep 2

WATCHDOG_RPC="${NETCONF_TMP}/watchdog.xml"
cat > "$WATCHDOG_RPC" <<'EORPC'
<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>
EORPC

(
_last_wdog_count=0
while true; do
	_cur_count=$(grep -c -a -F 'supervision-notification xmlns="urn:o-ran:supervision:1.0"' "$LOG" 2>/dev/null) || true
	if (( _cur_count > _last_wdog_count )); then
		_last_wdog_count=$_cur_count
		echo "user-rpc --content $WATCHDOG_RPC" >&3 2>/dev/null || true
		echo "Client SENT : user-rpc --content $WATCHDOG_RPC" >>"$LOG" 2>&1
	fi
	sleep 1
done
) &
WATCHDOG_PID=$!

START_TRACE_RPC="${NETCONF_TMP}/edit/start_trace.xml"
cat > "$START_TRACE_RPC" <<'EORPC'
<start-trace-logs xmlns="urn:o-ran:trace:1.0"/>
EORPC

START_TRACE_OUT="${NETCONF_TMP}/edit/start-trace.xml"
rm -f "$START_TRACE_OUT"
send_cmd "user-rpc --content $START_TRACE_RPC --out $START_TRACE_OUT"

RESULT0="NOK"
PAT_TRACE_OK='<status xmlns="urn:o-ran:trace:1.0">SUCCESS</status>'
for _w in $(seq 1 20); do
	if [[ -f "$START_TRACE_OUT" ]] && grep -a -F "$PAT_TRACE_OK" "$START_TRACE_OUT" >/dev/null 2>&1; then
		RESULT0="OK"
		echo "[WAIT]	Wait for Trace-log generated."
		echo "start-shell" >&20 2>/dev/null || true
		echo "watch -d -n0 'vtysh -c \"show system\"'" >&20 2>/dev/null || true
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "Start trace"
	exit 1
fi

RESULT3="NOK"
PAT_FIRST_TRACE="false</is-notification-last></trace-log-generated></notification>"
LOG_FILE=""
for _w in $(seq 1 3000); do
	if grep -a -F "$PAT_FIRST_TRACE" "$LOG" >/dev/null 2>&1; then
		RESULT3="OK"
		LOG_FILE=$(grep -oP '(?<=<log-file-name>).*(?=</log-file-name>)' "$LOG" | head -1) || true
		break
	fi
	sleep 0.2
done
echo "[$RESULT3]	Step 3.	First Trace-log generate."

if [[ "$RESULT3" != "OK" || -z "$LOG_FILE" ]]; then
	test_fail "First trace log notification"
	exit 1
fi

FILE_UPLOAD_RPC="${NETCONF_TMP}/edit/file_upload.xml"
cat > "$FILE_UPLOAD_RPC" <<EORPC
<file-upload xmlns="urn:o-ran:file-management:1.0">
  <local-logical-file-path>${LOG_FILE}</local-logical-file-path>
  <remote-file-path>sftp://${FILE_ID}@${LOCAL_IP}/tmp/${LOG_FILE}</remote-file-path>
  <password>${FILE_PW}</password>
</file-upload>
EORPC

rm -f "/tmp/${LOG_FILE}" 2>/dev/null || true
FILE_UPLOAD_OUT="${NETCONF_TMP}/edit/file_upload_out.xml"
rm -f "$FILE_UPLOAD_OUT"
send_cmd "user-rpc --content $FILE_UPLOAD_RPC --out $FILE_UPLOAD_OUT"

RESULT0="NOK"
PAT_FILE_MGMT_OK='<status xmlns="urn:o-ran:file-management:1.0">SUCCESS</status>'
for _w in $(seq 1 20); do
	if [[ -f "$FILE_UPLOAD_OUT" ]] && grep -a -F "$PAT_FILE_MGMT_OK" "$FILE_UPLOAD_OUT" >/dev/null 2>&1; then
		RESULT0="OK"
		echo "[WAIT]	Wait for File Upload to sftp://$FILE_ID@$LOCAL_IP/tmp/$LOG_FILE"
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "File upload RPC (first)"
	exit 1
fi

RESULT4="NOK"
PAT_UPLOAD_NOTIF="<status>SUCCESS</status></file-upload-notification></notification>"
UPLOAD_NOTIF_COUNT_BEFORE=$(grep -c -a -F "$PAT_UPLOAD_NOTIF" "$LOG" 2>/dev/null) || true
for _w in $(seq 1 3000); do
	_cur=$(grep -c -a -F "$PAT_UPLOAD_NOTIF" "$LOG" 2>/dev/null) || true
	if (( _cur > UPLOAD_NOTIF_COUNT_BEFORE )); then
		if [[ -f "/tmp/${LOG_FILE}" ]]; then
			RESULT4="OK"
		fi
		break
	fi
	sleep 0.2
done
echo "[$RESULT4]	Step 4.	File Upload ( $LOG_FILE )"

if [[ "$RESULT4" != "OK" ]]; then
	test_fail "File upload (first)"
	exit 1
fi

STOP_TRACE_RPC="${NETCONF_TMP}/edit/stop_trace.xml"
cat > "$STOP_TRACE_RPC" <<'EORPC'
<stop-trace-logs xmlns="urn:o-ran:trace:1.0"/>
EORPC

STOP_TRACE_OUT="${NETCONF_TMP}/edit/stop-trace.xml"
rm -f "$STOP_TRACE_OUT"
send_cmd "user-rpc --content $STOP_TRACE_RPC --out $STOP_TRACE_OUT"

RESULT0="NOK"
for _w in $(seq 1 20); do
	if [[ -f "$STOP_TRACE_OUT" ]] && grep -a -F "$PAT_TRACE_OK" "$STOP_TRACE_OUT" >/dev/null 2>&1; then
		RESULT0="OK"
		echo "[WAIT]	Wait for trace to end."
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "Stop trace"
	exit 1
fi

RESULT5="NOK"
PAT_LAST_TRACE="true</is-notification-last></trace-log-generated></notification>"
LOG_FILE=""
for _w in $(seq 1 3000); do
	if grep -a -F "$PAT_LAST_TRACE" "$LOG" >/dev/null 2>&1; then
		RESULT5="OK"
		LOG_FILE=$(grep -oP '(?<=<log-file-name>).*(?=</log-file-name>)' "$LOG" | tail -1) || true
		break
	fi
	sleep 0.2
done
echo "[$RESULT5]	Step 5.	Last Trace-log generated."

if [[ "$RESULT5" != "OK" || -z "$LOG_FILE" ]]; then
	test_fail "Last trace log notification"
	exit 1
fi

FILE_UPLOAD_RPC2="${NETCONF_TMP}/edit/file_upload2.xml"
cat > "$FILE_UPLOAD_RPC2" <<EORPC
<file-upload xmlns="urn:o-ran:file-management:1.0">
  <local-logical-file-path>${LOG_FILE}</local-logical-file-path>
  <remote-file-path>sftp://${FILE_ID}@${LOCAL_IP}/tmp/${LOG_FILE}</remote-file-path>
  <password>${FILE_PW}</password>
</file-upload>
EORPC

rm -f "/tmp/${LOG_FILE}" 2>/dev/null || true
FILE_UPLOAD_OUT2="${NETCONF_TMP}/edit/file_upload_out2.xml"
rm -f "$FILE_UPLOAD_OUT2"
send_cmd "user-rpc --content $FILE_UPLOAD_RPC2 --out $FILE_UPLOAD_OUT2"

RESULT0="NOK"
for _w in $(seq 1 20); do
	if [[ -f "$FILE_UPLOAD_OUT2" ]] && grep -a -F "$PAT_FILE_MGMT_OK" "$FILE_UPLOAD_OUT2" >/dev/null 2>&1; then
		RESULT0="OK"
		echo "[WAIT]	Wait for File Upload to sftp://$FILE_ID@$LOCAL_IP/tmp/$LOG_FILE"
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "File upload RPC (last)"
	exit 1
fi

RESULT6="NOK"
UPLOAD_NOTIF_COUNT_BEFORE2=$(grep -c -a -F "$PAT_UPLOAD_NOTIF" "$LOG" 2>/dev/null) || true
for _w in $(seq 1 3000); do
	_cur=$(grep -c -a -F "$PAT_UPLOAD_NOTIF" "$LOG" 2>/dev/null) || true
	if (( _cur > UPLOAD_NOTIF_COUNT_BEFORE2 )); then
		if [[ -f "/tmp/${LOG_FILE}" ]]; then
			RESULT6="OK"
		fi
		break
	fi
	sleep 0.2
done
echo "[$RESULT6]	Step 6.	File Upload ( $LOG_FILE )"

if [[ "$RESULT6" != "OK" ]]; then
	test_fail "File upload (last)"
	exit 1
fi

echo "[PASS]"

echo "[INFO] 3.1.12.2 Trace Test completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
