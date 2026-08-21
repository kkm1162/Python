#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Soft PTP master over TRex STL (DPDK ports).

Uses push_packets for Sync/Announce/Delay_Resp and start_capture for Delay_Req.
Runs on the TRex Linux host (local STLClient → 127.0.0.1).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Optional

from ptp_codec import (
    ETH_TYPE_PTP,
    MSG_DELAY_REQ,
    Timestamp,
    build_announce,
    build_delay_resp,
    build_follow_up,
    build_sync,
    clock_id_from_mac,
    extract_ptp_payload,
    parse_header,
)
from soft_master import (
    MasterConfig,
    MasterStats,
    interval_to_log_message_interval,
    rate_per_sec_to_interval,
)


LogFn = Callable[[str], None]


def _insert_trex_path(trex_root: str) -> str:
    ipath = trex_root.rstrip("/") + "/automation/trex_control_plane/interactive"
    if ipath not in sys.path:
        sys.path.insert(0, ipath)
    return ipath


def list_trex_ports(
    trex_path: str,
    rpc_server: str = "127.0.0.1",
) -> list[dict[str, Any]]:
    """Return TRex port summaries (requires TRex daemon running)."""
    _insert_trex_path(trex_path)
    from trex.stl.api import STLClient  # type: ignore

    c = STLClient(server=rpc_server)
    c.connect()
    try:
        # acquire ports first — TRex may not report link status until acquired
        all_ports = c.get_all_ports()
        try:
            c.acquire(ports=all_ports, force=True)
        except Exception:
            pass
        infos = c.get_port_info()
        try:
            c.release(ports=all_ports)
        except Exception:
            pass
        out: list[dict[str, Any]] = []
        for i, info in enumerate(infos or []):
            # TRex get_formatted_info() returns 'link' as "UP"/"DOWN" string
            link_raw = info.get("link", info.get("link_up", ""))
            if isinstance(link_raw, bool):
                link_up = link_raw
            elif isinstance(link_raw, str):
                link_up = link_raw.strip().upper() == "UP"
            else:
                link_up = bool(link_raw)
            # speed: TRex returns e.g. 10 (Gbps) or 10000 (Mbps) or "10 Gbps"
            speed_raw = info.get("speed", "")
            if isinstance(speed_raw, (int, float)) and speed_raw > 0:
                if speed_raw >= 1000:
                    speed_str = f"{speed_raw / 1000:.0f}G"
                else:
                    speed_str = f"{speed_raw}G"
            elif speed_raw:
                speed_str = str(speed_raw)
            else:
                speed_str = "?"
            out.append(
                {
                    "port": i,
                    "link_up": link_up,
                    "speed": speed_str,
                    "src_mac": info.get("src_mac") or info.get("hw_mac") or info.get("mac") or "",
                    "driver": info.get("driver", ""),
                    "description": info.get("description", "") or info.get("numa_node", ""),
                    "status": str(info.get("status", "")),
                    "_raw_link": str(link_raw),
                    "_raw_speed": str(speed_raw),
                    "supp_speeds": str(info.get("supp_speeds", "")),
                }
            )
        return out
    finally:
        try:
            c.disconnect()
        except Exception:
            pass


