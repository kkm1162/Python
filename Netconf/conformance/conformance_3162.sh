#!/usr/bin/env bash
# O-RAN M-Plane 3.1.6.2 — O-RU Software Update (negative)
set -u
set -o pipefail

TESTID="3162"
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

# Negative test: non-existent file to trigger FILE_NOT_FOUND
SWM_NOTFOUND_PATH="${SW_PKG_NEGATIVE_NOTFOUND_PATH:-${SWM_PATH%/*}/NotExistFile_NEGATIVE_TEST.EXT}"

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

GET_SLOTS_RPC="${NETCONF_TMP}/get/get_software_slots.xml"
cat > "$GET_SLOTS_RPC" <<'EORPC'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <filter type="subtree">
    <software-inventory xmlns="urn:o-ran:software-management:1.0">
      <software-slot/>
    </software-inventory>
  </filter>
</get>
EORPC

_rpc_has_error() {
	local f="$1"
	[[ -f "$f" ]] || return 1
	grep -aq "<rpc-error" "$f" 2>/dev/null
}

# Last <install-event> status/error-message from session log (not download COMPLETED).
_swm_last_install_event_fields() {
	local log="$1"
	python3 - "$log" <<'PY'
import re, sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
events = re.findall(
    r"<install-event\b[^>]*>([\s\S]*?)</install-event>", text, flags=re.IGNORECASE
)
if not events:
    print("\t")
    raise SystemExit(0)
last = events[-1]
sm = re.search(r"<status>([^<]*)</status>", last, flags=re.IGNORECASE)
em = re.search(r"<error-message>([^<]*)</error-message>", last, flags=re.IGNORECASE)
status = (sm.group(1).strip() if sm else "")
err = (em.group(1).strip() if em else "")
print(f"{status}\t{err}")
PY
}

_swm_install_reply_status() {
	local f="$1"
	[[ -f "$f" ]] || return 0
	python3 - "$f" <<'PY'
import re, sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
m = re.search(
    r"<status\b[^>]*xmlns=[\"']urn:o-ran:software-management:1.0[\"'][^>]*>([^<]*)</status>",
    text,
    flags=re.IGNORECASE,
)
if not m:
    m = re.search(r"<status>([^<]*)</status>", text, flags=re.IGNORECASE)
print((m.group(1).strip() if m else ""))
PY
}

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
if [[ "$RESULT3" == "OK" ]]; then
	RUNNINGSLOT=$(xmlstarlet sel -N x="urn:o-ran:software-management:1.0" -t -v "//x:name" "$GET_RUNNING_OUT" 2>/dev/null) || true
fi
echo "[$RESULT3] STEP 3. Check Running Slot, ${RUNNINGSLOT:-unknown} is Running now."
if [[ "$RESULT3" != "OK" ]]; then
	test_fail "get running slot"
	exit 1
fi

########################################################################
# STEP 3.1. Get Slot States (running/active flags)
########################################################################
GET_SLOTS_OUT="${NETCONF_TMP}/get/get_software_slots_out.xml"
rm -f "$GET_SLOTS_OUT"
send_cmd "user-rpc --content $GET_SLOTS_RPC --out $GET_SLOTS_OUT"
RESULT3A="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$GET_SLOTS_OUT" ]] && grep -aq "</data>" "$GET_SLOTS_OUT" 2>/dev/null; then
		RESULT3A="OK"
		break
	fi
	sleep 0.5
done
if [[ "$RESULT3A" != "OK" ]]; then
	test_fail "get software slots"
	exit 1
fi

_pick_install_slot() {
	local xml="$1"
	# Choose a slot with running=false AND active=false.
	# ACORN RU also exposes read-only slots (e.g. "factory"). Installation must target first/second only.
	xmlstarlet sel -N x="urn:o-ran:software-management:1.0" \
		-t -m "//x:software-slot[(x:name='first' or x:name='second') and x:running='false' and x:active='false']" \
		-v "x:name" -n \
		"$xml" 2>/dev/null | head -n 1 | tr -d '\r\n '
}

########################################################################
# STEP 4. Software Download — non-existent file (expect FILE_NOT_FOUND)
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
# STEP 5. Software Install (expect install rejection: INTEGRITY_ERROR, FILE_ERROR, …)
########################################################################
NONRUNNINGSLOT="$(_pick_install_slot "$GET_SLOTS_OUT")"
if [[ -z "${NONRUNNINGSLOT:-}" ]]; then
	# Prevent indefinite wait: RU rejects install unless active=false and running=false.
	test_fail "no installable slot among {first,second} (need running=false & active=false)"
	exit 1
