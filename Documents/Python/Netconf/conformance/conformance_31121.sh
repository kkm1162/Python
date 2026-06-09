#!/usr/bin/env bash
# O-RAN M-Plane 3.1.12.1 — Log Management
set -u
set -o pipefail

TESTID="31121"
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
FILE_SERVER_IP=$(jq -r '.["management-configurations"]["FileServer-IP"] // empty' "$CONFIG")
LOCAL_LOG_PREFIX=$(jq -r '.["management-configurations"]["local-log-prefix"] // "O-RAN/log"' "$CONFIG")
LOCAL_LOG_PREFIX="${LOCAL_LOG_PREFIX%/}"
REMOTE_UPLOAD_DIR=$(jq -r '.["management-configurations"]["remote-upload-dir"] // "/tmp"' "$CONFIG")
REMOTE_UPLOAD_DIR="${REMOTE_UPLOAD_DIR:-/tmp}"
REMOTE_UPLOAD_DIR="${REMOTE_UPLOAD_DIR%/}"
[[ -n "$FILE_SERVER_IP" ]] || FILE_SERVER_IP="$LOCAL_IP"
[[ "$REMOTE_UPLOAD_DIR" == /* ]] || REMOTE_UPLOAD_DIR="/${REMOTE_UPLOAD_DIR}"

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

_extract_log_file_name() {
	local name=""
	name=$(grep -oP '(?<=<log-file-name>)[^<]+(?=</log-file-name>)' "$LOG" 2>/dev/null | head -1) || true
	if [[ -z "$name" ]]; then
		name=$(sed -n 's:.*<log-file-name>\([^<]*\)</log-file-name>.*:\1:p' "$LOG" | head -1) || true
	fi
	echo "${name//$'\r'/}" | tr -d '\n'
}

COPROC_READY=0
NETOPEER_COPROC_PID=""
WATCHDOG_PID=""
cleanup() {
	if [[ "$COPROC_READY" == "1" ]]; then
		send_cmd "disconnect" 2>/dev/null || true
		sleep 1 || true
		exec 3>&- 2>/dev/null || true
	fi
	if [[ -n "${WATCHDOG_PID:-}" ]]; then
		kill "$WATCHDOG_PID" 2>/dev/null || true
		wait "$WATCHDOG_PID" 2>/dev/null || true
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

RESULT1=$(conformance_callhome_wait_step1 1500)

echo "[$RESULT1]	STEP 1.	The Netconf Client receive the CallHome from ORU"
if [[ "$RESULT1" != "OK" ]]; then
	test_fail "Call Home"
	exit 1
fi

RESULT2=$(conformance_callhome_wait_auth 150)

echo "[$RESULT2]	STEP 2.	Successfully login with the correct username and password ($USER / ***)"
if [[ "$RESULT2" != "OK" ]]; then
	test_fail "login"
	exit 1
fi

sleep 3

send_cmd "subscribe --stream NETCONF"
sleep 2

mkdir -p "${NETCONF_TMP}/edit" "${NETCONF_TMP}/get"

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

START_TROUBLESHOOT_RPC="${NETCONF_TMP}/edit/start_troubleshooting.xml"
cat > "$START_TROUBLESHOOT_RPC" <<'EORPC'
<start-troubleshooting-logs xmlns="urn:o-ran:troubleshooting:1.0"/>
EORPC

START_TROUBLESHOOT_OUT="${NETCONF_TMP}/edit/start_troubleshooting_out.xml"
rm -f "$START_TROUBLESHOOT_OUT"
send_cmd "user-rpc --content $START_TROUBLESHOOT_RPC --out $START_TROUBLESHOOT_OUT"

RESULT0="NOK"
PAT_TROUBLESHOOT_OK='<status xmlns="urn:o-ran:troubleshooting:1.0">SUCCESS</status>'
for _w in $(seq 1 20); do
	if [[ -f "$START_TROUBLESHOOT_OUT" ]] && grep -a -F "$PAT_TROUBLESHOOT_OK" "$START_TROUBLESHOOT_OUT" >/dev/null 2>&1; then
		RESULT0="OK"
		echo "[WAIT]	Wait for Troubleshooting-log generated."
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "Start troubleshooting"
	exit 1
fi

RESULT3="NOK"
PAT_TROUBLESHOOT_NOTIF="</log-file-name></troubleshooting-log-generated></notification>"
LOG_FILE=""
for _w in $(seq 1 3000); do
	if grep -a -F "$PAT_TROUBLESHOOT_NOTIF" "$LOG" >/dev/null 2>&1; then
		RESULT3="OK"
		LOG_FILE="$(_extract_log_file_name)"
		break
	fi
	sleep 0.2
done
echo "[$RESULT3]	Step 3.	Troubleshooting-log generate."

if [[ "$RESULT3" != "OK" || -z "$LOG_FILE" ]]; then
	test_fail "Troubleshooting log notification"
	exit 1
fi

LOG_BASENAME="${LOG_FILE##*/}"
LOCAL_LOGICAL_PATH="${LOCAL_LOG_PREFIX}/${LOG_BASENAME}"
echo "[INFO]	Troubleshooting log file: ${LOG_BASENAME}"
echo "[INFO]	local-logical-file-path: ${LOCAL_LOGICAL_PATH}"
# 3.1.12.2(trace)와 동일: stop 전에 file-upload (stop 후에는 일부 RU가 upload RPC 미수신/거부)
FILE_UPLOAD_GUARD_SEC="${FILE_UPLOAD_GUARD_SEC:-2}"
if [[ "$FILE_UPLOAD_GUARD_SEC" =~ ^[0-9]+$ ]] && (( FILE_UPLOAD_GUARD_SEC > 0 )); then
	echo "[INFO]	file-upload guard ${FILE_UPLOAD_GUARD_SEC}s after log-generated notification"
	sleep "$FILE_UPLOAD_GUARD_SEC"
fi

if [[ -z "$FILE_ID" || -z "$FILE_PW" ]]; then
	echo "[FAIL] FileServer-ID or FileServer-PW missing in --config (3.1.12.1 settings)"
	test_fail "File server credentials"
	exit 1
fi

FILE_UPLOAD_RPC="${NETCONF_TMP}/edit/file_upload.xml"
cat > "$FILE_UPLOAD_RPC" <<EORPC
<file-upload xmlns="urn:o-ran:file-management:1.0" xmlns:ict="urn:ietf:params:xml:ns:yang:ietf-crypto-types">
  <local-logical-file-path>${LOCAL_LOGICAL_PATH}</local-logical-file-path>
  <remote-file-path>sftp://${FILE_ID}@${FILE_SERVER_IP}${REMOTE_UPLOAD_DIR}/${LOG_BASENAME}</remote-file-path>
  <password>
    <password>${FILE_PW}</password>
  </password>
</file-upload>
EORPC

REMOTE_RECEIVER_FILE="${REMOTE_UPLOAD_DIR}/${LOG_BASENAME}"
echo "[INFO]	file-upload target: sftp://${FILE_ID}@${FILE_SERVER_IP}${REMOTE_UPLOAD_DIR}/${LOG_BASENAME}"
echo "[INFO]	SFTP receiver check: ${REMOTE_RECEIVER_FILE}"
echo "[INFO]	file-upload RPC body:"
sed 's/^/[INFO]	  /' "$FILE_UPLOAD_RPC" 2>/dev/null || true
rm -f "${REMOTE_RECEIVER_FILE}" 2>/dev/null || true
FILE_UPLOAD_OUT="${NETCONF_TMP}/edit/file_upload_out.xml"
rm -f "$FILE_UPLOAD_OUT"
send_cmd "user-rpc --content $FILE_UPLOAD_RPC --out $FILE_UPLOAD_OUT"

RESULT_UPLOAD_RPC="NOK"
PAT_FILE_MGMT_OK='<status xmlns="urn:o-ran:file-management:1.0">SUCCESS</status>'
for _w in $(seq 1 60); do
	if [[ -f "$FILE_UPLOAD_OUT" ]] && grep -a -F "$PAT_FILE_MGMT_OK" "$FILE_UPLOAD_OUT" >/dev/null 2>&1; then
		RESULT_UPLOAD_RPC="OK"
		echo "[WAIT]	Wait for File Upload to sftp://$FILE_ID@$FILE_SERVER_IP${REMOTE_UPLOAD_DIR}/$LOG_BASENAME"
		break
	fi
	sleep 0.5
done
if [[ "$RESULT_UPLOAD_RPC" != "OK" ]]; then
	echo "[FAIL] File upload RPC (no file-management SUCCESS in reply within 30s)"
	if [[ -f "$FILE_UPLOAD_OUT" ]]; then
		echo "[INFO]	file-upload rpc-reply tail:"
		tail -n 15 "$FILE_UPLOAD_OUT" 2>/dev/null | while IFS= read -r _ln; do
			echo "[INFO]	  ${_ln}"
		done
	else
		echo "[INFO]	missing reply file: $FILE_UPLOAD_OUT"
	fi
	test_fail "File upload RPC"
	exit 1
fi

RESULT4="NOK"
PAT_UPLOAD_NOTIF="<status>SUCCESS</status></file-upload-notification></notification>"
UPLOAD_NOTIF_COUNT_BEFORE=$(grep -c -a -F "$PAT_UPLOAD_NOTIF" "$LOG" 2>/dev/null) || true
UPLOAD_NOTIF_COUNT_BEFORE=${UPLOAD_NOTIF_COUNT_BEFORE:-0}
for _w in $(seq 1 3000); do
	_cur=$(grep -c -a -F "$PAT_UPLOAD_NOTIF" "$LOG" 2>/dev/null) || true
	_cur=${_cur:-0}
	if (( _cur > UPLOAD_NOTIF_COUNT_BEFORE )); then
		if [[ -f "${REMOTE_RECEIVER_FILE}" ]]; then
			RESULT4="OK"
			break
		fi
		if (( _w % 25 == 0 )); then
			echo "[INFO]	upload-notification seen; waiting for ${REMOTE_RECEIVER_FILE}"
		fi
	fi
	sleep 0.2
done
echo "[$RESULT4]	Step 4.	File Upload ( ${LOG_BASENAME} )."

if [[ "$RESULT4" != "OK" ]]; then
	if [[ ! -f "${REMOTE_RECEIVER_FILE}" ]]; then
		echo "[FAIL] SFTP file missing on receiver: ${REMOTE_RECEIVER_FILE}"
		ls -la "${REMOTE_UPLOAD_DIR}/" 2>/dev/null | head -15 | sed 's/^/[INFO]	  /' || true
	else
		echo "[FAIL] file-upload-notification SUCCESS not seen after upload RPC"
	fi
	test_fail "File upload"
	exit 1
fi

STOP_TROUBLESHOOT_RPC="${NETCONF_TMP}/edit/stop_troubleshooting.xml"
cat > "$STOP_TROUBLESHOOT_RPC" <<'EORPC'
<stop-troubleshooting-logs xmlns="urn:o-ran:troubleshooting:1.0"/>
EORPC

STOP_TROUBLESHOOT_OUT="${NETCONF_TMP}/edit/stop_troubleshooting_out.xml"
rm -f "$STOP_TROUBLESHOOT_OUT"
send_cmd "user-rpc --content $STOP_TROUBLESHOOT_RPC --out $STOP_TROUBLESHOOT_OUT"

RESULT_STOP="NOK"
for _w in $(seq 1 20); do
	if [[ -f "$STOP_TROUBLESHOOT_OUT" ]] && grep -a -F "$PAT_TROUBLESHOOT_OK" "$STOP_TROUBLESHOOT_OUT" >/dev/null 2>&1; then
		RESULT_STOP="OK"
		echo "[$RESULT_STOP]	Step 5.	Stop troubleshooting."
		break
	fi
	sleep 0.5
done
if [[ "$RESULT_STOP" != "OK" ]]; then
	echo "[WARN]	stop-troubleshooting did not report SUCCESS (upload already OK)"
fi

echo "[PASS]"

echo "[INFO] 3.1.12.1 Log Management completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
