#!/usr/bin/env bash
# O-RAN M-Plane 3.1.6.1 — O-RU Software Update (positive)
set -u
set -o pipefail

TESTID="3161"
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
        <running>true</running>
      </software-slot>
    </software-inventory>
  </filter>
</get>
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
PAT_ACCEPT="Accepted a connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
for _w in $(seq 1 1500); do
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
# STEP 3. Get Running Slot
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
RUNNINGVERSION=""
if [[ "$RESULT3" == "OK" ]]; then
	RUNNINGSLOT=$(xmlstarlet sel -N x="urn:o-ran:software-management:1.0" -t -v "//x:name" "$GET_RUNNING_OUT" 2>/dev/null) || true
	RUNNINGVERSION=$(xmlstarlet sel -N x="urn:o-ran:software-management:1.0" -t -v "//x:version" "$GET_RUNNING_OUT" 2>/dev/null) || true
fi
echo "[$RESULT3] STEP 3. Check Running Slot, ${RUNNINGSLOT:-unknown} is Running now. (Version: ${RUNNINGVERSION:-unknown})"
if [[ "$RESULT3" != "OK" ]]; then
	test_fail "get running slot"
	exit 1
fi

########################################################################
# STEP 3.5. Verify secure/nonsecure match
########################################################################
if [[ -n "$RUNNINGVERSION" && -n "$SWM_PATH" ]]; then
	RUN_SEC_TYPE=""
	if [[ "${RUNNINGVERSION,,}" == *"nonsecure"* ]]; then
		RUN_SEC_TYPE="nonsecure"
	elif [[ "${RUNNINGVERSION,,}" == *"secure"* ]]; then
		RUN_SEC_TYPE="secure"
	fi

	PKG_SEC_TYPE=""
	if [[ "${SWM_PATH,,}" == *"nonsecure"* ]]; then
		PKG_SEC_TYPE="nonsecure"
	elif [[ "${SWM_PATH,,}" == *"secure"* ]]; then
		PKG_SEC_TYPE="secure"
	fi

	if [[ -n "$RUN_SEC_TYPE" && -n "$PKG_SEC_TYPE" ]]; then
		if [[ "$RUN_SEC_TYPE" != "$PKG_SEC_TYPE" ]]; then
			echo "[FAIL] Running slot version ($RUNNINGVERSION) is $RUN_SEC_TYPE, but package ($SWM_PATH) is $PKG_SEC_TYPE. Mismatch!"
			test_fail "secure/nonsecure mismatch between running slot and install package"
			exit 1
		else
			echo "[INFO] Secure/nonsecure match verified: $RUN_SEC_TYPE."
		fi
	else
		echo "[WARN] Could not determine secure/nonsecure type for comparison. (RUN: ${RUN_SEC_TYPE:-none}, PKG: ${PKG_SEC_TYPE:-none})"
	fi
fi

