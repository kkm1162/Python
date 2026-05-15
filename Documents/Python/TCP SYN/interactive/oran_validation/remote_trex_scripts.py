# -*- coding: utf-8 -*-
"""Remote Python payloads executed on the TRex server (stats, STL TX, stop)."""


def _trex_interactive_path(trex_root: str) -> str:
    return trex_root + "/automation/trex_control_plane/interactive"


def link_check_script(trex_path: str, port: int) -> str:
    """Return one-line JSON-like info for a single TRex port."""
    ipath = _trex_interactive_path(trex_path)
    return f"""
import json
import sys
sys.path.insert(0, {repr(ipath)})
from trex.stl.api import STLClient

c = None
try:
    c = STLClient(server='127.0.0.1')
    c.connect()
    c.acquire(ports=[{int(port)}], force=True)
    info = c.get_port_info(ports=[{int(port)}])
    port_info = info[0] if info else {{}}
    print(json.dumps({{
        "ok": True,
        "port": {int(port)},
        "link_up": bool(port_info.get("link_up", False)),
        "speed": port_info.get("speed", ""),
        "status": str(port_info.get("status", "")),
    }}))
except Exception as e:
    print(json.dumps({{
        "ok": False,
        "port": {int(port)},
        "error": str(e),
    }}))
finally:
    try:
        if c is not None:
            c.release(ports=[{int(port)}])
            c.disconnect()
    except Exception:
        pass
"""


def stats_monitor_script(trex_path: str, server_ip: str, ports) -> str:
    """Long-running stats poller; reconnects STLClient on RPC errors."""
    ipath = _trex_interactive_path(trex_path)
    return f"""
import sys, time
sys.path.insert(0, {repr(ipath)})
try:
    from trex.stl.api import STLClient
    connected = False
    c = None
    rpc_target = "127.0.0.1"
    
    while True:
        try:
            if not connected:
                if c is not None:
                    try: c.disconnect()
                    except: pass

                # RPC는 서버 로컬 루프백이 가장 안정적이므로 우선 시도하고, 필요 시 GUI 입력 IP로 폴백한다.
                candidates = []
                for host in ["127.0.0.1", {repr(server_ip)}]:
                    host = str(host or "").strip()
                    if host and host not in candidates:
                        candidates.append(host)

                last_err = ""
                c = None
                for host in candidates:
                    c_try = None
                    try:
                        c_try = STLClient(server=host)
                        c_try.connect()
                        # 연결 직후 실제 stats 호출 가능 여부까지 확인해 false-positive 연결을 배제한다.
                        c_try.get_stats(ports={ports!r})
                        c = c_try
                        rpc_target = host
                        last_err = ""
                        break
                    except Exception as conn_e:
                        last_err = str(conn_e)
                        try:
                            if c_try is not None:
                                c_try.disconnect()
                        except Exception:
                            pass

                if c is None:
                    raise RuntimeError(last_err or "RPC connect failed")

                connected = True
                print("__STATE__|ready")
                print(f"__INFO__|TRex RPC connected ({{rpc_target}})")
                sys.stdout.flush()

            stats = c.get_stats(ports={ports!r})
            global_stats = stats.get('global', {{}})

            for p in {ports!r}:
                port_stats = stats.get(p, {{}})
                tx_bps = port_stats.get('tx_bps', 0)
                rx_bps = port_stats.get('rx_bps', 0)
                tx_pps = port_stats.get('tx_pps', 0)
                tx_pkts = port_stats.get('tx_pkts', 0)
                cpu_util = global_stats.get('cpu_util', 0)
                q_full = global_stats.get('queue_full', 0)

                print(f"__STAT__|{{p}}|{{tx_bps}}|{{rx_bps}}|{{tx_pps}}|{{tx_pkts}}|{{cpu_util}}|{{q_full}}")

            sys.stdout.flush()
            time.sleep(1.0)
            
        except Exception as api_e:
            err_str = str(api_e)
            print("__STATE__|재연결중")
            print(f"__INFO__|RPC 통신 지연. 새로운 세션으로 재연결 시도 중... ({{err_str[:40]}})")
            connected = False # 다음 루프에서 객체를 새로 만들도록 플래그 설정
            sys.stdout.flush()
            time.sleep(0.5)
        
except Exception as e:
    print("__STATE__|오류")
    print(f"__INFO__|FATAL: {{str(e)[:120]}}")
"""


