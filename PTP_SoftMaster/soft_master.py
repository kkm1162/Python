#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stateful soft PTP master: Announce/Sync/Follow_Up + Delay_Resp with T1/T4 shake."""

from __future__ import annotations

import math
import random
import socket
import struct
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Optional

from scapy.all import (  # type: ignore
    Ether,
    get_if_hwaddr,
    get_if_list,
    sendp,
    sniff,
)

from ptp_codec import (
    ETH_TYPE_PTP,
    FLAG_TWO_STEP,
    MSG_DELAY_REQ,
    MSG_SYNC,
    PtpHeader,
    Timestamp,
    build_announce,
    build_delay_resp,
    build_follow_up,
    build_sync,
    clock_id_from_mac,
    extract_ptp_payload,
    parse_header,
)


LogFn = Callable[[str], None]

PTP_MCAST_DEFAULT = "01:1b:19:00:00:00"
PTP_MCAST_LINK_LOCAL = "01:80:c2:00:00:0e"  # often labeled non-forwardable in tools
ETH_MIN_FRAME = 60  # without FCS


def rate_per_sec_to_interval(rate: float) -> float:
    return 1.0 / max(float(rate), 0.001)


def interval_to_log_message_interval(interval_s: float) -> int:
    if interval_s <= 0:
        return 0
    return int(round(math.log2(interval_s)))


def mac_to_bytes(mac: str) -> bytes:
    parts = [int(x, 16) for x in mac.replace("-", ":").split(":")]
    if len(parts) != 6:
        raise ValueError(f"invalid MAC: {mac}")
    return bytes(parts)


def build_l2_frame(
    dst_mac: str,
    src_mac: str,
    payload: bytes,
    *,
    ethertype: int = ETH_TYPE_PTP,
    vlan: int = 0,
) -> bytes:
    """Build Ethernet frame bytes and pad to 60B (Sync is only 58B without pad)."""
    dst = mac_to_bytes(dst_mac)
    src = mac_to_bytes(src_mac)
    if vlan and vlan > 0:
        tci = int(vlan) & 0x0FFF
        frame = dst + src + struct.pack("!HHH", 0x8100, tci, ethertype & 0xFFFF) + payload
    else:
        frame = dst + src + struct.pack("!H", ethertype & 0xFFFF) + payload
    if len(frame) < ETH_MIN_FRAME:
        frame += b"\x00" * (ETH_MIN_FRAME - len(frame))
    return frame


def read_iface_state(iface: str) -> dict[str, str]:
    """Best-effort Linux sysfs link info (empty values on Windows)."""
    info = {"iface": iface, "operstate": "?", "carrier": "?", "mac": "?", "mtu": "?"}
    base = Path("/sys/class/net") / iface
    if not base.is_dir():
        return info
    for key in ("operstate", "carrier", "mtu"):
        try:
            info[key] = (base / key).read_text(encoding="utf-8").strip()
        except Exception:
            pass
    try:
        info["mac"] = (base / "address").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return info


@dataclass
class MasterConfig:
    iface: str = ""
    src_mac: str = ""
    dst_mac: str = PTP_MCAST_DEFAULT
    use_link_local_mcast: bool = False  # 01:80:C2:00:00:0E
    vlan: int = 0
    domain: int = 24
    encapsulation: str = "None"  # L2 only for now
    two_step: bool = False  # screenshot default: 1 Step
    # message rates (messages per second)
    announce_per_sec: float = 8.0
    sync_per_sec: float = 32.0
    delay_req_per_sec: float = 16.0  # advertised min Delay_Req rate (logInterval on Delay_Resp)
    # clock attributes (Announce)
    priority1: int = 128
    priority2: int = 255
    clock_class: int = 6
    clock_accuracy: int = 0x21  # Within 100 ns
    time_source: int = 0xA0  # Internal Oscillator
    utc_offset_s: int = 37
    freq_traceable: bool = True
    time_traceable: bool = True
    # T1 / T4 shake
    t1_offset_ns: int = 0
    t1_jitter_ns: int = 0
    t4_offset_ns: int = 0
    t4_jitter_ns: int = 0
    t1_drift_step_ns: int = 0
    t4_drift_step_ns: int = 0
    t1_random_enable: bool = False
    t1_random_max_ns: int = 0
    t4_random_enable: bool = False
    t4_random_max_ns: int = 0

    @property
    def sync_interval_s(self) -> float:
        return rate_per_sec_to_interval(self.sync_per_sec)

    @property
    def announce_interval_s(self) -> float:
        return rate_per_sec_to_interval(self.announce_per_sec)

    def effective_dst_mac(self) -> str:
        if self.use_link_local_mcast:
            return PTP_MCAST_LINK_LOCAL
        return (self.dst_mac or PTP_MCAST_DEFAULT).strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MasterConfig":
        allowed = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**cleaned)