fi
if [[ "$NONRUNNINGSLOT" != "first" && "$NONRUNNINGSLOT" != "second" ]]; then
	test_fail "invalid install slot chosen: $NONRUNNINGSLOT (must be first/second only)"
	exit 1
fi

SW_INSTALL_RPC="${NETCONF_TMP}/edit/software_install.xml"
cat > "$SW_INSTALL_RPC" <<EORPC
<software-install xmlns="urn:o-ran:software-management:1.0">
  <slot-name>${NONRUNNINGSLOT}</slot-name>
  <file-names>${SWMFILE}</file-names>
</software-install>
EORPC

SW_INSTALL_OUT="${NETCONF_TMP}/edit/software_install_out.xml"
rm -f "$SW_INSTALL_OUT"
send_cmd "user-rpc --content $SW_INSTALL_RPC --out $SW_INSTALL_OUT"
echo "      Software Installation Started   .slot = $NONRUNNINGSLOT"

# If install RPC was rejected, fail fast (GUI should not keep waiting).
for _w in $(seq 1 80); do
	if [[ -f "$SW_INSTALL_OUT" ]] && (grep -aq "<ok/>" "$SW_INSTALL_OUT" 2>/dev/null || _rpc_has_error "$SW_INSTALL_OUT"); then
		break
	fi
	sleep 0.2
done
if _rpc_has_error "$SW_INSTALL_OUT"; then
	ERR_RPC=$(grep -aoP '(?<=<error-message[^>]*>).*?(?=</error-message>)' "$SW_INSTALL_OUT" 2>/dev/null | tail -1) || true
	echo "[FAIL] software-install RPC rejected: ${ERR_RPC:-rpc-error}"
	exit 1
fi

REPLY_STATUS="$(_swm_install_reply_status "$SW_INSTALL_OUT")"
if [[ -n "${REPLY_STATUS:-}" && "$REPLY_STATUS" != "STARTED" ]]; then
	echo "[FAIL] software-install RPC reply status=${REPLY_STATUS} (expected async install-event with install failure)"
	test_fail "unexpected install RPC reply status: $REPLY_STATUS"
	exit 1
fi

RESULT6="NOK"
SWMSTATUS6=""
ERR_MSG6=""
for _w in $(seq 1 1000); do
	if grep -a -F "</install-event></notification>" "$LOG" >/dev/null 2>&1; then
		IFS=$'\t' read -r SWMSTATUS6 ERR_MSG6 < <(_swm_last_install_event_fields "$LOG")
		break
	fi
	sleep 0.2
done

if [[ -z "${SWMSTATUS6:-}" ]]; then
	echo "[FAIL] STEP 6: no install-event notification (expected install failure event)"
	test_fail "missing install-event"
	exit 1
fi

# Negative test PASS: O-RU rejects install (any failure status). FAIL only if install succeeds.
case "$SWMSTATUS6" in
	INTEGRITY_ERROR|FILE_ERROR|FILE_NOT_FOUND|FAILED|APPLICATION_ERROR)
		RESULT6="OK"
		;;
	COMPLETED|VALID)
		echo "[FAIL] STEP 6: install should have failed for negative PKG, got ${SWMSTATUS6}"
		test_fail "install succeeded unexpectedly (negative PKG)"
		exit 1
		;;
	*)
		if [[ "$ERR_MSG6" == *"Can't extract download software package."* ]]; then
			RESULT6="OK"
		else
			echo "[FAIL] STEP 6: unknown install status ${SWMSTATUS6:-?} (${ERR_MSG6:-no error-message})"
			test_fail "negative install (expected rejection status or extraction error)"
			exit 1
		fi
		;;
esac

echo "[$RESULT6] STEP 6. Installation Normally Failed.	.status = ${SWMSTATUS6}, .error-message = ${ERR_MSG6:-none}"
if [[ "$RESULT6" != "OK" ]]; then
	test_fail "negative install (install rejection expected)"
	exit 1
fi

echo "[PASS]"
echo "[INFO] 3.1.6.2 O-RU Software Update (negative) completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
