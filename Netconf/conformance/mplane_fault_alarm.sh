#!/usr/bin/env bash
# M-Plane Fault Alarm batch test
# - Hold one CallHome NETCONF session (subscribe)
# - Query ORU CLI: show alarm information
# - For each fault-id: raise → wait alarm-notif → clear → wait clear-notif
# - Emit machine-readable markers for GUI detail popup / txt
set -u
set -o pipefail

TESTID="fault_alarm"
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

if [[ -z "${CONFIG}" || ! -f "$CONFIG" ]]; then
	echo "[ERROR] --config <path> required"
	exit 2
fi

USER=$(jq -r '.["management-configurations"]["NETCONF-ID"] // empty' "$CONFIG")
PASSWORD=$(jq -r '.["management-configurations"]["NETCONF-PW"] // empty' "$CONFIG")
ALLOWED_IP_CFG=$(jq -r '.["management-configurations"]["SERVER-IP"] // empty' "$CONFIG")
LOCAL_IP_CFG=$(jq -r '.["management-configurations"]["LOCAL-IP"] // empty' "$CONFIG")
PRODUCT=$(jq -r '.["management-configurations"]["PRODUCT-CODE"] // empty' "$CONFIG")

LISTEN_PORT="${CALLHOME_PORT:-4334}"
NETCONF_TMP="${NETCONF_TMP:-/var/tmp/netconf_tmp}"
LOCAL_IP="${LOCAL_IP:-$LOCAL_IP_CFG}"
ALLOWED_IP="${ALLOWED_IP:-$ALLOWED_IP_CFG}"

# Alarm Id 목록은 항상 show alarm information 결과에서 파싱 (설정값 없음)
FAULT_IDS="${FAULT_IDS:-all}"
SHOW_CMD="${SHOW_CMD:-show alarm information oran}"
ACTIVE_SHOW_CMD="${ACTIVE_SHOW_CMD:-show alarm active-alarms}"
# Templates: prefer base64 (avoids bash brace corruption of {source_id})
if [[ -n "${RAISE_TMPL_B64:-}" ]]; then
	RAISE_TMPL="$(printf '%s' "$RAISE_TMPL_B64" | base64 -d 2>/dev/null || true)"
fi
if [[ -n "${CLEAR_TMPL_B64:-}" ]]; then
	CLEAR_TMPL="$(printf '%s' "$CLEAR_TMPL_B64" | base64 -d 2>/dev/null || true)"
fi
# Do NOT put {braces} in :- defaults on this line — bash can mangle them
if [[ -z "${RAISE_TMPL:-}" ]]; then
	RAISE_TMPL="test alarm alarm-id {alarm_id} source-id {source_id} start-alarm"
fi
if [[ -z "${CLEAR_TMPL:-}" ]]; then
	CLEAR_TMPL="no test alarm alarm-id {alarm_id} source-id {source_id}"
fi
SOURCE_ID="${SOURCE_ID:-0}"
ALARM_TIMEOUT_SEC="${ALARM_TIMEOUT_SEC:-60}"
ORU_CLI_ID="${ORU_CLI_ID:-}"
ORU_CLI_PW="${ORU_CLI_PW:-}"
ORU_SSH_IP="${ORU_SSH_IP:-$ALLOWED_IP}"
SSH_FAMILY="${SSH_FAMILY:-v4}"
REQUIRE_NOTI="${REQUIRE_NOTI:-1}"
# CLI show 덤프는 Live 로그에 찍지 않음 (판정은 NETCONF raise/clear noti)
SKIP_NORMAL="${SKIP_NORMAL:-1}"

if [[ -z "$USER" || -z "$PASSWORD" || -z "$LOCAL_IP" || -z "$ALLOWED_IP" ]]; then
	echo "[ERROR] NETCONF-ID/PW, LOCAL_IP, ALLOWED_IP(SERVER-IP) required"
	exit 2
fi
if [[ -z "$ORU_CLI_ID" ]]; then
	echo "[ERROR] ORU_CLI_ID required"
	exit 2
fi

WATCHDOG_RPC="${NETCONF_TMP}/edit/watchdog_reset.xml"
mkdir -p "${NETCONF_TMP}/edit"
cat > "${WATCHDOG_RPC}" <<'EORPC'
<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>
EORPC

