#!/bin/bash
# -------------------------------------------------------
# [설정] 장비 접속 정보 & Supervision 파라미터
# -------------------------------------------------------
USER="${USER:-oranuser}"
PASSWORD="${PASSWORD:-o-ran-password}"
ALLOWED_IP="${ALLOWED_IP:-10.0.20.128}"
LOCAL_IP="${LOCAL_IP:-10.0.20.254}"
CALLHOME_PORT="${CALLHOME_PORT:-${PORT:-4334}}"
NETCONF_PORT="${NETCONF_PORT:-830}"
PRODUCT="${PRODUCT:-nDLPU}"

LOG_PATH="${LOG_PATH:-/var/tmp/log/${PRODUCT}}"
CONN_DELAY="${CONN_DELAY:-3}"

NETCONF_RPC_TIMEOUT="${NETCONF_RPC_TIMEOUT:-30}"
NETCONF_IDLE_TIMEOUT="${NETCONF_IDLE_TIMEOUT:-120}"
SUPERVISION_INTERVAL="${SUPERVISION_INTERVAL:-60}"
SUPERVISION_EARLY_RESET=$((SUPERVISION_INTERVAL - 10))
SSH_KEEPALIVE_INTERVAL="${SSH_KEEPALIVE_INTERVAL:-30}"
CMD_LOCK_FILE="/var/tmp/netconf_tmp/netconf_cmd.lock"
NETCONF_CONTROL_FIFO="${NETCONF_CONTROL_FIFO:-/var/tmp/netconf_tmp/netconf_control.fifo}"
# 세션 끊김(netopeer 종료·연속 RPC 실패) 후 listen부터 다시 시도 (0이면 한 번만 실행 후 종료)
AUTO_RECONNECT="${AUTO_RECONNECT:-1}"
RECONNECT_DELAY="${RECONNECT_DELAY:-5}"

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
LAST_RPC_TIME=$(date +%s)
LAST_SUPERVISION_RESET=$(date +%s)
RPC_RESPONSE_RECEIVED=0
SESSION_ERROR_COUNT=0
MAX_SESSION_ERRORS=5

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
    
    sudo iptables -D INPUT -p tcp --dport "$CALLHOME_PORT" -j DROP >/dev/null 2>&1
    sudo iptables -D INPUT -p tcp --dport "$CALLHOME_PORT" -s "$ALLOWED_IP" -j ACCEPT >/dev/null 2>&1
    
    rm -f "$SUPERVISION_RESET" "$CMD_LOCK_FILE" "$NETCONF_CONTROL_FIFO"
    
    echo "[INFO] Cleanup complete. Exiting."
    exit 0
}
trap cleanup EXIT INT TERM

# -------------------------------------------------------
# 로그 설정
# -------------------------------------------------------
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
send_cmd() {
    local cmd="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S.%3N')
    {
        flock -x 201
        echo "[$timestamp] CLIENT_SENT: $cmd" >> "$LOG" 2>&1
        echo "$cmd" >&5
    } 201>"$CMD_LOCK_FILE"
    LAST_RPC_TIME=$(date +%s)
    RPC_RESPONSE_RECEIVED=0
}

# -------------------------------------------------------
# 외부(stdin) 명령 브릿지: GUI 입력을 netopeer2-cli로 전달
# -------------------------------------------------------
stdin_bridge() {
    echo "[INFO] External stdin bridge started." >> "$LOG"
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
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
    local timeout="${2:-$NETCONF_RPC_TIMEOUT}"
    local start_time=$(date +%s)
    
    while true; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))
        
        if [[ $elapsed -gt $timeout ]]; then
            echo "[ERROR] Response timeout after ${timeout}s for pattern: $expected_pattern" >> "$LOG"
            return 1
        fi
        
        if tail -n 100 "$LOG" | grep -qiE "$expected_pattern"; then
            RPC_RESPONSE_RECEIVED=1
            return 0
        fi
        
        sleep 0.5
    done
}

