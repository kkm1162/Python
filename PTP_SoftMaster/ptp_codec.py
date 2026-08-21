#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal IEEE 1588-2008 (PTPv2) encode/decode helpers."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Optional

ETH_TYPE_PTP = 0x88F7
PTP_EVENT_UDP = 319
PTP_GENERAL_UDP = 320

MSG_SYNC = 0x0
MSG_DELAY_REQ = 0x1
MSG_FOLLOW_UP = 0x8
MSG_DELAY_RESP = 0x9
MSG_ANNOUNCE = 0xB

FLAG_TWO_STEP = 0x0200  # flagField0 bit1
FLAG_UTC_OFFSET_VALID = 0x0004  # flagField1 bit2
FLAG_PTP_TIMESCALE = 0x0008  # flagField1 bit3
FLAG_TIME_TRACEABLE = 0x0010  # flagField1 bit4
FLAG_FREQ_TRACEABLE = 0x0020  # flagField1 bit5

# clockAccuracy (IEEE 1588 Table 6)
CLOCK_ACCURACY = {
    "Within 25 ns": 0x20,
    "Within 100 ns": 0x21,
    "Within 250 ns": 0x22,
    "Within 1 us": 0x23,
    "Within 2.5 us": 0x24,
    "Within 10 us": 0x25,
    "Within 25 us": 0x26,
    "Within 100 us": 0x27,
    "Within 250 us": 0x28,
    "Within 1 ms": 0x29,
    "Within 2.5 ms": 0x2A,
    "Within 10 ms": 0x2B,
    "Within 25 ms": 0x2C,
    "Within 100 ms": 0x2D,
    "Within 250 ms": 0x2E,
    "Within 1 s": 0x2F,
    "Within 10 s": 0x30,
    "Unknown": 0xFE,
}

# timeSource (IEEE 1588 Table 7)
TIME_SOURCE = {
    "Atomic Clock": 0x10,
    "GPS": 0x20,
    "Terrestrial Radio": 0x30,
    "PTP": 0x40,
    "NTP": 0x50,
    "Hand Set": 0x60,
    "Other": 0x90,
    "Internal Oscillator": 0xA0,
}

# clockClass common values
CLOCK_CLASS = {
    "Primary (6)": 6,
    "Primary holdover (7)": 7,
    "Application (13)": 13,
    "Application (14)": 14,
    "Default (248)": 248,
    "Slave-only (255)": 255,
}


