#!/usr/bin/env bash
# Shared helpers: run M-Plane RPC sequence from Excel (--config mplane-rpc-* keys).
# Uses netopeer2-cli ``edit-config --config=`` (same as GUI raw_rpc), not ``user-rpc``.
set -u
set -o pipefail

conformance_mplane_resolve_template() {
	local leaf="$1"
	local cand
	for cand in \
		"${CONFORMANCE_REMOTE_DIR:-/var/tmp/conformance}/mplane_templates/edit/${leaf}" \
		"${NETCONF_TMP}/edit/${leaf}" \
		"/var/tmp/mplane_automation/miniDU/edit/${leaf}" \
		"/mplane_automation/miniDU/edit/${leaf}"; do
		if [[ -f "$cand" ]]; then
			echo "$cand"
			return 0
		fi
	done
	return 1
}

conformance_mplane_rpc_ok() {
	local out_file="$1"
	[[ -f "$out_file" ]] || return 1
	if grep -a "<ok/>" "$out_file" >/dev/null 2>&1 \
		|| grep -aqi "<rpc-reply[^>]*>.*<ok" "$out_file" >/dev/null 2>&1 \
		|| grep -aqE '^[[:space:]]*OK[[:space:]]*$' "$out_file" >/dev/null 2>&1 \
		|| grep -aq "OK" "$out_file" 2>/dev/null; then
		return 0
	fi
	return 1
}

conformance_mplane_log_rpc_out() {
	local label="$1"
	local out_file="$2"
	echo "[INFO] ${label} reply ($(wc -c <"${out_file}" 2>/dev/null || echo 0) bytes in out-file):"
	if [[ -f "$out_file" ]] && [[ -s "$out_file" ]]; then
		head -c 1200 "$out_file" 2>/dev/null | sed 's/^/  /'
		echo
	fi
	if [[ -n "${LOG:-}" && -f "$LOG" ]]; then
		echo "[INFO] ${label} — last rpc-error / ok in session log:"
		grep -aE '<rpc-error|<ok/>' "$LOG" 2>/dev/null | tail -5 | sed 's/^/  /' || true
	fi
}

conformance_mplane_wait_rpc_ok() {
	local out_file="$1"
	local max_wait="${2:-40}"
	local ok_before="${3:-0}"
	local err_before="${4:-}"
	local i _ok_now _err_now
	# snapshot rpc-error count at call time if not provided
	if [[ -z "$err_before" && -n "${LOG:-}" && -f "$LOG" ]]; then
		err_before=$(grep -c -a "<rpc-error" "$LOG" 2>/dev/null) || err_before=0
	fi
	err_before="${err_before:-0}"
	for ((i=1; i<=max_wait; i++)); do
		if [[ -n "$out_file" ]] && conformance_mplane_rpc_ok "$out_file"; then
			return 0
		fi
		if [[ -n "${LOG:-}" && -f "$LOG" ]]; then
			_ok_now=$(grep -c -a "<ok/>" "$LOG" 2>/dev/null) || _ok_now=0
			if (( _ok_now > ok_before )); then
				[[ -n "$out_file" ]] && echo "OK" >"$out_file"
				return 0
			fi
			_err_now=$(grep -c -a "<rpc-error" "$LOG" 2>/dev/null) || _err_now=0
			if (( _err_now > err_before )); then
				return 1
			fi
		fi
		sleep 0.5
	done
	return 1
}

conformance_mplane_wait_rpc_error() {
	local out_file="$1"
	local max_wait="${2:-40}"
	local i
	for ((i=1; i<=max_wait; i++)); do
		if [[ -f "$out_file" ]] && grep -aq "<rpc-error" "$out_file" 2>/dev/null; then
			return 0
		fi
		if [[ -n "${LOG:-}" && -f "$LOG" ]] && grep -a "<rpc-error" "$LOG" 2>/dev/null | tail -1 | grep -q .; then
			return 0
		fi
		if conformance_mplane_rpc_ok "$out_file"; then
			return 1
		fi
		sleep 0.5
	done
	return 1
}

# GUI Netconf Client (miniDU_callhome FIFO) or conformance coproc — same CLI line
conformance_mplane_netconf_send() {
	local cmd="$1"
	local fifo="${NETCONF_CONTROL_FIFO:-/var/tmp/netconf_tmp/netconf_control.fifo}"
	echo "Client SENT : $cmd" >>"${LOG:-/dev/null}" 2>&1 || true
	if [[ "${CONFORMANCE_GUI_NETCONF:-0}" == "1" && -p "$fifo" ]]; then
		echo "[INFO] → GUI Netconf FIFO: $cmd"
		if [[ -f "${CMD_LOCK_FILE:-}" ]]; then
			{ flock -x 201; printf '%s\n' "$cmd" >"$fifo"; } 201>>"${CMD_LOCK_FILE}"
		else
			printf '%s\n' "$cmd" >"$fifo"
		fi
		return 0
	fi
	send_cmd "$cmd"
}

conformance_mplane_prepare_config_payload() {
	local edit_xml="$1"
	local out_cfg="$2"
	python3 - "$edit_xml" "$out_cfg" <<'PY'
import re, sys
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
text = src.read_text(encoding="utf-8", errors="replace").strip()
text = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", text, flags=re.I)
m = re.search(r"<rpc\b[^>]*>([\s\S]*)</rpc\s*>", text, flags=re.I)
if m:
    text = m.group(1).strip()
inner = text
m_cfg = re.search(r"(<config\b[\s\S]*?</config>)", text, flags=re.I)
if m_cfg:
    cfg = m_cfg.group(1)
    m_in = re.match(r"^\s*<config\b[^>]*>([\s\S]*)</config>\s*$", cfg, flags=re.I | re.S)
    inner = m_in.group(1).strip() if m_in else cfg
dst.write_text(inner + "\n", encoding="utf-8")
PY
}