# 세션만 재시작 (iptables·tail·로그 파일 유지). 재연결 루프에서 사용.
teardown_netopeer_for_reconnect() {
    echo "[INFO] netopeer2-cli 세션 종료 — 브리지·프로세스 정리 중..." | tee -a "$LOG"
    SESSION_ESTABLISHED=0
    if [[ -n "${STDIN_BRIDGE_PID:-}" ]]; then
        kill "$STDIN_BRIDGE_PID" >/dev/null 2>&1 || true
        wait "$STDIN_BRIDGE_PID" 2>/dev/null || true
    fi
    if [[ -n "${FIFO_BRIDGE_PID:-}" ]]; then
        kill "$FIFO_BRIDGE_PID" >/dev/null 2>&1 || true
        wait "$FIFO_BRIDGE_PID" 2>/dev/null || true
    fi
    if [[ -n "${NP2_PID:-}" ]]; then
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
# -------------------------------------------------------
sudo iptables -A INPUT -p tcp --dport "$CALLHOME_PORT" -j DROP
sudo iptables -I INPUT -p tcp --dport "$CALLHOME_PORT" -s "$ALLOWED_IP" -j ACCEPT

echo "[INFO] Waiting ${CONN_DELAY}s for network stabilization..."
sleep "$CONN_DELAY"

# Filter low-level transport noise before writing to log.
noise_filter() {
    while IFS= read -r line; do
        case "$line" in
            *"nc DEBUG: SSH:"*|*"ssh_packet_"*|*"ssh_socket_"*|*"channel_rcv_data"*|*"channel_default_bufferize"*|*"channel windows are now"*|*"Read ("*"buffered"*|*"Dispatching handler for packet type"*|*"bytes left in socket buffer"*)
                continue
                ;;
        esac
        echo "$line"
    done
}

# stdin 복사는 프로세스당 한 번 (재연결 시 브리지만 다시 띄움)
exec 6<&0

SESSION_ROUND=0

