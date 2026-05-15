#!/usr/bin/env bash
# O-RAN M-Plane 3.1.13.1 — Ethernet Connectivity Monitoring (LBM/LBR)
set -u
set -o pipefail

TESTID="31131"
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
LOCAL_IF=$(jq -r '.["management-configurations"]["LOCAL-IF"] // empty' "$CONFIG")

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
		wait "$WATCHDOG_PID" 2>/dev/null || true
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

mkdir -p "${NETCONF_TMP}/edit" "${NETCONF_TMP}/get"

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

INIT_UPLANE_OUT="${NETCONF_TMP}/edit/edit-init-uplane-conf.xml"
rm -f "$INIT_UPLANE_OUT"
cp /mplane_automation/miniDU/edit/edit_init_uplane_conf.xml "${NETCONF_TMP}/edit/edit_init_uplane_conf.xml" 2>/dev/null || true
send_cmd "user-rpc --content ${NETCONF_TMP}/edit/edit_init_uplane_conf.xml --out $INIT_UPLANE_OUT"
RESULT0="NOK"
for _w in $(seq 1 20); do
	if [[ -f "$INIT_UPLANE_OUT" ]] && grep -aq "OK" "$INIT_UPLANE_OUT" 2>/dev/null; then
		RESULT0="OK"
		break
	fi
	sleep 0.5
done
if [[ "$RESULT0" != "OK" ]]; then
	test_fail "Initialize uplane conf"
	exit 1
fi

declare -a ToDUifname ToDUifvlan

