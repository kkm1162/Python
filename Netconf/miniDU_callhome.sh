#!/bin/bash
# -------------------------------------------------------
# [설정] 장비 접속 정보 & Supervision 파라미터
# -------------------------------------------------------
# NETCONF CallHome 로그인 계정 (리눅스 시스템 $USER 와 충돌 방지)
NETCONF_USER="${NETCONF_USER:-${USER:-oranuser}}"
# 하위 호환: 기존 코드/GUI 필드명 USER 유지
USER="$NETCONF_USER"
PASSWORD="${PASSWORD:-o-ran-password}"
# ALLOWED_IP = RU M-Plane IP(s) CallHome source. Comma-separated OK: 10.0.30.103,10.0.30.104
# Start only *adds* ACCEPT for these IPs — never deletes other RU ACCEPTs / never kills their sessions.
ALLOWED_IP="${ALLOWED_IP:-10.0.20.128}"
LOCAL_IP="${LOCAL_IP:-10.0.20.254}"
CALLHOME_PORT="${CALLHOME_PORT:-${PORT:-4334}}"
NETCONF_PORT="${NETCONF_PORT:-830}"
PRODUCT="${PRODUCT:-nDLPU}"

LOG_PATH="${LOG_PATH:-/var/tmp/log/${PRODUCT}}"
CONN_DELAY="${CONN_DELAY:-1}"
POST_LISTEN_WAIT="${POST_LISTEN_WAIT:-0}"
NP2_BOOT_WAIT="${NP2_BOOT_WAIT:-2}"
NP2_YANG_WAIT="${NP2_YANG_WAIT:-90}"
NP2_YANG_WAIT_RECONNECT="${NP2_YANG_WAIT_RECONNECT:-15}"
NP2_SILENT_BOOT_OK="${NP2_SILENT_BOOT_OK:-12}"
INIT_GAP_VERB="${INIT_GAP_VERB:-0.2}"
INIT_GAP_KNOWNHOSTS="${INIT_GAP_KNOWNHOSTS:-0.3}"
LOGIN_POLL_SEC="${LOGIN_POLL_SEC:-0.2}"
# listen --timeout 기본 300s 와 맞춘다. 짧으면 RU 재 CallHome 전에 LOGIN=NOK 로 끊긴다.
LISTEN_TIMEOUT_SEC="${LISTEN_TIMEOUT_SEC:-300}"
LOGIN_WAIT_SEC="${LOGIN_WAIT_SEC:-$LISTEN_TIMEOUT_SEC}"
WAIT_POLL_SEC="${WAIT_POLL_SEC:-0.15}"
MPLANE_GAP_SUBSCRIBE="${MPLANE_GAP_SUBSCRIBE:-1}"
MPLANE_GAP_GET="${MPLANE_GAP_GET:-0.5}"

RPC_OUT_WAIT_ITER="${RPC_OUT_WAIT_ITER:-600}"
EDIT_REPLY_WAIT_ITER="${EDIT_REPLY_WAIT_ITER:-300}"
NETCONF_IDLE_TIMEOUT="${NETCONF_IDLE_TIMEOUT:-120}"
SUPERVISION_INTERVAL="${SUPERVISION_INTERVAL:-60}"
SUPERVISION_EARLY_RESET=$((SUPERVISION_INTERVAL - 10))
SSH_KEEPALIVE_INTERVAL="${SSH_KEEPALIVE_INTERVAL:-30}"
# Old netopeer2-cli (no knownhosts --mode): PTY + send "yes" only if host-key prompt appears.
# Set 0 to disable and use the previous setsid launch (easy revert).
NP2_HOSTKEY_AUTOYES="${NP2_HOSTKEY_AUTOYES:-1}"
CMD_LOCK_FILE="/var/tmp/netconf_tmp/netconf_cmd.lock"
SUPPRESS_NP2_RPC_DUMP_FILE="/var/tmp/netconf_tmp/.suppress_np2_rpc_dump"
EDIT_RPC_CAPTURE_PTR="/var/tmp/netconf_tmp/.edit_rpc_capture_ptr"
# stdin/fifo 브리지는 백그라운드 서브셸이라 부모의 SESSION_ESTABLISHED 변수 변경을 못 본다.
# 파일 플래그로 CallHome 로그인 완료를 공유한다.
SESSION_READY_FILE="/var/tmp/netconf_tmp/.callhome_session_ready"
NETCONF_CONTROL_FIFO="${NETCONF_CONTROL_FIFO:-/var/tmp/netconf_tmp/netconf_control.fifo}"
# 세션 끊김(netopeer 종료·연속 RPC 실패) 후 listen부터 다시 시도 (0이면 한 번만 실행 후 종료)
AUTO_RECONNECT="${AUTO_RECONNECT:-1}"
RECONNECT_DELAY="${RECONNECT_DELAY:-5}"

SCRIPT_LOCK_FILE="/var/tmp/netconf_tmp/miniDU_callhome.lock"
mkdir -p "/var/tmp/netconf_tmp" 2>/dev/null
exec 200>"$SCRIPT_LOCK_FILE"
if ! flock -n 200; then
    echo "[ERROR] Another miniDU_callhome.sh instance is already running ($SCRIPT_LOCK_FILE)." >&2
    exit 1
fi

# -------------------------------------------------------
# Supervision Reset XML 파일 생성
# -------------------------------------------------------
SUPERVISION_RESET="/var/tmp/netconf_tmp/supervision_reset.xml"

mkdir -p "/var/tmp/netconf_tmp" 2>/dev/null

cat <<'EOF' > "$SUPERVISION_RESET"
<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>
EOF

# -------------------------------------------------------
# 상태 추적 변수
# -------------------------------------------------------
SESSION_ESTABLISHED=0
rm -f "$SESSION_READY_FILE" 2>/dev/null || true
LAST_RPC_TIME=$(date +%s)
LAST_SUPERVISION_RESET=$(date +%s)
RPC_RESPONSE_RECEIVED=0
SESSION_ERROR_COUNT=0
MAX_SESSION_ERRORS=5

_set_session_ready() {
    SESSION_ESTABLISHED=1
    : > "$SESSION_READY_FILE"
}

_clear_session_ready() {
    SESSION_ESTABLISHED=0
    rm -f "$SESSION_READY_FILE" 2>/dev/null || true
}

