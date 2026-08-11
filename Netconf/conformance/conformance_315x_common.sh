#!/usr/bin/env bash
# Shared helpers for 3.1.5.1 / 3.1.5.2 (L2SW CLI + eventTime summary for GUI).

l2sw_send() {
	local _ts
	_ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || true)"
	echo "[L2SW][${_ts:-unknown}] >>> $*"
	echo "$*" >&20 2>/dev/null || true
	sleep 1
}

# Split OFF/ON command strings on ", " (comma + space).
# Bare commas inside a command (e.g. "interface range ethernet 0/16,0/18") stay intact.
# Usage: split_l2sw_cmds "$ALARM_OFF_CMDS" OFF_ARR
split_l2sw_cmds() {
	local _input="$1"
	local -n _arr_ref="$2"
	local _rest _piece
	_arr_ref=()
	_rest="$_input"
	while [[ "$_rest" == *", "* ]]; do
		_piece="${_rest%%, *}"
		_piece="${_piece#"${_piece%%[![:space:]]*}"}"
		_piece="${_piece%"${_piece##*[![:space:]]}"}"
		[[ -n "$_piece" ]] && _arr_ref+=("$_piece")
		_rest="${_rest#*, }"
	done
	_piece="$_rest"
	_piece="${_piece#"${_piece%%[![:space:]]*}"}"
	_piece="${_piece%"${_piece##*[![:space:]]}"}"
	[[ -n "$_piece" ]] && _arr_ref+=("$_piece")
}

_print_sync_state_times() {
	python3 - "$LOG" <<'PY'
import re, sys
from pathlib import Path

log = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")

def norm_state(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").strip().upper())

def nearest_event_time(text: str, pos: int) -> str | None:
    chunk = text[max(0, pos - 8000):pos]
    hits = re.findall(r"<eventTime>([^<]+)</eventTime>", chunk, re.I)
    return hits[-1].strip() if hits else None

first = {"HOLDOVER": None, "FREERUN": None, "LOCKED": None}
last_sync = None

# Pass 1: structured notification blocks
for m in re.finditer(r"<notification\b[^>]*>([\s\S]*?)</notification>", log, re.I):
    block = m.group(1)
    etm = re.search(r"<eventTime>([^<]+)</eventTime>", block, re.I)
    if not etm:
        continue
    ts = etm.group(1).strip()
    pl = block.lower()
    if "synchronization-state-change" in pl or "sync-state" in pl:
        sm = re.search(r"<sync-state(?:\s[^>]*)?>\s*([^<]+?)\s*</sync-state>", block, re.I)
        if sm:
            st = norm_state(sm.group(1))
            last_sync = ts
            if st in first and first[st] is None:
                first[st] = ts
    if first["FREERUN"] is None and "ptp-state-change" in pl:
        pm = re.search(r"<ptp-state(?:\s[^>]*)?>\s*([^<]+?)\s*</ptp-state>", block, re.I)
        if pm and norm_state(pm.group(1)) == "FREERUN":
            first["FREERUN"] = ts

# Pass 2: global sync-state tags (wrapped/split netopeer logs)
for m in re.finditer(r"<sync-state(?:\s[^>]*)?>\s*([^<]+?)\s*</sync-state>", log, re.I):
    st = norm_state(m.group(1))
    if st not in first or first[st] is not None:
        continue
    ts = nearest_event_time(log, m.start())
    if ts:
        first[st] = ts
        last_sync = ts

# Pass 3: global ptp-state FREERUN fallback
if first["FREERUN"] is None:
    for m in re.finditer(r"<ptp-state(?:\s[^>]*)?>\s*([^<]+?)\s*</ptp-state>", log, re.I):
        if norm_state(m.group(1)) != "FREERUN":
            continue
        ts = nearest_event_time(log, m.start())
        if ts:
            first["FREERUN"] = ts
            break

if last_sync:
    print(f"[TIME] SYNC_EVENT_TIME={last_sync}")
for k in ("HOLDOVER", "FREERUN", "LOCKED"):
    if first[k]:
        print(f"[TIME] {k}_EVENT_TIME={first[k]}")
PY
}

_print_alarm_times() {
	python3 - "$LOG" <<'PY'
import re, sys
from pathlib import Path

log = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
first_occ = None
first_clr = None
for m in re.finditer(
    r"<notification\b[^>]*>[\s\S]*?<eventTime>([^<]+)</eventTime>([\s\S]*?)</notification>",
    log,
    re.I,
):
    ts = m.group(1).strip()
    payload = m.group(2)
    if "fault-id" not in payload.lower():
        continue
    if first_occ is None and re.search(r"<is-cleared>\s*false\s*</is-cleared>", payload, re.I):
        first_occ = ts
    if first_clr is None and re.search(r"<is-cleared>\s*true\s*</is-cleared>", payload, re.I):
        first_clr = ts
if first_occ:
    print(f"[TIME] ALARM_OCCUR_EVENT_TIME={first_occ}")
if first_clr:
    print(f"[TIME] ALARM_CLEAR_EVENT_TIME={first_clr}")
PY
}

print_conformance_event_times() {
	emit_conformance_event_times_progress
}

# Emit newly seen [TIME] lines once each (for live GUI Sync 이력).
# Call periodically during wait loops; safe to call repeatedly.
declare -A _315X_TIME_EMITTED=()
_315X_LAST_LOG_SIZE=0
_315X_EMIT_TICK=0
emit_conformance_event_times_progress() {
	local line key out sz
	[[ -n "${LOG:-}" && -f "${LOG:-}" ]] || return 0
	sz=$(wc -c <"$LOG" 2>/dev/null || echo 0)
	_315X_EMIT_TICK=$(( _315X_EMIT_TICK + 1 ))
	# Skip heavy parse only when LOG size is unchanged; retry every ~5s anyway.
	if [[ "$sz" == "${_315X_LAST_LOG_SIZE:-0}" ]] && (( _315X_EMIT_TICK % 5 != 0 )); then
		return 0
	fi
	_315X_LAST_LOG_SIZE="$sz"
	out="$(
		{
			_print_sync_state_times
			_print_alarm_times
		} 2>/dev/null || true
	)"
	while IFS= read -r line || [[ -n "${line:-}" ]]; do
		[[ -n "$line" ]] || continue
		[[ "$line" == \[TIME\]* ]] || continue
		key="${line%%=*}"
		if [[ -z "${_315X_TIME_EMITTED[$key]:-}" ]]; then
			_315X_TIME_EMITTED[$key]=1
			echo "$line"
		fi
	done <<< "$out"
}
