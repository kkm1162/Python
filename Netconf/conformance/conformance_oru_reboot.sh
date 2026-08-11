#!/usr/bin/env bash
# GUI Conformance helper: Call Home → login → o-ran-operations <reset/>
# Used after a selected test when 「재부팅」 is checked.
set -u
set -o pipefail

TESTID="oru_reboot"
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
PRODUCT=$(jq -r '.["management-configurations"]["PRODUCT-CODE"] // empty' "$CONFIG")

LISTEN_PORT="${CALLHOME_PORT:-4334}"
NETCONF_TMP="${NETCONF_TMP:-/var/tmp/netconf_tmp}"
LOGIN_WAIT_SEC="${LOGIN_WAIT_SEC:-120}"
LOGIN_POLL_SEC="${LOGIN_POLL_SEC:-0.2}"
CONN_DELAY="${CONN_DELAY:-1}"

echo "[INFO] ORU reboot helper: USER=$USER, ALLOWED_IP=$ALLOWED_IP, LOCAL_IP=$LOCAL_IP, LISTEN_PORT=$LISTEN_PORT, PRODUCT=$PRODUCT"

LOG_BASE="${LOG_PATH:-${CONFORMANCE_REMOTE_DIR:-/var/tmp/conformance}/logs}"
LOG_BASE="${LOG_BASE%/}"
LOG_DIR="${LOG_BASE}/${PRODUCT:-_unknown_}"
mkdir -p "$LOG_DIR" "$NETCONF_TMP/edit"
LOG="$LOG_DIR/CONF_${TESTID}_$(date +'%y%m%d_%H-%M-%S').log"
: >"$LOG"
chmod 0644 "$LOG" 2>/dev/null || true

RESET_RPC="${NETCONF_TMP}/edit/oru_gui_reset.xml"
cat >"$RESET_RPC" <<'EORPC'
<reset xmlns="urn:o-ran:operations:1.0"/>
EORPC

send_cmd() {
	local cmd="$*"
	echo "Client SENT : $cmd" >>"$LOG" 2>&1
	set +u
	local _wfd="${NP2[1]:-}"
	set -u
	[[ -n "${_wfd}" ]] || return 0
	echo "$cmd" >&"${_wfd}" 2>/dev/null || true
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

# Free Call Home port (miniDU / previous test may still hold it)
sudo fuser -k "${LISTEN_PORT}/tcp" 2>/dev/null || true
sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -j DROP 2>/dev/null || true
sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT 2>/dev/null || true
sleep 1

sudo iptables -A INPUT -p tcp --dport "$LISTEN_PORT" -j DROP
sudo iptables -I INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT
if [[ "$CONN_DELAY" != "0" && -n "$CONN_DELAY" ]]; then
	sleep "$CONN_DELAY"
fi

coproc NP2 {
	setsid stdbuf -oL sshpass -p "$PASSWORD" netopeer2-cli 2>&1
} >>"$LOG" 2>&1
NETOPEER_COPROC_PID="${NP2_PID:-$!}"
exec 3>&"${NP2[1]}"
COPROC_READY=1

send_cmd "verb 3"
sleep 0.2
send_cmd "knownhosts --mode skip"
sleep 0.3

LISTEN_LOG_START=$(wc -l <"$LOG" | tr -d ' ')
echo "[INFO] Starting CallHome listener on ${LISTEN_PORT} for ORU reset..."
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout 300"

LOGIN="NOK"
login_deadline=$(( $(date +%s) + LOGIN_WAIT_SEC ))
while [[ $(date +%s) -lt $login_deadline ]]; do
	if tail -n "+${LISTEN_LOG_START}" "$LOG" 2>/dev/null | grep -q "Authentication successful"; then
		LOGIN="OK"
		echo "[INFO] Login successful — sending ORU reset"
		break
	fi
	if tail -n "+${LISTEN_LOG_START}" "$LOG" 2>/dev/null | grep -qiE "authentication failed|Authentication failed"; then
		echo "[ERROR] Authentication failed"
		LOGIN="FAIL"
		break
	fi
	sleep "$LOGIN_POLL_SEC"
done

if [[ "$LOGIN" != "OK" ]]; then
	echo "[FAIL] Call Home / login not established (LOGIN=${LOGIN})"
	exit 1
fi

sleep 1
send_cmd "subscribe --stream NETCONF"
sleep 1

RESET_MARK=$(wc -l <"$LOG" | tr -d ' ')
send_cmd "user-rpc --content $RESET_RPC"
echo "[INFO] Sent <reset xmlns=\"urn:o-ran:operations:1.0\"/>"

# Wait briefly for rpc-reply ok / notification (RU may drop quickly)
RESULT="NOK"
for _w in $(seq 1 50); do
	if tail -n "+$((RESET_MARK + 1))" "$LOG" 2>/dev/null | grep -qiE '<ok\s*/>|rpc-reply'; then
		RESULT="OK"
		break
	fi
	# Session drop after reset is also success
	if ! kill -0 "$NETOPEER_COPROC_PID" 2>/dev/null; then
		RESULT="OK"
		echo "[INFO] netopeer exited after reset (expected)"
		break
	fi
	sleep 0.2
done

if [[ "$RESULT" == "OK" ]]; then
	echo "[OK] ORU reset RPC accepted / session ending"
	echo "[PASS] ORU reboot requested"
	exit 0
fi

echo "[WARN] No clear rpc-reply for reset within timeout — RU may still reboot"
echo "[PASS] ORU reboot requested (best-effort)"
exit 0