cleanup() {
    echo -e "\n[INFO] Termination signal received. Cleaning up..."
    
    if [[ -n "$TAIL_RUNTIME_PID" ]]; then
        kill $TAIL_RUNTIME_PID >/dev/null 2>&1
    fi

    if [[ -n "$STDIN_BRIDGE_PID" ]]; then
        kill $STDIN_BRIDGE_PID >/dev/null 2>&1
    fi
    if [[ -n "$FIFO_BRIDGE_PID" ]]; then
        kill $FIFO_BRIDGE_PID >/dev/null 2>&1
    fi
    
    if [[ $SESSION_ESTABLISHED -eq 1 ]]; then
        echo "[INFO] Gracefully closing NETCONF session..."
        send_cmd "disconnect" 2>/dev/null
        sleep 2
    fi
    
    exec 5>&- 2>/dev/null
    exec 6<&- 2>/dev/null
    
    if [[ -n "$NP2_PID" ]]; then
        sudo kill -15 $NP2_PID >/dev/null 2>&1
        sleep 1
        sudo kill -9 $NP2_PID >/dev/null 2>&1
    fi
    
    # 동일 포트 규칙이 여러 번 쌓여 있을 수 있어 반복 삭제
    for _i in 1 2 3 4 5 6 7 8; do
        sudo iptables -D INPUT -p tcp --dport "$CALLHOME_PORT" -j DROP >/dev/null 2>&1 || true
        sudo iptables -D INPUT -p tcp --dport "$CALLHOME_PORT" -s "$ALLOWED_IP" -j ACCEPT >/dev/null 2>&1 || true
    done
    
    rm -f "$SUPERVISION_RESET" "$CMD_LOCK_FILE" "$NETCONF_CONTROL_FIFO" "$SUPPRESS_NP2_RPC_DUMP_FILE" "$SESSION_READY_FILE"
    
    echo "[INFO] Cleanup complete. Exiting."
    exit 0
}
trap cleanup EXIT INT TERM

# -------------------------------------------------------
# 로그 설정
# -------------------------------------------------------
log_script_info() {
    # Append only to $LOG; tail -F forwards to GUI (avoid tee+tail duplicate lines).
    echo "$*" >> "$LOG"
}

_log_line_count() {
    wc -l < "$LOG" | tr -d ' '
}

# netopeer2-cli는 verb/knownhosts 성공 시 출력이 없음 — "Verbosity set" 패턴 대기 금지.
wait_for_np2_boot() {
    local timeout="${1:-$NP2_YANG_WAIT}"
    local since_line="${2:-0}"
    local start_ts=$(date +%s)
    local reconnect_cap="${NP2_YANG_WAIT_RECONNECT:-15}"
    local silent_ok="${NP2_SILENT_BOOT_OK:-12}"
    if (( SESSION_ROUND > 1 )) && (( timeout > reconnect_cap )); then
        timeout="$reconnect_cap"
    fi
    log_script_info "[INFO] netopeer2-cli boot wait (up to ${timeout}s, YANG preload)..."
    while true; do
        if ! kill -0 "$NP2_PID" 2>/dev/null; then
            log_script_info "[ERROR] netopeer2-cli exited during boot"
            return 1
        fi
        local cur elapsed
        cur=$(_log_line_count)
        elapsed=$(( $(date +%s) - start_ts ))
        if (( cur > since_line )); then
            log_script_info "[INFO] netopeer2-cli boot output detected (log lines ${since_line} -> ${cur})"
            return 0
        fi
        if (( elapsed >= silent_ok )); then
            log_script_info "[INFO] netopeer2-cli silent ${silent_ok}s — proceeding (PID alive)"
            return 0
        fi
        if (( elapsed >= timeout )); then
            log_script_info "[WARN] netopeer2-cli boot wait ${timeout}s — proceeding (PID alive)"
            return 0
        fi
        sleep 0.25
    done
}

mkdir -p "$LOG_PATH"
LOG="${LOG_PATH}/${PRODUCT}_$(date +'%Y%m%d_%H%M%S').log"
: > "$LOG"
chmod 0644 "$LOG"

echo "------------------------------------------------------------"
echo " [LIVE LOG START] - NETCONF Session with Supervision"
echo "------------------------------------------------------------"
tail -F "$LOG" &
TAIL_RUNTIME_PID=$!

# -------------------------------------------------------
# 명령 송신 함수
# -------------------------------------------------------
_append_rpc_reply_log() {
    local out_path="$1"
    local log_since="${2:-0}"
    local body=""
    local out_body=""
    local cap_file=""
    local has_reply=0

    if [[ -f "$EDIT_RPC_CAPTURE_PTR" ]]; then
        cap_file=$(tr -d '\r\n' < "$EDIT_RPC_CAPTURE_PTR" 2>/dev/null)
    fi
    # stdout capture: often contains only the request (<rpc>…</rpc>) when --out is used,
    # because netopeer2-cli writes <rpc-reply> to --out and may omit it from the dump.
    if [[ -n "$cap_file" && -f "$cap_file" && -s "$cap_file" ]]; then
        body=$(tr -d '\r' < "$cap_file")
    fi
    if [[ -n "$out_path" && -f "$out_path" && -s "$out_path" ]]; then
        out_body=$(tr -d '\r' < "$out_path")
    fi
    if echo "$body" | grep -qiE '<rpc-reply\b|<rpc-error\b|<ok\s*/>|^OK$|^DATA$'; then
        has_reply=1
    fi
    # Merge --out reply when capture has request-only (or is empty).
    if [[ -n "${out_body//[[:space:]]/}" ]]; then
        if [[ $has_reply -eq 0 ]]; then
            if [[ -n "${body//[[:space:]]/}" ]]; then
                body="${body}"$'\n'"${out_body}"
            else
                body="$out_body"
            fi
            has_reply=1
        elif ! echo "$body" | grep -qiE '<ok\s*/>|<rpc-error\b|<data\b' \
            && echo "$out_body" | grep -qiE '<ok\s*/>|<rpc-error\b|<data\b|^OK$|^DATA$'; then
            # Capture has a reply stub but --out has the real payload.
            body="${body}"$'\n'"${out_body}"
        fi
    fi
    if [[ -z "${body//[[:space:]]/}" ]]; then
        body=$(tail -n +$((log_since + 1)) "$LOG" 2>/dev/null | sed -n '/<rpc[ >]/,/<\/rpc-reply>/p' | sed -n '1,800p')
    fi
    {
        flock -x 201
        if [[ -n "${body//[[:space:]]/}" ]]; then
            echo "[GUI] NETCONF RPC exchange begin" >> "$LOG" 2>&1
            printf '%s\n' "$body" >> "$LOG" 2>&1
            echo "[GUI] NETCONF RPC exchange end" >> "$LOG" 2>&1
        else
            echo "[GUI] NETCONF RPC exchange begin" >> "$LOG" 2>&1
            echo "[GUI] (no rpc/rpc-reply XML captured)" >> "$LOG" 2>&1
            echo "[GUI] NETCONF RPC exchange end" >> "$LOG" 2>&1
        fi
        if echo "$body" | grep -qiE '<rpc-error|operation-failed|bad-element|bad-attribute'; then
            echo "[GUI] RPC reply verdict: FAIL (rpc-error)" >> "$LOG" 2>&1
        elif echo "$body" | grep -qiE '<ok\s*/>|^OK$'; then
            echo "[GUI] RPC reply verdict: OK" >> "$LOG" 2>&1
        elif echo "$body" | grep -qiE '<(data)\b|^DATA$'; then
            echo "[GUI] RPC reply verdict: OK (data)" >> "$LOG" 2>&1
        elif [[ -n "${body//[[:space:]]/}" ]]; then
            echo "[WARN] RPC reply verdict: unknown (see NETCONF RPC exchange)" >> "$LOG" 2>&1
        fi
    } 201>"$CMD_LOCK_FILE"
    rm -f "$EDIT_RPC_CAPTURE_PTR"
    [[ -n "$cap_file" ]] && rm -f "$cap_file" 2>/dev/null || true
}

