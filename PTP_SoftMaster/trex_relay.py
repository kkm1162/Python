#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PTP Relay via TRex: receive GM packets on one port, modify T1/T4, transmit on another.

Port 0 (rx_port) ← GM Sync/FU/Announce/Delay_Resp
Port 1 (tx_port) → RU  (modified GM frames)
Port 1 (rx)      ← RU  Delay_Req  → forward to GM on rx_port
GM Delay_Resp    → relay to RU (optional T4 shake on GM HW timestamp)

Delay_Req/Resp must pass through the real GM for LOCK; locally generated
Delay_Resp uses SW timestamps and breaks path-delay measurement.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable, Optional

from ptp_codec import (
    MSG_ANNOUNCE,
    MSG_DELAY_REQ,
    MSG_DELAY_RESP,
    MSG_FOLLOW_UP,
    MSG_SYNC,
    clock_id_from_mac,
    extract_ptp_payload,
    pack_timestamp,
    parse_header,
    unpack_timestamp,
)

LogFn = Callable[[str], None]


def _insert_trex_path(trex_root: str) -> str:
    ipath = trex_root.rstrip("/") + "/automation/trex_control_plane/interactive"
    if ipath not in sys.path:
        sys.path.insert(0, ipath)
    return ipath


@dataclass
class RelayConfig:
    domain: int = 24
    vlan: int = 0
    # T1 shake (applied to Follow_Up originTimestamp or 1-step Sync)
    t1_offset_ns: int = 0
    t1_jitter_ns: int = 0
    t1_drift_step_ns: int = 0
    t1_random_enable: bool = False
    t1_random_max_ns: int = 0
    # T4 shake (applied to Delay_Resp receiveTimestamp)
    t4_offset_ns: int = 0
    t4_jitter_ns: int = 0
    t4_drift_step_ns: int = 0
    t4_random_enable: bool = False
    t4_random_max_ns: int = 0
    # Correction field offset (ns) added to relayed Sync/FU to compensate relay residence time
    correction_offset_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelayConfig":
        allowed = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**cleaned)


@dataclass
class RelayStats:
    sync_relayed: int = 0
    follow_up_relayed: int = 0
    announce_relayed: int = 0
    delay_req_rx: int = 0
    delay_req_fwd: int = 0
    delay_resp_tx: int = 0
    delay_resp_relayed: int = 0
    other_relayed: int = 0
    gm_rx_total: int = 0
    last_t1_ns_adj: int = 0
    last_t4_ns_adj: int = 0
    push_errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelayStats":
        allowed = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**cleaned)