for ((i=0; i < $(jq -r '.["interface-configurations"]["to-DU-interface"].name | length // empty' "$CONFIG"); i++)); do
	ToDUifname[$i]=$(jq -r '.["interface-configurations"]["to-DU-interface"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	ToDUifvlan[$i]=$(jq -r '.["interface-configurations"]["to-DU-interface"].vlan['"$i"'] | to_entries[].value // empty' "$CONFIG")
	if [[ -n "${ToDUifname[$i]:-}" ]]; then
		cp /mplane_automation/miniDU/edit/edit_delete_if_org.xml "${NETCONF_TMP}/edit/edit_delete_if_mod.xml" 2>/dev/null || true
		sed -i "s/IFNAME/${ToDUifname[$i]}_${ToDUifvlan[$i]}/g" "${NETCONF_TMP}/edit/edit_delete_if_mod.xml"

		DEL_IF_OUT="${NETCONF_TMP}/edit/edit-delete-if.xml"
		rm -f "$DEL_IF_OUT"
		send_cmd "user-rpc --content ${NETCONF_TMP}/edit/edit_delete_if_mod.xml --out $DEL_IF_OUT"
		RESULT0="NOK"
		for _w in $(seq 1 20); do
			if [[ -f "$DEL_IF_OUT" ]] && grep -aq "OK" "$DEL_IF_OUT" 2>/dev/null; then
				RESULT0="OK"
				break
			fi
			sleep 0.5
		done
		if [[ "$RESULT0" != "OK" ]]; then
			test_fail "Delete existing interface $i"
			exit 1
		fi
	fi
done

GET_MP_VER_RPC="${NETCONF_TMP}/get/get_mp_version.xml"
cat > "$GET_MP_VER_RPC" <<'EORPC'
<get xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <filter type="subtree">
    <operational-info xmlns="urn:o-ran:operations:1.0">
      <supported-mplane-version/>
    </operational-info>
  </filter>
</get>
EORPC

GET_MP_VER_OUT="${NETCONF_TMP}/get/get_mp_version_out.xml"
rm -f "$GET_MP_VER_OUT"
send_cmd "user-rpc --content $GET_MP_VER_RPC --out $GET_MP_VER_OUT"
for _w in $(seq 1 20); do
	if [[ -f "$GET_MP_VER_OUT" ]] && grep -aq "</data>" "$GET_MP_VER_OUT" 2>/dev/null; then
		break
	fi
	sleep 0.5
done
MPVERSION=$(xmlstarlet sel -N x="urn:o-ran:operations:1.0" -t -v "//x:supported-mplane-version" "$GET_MP_VER_OUT" 2>/dev/null) || true
MPVERSIONNUM=$((${MPVERSION//./}))

declare -a ToDUpename ToDUodumac MAC Port

if $(jq -r '.["interface-configurations"]["to-DU-interface"]["enable"] // empty' "$CONFIG") ; then

for ((i=0; i < $(jq -r '.["interface-configurations"]["to-DU-interface"].name | length // empty' "$CONFIG"); i++)); do
	ToDUifname[$i]=$(jq -r '.["interface-configurations"]["to-DU-interface"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	ToDUifvlan[$i]=$(jq -r '.["interface-configurations"]["to-DU-interface"].vlan['"$i"'] | to_entries[].value // empty' "$CONFIG")
	ToDUpename[$i]=$(jq -r '.["processing-element-configurations"]["to-DU-processing-element"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	ToDUodumac[$i]=$(jq -r '.["processing-element-configurations"]["to-DU-processing-element"].ODUMAC['"$i"'] | to_entries[].value // empty' "$CONFIG")

	if [[ -n "${ToDUifname[$i]:-}" ]]; then
		cp /mplane_automation/miniDU/get/get_interface_w_filter_org.xml "${NETCONF_TMP}/get/get_interface_w_filter_mod.xml" 2>/dev/null || true
		sed -i "s/get_interface_name/${ToDUifname[$i]}/g" "${NETCONF_TMP}/get/get_interface_w_filter_mod.xml"

		GET_IF_OUT="${NETCONF_TMP}/get/get_if.xml"
		rm -f "$GET_IF_OUT"
		send_cmd "user-rpc --content ${NETCONF_TMP}/get/get_interface_w_filter_mod.xml --out $GET_IF_OUT"
		for _w in $(seq 1 20); do
			if [[ -f "$GET_IF_OUT" ]] && grep -aq "</data>" "$GET_IF_OUT" 2>/dev/null; then
				break
			fi
			sleep 0.5
		done

		MAC[$i]=$(xmlstarlet sel -N x="urn:o-ran:interfaces:1.0" -N i="urn:ietf:params:xml:ns:yang:ietf-interfaces" -t -m "//i:interface[i:name='${ToDUifname[$i]}']" -v "//x:mac-address" "$GET_IF_OUT" 2>/dev/null) || true
		Port[$i]=$(xmlstarlet sel -N x="urn:o-ran:interfaces:1.0" -N i="urn:ietf:params:xml:ns:yang:ietf-interfaces" -t -m "//i:interface[i:name='${ToDUifname[$i]}']" -v "//x:port-number" "$GET_IF_OUT" 2>/dev/null) || true

		cp /mplane_automation/miniDU/edit/edit_interface_org.xml "${NETCONF_TMP}/edit/edit_interface_mod.xml" 2>/dev/null || true
		sed -i "s/IFNAME/${ToDUifname[$i]}_${ToDUifvlan[$i]}/g" "${NETCONF_TMP}/edit/edit_interface_mod.xml"
		sed -i "s/BASENAME/${ToDUifname[$i]}/g" "${NETCONF_TMP}/edit/edit_interface_mod.xml"
		sed -i "s/VLANID/${ToDUifvlan[$i]}/g" "${NETCONF_TMP}/edit/edit_interface_mod.xml"
		sed -i "s/ORUMAC/${MAC[$i]}/g" "${NETCONF_TMP}/edit/edit_interface_mod.xml"
		sed -i "s/PORTNUMBER/${Port[$i]}/g" "${NETCONF_TMP}/edit/edit_interface_mod.xml"

		EDIT_IF_OUT="${NETCONF_TMP}/edit/edit-if.xml"
		rm -f "$EDIT_IF_OUT"
		send_cmd "user-rpc --content ${NETCONF_TMP}/edit/edit_interface_mod.xml --out $EDIT_IF_OUT"

		RESULT_IF="NOK"
		for _w in $(seq 1 20); do
			if [[ -f "$EDIT_IF_OUT" ]] && grep -aq "OK" "$EDIT_IF_OUT" 2>/dev/null; then
				RESULT_IF="OK"
				echo "	Interface configurations Successed."
				break
			fi
			sleep 0.5
		done
		if [[ "$RESULT_IF" != "OK" ]]; then
			echo "	Interface configurations Failed."
			test_fail "Interface config"
			exit 1
		fi

		RESULT3="NOK"
		PAT_IF_NOTIF="interface[if:name='${ToDUifname[$i]}_${ToDUifvlan[$i]}']</target><operation>create</operation></edit></netconf-config-change>"
		for _w in $(seq 1 20); do
			if grep -a -F "$PAT_IF_NOTIF" "$LOG" >/dev/null 2>&1; then
				RESULT3="OK"
				break
			fi
			sleep 0.5
		done

		echo "[$RESULT3]	STEP 3.	Netconf config change notification is generated from O-RU ( create interface )"
		if [[ "$RESULT3" != "OK" ]]; then
			test_fail "Interface notification"
			exit 1
		fi

		if $(jq -r '.["processing-element-configurations"]["to-DU-processing-element"]["enable"] // empty' "$CONFIG") ; then
			cp /mplane_automation/miniDU/edit/edit_processing_element_org.xml "${NETCONF_TMP}/edit/edit_processing_element_mod.xml" 2>/dev/null || true
			sed -i "s/PENAME/${ToDUpename[$i]}/g" "${NETCONF_TMP}/edit/edit_processing_element_mod.xml"
			sed -i "s/IFNAME/${ToDUifname[$i]}_${ToDUifvlan[$i]}/g" "${NETCONF_TMP}/edit/edit_processing_element_mod.xml"
			sed -i "s/VLANID/${ToDUifvlan[$i]}/g" "${NETCONF_TMP}/edit/edit_processing_element_mod.xml"
			sed -i "s/ORUMAC/${MAC[$i]}/g" "${NETCONF_TMP}/edit/edit_processing_element_mod.xml"
			sed -i "s/ODUMAC/${ToDUodumac[$i]}/g" "${NETCONF_TMP}/edit/edit_processing_element_mod.xml"

			EDIT_PE_OUT="${NETCONF_TMP}/edit/edit-pe.xml"
			rm -f "$EDIT_PE_OUT"
			send_cmd "user-rpc --content ${NETCONF_TMP}/edit/edit_processing_element_mod.xml --out $EDIT_PE_OUT"
			RESULT_PE="NOK"
			for _w in $(seq 1 20); do
				if [[ -f "$EDIT_PE_OUT" ]] && grep -aq "OK" "$EDIT_PE_OUT" 2>/dev/null; then
					RESULT_PE="OK"
					echo "	Processing-element configurations Successed."
					break
				fi
				sleep 0.5
			done
			if [[ "$RESULT_PE" != "OK" ]]; then
				echo "	Processing-element configurations Failed."
				test_fail "PE config"
				exit 1
			fi
		fi

		RESULT4="NOK"
		PAT_PE_NOTIF="ru-elements[o-ran-elements:name='${ToDUpename[$i]}']</target><operation>create</operation></edit></netconf-config-change>"
		for _w in $(seq 1 20); do
			if grep -a -F "$PAT_PE_NOTIF" "$LOG" >/dev/null 2>&1; then
				RESULT4="OK"
				break
			fi
			sleep 0.5
		done

		echo "[$RESULT4]	STEP 4.	Netconf config change notification is generated from O-RU ( create proecssing-element )"
		if [[ "$RESULT4" != "OK" ]]; then
			test_fail "PE notification"
			exit 1
		fi
	fi
done
fi

MD_DATA_OUT="${NETCONF_TMP}/edit/edit_md_data_definitions.xml"
rm -f "$MD_DATA_OUT"
cp /mplane_automation/miniDU/edit/edit_md_data_definitions.xml "${NETCONF_TMP}/edit/edit_md_data_definitions_src.xml" 2>/dev/null || true
send_cmd "user-rpc --content ${NETCONF_TMP}/edit/edit_md_data_definitions_src.xml --out $MD_DATA_OUT"

RESULT5="NOK"
for _w in $(seq 1 20); do
	if [[ -f "$MD_DATA_OUT" ]] && grep -aq "OK" "$MD_DATA_OUT" 2>/dev/null; then
		RESULT5="OK"
		break
	fi
	sleep 0.5
done
echo "[$RESULT5]	STEP 5.	Create LBM"
if [[ "$RESULT5" != "OK" ]]; then
	test_fail "Create LBM"
	exit 1
fi

echo "[WAIT]	LBM Procedure."
LBM_LOG="${NETCONF_TMP}/LBM.log"
rm -f "$LBM_LOG"
sudo /mplane_automation/dot1ag-utils/src/ethping -i "$LOCAL_IF" -v "${ToDUifvlan[0]}" -l 7 -c 30 "${MAC[0]}" > "$LBM_LOG" 2>&1 || true

RESULT6="NOK"
for _w in $(seq 1 250); do
	if [[ -f "$LBM_LOG" ]] && grep -a -v "timeout for" "$LBM_LOG" 2>/dev/null | grep -a -F "${MAC[0]}" >/dev/null 2>&1; then
		RESULT6="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT6]	STEP 6.	CHECK if the O-RU transmitted LBR"
if [[ "$RESULT6" != "OK" ]]; then
	test_fail "LBR check"
	exit 1
fi

echo "[PASS]"

echo "[INFO] 3.1.13.1 Ethernet Connectivity Monitoring completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