_wait_rpc_out_background() {
    local out_path="$1"
    local log_since="${2:-0}"
    local wait_iter="${3:-$RPC_OUT_WAIT_ITER}"
    (
        for _w in $(seq 1 "$wait_iter"); do
            local cap_file=""
            if [[ -f "$EDIT_RPC_CAPTURE_PTR" ]]; then
                cap_file=$(tr -d '\r\n' < "$EDIT_RPC_CAPTURE_PTR" 2>/dev/null)
            fi
            # Prefer --out (authoritative reply). Do not stop early on request-only capture.
            if [[ -n "$out_path" && -s "$out_path" ]] && grep -qiE '<(rpc-reply|rpc-error|data)\b|<ok\s*/>|^OK$|^DATA$' "$out_path" 2>/dev/null; then
                break
            fi
            if [[ -n "$cap_file" && -f "$cap_file" ]] && grep -qiE '</rpc-reply>|<ok\s*/>' "$cap_file" 2>/dev/null; then
                # Only accept capture as complete if it already includes a reply.
                break
            fi
            sleep 0.1
        done
        _append_rpc_reply_log "$out_path" "$log_since"
        rm -f "$SUPPRESS_NP2_RPC_DUMP_FILE"
    ) &
}

_wait_edit_config_reply_background() {
    local log_before="$1"
    local wait_iter="${2:-$EDIT_REPLY_WAIT_ITER}"
    (
        for _w in $(seq 1 "$wait_iter"); do
            if tail -n +$((log_before + 1)) "$LOG" 2>/dev/null | grep -qE '<(rpc-reply|rpc-error)\b|</rpc-reply>|^OK$'; then
                break
            fi
            sleep 0.1
        done
        if tail -n +$((log_before + 1)) "$LOG" 2>/dev/null | grep -qE '<(rpc-reply|rpc-error)\b'; then
            {
                flock -x 201
                echo "[GUI] edit-config reply xml begin" >> "$LOG" 2>&1
                tail -n +$((log_before + 1)) "$LOG" | sed -n '/<[rR][pP][cC]-reply/,/<\/[rR][pP][cC]-reply>/p' | sed -n '1,400p' >> "$LOG" 2>&1
                echo "[GUI] edit-config reply xml end" >> "$LOG" 2>&1
            } 201>"$CMD_LOCK_FILE"
        elif tail -n +$((log_before + 1)) "$LOG" 2>/dev/null | grep -qE '^OK$'; then
            echo "[GUI] edit-config reply: OK" >> "$LOG" 2>&1
        elif tail -n +$((log_before + 1)) "$LOG" 2>/dev/null | grep -qiE 'rpc-error|operation-failed'; then
            echo "[GUI] edit-config reply: rpc-error (see NETCONF lines above)" >> "$LOG" 2>&1
        else
            echo "[WARN] edit-config reply not seen in log within $((wait_iter / 10))s" >> "$LOG" 2>&1
        fi
    ) &
}

send_cmd() {
    local cmd="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S.%3N')
    local content_path=""
    local out_path=""
    local config_path=""
    local _log_before=""
    local _edit_via_rpc=0
    content_path=$(printf '%s\n' "$cmd" | sed -n -E 's/.*--content[= ]([^[:space:]]+).*/\1/p' | tr -d '\r"'"'"'')
    out_path=$(printf '%s\n' "$cmd" | sed -n -E 's/.*--out[= ]([^[:space:]]+).*/\1/p' | tr -d '\r"'"'"'')
    config_path=$(printf '%s\n' "$cmd" | sed -n -E 's/.*--config[= ]([^[:space:]]+).*/\1/p' | tr -d '\r"'"'"'')
    # edit-config: netopeer2-cli는 OK 한 줄만 stdout에 남기고 rpc-reply는 suppress에 가려질 수 있음 → user-rpc --out 으로 응답 파일 캡처
    if [[ "$cmd" == *edit-config* && -n "$config_path" && -z "$out_path" && -f "$config_path" ]]; then
        local _ec_target _ec_defop _wrap
        _ec_target=$(printf '%s\n' "$cmd" | sed -n -E 's/.*--target[ =]+([^ ]+).*/\1/p' | tr -d '\r')
        _ec_defop=$(printf '%s\n' "$cmd" | sed -n -E 's/.*--defop[ =]+([^ ]+).*/\1/p' | tr -d '\r')
        _ec_target="${_ec_target:-running}"
        _ec_defop="${_ec_defop:-merge}"
        _wrap="/var/tmp/netconf_tmp/gui_edit_wrap_${RANDOM}_${SECONDS}.xml"
        out_path="/var/tmp/netconf_tmp/gui_edit_out_${RANDOM}_${SECONDS}.xml"
        {
            echo '<edit-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
            printf '  <target><%s/></target>\n' "$_ec_target"
            printf '  <default-operation>%s</default-operation>\n' "$_ec_defop"
            echo '  <config>'
            sed 's/^/    /' "$config_path"
            echo '  </config>'
            echo '</edit-config>'
        } > "$_wrap"
        content_path="$_wrap"
        cmd="user-rpc --content ${_wrap} --out ${out_path}"
        _edit_via_rpc=1
    fi
    # user-rpc without --out: capture reply (supervision reset은 stdout 응답 유지)
    if [[ "$cmd" == *user-rpc* && -n "$content_path" && -z "$out_path" ]]; then
        if [[ "$content_path" != *supervision_reset.xml* ]]; then
            out_path="/var/tmp/netconf_tmp/gui_rpc_out_${RANDOM}_${SECONDS}.xml"
            cmd="$cmd --out $out_path"
        fi
    fi

    # user-rpc --out (GET/SET): stdout rpc dump → capture 파일, LOG에는 NETCONF RPC exchange 한 번만.
    if [[ -n "$content_path" && -n "$out_path" ]]; then
        local _rpc_cap="/var/tmp/netconf_tmp/gui_rpc_exchange_${RANDOM}_${SECONDS}.xml"
        : > "$_rpc_cap"
        echo "$_rpc_cap" > "$EDIT_RPC_CAPTURE_PTR"
        : > "$SUPPRESS_NP2_RPC_DUMP_FILE" 2>/dev/null || touch "$SUPPRESS_NP2_RPC_DUMP_FILE"
    fi
    _log_before=$(wc -l < "$LOG" | tr -d ' ')
    {
        flock -x 201
        echo "[$timestamp] CLIENT_SENT: $cmd" >> "$LOG" 2>&1
        # --out 사용 시 요청 XML은 NETCONF RPC exchange에 포함 (GET/SET 중복 로그 방지).
        if [[ -n "$content_path" && -f "$content_path" && -z "$out_path" ]]; then
            if [[ "$content_path" != *supervision_reset.xml* ]]; then
                echo "[GUI] user-rpc request xml begin ($content_path)" >> "$LOG" 2>&1
                sed -n '1,800p' "$content_path" >> "$LOG" 2>&1
                echo "[GUI] user-rpc request xml end ($content_path)" >> "$LOG" 2>&1
            fi
        fi
        if [[ -n "$config_path" && -f "$config_path" && "$_edit_via_rpc" != "1" ]]; then
            echo "[GUI] edit-config config xml begin ($config_path)" >> "$LOG" 2>&1
            sed -n '1,1200p' "$config_path" >> "$LOG" 2>&1
            echo "[GUI] edit-config config xml end ($config_path)" >> "$LOG" 2>&1
        fi
        echo "$cmd" >&5
    } 201>"$CMD_LOCK_FILE"
    # Do not block stdin/FIFO bridge on RPC reply (GET --out used to stall edit-config ~60s).
    if [[ -n "$out_path" ]]; then
        _wait_rpc_out_background "$out_path" "$_log_before" &
    elif [[ -n "$config_path" ]]; then
        _wait_edit_config_reply_background "$_log_before" &
    fi
    LAST_RPC_TIME=$(date +%s)
    RPC_RESPONSE_RECEIVED=0
}