conformance_mplane_send_rpc_file() {
	local xml_path="$1"
	local out_file="$2"
	local cfg_payload="${NETCONF_TMP}/edit/_mplane_cfg_payload.xml"
	local target="running" defop="merge"
	local cmd
	local _ok_before=0

	rm -f "$out_file"
	if [[ ! -f "$xml_path" ]]; then
		echo "[ERROR] RPC XML not found: $xml_path"
		return 1
	fi
	mkdir -p "${NETCONF_TMP}/edit" 2>/dev/null || true
	conformance_mplane_prepare_config_payload "$xml_path" "$cfg_payload"
	chmod 0644 "$cfg_payload" 2>/dev/null || true

	# target / defop from edit-config wrapper in xml_path
	if grep -qi '<candidate[ />]' "$xml_path" 2>/dev/null; then
		target="candidate"
	fi
	if grep -qi '<default-operation>replace</default-operation>' "$xml_path" 2>/dev/null; then
		defop="replace"
	fi

	cmd="edit-config --target ${target} --defop ${defop} --config=${cfg_payload}"
	local _err_before=0
	if [[ -n "${LOG:-}" && -f "$LOG" ]]; then
		_ok_before=$(grep -c -a "<ok/>" "$LOG" 2>/dev/null) || _ok_before=0
		_err_before=$(grep -c -a "<rpc-error" "$LOG" 2>/dev/null) || _err_before=0
	fi
	echo "[INFO] netopeer2-cli: $cmd"
	conformance_mplane_netconf_send "$cmd"
	sleep 0.5
	if ! conformance_mplane_wait_rpc_ok "$out_file" 40 "$_ok_before" "$_err_before"; then
		return 1
	fi
	return 0
}

conformance_mplane_init_uplane() {
	local init_src out_file
	if [[ "${CONFORMANCE_MPLANE_SKIP_INIT:-0}" == "1" ]]; then
		echo "[INFO] CONFORMANCE_MPLANE_SKIP_INIT=1 — U-Plane init 생략"
		return 0
	fi
	for leaf in edit_init_uplane_conf.xml edit_init_uplane_conf_replace.xml; do
		init_src=$(conformance_mplane_resolve_template "$leaf") || init_src=""
		[[ -n "$init_src" ]] || continue
		out_file="${NETCONF_TMP}/edit/edit-init-uplane-conf.xml"
		rm -f "$out_file"
		echo "[INFO] Initialize uplane conf: ${init_src}"
		if conformance_mplane_send_rpc_file "$init_src" "$out_file"; then
			return 0
		fi
		conformance_mplane_log_rpc_out "init uplane (${leaf})" "$out_file"
	done
	echo "[FAIL] Initialize uplane conf (delete/replace 모두 실패)"
	return 1
}

conformance_mplane_run_xlsx_sequence() {
	local cfg="$1"
	local step rpc_path out_slug out_file result

	if [[ "$(jq -r '.["mplane-rpc-mode"] // empty' "$cfg")" != "xlsx" ]]; then
		echo "[ERROR] mplane-rpc-mode is not xlsx"
		return 1
	fi

	mkdir -p "${NETCONF_TMP}/edit" 2>/dev/null || true

	while IFS= read -r step; do
		[[ -n "$step" ]] || continue
		rpc_path=$(jq -r --arg s "$step" '.["mplane-rpc-files"][$s] // empty' "$cfg")
		if [[ -z "$rpc_path" || ! -f "$rpc_path" ]]; then
			echo "[ERROR] M-Plane RPC file missing for step: $step ($rpc_path)"
			return 1
		fi
		out_slug=$(echo "$step" | tr ' ' '_' | tr '[:upper:]' '[:lower:]')
		out_file="${NETCONF_TMP}/edit/mplane-${out_slug}.xml"
		echo "[INFO] M-Plane step: $step ← $rpc_path"
		result="NOK"
		if conformance_mplane_send_rpc_file "$rpc_path" "$out_file"; then
			result="OK"
			echo "	${step} configurations Successed."
		else
			echo "	${step} configurations Failed."
			conformance_mplane_log_rpc_out "$step" "$out_file"
			echo "[FAIL] M-Plane step failed: $step"
			return 1
		fi
	done < <(jq -r '.["mplane-rpc-steps"][]? // empty' "$cfg")

	return 0
}

conformance_mplane_run_duplicate_eaxc_negative() {
	local cfg="$1"
	local dup_path
	dup_path=$(jq -r '.["mplane-negative-duplicate-pdsch"] // empty' "$cfg")
	if [[ -z "$dup_path" || ! -f "$dup_path" ]]; then
		echo "[ERROR] duplicate eAxC PDSCH RPC not configured: $dup_path"
		return 1
	fi
	local out_file="${NETCONF_TMP}/edit/mplane-pdsch-dup-eaxc.xml"
	conformance_mplane_send_rpc_file "$dup_path" "$out_file"
	local result="NOK"
	if conformance_mplane_wait_rpc_error "$out_file"; then
		result="OK"
	fi
	echo "[$result]	STEP 8.	The NETCONF server rejection of the requested procedure ( Duplicate eAxC-ID )"
	if [[ "$result" != "OK" ]]; then
		conformance_mplane_log_rpc_out "duplicate eAxC" "$out_file"
		echo "[FAIL] Duplicate eAxC-ID rejection"
		return 1
	fi
	return 0
}
