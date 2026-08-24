#!/usr/bin/env bash
# O-RAN M-Plane 3.1.3.2 — M-Plane supervision (negative): subscribe + 1회 supervision 후 watchdog, 이후 세션 EOF
set -u
set -o pipefail

TESTID="3132"
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
WT_XML="${SUPERVISION_RESET:-${NETCONF_TMP}/supervision_reset.xml}"

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

# 이전 중단된 실행의 잔여 프로세스/포트 정리
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
for _w in $(seq 1 300); do
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

RESULT2="NOK"
for _w in $(seq 1 120); do
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

sleep 2
send_cmd "subscribe --stream NETCONF"

RESULT3="NOK"
for _w in $(seq 1 120); do
	if grep -a "<ok/>" "$LOG" >/dev/null 2>&1; then
		RESULT3="OK"
		break
	fi
	sleep 0.2
done

echo "STEP 3. Criteria : Create Subscription"
echo "STEP 3. Subscription : $RESULT3"
if [[ "$RESULT3" != "OK" ]]; then
	test_fail "subscribe"
	exit 1
fi

# 부정 시험: N회 supervision 알림 + watchdog → STEP4 OK, 이후 watchdog 안 보내서 세션 끊김 기대
# pretty-print body 줄 "^<supervision-notification" 으로 1건당 1줄만 카운트
# N회 watchdog 유지 후 중단 → RU supervision 실패(EOF) 유도
# SUPERVISION_NEEDED(시험⚙) → SUPERVISION_RESET_CYCLES(전역) 순 (NEGATIVE_FAIL_ON_CYCLE=3 은 사용 안 함)
NEEDED="${SUPERVISION_NEEDED:-${SUPERVISION_RESET_CYCLES:-30}}"
SUPERVISION_INTERVAL="${SUPERVISION_INTERVAL:-60}"
[[ "${NEEDED}" =~ ^[0-9]+$ ]] && (( NEEDED > 0 )) || NEEDED=30
[[ "${SUPERVISION_INTERVAL}" =~ ^[0-9]+$ ]] || SUPERVISION_INTERVAL=60
_per_cycle=$(( SUPERVISION_INTERVAL + 130 ))
_max_sec=$(( NEEDED * _per_cycle + 120 ))
if (( _max_sec < 600 )); then
	_max_sec=600
fi
_to_iter=$(( _max_sec * 4 ))
echo "[INFO] SUPERVISION_NEEDED=${NEEDED} (env NEEDED='${SUPERVISION_NEEDED:-}' RESET_CYCLES='${SUPERVISION_RESET_CYCLES:-}') interval=${SUPERVISION_INTERVAL}s wait_max=${_max_sec}s (phase=watchdog)"
notif_seen=0
RESULT4="NOK"
for _w in $(seq 1 "$_to_iter"); do
	_cnt=$(grep -acE '^\s*<supervision-notification' "$LOG" 2>/dev/null)
	if [[ "${_cnt:-0}" =~ ^[0-9]+$ ]] && (( _cnt > notif_seen )); then
		send_cmd "user-rpc --content $WT_XML --out ${NETCONF_TMP}/watchdog_rpc_reply.xml"
		notif_seen=$_cnt
		echo "[INFO] supervision notification #${notif_seen}/${NEEDED} detected, watchdog sent"
		if (( notif_seen >= NEEDED )); then
			RESULT4="OK"
			break
		fi
	fi
	sleep 0.25
done
if [[ "$RESULT4" != "OK" ]]; then
	echo "[WARN] supervision incomplete: seen=${notif_seen} needed=${NEEDED} (wait_max=${_max_sec}s)"
fi

echo "STEP 4. Criteria : Supervision Notification"
echo "STEP 4. Supervision : $RESULT4"
if [[ "$RESULT4" != "OK" ]]; then
	test_fail "supervision"
	exit 1
fi

# watchdog 안 보내면 ORU가 세션 끊음 → EOF 발생. supervision interval + guard 여유
_eof_max_sec=$(( SUPERVISION_INTERVAL + 200 ))
if (( _eof_max_sec < 600 )); then
	_eof_max_sec=600
fi
_eof_iter=$(( _eof_max_sec * 4 ))
echo "[INFO] waiting EOF after watchdog stop: max=${_eof_max_sec}s"
RESULT5="NOK"
EOF_PAT="SSH channel unexpected EOF."
for _w in $(seq 1 "$_eof_iter"); do
	if grep -a -F "$EOF_PAT" "$LOG" >/dev/null 2>&1; then
		RESULT5="OK"
		break
	fi
	sleep 0.25
done

echo "STEP 5. Criteria : Supervision Failure"
echo "STEP 5. Supervision : $RESULT5"
if [[ "$RESULT5" != "OK" ]]; then
	test_fail "expected EOF after supervision failure"
	exit 1
fi

echo "[INFO] 3.1.3.2 negative path completed. Detailed log: $LOG"
trap - EXIT INT TERM
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