LOG_BASE="${LOG_PATH:-${CONFORMANCE_REMOTE_DIR:-/var/tmp/conformance}/logs}"
LOG_BASE="${LOG_BASE%/}"
LOG_DIR="${LOG_BASE}/${PRODUCT:-_mplane_}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/MPLANE_${TESTID}_$(date +'%y%m%d_%H-%M-%S').log"
: >"$LOG"
chmod 0644 "$LOG" 2>/dev/null || true

echo "[INFO] USER=$USER LOCAL_IP=$LOCAL_IP ALLOWED_IP=$ALLOWED_IP LISTEN=$LISTEN_PORT"
echo "[INFO] ORU_SSH=$ORU_SSH_IP family=$SSH_FAMILY faults=$FAULT_IDS timeout=${ALARM_TIMEOUT_SEC}s skip_normal=$SKIP_NORMAL"
echo "[INFO] RAISE='$RAISE_TMPL' CLEAR='$CLEAR_TMPL' (pass=raise/clear NETCONF noti)"
echo "[INFO] log=$LOG"

wall_ts() { date +'%Y-%m-%d %H:%M:%S'; }

subst_fault() {
	# bash brace-safe substitution via python
	local tmpl="$1" fid="$2" sid="${SOURCE_ID:-0}"
	TMPL="$tmpl" FID="$fid" SID="$sid" python3 - <<'PY'
import os, re
t = os.environ.get("TMPL", "")
fid = os.environ.get("FID", "")
sid = os.environ.get("SID", "0")
for a, b in (("{fault_id}", fid), ("{alarm_id}", fid), ("{source_id}", sid)):
    t = t.replace(a, b)
t = t.rstrip("}").strip()
t = re.sub(rf"(?:\s*source-id\s+{re.escape(sid)})+", f" source-id {sid}", t)
t = re.sub(r"\s+", " ", t).strip()
# keep a single start-alarm at end if present
if t.count("start-alarm") > 1:
    t = t.replace("start-alarm", "", t.count("start-alarm") - 1)
    t = re.sub(r"\s+", " ", t).strip()
    if not t.endswith("start-alarm"):
        t = t + " start-alarm"
print(t)
PY
}

# Compact parse of information table → alarm_row lines + optional ID list
# stdout: machine markers only (no full CLI dump)
parse_alarm_catalog() {
	local show_txt="$1" mode="$2"  # mode=emit_rows | list_ids
	SHOW_TXT="$show_txt" MODE="$mode" SKIP_NORMAL="$SKIP_NORMAL" python3 - <<'PY' || true
import os, re
text = os.environ.get("SHOW_TXT", "")
mode = os.environ.get("MODE", "emit_rows")
skip_normal = os.environ.get("SKIP_NORMAL", "1").strip() not in ("0", "false", "no", "n", "")
sev_re = re.compile(r"^(CRITICAL|MAJOR|MINOR|WARNING|NORMAL|INDETERMINATE)$", re.I)
rows = []
# join multiline name wraps: "45 16 MAJOR" then next line "Unit unidentified module ..."
lines = text.splitlines()
i = 0
while i < len(lines):
    line = lines[i].rstrip()
    s = line.strip()
    i += 1
    if not s or s.startswith("#") or set(s) <= {"-", "="}:
        continue
    low = s.lower()
    if "alarm id" in low and "fault" in low:
        continue
    if low.startswith(("alarm oper", "alarm detection", "alarm notification", "total count", "proto:", "config:", "interval:", "related", "state:")):
        continue
    m = re.match(r"^(\d+)\s+(\d+)\s+(\S+)\s*(.*)$", s)
    if not m:
        continue
    aid, fid, sev, rest = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip()
    if not sev_re.match(sev):
        # maybe severity missing on this line — skip
        continue
    # name may wrap to next non-table line
    if not rest and i < len(lines):
        nxt = lines[i].strip()
        if nxt and not re.match(r"^\d+\s+\d+\s+", nxt) and not nxt.startswith("---"):
            rest = nxt
            i += 1
    toks = rest.split()
    source = ""
    config = ""
    name = rest
    src_keys = ("module", "ecpri", "disk", "ant-line-tx", "ant-line-rx", "carrier-tx", "carrier-rx")
    for j, t in enumerate(toks):
        tl = t.lower()
        if tl in src_keys or tl.startswith("ant-") or tl.startswith("carrier-"):
            source = t
            name = " ".join(toks[:j]).strip()
            rem = toks[j + 1 :]
            if rem:
                config = rem[0]
            break
    if skip_normal and (fid == "0" or sev.upper() == "NORMAL"):
        continue
    rows.append((aid, fid, sev.upper(), name or "", source, config))

if mode == "list_ids":
    print(",".join(a for a, *_ in rows))
else:
    print(f"alarm_count: {len(rows)}")
    print("alarm_ids: " + ",".join(a for a, *_ in rows))
    for aid, fid, sev, name, source, config in rows:
        # pipe-safe compact row for GUI detail
        safe = (name or "").replace("|", "/")
        print(f"alarm_row:{aid}|{fid}|{sev}|{safe}|{source}|{config}")
PY
}

