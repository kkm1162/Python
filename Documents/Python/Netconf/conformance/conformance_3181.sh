#!/usr/bin/env bash
# O-RAN M-Plane 3.1.8.1 — Sudo positive (user creation + NACM verification)
set -u
set -o pipefail

TESTID="3181"
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
conformance_callhome_set_listen_mark
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout 300"

RESULT1=$(conformance_callhome_wait_step1 1500)
echo "[$RESULT1] STEP 1. The Netconf Client receive the CallHome from ORU"
if [[ "$RESULT1" != "OK" ]]; then
	test_fail "Call Home"
	exit 1
fi

RESULT2=$(conformance_callhome_wait_auth 150)
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
# Phase 2: Create users (edit_user_mgmt)
###############################################################################
EDIT_USER_MGMT="${NETCONF_TMP}/edit/edit_user_mgmt.xml"
EDIT_USER_MGMT_OUT="${NETCONF_TMP}/get/edit_user_mgmt_out.xml"
cat > "$EDIT_USER_MGMT" <<'EORPC'
<edit-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <target><running/></target>
  <config>
    <users xmlns="urn:o-ran:user-mgmt:1.0">
      <user><name>nmsuser</name><password>nms-password</password></user>
      <user><name>fmpmuser</name><password>fm-pm-password</password></user>
      <user><name>swmuser</name><password>swm-password</password></user>
      <user><name>smouser</name><password>smo-password</password></user>
      <user><name>hybridoduuser</name><password>hybrid-odu-password</password></user>
      <user><name>sudouser</name><password>sudo-password</password></user>
    </users>
  </config>
</edit-config>
EORPC

rm -f "$EDIT_USER_MGMT_OUT"
send_cmd "user-rpc --content $EDIT_USER_MGMT --out $EDIT_USER_MGMT_OUT"

RESULT_UMG="NOK"
for _w in $(seq 1 50); do
	if [[ -f "$EDIT_USER_MGMT_OUT" ]] && grep -aq "OK" "$EDIT_USER_MGMT_OUT" 2>/dev/null; then
		RESULT_UMG="OK"
		break
	fi
	sleep 0.2
done
if [[ "$RESULT_UMG" != "OK" ]]; then
	test_fail "edit user management"
	exit 1
fi

###############################################################################
# Phase 3: Assign groups (edit_user_group)
###############################################################################
EDIT_USER_GRP="${NETCONF_TMP}/edit/edit_user_group.xml"
EDIT_USER_GRP_OUT="${NETCONF_TMP}/get/edit_user_group_out.xml"
cat > "$EDIT_USER_GRP" <<'EORPC'
<edit-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <target><running/></target>
  <config>
    <nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm">
      <groups>
        <group><name>nms</name><user-name>nmsuser</user-name></group>
        <group><name>fm-pm</name><user-name>fmpmuser</user-name></group>
        <group><name>swm</name><user-name>swmuser</user-name></group>
        <group><name>smo</name><user-name>smouser</user-name></group>
        <group><name>hybrid-odu</name><user-name>hybridoduuser</user-name></group>
        <group><name>sudo</name><user-name>sudouser</user-name></group>
      </groups>
    </nacm>
  </config>
</edit-config>
EORPC

rm -f "$EDIT_USER_GRP_OUT"
send_cmd "user-rpc --content $EDIT_USER_GRP --out $EDIT_USER_GRP_OUT"

RESULT_UGR="NOK"
for _w in $(seq 1 50); do
	if [[ -f "$EDIT_USER_GRP_OUT" ]] && grep -aq "OK" "$EDIT_USER_GRP_OUT" 2>/dev/null; then
		RESULT_UGR="OK"
		break
	fi
	sleep 0.2
done
if [[ "$RESULT_UGR" != "OK" ]]; then
	test_fail "edit user group"
	exit 1
fi

###############################################################################
# STEP 3: Re-get NACM and verify all users/groups
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

echo "[PASS]"
echo "[INFO] 3.1.8.1 Sudo positive completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
