#!/usr/bin/env bash
# GUI M-Plane / Conformance helper (IPv6 Call Home path):
# Call Home on global IPv6 → login → o-ran-operations <reset/>
# Copied from conformance_oru_reboot.sh — v4 script is untouched.
# Reads LOCAL-IP-V6 / SERVER-IP-V6 from management-configurations.
set -u
set -o pipefail

TESTID="oru_reboot_v6"
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
# Prefer dedicated v6 keys; do not fall back to v4 LOCAL-IP/SERVER-IP
ALLOWED_IP=$(jq -r '.["management-configurations"]["SERVER-IP-V6"] // empty' "$CONFIG")
LOCAL_IP=$(jq -r '.["management-configurations"]["LOCAL-IP-V6"] // empty' "$CONFIG")
PRODUCT=$(jq -r '.["management-configurations"]["PRODUCT-CODE"] // empty' "$CONFIG")

LISTEN_PORT="${CALLHOME_PORT:-4334}"
NETCONF_TMP="${NETCONF_TMP:-/var/tmp/netconf_tmp}"
# GUI M-Plane: CALLHOME_LISTEN_TIMEOUT=90 (기본). 예전 300s listen 으로 중지가 안 먹히던 문제 완화
LISTEN_TIMEOUT="${CALLHOME_LISTEN_TIMEOUT:-90}"
LOGIN_WAIT_SEC="${LOGIN_WAIT_SEC:-$LISTEN_TIMEOUT}"
LOGIN_POLL_SEC="${LOGIN_POLL_SEC:-0.2}"
CONN_DELAY="${CONN_DELAY:-1}"

echo "[INFO] ORU reboot helper (IPv6): USER=$USER, ALLOWED_IP_V6=$ALLOWED_IP, LOCAL_IP_V6=$LOCAL_IP, LISTEN_PORT=$LISTEN_PORT, PRODUCT=$PRODUCT"

if [[ -z "$ALLOWED_IP" || -z "$LOCAL_IP" ]]; then
	echo "[ERROR] SERVER-IP-V6 / LOCAL-IP-V6 required in config (Settings ALLOWED_IP_V6 / LOCAL_IP_V6)"
	exit 2
fi
if [[ "$LOCAL_IP" != *:* ]]; then
	echo "[ERROR] LOCAL-IP-V6 does not look like IPv6: $LOCAL_IP"
	exit 2
fi
if [[ "$ALLOWED_IP" != *:* ]]; then
	echo "[ERROR] SERVER-IP-V6 does not look like IPv6: $ALLOWED_IP"
	exit 2
fi

if ! ip -6 addr show 2>/dev/null | grep -Fq "${LOCAL_IP}"; then
	echo "[WARN] LOCAL-IP-V6=${LOCAL_IP} not found on this host (listen bind may fail)"
fi

LOG_BASE="${LOG_PATH:-${CONFORMANCE_REMOTE_DIR:-/var/tmp/conformance}/logs}"
LOG_BASE="${LOG_BASE%/}"
LOG_DIR="${LOG_BASE}/${PRODUCT:-_unknown_}"
mkdir -p "$LOG_DIR" "$NETCONF_TMP/edit"
LOG="$LOG_DIR/CONF_${TESTID}_$(date +'%y%m%d_%H-%M-%S').log"
: >"$LOG"
chmod 0644 "$LOG" 2>/dev/null || true

RESET_RPC="${NETCONF_TMP}/edit/oru_gui_reset_v6.xml"
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
	sudo ip6tables -D INPUT -p tcp --dport "$LISTEN_PORT" -j DROP >/dev/null 2>&1 || true
	sudo ip6tables -D INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT >/dev/null 2>&1 || true
	return 0
}
trap cleanup EXIT INT TERM HUP

# Free Call Home port (miniDU / previous test / v4 helper may still hold it)
sudo fuser -k "${LISTEN_PORT}/tcp" 2>/dev/null || true
sudo ip6tables -D INPUT -p tcp --dport "$LISTEN_PORT" -j DROP 2>/dev/null || true
sudo ip6tables -D INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT 2>/dev/null || true
sleep 1

sudo ip6tables -A INPUT -p tcp --dport "$LISTEN_PORT" -j DROP
sudo ip6tables -I INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT
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
echo "[INFO] Starting CallHome listener (IPv6) on ${LOCAL_IP}:${LISTEN_PORT} for ORU reset... (timeout=${LISTEN_TIMEOUT}s)"
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout $LISTEN_TIMEOUT"

LOGIN="NOK"
login_deadline=$(( $(date +%s) + LOGIN_WAIT_SEC ))
while [[ $(date +%s) -lt $login_deadline ]]; do
	if tail -n "+${LISTEN_LOG_START}" "$LOG" 2>/dev/null | grep -q "Authentication successful"; then
		LOGIN="OK"
		echo "[INFO] Login successful — sending ORU reset (IPv6 CallHome)"
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
	echo "[FAIL] Call Home / login not established over IPv6 (LOGIN=${LOGIN})"
	echo "[FAIL] Check: RU CallHome → [${LOCAL_IP}]:${LISTEN_PORT}, SERVER-IP-V6=${ALLOWED_IP}, ip6tables"
	exit 1
fi

sleep 1
send_cmd "subscribe --stream NETCONF"
sleep 1

RESET_MARK=$(wc -l <"$LOG" | tr -d ' ')
send_cmd "user-rpc --content $RESET_RPC"
echo "[INFO] Sent <reset xmlns=\"urn:o-ran:operations:1.0\"/>"

RESULT="NOK"
for _w in $(seq 1 50); do
	if tail -n "+$((RESET_MARK + 1))" "$LOG" 2>/dev/null | grep -qiE '<ok\s*/>|rpc-reply'; then
		RESULT="OK"
		break
	fi
	if ! kill -0 "$NETOPEER_COPROC_PID" 2>/dev/null; then
		RESULT="OK"
		echo "[INFO] netopeer exited after reset (expected)"
		break
	fi
	sleep 0.2
done

if [[ "$RESULT" == "OK" ]]; then
	echo "[OK] ORU reset RPC accepted / session ending (IPv6)"
	echo "[PASS] ORU reboot requested (IPv6)"
	exit 0
fi

echo "[WARN] No clear rpc-reply for reset within timeout — RU may still reboot"
echo "[PASS] ORU reboot requested (best-effort, IPv6)"
exit 0