lookup_yang_fault_id() {
	local aid="$1"
	AID="$aid" ROWS="${_ALARM_ROW_CACHE:-}" SHOW_TXT="${_SHOW_TABLE_TXT:-}" python3 - <<'PY' || true
import os, re
aid = os.environ.get("AID", "").strip()
rows = os.environ.get("ROWS", "")
for line in rows.splitlines():
    if not line.startswith("alarm_row:"):
        continue
    parts = line[len("alarm_row:"):].split("|")
    if parts and parts[0].strip() == aid and len(parts) > 1:
        print(parts[1].strip())
        raise SystemExit(0)
text = os.environ.get("SHOW_TXT", "")
for line in text.splitlines():
    m = re.match(rf"^\s*{re.escape(aid)}\s+(\d+)\s+\S+", line)
    if m:
        print(m.group(1))
        break
PY
}

cli_ok_hint() {
	# one-line CLI result for log (no banner dump)
	local out="$1"
	if printf '%s\n' "$out" | grep -qiE 'Unknown command|Invalid|Error:|failed'; then
		printf '%s\n' "$out" | grep -oiE 'Unknown command.*|Invalid[^[:cntrl:]]*|Error:[^[:cntrl:]]*' | head -1
		return 1
	fi
	echo "OK"
	return 0
}

parse_active_summary() {
	local active_txt="$1"
	SHOW_TXT="$active_txt" python3 - <<'PY' || true
import os, re
text = os.environ.get("SHOW_TXT", "")
ids = []
for line in text.splitlines():
    s = line.strip()
    # active table: "45 Unit unidentified ru_n2_n5 O------ ..."
    m = re.match(r"^(\d+)\s+\S+", s)
    if not m:
        continue
    low = s.lower()
    if "alarm id" in low or low.startswith(("alarm oper", "total count", "proto:", "config:", "state:")):
        continue
    # skip header-ish short lines
    if "fault" in low and "source" in low:
        continue
    ids.append(m.group(1))
# unique preserve order
seen = set()
out = []
for i in ids:
    if i not in seen:
        seen.add(i)
        out.append(i)
print("active_present: " + ("YES" if out else "NO"))
print("active_ids: " + (",".join(out) if out else ""))
print("active_count: " + str(len(out)))
PY
}

oru_cli() {
	# SOLiD O-RAN CLI 는 대화형 ru# — remote argv 만으로는 명령이 안 먹고 배너만 나옴.
	# ssh -tt + stdin 으로 명령/exit 전달.
	local cmd="$*"
	local flag="-4"
	[[ "$SSH_FAMILY" == "v6" || "$SSH_FAMILY" == "ipv6" ]] && flag="-6"
	export SSHPASS="$ORU_CLI_PW"
	{
		printf '%s\r\n' "$cmd"
		# show / long CLI 응답 대기 (exit 너무 빠르면 출력이 잘림)
		sleep 1.2
		printf 'exit\r\n'
		sleep 0.3
	} | sshpass -e ssh "$flag" -tt \
		-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
		-o ConnectTimeout=10 -o BatchMode=no \
		"${ORU_CLI_ID}@${ORU_SSH_IP}" 2>&1 || true
}