# -------------------------------------------------------
# 외부(stdin) 명령 브릿지: GUI 입력을 netopeer2-cli로 전달
# CallHome 로그인 전에는 GUI RPC를 절대 넣지 않는다.
# (재연결 직후 버퍼에 남은 edit-config 가 verb/listen 보다 먼저 나가면 세션이 즉시 죽는다)
# -------------------------------------------------------
_bridge_should_accept() {
    # 메인 루프의 send_cmd(verb/listen/supervision)는 브리지를 거치지 않음.
    # GUI/FIFO 입력만 로그인 이후에 통과시킨다.
    # (서브셸은 부모 변수 갱신을 못 보므로 파일 플래그를 본다)
    [[ -f "$SESSION_READY_FILE" ]]
}

stdin_bridge() {
    echo "[INFO] External stdin bridge started." >> "$LOG"
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if ! _bridge_should_accept "$line"; then
            echo "[WARN] Ignoring command before CallHome login: $line" >> "$LOG"
            continue
        fi
        echo "[INFO] External command received: $line" >> "$LOG"
        send_cmd "$line"
    done
    echo "[INFO] External stdin bridge ended (EOF)." >> "$LOG"
}

fifo_bridge() {
    echo "[INFO] External FIFO bridge started: $NETCONF_CONTROL_FIFO" >> "$LOG"
    while true; do
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            if ! _bridge_should_accept "$line"; then
                echo "[WARN] Ignoring FIFO command before CallHome login: $line" >> "$LOG"
                continue
            fi
            echo "[INFO] External FIFO command received: $line" >> "$LOG"
            send_cmd "$line"
        done < "$NETCONF_CONTROL_FIFO"
        sleep 0.1
    done
}

# -------------------------------------------------------
# 응답 대기 함수
# -------------------------------------------------------
wait_for_response() {
    local expected_pattern="$1"
    local timeout="${2:-}"
    local since_line="${3:-}"
    local start_time=$(date +%s)
    if [[ -z "$timeout" || ! "$timeout" =~ ^[0-9]+$ ]]; then
        timeout="${NETCONF_RPC_TIMEOUT:-30}"
    fi
    
    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -gt $timeout ]]; then
            echo "[ERROR] Response timeout after ${timeout}s for pattern: $expected_pattern" >> "$LOG"
            return 1
        fi
        
        if [[ -n "$since_line" && "$since_line" =~ ^[0-9]+$ ]]; then
            if tail -n "+$((since_line + 1))" "$LOG" 2>/dev/null | grep -qiE "$expected_pattern"; then
                RPC_RESPONSE_RECEIVED=1
                return 0
            fi
        elif tail -n 100 "$LOG" | grep -qiE "$expected_pattern"; then
            RPC_RESPONSE_RECEIVED=1
            return 0
        fi
        
        sleep "$WAIT_POLL_SEC"
    done
}

# 세션만 재시작 (iptables·tail·로그 파일 유지). 재연결 루프에서 사용.
teardown_netopeer_for_reconnect() {
    log_script_info "[INFO] netopeer2-cli 세션 종료 — 브리지·프로세스 정리 중..."
    _clear_session_ready
    rm -f "$SUPPRESS_NP2_RPC_DUMP_FILE" "$EDIT_RPC_CAPTURE_PTR" 2>/dev/null || true
    if [[ -n "${STDIN_BRIDGE_PID:-}" ]]; then
        kill "$STDIN_BRIDGE_PID" >/dev/null 2>&1 || true
        wait "$STDIN_BRIDGE_PID" 2>/dev/null || true
    fi
    if [[ -n "${FIFO_BRIDGE_PID:-}" ]]; then
        kill "$FIFO_BRIDGE_PID" >/dev/null 2>&1 || true
        wait "$FIFO_BRIDGE_PID" 2>/dev/null || true
    fi
    if [[ -n "${NP2_PID:-}" ]]; then
        # close-session best-effort (may already be dead)
        if kill -0 "$NP2_PID" 2>/dev/null; then
            echo "disconnect" >&5 2>/dev/null || true
            sleep 0.5
        fi
        sudo kill -15 "$NP2_PID" >/dev/null 2>&1 || true
        sleep 1
        sudo kill -9 "$NP2_PID" >/dev/null 2>&1 || true
    fi
    exec 5>&- 2>/dev/null || true
    rm -f "$NETCONF_CONTROL_FIFO"
    STDIN_BRIDGE_PID=
    FIFO_BRIDGE_PID=
    NP2_PID=
}

# -------------------------------------------------------
# 네트워크 (한 번만)
# CallHome 포트: 이전 실행에서 쌓인 ACCEPT/DROP 을 비운 뒤
#   1) ALLOWED_IP(RU) 만 ACCEPT
#   2) 그 외 동일 포트 DROP
# -------------------------------------------------------
# ALLOWED_IP may be a single IP or comma/space-separated list (e.g. 10.0.30.103,10.0.30.104).
_allowed_ip_list() {
    echo "${ALLOWED_IP}" | tr ',;' ' ' | xargs -n1 echo 2>/dev/null
}