while true; do
    ((SESSION_ROUND++)) || true
    echo "[INFO] ========== Call Home session round ${SESSION_ROUND} ==========" | tee -a "$LOG"

    coproc NP2 {
        stdbuf -oL -eL setsid sshpass -p "$PASSWORD" netopeer2-cli \
            -o "ServerAliveInterval=$SSH_KEEPALIVE_INTERVAL" \
            -o "ServerAliveCountMax=3" \
            -o "TCPKeepAlive=yes" \
            2>&1 | noise_filter
    } >> "$LOG" 2>&1
    NP2_PID=$!
    exec 5>&${NP2[1]}

    stdin_bridge <&6 &
    STDIN_BRIDGE_PID=$!
    rm -f "$NETCONF_CONTROL_FIFO"
    mkfifo "$NETCONF_CONTROL_FIFO"
    fifo_bridge &
    FIFO_BRIDGE_PID=$!

    # 초기화 시퀀스
    send_cmd "verb 3"
    wait_for_response "Verbosity set" 10 || echo "[WARN] verb response timeout"

    sleep 1

    send_cmd "knownhosts --mode skip"
    wait_for_response "All" 10 || echo "[WARN] knownhosts response timeout"

    sleep 2

    echo "[INFO] Starting CallHome listener on ${CALLHOME_PORT}..."
    # 이번 라운드 listen 이후 줄만 검사 (재연결 시 이전 세션 로그와 혼동 방지)
    LISTEN_LOG_START=$(wc -l < "$LOG" | tr -d ' ')
    send_cmd "listen --host $LOCAL_IP --port $CALLHOME_PORT --login $USER --timeout 300"
    sleep 3

    ########################################################################################
    ######## STEP 1 & 2. 검증 로직 (CallHome & Login) ########
    ########################################################################################
    echo "[INFO] Waiting for client connection (log from line ${LISTEN_LOG_START})..."

    LOGIN="NOK"
    WAIT_COUNTER=0
    MAX_WAIT=120

    while [[ $WAIT_COUNTER -lt $MAX_WAIT ]]; do
        if tail -n "+${LISTEN_LOG_START}" "$LOG" 2>/dev/null | grep -q "Authentication successful"; then
            LOGIN="OK"
            SESSION_ESTABLISHED=1
            echo "[INFO] Login successful."
            break
        fi

        if tail -n "+${LISTEN_LOG_START}" "$LOG" 2>/dev/null | grep -qiE "authentication failed|Authentication failed"; then
            echo "[ERROR] Authentication failed (this round)."
            LOGIN="FAIL"
            break
        fi

        sleep 1
        ((WAIT_COUNTER++))
    done

    if [[ "$LOGIN" != "OK" ]]; then
        echo "[WARN] Login not established this round (LOGIN=${LOGIN:-timeout})."
        teardown_netopeer_for_reconnect
        if [[ "$AUTO_RECONNECT" != "1" ]]; then
            echo "[ERROR] AUTO_RECONNECT off — exiting."
            exit 1
        fi
        echo "[INFO] Reconnecting in ${RECONNECT_DELAY}s..."
        sleep "$RECONNECT_DELAY"
        continue
    fi

    echo "[INFO] CallHome session established on ${CALLHOME_PORT}. Keeping session active."

    ########################################################################################
    ###### STEP 3. M-Plane 활성화 (Supervision 리셋 루프) ##############################
    ########################################################################################

    echo "[INFO] ========== M-PLANE ACTIVATION START =========="

    echo "[INFO] Step 1: Subscribing to events..."
    send_cmd "subscribe"
    wait_for_response "OK|rpc-reply|Subscribed" 15 || echo "[WARN] subscribe response timeout"
    sleep 3

    echo "[INFO] Step 2: Requesting sync status..."
    send_cmd "get --filter-xpath /o-ran-sync:sync"
    wait_for_response "rpc-reply|sync" 15 || echo "[WARN] get-sync response timeout"
    sleep 2

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

            send_cmd "user-rpc --content=$SUPERVISION_RESET"

            if wait_for_response "OK|rpc-reply" "$NETCONF_RPC_TIMEOUT"; then
                echo "[INFO] [Loop #$LOOP_COUNT] Supervision reset successful."
                ((SESSION_ALIVE_COUNTER++))
                SESSION_ERROR_COUNT=0
            else
                echo "[WARN] [Loop #$LOOP_COUNT] Supervision reset timeout. Attempting heartbeat..."

                send_cmd "get"
                if wait_for_response "rpc-reply|data" "$NETCONF_RPC_TIMEOUT"; then
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

        TIME_SINCE_RPC=$((CURRENT_TIME - LAST_RPC_TIME))
        if [[ $TIME_SINCE_RPC -gt $NETCONF_IDLE_TIMEOUT ]]; then
            echo "[WARN] Session idle for ${TIME_SINCE_RPC}s. Sending heartbeat..."
            send_cmd "get"
            if ! wait_for_response "rpc-reply" "$NETCONF_RPC_TIMEOUT"; then
                echo "[ERROR] Heartbeat failed."
                ((SESSION_ERROR_COUNT++))
            fi
        fi

        sleep 1
    done

    if [[ "$SUPERVISION_EXIT_REASON" == "np2_dead" ]]; then
        echo "[WARN] netopeer2-cli 프로세스 종료 감지 (세션 끊김)." | tee -a "$LOG"
    fi

    echo "[INFO] NETCONF session ended after $SESSION_ALIVE_COUNTER successful supervision cycles (reason=${SUPERVISION_EXIT_REASON})."
    echo "[INFO] ========== M-PLANE ACTIVATION END =========="

    SESSION_ESTABLISHED=0
    teardown_netopeer_for_reconnect

    if [[ "$AUTO_RECONNECT" != "1" ]]; then
        echo "[INFO] AUTO_RECONNECT off — exiting."
        exit 0
    fi

    echo "[INFO] Reconnecting in ${RECONNECT_DELAY}s (AUTO_RECONNECT=1)..."
    sleep "$RECONNECT_DELAY"
done