count_fault_tag() {
	grep -acE '<fault-id>' "$LOG" 2>/dev/null || true
}

# Count alarm-notif blocks that mention fault-id and is-cleared true/false (approx via nearby lines)
count_raise_noti() {
	local fid="$1"
	python3 - "$LOG" "$fid" <<'PY' || echo 0
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
fid = sys.argv[2].strip()
# split loosely on alarm-notif
parts = re.split(r"(?i)(?=<alarm-notif\b)", text)
n = 0
for p in parts:
    if "alarm-notif" not in p.lower():
        continue
    if not re.search(rf"<fault-id>\s*{re.escape(fid)}\s*</fault-id>", p, re.I):
        continue
    m = re.search(r"<is-cleared>\s*(true|false)\s*</is-cleared>", p, re.I)
    if m and m.group(1).lower() == "false":
        n += 1
print(n)
PY
}

count_clear_noti() {
	local fid="$1"
	python3 - "$LOG" "$fid" <<'PY' || echo 0
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
fid = sys.argv[2].strip()
parts = re.split(r"(?i)(?=<alarm-notif\b)", text)
n = 0
for p in parts:
    if "alarm-notif" not in p.lower():
        continue
    if not re.search(rf"<fault-id>\s*{re.escape(fid)}\s*</fault-id>", p, re.I):
        continue
    m = re.search(r"<is-cleared>\s*(true|false)\s*</is-cleared>", p, re.I)
    if m and m.group(1).lower() == "true":
        n += 1
print(n)
PY
}

extract_last_event_time() {
	local fid="$1" want_clear="$2"  # want_clear=0 raise, 1 clear
	python3 - "$LOG" "$fid" "$want_clear" <<'PY' || true
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
fid = sys.argv[2].strip()
want = sys.argv[3].strip() == "1"
parts = re.split(r"(?i)(?=<alarm-notif\b)", text)
last = ""
for p in parts:
    if "alarm-notif" not in p.lower():
        continue
    if not re.search(rf"<fault-id>\s*{re.escape(fid)}\s*</fault-id>", p, re.I):
        continue
    m = re.search(r"<is-cleared>\s*(true|false)\s*</is-cleared>", p, re.I)
    if not m:
        continue
    cleared = m.group(1).lower() == "true"
    if cleared != want:
        continue
    et = re.search(r"<eventTime>\s*([^<\s]+)\s*</eventTime>", p, re.I)
    if et:
        last = et.group(1).strip()
print(last)
PY
}

send_cmd() {
	local cmd="$*"
	echo "Client SENT : $cmd" >>"$LOG" 2>&1
	set +u
	local _wfd="${NP2[1]:-}"
	set -u
	[[ -n "${_wfd}" ]] || return 0
	echo "$cmd" >&"${_wfd}" 2>/dev/null || true
}

COPROC_READY=0
NETOPEER_COPROC_PID=""
WATCHDOG_PID=""

cleanup() {
	if [[ -n "${WATCHDOG_PID:-}" ]]; then
		kill "$WATCHDOG_PID" 2>/dev/null || true
		wait "$WATCHDOG_PID" 2>/dev/null || true
	fi
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
	# best-effort v6 filter cleanup (no-op if unused)
	sudo ip6tables -D INPUT -p tcp --dport "$LISTEN_PORT" -j DROP >/dev/null 2>&1 || true
	sudo ip6tables -D INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT >/dev/null 2>&1 || true
	return 0
}
trap cleanup EXIT INT TERM HUP

sudo fuser -k "${LISTEN_PORT}/tcp" 2>/dev/null || true
sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -j DROP 2>/dev/null || true
sudo iptables -D INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT 2>/dev/null || true
sleep 1
sudo iptables -A INPUT -p tcp --dport "$LISTEN_PORT" -j DROP
sudo iptables -I INPUT -p tcp --dport "$LISTEN_PORT" -s "$ALLOWED_IP" -j ACCEPT
sleep 2

LISTEN_TO="${CALLHOME_LISTEN_TIMEOUT:-180}"
coproc NP2 {
	setsid stdbuf -oL sshpass -p "$PASSWORD" netopeer2-cli 2>&1
} >>"$LOG" 2>&1
NETOPEER_COPROC_PID="${NP2_PID:-$!}"
exec 3>&"${NP2[1]}"
COPROC_READY=1

