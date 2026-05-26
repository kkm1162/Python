#!/usr/bin/env bash
# O-RAN M-Plane 3.1.8.6 — Sudo hierarchical positive (multi-session user verification)
set -u
set -o pipefail

TESTID="3186"
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
		sleep 3 || true
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
PAT_ACCEPT="Accepted a connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
for _w in $(seq 1 1500); do
	if grep -a -F "$PAT_ACCEPT" "$LOG" >/dev/null 2>&1; then
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
# Phase 1: Get NACM and clean unexpected users
###############################################################################
GET_NACM_RPC="${NETCONF_TMP}/get/get_nacm.xml"
GET_NACM_OUT="${NETCONF_TMP}/get/get_nacm_out.xml"
cat > "$GET_NACM_RPC" <<'EORPC'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <filter type="subtree">
    <nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm"/>
  </filter>
</get>
EORPC

rm -f "$GET_NACM_OUT"
send_cmd "user-rpc --content $GET_NACM_RPC --out $GET_NACM_OUT"

RESULT_NACM1="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$GET_NACM_OUT" ]]; then
		if grep -aq "</data>" "$GET_NACM_OUT" 2>/dev/null; then
			RESULT_NACM1="OK"
			break
		fi
	fi
	sleep 0.5
done
if [[ "$RESULT_NACM1" != "OK" ]]; then
	test_fail "get NACM (cleanup phase)"
	exit 1
fi

unset USER_LIST MAPPINGS FOUND_LIST 2>/dev/null || true
declare -A USER_LIST
declare -A FOUND_LIST
USER_LIST["admin"]="admin"
USER_LIST["root"]="admin"
USER_LIST["__nc"]="admin"
USER_LIST["oranuser"]="sudo"
if [[ "${CONFORMANCE_V11_ORANUSER_AT_DOMAIN:-1}" == "1" ]]; then
	USER_LIST["oranuser@o-ran.org"]="sudo"
fi
USER_LIST["nmsuser"]="nms"
USER_LIST["fmpmuser"]="fm-pm"
USER_LIST["swmuser"]="swm"
USER_LIST["smouser"]="smo"
USER_LIST["hybridoduuser"]="hybrid-odu"
USER_LIST["sudouser"]="sudo"

mapfile -t MAPPINGS < <(xmlstarlet sel -t -m "//*[local-name()='group']" \
	-m "*[local-name()='user-name']" \
	-v "concat(../*[local-name()='name'], ':', .)" -n "$GET_NACM_OUT")

for item in "${MAPPINGS[@]}"; do
	group="${item%:*}"
	user="${item#*:}"
	FOUND_LIST["$user"]="$group"
	if [[ ! -v USER_LIST["$user"] ]]; then
		echo "[INFO]    user-name : $user has been removed."
		EDIT_DEL_NACM="${NETCONF_TMP}/edit/edit_delete_nacm_mod.xml"
		EDIT_DEL_OUT="${NETCONF_TMP}/get/edit_delete_nacm_out.xml"
		cat > "$EDIT_DEL_NACM" <<EORPC
<edit-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <target><running/></target>
  <config>
    <nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm">
      <groups>
        <group>
          <name>${group}</name>
          <user-name xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">${user}</user-name>
        </group>
      </groups>
    </nacm>
  </config>
</edit-config>
EORPC
		rm -f "$EDIT_DEL_OUT"
		send_cmd "user-rpc --content $EDIT_DEL_NACM --out $EDIT_DEL_OUT"

		for _w in $(seq 1 50); do
			if [[ -f "$EDIT_DEL_OUT" ]] && grep -aq "OK" "$EDIT_DEL_OUT" 2>/dev/null; then
				break
			fi
			sleep 0.2
		done
	fi
done

