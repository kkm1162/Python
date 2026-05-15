#!/usr/bin/env bash
# O-RAN M-Plane 3.1.10.2 — O-RU configurability negative test (duplicate eAxC-ID rejection)
set -u
set -o pipefail

TESTID="31102"
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

ToDUifname_init=$(jq -r '.["interface-configurations"]["to-DU-interface"].name[0] | to_entries[].value // empty' "$CONFIG")
ToDUifvlan_init=$(jq -r '.["interface-configurations"]["to-DU-interface"].vlan[0] | to_entries[].value // empty' "$CONFIG")
cp /mplane_automation/miniDU/edit/edit_delete_if_org.xml "${NETCONF_TMP}/edit/edit_delete_if_mod.xml" 2>/dev/null || true
sed -i "s/IFNAME/${ToDUifname_init}_${ToDUifvlan_init}/g" "${NETCONF_TMP}/edit/edit_delete_if_mod.xml"

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
	test_fail "Delete existing interface"
	exit 1
fi

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

declare -a ToDUifname ToDUifvlan ToDUpename ToDUodumac MAC Port

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

FRAMESTRUCTURE=$(jq -r '.["uplane-configurations"]["frame-structure"] // empty' "$CONFIG")
CPTYPE=$(jq -r '.["uplane-configurations"]["cp-type"] // empty' "$CONFIG")
CPLENGTH=$(jq -r '.["uplane-configurations"]["cp-length"] // empty' "$CONFIG")
CPLENGTHOTHER=$(jq -r '.["uplane-configurations"]["cp-length-other"] // empty' "$CONFIG")
ALPHA=$(jq -r '.["uplane-configurations"]["downlink-radio-frame-offset"] // empty' "$CONFIG")
NTAOFFSET=$(jq -r '.["uplane-configurations"]["n-ta-offset"] // empty' "$CONFIG")
TDDPATTERN=$(jq -r '.["uplane-configurations"]["tdd-pattern"] // empty' "$CONFIG")
NUMOFDL=$(jq -r '.["uplane-configurations"]["number-of-dl-symbol"] // empty' "$CONFIG")
NUMOFUL=$(jq -r '.["uplane-configurations"]["number-of-ul-symbol"] // empty' "$CONFIG")
TDDSCS=$(jq -r '.["uplane-configurations"]["tdd-scs"] // empty' "$CONFIG")
TDDPATTERNID=$(jq -r '.["uplane-configurations"]["tdd-pattern-id"] // empty' "$CONFIG")

declare -a TXEPNAME TXEAXCID TXCOMPMETHOD TXCOMPBIT TXCOMPTYPE TXSCS TXPRB
declare -a TXARRAYNAME TXARRAYCENTER TXARRAYBW TXARRAYPATTERN TXARRAYGAIN TXARRAYTECH
declare -a TXLINKNAME TXLINKEP TXLINKARRAY TXLINKPE