@dataclass
class MasterStats:
    announce_sent: int = 0
    sync_sent: int = 0
    follow_up_sent: int = 0
    delay_req_rx: int = 0
    delay_resp_tx: int = 0
    last_t1_ns_adj: int = 0
    last_t4_ns_adj: int = 0
    bytes_sent: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MasterStats":
        allowed = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**cleaned)


class SoftPtpMaster:
    def __init__(self, config: MasterConfig, log: Optional[LogFn] = None):
        self.cfg = config
        self.log = log or (lambda m: None)
        self.stats = MasterStats()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq_sync = 0
        self._seq_ann = 0
        self._lock = threading.Lock()
        self._t1_drift = 0
        self._t4_drift = 0
        self._last_t1_unix: float = 0.0
        self._l2sock: Optional[socket.socket] = None
        self._tx_mode = "scapy"

        if not self.cfg.src_mac:
            try:
                self.cfg.src_mac = get_if_hwaddr(self.cfg.iface)
            except Exception:
                self.cfg.src_mac = "02:00:00:00:00:01"
        self.cfg.dst_mac = self.cfg.effective_dst_mac()
        self._clock_id = clock_id_from_mac(self.cfg.src_mac)

    def _shake(self, offset_ns: int, jitter_ns: int, drift_acc: int) -> int:
        jitter = 0
        if jitter_ns > 0:
            jitter = random.randint(-jitter_ns, jitter_ns)
        return int(offset_ns) + int(jitter) + int(drift_acc)

    def _adj_t1(self) -> int:
        if self.cfg.t1_random_enable:
            mx = max(0, int(self.cfg.t1_random_max_ns))
            return random.randint(0, mx)
        adj = self._shake(self.cfg.t1_offset_ns, self.cfg.t1_jitter_ns, self._t1_drift)
        self._t1_drift += self.cfg.t1_drift_step_ns
        return adj

    def _adj_t4(self) -> int:
        if self.cfg.t4_random_enable:
            mx = max(0, int(self.cfg.t4_random_max_ns))
            return random.randint(0, mx)
        adj = self._shake(self.cfg.t4_offset_ns, self.cfg.t4_jitter_ns, self._t4_drift)
        self._t4_drift += self.cfg.t4_drift_step_ns
        return adj

    def update_config(self, cfg: MasterConfig) -> None:
        """Hot-update protocol/shake settings while running (iface unchanged)."""
        with self._lock:
            keep_iface = self.cfg.iface
            keep_src = self.cfg.src_mac
            self.cfg = cfg
            self.cfg.iface = keep_iface
            if not self.cfg.src_mac:
                self.cfg.src_mac = keep_src
            self.cfg.dst_mac = self.cfg.effective_dst_mac()
        self.log(
            f"[CFG] updated domain={cfg.domain} sync={cfg.sync_per_sec}/s "
            f"ann={cfg.announce_per_sec}/s two_step={cfg.two_step} "
            f"dst={self.cfg.dst_mac} T1_rand={cfg.t1_random_enable}/{cfg.t1_random_max_ns}"
        )

    def update_shake_config(self, cfg: MasterConfig) -> None:
        self.update_config(cfg)

    def _next_sync_seq(self) -> int:
        with self._lock:
            self._seq_sync = (self._seq_sync + 1) & 0xFFFF
            return self._seq_sync

    def _next_ann_seq(self) -> int:
        with self._lock:
            self._seq_ann = (self._seq_ann + 1) & 0xFFFF
            return self._seq_ann

    def _hdr(self, seq: int, *, log_interval: int, two_step_flag: bool) -> PtpHeader:
        from ptp_codec import FLAG_PTP_TIMESCALE
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

    def _open_l2(self) -> None:
        self._close_l2()
        iface = self.cfg.iface
        # Prefer Linux AF_PACKET bound to the exact NIC (avoids scapy route quirks).
        if hasattr(socket, "AF_PACKET"):
            try:
                sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
                sock.bind((iface, 0))
                self._l2sock = sock
                self._tx_mode = "af_packet"
                self.log(f"[TX] AF_PACKET bound to {iface}")
                return
            except Exception as exc:
                self.log(f"[WARN] AF_PACKET open failed ({exc}); fallback scapy")
        self._l2sock = None
        self._tx_mode = "scapy"
        self.log(f"[TX] using scapy sendp iface={iface}")

    def _close_l2(self) -> None:
        sock = self._l2sock
        self._l2sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _send_ptp(self, payload: bytes) -> None:
        frame = build_l2_frame(
            self.cfg.effective_dst_mac(),
            self.cfg.src_mac,
            payload,
            ethertype=ETH_TYPE_PTP,
            vlan=self.cfg.vlan,
        )
        sock = self._l2sock
        if sock is not None:
            n = sock.send(frame)
            self.stats.bytes_sent += int(n)
            return
        sendp(Ether(frame), iface=self.cfg.iface, verbose=False)
        self.stats.bytes_sent += len(frame)

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
        self._send_ptp(pkt)
        self.stats.announce_sent += 1
        if self.stats.announce_sent <= 3 or self.stats.announce_sent % 32 == 0:
            self.log(f"[TX] Announce seq={seq} domain={self.cfg.domain} p1={self.cfg.priority1}")

    def _stamp_t1(self, adj: int) -> Timestamp:
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
            self._send_ptp(build_sync(hdr, Timestamp(0, 0), two_step=True))
            self.stats.sync_sent += 1
            t1 = self._stamp_t1(adj)
            self._send_ptp(build_follow_up(hdr, t1))
            self.stats.follow_up_sent += 1
            if self.stats.sync_sent <= 5 or self.stats.sync_sent % 64 == 0:
                self.log(
                    f"[TX] Sync+FU seq={seq} T1={t1.seconds}.{t1.nanoseconds:09d} adj={adj:+d}ns"
                )
        else:
            t1 = self._stamp_t1(adj)
            self._send_ptp(build_sync(hdr, t1, two_step=False))
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
        t_rx = Timestamp.from_unix(rx_unix + self.cfg.utc_offset_s)
        adj = self._adj_t4()
        t4 = t_rx.add_ns(adj)
        self.stats.last_t4_ns_adj = adj

        log_i = interval_to_log_message_interval(rate_per_sec_to_interval(self.cfg.delay_req_per_sec))
        resp_hdr = self._hdr(hdr.sequence_id, log_interval=log_i, two_step_flag=False)
        resp = build_delay_resp(resp_hdr, t4, hdr.source_clock_id, hdr.source_port)
        self._send_ptp(resp)
        self.stats.delay_resp_tx += 1
        if self.stats.delay_resp_tx <= 5 or self.stats.delay_resp_tx % 64 == 0:
            self.log(
                f"[RX] Delay_Req seq={hdr.sequence_id} -> Delay_Resp "
                f"T4={t4.seconds}.{t4.nanoseconds:09d} adj={adj:+d}ns"
            )

    def _sniff_loop(self) -> None:
        iface = self.cfg.iface

        def _cb(pkt) -> None:
            if self._stop.is_set():
                return
            try:
                payload = extract_ptp_payload(bytes(pkt))
                if not payload:
                    return
                h = parse_header(payload)
                if h and h.message_type == MSG_DELAY_REQ:
                    self._handle_delay_req(payload, time.time())
            except Exception as exc:
                self.log(f"[ERR] sniff handler: {exc}")

        bpf = "ether proto 0x88f7 or (vlan and ether proto 0x88f7)"
        try:
            sniff(
                iface=iface,
                prn=_cb,
                store=False,
                filter=bpf,
                stop_filter=lambda _: self._stop.is_set(),
            )
        except Exception as exc:
            self.log(f"[WARN] BPF filter failed ({exc}); sniffing without filter")
            sniff(
                iface=iface,
                prn=_cb,
                store=False,
                stop_filter=lambda _: self._stop.is_set(),
            )

    def _rate_loop(self, interval_fn: Callable[[], float], send_fn: Callable[[], None], name: str) -> None:
        next_mono = time.monotonic()
        while not self._stop.is_set():
            interval = max(0.001, float(interval_fn()))
            next_mono += interval
            delay = next_mono - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            try:
                send_fn()
            except Exception as exc:
                self.log(f"[ERR] {name} TX: {exc}")

    def start(self) -> None:
        if self._threads:
            return
        if not self.cfg.iface:
            raise ValueError("iface is required")
        if (self.cfg.encapsulation or "None").strip().lower() not in ("none", "l2", ""):
            self.log("[WARN] Encapsulation other than None/L2 ignored (L2 only)")

        self._stop.clear()
        self._t1_drift = 0
        self._t4_drift = 0
        self.cfg.dst_mac = self.cfg.effective_dst_mac()

        st = read_iface_state(self.cfg.iface)
        self.log(
            f"[IFACE] {st['iface']} operstate={st['operstate']} carrier={st['carrier']} "
            f"mac={st['mac']} mtu={st['mtu']}"
        )
        if st.get("operstate") not in ("?", "up") or st.get("carrier") == "0":
            self.log(
                "[WARN] interface may be DOWN / no carrier — PTP frames will not reach the RU. "
                "Check cable and: ip link set <iface> up"
            )

        self._open_l2()
        self.log(
            f"[START] mode={self._tx_mode} L2 mcast={self.cfg.dst_mac} "
            f"src={self.cfg.src_mac} vlan={self.cfg.vlan or 'none'} "
            f"domain={self.cfg.domain} sync={self.cfg.sync_per_sec}/s "
            f"announce={self.cfg.announce_per_sec}/s two_step={self.cfg.two_step}"
        )
        self.log(
            f"[CLK] p1={self.cfg.priority1} p2={self.cfg.priority2} class={self.cfg.clock_class} "
            f"acc=0x{self.cfg.clock_accuracy:02X} utc_off={self.cfg.utc_offset_s} "
            f"trace T/F={self.cfg.time_traceable}/{self.cfg.freq_traceable}"
        )

        try:
            self._send_announce()
            self._send_sync_cycle()
            self.log(f"[TX] first frames ok bytes_sent={self.stats.bytes_sent}")
        except Exception as exc:
            self.log(f"[ERR] first TX: {exc}")
            self._close_l2()
            raise

        t_sniff = threading.Thread(target=self._sniff_loop, name="ptp-sniff", daemon=True)
        t_sync = threading.Thread(
            target=self._rate_loop,
            args=(lambda: self.cfg.sync_interval_s, self._send_sync_cycle, "sync"),
            name="ptp-sync",
            daemon=True,
        )
        t_ann = threading.Thread(
            target=self._rate_loop,
            args=(lambda: self.cfg.announce_interval_s, self._send_announce, "announce"),
            name="ptp-ann",
            daemon=True,
        )
        self._threads = [t_sniff, t_sync, t_ann]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self._threads = []
        self._close_l2()
        self.log("[STOP] Soft PTP master stopped")


def list_interfaces() -> list[str]:
    try:
        return list(get_if_list())
    except Exception:
        return []