@dataclass
class Timestamp:
    seconds: int = 0
    nanoseconds: int = 0

    @classmethod
    def from_unix(cls, unix_s: float) -> "Timestamp":
        sec = int(unix_s)
        nsec = int((unix_s - sec) * 1_000_000_000)
        if nsec < 0:
            sec -= 1
            nsec += 1_000_000_000
        if nsec >= 1_000_000_000:
            sec += nsec // 1_000_000_000
            nsec %= 1_000_000_000
        return cls(sec, nsec)

    @classmethod
    def now(cls) -> "Timestamp":
        return cls.from_unix_ns(time.time_ns())

    @classmethod
    def from_unix_ns(cls, unix_ns: int) -> "Timestamp":
        unix_ns = int(unix_ns)
        sec, nsec = divmod(unix_ns, 1_000_000_000)
        return cls(sec, nsec)

    @classmethod
    def now_ptp(cls, utc_offset_s: int = 37) -> "Timestamp":
        """
        PTP timescale timestamp (TAI-like epoch) for messages with PTP_TIMESCALE set.
        Linux CLOCK_REALTIME is UTC; add currentUtcOffset from Announce.
        """
        ns = time.time_ns()
        sec, nsec = divmod(ns, 1_000_000_000)
        sec += int(utc_offset_s)
        return cls(sec, nsec)

    def to_unix(self) -> float:
        return float(self.seconds) + self.nanoseconds / 1_000_000_000.0

    def add_ns(self, delta_ns: int) -> "Timestamp":
        total = self.seconds * 1_000_000_000 + self.nanoseconds + int(delta_ns)
        if total < 0:
            total = 0
        return Timestamp(total // 1_000_000_000, total % 1_000_000_000)

    def pack(self) -> bytes:
        return pack_timestamp(self)


def pack_timestamp(ts: Timestamp) -> bytes:
    """48-bit seconds + 32-bit nanoseconds (10 bytes)."""
    sec = int(ts.seconds) & 0xFFFFFFFFFFFF
    hi = (sec >> 32) & 0xFFFF
    lo = sec & 0xFFFFFFFF
    return struct.pack("!HII", hi, lo, int(ts.nanoseconds) & 0xFFFFFFFF)


def unpack_timestamp(data: bytes, offset: int = 0) -> Timestamp:
    hi, lo, nsec = struct.unpack_from("!HII", data, offset)
    sec = (hi << 32) | lo
    return Timestamp(sec, nsec)


def pack_port_identity(clock_id: bytes, port_number: int = 1) -> bytes:
    if len(clock_id) != 8:
        raise ValueError("clockIdentity must be 8 bytes")
    return clock_id + struct.pack("!H", port_number & 0xFFFF)


def unpack_port_identity(data: bytes, offset: int = 0) -> tuple[bytes, int]:
    clock_id = data[offset : offset + 8]
    port = struct.unpack_from("!H", data, offset + 8)[0]
    return clock_id, port


def clock_id_from_mac(mac: str) -> bytes:
    """EUI-64 style clockIdentity derived from MAC (common soft-master practice)."""
    parts = [int(x, 16) for x in mac.replace("-", ":").split(":")]
    if len(parts) != 6:
        raise ValueError(f"invalid MAC: {mac}")
    return bytes([parts[0], parts[1], parts[2], 0xFF, 0xFE, parts[3], parts[4], parts[5]])


@dataclass
class PtpHeader:
    message_type: int
    version: int = 2
    message_length: int = 34
    domain: int = 0
    flags: int = 0
    correction_ns: int = 0  # stored as ns; packed into correctionField (ns << 16)
    source_clock_id: bytes = b"\x00" * 8
    source_port: int = 1
    sequence_id: int = 0
    control: int = 0
    log_message_interval: int = 0
    transport_specific: int = 0

    def pack(self) -> bytes:
        b0 = ((self.transport_specific & 0xF) << 4) | (self.message_type & 0xF)
        b1 = self.version & 0xF
        # correctionField: nanoseconds in upper 48 bits of scaled ns (<<16)
        corr = (int(self.correction_ns) & 0xFFFFFFFFFFFF) << 16
        body = struct.pack(
            "!BBHBBHqI",
            b0,
            b1,
            self.message_length & 0xFFFF,
            self.domain & 0xFF,
            0,
            self.flags & 0xFFFF,
            corr,
            0,
        )
        body += pack_port_identity(self.source_clock_id, self.source_port)
        body += struct.pack(
            "!HBb",
            self.sequence_id & 0xFFFF,
            self.control & 0xFF,
            self.log_message_interval,
        )
        return body


def parse_header(data: bytes) -> Optional[PtpHeader]:
    if len(data) < 34:
        return None
    b0, b1, mlen, domain, _r1, flags, corr, _mts = struct.unpack_from("!BBHBBHqI", data, 0)
    clock_id, port = unpack_port_identity(data, 20)
    seq, control, log_i = struct.unpack_from("!HBb", data, 30)
    return PtpHeader(
        message_type=b0 & 0xF,
        version=b1 & 0xF,
        message_length=mlen,
        domain=domain,
        flags=flags,
        correction_ns=(corr >> 16),
        source_clock_id=clock_id,
        source_port=port,
        sequence_id=seq,
        control=control,
        log_message_interval=log_i,
        transport_specific=(b0 >> 4) & 0xF,
    )


def build_sync(
    hdr: PtpHeader,
    origin: Timestamp,
    *,
    two_step: bool = True,
) -> bytes:
    h = PtpHeader(**{**hdr.__dict__})
    h.message_type = MSG_SYNC
    h.control = 0x00
    h.message_length = 44
    if two_step:
        h.flags = (h.flags | FLAG_TWO_STEP) & 0xFFFF
        # 2-step Sync often carries zero originTimestamp; precise value is in Follow_Up.
        origin = Timestamp(0, 0)
    else:
        h.flags = h.flags & ~FLAG_TWO_STEP
    return h.pack() + pack_timestamp(origin)


def build_follow_up(hdr: PtpHeader, precise_origin: Timestamp) -> bytes:
    h = PtpHeader(**{**hdr.__dict__})
    h.message_type = MSG_FOLLOW_UP
    h.control = 0x02
    h.message_length = 44
    h.flags = h.flags & ~FLAG_TWO_STEP
    return h.pack() + pack_timestamp(precise_origin)


def build_delay_resp(
    hdr: PtpHeader,
    receive_ts: Timestamp,
    requesting_clock_id: bytes,
    requesting_port: int,
) -> bytes:
    h = PtpHeader(**{**hdr.__dict__})
    h.message_type = MSG_DELAY_RESP
    h.control = 0x03
    h.message_length = 54
    h.flags = h.flags & ~FLAG_TWO_STEP
    return h.pack() + pack_timestamp(receive_ts) + pack_port_identity(requesting_clock_id, requesting_port)


def build_announce(
    hdr: PtpHeader,
    *,
    origin: Timestamp | None = None,
    current_utc_offset: int = 37,
    priority1: int = 128,
    priority2: int = 255,
    clock_class: int = 6,
    clock_accuracy: int = 0x21,
    offset_scaled_log_variance: int = 0xFFFF,
    grandmaster_identity: bytes | None = None,
    steps_removed: int = 0,
    time_source: int = 0xA0,
    time_traceable: bool = True,
    freq_traceable: bool = True,
    utc_offset_valid: bool = True,
) -> bytes:
    """PTPv2 Announce (messageType 0xB), messageLength 64."""
    h = PtpHeader(**{**hdr.__dict__})
    h.message_type = MSG_ANNOUNCE
    h.control = 0x05
    h.message_length = 64
    flags = h.flags & ~FLAG_TWO_STEP
    flags |= FLAG_PTP_TIMESCALE
    if utc_offset_valid:
        flags |= FLAG_UTC_OFFSET_VALID
    if time_traceable:
        flags |= FLAG_TIME_TRACEABLE
    if freq_traceable:
        flags |= FLAG_FREQ_TRACEABLE
    h.flags = flags & 0xFFFF

    origin = origin or Timestamp.now_ptp(current_utc_offset)
    gm_id = grandmaster_identity or h.source_clock_id
    if len(gm_id) != 8:
        raise ValueError("grandmasterIdentity must be 8 bytes")

    body = h.pack()
    body += pack_timestamp(origin)
    body += struct.pack("!h", int(current_utc_offset))
    body += struct.pack("!B", 0)  # reserved
    body += struct.pack("!B", priority1 & 0xFF)
    body += struct.pack(
        "!BBH",
        clock_class & 0xFF,
        clock_accuracy & 0xFF,
        offset_scaled_log_variance & 0xFFFF,
    )
    body += struct.pack("!B", priority2 & 0xFF)
    body += gm_id
    body += struct.pack("!H", steps_removed & 0xFFFF)
    body += struct.pack("!B", time_source & 0xFF)
    return body


def extract_ptp_payload(pkt_bytes: bytes) -> Optional[bytes]:
    """Return PTP payload bytes from Ethernet (0x88F7) or UDP/319|320 frame."""
    if len(pkt_bytes) < 14:
        return None
    eth_type = struct.unpack("!H", pkt_bytes[12:14])[0]
    offset = 14
    if eth_type == 0x8100 and len(pkt_bytes) >= 18:
        eth_type = struct.unpack("!H", pkt_bytes[16:18])[0]
        offset = 18
    if eth_type == ETH_TYPE_PTP:
        return pkt_bytes[offset:]
    if eth_type != 0x0800:
        return None
    # IPv4 + UDP
    ip = pkt_bytes[offset:]
    if len(ip) < 20 or (ip[0] >> 4) != 4:
        return None
    ihl = (ip[0] & 0xF) * 4
    if len(ip) < ihl + 8:
        return None
    if ip[9] != 17:  # UDP
        return None
    udp = ip[ihl:]
    dport = struct.unpack("!H", udp[2:4])[0]
    if dport not in (PTP_EVENT_UDP, PTP_GENERAL_UDP):
        return None
    return udp[8:]