for ((i=0; i < 1; i++)); do
	TXEPNAME[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXEAXCID[$i]=$(( $(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].endpoint_id['"$i"'] | to_entries[].value // empty' "$CONFIG") ))
	TXCOMPMETHOD[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].compression_method['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXCOMPBIT[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].compression_bit['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXCOMPTYPE[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].compression_type['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXSCS[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].Sub_Carrier_Spacing['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXPRB[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].Number_Of_PRB['"$i"'] | to_entries[].value // empty' "$CONFIG")

	TXARRAYNAME[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYCENTER[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].Center_frequency['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYBW[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].BandWidth['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYPATTERN[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].pattern['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYGAIN[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].Gain['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYTECH[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].Tech['"$i"'] | to_entries[].value // empty' "$CONFIG")

	TXLINKNAME[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXLINKEP[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].endpoints['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXLINKARRAY[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].tx_array_carriers['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXLINKPE[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].processing_element['"$i"'] | to_entries[].value // empty' "$CONFIG")

	cp /mplane_automation/miniDU/edit/edit_pdsch_org.xml "${NETCONF_TMP}/edit/edit_pdsch_mod.xml" 2>/dev/null || true
	sed -i "s/UPENDPOINT/${TXEPNAME[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCOMPTYPE/${TXCOMPTYPE[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	if (( MPVERSIONNUM >= 800 )); then
		sed -i "s/UPCOMPMETHOD/${TXCOMPMETHOD[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	else
		if [[ "${TXCOMPMETHOD[$i]}" == "NO_COMPRESSION" ]]; then
			sed -i "/<compression-method>UPCOMPMETHOD<\/compression-method>/d" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
		else
			sed -i "s/<compression-method>UPCOMPMETHOD<\/compression-method>/<exponent>4<\/exponent>/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
		fi
	fi

	sed -i "s/UPCOMPBIT/${TXCOMPBIT[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPFRAMESTRUCTURE/${FRAMESTRUCTURE}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCPTYPE/${CPTYPE}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCPLENGTH/${CPLENGTH}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCPOTHERLENGTH/${CPLENGTHOTHER}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPSCS/KHZ_${TXSCS[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPNUMBEROFPRB/${TXPRB[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPEAXCID/${TXEAXCID[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPTXCARRIER/${TXARRAYNAME[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCENTERFREQ/$(echo "${TXARRAYCENTER[$i]}*10^6/1" | bc)/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPBANDWIDTH/$(echo "${TXARRAYBW[$i]}*10^6/1" | bc)/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPTECH/${TXARRAYTECH[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPALPHA/${ALPHA}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPGAIN/${TXARRAYGAIN[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"

	sed -i "s/UPLINK/${TXLINKNAME[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPPE/${TXLINKPE[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPTXCARRIER/${TXLINKARRAY[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPENDPOINT/${TXLINKEP[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"

	if [[ "${TXARRAYPATTERN[$i]}" == "TDD" ]]; then
		sed -i "s/UPTDDPATTERNID/$TDDPATTERNID/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	else
		sed -i "/<configurable-tdd-pattern>UPTDDPATTERNID<\/configurable-tdd-pattern>/d" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	fi

	PDSCH_OUT="${NETCONF_TMP}/edit/edit-pdsch.xml"
	rm -f "$PDSCH_OUT"
	send_cmd "user-rpc --content ${NETCONF_TMP}/edit/edit_pdsch_mod.xml --out $PDSCH_OUT"
	RESULT_PDSCH="NOK"
	for _w in $(seq 1 20); do
		if [[ -f "$PDSCH_OUT" ]] && grep -aq "OK" "$PDSCH_OUT" 2>/dev/null; then
			RESULT_PDSCH="OK"
			echo "	PDSCH ID $i Configurations Successed."
			break
		fi
		sleep 0.5
	done
	if [[ "$RESULT_PDSCH" != "OK" ]]; then
		echo "	PDSCH ID $i Configurations Failed."
		test_fail "PDSCH config"
		exit 1
	fi
done

RESULT5="NOK"
LAST_IDX=$(( i - 1 ))
PAT_TXLINK="low-level-tx-links[o-ran-uplane-conf:name='${TXLINKNAME[$LAST_IDX]}']</target><operation>create</operation>"
for _w in $(seq 1 20); do
	if grep -a -F "$PAT_TXLINK" "$LOG" >/dev/null 2>&1; then
		RESULT5="OK"
		break
	fi
	sleep 0.5
done
echo "[$RESULT5]	STEP 5.	Netconf config change notification is generated from O-RU ( create low-level-tx-links )"
if [[ "$RESULT5" != "OK" ]]; then
	test_fail "tx-links notification"
	exit 1
fi

RESULT6="NOK"
PAT_TXEP="low-level-tx-endpoints[o-ran-uplane-conf:name='${TXEPNAME[$LAST_IDX]}']</target><operation>create</operation>"
for _w in $(seq 1 20); do
	if grep -a -F "$PAT_TXEP" "$LOG" >/dev/null 2>&1; then
		RESULT6="OK"
		break
	fi
	sleep 0.5
done
echo "[$RESULT6]	STEP 6.	Netconf config change notification is generated from O-RU ( create low-level-tx-endpoints )"
if [[ "$RESULT6" != "OK" ]]; then
	test_fail "tx-endpoints notification"
	exit 1
fi

RESULT7="NOK"
PAT_TXARRAY="tx-array-carriers[o-ran-uplane-conf:name='${TXLINKARRAY[$LAST_IDX]}']</target><operation>create</operation>"
for _w in $(seq 1 20); do
	if grep -a -F "$PAT_TXARRAY" "$LOG" >/dev/null 2>&1; then
		RESULT7="OK"
		break
	fi
	sleep 0.5
done
echo "[$RESULT7]	STEP 7.	Netconf config change notification is generated from O-RU ( create tx-array-carriers )"
if [[ "$RESULT7" != "OK" ]]; then
	test_fail "tx-array-carriers notification"
	exit 1
fi

FRAMESTRUCTURE=$(jq -r '.["uplane-configurations"]["frame-structure"] // empty' "$CONFIG")
CPTYPE=$(jq -r '.["uplane-configurations"]["cp-type"] // empty' "$CONFIG")
CPLENGTH=$(jq -r '.["uplane-configurations"]["cp-length"] // empty' "$CONFIG")
CPLENGTHOTHER=$(jq -r '.["uplane-configurations"]["cp-length-other"] // empty' "$CONFIG")
ALPHA=$(jq -r '.["uplane-configurations"]["downlink-radio-frame-offset"] // empty' "$CONFIG")
NTAOFFSET=$(jq -r '.["uplane-configurations"]["n-ta-offset"] // empty' "$CONFIG")
TDDPATTERN=$(jq -r '.["uplane-configurations"]["tdd-pattern"] // empty' "$CONFIG")
NUMOFDL=$(jq -r '.["uplane-configurations"]["number-of-dl-symbol"] // empty' "$CONFIG")
NUMOFUL=$(jq -r '.["uplane-configurations"]["number-of-ul-symbol"] // empty' "$CONFIG")
TDDSCS=$(jq -r '.["uplane-configurations"]["tdd-scs"] // empty' "$CONFIG")
TDDPATTERNID=$(jq -r '.["uplane-configurations"]["tdd-pattern-id"] // empty' "$CONFIG")

for ((i=1; i < 2; i++)); do
	TXEPNAME[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXEAXCID[$i]=$(( $(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].endpoint_id['"$((i-1))"'] | to_entries[].value // empty' "$CONFIG") ))
	TXCOMPMETHOD[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].compression_method['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXCOMPBIT[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].compression_bit['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXCOMPTYPE[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].compression_type['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXSCS[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].Sub_Carrier_Spacing['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXPRB[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-endpoints"].Number_Of_PRB['"$i"'] | to_entries[].value // empty' "$CONFIG")

	TXARRAYNAME[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYCENTER[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].Center_frequency['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYBW[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].BandWidth['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYPATTERN[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].pattern['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYGAIN[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].Gain['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXARRAYTECH[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["tx_array_carriers"].Tech['"$i"'] | to_entries[].value // empty' "$CONFIG")

	TXLINKNAME[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].name['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXLINKEP[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].endpoints['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXLINKARRAY[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].tx_array_carriers['"$i"'] | to_entries[].value // empty' "$CONFIG")
	TXLINKPE[$i]=$(jq -r '.["uplane-configurations"]["PDSCH_configurations"]["low-level-tx-links"].processing_element['"$i"'] | to_entries[].value // empty' "$CONFIG")

	cp /mplane_automation/miniDU/edit/edit_pdsch_org.xml "${NETCONF_TMP}/edit/edit_pdsch_mod.xml" 2>/dev/null || true
	sed -i "s/UPENDPOINT/${TXEPNAME[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCOMPTYPE/${TXCOMPTYPE[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	if (( MPVERSIONNUM >= 800 )); then
		sed -i "s/UPCOMPMETHOD/${TXCOMPMETHOD[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	else
		if [[ "${TXCOMPMETHOD[$i]}" == "NO_COMPRESSION" ]]; then
			sed -i "/<compression-method>UPCOMPMETHOD<\/compression-method>/d" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
		else
			sed -i "s/<compression-method>UPCOMPMETHOD<\/compression-method>/<exponent>4<\/exponent>/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
		fi
	fi

	sed -i "s/UPCOMPBIT/${TXCOMPBIT[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPFRAMESTRUCTURE/${FRAMESTRUCTURE}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCPTYPE/${CPTYPE}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCPLENGTH/${CPLENGTH}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCPOTHERLENGTH/${CPLENGTHOTHER}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPSCS/KHZ_${TXSCS[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPNUMBEROFPRB/${TXPRB[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPEAXCID/${TXEAXCID[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPTXCARRIER/${TXARRAYNAME[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPCENTERFREQ/$(echo "${TXARRAYCENTER[$i]}*10^6/1" | bc)/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPBANDWIDTH/$(echo "${TXARRAYBW[$i]}*10^6/1" | bc)/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPTECH/${TXARRAYTECH[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPALPHA/${ALPHA}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPGAIN/${TXARRAYGAIN[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"

	sed -i "s/UPLINK/${TXLINKNAME[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPPE/${TXLINKPE[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPTXCARRIER/${TXLINKARRAY[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	sed -i "s/UPENDPOINT/${TXLINKEP[$i]}/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"

	if [[ "${TXARRAYPATTERN[$i]}" == "TDD" ]]; then
		sed -i "s/UPTDDPATTERNID/$TDDPATTERNID/g" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	else
		sed -i "/<configurable-tdd-pattern>UPTDDPATTERNID<\/configurable-tdd-pattern>/d" "${NETCONF_TMP}/edit/edit_pdsch_mod.xml"
	fi

	PDSCH_OUT="${NETCONF_TMP}/edit/edit-pdsch.xml"
	rm -f "$PDSCH_OUT"
	send_cmd "user-rpc --content ${NETCONF_TMP}/edit/edit_pdsch_mod.xml --out $PDSCH_OUT"
	RESULT8="NOK"
	for _w in $(seq 1 20); do
		if [[ -f "$PDSCH_OUT" ]] && grep -aq "ERROR" "$PDSCH_OUT" 2>/dev/null; then
			RESULT8="OK"
			break
		fi
		sleep 0.5
	done
	echo "[$RESULT8]	STEP 8.	The NETCONF server rejection of the requested procedure ( Duplicate eAxC-ID )"
	if [[ "$RESULT8" != "OK" ]]; then
		test_fail "Duplicate eAxC-ID rejection"
		exit 1
	fi
done

echo "[PASS]"

echo "[INFO] 3.1.10.2 O-RU configurability negative test completed. Detailed log: $LOG"
trap - EXIT INT TERM HUP
cleanup || true
if [[ -n "${NETOPEER_COPROC_PID:-}" ]]; then
	wait "$NETOPEER_COPROC_PID" 2>/dev/null || true
fi
exit 0