class TrexPtpRelay:
    """Relay GM PTP from rx_port to tx_port with optional T1/T4 modification."""

    def __init__(
        self,
        config: RelayConfig,
        *,
        trex_path: str,
        rx_port: int = 0,
        tx_port: int = 1,
        rpc_server: str = "127.0.0.1",
        log: Optional[LogFn] = None,
    ):
        self.cfg = config
        self.trex_path = trex_path.rstrip("/")
        self.rx_port = int(rx_port)
        self.tx_port = int(tx_port)
        self.rpc_server = rpc_server or "127.0.0.1"
        self.log = log or (lambda m: None)
        self.stats = RelayStats()

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._t1_drift = 0
        self._t4_drift = 0
        self._lock = threading.Lock()

        self._client = None
        self._gm_cap_id: Optional[int] = None
        self._ru_cap_id: Optional[int] = None
        self._client_lock = threading.Lock()
        self._Ether = None
        self._Raw = None
        self._Dot1Q = None

        self._tx_src_mac = ""
        self._clock_id = b"\x00" * 8
        self._gm_clock_id: Optional[bytes] = None
        self._gm_source_port: int = 1
        self._gm_eth_src: str = ""
        self._gm_cap_start_ts: float = 0.0
        self._ru_cap_start_ts: float = 0.0

    def _needs_timing_modify(self) -> bool:
        """True when shake/correction settings require changing PTP timestamps."""
        c = self.cfg
        return bool(
            c.t1_offset_ns
            or c.t1_jitter_ns
            or c.t1_drift_step_ns
            or c.t1_random_enable
            or c.t4_offset_ns
            or c.t4_jitter_ns
            or c.t4_drift_step_ns
            or c.t4_random_enable
            or c.correction_offset_ns
        )

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

    def update_config(self, cfg: RelayConfig) -> None:
        with self._lock:
            self.cfg = cfg
        self.log(
            f"[CFG] relay t1_off={cfg.t1_offset_ns} t1_jit={cfg.t1_jitter_ns} "
            f"t4_off={cfg.t4_offset_ns} t4_jit={cfg.t4_jitter_ns} "
            f"corr_off={cfg.correction_offset_ns}"
        )

    # ── frame manipulation ──────────────────────────────────────────

    def _modify_timestamp_in_payload(self, payload: bytearray, offset: int, adj_ns: int) -> None:
        """In-place modify a 10-byte PTP timestamp at *offset* by adj_ns."""
        ts = unpack_timestamp(bytes(payload), offset)
        ts = ts.add_ns(adj_ns)
        packed = pack_timestamp(ts)
        payload[offset: offset + 10] = packed

    def _modify_correction_field(self, frame: bytearray, ptp_offset: int, add_ns: int) -> None:
        """Add nanoseconds to the 8-byte correctionField at ptp_offset + 8."""
        off = ptp_offset + 8
        corr_raw = struct.unpack_from("!q", frame, off)[0]
        corr_ns = corr_raw >> 16
        corr_ns += add_ns
        new_raw = (int(corr_ns) & 0xFFFFFFFFFFFF) << 16
        struct.pack_into("!q", frame, off, new_raw)

    def _rewrite_src_mac(self, frame: bytearray) -> None:
        """Keep GM Ethernet addresses by default — RU expects consistent L2/PTP binding."""
        # Do NOT rewrite src MAC; transparent L2 relay preserves GM wire format.
        pass

    def _record_gm_identity(self, hdr, raw_frame: bytes) -> None:
        if hdr.source_clock_id and hdr.source_clock_id != b"\x00" * 8:
            self._gm_clock_id = hdr.source_clock_id
        self._gm_source_port = int(hdr.source_port)
        mac = self._frame_src_mac(raw_frame)
        if mac:
            self._gm_eth_src = mac

    def _residence_ns(self, cap_pkt: Optional[dict] = None) -> int:
        """Relay residence time from capture timestamp (not poll time)."""
        base = int(self.cfg.correction_offset_ns)
        if cap_pkt is None:
            return base
        rel_ts = cap_pkt.get("ts")
        if rel_ts is None:
            return base
        try:
            rel = float(rel_ts)
        except (TypeError, ValueError):
            return base
        # TRex capture ts is seconds since capture start — convert to age at push time.
        cap_age_ns = int(max(0.0, (time.monotonic() - self._gm_cap_start_ts) - rel) * 1_000_000_000)
        return cap_age_ns + base

    def _push_frame_on_port(self, port: int, frame_bytes: bytes) -> None:
        c = self._client
        if c is None:
            return
        assert self._Ether is not None
        pkt = self._Ether(frame_bytes)
        try:
            with self._client_lock:
                c.push_packets(ports=[int(port)], pkts=[pkt], force=True)
        except Exception as exc:
            self.stats.push_errors += 1
            if self.stats.push_errors <= 10:
                self.log(f"[ERR] push port {port}: {exc}")

    def _push_frame(self, frame_bytes: bytes) -> None:
        self._push_frame_on_port(self.tx_port, frame_bytes)

    def _relay_gm_frame(self, raw_frame: bytes, *, cap_pkt: Optional[dict] = None) -> None:
        """Process one GM frame: modify if PTP, then TX on tx_port."""
        payload = extract_ptp_payload(raw_frame)
        if not payload:
            return

        hdr = parse_header(payload)
        if hdr is None:
            return
        if hdr.domain != self.cfg.domain:
            return

        self.stats.gm_rx_total += 1

        # Baseline LOCK: pass GM wire format through unchanged (no SW jitter on correctionField).
        if not self._needs_timing_modify():
            if hdr.message_type in (MSG_SYNC, MSG_FOLLOW_UP, MSG_ANNOUNCE):
                self._record_gm_identity(hdr, raw_frame)
            self._push_frame(raw_frame)
            if hdr.message_type == MSG_SYNC:
                self.stats.sync_relayed += 1
            elif hdr.message_type == MSG_FOLLOW_UP:
                self.stats.follow_up_relayed += 1
            elif hdr.message_type == MSG_ANNOUNCE:
                self.stats.announce_relayed += 1
            elif hdr.message_type == MSG_DELAY_RESP:
                self.stats.delay_resp_relayed += 1
                self.stats.delay_resp_tx += 1
            else:
                self.stats.other_relayed += 1
            return

        frame = bytearray(raw_frame)

        eth_type = struct.unpack("!H", frame[12:14])[0]
        ptp_offset = 14
        if eth_type == 0x8100 and len(frame) >= 18:
            ptp_offset = 18

        msg_type = hdr.message_type

        if msg_type == MSG_SYNC:
            self._record_gm_identity(hdr, raw_frame)
            residence = self._residence_ns(cap_pkt)
            if residence:
                self._modify_correction_field(frame, ptp_offset, residence)
            flags = hdr.flags
            two_step = bool(flags & 0x0200)
            if not two_step:
                adj = self._adj_t1()
                self.stats.last_t1_ns_adj = adj
                if adj != 0:
                    self._modify_timestamp_in_payload(frame, ptp_offset + 34, adj)
            self._rewrite_src_mac(frame)
            self._push_frame(bytes(frame))
            self.stats.sync_relayed += 1
            if self.stats.sync_relayed <= 5 or self.stats.sync_relayed % 64 == 0:
                self.log(f"[RELAY] Sync seq={hdr.sequence_id} two_step={two_step} resid={residence}ns")

        elif msg_type == MSG_FOLLOW_UP:
            self._record_gm_identity(hdr, raw_frame)
            adj = self._adj_t1()
            self.stats.last_t1_ns_adj = adj
            if adj != 0:
                self._modify_timestamp_in_payload(frame, ptp_offset + 34, adj)
            residence = self._residence_ns(cap_pkt)
            if residence:
                self._modify_correction_field(frame, ptp_offset, residence)
            self._rewrite_src_mac(frame)
            self._push_frame(bytes(frame))
            self.stats.follow_up_relayed += 1
            if self.stats.follow_up_relayed <= 5 or self.stats.follow_up_relayed % 64 == 0:
                self.log(f"[RELAY] Follow_Up seq={hdr.sequence_id} adj={adj:+d}ns")

        elif msg_type == MSG_ANNOUNCE:
            self._record_gm_identity(hdr, raw_frame)
            self._rewrite_src_mac(frame)
            self._push_frame(bytes(frame))
            self.stats.announce_relayed += 1
            if self.stats.announce_relayed <= 3 or self.stats.announce_relayed % 32 == 0:
                self.log(f"[RELAY] Announce seq={hdr.sequence_id}")

        elif msg_type == MSG_DELAY_RESP:
            adj = self._adj_t4()
            self.stats.last_t4_ns_adj = adj
            if adj != 0:
                self._modify_timestamp_in_payload(frame, ptp_offset + 34, adj)
            residence = self._residence_ns(cap_pkt)
            if residence:
                self._modify_correction_field(frame, ptp_offset, residence)
            self._push_frame(bytes(frame))
            self.stats.delay_resp_relayed += 1
            self.stats.delay_resp_tx += 1
            if self.stats.delay_resp_relayed <= 5 or self.stats.delay_resp_relayed % 64 == 0:
                self.log(
                    f"[RELAY] Delay_Resp seq={hdr.sequence_id} from GM "
                    f"adj={adj:+d}ns resid={residence}ns"
                )

        else:
            self._rewrite_src_mac(frame)
            self._push_frame(bytes(frame))
            self.stats.other_relayed += 1

    @staticmethod
    def _frame_src_mac(raw_frame: bytes) -> str:
        if len(raw_frame) >= 12:
            return ":".join(f"{b:02x}" for b in raw_frame[6:12])
        return ""

    def _handle_delay_req(self, raw_frame: bytes, rx_unix: float) -> None:
        """RU Delay_Req on tx_port → forward to GM on rx_port (GM answers with HW T4)."""
        del rx_unix  # GM path uses real Delay_Resp from GM capture
        payload = extract_ptp_payload(raw_frame)
        if not payload:
            return
        hdr = parse_header(payload)
        if hdr is None or hdr.message_type != MSG_DELAY_REQ:
            return
        if hdr.domain != self.cfg.domain:
            return
        if len(payload) < 44:
            return

        self.stats.delay_req_rx += 1
        ru_mac = self._frame_src_mac(raw_frame)
        self._push_frame_on_port(self.rx_port, raw_frame)
        self.stats.delay_req_fwd += 1
        if self.stats.delay_req_fwd <= 5 or self.stats.delay_req_fwd % 64 == 0:
            self.log(
                f"[RELAY] Delay_Req seq={hdr.sequence_id} fwd to GM port={self.rx_port} "
                f"ru={ru_mac or '?'}"
            )

    # ── capture polling loops ───────────────────────────────────────

    def _rx_unix_from_cap(self, pkt: dict) -> float:
        raw = pkt.get("ts")
        if raw is None:
            return time.time()
        ts = float(raw)
        if ts > 1_000_000_000:
            return ts
        return time.time()

    def _fetch_capture_batch(self, cap_id: int, handler, *, max_pkts: int = 16) -> int:
        """Fetch a small batch so relay TX keeps GM inter-packet spacing (no bursts)."""
        c = self._client
        if c is None:
            return 0
        pkts: list = []
        with self._client_lock:
            c.fetch_capture_packets(cap_id, pkts, pkt_count=max_pkts)
        count = 0
        for item in pkts:
            if not isinstance(item, dict):
                continue
            binary = item.get("binary")
            if not binary:
                continue
            handler(bytes(binary), item)
            count += 1
        return count

    def _gm_rx_loop(self) -> None:
        """Poll GM capture (rx_port) and relay frames to tx_port."""
        while not self._stop.is_set():
            cap_id = self._gm_cap_id
            if self._client is None or cap_id is None:
                if self._stop.wait(0.05):
                    break
                continue
            try:
                # Small fetch (≤16): fetching 500 and push_packets back-to-back caused sync bursts (sync=47/s).
                self._fetch_capture_batch(
                    cap_id,
                    lambda raw, item: self._relay_gm_frame(raw, cap_pkt=item),
                    max_pkts=16,
                )
            except Exception as exc:
                if not self._stop.is_set():
                    self.log(f"[WARN] gm capture poll: {exc}")
                    if self._stop.wait(0.2):
                        break
                    continue
            if self._stop.wait(0.001):
                break

    def _ru_rx_loop(self) -> None:
        """Poll RU capture (tx_port) for Delay_Req."""
        while not self._stop.is_set():
            cap_id = self._ru_cap_id
            if self._client is None or cap_id is None:
                if self._stop.wait(0.05):
                    break
                continue
            try:
                self._fetch_capture_batch(
                    cap_id,
                    lambda raw, item: self._handle_delay_req(raw, self._rx_unix_from_cap(item)),
                    max_pkts=32,
                )
            except Exception as exc:
                if not self._stop.is_set():
                    self.log(f"[WARN] ru capture poll: {exc}")
                    if self._stop.wait(0.2):
                        break
                    continue
            if self._stop.wait(0.002):
                break

    def _stat_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(5.0):
                break
            s = self.stats
            self.log(
                f"[STAT] relay sync={s.sync_relayed} fu={s.follow_up_relayed} "
                f"ann={s.announce_relayed} dreq={s.delay_req_rx} dreq_fwd={s.delay_req_fwd} "
                f"dresp={s.delay_resp_relayed} gm_rx={s.gm_rx_total} err={s.push_errors}"
            )

    # ── lifecycle ───────────────────────────────────────────────────

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

        self.log(
            f"[START] PTP Relay  rx_port={self.rx_port} tx_port={self.tx_port} "
            f"rpc={self.rpc_server}"
        )

        c = STLClient(server=self.rpc_server)
        c.connect()
        self._client = c

        try:
            ports = [self.rx_port, self.tx_port]
            c.acquire(ports=ports, force=True)
            c.set_service_mode(ports=ports, enabled=True)

            # Resolve tx_port MAC for src rewrite
            try:
                infos = c.get_port_info(ports=[self.tx_port])
                info = infos[0] if infos else {}
                self._tx_src_mac = str(
                    info.get("src_mac") or info.get("hw_mac") or info.get("mac") or ""
                ).lower()
            except Exception:
                self._tx_src_mac = ""
            if self._tx_src_mac:
                self._clock_id = clock_id_from_mac(self._tx_src_mac)

            for pidx in ports:
                try:
                    infos = c.get_port_info(ports=[pidx])
                    info = infos[0] if infos else {}
                    link_raw = info.get("link", info.get("link_up", ""))
                    if isinstance(link_raw, bool):
                        link_up = link_raw
                    elif isinstance(link_raw, str):
                        link_up = link_raw.strip().upper() == "UP"
                    else:
                        link_up = bool(link_raw)
                    self.log(
                        f"[PORT] trex:{pidx} link={'UP' if link_up else 'DOWN'} "
                        f"speed={info.get('speed')} mac={info.get('src_mac') or info.get('hw_mac')}"
                    )
                    if not link_up:
                        self.log(f"[WARN] port {pidx} link DOWN")
                except Exception:
                    pass

            bpf_gm = "ether proto 0x88f7 or (vlan and ether proto 0x88f7)"
            # RU port: capture Delay_Req only (PTP msg type 0x1 at byte 14 or 18 with VLAN)
            bpf_ru = (
                "(ether proto 0x88f7 and ether[14] & 0x0f = 0x01) or "
                "(vlan and ether proto 0x88f7 and ether[18] & 0x0f = 0x01)"
            )

            cap_gm = c.start_capture(
                rx_ports=[self.rx_port],
                limit=10000,
                mode="cyclic",
                bpf_filter=bpf_gm,
            )
            self._gm_cap_id = cap_gm["id"]
            self._gm_cap_start_ts = time.monotonic()

            cap_ru = c.start_capture(
                rx_ports=[self.tx_port],
                limit=5000,
                mode="cyclic",
                bpf_filter=bpf_ru,
            )
            self._ru_cap_id = cap_ru["id"]
            self._ru_cap_start_ts = time.monotonic()

            self.log(
                f"[RX] GM capture id={self._gm_cap_id} on port {self.rx_port}  "
                f"RU capture id={self._ru_cap_id} on port {self.tx_port}"
            )
            self.log(
                f"[CFG] domain={self.cfg.domain} t1_off={self.cfg.t1_offset_ns} "
                f"t4_off={self.cfg.t4_offset_ns} corr={self.cfg.correction_offset_ns} "
                f"transparent={not self._needs_timing_modify()}"
            )
            if self._needs_timing_modify():
                self.log(
                    "[WARN] shake/correction non-zero — Sync timing will be modified; "
                    "set T1/T4/correction all 0 for LOCK baseline"
                )
        except Exception:
            self._cleanup_client()
            raise

        t_gm = threading.Thread(target=self._gm_rx_loop, name="relay-gm-rx", daemon=True)
        t_ru = threading.Thread(target=self._ru_rx_loop, name="relay-ru-rx", daemon=True)
        t_st = threading.Thread(target=self._stat_loop, name="relay-stat", daemon=True)
        self._threads = [t_gm, t_ru, t_st]
        for t in self._threads:
            t.start()

    def _cleanup_client(self) -> None:
        c = self._client
        self._client = None
        gm_cap = self._gm_cap_id
        ru_cap = self._ru_cap_id
        self._gm_cap_id = None
        self._ru_cap_id = None
        if c is None:
            return
        try:
            for cap_id in (gm_cap, ru_cap):
                if cap_id is not None:
                    try:
                        c.stop_capture(capture_id=cap_id)
                    except Exception:
                        pass
            ports = [self.rx_port, self.tx_port]
            try:
                c.stop(ports=ports)
            except Exception:
                pass
            try:
                c.set_service_mode(ports=ports, enabled=False)
            except Exception:
                pass
            try:
                c.release(ports=ports)
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
        self.log("[STOP] PTP Relay stopped")