def play_traffic_stl_script(
    trex_path: str,
    pcap_full: str,
    ports,
    rate_command: str,
    duration_sec,
) -> str:
    ipath = _trex_interactive_path(trex_path)
    return f"""
import sys
import time
sys.path.insert(0, {repr(ipath)})
from trex.stl.api import STLClient, STLStream, STLPktBuilder, STLTXCont
from scapy.all import rdpcap

c = None
ports = {ports!r}
try:
    c = STLClient(server='127.0.0.1')
    c.connect()

    c.acquire(ports=ports, force=True)
    c.reset(ports=ports)
    c.remove_all_streams(ports=ports)
    c.clear_stats()

    packets = rdpcap({repr(pcap_full)})
    
    # [핵심] TRex 엔진 프리징을 막기 위해 전체 PCAP에서 50개만 샘플링
    # 주의: 고정 step 샘플링은 패턴(예: 5개 반복)과 공진해 동일 프레임만 반복 선택될 수 있다.
    total_pkts = len(packets)
    if total_pkts < 1:
        raise RuntimeError("PCAP packet count is 0")
    streams = []

    max_streams = 50
    if total_pkts <= max_streams:
        picked_indexes = list(range(total_pkts))
    else:
        # step 기반이되, index에 오프셋을 더해 반복 패턴 공진을 피한다.
        step = max(1, total_pkts // max_streams)
        picked = []
        seen = set()
        for k in range(max_streams):
            idx = (k * step + k) % total_pkts
            if idx in seen:
                # 충돌 시 다음 인덱스로 이동
                j = idx
                while j in seen:
                    j = (j + 1) % total_pkts
                idx = j
            seen.add(idx)
            picked.append(idx)
        picked_indexes = picked

    # 샘플된 패킷 길이를 로그로 남겨 "PCAP은 정상인데 출력이 한 사이즈" 문제를 즉시 진단한다.
    lens = []
    for idx in picked_indexes:
        try:
            lens.append(len(bytes(packets[idx])))
        except Exception:
            pass
    uniq = sorted(set(lens))
    if len(uniq) > 12:
        uniq_text = ",".join(str(x) for x in uniq[:12]) + ",..."
    else:
        uniq_text = ",".join(str(x) for x in uniq)
    print(f"[INFO] pcap_total={{total_pkts}} sampled={{len(picked_indexes)}} unique_pkt_lens={{len(uniq)}} lens=[{{uniq_text}}]")
    
    for idx in picked_indexes:
        streams.append(STLStream(packet=STLPktBuilder(pkt=packets[idx]), mode=STLTXCont()))

    for p in ports:
        c.add_streams(streams, ports=[p])
    c.start(ports=ports, mult={repr(rate_command)}, duration={duration_sec})
    time.sleep(1.5)
    stats = c.get_stats(ports=ports)
    tx_sum = 0.0
    for p in ports:
        p_stats = stats.get(p, {{}})
        p_tx_bps = float(p_stats.get("tx_bps", 0.0))
        p_tx_pps = float(p_stats.get("tx_pps", 0.0))
        tx_sum += p_tx_bps
        print(f"[INFO] port={{p}} first-check tx_bps={{p_tx_bps}} tx_pps={{p_tx_pps}}")

    # 간헐적 시작 실패 보정: TX=0이면 1회 재시도
    if tx_sum <= 0.0:
        print("[WARN] first start tx_bps=0, retrying once")
        c.stop(ports=ports)
        c.remove_all_streams(ports=ports)
        for p in ports:
            c.add_streams(streams, ports=[p])
        time.sleep(0.5)
        c.start(ports=ports, mult={repr(rate_command)}, duration={duration_sec})
        time.sleep(1.5)
        stats = c.get_stats(ports=ports)
        tx_sum = 0.0
        for p in ports:
            p_stats = stats.get(p, {{}})
            p_tx_bps = float(p_stats.get("tx_bps", 0.0))
            p_tx_pps = float(p_stats.get("tx_pps", 0.0))
            tx_sum += p_tx_bps
            print(f"[INFO] port={{p}} retry-check tx_bps={{p_tx_bps}} tx_pps={{p_tx_pps}}")

    # 특정 NIC 조합에서 단일 포트 재시작이 먹히지 않는 경우 추가 1회 강제 재기동
    if tx_sum <= 0.0:
        print("[WARN] second retry with reset/remove/add")
        c.stop(ports=ports)
        c.reset(ports=ports)
        c.remove_all_streams(ports=ports)
        c.clear_stats()
        for p in ports:
            c.add_streams(streams, ports=[p])
        c.start(ports=ports, mult={repr(rate_command)}, duration={duration_sec})
        time.sleep(1.5)
        stats = c.get_stats(ports=ports)
        tx_sum = 0.0
        for p in ports:
            p_stats = stats.get(p, {{}})
            p_tx_bps = float(p_stats.get("tx_bps", 0.0))
            p_tx_pps = float(p_stats.get("tx_pps", 0.0))
            tx_sum += p_tx_bps
            print(f"[INFO] port={{p}} force-retry tx_bps={{p_tx_bps}} tx_pps={{p_tx_pps}}")

    if tx_sum <= 0.0:
        port_info = c.get_port_info(ports=ports)
        print(f"[ERROR] started but tx_bps is 0. port_info={{port_info}}")
    else:
        print(f"[INFO] start check ok tx_bps_sum={{tx_sum}}")

    # 동시 포트 제어를 위해 start 명령 후 즉시 세션을 반환한다.
    # (duration은 TRex 엔진이 자체 처리)

except Exception as e:
    print(f"[ERROR] {{e}}")
finally:
    try:
        if c is not None:
            c.release(ports=ports)
            c.disconnect()
    except Exception:
        pass
"""


def stop_traffic_script(trex_path: str, ports) -> str:
    ipath = _trex_interactive_path(trex_path)
    return f"""
import sys
sys.path.insert(0, {repr(ipath)})
from trex.stl.api import STLClient

try:
    c = STLClient(server='127.0.0.1')
    c.connect()

    ports = {ports!r}
    c.acquire(ports=ports, force=True)
    c.stop(ports=ports)
    c.clear_stats()
    c.release(ports=ports)
    c.disconnect()
except Exception as e:
    print(f"[ERROR] {{e}}")
"""