###############################################################################
# Phase 2: Create additional users (oranuser2, oranuser3)
###############################################################################
EDIT_USER_MGMT2="${NETCONF_TMP}/edit/edit_user_mgmt2.xml"
EDIT_USER_MGMT2_OUT="${NETCONF_TMP}/get/edit_user_mgmt2_out.xml"
cat > "$EDIT_USER_MGMT2" <<'EORPC'
<edit-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <target><running/></target>
  <config>
    <users xmlns="urn:o-ran:user-mgmt:1.0">
      <user><name>oranuser2</name><password>oran-password2</password></user>
      <user><name>oranuser3</name><password>oran-password3</password></user>
    </users>
  </config>
</edit-config>
EORPC

rm -f "$EDIT_USER_MGMT2_OUT"
send_cmd "user-rpc --content $EDIT_USER_MGMT2 --out $EDIT_USER_MGMT2_OUT"

RESULT_UMG2="NOK"
for _w in $(seq 1 50); do
	if [[ -f "$EDIT_USER_MGMT2_OUT" ]] && grep -aq "OK" "$EDIT_USER_MGMT2_OUT" 2>/dev/null; then
		RESULT_UMG2="OK"
		break
	fi
	sleep 0.2
done
if [[ "$RESULT_UMG2" != "OK" ]]; then
	test_fail "edit user management (oranuser2/3)"
	exit 1
fi

###############################################################################
# Phase 3: Assign groups for new users
###############################################################################
EDIT_USER_GRP2="${NETCONF_TMP}/edit/edit_user_group2.xml"
EDIT_USER_GRP2_OUT="${NETCONF_TMP}/get/edit_user_group2_out.xml"
cat > "$EDIT_USER_GRP2" <<'EORPC'
<edit-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <target><running/></target>
  <config>
    <nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm">
      <groups>
        <group><name>sudo</name><user-name>oranuser2</user-name></group>
        <group><name>carrier</name><user-name>oranuser3</user-name></group>
      </groups>
    </nacm>
  </config>
</edit-config>
EORPC

rm -f "$EDIT_USER_GRP2_OUT"
send_cmd "user-rpc --content $EDIT_USER_GRP2 --out $EDIT_USER_GRP2_OUT"

RESULT_UGR2="NOK"
for _w in $(seq 1 50); do
	if [[ -f "$EDIT_USER_GRP2_OUT" ]] && grep -aq "OK" "$EDIT_USER_GRP2_OUT" 2>/dev/null; then
		RESULT_UGR2="OK"
		break
	fi
	sleep 0.2
done
if [[ "$RESULT_UGR2" != "OK" ]]; then
	test_fail "edit user group (oranuser2/3)"
	exit 1
fi

###############################################################################
# STEP 3: Re-get NACM and verify all users/groups (including oranuser2/3)
###############################################################################
rm -f "$GET_NACM_OUT"
send_cmd "user-rpc --content $GET_NACM_RPC --out $GET_NACM_OUT"

RESULT_NACM2="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$GET_NACM_OUT" ]]; then
		if grep -aq "</data>" "$GET_NACM_OUT" 2>/dev/null; then
			RESULT_NACM2="OK"
			break
		fi
	fi
	sleep 0.5
done
if [[ "$RESULT_NACM2" != "OK" ]]; then
	test_fail "get NACM (verify phase)"
	exit 1
fi

unset USER_LIST MAPPINGS FOUND_LIST 2>/dev/null || true
declare -A USER_LIST
declare -A FOUND_LIST
USER_LIST["admin"]="admin"
USER_LIST["root"]="admin"
USER_LIST["__nc"]="admin"
USER_LIST["oranuser"]="sudo"
if [[ "${CONFORMANCE_V11_ORANUSER_AT_DOMAIN:-1}" == "1" ]]; then
	USER_LIST["oranuser@o-ran.org"]="sudo"
fi
USER_LIST["nmsuser"]="nms"
USER_LIST["fmpmuser"]="fm-pm"
USER_LIST["swmuser"]="swm"
USER_LIST["smouser"]="smo"
USER_LIST["hybridoduuser"]="hybrid-odu"
USER_LIST["sudouser"]="sudo"
USER_LIST["oranuser2"]="sudo"
USER_LIST["oranuser3"]="carrier"

