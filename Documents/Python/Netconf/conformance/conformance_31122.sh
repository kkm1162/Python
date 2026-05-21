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

_trace_name_from_block() {
	local block="$1"
	local n="${block#*<log-file-name>}"
	n="${n%%</log-file-name>*}"
	n="${n##*/}"
	printf '%s' "$n" | tr -d '\r\n '
}

_extract_first_false_trace_log() {
	local blk
	blk=$(awk '
		/<trace-log-generated/ { block = "" }
		{ block = block $0 "\n" }
		/<\/trace-log-generated>/ {
			if (block ~ /<is-notification-last>[[:space:]]*false[[:space:]]*<\// && block ~ /<log-file-name>/) {
				print block
				exit
			}
			block = ""
		}
	' "$LOG")
	if [[ -n "$blk" ]]; then
		_trace_name_from_block "$blk"
	fi
}

_extract_last_true_trace_log() {
	local blk
	blk=$(awk '
		/<trace-log-generated/ { block = "" }
		{ block = block $0 "\n" }
		/<\/trace-log-generated>/ {
			if (block ~ /<is-notification-last>[[:space:]]*true[[:space:]]*<\// && block ~ /<log-file-name>/) {
				last = block
			}
			block = ""
		}
		END { if (last != "") print last }
	' "$LOG")
	if [[ -n "$blk" ]]; then
		_trace_name_from_block "$blk"
	fi
}

_extract_last_true_trace_log_after_line() {
	local min_line="${1:-1}"
	local blk
	blk=$(awk -v min="$min_line" '
		/<trace-log-generated/ { block = "" }
		{
			if (NR >= min) {
				block = block $0 "\n"
			}
		}
		/<\/trace-log-generated>/ {
			if (NR >= min && block ~ /<is-notification-last>[[:space:]]*true[[:space:]]*<\// && block ~ /<log-file-name>/) {
				last = block
			}
			block = ""
		}
		END { if (last != "") print last }
	' "$LOG")
	if [[ -n "$blk" ]]; then
		_trace_name_from_block "$blk"
	fi
}

_trace_serial_num() {
	local name="${1##*/}"
	if [[ "$name" =~ ^trace\.log\.([0-9]+)$ ]]; then
		printf '%s' "${BASH_REMATCH[1]}"
		return 0
	fi
	return 1
}

_last_trace_serial_gt_first() {
	local cand="$1" first="$2"
	local sc fc
	sc=$(_trace_serial_num "$cand") || return 1
	fc=$(_trace_serial_num "$first") || return 1
	(( 10#${sc} > 10#${fc} ))
}

_max_trace_serial_in_log() {
	awk '
		/<trace-log-generated/ { block = "" }
		{ block = block $0 "\n" }
		/<\/trace-log-generated>/ {
			if (block ~ /<log-file-name>/) {
				n = block
				sub(/^.*<log-file-name>[[:space:]]*/, "", n)
				sub(/[[:space:]]*<\/log-file-name>.*/, "", n)
				sub(/^.*\//, "", n)
				if (n ~ /^trace\.log\.[0-9]+$/) {
					sub(/^trace\.log\./, "", n)
					if (n + 0 > max) max = n + 0
				}
			}
			block = ""
		}
		END { print (max + 0) }
	' "$LOG"
}

_count_trace_last_true() {
	grep -c -a -F "true</is-notification-last></trace-log-generated></notification>" "$LOG" 2>/dev/null || true
}

_count_trace_notifications() {
	grep -c -a -F "<trace-log-generated xmlns=\"urn:o-ran:trace:1.0\">" "$LOG" 2>/dev/null || true
}

_trace_notifications_since() {
	local seen="${1:-0}"
	awk -v seen="$seen" '
		/<trace-log-generated xmlns="urn:o-ran:trace:1.0">/ { inb=1; blk=""; next }
		inb { blk = blk $0 "\n" }
		/<\/trace-log-generated>/ && inb {
			cnt++
			if (cnt > seen) {
				name = blk
				last = blk
				sub(/^.*<log-file-name>[[:space:]]*/, "", name)
				sub(/[[:space:]]*<\/log-file-name>.*/, "", name)
				sub(/^.*\//, "", name)
				sub(/^.*<is-notification-last>[[:space:]]*/, "", last)
				sub(/[[:space:]]*<\/is-notification-last>.*/, "", last)
				if (name != "" && last != "") {
					printf("[INFO]\tTrace noti received: %s (last=%s)\n", name, last)
				}
			}
			inb=0
			blk=""
		}
	' "$LOG"
}

# 0=OK, 1=timeout, 2=trace ended before file landed (first upload only)
_wait_trace_sftp_upload() {
	local label="$1"
	local receiver_file="$2"
	local notif_before="$3"
	local detect_trace_end="${4:-1}"
	local _w _cur
	for _w in $(seq 1 3000); do
		if [[ -f "${receiver_file}" ]]; then
			echo "[OK]	${label}: SFTP file present (${receiver_file})" >&2
			return 0
		fi
		if (( detect_trace_end )) && grep -a -F "true</is-notification-last></trace-log-generated></notification>" "$LOG" >/dev/null 2>&1; then
			echo "[WARN]	${label}: is-notification-last=true before SFTP file ready (${receiver_file})" >&2
			return 2
		fi
		_cur=$(grep -c -a -F "<status>SUCCESS</status></file-upload-notification></notification>" "$LOG" 2>/dev/null) || true
		_cur=${_cur:-0}
		if (( _cur > notif_before )); then
			if (( _w % 25 == 0 )); then
				echo "[INFO]	${label}: upload-notification seen; waiting for ${receiver_file}" >&2
			fi
		elif (( _w == 1 || _w % 50 == 0 )); then
			echo "[INFO]	${label}: waiting (notification + ${receiver_file}, ${_w}/3000)" >&2
		fi
		if (( _w % 150 == 0 && _w > 0 )); then
			echo "[INFO]	${label}: still waiting SFTP (${receiver_file}, ${_w}/3000)" >&2
			ls -la "$(dirname "${receiver_file}")/" 2>/dev/null | head -8 | sed 's/^/[INFO]	  /' >&2 || true
		fi
		sleep 0.2
	done
	return 1
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
		sudo kill -TERM "$CLI_PID" 2>/dev/null || true
		sleep 0.3 || true
		sudo kill -KILL "$CLI_PID" 2>/dev/null || true
		wait "$CLI_PID" 2>/dev/null || true
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
		if [[ "${ORU_LOG_BOOST:-0}" == "1" ]]; then
			echo "[INFO]	ORU log boost (GUI 1s only); no netopeer watch show system"
		else
			echo "[INFO]	O-RU log boost off — no continuous show system (Conformance 설정에서만 켜짐)"
		fi
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
TRACE_NOTI_SEEN=$(_count_trace_notifications)
TRACE_NOTI_SEEN=${TRACE_NOTI_SEEN:-0}
for _w in $(seq 1 3000); do
	_noti_now=$(_count_trace_notifications)
	_noti_now=${_noti_now:-0}
	if (( _noti_now > TRACE_NOTI_SEEN )); then
		_trace_notifications_since "$TRACE_NOTI_SEEN"
		TRACE_NOTI_SEEN=$_noti_now
	fi
	if grep -a -F "$PAT_FIRST_TRACE" "$LOG" >/dev/null 2>&1; then
		RESULT3="OK"
		LOG_FILE="$(_extract_first_false_trace_log)"
		break
	fi
	sleep 0.2
done
echo "[$RESULT3]	Step 3.	First Trace-log generate ( ${LOG_FILE:-?} )."

if [[ "$RESULT3" != "OK" || -z "$LOG_FILE" ]]; then
	test_fail "First trace log notification"
	exit 1
fi

LOG_BASENAME="${LOG_FILE##*/}"
FIRST_TRACE_BASENAME="${LOG_BASENAME}"
LOCAL_LOGICAL_PATH="${LOCAL_LOG_PREFIX}/${LOG_BASENAME}"
REMOTE_RECEIVER_FILE="${REMOTE_UPLOAD_DIR}/${LOG_BASENAME}"

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

echo "[INFO]	SFTP receiver check: ${REMOTE_RECEIVER_FILE}"
rm -f "${REMOTE_RECEIVER_FILE}" 2>/dev/null || true
FILE_UPLOAD_OUT="${NETCONF_TMP}/edit/file_upload_out.xml"
rm -f "$FILE_UPLOAD_OUT"
send_cmd "user-rpc --content $FILE_UPLOAD_RPC --out $FILE_UPLOAD_OUT"

RESULT0="NOK"
PAT_FILE_MGMT_OK='<status xmlns="urn:o-ran:file-management:1.0">SUCCESS</status>'
for _w in $(seq 1 60); do
	if [[ -f "$FILE_UPLOAD_OUT" ]] && grep -a -F "$PAT_FILE_MGMT_OK" "$FILE_UPLOAD_OUT" >/dev/null 2>&1; then
		RESULT0="OK"
		echo "[OK]	file-upload RPC accepted ( ${LOG_BASENAME} )."
		echo "[INFO]	Step 4: RU → SFTP → miniDU file (not Step 3 trace notification)."
		echo "[WAIT]	Wait for SFTP file: sftp://$FILE_ID@$FILE_SERVER_IP${REMOTE_UPLOAD_DIR}/$LOG_BASENAME"
		echo "[INFO]	local-logical-file-path on RU: ${LOCAL_LOGICAL_PATH}"
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "File upload RPC (first)"
	exit 1
fi

RESULT4="NOK"
SKIP_FIRST_UPLOAD=0
UPLOAD_NOTIF_COUNT_BEFORE=$(grep -c -a -F "<status>SUCCESS</status></file-upload-notification></notification>" "$LOG" 2>/dev/null) || true
UPLOAD_NOTIF_COUNT_BEFORE=${UPLOAD_NOTIF_COUNT_BEFORE:-0}
_wait_trace_sftp_upload "first upload" "${REMOTE_RECEIVER_FILE}" "$UPLOAD_NOTIF_COUNT_BEFORE" 1
_wait_rc=$?
if [[ "$_wait_rc" -eq 0 ]]; then
	RESULT4="OK"
elif [[ "$_wait_rc" -eq 2 ]]; then
	echo "[WARN]	Step 4 skipped: RU already sent last trace before first SFTP completed"
	SKIP_FIRST_UPLOAD=1
	RESULT4="SKIP"
else
	RESULT4="NOK"
fi
echo "[$RESULT4]	Step 4.	File Upload ( ${LOG_BASENAME} )."

if [[ "$RESULT4" == "NOK" ]]; then
	ls -la "${REMOTE_UPLOAD_DIR}/" 2>/dev/null | head -15 | sed 's/^/[INFO]	  /' || true
	test_fail "File upload (first)"
	exit 1
fi

TRACE_ORU_END_TIMEOUT="${TRACE_ORU_END_TIMEOUT:-${TRACE_WAIT_SECOND_SEC:-600}}"
TRACE_ORU_END_TIMEOUT="${TRACE_ORU_END_TIMEOUT//[^0-9]/}"
[[ -n "$TRACE_ORU_END_TIMEOUT" ]] || TRACE_ORU_END_TIMEOUT=600

LOG_LINE_AFTER_STEP4=$(wc -l <"$LOG" 2>/dev/null | tr -d '[:space:]')
LOG_LINE_AFTER_STEP4=${LOG_LINE_AFTER_STEP4:-0}
TRACE_LAST_TRUE_BEFORE_END=$(_count_trace_last_true)
TRACE_LAST_TRUE_BEFORE_END=${TRACE_LAST_TRUE_BEFORE_END:-0}
echo "[INFO]	client will NOT send stop-trace-logs (wait for O-RU to end trace)."
echo "[INFO]	last=true count after first upload: ${TRACE_LAST_TRUE_BEFORE_END}"

RESULT5="NOK"
LOG_FILE=""
echo "[WAIT]	Wait for O-RU to end trace (new is-notification-last=true, max ${TRACE_ORU_END_TIMEOUT}s)."
TRACE_NOTI_SEEN=$(_count_trace_notifications)
TRACE_NOTI_SEEN=${TRACE_NOTI_SEEN:-0}
for _w in $(seq 1 $(( TRACE_ORU_END_TIMEOUT * 5 ))); do
	_noti_now=$(_count_trace_notifications)
	_noti_now=${_noti_now:-0}
	if (( _noti_now > TRACE_NOTI_SEEN )); then
		_trace_notifications_since "$TRACE_NOTI_SEEN"
		TRACE_NOTI_SEEN=$_noti_now
	fi
	_cur_last=$(_count_trace_last_true)
	_cur_last=${_cur_last:-0}
	if (( _cur_last > TRACE_LAST_TRUE_BEFORE_END )); then
		_cand="$(_extract_last_true_trace_log_after_line "$LOG_LINE_AFTER_STEP4")"
		_cand="${_cand##*/}"
		if [[ "$_cand" == "trace.log" ]]; then
			echo "[NOK]	ORU ended with aggregate trace.log (early stop on O-RU)."
			test_fail "ORU self-stop with aggregate trace.log"
			exit 1
		fi
		if [[ -n "$_cand" ]] && _last_trace_serial_gt_first "$_cand" "$FIRST_TRACE_BASENAME"; then
			LOG_FILE="$_cand"
			RESULT5="OK"
			echo "[OK]	ORU self-stop: last trace ( ${LOG_FILE} )."
			break
		fi
		if [[ -n "$_cand" ]] && (( _w % 50 == 0 )); then
			echo "[INFO]	ORU last=true seen (${_cand}); need trace.log.NNNN > ${FIRST_TRACE_BASENAME}"
		fi
	fi
	sleep 0.2
done
echo "[$RESULT5]	Step 5.	Last Trace-log generated ( ${LOG_FILE:-?} )."

if [[ "$RESULT5" != "OK" || -z "$LOG_FILE" ]]; then
	test_fail "ORU did not self-stop trace (no valid last=true with trace.log.NNNN > ${FIRST_TRACE_BASENAME})"
	exit 1
fi

LOG_BASENAME="${LOG_FILE##*/}"
LOCAL_LOGICAL_PATH="${LOCAL_LOG_PREFIX}/${LOG_BASENAME}"
REMOTE_RECEIVER_FILE="${REMOTE_UPLOAD_DIR}/${LOG_BASENAME}"

FILE_UPLOAD_RPC2="${NETCONF_TMP}/edit/file_upload2.xml"
cat > "$FILE_UPLOAD_RPC2" <<EORPC
<file-upload xmlns="urn:o-ran:file-management:1.0" xmlns:ict="urn:ietf:params:xml:ns:yang:ietf-crypto-types">
  <local-logical-file-path>${LOCAL_LOGICAL_PATH}</local-logical-file-path>
  <remote-file-path>sftp://${FILE_ID}@${FILE_SERVER_IP}${REMOTE_UPLOAD_DIR}/${LOG_BASENAME}</remote-file-path>
  <password>
    <password>${FILE_PW}</password>
  </password>
</file-upload>
EORPC

echo "[INFO]	SFTP receiver check: ${REMOTE_RECEIVER_FILE}"
rm -f "${REMOTE_RECEIVER_FILE}" 2>/dev/null || true
FILE_UPLOAD_OUT2="${NETCONF_TMP}/edit/file_upload_out2.xml"
rm -f "$FILE_UPLOAD_OUT2"
send_cmd "user-rpc --content $FILE_UPLOAD_RPC2 --out $FILE_UPLOAD_OUT2"

RESULT0="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$FILE_UPLOAD_OUT2" ]] && grep -a -F "$PAT_FILE_MGMT_OK" "$FILE_UPLOAD_OUT2" >/dev/null 2>&1; then
		RESULT0="OK"
		echo "[OK]	file-upload RPC accepted ( ${LOG_BASENAME} )."
		echo "[WAIT]	Wait for SFTP file: sftp://$FILE_ID@$FILE_SERVER_IP${REMOTE_UPLOAD_DIR}/$LOG_BASENAME"
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "File upload RPC (last)"
	exit 1
fi

RESULT6="NOK"
UPLOAD_NOTIF_COUNT_BEFORE2=$(grep -c -a -F "<status>SUCCESS</status></file-upload-notification></notification>" "$LOG" 2>/dev/null) || true
UPLOAD_NOTIF_COUNT_BEFORE2=${UPLOAD_NOTIF_COUNT_BEFORE2:-0}
_wait_trace_sftp_upload "last upload" "${REMOTE_RECEIVER_FILE}" "$UPLOAD_NOTIF_COUNT_BEFORE2" 0
_wait_rc=$?
if [[ "$_wait_rc" -eq 0 ]]; then
	RESULT6="OK"
else
	RESULT6="NOK"
fi
echo "[$RESULT6]	Step 6.	File Upload ( ${LOG_BASENAME} )."

if [[ "$RESULT6" != "OK" ]]; then
	ls -la "${REMOTE_UPLOAD_DIR}/" 2>/dev/null | head -15 | sed 's/^/[INFO]	  /' || true
	test_fail "File upload (last)"
	exit 1
fi

if [[ "$SKIP_FIRST_UPLOAD" -eq 1 ]]; then
	echo "[WARN]	PASS with first upload skipped (RU finished trace before first SFTP file landed)"
fi

echo "[PASS]"

echo "[INFO] 3.1.12.2 Trace Test completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