send_cmd "verb 3"
send_cmd "knownhosts --mode skip"
send_cmd "listen --host $LOCAL_IP --port $LISTEN_PORT --login $USER --timeout $LISTEN_TO"

########################################################################################
# Call Home + login
########################################################################################
RESULT_CH="NOK"
PAT_OLD="Accepted a connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
PAT_NEW="Accepted a new connection on ${LOCAL_IP}:${LISTEN_PORT} from ${ALLOWED_IP}"
_iters=$(( LISTEN_TO * 5 ))
for _w in $(seq 1 "$_iters"); do
	if grep -a -F -e "$PAT_OLD" -e "$PAT_NEW" "$LOG" >/dev/null 2>&1; then
		RESULT_CH="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT_CH]	STEP CallHome receive"
if [[ "$RESULT_CH" != "OK" ]]; then
	echo "===SUMMARY==="
	echo "FAIL callhome"
	echo "===SUMMARY_END==="
	exit 1
fi

RESULT_LOGIN="NOK"
for _w in $(seq 1 150); do
	if grep -a -F "Authentication successful" "$LOG" >/dev/null 2>&1; then
		RESULT_LOGIN="OK"
		break
	fi
	sleep 0.2
done
echo "[$RESULT_LOGIN]	STEP login"
if [[ "$RESULT_LOGIN" != "OK" ]]; then
	echo "===SUMMARY==="
	echo "FAIL login"
	echo "===SUMMARY_END==="
	exit 1
fi

sleep 3
_ok_before=$(grep -c -a -F "OK" "$LOG" 2>/dev/null) || true
send_cmd "subscribe --stream NETCONF"
for _w in $(seq 1 300); do
	_ok_now=$(grep -c -a -F "OK" "$LOG" 2>/dev/null) || true
	if (( _ok_now > _ok_before )); then
		break
	fi
	sleep 0.2
done
echo "[OK]	STEP subscribe NETCONF stream (session held)"

(
	_last_count=0
	while true; do
		sleep 2
		_cur_count=$(grep -acE '^\s*<supervision-notification' "$LOG" 2>/dev/null) || true
		if [[ "${_cur_count:-0}" =~ ^[0-9]+$ ]] && (( _cur_count > _last_count )); then
			for _i in $(seq 1 $(( _cur_count - _last_count ))); do
				echo "user-rpc --content ${WATCHDOG_RPC}" >&3 2>/dev/null || true
				echo "Client SENT : user-rpc --content ${WATCHDOG_RPC}" >>"$LOG" 2>&1
			done
			_last_count=$_cur_count
		fi
	done
) &
WATCHDOG_PID=$!

########################################################################################
# Baseline: active / catalog (CLI raw는 로그에 안 찍음 — 요약·alarm_row 만)
########################################################################################
echo "===ACTIVE_ALARMS_BEGIN==="
echo "# cmd: $ACTIVE_SHOW_CMD"
echo "# time: $(wall_ts)"
_active_out="$(oru_cli "$ACTIVE_SHOW_CMD" || true)"
_ACTIVE_SUMMARY="$(parse_active_summary "$_active_out")"
printf '%s\n' "$_ACTIVE_SUMMARY"
echo "===ACTIVE_ALARMS_END==="
_ACTIVE_IDS="$(printf '%s\n' "$_ACTIVE_SUMMARY" | sed -n 's/^active_ids:[[:space:]]*//p' | head -1 | tr -d '[:space:]')"

echo "===ALARM_QUERY_BEGIN==="
echo "# cmd: $SHOW_CMD"
echo "# time: $(wall_ts)"
_show_out="$(oru_cli "$SHOW_CMD" || true)"
_SHOW_TABLE_TXT="$_show_out"
_ALARM_ROW_CACHE="$(parse_alarm_catalog "$_show_out" emit_rows)"
printf '%s\n' "$_ALARM_ROW_CACHE"
echo "===ALARM_QUERY_END==="

