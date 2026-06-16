#!/usr/bin/env bash
# Shared helpers for 3.1.5.1 / 3.1.5.2 (L2SW CLI + eventTime summary for GUI).

l2sw_send() {
	local _ts
	_ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || true)"
	echo "[L2SW][${_ts:-unknown}] >>> $*"
	echo "$*" >&20 2>/dev/null || true
	sleep 1
}

_print_sync_state_times() {
	python3 - "$LOG" <<'PY'
import re, sys
from pathlib import Path

log = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")

def norm_state(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").strip().upper())

first = {"HOLDOVER": None, "FREERUN": None, "LOCKED": None}
last_sync = None
for m in re.finditer(
    r"<notification\b[^>]*>[\s\S]*?<eventTime>([^<]+)</eventTime>([\s\S]*?)</notification>",
    log,
    re.I,
):
    ts = m.group(1).strip()
    payload = m.group(2)
    pl = payload.lower()
    if "synchronization-state-change" in pl:
        sm = re.search(r"<sync-state[^>]*>([^<]+)</sync-state>", payload, re.I)
        if sm:
            st = norm_state(sm.group(1))
            last_sync = ts
            if st in first and first[st] is None:
                first[st] = ts
    if first["FREERUN"] is None and "ptp-state-change" in pl:
        pm = re.search(r"<ptp-state[^>]*>([^<]+)</ptp-state>", payload, re.I)
        if pm and norm_state(pm.group(1)) == "FREERUN":
            first["FREERUN"] = ts

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
	_print_sync_state_times
	_print_alarm_times
}