_callhome_iptables_reset() {
    local _ip _rules _has_drop=0 _dup_drop _ok=0
    # Additive only: never delete other RU ACCEPT rules, never kill other sessions.
    _rules=$(sudo -n iptables -S INPUT 2>/dev/null | grep -E -- "--dport[= ]${CALLHOME_PORT}([[:space:]]|$)" || true)
    while read -r _ip; do
        [[ -z "$_ip" ]] && continue
        if echo "$_rules" | grep -Fq -- "-s ${_ip}/32" || echo "$_rules" | grep -Fq -- "-s ${_ip} "; then
            _ok=1
            continue
        fi
        if sudo -n iptables -I INPUT -p tcp --dport "$CALLHOME_PORT" -s "$_ip" -j ACCEPT; then
            _ok=1
            log_script_info "[INFO] iptables: added ACCEPT tcp/${CALLHOME_PORT} from ${_ip}"
        fi
    done < <(_allowed_ip_list)
    _rules=$(sudo -n iptables -S INPUT 2>/dev/null | grep -E -- "--dport[= ]${CALLHOME_PORT}([[:space:]]|$)" || true)
    if echo "$_rules" | grep -q -- '-j DROP'; then
        _has_drop=1
    fi
    if [[ $_has_drop -eq 0 ]]; then
        sudo -n iptables -A INPUT -p tcp --dport "$CALLHOME_PORT" -j DROP || true
    else
        # Collapse duplicate DROP lines only (keep a single DROP). Do not touch ACCEPTs.
        _dup_drop=0
        while read -r _line; do
            [[ "$_line" == *"-j DROP"* ]] || continue
            _dup_drop=$((_dup_drop + 1))
            if [[ $_dup_drop -gt 1 ]]; then
                _spec=${_line#-A INPUT }
                # shellcheck disable=SC2086
                sudo -n iptables -D INPUT ${_spec} >/dev/null 2>&1 || true
            fi
        done <<< "$_rules"
    fi
    if [[ $_ok -ne 1 ]]; then
        log_script_info "[WARN] iptables CallHome ACCEPT add failed (sudo -n?). Check ALLOWED_IP=${ALLOWED_IP}."
        return 1
    fi
    log_script_info "[INFO] iptables ${CALLHOME_PORT} now: $(sudo -n iptables -S INPUT 2>/dev/null | grep -E -- "--dport[= ]${CALLHOME_PORT}" | tr '\n' ' ' | head -c 400)"
    return 0
}

_callhome_iptables_reset
log_script_info "[INFO] iptables CallHome: ensure ACCEPT for ${ALLOWED_IP} on tcp/${CALLHOME_PORT} (other RU rules kept)"

echo "[INFO] USER=$USER, ALLOWED_IP=$ALLOWED_IP, LOCAL_IP=$LOCAL_IP, CALLHOME_PORT=$CALLHOME_PORT, CONN_DELAY=${CONN_DELAY}s"
log_script_info "[INFO] CallHome expect: RU(${ALLOWED_IP}) -> client(${LOCAL_IP}:${CALLHOME_PORT}), login=${USER}"
if [[ "$CONN_DELAY" != "0" && -n "$CONN_DELAY" ]]; then
    echo "[INFO] Waiting ${CONN_DELAY}s for network stabilization..."
    sleep "$CONN_DELAY"
fi

# Filter low-level transport noise before writing to log.
noise_filter() {
    local in_rpc_dump=0
    local _edit_cap_file=""
    while IFS= read -r line; do
        if [[ -f "$EDIT_RPC_CAPTURE_PTR" ]]; then
            _edit_cap_file=$(tr -d '\r\n' < "$EDIT_RPC_CAPTURE_PTR" 2>/dev/null)
        else
            _edit_cap_file=""
        fi
        # GET/SET user-rpc --out: hide CLI rpc dump (XML은 capture 파일로만 보관).
        if [[ -f "$SUPPRESS_NP2_RPC_DUMP_FILE" ]]; then
            if [[ $in_rpc_dump -eq 1 ]]; then
                if [[ -n "$_edit_cap_file" ]]; then
                    printf '%s\n' "$line" >> "$_edit_cap_file"
                fi
                case "$line" in
                    *"</rpc-reply>"*)
                        in_rpc_dump=0
                        # Mirror a short success marker to LOG so wait_for_response /
                        # supervision loop still see a reply while dump is suppressed.
                        echo "OK"
                        ;;
                    *"</rpc>"*|*"</data>"*)
                        in_rpc_dump=0
                        ;;
                esac
                continue
            fi
            case "$line" in
                "<rpc "*|"<rpc>"|"<rpc-reply "*|"<rpc-reply>")
                    if [[ -n "$_edit_cap_file" ]]; then
                        printf '%s\n' "$line" >> "$_edit_cap_file"
                    fi
                    in_rpc_dump=1
                    continue
                    ;;
                DATA|"<data "*|"<data>")
                    if [[ -n "$_edit_cap_file" ]]; then
                        printf '%s\n' "$line" >> "$_edit_cap_file"
                    fi
                    in_rpc_dump=1
                    continue
                    ;;
                OK)
                    # Keep bare OK visible even while another --out capture is active.
                    echo "OK"
                    continue
                    ;;
            esac
        fi
        # Keep auth / host-key prompt lines even under "nc DEBUG: SSH:" (needed for LOGIN=OK).
        case "$line" in
            *[Aa]uthenticat*|*[Aa]ccess\ granted*|*Permission\ denied*|*authenticity\ of\ the\ host*|*Are\ you\ sure\ you\ want\ to\ continue\ connecting*)
                echo "$line"
                continue
                ;;
            *"ssh_packet_"*|*"ssh_socket_"*|*"channel_rcv_data"*|*"channel_default_bufferize"*|*"channel windows are now"*|*"Read ("*"buffered"*|*"Dispatching handler for packet type"*|*"bytes left in socket buffer"*)
                continue
                ;;
            *"nc DEBUG: SSH:"*)
                continue
                ;;
            *"nc DEBUG:"*|*"nc VERBOSE:"*)
                case "$line" in
                    *hello*|*Hello*|*rpc-reply*|*Accepted\ a\ connection*|*[Cc]all*[Hh]ome*|*authenticity*|*Are\ you\ sure*)
                        ;; # keep
                    *)
                        continue
                        ;;
                esac
                ;;
        esac
        echo "$line"
    done
}

# true = old cli needs host-key yes (no --mode skip). Normal server with --mode → false.
_np2_needs_hostkey_autoyes() {
    [[ "${NP2_HOSTKEY_AUTOYES:-1}" == "1" ]] || return 1
    local bin
    bin=$(command -v netopeer2-cli 2>/dev/null) || return 1
    if strings "$bin" 2>/dev/null | grep -qF '--mode <accept|accept-new|ask|skip|strict>'; then
        return 1
    fi
    return 0
}

# stdin 복사는 프로세스당 한 번 (재연결 시 브리지만 다시 띄움)
exec 6<&0

SESSION_ROUND=0

