#!/usr/bin/env bash
# netopeer2-cli (user-rpc) scripts: U-Plane init (delete → replace, miniDU combined fallback).
set -u

CONFORMANCE_NETPEER_UPLANE_INIT_VER="20260608-rerun-idempotent"

conformance_netpeer_resolve_uplane_init_template() {
	local leaf="$1"
	local cand
	for cand in \
		"${CONFORMANCE_REMOTE_DIR:-/var/tmp/conformance}/mplane_templates/edit/${leaf}" \
		"${NETCONF_TMP:-/var/tmp/netconf_tmp}/edit/${leaf}" \
		"/var/tmp/mplane_automation/miniDU/edit/${leaf}" \
		"/mplane_automation/miniDU/edit/${leaf}"; do
		if [[ -f "$cand" ]]; then
			echo "$cand"
			return 0
		fi
	done
	return 1
}

conformance_netpeer_resolve_minidu_combined_uplane_init() {
	local cand
	for cand in \
		"/mplane_automation/miniDU/edit/edit_init_uplane_conf.xml" \
		"/var/tmp/mplane_automation/miniDU/edit/edit_init_uplane_conf.xml"; do
		if [[ -f "$cand" ]]; then
			echo "$cand"
			return 0
		fi
	done
	return 1
}

conformance_netpeer_log_mark() {
	CONFORMANCE_NETPEER_LOG_MARK=$(wc -l <"${LOG:-/dev/null}" 2>/dev/null) || CONFORMANCE_NETPEER_LOG_MARK=0
}

conformance_netpeer_log_tail_has() {
	local pat="$1"
	[[ -n "${LOG:-}" && -f "$LOG" ]] || return 1
	tail -n +$((CONFORMANCE_NETPEER_LOG_MARK + 1)) "$LOG" 2>/dev/null | grep -aqE "$pat"
}

conformance_netpeer_outfile_rpc_ok() {
	local out_file="$1"
	[[ -f "$out_file" ]] || return 1
	grep -aq "OK" "$out_file" 2>/dev/null || grep -aq "<ok/>" "$out_file" 2>/dev/null
}

conformance_netpeer_outfile_rpc_error() {
	local out_file="$1"
	[[ -f "$out_file" ]] && grep -aq "<rpc-error" "$out_file" 2>/dev/null
}

conformance_netpeer_rpc_has_error_tag() {
	local out_file="$1"
	local tag="$2"
	[[ -f "$out_file" ]] && grep -aq "<error-tag>${tag}</error-tag>" "$out_file" 2>/dev/null && return 0
	[[ -f "$out_file" ]] && grep -aq "${tag}" "$out_file" 2>/dev/null && return 0
	conformance_netpeer_log_tail_has "<error-tag>${tag}</error-tag>"
}

conformance_netpeer_is_uplane_delete_label() {
	local label="$1"
	[[ "$label" == edit_init_uplane_conf.xml* ]]
}

# Idempotent delete: <ok/> or data-missing (already absent) both succeed.
conformance_netpeer_user_rpc_ok_or_missing() {
	local xml_path="$1"
	local out_file="$2"
	local label="$3"
	local max_wait="${4:-40}"

	rm -f "$out_file"
	conformance_netpeer_log_mark
	echo "[INFO] ${label}"
	send_cmd "user-rpc --content ${xml_path} --out ${out_file}"
	if conformance_netpeer_wait_rpc_ok "$out_file" "$max_wait"; then
		return 0
	fi
	if conformance_netpeer_rpc_has_error_tag "$out_file" "data-missing"; then
		echo "[INFO] ${label} — already absent (data-missing), OK"
		echo "OK" >"$out_file"
		return 0
	fi
	conformance_netpeer_log_uplane_init_failure "$out_file" "$label"
	return 1
}

# --out file may stay 0 bytes when netopeer logs the reply only in the session LOG.
conformance_netpeer_wait_rpc_ok() {
	local out_file="$1"
	local max_wait="${2:-40}"
	local i

	for ((i=1; i<=max_wait; i++)); do
		if conformance_netpeer_outfile_rpc_ok "$out_file"; then
			return 0
		fi
		if conformance_netpeer_outfile_rpc_error "$out_file"; then
			return 1
		fi
		if conformance_netpeer_log_tail_has '<ok/>'; then
			[[ -n "$out_file" ]] && echo "OK" >"$out_file"
			return 0
		fi
		if conformance_netpeer_log_tail_has '<rpc-error'; then
			return 1
		fi
		sleep 0.5
	done
	return 1
}

