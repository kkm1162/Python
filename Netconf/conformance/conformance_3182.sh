#!/usr/bin/env bash
# O-RAN M-Plane 3.1.8.2 — Sudo negative (password visibility check)
set -u
set -o pipefail

TESTID="3182"
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

USER="sudouser"
PASSWORD="sudo-password"

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
	if [[ "$COPROC_READY" == "1" ]]; then
		send_cmd "disconnect" 2>/dev/null || true
		sleep 1 || true
		exec 3>&- 2>/dev/null || true
	fi
	if [[ -n "${WATCHDOG_PID:-}" ]]; then
		kill "$WATCHDOG_PID" 2>/dev/null || true
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
echo "[$RESULT1] STEP 1. The Netconf Client receive the CallHome from ORU"
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
echo "[$RESULT2] STEP 2. Successfully login with the correct username and password ($USER / ***)"
if [[ "$RESULT2" != "OK" ]]; then
	test_fail "login"
	exit 1
fi

sleep 5

send_cmd "subscribe --stream NETCONF"
for _w in $(seq 1 300); do
	if grep -a -F "OK" "$LOG" >/dev/null 2>&1; then break; fi
	sleep 0.2
done

mkdir -p "${NETCONF_TMP}/edit" "${NETCONF_TMP}/get"
WD_RPC="${NETCONF_TMP}/edit/watchdog_reset.xml"
cat > "$WD_RPC" <<'EORPC'
<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>
EORPC

(
_wd_last=0
while true; do
	sleep 2 || break
	_wd_cur=$(grep -c -a -F '<supervision-notification xmlns="urn:o-ran:supervision:1.0"/>' "$LOG" 2>/dev/null) || _wd_cur=0
	if (( _wd_cur > _wd_last )); then
		_wd_last=$_wd_cur
		echo "user-rpc --content $WD_RPC" >&3 2>/dev/null || true
		echo "Client SENT : user-rpc --content $WD_RPC" >>"$LOG" 2>&1
	fi
done
) &
WATCHDOG_PID=$!

###############################################################################
# STEP 3: Get user data and verify no password nodes visible (sudouser)
###############################################################################
GET_USER_RPC="${NETCONF_TMP}/get/get_user.xml"
GET_USER_OUT="${NETCONF_TMP}/get/get_user_out.xml"
cat > "$GET_USER_RPC" <<'EORPC'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <filter type="subtree">
    <users xmlns="urn:o-ran:user-mgmt:1.0"/>
  </filter>
</get>
EORPC

rm -f "$GET_USER_OUT"
send_cmd "user-rpc --content $GET_USER_RPC --out $GET_USER_OUT"

RESULT3="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$GET_USER_OUT" ]]; then
		if grep -aq "</data>" "$GET_USER_OUT" 2>/dev/null; then
			RESULT3="OK"
			break
		fi
	fi
	sleep 0.5
done
if [[ "$RESULT3" != "OK" ]]; then
	test_fail "get user data"
	exit 1
fi

if (( $(xmlstarlet sel -t -v "count(/*[local-name()='data']/*[local-name()='users']/*[local-name()='user']/*[local-name()='password'])" -n "$GET_USER_OUT") )); then
	RESULT3="NOK"
fi
echo "[$RESULT3] STEP 3. Check \"password\" node."
if [[ "$RESULT3" != "OK" ]]; then
	test_fail "password node visible"
	exit 1
fi

echo "[PASS]"
echo "[INFO] 3.1.8.2 Sudo negative completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