while true; do
    ((SESSION_ROUND++)) || true
    log_script_info "[INFO] ========== Call Home session round ${SESSION_ROUND} (pid $$) =========="

    _np2_since=$(_log_line_count)
    # Old cli: no setsid so sshpass PTY stays controlling tty (host-key yes/no + password).
    # New cli (--mode skip): keep setsid for SIGHUP resilience on normal server.
    _NP2_HK_AUTOYES=0
    if _np2_needs_hostkey_autoyes; then
        _NP2_HK_AUTOYES=1
        log_script_info "[INFO] Host-key auto-yes: ON (old cli, no --mode skip). Revert: NP2_HOSTKEY_AUTOYES=0"
        coproc NP2 {
            stdbuf -oL -eL sshpass -p "$PASSWORD" netopeer2-cli 2>&1 | noise_filter
        } >> "$LOG" 2>&1
    else
        coproc NP2 {
            stdbuf -oL -eL setsid sshpass -p "$PASSWORD" netopeer2-cli \
                -o "ServerAliveInterval=$SSH_KEEPALIVE_INTERVAL" \
                -o "ServerAliveCountMax=3" \
                -o "TCPKeepAlive=yes" \
                2>&1 | noise_filter
        } >> "$LOG" 2>&1
    fi
    NP2_PID=$!
    exec 5>&${NP2[1]}

    stdin_bridge <&6 &
    STDIN_BRIDGE_PID=$!
    rm -f "$NETCONF_CONTROL_FIFO"
    mkfifo "$NETCONF_CONTROL_FIFO"
    fifo_bridge &
    FIFO_BRIDGE_PID=$!

    if ! wait_for_np2_boot "$NP2_YANG_WAIT" "$_np2_since"; then
        teardown_netopeer_for_reconnect
        if [[ "$AUTO_RECONNECT" != "1" ]]; then
            log_script_info "[ERROR] AUTO_RECONNECT off — exiting."
            exit 1
        fi
        log_script_info "[INFO] Reconnecting in ${RECONNECT_DELAY}s..."
        sleep "$RECONNECT_DELAY"
        continue
    fi

    if [[ "$NP2_BOOT_WAIT" != "0" && -n "$NP2_BOOT_WAIT" ]]; then
        sleep "$NP2_BOOT_WAIT"
    fi

    # verb / knownhosts: netopeer2-cli는 성공 시 메시지 없음 → 전송 후 짧은 gap만
    send_cmd "verb 3"
    if [[ "$INIT_GAP_VERB" != "0" && -n "$INIT_GAP_VERB" ]]; then
        sleep "$INIT_GAP_VERB"
    fi

    # Newer cli: skip hostkey checks. Old 2.1.71 has no --mode (error is harmless).
    send_cmd "knownhosts --mode skip"
    if [[ "$INIT_GAP_KNOWNHOSTS" != "0" && -n "$INIT_GAP_KNOWNHOSTS" ]]; then
        sleep "$INIT_GAP_KNOWNHOSTS"
    fi

    if ! kill -0 "$NP2_PID" 2>/dev/null; then
        log_script_info "[WARN] netopeer2-cli died after init — restarting round"
        teardown_netopeer_for_reconnect
        if [[ "$AUTO_RECONNECT" != "1" ]]; then
            log_script_info "[ERROR] AUTO_RECONNECT off — exiting."
            exit 1
        fi
        log_script_info "[INFO] Reconnecting in ${RECONNECT_DELAY}s..."
        sleep "$RECONNECT_DELAY"
        continue
    fi

    # LOCAL_IP 가 이 서버에 없으면 listen bind 실패 → CallHome 불가
    if ! ip -4 addr show 2>/dev/null | grep -q "inet ${LOCAL_IP}/"; then
        log_script_info "[ERROR] LOCAL_IP=${LOCAL_IP} is not configured on this host. CallHome listen cannot bind."
        log_script_info "[ERROR] ip -4 addr: $(ip -4 addr show 2>/dev/null | grep 'inet ' | tr '\n' ' ' | head -c 400)"
        teardown_netopeer_for_reconnect
        if [[ "$AUTO_RECONNECT" != "1" ]]; then
            exit 1
        fi
        sleep "$RECONNECT_DELAY"
        continue
    fi

    log_script_info "[INFO] Starting CallHome listener on ${CALLHOME_PORT} (expect RU from ${ALLOWED_IP})..."
    # 이번 라운드 listen 이후 줄만 검사 (재연결 시 이전 세션 로그와 혼동 방지)
    LISTEN_LOG_START=$(wc -l < "$LOG" | tr -d ' ')
    send_cmd "listen --host $LOCAL_IP --port $CALLHOME_PORT --login $USER --timeout $LISTEN_TIMEOUT_SEC"
    if [[ "$POST_LISTEN_WAIT" != "0" && -n "$POST_LISTEN_WAIT" ]]; then
        sleep "$POST_LISTEN_WAIT"
    fi

    # listen 소켓이 실제로 열렸는지 확인 (안 열리면 120s 기다려도 CallHome 불가)
    _listen_up=0
    for _w in $(seq 1 50); do
        if ss -tnlp 2>/dev/null | grep -qE ":${CALLHOME_PORT}\\b"; then
            _listen_up=1
            break
        fi
        sleep 0.2
    done
    if [[ $_listen_up -ne 1 ]]; then
        log_script_info "[WARN] listen port ${CALLHOME_PORT} not open after listen cmd — retry bind 0.0.0.0"
        send_cmd "listen --host 0.0.0.0 --port $CALLHOME_PORT --login $USER --timeout $LISTEN_TIMEOUT_SEC"
        sleep 1
        if ss -tnlp 2>/dev/null | grep -qE ":${CALLHOME_PORT}\\b"; then
            _listen_up=1
            log_script_info "[INFO] CallHome listen OK on 0.0.0.0:${CALLHOME_PORT}"
        else
            log_script_info "[ERROR] CallHome listen FAILED — port ${CALLHOME_PORT} still not listening."
            log_script_info "[WARN] [DIAG] ss: $(ss -tnlp 2>/dev/null | tr '\n' ' ' | head -c 400)"
            teardown_netopeer_for_reconnect
            if [[ "$AUTO_RECONNECT" != "1" ]]; then
                exit 1
            fi
            sleep "$RECONNECT_DELAY"
            continue
        fi
    else
        log_script_info "[INFO] CallHome listen OK: $(ss -tnlp 2>/dev/null | grep -E ":${CALLHOME_PORT}\\b" | tr '\n' ' ' | head -c 240)"
    fi

    ########################################################################################
    ######## STEP 1 & 2. 검증 로직 (CallHome & Login) ########
    ########################################################################################
    log_script_info "[INFO] Waiting for client connection (log from line ${LISTEN_LOG_START}, up to ${LOGIN_WAIT_SEC}s)..."

    LOGIN="NOK"
    CALLHOME_SEEN=0
    _estab_hits=0
    LOGIN_FAIL_REASON="timeout"
    login_deadline=$(( $(date +%s) + LOGIN_WAIT_SEC ))
    # netopeer2 / libnetconf2 버전별 성공 문구 차이 흡수
    # NOTE: do not match our own "[INFO] ... Authentication successful" wait text.
    AUTH_OK_RE='Authentication successful|User authenticated successfully|Authenticated successfully|Access granted'
    AUTH_FAIL_RE='authentication failed|Authentication failed|Permission denied|auth fail'
    ACCEPT_RE="Accepted a (new )?connection on ${LOCAL_IP}:${CALLHOME_PORT}|Accepted a (new )?connection|Incoming connection|call.?home"

    _ss_has_estab_from_ru() {
        # Any ALLOWED_IP (comma-list OK) ESTAB to CallHome port.
        local _ip
        while read -r _ip; do
            [[ -z "$_ip" ]] && continue
            if ss -tn 2>/dev/null | grep -E "ESTAB" | grep -qE "${_ip}.*:${CALLHOME_PORT}|:${CALLHOME_PORT}.*${_ip}"; then
                return 0
            fi
        done < <(_allowed_ip_list)
        return 1
    }

    _slice_has_auth_ok() {
        echo "$1" | grep -a -viE '^\s*\[(INFO|WARN|ERROR|GUI|Conformance)' | grep -a -qiE "$AUTH_OK_RE"
    }

    _hostkey_yes_sent=0
    while [[ $(date +%s) -lt $login_deadline ]]; do
        if ! kill -0 "$NP2_PID" 2>/dev/null; then
            log_script_info "[WARN] netopeer2-cli died during CallHome login wait."
            LOGIN="NOK"
            LOGIN_FAIL_REASON="np2_dead"
            break
        fi

        _slice=$(tail -n "+${LISTEN_LOG_START}" "$LOG" 2>/dev/null || true)

        if _slice_has_auth_ok "$_slice"; then
            LOGIN="OK"
            _set_session_ready
            log_script_info "[INFO] Login successful (netopeer auth OK)."
            log_script_info "[INFO] GUI/FIFO send enabled (session ready file set)."
            break
        fi

        if echo "$_slice" | grep -a -viE '^\s*\[(INFO|WARN|ERROR|GUI|Conformance)' | grep -a -qiE "$AUTH_FAIL_RE"; then
            log_script_info "[ERROR] Authentication failed (this round). Check USER/PASSWORD."
            LOGIN="FAIL"
            LOGIN_FAIL_REASON="auth_failed"
            break
        fi

        if [[ $CALLHOME_SEEN -eq 0 ]] && echo "$_slice" | grep -a -qiE "$ACCEPT_RE"; then
            CALLHOME_SEEN=1
            # Wording must NOT contain AUTH_OK_RE (false LOGIN match).
            log_script_info "[INFO] CallHome TCP accepted — waiting for netopeer auth OK..."
            # Old cli often prompts host-key right after accept; prompt may stay on pty.
            if [[ "${_NP2_HK_AUTOYES:-0}" == "1" && $_hostkey_yes_sent -eq 0 ]]; then
                sleep 0.3
                log_script_info "[INFO] Host-key auto-yes: sending yes after CallHome accept"
                printf 'yes\n' >&5 2>/dev/null || true
                _hostkey_yes_sent=1
            fi
        fi

        # If prompt text appears later in log, send yes once (idempotent).
        if [[ "${_NP2_HK_AUTOYES:-0}" == "1" && $_hostkey_yes_sent -eq 0 ]] \
            && echo "$_slice" | grep -aqiE 'Are you sure you want to continue connecting'; then
            log_script_info "[INFO] Host-key prompt detected — sending yes"
            printf 'yes\n' >&5 2>/dev/null || true
            _hostkey_yes_sent=1
        fi

        # ESTAB = CallHome TCP only. Never LOGIN=OK by itself (TCP != NETCONF session).
        if _ss_has_estab_from_ru; then
            CALLHOME_SEEN=1
        fi

        # listen 자체 타임아웃/에러
        if echo "$_slice" | grep -a -qiE 'listen timeout|Listening failed|Address already in use|bind failed|Cannot bind|bind\(\) failed'; then
            log_script_info "[WARN] listen ended/failed before login (see NETCONF log)."
            LOGIN="NOK"
            LOGIN_FAIL_REASON="listen_failed"
            break
        fi

        sleep "$LOGIN_POLL_SEC"
    done

    if [[ "$LOGIN" != "OK" ]]; then
        if [[ "$LOGIN" == "FAIL" ]]; then
            log_script_info "[WARN] Login not established this round (LOGIN=FAIL reason=${LOGIN_FAIL_REASON})."
        elif [[ $CALLHOME_SEEN -eq 1 ]]; then
            log_script_info "[WARN] Login not established this round (LOGIN=NOK reason=auth_pending CallHome seen, no Authentication successful within ${LOGIN_WAIT_SEC}s)."
        else
            log_script_info "[WARN] Login not established this round (LOGIN=NOK reason=${LOGIN_FAIL_REASON} no CallHome from ${ALLOWED_IP} -> ${LOCAL_IP}:${CALLHOME_PORT} within ${LOGIN_WAIT_SEC}s)."
            log_script_info "[WARN] Check: (1) Settings ALLOWED_IP == RU M-Plane IP (2) RU CallHome server=${LOCAL_IP}:${CALLHOME_PORT} (3) RU has no stale NETCONF session to this host (4) iptables."
        fi
        log_script_info "[WARN] [DIAG] ss listen ${CALLHOME_PORT}: $(ss -tnlp 2>/dev/null | grep -E ":${CALLHOME_PORT}\\b" | tr '\n' ' ' | head -c 300)"
        log_script_info "[WARN] [DIAG] ss estab: $(ss -tn 2>/dev/null | grep -E ":${CALLHOME_PORT}\\b" | tr '\n' ' ' | head -c 300)"
        log_script_info "[WARN] [DIAG] iptables ${CALLHOME_PORT}: $(sudo iptables -S INPUT 2>/dev/null | grep -E "dport ${CALLHOME_PORT}" | tr '\n' ' ' | head -c 400)"
        log_script_info "[WARN] [DIAG] If RU 'show netconf-server session' still lists source-host=${LOCAL_IP}, clear that session on RU so it can CallHome again."
        teardown_netopeer_for_reconnect
        if [[ "$AUTO_RECONNECT" != "1" ]]; then
            log_script_info "[ERROR] AUTO_RECONNECT off — exiting."
            exit 1
        fi
        # LOGIN 실패가 반복되면 재시도 간격을 늘려 RU/포트 회복 여유를 준다.
        _backoff="$RECONNECT_DELAY"
        if [[ "${SESSION_ROUND:-1}" -ge 5 ]]; then
            _backoff=$(( RECONNECT_DELAY * 3 ))
        fi
        if [[ "${SESSION_ROUND:-1}" -ge 20 ]]; then
            _backoff=$(( RECONNECT_DELAY * 6 ))
        fi
        log_script_info "[INFO] Reconnecting in ${_backoff}s... (AUTO_RECONNECT=1, round=${SESSION_ROUND}, reason=${LOGIN_FAIL_REASON})"
        sleep "$_backoff"
        continue
    fi

    log_script_info "[INFO] CallHome session established on ${CALLHOME_PORT}. Keeping session active."

    ########################################################################################
    ###### STEP 3. M-Plane 활성화 (Supervision 리셋 루프) ##############################
    ########################################################################################

    echo "[INFO] ========== M-PLANE ACTIVATION START =========="

    echo "[INFO] Step 1: Subscribing to events..."
    send_cmd "subscribe"
    wait_for_response "OK|rpc-reply|Subscribed" 15 || echo "[WARN] subscribe response timeout"
    if [[ "$MPLANE_GAP_SUBSCRIBE" != "0" && -n "$MPLANE_GAP_SUBSCRIBE" ]]; then
        sleep "$MPLANE_GAP_SUBSCRIBE"
    fi

    echo "[INFO] Step 2: Requesting sync status..."
    send_cmd "get --filter-xpath /o-ran-sync:sync"
    wait_for_response "rpc-reply|sync" 15 || echo "[WARN] get-sync response timeout"
    if [[ "$MPLANE_GAP_GET" != "0" && -n "$MPLANE_GAP_GET" ]]; then
        sleep "$MPLANE_GAP_GET"
    fi

    echo "[INFO] Step 3: Starting Supervision Watchdog Loop (reset every ${SUPERVISION_EARLY_RESET}s)..."
    echo "[INFO] ========== SUPERVISION ACTIVE =========="

    LOOP_COUNT=0
    SESSION_ALIVE_COUNTER=0
    SESSION_ERROR_COUNT=0
    LAST_SUPERVISION_RESET=$(date +%s)

    SUPERVISION_EXIT_REASON="np2_dead"

    while kill -0 "$NP2_PID" 2>/dev/null; do
        CURRENT_TIME=$(date +%s)
        TIME_SINCE_RESET=$((CURRENT_TIME - LAST_SUPERVISION_RESET))

        if [[ $TIME_SINCE_RESET -ge $SUPERVISION_EARLY_RESET ]]; then
            ((LOOP_COUNT++))
            echo "[INFO] [Loop #$LOOP_COUNT] Sending Supervision Watchdog Reset (elapsed: ${TIME_SINCE_RESET}s)..."

            # Use --out + since_line so success is not confused with older rpc-reply in LOG,
            # and so concurrent GUI --out capture cannot hide the supervision reply.
            _sup_out="/var/tmp/netconf_tmp/supervision_out_${RANDOM}_${SECONDS}.xml"
            _sup_mark=$(wc -l < "$LOG" | tr -d ' ')
            send_cmd "user-rpc --content=$SUPERVISION_RESET --out $_sup_out"

            _sup_ok=0
            _sup_deadline=$(( $(date +%s) + ${NETCONF_RPC_TIMEOUT:-30} ))
            while [[ $(date +%s) -lt $_sup_deadline ]]; do
                if [[ -s "$_sup_out" ]] && grep -qiE '<ok\s*/>|<rpc-reply\b|^OK$' "$_sup_out" 2>/dev/null; then
                    _sup_ok=1
                    break
                fi
                if tail -n "+$((_sup_mark + 1))" "$LOG" 2>/dev/null | grep -qiE '<ok\s*/>|<rpc-reply\b|^OK$'; then
                    _sup_ok=1
                    break
                fi
                sleep "$WAIT_POLL_SEC"
            done
            rm -f "$_sup_out" 2>/dev/null || true

            if [[ $_sup_ok -eq 1 ]]; then
                echo "[INFO] [Loop #$LOOP_COUNT] Supervision reset successful."
                ((SESSION_ALIVE_COUNTER++))
                SESSION_ERROR_COUNT=0
            else
                echo "[WARN] [Loop #$LOOP_COUNT] Supervision reset timeout. Attempting heartbeat..."

                _hb_mark=$(wc -l < "$LOG" | tr -d ' ')
                send_cmd "get --filter-xpath /ietf-netconf-monitoring:netconf-state/sessions"
                if wait_for_response "OK|rpc-reply|data|sessions" "$NETCONF_RPC_TIMEOUT" "$_hb_mark"; then
                    echo "[INFO] [Loop #$LOOP_COUNT] Heartbeat successful, session alive."
                    ((SESSION_ALIVE_COUNTER++))
                    SESSION_ERROR_COUNT=0
                else
                    echo "[ERROR] [Loop #$LOOP_COUNT] Both supervision reset and heartbeat failed."
                    ((SESSION_ERROR_COUNT++))
                fi
            fi

            LAST_SUPERVISION_RESET=$CURRENT_TIME

            if [[ $SESSION_ERROR_COUNT -ge $MAX_SESSION_ERRORS ]]; then
                echo "[ERROR] Max session errors ($MAX_SESSION_ERRORS) reached."
                SUPERVISION_EXIT_REASON="max_errors"
                break
            fi
        fi

        # Disabled: idle heartbeat used unfiltered `get` (full running datastore) and flooded logs.
        # Supervision watchdog-reset still runs on SUPERVISION_EARLY_RESET interval.
        # TIME_SINCE_RPC=$((CURRENT_TIME - LAST_RPC_TIME))
        # if [[ $TIME_SINCE_RPC -gt $NETCONF_IDLE_TIMEOUT ]]; then
        #     echo "[WARN] Session idle for ${TIME_SINCE_RPC}s. Sending heartbeat..."
        #     send_cmd "get"
        #     if ! wait_for_response "rpc-reply" "$NETCONF_RPC_TIMEOUT"; then
        #         echo "[ERROR] Heartbeat failed."
        #         ((SESSION_ERROR_COUNT++))
        #     fi
        # fi

        sleep 1
    done

    if [[ "$SUPERVISION_EXIT_REASON" == "np2_dead" ]]; then
        log_script_info "[WARN] netopeer2-cli 프로세스 종료 감지 (세션 끊김)."
    fi

    echo "[INFO] NETCONF session ended after $SESSION_ALIVE_COUNTER successful supervision cycles (reason=${SUPERVISION_EXIT_REASON})."
    echo "[INFO] ========== M-PLANE ACTIVATION END =========="

    teardown_netopeer_for_reconnect

    if [[ "$AUTO_RECONNECT" != "1" ]]; then
        echo "[INFO] AUTO_RECONNECT off — exiting."
        exit 0
    fi

    echo "[INFO] Reconnecting in ${RECONNECT_DELAY}s (AUTO_RECONNECT=1)..."
    sleep "$RECONNECT_DELAY"
done