class TrexSoftPtpMaster:
    """PTP Soft Master transmitting on a TRex DPDK port."""

    def __init__(
        self,
        config: MasterConfig,
        *,
        trex_path: str,
        trex_port: int = 0,
        rpc_server: str = "127.0.0.1",
        log: Optional[LogFn] = None,
    ):
        self.cfg = config
        self.trex_path = trex_path.rstrip("/")
        self.trex_port = int(trex_port)
        self.rpc_server = rpc_server or "127.0.0.1"
        self.log = log or (lambda m: None)
        self.stats = MasterStats()

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq_sync = 0
        self._seq_ann = 0
        self._lock = threading.Lock()
        self._t1_drift = 0
        self._t4_drift = 0

        self._client = None
        self._capture_id: Optional[int] = None
        self._last_cap_index: int = -1
        self._cap_wall0: Optional[float] = None
        self._cap_ts0: Optional[float] = None
        self._last_t1_unix: float = 0.0
        self._client_lock = threading.Lock()
        self._push_errors: int = 0
        self._Ether = None
        self._Dot1Q = None
        self._Raw = None
        self._clock_id = b"\x00" * 8

    def _shake(self, offset_ns: int, jitter_ns: int, drift_acc: int) -> int:
        import random

        jitter = 0
        if jitter_ns > 0:
            jitter = random.randint(-jitter_ns, jitter_ns)
        return int(offset_ns) + int(jitter) + int(drift_acc)

    def _adj_t1(self) -> int:
        import random

        if self.cfg.t1_random_enable:
            mx = max(0, int(self.cfg.t1_random_max_ns))
            return random.randint(0, mx)
        adj = self._shake(self.cfg.t1_offset_ns, self.cfg.t1_jitter_ns, self._t1_drift)
        self._t1_drift += self.cfg.t1_drift_step_ns
        return adj

    def _adj_t4(self) -> int:
        import random

        if self.cfg.t4_random_enable:
            mx = max(0, int(self.cfg.t4_random_max_ns))
            return random.randint(0, mx)
        adj = self._shake(self.cfg.t4_offset_ns, self.cfg.t4_jitter_ns, self._t4_drift)
        self._t4_drift += self.cfg.t4_drift_step_ns
        return adj

    def update_config(self, cfg: MasterConfig) -> None:
        with self._lock:
            # Keep port/iface binding stable; MAC may update if user sets it.
            keep_src = self.cfg.src_mac
            self.cfg = cfg
            if not self.cfg.src_mac:
                self.cfg.src_mac = keep_src
            self.cfg.dst_mac = self.cfg.effective_dst_mac()
            if self.cfg.src_mac:
                try:
                    self._clock_id = clock_id_from_mac(self.cfg.src_mac)
                except Exception:
                    pass
        self.log(
            f"[CFG] trex-port={self.trex_port} domain={cfg.domain} "
            f"sync={cfg.sync_per_sec}/s ann={cfg.announce_per_sec}/s "
            f"two_step={cfg.two_step} dst={self.cfg.dst_mac}"
        )

    def _next_sync_seq(self) -> int:
        with self._lock:
            self._seq_sync = (self._seq_sync + 1) & 0xFFFF
            return self._seq_sync

    def _next_ann_seq(self) -> int:
        with self._lock:
            self._seq_ann = (self._seq_ann + 1) & 0xFFFF
            return self._seq_ann

    def _hdr(self, seq: int, *, log_interval: int, two_step_flag: bool):
        from ptp_codec import FLAG_PTP_TIMESCALE, FLAG_TWO_STEP, MSG_SYNC, PtpHeader

        flags = FLAG_PTP_TIMESCALE
        if two_step_flag:
            flags |= FLAG_TWO_STEP
        return PtpHeader(
            message_type=MSG_SYNC,
            domain=self.cfg.domain,
            flags=flags,
            source_clock_id=self._clock_id,
            source_port=1,
            sequence_id=seq,
            log_message_interval=log_interval,
        )

    def _build_scapy(self, payload: bytes):
        """Build Ether with explicit src/dst so TRex does not rewrite MACs."""
        assert self._Ether is not None and self._Raw is not None
        dst = self.cfg.effective_dst_mac()
        src = self.cfg.src_mac
        # Pad to Ethernet minimum
        frame_overhead = 18 if (self.cfg.vlan and self.cfg.vlan > 0) else 14
        need = 60 - frame_overhead - len(payload)
        if need > 0:
            payload = payload + (b"\x00" * need)
        if self.cfg.vlan and self.cfg.vlan > 0:
            assert self._Dot1Q is not None
            return (
                self._Ether(src=src, dst=dst, type=0x8100)
                / self._Dot1Q(vlan=int(self.cfg.vlan), type=ETH_TYPE_PTP)
                / self._Raw(load=payload)
            )
        return self._Ether(src=src, dst=dst, type=ETH_TYPE_PTP) / self._Raw(load=payload)

    def _push(self, payload: bytes) -> None:
        c = self._client
        if c is None:
            raise RuntimeError("TRex client not connected")
        pkt = self._build_scapy(payload)
        with self._client_lock:
            c.push_packets(ports=[self.trex_port], pkts=[pkt], force=True)
        self.stats.bytes_sent += len(bytes(pkt))

    def _fetch_capture(self, cap_id: int, pkts: list) -> None:
        c = self._client
        if c is None:
            return
        with self._client_lock:
            c.fetch_capture_packets(cap_id, pkts, pkt_count=200)

    def _send_announce(self) -> None:
        seq = self._next_ann_seq()
        log_i = interval_to_log_message_interval(self.cfg.announce_interval_s)
        hdr = self._hdr(seq, log_interval=log_i, two_step_flag=False)
        pkt = build_announce(
            hdr,
            current_utc_offset=self.cfg.utc_offset_s,
            priority1=self.cfg.priority1,
            priority2=self.cfg.priority2,
            clock_class=self.cfg.clock_class,
            clock_accuracy=self.cfg.clock_accuracy,
            grandmaster_identity=self._clock_id,
            time_source=self.cfg.time_source,
            time_traceable=self.cfg.time_traceable,
            freq_traceable=self.cfg.freq_traceable,
            utc_offset_valid=True,
        )
        self._push(pkt)
        self.stats.announce_sent += 1
        if self.stats.announce_sent <= 3 or self.stats.announce_sent % 32 == 0:
            self.log(f"[TX] Announce seq={seq} domain={self.cfg.domain} p1={self.cfg.priority1}")

    def _rx_unix_from_cap_pkt(self, pkt: dict) -> float:
        """Map TRex capture timestamp to wall clock (same host as agent)."""
        raw = pkt.get("ts")
        if raw is None:
            return time.time()
        ts = float(raw)
        now = time.time()
        # TRex usually stores gettimeofday seconds (absolute).
        if ts > 1_000_000_000:
            return ts
        if self._cap_wall0 is None:
            self._cap_wall0 = now
            self._cap_ts0 = ts
        return self._cap_wall0 + (ts - (self._cap_ts0 or ts))

    def _stamp_t1(self, adj: int) -> Timestamp:
        """Monotonic PTP-timescale T1 (never steps backward)."""
        t1 = Timestamp.now_ptp(self.cfg.utc_offset_s).add_ns(adj)
        u = t1.to_unix()
        if u <= self._last_t1_unix:
            u = self._last_t1_unix + 1e-9
            t1 = Timestamp.from_unix(u)
        self._last_t1_unix = u
        return t1

    def _send_sync_cycle(self) -> None:
        seq = self._next_sync_seq()
        log_i = interval_to_log_message_interval(self.cfg.sync_interval_s)
        hdr = self._hdr(seq, log_interval=log_i, two_step_flag=self.cfg.two_step)

        adj = self._adj_t1()
        self.stats.last_t1_ns_adj = adj

        if self.cfg.two_step:
            self._push(build_sync(hdr, Timestamp(0, 0), two_step=True))
            self.stats.sync_sent += 1
            t1 = self._stamp_t1(adj)
            self._push(build_follow_up(hdr, t1))
            self.stats.follow_up_sent += 1
            if self.stats.sync_sent <= 5 or self.stats.sync_sent % 64 == 0:
                self.log(
                    f"[TX] Sync+FU seq={seq} T1={t1.seconds}.{t1.nanoseconds:09d} adj={adj:+d}ns"
                )
        else:
            t1 = self._stamp_t1(adj)
            self._push(build_sync(hdr, t1, two_step=False))
            self.stats.sync_sent += 1
            if self.stats.sync_sent <= 5 or self.stats.sync_sent % 64 == 0:
                self.log(
                    f"[TX] Sync(1-step) seq={seq} T1={t1.seconds}.{t1.nanoseconds:09d} adj={adj:+d}ns"
                )

    def _handle_delay_req(self, payload: bytes, rx_unix: float) -> None:
        hdr = parse_header(payload)
        if hdr is None or hdr.message_type != MSG_DELAY_REQ:
            return
        if hdr.domain != self.cfg.domain:
            return
        if len(payload) < 44:
            return

        self.stats.delay_req_rx += 1
        # T4: same PTP timescale as T1 (UTC wall + utc_offset).
        t_rx = Timestamp.from_unix(rx_unix + self.cfg.utc_offset_s)
        adj = self._adj_t4()
        t4 = t_rx.add_ns(adj)
        self.stats.last_t4_ns_adj = adj

        log_i = interval_to_log_message_interval(rate_per_sec_to_interval(self.cfg.delay_req_per_sec))
        resp_hdr = self._hdr(hdr.sequence_id, log_interval=log_i, two_step_flag=False)
        resp = build_delay_resp(resp_hdr, t4, hdr.source_clock_id, hdr.source_port)
        self._push(resp)
        self.stats.delay_resp_tx += 1
        if self.stats.delay_resp_tx <= 5 or self.stats.delay_resp_tx % 64 == 0:
            self.log(
                f"[RX] Delay_Req seq={hdr.sequence_id} -> Delay_Resp "
                f"T4={t4.seconds}.{t4.nanoseconds:09d} adj={adj:+d}ns"
            )

    def _tx_scheduler(self) -> None:
        """Single-thread TX for Sync/Announce — avoids TRex RPC races."""
        next_sync = time.monotonic()
        next_ann = time.monotonic()
        next_stat = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            due_sync = now >= next_sync
            due_ann = now >= next_ann
            if not due_sync and not due_ann:
                wait = min(next_sync, next_ann) - now
                if self._stop.wait(max(0.0005, wait)):
                    break
                continue
            if due_ann:
                next_ann += max(0.001, float(self.cfg.announce_interval_s))
                try:
                    self._send_announce()
                except Exception as exc:
                    self._push_errors += 1
                    self.log(f"[ERR] announce TX: {exc}")
            if due_sync:
                next_sync += max(0.001, float(self.cfg.sync_interval_s))
                try:
                    self._send_sync_cycle()
                except Exception as exc:
                    self._push_errors += 1
                    self.log(f"[ERR] sync TX: {exc}")
            if now >= next_stat:
                next_stat += 5.0
                self.log(
                    f"[STAT] TX sync={self.stats.sync_sent} fu={self.stats.follow_up_sent} "
                    f"ann={self.stats.announce_sent} dreq={self.stats.delay_req_rx} "
                    f"dresp={self.stats.delay_resp_tx} push_err={self._push_errors}"
                )

    def _rate_loop(self, interval_fn: Callable[[], float], send_fn: Callable[[], None], name: str) -> None:
        """Legacy — unused; kept for compatibility."""
        self._tx_scheduler()

    def _rx_loop(self) -> None:
        """Poll TRex capture for Delay_Req (use capture ts, not poll time, for T4)."""
        while not self._stop.is_set():
            c = self._client
            cap_id = self._capture_id
            if c is None or cap_id is None:
                if self._stop.wait(0.05):
                    break
                continue
            try:
                pkts: list = []
                self._fetch_capture(cap_id, pkts)
                for item in pkts:
                    cap_idx = item.get("index")
                    if cap_idx is not None:
                        if cap_idx <= self._last_cap_index:
                            continue
                        self._last_cap_index = int(cap_idx)
                    binary = item.get("binary") if isinstance(item, dict) else None
                    if not binary:
                        continue
                    payload = extract_ptp_payload(bytes(binary))
                    if not payload:
                        continue
                    rx_unix = self._rx_unix_from_cap_pkt(item)
                    self._handle_delay_req(payload, rx_unix)
            except Exception as exc:
                if not self._stop.is_set():
                    self.log(f"[WARN] capture poll: {exc}")
                    if self._stop.wait(0.2):
                        break
                    continue
            if self._stop.wait(0.005):
                break

    def start(self) -> None:
        if self._threads:
            return

        _insert_trex_path(self.trex_path)
        from trex.stl.api import STLClient  # type: ignore
        from scapy.all import Dot1Q, Ether, Raw  # type: ignore

        self._Ether = Ether
        self._Dot1Q = Dot1Q
        self._Raw = Raw

        self._stop.clear()
        self._t1_drift = 0
        self._t4_drift = 0
        self.cfg.dst_mac = self.cfg.effective_dst_mac()

        self.log(
            f"[START] TRex Soft PTP  rpc={self.rpc_server} path={self.trex_path} "
            f"port={self.trex_port}"
        )

        c = STLClient(server=self.rpc_server)
        c.connect()
        self._client = c
        try:
            c.acquire(ports=[self.trex_port], force=True)
            c.set_service_mode(ports=[self.trex_port], enabled=True)

            # Resolve src MAC from TRex port if empty
            if not self.cfg.src_mac:
                try:
                    infos = c.get_port_info(ports=[self.trex_port])
                    info = infos[0] if infos else {}
                    mac = (
                        info.get("src_mac")
                        or info.get("hw_mac")
                        or info.get("mac")
                        or ""
                    )
                    if mac:
                        self.cfg.src_mac = str(mac).lower()
                except Exception as exc:
                    self.log(f"[WARN] get_port_info MAC: {exc}")
            if not self.cfg.src_mac:
                # fallback: port attr
                try:
                    attr = c.get_port_attr(port=self.trex_port)
                    mac = attr.get("src_mac") or attr.get("hw_mac") or ""
                    if mac:
                        self.cfg.src_mac = str(mac).lower()
                except Exception:
                    pass
            if not self.cfg.src_mac:
                self.cfg.src_mac = "02:00:00:00:00:01"
                self.log("[WARN] using fallback src MAC 02:00:00:00:00:01")

            self._clock_id = clock_id_from_mac(self.cfg.src_mac)

            # Link status
            try:
                infos = c.get_port_info(ports=[self.trex_port])
                info = infos[0] if infos else {}
                link_raw = info.get("link", info.get("link_up", ""))
                link_up = (link_raw is True) or (isinstance(link_raw, str) and link_raw.strip().upper() == "UP")
                self.log(
                    f"[PORT] trex:{self.trex_port} link={link_raw} "
                    f"speed={info.get('speed')} src_mac={self.cfg.src_mac}"
                )
                if not link_up:
                    self.log("[WARN] TRex port link is DOWN — RU will not see PTP")
            except Exception:
                self.log(f"[PORT] trex:{self.trex_port} src_mac={self.cfg.src_mac}")

            cap = c.start_capture(
                rx_ports=[self.trex_port],
                limit=5000,
                mode="cyclic",
                bpf_filter="ether proto 0x88f7 or (vlan and ether proto 0x88f7)",
            )
            self._capture_id = cap["id"]
            self._last_cap_index = -1
            self._cap_wall0 = time.time()
            self._cap_ts0 = None
            self.log(f"[RX] capture id={self._capture_id} bpf=PTP (T4 uses capture ts)")

            self.log(
                "[NOTE] Software PTP via TRex — use 2-Step + Sync 8/s for stable freq. "
                "Disable chrony/NTP on TRex host during test."
            )

            self.log(
                f"[CFG] domain={self.cfg.domain} dst={self.cfg.dst_mac} "
                f"sync={self.cfg.sync_per_sec}/s ann={self.cfg.announce_per_sec}/s "
                f"dreq_adv={self.cfg.delay_req_per_sec}/s two_step={self.cfg.two_step} "
                f"p1={self.cfg.priority1} p2={self.cfg.priority2} class={self.cfg.clock_class} "
                f"utc_off={self.cfg.utc_offset_s} vlan={self.cfg.vlan or 0}"
            )

            self._send_announce()
            self._send_sync_cycle()
            self.log(f"[TX] first frames ok bytes_sent={self.stats.bytes_sent}")
        except Exception:
            self._cleanup_client()
            raise

        t_rx = threading.Thread(target=self._rx_loop, name="trex-ptp-rx", daemon=True)
        t_tx = threading.Thread(target=self._tx_scheduler, name="trex-ptp-tx", daemon=True)
        self._threads = [t_rx, t_tx]
        for t in self._threads:
            t.start()

    def _cleanup_client(self) -> None:
        c = self._client
        self._client = None
        cap_id = self._capture_id
        self._capture_id = None
        if c is None:
            return
        try:
            if cap_id is not None:
                try:
                    c.stop_capture(capture_id=cap_id)
                except Exception:
                    pass
            try:
                c.stop(ports=[self.trex_port])
            except Exception:
                pass
            try:
                c.set_service_mode(ports=[self.trex_port], enabled=False)
            except Exception:
                pass
            try:
                c.release(ports=[self.trex_port])
            except Exception:
                pass
            try:
                c.disconnect()
            except Exception:
                pass
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        self._threads = []
        self._cleanup_client()
        self.log("[STOP] TRex Soft PTP master stopped")