########################################################################
# STEP 4. Software Download
########################################################################
# SFTP URL → DU local path (GUI uploads to /tmp/netconf_PKG/ before test)
SWM_FS_PATH=""
if [[ "$SWM_PATH" =~ ^sftp://[^@]+@[^/]+(/.*)$ ]]; then
	SWM_FS_PATH="${BASH_REMATCH[1]}"
fi
if [[ -n "$SWM_FS_PATH" ]]; then
	if [[ -f "$SWM_FS_PATH" ]]; then
		SWM_FS_SIZE=$(stat -c%s "$SWM_FS_PATH" 2>/dev/null || wc -c <"$SWM_FS_PATH")
		echo "[INFO] PKG on SFTP server: $SWM_FS_PATH ($SWM_FS_SIZE bytes) — skip re-upload if unchanged"
	else
		echo "[FAIL] PKG not on SFTP server: $SWM_FS_PATH"
		test_fail "PKG missing on server (run Conformance to upload or copy PKG to /tmp/netconf_PKG/)"
		exit 1
	fi
fi
# Build download URL with embedded credentials (user:password@host format)
# SWM_PATH is like sftp://user@ip/path — inject password after user
if [[ "$SWM_PATH" =~ ^(sftp://[^:@]+)(:[^@]+)?(@.*)$ ]]; then
    # 만약 기존 경로에 비밀번호가 포함되어 있다면 제거하고 유저와 호스트 정보만 추출합니다.
    SWM_DOWNLOAD_URL="${BASH_REMATCH[1]}${BASH_REMATCH[3]}"
else
    SWM_DOWNLOAD_URL="${SWM_PATH}"
fi

echo "[INFO] Download URL: ${SWM_DOWNLOAD_URL}"

SW_DOWNLOAD_RPC="${NETCONF_TMP}/edit/software_download.xml"
cat > "$SW_DOWNLOAD_RPC" <<EORPC
<software-download xmlns="urn:o-ran:software-management:1.0">
  <remote-file-path>${SWM_DOWNLOAD_URL}</remote-file-path>
    <password>
    <password>${SWM_PASSWORD}</password>
    </password>
</software-download>
EORPC

send_cmd "user-rpc --content $SW_DOWNLOAD_RPC"
echo "		Software Download Started."

RESULT4="NOK"
SWMFILE=""
for _w in $(seq 1 500); do
	if grep -a -F "<status>COMPLETED</status></download-event></notification>" "$LOG" >/dev/null 2>&1; then
		SWMFILE=$(grep -oP '(?<=<file-name>).*?(?=</file-name>)' "$LOG" 2>/dev/null | tail -1) || true
		RESULT4="OK"
		break
	fi
	sleep 0.2
done

echo "[$RESULT4] STEP 4. Download Completed,		.filename = ${SWMFILE:-unknown}"
if [[ "$RESULT4" != "OK" ]]; then
	test_fail "software download"
	exit 1
fi

########################################################################
# STEP 5. Software Install
########################################################################
# 추가된 부분: RUNNINGSLOT 변수에서 첫 번째 줄(실제 슬롯 이름)만 추출하여 공백/줄바꿈을 제거합니다.
ACTUAL_RUNNINGSLOT=$(echo "$RUNNINGSLOT" | head -n 1 | tr -d '\r\n ')

# 조건문을 정제된 변수(ACTUAL_RUNNINGSLOT) 기준으로 변경합니다.
if [[ "$ACTUAL_RUNNINGSLOT" == "first" ]]; then
    NONRUNNINGSLOT="second"
else
    NONRUNNINGSLOT="first"
fi

SW_INSTALL_RPC="${NETCONF_TMP}/edit/software_install.xml"
cat > "$SW_INSTALL_RPC" <<EORPC
<software-install xmlns="urn:o-ran:software-management:1.0">
  <slot-name>${NONRUNNINGSLOT}</slot-name>
  <file-names>${SWMFILE}</file-names>
</software-install>
EORPC

send_cmd "user-rpc --content $SW_INSTALL_RPC"
echo "      Software Installation Started   .slot = $NONRUNNINGSLOT"

RESULT5="NOK"
for _w in $(seq 1 1000); do
	if grep -a -F "<status>COMPLETED</status></install-event></notification>" "$LOG" >/dev/null 2>&1; then
		RESULT5="OK"
		break
	fi
	sleep 0.2
done

echo "[$RESULT5] STEP 5. Installation Completed."
if [[ "$RESULT5" != "OK" ]]; then
	test_fail "software install"
	exit 1
fi

########################################################################
# STEP 6. Verify Non-Running Slot Status (VALID)
########################################################################
GET_NONRUNNING_RPC="${NETCONF_TMP}/get/get_nonrunning_slot.xml"
cat > "$GET_NONRUNNING_RPC" <<EORPC
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

GET_NONRUNNING_OUT="${NETCONF_TMP}/get/get_nonrunning_slot_out.xml"
rm -f "$GET_NONRUNNING_OUT"
send_cmd "user-rpc --content $GET_NONRUNNING_RPC --out $GET_NONRUNNING_OUT"

RESULT6="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$GET_NONRUNNING_OUT" ]]; then
		if grep -aq "</data>" "$GET_NONRUNNING_OUT" 2>/dev/null; then
			break
		fi
	fi
	sleep 0.5
done

SLOT_STATUS=""
if [[ -f "$GET_NONRUNNING_OUT" ]]; then
	SLOT_STATUS=$(xmlstarlet sel -N x="urn:o-ran:software-management:1.0" -t -v "//x:status" "$GET_NONRUNNING_OUT" 2>/dev/null) || true
fi

if [[ "$SLOT_STATUS" == "VALID" ]]; then
	RESULT6="OK"
	echo "[$RESULT6] STEP 6. Check Software Validate."
else
	RESULT6="NOK"
	echo "[$RESULT6] STEP 6. Check Software Validate.	.status = ${SLOT_STATUS:-empty}"
fi

if [[ "$RESULT6" != "OK" ]]; then
	test_fail "software validation"
	exit 1
fi

echo "[PASS]"
echo "[INFO] 3.1.6.1 O-RU Software Update (positive) completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