# information 전체 − 이미 active 인 Alarm Id 제외
_CATALOG_IDS="$(parse_alarm_catalog "$_show_out" list_ids)"
echo "[INFO] catalog FAULT_IDS: $_CATALOG_IDS"
echo "[INFO] pre-active ids (exclude): ${_ACTIVE_IDS:-"(none)"}"

FILTER_OUT="$(_ACTIVE_IDS="$_ACTIVE_IDS" CATALOG="$_CATALOG_IDS" ROWS="$_ALARM_ROW_CACHE" python3 - <<'PY' || true
import os
active = {x.strip() for x in os.environ.get("_ACTIVE_IDS", "").split(",") if x.strip()}
catalog = [x.strip() for x in os.environ.get("CATALOG", "").split(",") if x.strip()]
rows = {}
for line in os.environ.get("ROWS", "").splitlines():
    if not line.startswith("alarm_row:"):
        continue
    p = line[len("alarm_row:"):].split("|")
    if p:
        rows[p[0].strip()] = p
test = [a for a in catalog if a not in active]
skip = [a for a in catalog if a in active]
# also active ids not in catalog (still report)
for a in sorted(active, key=lambda x: int(x) if x.isdigit() else x):
    if a not in skip and a not in catalog:
        skip.append(a)
print("TEST_IDS=" + ",".join(test))
print("SKIP_IDS=" + ",".join(skip))
print("===SKIPPED_ACTIVE_BEGIN===")
print(f"skipped_count: {len(skip)}")
print("skipped_ids: " + ",".join(skip))
print("reason: already active before test")
for a in skip:
    p = rows.get(a)
    if p and len(p) >= 4:
        print(f"skip_row:{a}|{p[1] if len(p)>1 else ''}|{p[2] if len(p)>2 else ''}|{p[3] if len(p)>3 else ''}|already_active")
    else:
        print(f"skip_row:{a}||||already_active")
print("===SKIPPED_ACTIVE_END===")
PY
)"

# extract markers / TEST_IDS from python block
FAULT_IDS="$(printf '%s\n' "$FILTER_OUT" | sed -n 's/^TEST_IDS=//p' | head -1)"
_SKIP_IDS="$(printf '%s\n' "$FILTER_OUT" | sed -n 's/^SKIP_IDS=//p' | head -1)"
printf '%s\n' "$FILTER_OUT" | sed -n '/^===SKIPPED_ACTIVE_BEGIN===/,/^===SKIPPED_ACTIVE_END===/p'

echo "[INFO] FAULT_IDS after exclude active: ${FAULT_IDS:-"(none)"}"
_n_batch=0
if [[ -n "$FAULT_IDS" ]]; then
	_n_batch="$(echo "$FAULT_IDS" | awk -F',' '{print NF}')"
fi
_n_skip=0
if [[ -n "${_SKIP_IDS:-}" ]]; then
	_n_skip="$(echo "$_SKIP_IDS" | awk -F',' '{print NF}')"
fi
echo "[INFO] batch will test ${_n_batch} alarm(s), skipped_active=${_n_skip}"
if [[ -z "$FAULT_IDS" ]]; then
	echo "===SUMMARY==="
	if (( _n_skip > 0 )); then
		echo "PASS raised_cleared=0/0 skipped_active=${_n_skip}"
		_rc=0
	else
		echo "FAIL no_faults"
		_rc=1
	fi
	echo "===SUMMARY_END==="
	echo "[INFO] nothing to test after excluding pre-active alarms"
	exit "$_rc"
fi

########################################################################################
# Batch raise / clear — 판정은 NETCONF raise/clear noti 만 (CLI show 재조회 없음)
########################################################################################
IFS=',' read -r -a FID_ARR <<< "$FAULT_IDS"
PASS_N=0
FAIL_N=0
TOTAL=0