mapfile -t MAPPINGS < <(xmlstarlet sel -t -m "//*[local-name()='group']" \
	-m "*[local-name()='user-name']" \
	-v "concat(../*[local-name()='name'], ':', .)" -n "$GET_NACM_OUT")

for item in "${MAPPINGS[@]}"; do
	group="${item%:*}"
	user="${item#*:}"
	FOUND_LIST["$user"]="$group"
done

RESULT3="OK"
for master_node in "${!USER_LIST[@]}"; do
	sleep 0.1
	if [[ ! -v FOUND_LIST["$master_node"] ]]; then
		echo "[FAIL]    \"$master_node\" does not exist in O-RU."
		RESULT3="NOK"
	elif [[ "${FOUND_LIST[$master_node]}" != "${USER_LIST[$master_node]}" ]]; then
		echo "[FAIL]    \"$master_node\" should be \"${USER_LIST[$master_node]}\" group, result=${FOUND_LIST[$master_node]}"
		RESULT3="NOK"
	fi
done

echo "[$RESULT3] STEP 3. Create New Account"
if [[ "$RESULT3" == "NOK" ]]; then
	test_fail "Create New Account"
	exit 1
fi

###############################################################################
# STEP 4: Get user data and verify no password nodes visible
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

RESULT4="NOK"
for _w in $(seq 1 60); do
	if [[ -f "$GET_USER_OUT" ]]; then
		if grep -aq "</data>" "$GET_USER_OUT" 2>/dev/null; then
			RESULT4="OK"
			break
		fi
	fi
	sleep 0.5
done
if [[ "$RESULT4" != "OK" ]]; then
	test_fail "get user data"
	exit 1
fi

if (( $(xmlstarlet sel -t -v "count(/*[local-name()='data']/*[local-name()='users']/*[local-name()='user']/*[local-name()='password'])" -n "$GET_USER_OUT") )); then
	RESULT4="NOK"
fi
echo "[$RESULT4] STEP 4. Check \"password\" node."
if [[ "$RESULT4" != "OK" ]]; then
	test_fail "password node visible"
	exit 1
fi

###############################################################################
# Session 2: Disconnect and reconnect as oranuser2
###############################################################################
send_cmd "disconnect" 2>/dev/null || true
sleep 1 || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	sudo kill -15 "$NETOPEER_COPROC_PID" 2>/dev/null || true
	sleep 1 || true
	sudo kill -9 "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
if [[ -n "${WATCHDOG_PID:-}" ]]; then
	kill "$WATCHDOG_PID" 2>/dev/null || true
	WATCHDOG_PID=""
fi
COPROC_READY=0
exec 3>&- 2>/dev/null || true

echo "[WAIT]    Wait for Next Callhome ( oranuser2 )"
sleep 10

coproc NP2 {
	setsid stdbuf -oL sshpass -p "oran-password2" netopeer2-cli 2>&1
} >>"$LOG" 2>&1
NETOPEER_COPROC_PID="${NP2_PID:-$!}"
exec 3>&"${NP2[1]}"
COPROC_READY=1

send_cmd "verb 3"
send_cmd "knownhosts --mode skip"
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login oranuser2 --timeout 300"

RESULT5="NOK"
for _w in $(seq 1 1500); do
	if grep -a -F "$PAT_ACCEPT" "$LOG" >/dev/null 2>&1; then
		ACCEPT_COUNT=$(grep -c -a -F "$PAT_ACCEPT" "$LOG" 2>/dev/null) || ACCEPT_COUNT=0
		if (( ACCEPT_COUNT >= 2 )); then
			RESULT5="OK"
			break
		fi
	fi
	sleep 0.2