conformance_netpeer_log_uplane_init_failure() {
	local out_file="$1"
	local label="$2"
	echo "[INFO] Initialize uplane conf failed (${label}): out-file=$(
		[[ -f "$out_file" ]] && wc -c <"$out_file" 2>/dev/null || echo 0
	) bytes"
	if [[ -f "$out_file" && -s "$out_file" ]]; then
		head -c 800 "$out_file" 2>/dev/null | sed 's/^/  /' || true
		echo
	fi
	if [[ -n "${LOG:-}" && -f "$LOG" ]]; then
		echo "[INFO] rpc-reply after this attempt (session log tail):"
		tail -n +$((CONFORMANCE_NETPEER_LOG_MARK + 1)) "$LOG" 2>/dev/null \
			| grep -aE '<rpc-reply|<rpc-error|<ok/>|ly ERR|Failed to' \
			| tail -5 | sed 's/^/  /' || true
	fi
}

conformance_netpeer_try_uplane_init_file() {
	local init_src="$1"
	local label="$2"
	local init_out try_xml

	init_out="${NETCONF_TMP:-/var/tmp/netconf_tmp}/edit/edit-init-uplane-conf.xml"
	try_xml="${NETCONF_TMP:-/var/tmp/netconf_tmp}/edit/_init_uplane_try.xml"
	rm -f "$init_out"
	cp -f "$init_src" "$try_xml"

	if [[ -n "${WATCHDOG_RPC:-}" && -f "${WATCHDOG_RPC:-}" ]]; then
		conformance_netpeer_log_mark
		send_cmd "user-rpc --content ${WATCHDOG_RPC}"
		sleep 0.5
	fi

	conformance_netpeer_log_mark
	echo "[INFO] Initialize uplane conf: ${label} ← ${init_src}"
	send_cmd "user-rpc --content ${try_xml} --out ${init_out}"
	if conformance_netpeer_wait_rpc_ok "$init_out" 40; then
		echo "[INFO] Initialize uplane conf: ${label} — OK"
		return 0
	fi
	# Re-run: u-plane already cleared on prior PASS — delete on empty config is OK.
	if conformance_netpeer_is_uplane_delete_label "$label"; then
		if conformance_netpeer_rpc_has_error_tag "$init_out" "data-missing"; then
			echo "[INFO] Initialize uplane conf: ${label} — user-plane-configuration absent (data-missing), OK"
			echo "OK" >"$init_out"
			return 0
		fi
	fi
	conformance_netpeer_log_uplane_init_failure "$init_out" "$label"
	return 1
}

# Requires: send_cmd, NETCONF_TMP, LOG; optional WATCHDOG_RPC (caller keeps watchdog loop running).
conformance_netpeer_init_uplane() {
	local leaf init_src minidu_src tpl _round

	echo "[INFO] conformance_netpeer_uplane_init ${CONFORMANCE_NETPEER_UPLANE_INIT_VER}"
	mkdir -p "${NETCONF_TMP:-/var/tmp/netconf_tmp}/edit" 2>/dev/null || true

	for _round in 1 2; do
		# delete → replace first (re-run after PASS); miniDU combined last (often fails if state already set).
		for leaf in edit_init_uplane_conf.xml edit_init_uplane_conf_replace.xml; do
			init_src=$(conformance_netpeer_resolve_uplane_init_template "$leaf") || continue
			if conformance_netpeer_try_uplane_init_file "$init_src" "${leaf} (${_round}/2)"; then
				return 0
			fi
		done
		minidu_src=$(conformance_netpeer_resolve_minidu_combined_uplane_init) || minidu_src=""
		if [[ -n "$minidu_src" ]] \
			&& conformance_netpeer_try_uplane_init_file "$minidu_src" "miniDU combined (${_round}/2)"; then
			return 0
		fi
		if [[ "$_round" == 1 ]]; then
			echo "[INFO] Initialize uplane conf: retry after 3s"
			sleep 3
		fi
	done
	return 1
}