for _raw in "${FID_ARR[@]}"; do
	fid="$(echo "$_raw" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
	[[ -n "$fid" ]] || continue
	TOTAL=$((TOTAL + 1))
	raise_cmd="$(subst_fault "$RAISE_TMPL" "$fid")"
	clear_cmd="$(subst_fault "$CLEAR_TMPL" "$fid")"
	# NETCONF noti 의 <fault-id> 는 표의 Fault Id (Alarm Id 와 다를 수 있음)
	yang_fid="$(lookup_yang_fault_id "$fid")"
	[[ -n "$yang_fid" ]] || yang_fid="$fid"

	echo "===ALARM alarm_id=${fid}==="
	echo "yang_fault_id: $yang_fid"
	echo "raise_cmd: $raise_cmd"
	echo "clear_cmd: $clear_cmd"

	_raise_before="$(count_raise_noti "$yang_fid")"
	_clear_before="$(count_clear_noti "$yang_fid")"
	[[ "${_raise_before}" =~ ^[0-9]+$ ]] || _raise_before=0
	[[ "${_clear_before}" =~ ^[0-9]+$ ]] || _clear_before=0

	raise_sent="$(wall_ts)"
	echo "raise_sent: $raise_sent"
	_raise_cli="$(oru_cli "$raise_cmd" || true)"
	_raise_hint="$(cli_ok_hint "$_raise_cli" || true)"
	echo "raise_cli: ${_raise_hint:-OK}"

	raise_noti="NOK"
	raise_event=""
	_to_iter=$(( ALARM_TIMEOUT_SEC * 5 ))
	for _w in $(seq 1 "$_to_iter"); do
		_now="$(count_raise_noti "$yang_fid")"
		[[ "${_now}" =~ ^[0-9]+$ ]] || _now=0
		if (( _now > _raise_before )); then
			raise_noti="OK"
			raise_event="$(extract_last_event_time "$yang_fid" 0)"
			break
		fi
		sleep 0.2
	done
	raise_wall="$(wall_ts)"
	echo "raise_noti: $raise_noti"
	echo "raise_noti_wall: $raise_wall"
	echo "raise_eventTime: ${raise_event:-}"

	clear_sent="$(wall_ts)"
	echo "clear_sent: $clear_sent"
	_clear_cli="$(oru_cli "$clear_cmd" || true)"
	_clear_hint="$(cli_ok_hint "$_clear_cli" || true)"
	echo "clear_cli: ${_clear_hint:-OK}"

	clear_noti="NOK"
	clear_event=""
	for _w in $(seq 1 "$_to_iter"); do
		_now="$(count_clear_noti "$yang_fid")"
		[[ "${_now}" =~ ^[0-9]+$ ]] || _now=0
		if (( _now > _clear_before )); then
			clear_noti="OK"
			clear_event="$(extract_last_event_time "$yang_fid" 1)"
			break
		fi
		sleep 0.2
	done
	clear_wall="$(wall_ts)"
	echo "clear_noti: $clear_noti"
	echo "clear_noti_wall: $clear_wall"
	echo "clear_eventTime: ${clear_event:-}"

	row_ok=1
	if [[ "$REQUIRE_NOTI" == "1" ]]; then
		[[ "$raise_noti" == "OK" ]] || row_ok=0
		[[ "$clear_noti" == "OK" ]] || row_ok=0
	fi
	if printf '%s\n' "$_raise_cli" | grep -qi "Unknown command"; then
		echo "raise_cli: Unknown command"
		row_ok=0
	fi
	if printf '%s\n' "$_clear_cli" | grep -qi "Unknown command"; then
		echo "clear_cli: Unknown command"
		row_ok=0
	fi
	if [[ "$row_ok" == "1" ]]; then
		echo "fault_result: PASS"
		PASS_N=$((PASS_N + 1))
	else
		echo "fault_result: FAIL"
		FAIL_N=$((FAIL_N + 1))
	fi
	echo "===ALARM_END==="
done

echo "===SUMMARY==="
if (( TOTAL == 0 )); then
	if (( _n_skip > 0 )); then
		echo "PASS raised_cleared=0/0 skipped_active=${_n_skip}"
		_rc=0
	else
		echo "FAIL no_faults"
		_rc=1
	fi
elif (( FAIL_N == 0 )); then
	echo "PASS raised_cleared=${PASS_N}/${TOTAL} skipped_active=${_n_skip}"
	_rc=0
else
	echo "FAIL pass=${PASS_N} fail=${FAIL_N} total=${TOTAL} skipped_active=${_n_skip}"
	_rc=1
fi
echo "===SUMMARY_END==="
echo "[INFO] session held for full batch; disconnect on exit"
exit "$_rc"