done
echo "[$RESULT5] STEP 5. The Netconf Client receive the CallHome from ORU"
if [[ "$RESULT5" != "OK" ]]; then
	test_fail "Call Home (oranuser2)"
	exit 1
fi

RESULT6="NOK"
for _w in $(seq 1 150); do
	AUTH_COUNT=$(grep -c -a -F "Authentication successful" "$LOG" 2>/dev/null) || AUTH_COUNT=0
	if (( AUTH_COUNT >= 2 )); then
		RESULT6="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT6] STEP 6. Successfully login with the correct username and password (oranuser2 / oran-password2)"
if [[ "$RESULT6" != "OK" ]]; then
	test_fail "login (oranuser2)"
	exit 1
fi

RESULT7="NOK"
for _w in $(seq 1 150); do
	if grep -a -E '</capabilities><session-id>[0-9]+</session-id></hello>' "$LOG" >/dev/null 2>&1; then
		HELLO_COUNT=$(grep -c -a -E '</capabilities><session-id>[0-9]+</session-id></hello>' "$LOG" 2>/dev/null) || HELLO_COUNT=0
		if (( HELLO_COUNT >= 2 )); then
			RESULT7="OK"
			break
		fi
	fi
	sleep 0.2
done
echo "[$RESULT7] STEP 7. Check Hello Message"
if [[ "$RESULT7" != "OK" ]]; then
	test_fail "Hello message (oranuser2)"
	exit 1
fi

###############################################################################
# Session 3: Disconnect and reconnect as oranuser3
###############################################################################
send_cmd "disconnect" 2>/dev/null || true
sleep 1 || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	sudo kill -15 "$NETOPEER_COPROC_PID" 2>/dev/null || true
	sleep 1 || true
	sudo kill -9 "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
COPROC_READY=0
exec 3>&- 2>/dev/null || true

echo "[WAIT]    Wait for Next Callhome ( oranuser3 )"
sleep 10

coproc NP2 {
	setsid stdbuf -oL sshpass -p "oran-password3" netopeer2-cli 2>&1
} >>"$LOG" 2>&1
NETOPEER_COPROC_PID="${NP2_PID:-$!}"
exec 3>&"${NP2[1]}"
COPROC_READY=1

send_cmd "verb 3"
send_cmd "knownhosts --mode skip"
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login oranuser3 --timeout 300"

RESULT8="NOK"
for _w in $(seq 1 1500); do
	ACCEPT_COUNT=$(grep -c -a -F "$PAT_ACCEPT" "$LOG" 2>/dev/null) || ACCEPT_COUNT=0
	if (( ACCEPT_COUNT >= 3 )); then
		RESULT8="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT8] STEP 8. The Netconf Client receive the CallHome from ORU"
if [[ "$RESULT8" != "OK" ]]; then
	test_fail "Call Home (oranuser3)"
	exit 1
fi

RESULT9="NOK"
for _w in $(seq 1 150); do
	AUTH_COUNT=$(grep -c -a -F "Authentication successful" "$LOG" 2>/dev/null) || AUTH_COUNT=0
	if (( AUTH_COUNT >= 3 )); then
		RESULT9="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT9] STEP 9. Successfully login with the correct username and password (oranuser3 / oran-password3)"
if [[ "$RESULT9" != "OK" ]]; then
	test_fail "login (oranuser3)"
	exit 1
fi

RESULT10="NOK"
for _w in $(seq 1 150); do
	HELLO_COUNT=$(grep -c -a -E '</capabilities><session-id>[0-9]+</session-id></hello>' "$LOG" 2>/dev/null) || HELLO_COUNT=0
	if (( HELLO_COUNT >= 3 )); then
		RESULT10="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT10] STEP 10.Check Hello Message"
if [[ "$RESULT10" != "OK" ]]; then
	test_fail "Hello message (oranuser3)"
	exit 1
fi

echo "[PASS]"
echo "[INFO] 3.1.8.6 Sudo hierarchical positive completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
