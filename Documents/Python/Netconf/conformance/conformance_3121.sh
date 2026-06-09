#!/usr/bin/env bash
# O-RAN M-Plane 3.1.2.1 — Subscription (create-subscription over Call Home)
set -u
set -o pipefail

TESTID="3121"
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
CONN_DELAY="${CONN_DELAY:-3}"
POST_LISTEN_WAIT="${CONFORMANCE_POST_LISTEN_WAIT:-15}"
if ! [[ "$POST_LISTEN_WAIT" =~ ^[0-9]+$ ]]; then
	POST_LISTEN_WAIT=15
fi
if [[ "$POST_LISTEN_WAIT" -gt 120 ]]; then
	POST_LISTEN_WAIT=120
fi

echo "[INFO] USER=$USER, ALLOWED_IP=$ALLOWED_IP, LOCAL_IP=$LOCAL_IP, LISTEN_PORT=$LISTEN_PORT (Call Home), NETCONF_PORT=$NETCONF_PORT (JSON PORT), PRODUCT=$PRODUCT"
echo "[INFO] CONN_DELAY=${CONN_DELAY}s, post_listen_wait=${POST_LISTEN_WAIT}s (GUI Conformance post_listen_wait)"

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

mkdir -p "${NETCONF_TMP}/edit"
SUB_RPC="${NETCONF_TMP}/edit/subscription_3121_rpc.xml"
OUT_RPC="${NETCONF_TMP}/edit/subscription_3121_out.xml"
rm -f "$OUT_RPC"
# user-rpc --content 는 “<rpc> 래퍼 없이” 연산 노드만 넣어야 함. 전체 <rpc> 를 넣으면
# netopeer2-cli: Node "rpc" not found in the "ietf-netconf" module / Failed to create RPC
cat >"$SUB_RPC" <<'EOSUB'
<create-subscription xmlns="urn:ietf:params:xml:ns:netconf:notification:1.0">
  <stream>NETCONF</stream>
</create-subscription>
EOSUB
chmod 0644 "$SUB_RPC" 2>/dev/null || true

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
echo "[INFO] Waiting ${CONN_DELAY}s for network stabilization (iptables applied)..."
sleep "$CONN_DELAY"

coproc NP2 {
	setsid stdbuf -oL sshpass -p "$PASSWORD" netopeer2-cli 2>&1
} >>"$LOG" 2>&1
NETOPEER_COPROC_PID="${NP2_PID:-$!}"
exec 3>&"${NP2[1]}"
COPROC_READY=1

send_cmd "verb 3"
sleep 1
send_cmd "knownhosts --mode skip"
sleep 1
conformance_callhome_set_listen_mark
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout 300"
sleep 3
if [[ "$POST_LISTEN_WAIT" -gt 0 ]]; then
	echo "[INFO] post_listen_wait ${POST_LISTEN_WAIT}s (ORU Call Home 재시도 대기)..."
	sleep "$POST_LISTEN_WAIT"
fi

RESULT1=$(conformance_callhome_wait_step1 450)

echo "STEP 1. Criteria : The Netconf Client receive the CallHome from ORU"
echo "STEP 1. CallHome : $RESULT1"
if [[ "$RESULT1" != "OK" ]]; then
	conformance_callhome_print_hints
	test_fail "Call Home"
	exit 1
fi

RESULT2=$(conformance_callhome_wait_auth 120)

echo "[$RESULT2] STEP 2. Successfully login with the correct username and password ($USER / ***)"
if [[ "$RESULT2" != "OK" ]]; then
	test_fail "login"
	exit 1
fi

sleep 2
send_cmd "user-rpc --content $SUB_RPC --out $OUT_RPC"

RESULT3="NOK"
for _w in $(seq 1 120); do
	if [[ -f "$OUT_RPC" ]]; then
		# netopeer2-cli --out: XML <ok/> 이거나, 사람이 읽기 쉬운 한 줄 "OK" 만 쓰는 경우가 있음
		if grep -a "<ok/>" "$OUT_RPC" >/dev/null 2>&1 \
			|| grep -aqi "<rpc-reply[^>]*>.*<ok" "$OUT_RPC" >/dev/null 2>&1 \
			|| grep -aE '^[[:space:]]*OK[[:space:]]*$' "$OUT_RPC" >/dev/null 2>&1; then
			RESULT3="OK"
			break
		fi
		if grep -a "<rpc-error" "$OUT_RPC" >/dev/null 2>&1; then
			test_fail "create-subscription rpc-error (see $OUT_RPC)"
			exit 1
		fi
	fi
	sleep 0.2
done

echo "STEP 3. Criteria : Create Subscription"
echo "STEP 3. Subscription : $RESULT3"
if [[ "$RESULT3" != "OK" ]]; then
	if [[ ! -f "$OUT_RPC" ]]; then
		echo "[INFO] 출력 파일이 없습니다: $OUT_RPC"
		echo "[INFO] user-rpc가 RPC 생성/전송 단계에서 실패하면 netopeer2-cli가 --out 파일을 만들지 않는 경우가 많습니다."
		echo "[INFO] 이 세션 로그($LOG)에서 'user-rpc' 직후의 ly ERROR / nc ERROR / Failed to create RPC / Failed to send 줄을 확인하세요."
	else
		echo "[INFO] 출력 파일($OUT_RPC) 앞부분:"
		head -n 40 "$OUT_RPC" 2>/dev/null | sed 's/^/  | /' || true
	fi
	test_fail "subscription (out: $OUT_RPC)"
	exit 1
fi

echo "[INFO] 3.1.2.1 completed. Detailed log: $LOG"
trap - EXIT INT TERM
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
