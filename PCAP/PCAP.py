"""
O-RAN / eCPRI PCAP Protocol Analyzer
- PCAP 로드 → 프로토콜 트리 + Hex
- O-RAN Msg1~4 구분 / 시퀀스 실패 분석
- 라더 다이어그램 / C-U Plane 연동 뷰
"""

from __future__ import annotations

import os
import queue
import struct
import threading
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from oran_msg import (
    ORAN_MSG_LABELS,
    _pick_best_for_msg,
    analyze_msg2_gap,
        collect_msg2_sequence_issues,
    compute_hex_highlights,
    explain_orphan_msg2,
    format_msg2_gap_report,
    get_criteria_tab_rows,
    packet_oran_msg,
    timing_key,
)
from oran_fronthaul import apply_fronthaul_roles, expected_flow_for_msg, infer_du_ru_ips
from oran_uplane import find_dl_uplane_msg2_candidates, format_uplane_msg2_search_summary
from oran_advanced import (
    AdvancedAnalysisReport,
    format_advanced_report,
    get_advanced_criteria_rows,
    run_full_advanced_analysis,
)
from typing import Any

try:
    from scapy.all import Ether, IP, IPv6, Raw, TCP, UDP, rdpcap
    from scapy.packet import Packet
    from scapy.utils import PcapReader
except ImportError:
    Ether = IP = IPv6 = Raw = TCP = UDP = rdpcap = Packet = PcapReader = None  # type: ignore

# ---------------------------------------------------------------------------
# eCPRI / O-RAN constants
# ---------------------------------------------------------------------------
ECPRI_MSG_TYPES = {
    0x00: "IQ Data",
    0x01: "Bit Sequence",
    0x02: "Real-Time Control Data",
    0x03: "Generic Data Transfer",
    0x04: "Remote Memory Access",
    0x05: "One-Way Delay Measurement",
    0x06: "Remote Reset",
    0x07: "Event Indication",
}

ORAN_SECTION_TYPES = {
    0: "U-Plane Type-0",
    1: "Most DL UL radio channels",
    2: "PRACH and mixed-numerology channels",
    3: "PRACH and mixed-numerology channels",
    4: "Reserved",
    5: "UE scheduling information",
    6: "C-Plane RAN Scheduling",
    7: "LAA",
    8: "Real-Time Control",
}

RACH_MSG_DEFS = {
    m: {**v, "direction": d}
    for m, v, d in (
        (1, ORAN_MSG_LABELS[1], "U-Plane"),
        (2, ORAN_MSG_LABELS[2], "O-DU → O-RU (DL 스케줄)"),
        (3, ORAN_MSG_LABELS[3], "O-DU → O-RU (UL 스케줄)"),
        (4, ORAN_MSG_LABELS[4], "O-DU → O-RU"),
    )
}

# 슬롯 내 MSG1~4 허용 시간 (ms) — 같은 Frame/Subframe/Slot
ORAN_SLOT_WINDOW_MS = 50.0
RACH_SEQUENCE_WINDOW_MS = ORAN_SLOT_WINDOW_MS

ANALYSIS_MODES = [
    ("tree", "프로토콜 트리 뷰"),
    ("ladder", "라더 다이어그램 뷰"),
    ("cuplane", "C/U-Plane 연동 뷰"),
]

# 대용량 PCAP (2~3GB) 안정성
LIST_PAGE_SIZE = 10_000
LIST_INSERT_BATCH = 400
PARSE_PROGRESS_EVERY = 5_000
LARGE_FILE_WARN_BYTES = 512 * 1024 * 1024

# 패킷 목록 컬럼 (id → title, width, anchor)
PACKET_LIST_COL_DEFS: dict[str, tuple[str, int, str]] = {
    "no": ("#", 32, "center"),
    "source": ("Source", 72, "w"),
    "rach": ("Msg", 56, "center"),
    "eaxc": ("c_eAxC_ID", 56, "w"),
    "length": ("Length", 52, "center"),
    "direction": ("Data Direction", 72, "w"),
    "flow": ("Fronthaul", 64, "center"),
    "frame": ("Frame ID", 52, "center"),
    "subframe": ("Subframe ID", 68, "center"),
    "slot": ("Slot ID", 48, "center"),
    "symbol": ("Symbol ID", 60, "center"),
    "info": ("Info", 200, "w"),
    "time": ("Time", 72, "w"),
}
PACKET_LIST_COLUMNS = tuple(PACKET_LIST_COL_DEFS.keys())


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class TreeNode:
    label: str
    value: str = ""
    children: list[TreeNode] = field(default_factory=list)

    def to_tuples(self) -> tuple[str, str, list]:
        return (self.label, self.value, [c.to_tuples() for c in self.children])


@dataclass
class RachSequence:
    seq_id: int
    msgs: dict[int, ParsedPacket] = field(default_factory=dict)
    source: str = "strict"
    failed_at_msg: int | None = None  # 최초 누락 Msg 번호 (2=Msg2 fail)

    @property
    def start_time(self) -> float:
        if 1 in self.msgs:
            return self.msgs[1].timestamp
        if self.msgs:
            return min(p.timestamp for p in self.msgs.values())
        return 0.0

    @property
    def is_complete(self) -> bool:
        return len(self.msgs) == 4 and self.failed_at_msg is None

    @property
    def is_failed(self) -> bool:
        return self.failed_at_msg is not None and 1 in self.msgs

    @property
    def status_text(self) -> str:
        if self.is_complete:
            return "성공"
        if self.is_failed:
            return f"실패 (Msg{self.failed_at_msg} 없음)"
        return "부분"

    def label(self) -> str:
        parts = [f"Msg{m}=#{self.msgs[m].index + 1}" for m in sorted(self.msgs)]
        return " | ".join(parts)


@dataclass
class ParsedPacket:
    index: int
    timestamp: float
    summary: str
    src: str
    dst: str
    length: int
    tree: TreeNode | None = None
    raw_bytes: bytes = field(default_factory=bytes)
    raw_hex: str = ""
    file_offset: int = 0
    pcap_source: str = ""
    _materialized: bool = field(default=False, repr=False)
    is_ecpri: bool = False
    ecpri_type: int | None = None
    section_type: int | None = None
    rach_msg: int | None = None
    rach_source: str | None = None  # "keyword" | "section" | "sequence"
    rach_sequence_id: int | None = None
    is_uplink: bool | None = None
    plane: str = "unknown"
    ecpri_payload_size: int = 0
    udp_payload_len: int = 0
    frame_id: int | None = None
    subframe_id: int | None = None
    slot_id: int | None = None
    eaxc_id: str = ""
    symbol_id: int | None = None
    section_id: int | None = None
    start_prb: int | None = None
    end_prb: int | None = None
    num_prb: int | None = None
    info_text: str = ""
    direction: str = ""
    flow: str = ""
    ecpri_offset: int | None = None
    oran_offset: int | None = None
    udp_sport: int | None = None
    udp_dport: int | None = None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _compute_ecpri_offsets(pkt: Packet, raw_bytes: bytes) -> tuple[int | None, int | None]:
    payload = b""
    if pkt.haslayer(UDP):
        payload = bytes(pkt[UDP].payload)
    elif pkt.haslayer(Raw):
        payload = bytes(pkt[Raw].load)
    if len(payload) < 4:
        return None, None
    needle = payload[: min(8, len(payload))]
    ecpri_off = raw_bytes.find(needle)
    if ecpri_off < 0:
        return None, None
    msg_type = payload[1]
    oran_rel = 8 if msg_type in (0x00, 0x02) and len(payload) >= 8 else 4
    oran_off = ecpri_off + oran_rel if len(raw_bytes) > ecpri_off + oran_rel else None
    return ecpri_off, oran_off


def _hex_index_range(byte_start: int, byte_end: int) -> list[tuple[str, str]]:
    """raw 바이트 구간 → tk.Text 인덱스 (start, end) 목록."""
    spans: list[tuple[str, str]] = []
    for b in range(byte_start, byte_end):
        line = b // 16 + 1
        col = 8 + (b % 16) * 3
        spans.append((f"{line}.{col}", f"{line}.{col + 2}"))
    return spans


def format_full_hex(data: bytes) -> str:
    lines: list[str] = []
    for i in range(0, len(data), 16):
        part = data[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in part)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in part)
        lines.append(f"{i:06x}  {hex_part:<48}  {ascii_part}")
    return "\n".join(lines)


def _hex_preview(data: bytes, limit: int = 256) -> str:
    chunk = data[:limit]
    lines = []
    for i in range(0, len(chunk), 16):
        part = chunk[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in part)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in part)
        lines.append(f"{i:04x}  {hex_part:<48}  {ascii_part}")
    if len(data) > limit:
        lines.append(f"... ({len(data) - limit} bytes more)")
    return "\n".join(lines)


def _format_eaxc(pc_id: int) -> str:
    """O-RAN c_eAxC_ID: DU:BandSector:CC:RU"""
    return f"{(pc_id >> 12) & 0xF}:{(pc_id >> 8) & 0xF}:{(pc_id >> 4) & 0xF}:{pc_id & 0xF}"


def _build_info_text(
    ecpri_type: int | None,
    section_id: int | None,
    start_prb: int | None,
    end_prb: int | None,
    section_type: int | None,
) -> str:
    if ecpri_type == 0x00:
        plane = "U-Plane"
    elif ecpri_type == 0x02:
        plane = "C-Plane"
    elif ecpri_type == 0x04:
        plane = "ACK/NACK"
    else:
        plane = "eCPRI"
    parts = [plane]
    if section_id is not None:
        parts.append(f"Id: {section_id}")
    if start_prb is not None:
        if end_prb is not None and end_prb >= start_prb:
            parts.append(f"(PRB: {start_prb:3d}-{end_prb:3d})")
        else:
            parts.append(f"(PRB: {start_prb:3d})")
    elif section_type is not None:
        parts.append(f"SecType {section_type}")
    return ", ".join(parts)


def _parse_ecpri(payload: bytes) -> tuple[TreeNode | None, dict[str, Any]]:
    if len(payload) < 4:
        return None, {}
    version_concat = payload[0]
    msg_type = payload[1]
    payload_size = struct.unpack("!H", payload[2:4])[0]
    version = (version_concat >> 4) & 0x0F
    concat = version_concat & 0x01

    info: dict[str, Any] = {
        "msg_type": msg_type,
        "payload_size": payload_size,
        "is_ecpri": True,
    }

    children: list[TreeNode] = [
        TreeNode("Version", str(version)),
        TreeNode("Concatenation", str(concat)),
        TreeNode("Message Type", f"0x{msg_type:02x} ({ECPRI_MSG_TYPES.get(msg_type, 'Unknown')})"),
        TreeNode("Payload Size", str(payload_size)),
    ]

    offset = 4
    if msg_type in (0x00, 0x02) and len(payload) >= offset + 4:
        pc_id = struct.unpack("!H", payload[offset : offset + 2])[0]
        seq_id = struct.unpack("!H", payload[offset + 2 : offset + 4])[0]
        children.append(TreeNode("c_eAxC_ID", _format_eaxc(pc_id)))
        children.append(TreeNode("eCPRI PC_ID", f"0x{pc_id:04x}"))
        children.append(TreeNode("eCPRI Seq_ID", f"0x{seq_id:04x}"))
        offset += 4
        info["plane"] = "user" if msg_type == 0x00 else "control"
        info["pc_id"] = pc_id
        info["eaxc_id"] = _format_eaxc(pc_id)

    # O-RAN CUS: dataDirection bit7 — 0=Uplink(gNB Rx), 1=Downlink(gNB Tx)
    if len(payload) > offset + 4:
        oran_children, oran_info = _parse_oran_header(payload[offset:], msg_type)
        if oran_children:
            children.append(TreeNode("O-RAN Control Header", children=oran_children))
            info.update(oran_info)
            if msg_type == 0x00:
                info["plane"] = "user"
            elif msg_type == 0x02:
                info["plane"] = "control"

    return TreeNode("eCPRI Header", children=children), info


def _parse_cplane_section_type(data: bytes) -> int | None:
    """C-Plane: numberOfSections(byte4) + sectionType(byte5), 폴백 탐색."""
    if len(data) < 6:
        return None
    st = data[5]
    if 1 <= st <= 11:
        return st
    nos = data[4]
    if st == 0 and 1 <= nos <= 11:
        return nos
    if st == 0:
        for off in (4, 6, 7, 8):
            if off < len(data) and data[off] == 6:
                return 6
    return None


def _parse_oran_header(data: bytes, ecpri_type: int | None = None) -> tuple[list[TreeNode], dict[str, Any]]:
    if len(data) < 4:
        return [], {}
    data_dir = (data[0] >> 7) & 1
    is_uplink = data_dir == 0
    payload_ver = (data[0] >> 4) & 0x07
    filter_idx = data[0] & 0x0F
    frame_id = data[1]
    subframe_id = (data[2] >> 4) & 0x0F
    slot_id = ((data[2] & 0x0F) << 2) | ((data[3] >> 6) & 0x03)
    symbol_id = data[3] & 0x3F
    section_type = None
    section_id = None
    start_prb = None
    end_prb = None

    nodes: list[TreeNode] = [
        TreeNode("Data Direction", "Uplink" if is_uplink else "Downlink"),
        TreeNode("Payload Version", str(payload_ver)),
        TreeNode("Filter Index", str(filter_idx)),
        TreeNode("Frame ID", str(frame_id)),
        TreeNode("Subframe ID", str(subframe_id)),
        TreeNode("Slot ID", str(slot_id)),
        TreeNode("Symbol Identifier", f"0x{symbol_id:02x}"),
    ]

    info: dict[str, Any] = {
        "is_uplink": is_uplink,
        "frame_id": frame_id,
        "subframe_id": subframe_id,
        "slot_id": slot_id,
        "symbol_id": symbol_id,
        "direction": "Uplink" if is_uplink else "Downlink",
    }

    if ecpri_type == 0x02 and len(data) >= 6:
        # C-Plane: sectionType은 common header 다음 byte 5 (Wireshark ORAN)
        section_type = _parse_cplane_section_type(data)
        if section_type is not None:
            nodes.append(TreeNode("Section Type", f"{section_type} ({ORAN_SECTION_TYPES.get(section_type, 'Other')})"))
            info["section_type"] = section_type
        if len(data) >= 8:
            section_id = struct.unpack("!H", data[6:8])[0] & 0x0FFF
            nodes.append(TreeNode("Section ID", str(section_id)))
            info["section_id"] = section_id
    elif len(data) >= 8:
        # U-Plane: sectionId 12bit + rb/symInc 등 (sectionType 필드 없음)
        section_word = struct.unpack("!H", data[4:6])[0]
        section_id = section_word & 0x0FFF
        section_type = (section_word >> 12) & 0x0F
        if section_type:
            nodes.append(TreeNode("Section Type", f"{section_type} ({ORAN_SECTION_TYPES.get(section_type, 'Other')})"))
            info["section_type"] = section_type
        nodes.append(TreeNode("Section ID", str(section_id)))
        info["section_id"] = section_id
        if len(data) >= 8:
            start_prb = data[7]
            nodes.append(TreeNode("Start PRB", str(start_prb)))
            info["start_prb"] = start_prb
        if len(data) >= 11:
            num_prb = data[10]
            info["num_prb"] = num_prb
            if num_prb > 0 and start_prb is not None:
                nodes.append(TreeNode("Num PRB", str(num_prb)))
                info["end_prb"] = start_prb + num_prb - 1

    return nodes, info


def _layer_tree(pkt: Packet) -> TreeNode:
    def walk(p: Packet, depth: int = 0) -> TreeNode:
        name = p.__class__.__name__
        fields: list[TreeNode] = []
        if hasattr(p, "fields_desc"):
            for fd in p.fields_desc:
                fname = fd.name
                try:
                    val = getattr(p, fname)
                    if isinstance(val, (bytes, bytearray)):
                        disp = val.hex() if len(val) <= 16 else f"{val[:16].hex()}..."
                    else:
                        disp = str(val)
                    fields.append(TreeNode(fname, disp))
                except Exception:
                    pass
        payload = bytes(p.payload) if p.payload else b""
        if payload and p.__class__.__name__ not in ("Raw", "Padding"):
            if len(payload) >= 4 and p.__class__.__name__ in ("UDP", "TCP"):
                ecpri_node, _ = _parse_ecpri(payload)
                if ecpri_node:
                    fields.append(ecpri_node)
                else:
                    fields.append(TreeNode("Payload", f"{len(payload)} bytes", [
                        TreeNode("Hex Preview", _hex_preview(payload, 64).replace("\n", " | "))
                    ]))
        return TreeNode(name, children=fields)

    return walk(pkt)


def _packet_endpoints(pkt: Packet) -> tuple[str, str]:
    src = dst = "?"
    if pkt.haslayer(Ether):
        src = pkt[Ether].src
        dst = pkt[Ether].dst
    if pkt.haslayer(IP):
        src = pkt[IP].src
        dst = pkt[IP].dst
    elif pkt.haslayer(IPv6):
        src = pkt[IPv6].src
        dst = pkt[IPv6].dst
    return src, dst


# IEEE 1588 PTP (EtherType 0x88F7) — Fronthaul 분석에서 제외
PTP_ETHERTYPE = 0x88F7


def _is_ptp_packet(pkt: Packet) -> bool:
    """EtherType 0x88F7 만 PTP로 간주."""
    if pkt.haslayer(Ether):
        try:
            if int(pkt[Ether].type) == PTP_ETHERTYPE:
                return True
        except (TypeError, ValueError):
            pass
    raw = bytes(pkt)
    for off in (12, 16, 18, 20):
        if len(raw) >= off + 2 and raw[off : off + 2] == b"\x88\xf7":
            return True
    return False


def _clear_rach(packets: list[ParsedPacket]) -> None:
    for p in packets:
        p.rach_msg = None
        p.rach_source = None
        p.rach_sequence_id = None


def _build_slot_sequence(group: list[ParsedPacket]) -> RachSequence | None:
    """동일 Frame/Subframe/Slot 내 MSG1→2→3→4 (Msg2 없으면 Msg3/4 미포함)."""
    by_msg: dict[int, list[ParsedPacket]] = {i: [] for i in range(1, 5)}
    for p in group:
        m, _ = packet_oran_msg(p)
        if m is not None:
            by_msg[m].append(p)

    if not by_msg[1]:
        return None

    msgs: dict[int, ParsedPacket] = {}
    failed_at: int | None = None
    for m in (1, 2, 3, 4):
        pick = _pick_best_for_msg(by_msg[m], m)
        if pick is None:
            failed_at = m
            break
        msgs[m] = pick

    return RachSequence(seq_id=-1, msgs=msgs, source="oran", failed_at_msg=failed_at)


def discover_all_rach_sequences(
    packets: list[ParsedPacket],
    *,
    include_heuristic: bool = False,
) -> list[RachSequence]:
    _clear_rach(packets)

    from collections import defaultdict

    slots: dict[tuple[int, int, int], list[ParsedPacket]] = defaultdict(list)
    for p in packets:
        tk = timing_key(p)
        if tk is not None:
            slots[tk].append(p)

    sequences: list[RachSequence] = []
    for tk in sorted(slots.keys()):
        seq = _build_slot_sequence(slots[tk])
        if seq is None:
            continue
        seq.seq_id = len(sequences)
        sequences.append(seq)

    # 태깅: 시퀀스에 포함된 패킷만 (Msg2 실패 시 Msg1만, Msg3/4 절대 미태깅)
    for seq in sequences:
        for msg_num, pkt in seq.msgs.items():
            pkt.rach_msg = msg_num
            pkt.rach_sequence_id = seq.seq_id
            pkt.rach_source = "oran"

    sequences.sort(key=lambda s: s.start_time)
    for i, seq in enumerate(sequences):
        seq.seq_id = i
        for pkt in seq.msgs.values():
            pkt.rach_sequence_id = i

    return [s for s in sequences if s.msgs]


def _detect_rach_strict(packets: list[ParsedPacket]) -> int:
    sequences = finalize_pcap_load(packets)
    return sum(len(s.msgs) for s in sequences)


def _build_packet_metadata(
    pkt: Packet,
    index: int,
    file_offset: int,
    *,
    pcap_source: str = "",
) -> ParsedPacket:
    """메타데이터만 추출 (tree/hex/raw_bytes는 선택 시 로드)."""
    src, dst = _packet_endpoints(pkt)
    ts = float(pkt.time) if hasattr(pkt, "time") else 0.0
    length = len(pkt)
    tmp_bytes = bytes(pkt)

    is_ecpri = False
    ecpri_type = None
    section_type = None
    plane = "unknown"
    is_uplink = None
    ecpri_payload_size = 0
    frame_id = subframe_id = slot_id = None
    eaxc_id = ""
    symbol_id = None
    section_id = None
    start_prb = end_prb = None
    num_prb = None
    direction = ""
    info_text = ""

    udp_sport = udp_dport = None
    if pkt.haslayer(UDP):
        udp_sport = int(pkt[UDP].sport)
        udp_dport = int(pkt[UDP].dport)

    payload = b""
    if pkt.haslayer(UDP):
        payload = bytes(pkt[UDP].payload)
    elif pkt.haslayer(Raw):
        payload = bytes(pkt[Raw].load)
    udp_payload_len = len(payload)

    if payload:
        _, ecpri_info = _parse_ecpri(payload)
        is_ecpri = ecpri_info.get("is_ecpri", False)
        ecpri_type = ecpri_info.get("msg_type")
        section_type = ecpri_info.get("section_type")
        plane = ecpri_info.get("plane", "unknown")
        is_uplink = ecpri_info.get("is_uplink")
        ecpri_payload_size = ecpri_info.get("payload_size", 0)
        frame_id = ecpri_info.get("frame_id")
        subframe_id = ecpri_info.get("subframe_id")
        slot_id = ecpri_info.get("slot_id")
        eaxc_id = ecpri_info.get("eaxc_id", "")
        symbol_id = ecpri_info.get("symbol_id")
        section_id = ecpri_info.get("section_id")
        start_prb = ecpri_info.get("start_prb")
        end_prb = ecpri_info.get("end_prb")
        num_prb = ecpri_info.get("num_prb")
        direction = ecpri_info.get("direction", "")
        info_text = _build_info_text(ecpri_type, section_id, start_prb, end_prb, section_type)

    ecpri_off, oran_off = _compute_ecpri_offsets(pkt, tmp_bytes)

    return ParsedPacket(
        index=index,
        timestamp=ts,
        summary=info_text or f"#{index + 1}",
        src=src,
        dst=dst,
        length=length,
        tree=None,
        raw_bytes=b"",
        raw_hex="",
        file_offset=file_offset,
        pcap_source=pcap_source,
        is_ecpri=is_ecpri,
        ecpri_type=ecpri_type,
        section_type=section_type,
        rach_msg=None,
        rach_source=None,
        is_uplink=is_uplink,
        plane=plane,
        ecpri_payload_size=ecpri_payload_size,
        udp_payload_len=udp_payload_len,
        frame_id=frame_id,
        subframe_id=subframe_id,
        slot_id=slot_id,
        eaxc_id=eaxc_id,
        symbol_id=symbol_id,
        section_id=section_id,
        start_prb=start_prb,
        end_prb=end_prb,
        num_prb=num_prb,
        info_text=info_text,
        direction=direction,
        ecpri_offset=ecpri_off,
        oran_offset=oran_off,
        udp_sport=udp_sport,
        udp_dport=udp_dport,
    )


def materialize_packet(meta: ParsedPacket, path: str) -> None:
    """선택된 패킷만 파일에서 읽어 tree/hex/raw_bytes 생성."""
    if meta._materialized:
        return
    if PcapReader is None:
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")
    with PcapReader(path) as reader:
        reader.f.seek(meta.file_offset)
        pkt = reader.read_packet()
    meta.raw_bytes = bytes(pkt)
    meta.raw_hex = format_full_hex(meta.raw_bytes)
    meta.tree = _layer_tree(pkt)
    if pkt.haslayer(UDP):
        meta.udp_sport = int(pkt[UDP].sport)
        meta.udp_dport = int(pkt[UDP].dport)
    if meta.ecpri_offset is None:
        meta.ecpri_offset, meta.oran_offset = _compute_ecpri_offsets(pkt, meta.raw_bytes)
    meta._materialized = True


def dematerialize_packet(meta: ParsedPacket) -> None:
    meta.raw_bytes = b""
    meta.raw_hex = ""
    meta.tree = None
    meta._materialized = False


def parse_pcap(
    path: str,
    *,
    pcap_source: str | None = None,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    load_stats: dict[str, int] | None = None,
) -> list[ParsedPacket]:
    if PcapReader is None:
        raise RuntimeError("scapy가 설치되어 있지 않습니다. pip install scapy 를 실행하세요.")

    source = pcap_source or path
    results: list[ParsedPacket] = []
    ptp_skipped = 0
    with PcapReader(path) as reader:
        i = 0
        while True:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("사용자가 로드를 취소했습니다.")
            try:
                offset = reader.f.tell()
                pkt = reader.read_packet()
            except EOFError:
                break
            if _is_ptp_packet(pkt):
                ptp_skipped += 1
                continue
            results.append(_build_packet_metadata(pkt, i, offset, pcap_source=source))
            i += 1
            if progress_cb and i % PARSE_PROGRESS_EVERY == 0:
                progress_cb(i, "parse")
    if load_stats is not None:
        load_stats["ptp_skipped"] = load_stats.get("ptp_skipped", 0) + ptp_skipped
    if progress_cb:
        progress_cb(len(results), "parse")
    return results


def parse_pcaps(
    paths: list[str],
    *,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    load_stats: dict[str, int] | None = None,
) -> list[ParsedPacket]:
    """여러 PCAP을 읽어 타임스탬프 순으로 병합."""
    if not paths:
        return []
    if len(paths) == 1:
        return parse_pcap(
            paths[0],
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            load_stats=load_stats,
        )

    merged: list[ParsedPacket] = []
    total_files = len(paths)
    for fi, path in enumerate(paths):
        base = len(merged)

        def file_progress(count: int, phase: str) -> None:
            if progress_cb:
                progress_cb(
                    base + count,
                    phase,
                    fi + 1,
                    total_files,
                    os.path.basename(path),
                )

        batch = parse_pcap(
            path,
            pcap_source=path,
            progress_cb=file_progress,
            cancel_event=cancel_event,
            load_stats=load_stats,
        )
        merged.extend(batch)

    merged.sort(key=lambda p: (p.timestamp, p.pcap_source, p.file_offset))
    for i, p in enumerate(merged):
        p.index = i
    if progress_cb:
        progress_cb(len(merged), "parse_done", total_files, total_files, "")
    return merged


def finalize_pcap_load(packets: list[ParsedPacket]) -> list[RachSequence]:
    apply_fronthaul_roles(packets)
    sequences = discover_all_rach_sequences(packets, include_heuristic=False)
    _update_packet_summaries(packets)
    return sequences


def _update_packet_summaries(packets: list[ParsedPacket]) -> None:
    for p in packets:
        parts: list[str] = []
        if p.rach_msg:
            label = RACH_MSG_DEFS[p.rach_msg]["short"]
            if p.rach_sequence_id is not None:
                label += f" S{p.rach_sequence_id + 1}"
            parts.append(label)
        if p.info_text:
            parts.append(p.info_text)
        p.summary = " | ".join(parts) if parts else p.info_text or f"#{p.index + 1}"


def get_rach_packets_for_sequence(
    packets: list[ParsedPacket],
    seq_id: int | None,
) -> dict[int, ParsedPacket]:
    out: dict[int, ParsedPacket] = {}
    for p in sorted(packets, key=lambda x: x.timestamp):
        if p.rach_msg and p.rach_sequence_id == seq_id and p.rach_msg not in out:
            out[p.rach_msg] = p
    return out


def get_rach_packets(packets: list[ParsedPacket]) -> dict[int, ParsedPacket]:
    """첫 번째 시퀀스 (하위 호환)."""
    seq_ids = sorted({p.rach_sequence_id for p in packets if p.rach_sequence_id is not None})
    sid = seq_ids[0] if seq_ids else None
    return get_rach_packets_for_sequence(packets, sid)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class ProtocolAnalyzerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("O-RAN Protocol Analyzer")
        self.geometry("1280x800")
        self.minsize(960, 640)

        self.packets: list[ParsedPacket] = []
        self.pcap_paths: list[str] = []
        self.mode_index = 0
        self.selected_packet: ParsedPacket | None = None
        self._rach: dict[int, ParsedPacket] = {}
        self.rach_sequences: list[RachSequence] = []
        self.current_sequence_idx = 0
        self.filter_rach_only = tk.BooleanVar(value=False)
        self.filter_orphan_msg2 = tk.BooleanVar(value=False)
        self.filter_uplane_msg2 = tk.BooleanVar(value=False)
        self._uplane_msg2_max_rb = 4
        self._criteria_tab_open = False
        self._list_page = 0
        self._list_indices: list[int] = []
        self._use_pagination = False
        self._populate_job: str | None = None
        self._loading = False
        self._load_cancel = threading.Event()
        self._load_queue: queue.Queue = queue.Queue()
        self._load_dialog: tk.Toplevel | None = None
        self._materialized_index: int | None = None
        self.oran_version = tk.StringVar(value="3.0")
        self._col_drag_src: str | None = None
        self.du_ip: str | None = None
        self.ru_ip: str | None = None
        self._orphan_msg2_indices: list[int] = []
        self._orphan_msg2_index_set: set[int] = set()
        self._orphan_msg2_reasons: dict[int, str] = {}
        self._seq_fail_msg2: list[RachSequence] = []
        self._orphan_msg2_nav = 0
        self._seq_fail_msg2_nav = 0
        self._msg2_gap_report = None
        self._hidden_msg2_indices: list[int] = []
        self._hidden_msg2_reasons: dict[int, str] = {}
        self._hidden_msg2_nav = 0
        self._uplane_msg2_indices: list[int] = []
        self._uplane_msg2_index_set: set[int] = set()
        self._uplane_msg2_reasons: dict[int, str] = {}
        self._uplane_msg2_nav = 0

        self._build_ui()
        self._show_welcome()

    def _build_ui(self) -> None:
        # Header
        header = ttk.Frame(self, padding=(12, 8))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="O-RAN Studio 프로토콜 분석기",
            font=("Segoe UI", 16, "bold"),
        ).pack(side=tk.LEFT)
        self._open_btn = ttk.Button(header, text="PCAP 열기...", command=self._open_pcap)
        self._open_btn.pack(side=tk.RIGHT, padx=4)
        ttk.Button(header, text="Msg2 U-Plane", command=self._open_uplane_msg2_search).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header, text="심화 MSG 분석", command=self._open_advanced_analysis).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header, text="RACH 메시지 스캔", command=self._scan_rach).pack(side=tk.RIGHT, padx=4)

        # Main paned: packet list | analysis area
        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # Left — packet list
        left = ttk.Frame(main, width=520)
        main.add(left, weight=2)

        list_header = ttk.Frame(left)
        list_header.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(list_header, text="패킷 목록", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(
            list_header, text="헤더 드래그: 순서 | 경계 드래그: 크기",
            font=("Segoe UI", 8), foreground="gray",
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(
            list_header,
            text="RACH만",
            variable=self.filter_rach_only,
            command=self._on_rach_filter_changed,
        ).pack(side=tk.RIGHT, padx=4)
        ttk.Checkbutton(
            list_header,
            text="M2-U",
            variable=self.filter_uplane_msg2,
            command=self._on_uplane_msg2_filter_changed,
        ).pack(side=tk.RIGHT, padx=4)
        ttk.Checkbutton(
            list_header,
            text="Msg2≠",
            variable=self.filter_orphan_msg2,
            command=self._on_orphan_msg2_filter_changed,
        ).pack(side=tk.RIGHT, padx=4)

        jump_bar = ttk.Frame(left)
        jump_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(jump_bar, text="바로가기:", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 4))
        for msg in (1, 2, 3, 4):
            ttk.Button(
                jump_bar,
                text=f"Msg{msg}",
                width=5,
                command=lambda m=msg: self._jump_to_rach(m),
            ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            jump_bar,
            text="Msg2≠",
            width=6,
            command=self._jump_next_orphan_msg2,
        ).pack(side=tk.LEFT, padx=(8, 2))
        ttk.Button(
            jump_bar,
            text="S2실패",
            width=6,
            command=self._jump_next_seq_fail_msg2,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            jump_bar,
            text="Msg2?",
            width=6,
            command=self._jump_next_hidden_msg2,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            jump_bar,
            text="M2-U",
            width=5,
            command=self._jump_next_uplane_msg2,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            jump_bar,
            text="Msg2진단",
            width=7,
            command=self._show_msg2_gap_dialog,
        ).pack(side=tk.LEFT, padx=2)

        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.packet_tree = ttk.Treeview(
            list_frame, columns=PACKET_LIST_COLUMNS, show="headings", selectmode="browse",
        )
        self._packet_col_widths = {c: PACKET_LIST_COL_DEFS[c][1] for c in PACKET_LIST_COLUMNS}
        self._setup_packet_tree_columns()
        self._bind_packet_tree_column_controls()
        sb_y = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.packet_tree.yview)
        sb_x = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=self.packet_tree.xview)
        self.packet_tree.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        self.packet_tree.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.packet_tree.bind("<<TreeviewSelect>>", self._on_packet_select)

        page_bar = ttk.Frame(left)
        page_bar.pack(fill=tk.X, pady=(4, 0))
        self._page_prev_btn = ttk.Button(page_bar, text="◀", width=3, command=self._prev_list_page)
        self._page_prev_btn.pack(side=tk.LEFT)
        self.page_label = ttk.Label(page_bar, text="", font=("Segoe UI", 9))
        self.page_label.pack(side=tk.LEFT, padx=8)
        self._page_next_btn = ttk.Button(page_bar, text="▶", width=3, command=self._next_list_page)
        self._page_next_btn.pack(side=tk.LEFT)

        # RACH summary + sequence navigation
        rach_frame = ttk.LabelFrame(left, text="RACH Msg1~4", padding=6)
        rach_frame.pack(fill=tk.X, pady=(8, 0))

        seq_nav = ttk.Frame(rach_frame)
        seq_nav.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(seq_nav, text="시퀀스:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        ttk.Button(seq_nav, text="◀", width=3, command=self._prev_sequence).pack(side=tk.LEFT, padx=2)
        self.seq_label = ttk.Label(seq_nav, text="0 / 0", width=18, anchor=tk.CENTER)
        self.seq_label.pack(side=tk.LEFT)
        ttk.Button(seq_nav, text="▶", width=3, command=self._next_sequence).pack(side=tk.LEFT, padx=2)

        self.rach_labels: dict[int, ttk.Label] = {}
        self.rach_jump_btns: dict[int, ttk.Button] = {}
        self.rach_status_banner = ttk.Label(
            rach_frame, text="", font=("Segoe UI", 9, "bold"), foreground="#dc3545",
        )
        self.rach_status_banner.pack(fill=tk.X, pady=(0, 4))

        issue_bar = ttk.Frame(rach_frame)
        issue_bar.pack(fill=tk.X, pady=(0, 6))
        self.orphan_msg2_label = ttk.Label(
            issue_bar,
            text="미포함 Msg2: —",
            font=("Segoe UI", 8),
            foreground="gray",
            cursor="hand2",
        )
        self.orphan_msg2_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.orphan_msg2_label.bind("<Button-1>", lambda _e: self._jump_next_orphan_msg2())
        ttk.Button(issue_bar, text="찾기", width=5, command=self._jump_next_orphan_msg2).pack(side=tk.RIGHT, padx=(4, 0))
        issue_bar2 = ttk.Frame(rach_frame)
        issue_bar2.pack(fill=tk.X, pady=(0, 6))
        self.seq_fail_msg2_label = ttk.Label(
            issue_bar2,
            text="시퀀스 Msg2실패: —",
            font=("Segoe UI", 8),
            foreground="gray",
            cursor="hand2",
        )
        self.seq_fail_msg2_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.seq_fail_msg2_label.bind("<Button-1>", lambda _e: self._jump_next_seq_fail_msg2())
        ttk.Button(issue_bar2, text="찾기", width=5, command=self._jump_next_seq_fail_msg2).pack(side=tk.RIGHT, padx=(4, 0))

        for msg in (1, 2, 3, 4):
            row = ttk.Frame(rach_frame)
            row.pack(fill=tk.X, pady=1)
            info = RACH_MSG_DEFS[msg]
            ttk.Label(row, text=info["name"], width=24).pack(side=tk.LEFT)
            lbl = ttk.Label(row, text="—", foreground="gray", cursor="hand2")
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            lbl.bind("<Button-1>", lambda _e, m=msg: self._jump_to_rach(m))
            self.rach_labels[msg] = lbl
            btn = ttk.Button(row, text="→", width=3, command=lambda m=msg: self._jump_to_rach(m))
            btn.pack(side=tk.RIGHT)
            btn.state(["disabled"])
            self.rach_jump_btns[msg] = btn

        # Right — mode views
        right = ttk.Frame(main)
        main.add(right, weight=3)

        self.view_container = ttk.Frame(right)
        self.view_container.pack(fill=tk.BOTH, expand=True)

        # Protocol tree + hex (단일 분할)
        self.tree_frame = ttk.Frame(self.view_container)
        ttk.Label(self.tree_frame, text="프로토콜 트리", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)

        tree_paned = ttk.PanedWindow(self.tree_frame, orient=tk.VERTICAL)
        tree_paned.pack(fill=tk.BOTH, expand=True, pady=4)

        proto_frame = ttk.Frame(tree_paned)
        tree_paned.add(proto_frame, weight=1)
        self.proto_tree = ttk.Treeview(proto_frame)
        psb = ttk.Scrollbar(proto_frame, orient=tk.VERTICAL, command=self.proto_tree.yview)
        self.proto_tree.configure(yscrollcommand=psb.set)
        self.proto_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        psb.pack(side=tk.RIGHT, fill=tk.Y)

        hex_outer = ttk.LabelFrame(tree_paned, text="데이터 필드", padding=4)
        tree_paned.add(hex_outer, weight=2)

        hex_toolbar = ttk.Frame(hex_outer)
        hex_toolbar.pack(fill=tk.X, pady=(0, 4))

        self._hex_tab_bar = ttk.Frame(hex_toolbar)
        self._hex_tab_bar.pack(side=tk.LEFT)
        self._hex_tab_hex = tk.Label(
            self._hex_tab_bar, text="  Hex  ", padx=8, pady=2,
            relief=tk.SOLID, borderwidth=1, bg="#ffffff",
        )
        self._hex_tab_hex.pack(side=tk.LEFT)
        self._hex_tab_crit = ttk.Frame(self._hex_tab_bar)
        self._hex_tab_crit_lbl = tk.Label(
            self._hex_tab_crit, text="  판단 조건  ", padx=8, pady=2,
            cursor="hand2",
        )
        self._hex_tab_crit_lbl.pack(side=tk.LEFT)
        self._hex_tab_crit_lbl.bind("<Button-1>", lambda _e: self._open_criteria_tab())
        self._hex_tab_crit_close = ttk.Button(
            self._hex_tab_crit, text="×", width=2,
            command=self._close_criteria_tab,
        )
        self._hex_tab_crit_close.pack(side=tk.LEFT)
        self._hex_tab_crit.pack_forget()

        self._open_criteria_btn = ttk.Button(
            hex_toolbar, text="판단 조건", width=10,
            command=self._open_criteria_tab,
        )
        self._open_criteria_btn.pack(side=tk.RIGHT)

        legend = ttk.Frame(hex_outer)
        legend.pack(fill=tk.X, pady=(0, 2))
        for txt, bg in (
            ("Msg1", ORAN_MSG_LABELS[1]["color"]),
            ("Msg2", ORAN_MSG_LABELS[2]["color"]),
            ("Msg3", ORAN_MSG_LABELS[3]["color"]),
            ("Msg4", ORAN_MSG_LABELS[4]["color"]),
            ("불충족", "#ffcdd2"),
            ("정보", "#bbdefb"),
        ):
            chip = tk.Label(legend, text=f" {txt} ", bg=bg, font=("Segoe UI", 8), padx=2)
            chip.pack(side=tk.LEFT, padx=2)

        self.criteria_panel = ttk.Frame(hex_outer)
        crit_cols = ("msg", "rule", "result")
        self.criteria_tree = ttk.Treeview(
            self.criteria_panel, columns=crit_cols, show="headings", height=5,
        )
        self.criteria_tree.heading("msg", text="Msg")
        self.criteria_tree.heading("rule", text="판정 조건")
        self.criteria_tree.heading("result", text="결과")
        self.criteria_tree.column("msg", width=44, stretch=False, anchor=tk.CENTER)
        self.criteria_tree.column("rule", width=420, stretch=True)
        self.criteria_tree.column("result", width=56, stretch=False, anchor=tk.CENTER)
        self.criteria_tree.pack(fill=tk.X)
        self.criteria_tree.tag_configure("pass", background="#d4edda")
        self.criteria_tree.tag_configure("fail", background="#f8d7da")
        self.criteria_tree.tag_configure("neutral", background="#f0f0f0")

        hex_box = ttk.Frame(hex_outer)
        self.hex_box = hex_box
        hex_box.pack(fill=tk.BOTH, expand=True)
        self.hex_text = tk.Text(hex_box, font=("Consolas", 9), wrap=tk.NONE, undo=False)
        hex_xsb = ttk.Scrollbar(hex_box, orient=tk.HORIZONTAL, command=self.hex_text.xview)
        hex_ysb = ttk.Scrollbar(hex_box, orient=tk.VERTICAL, command=self.hex_text.yview)
        self.hex_text.configure(xscrollcommand=hex_xsb.set, yscrollcommand=hex_ysb.set)
        self.hex_text.grid(row=0, column=0, sticky="nsew")
        hex_ysb.grid(row=0, column=1, sticky="ns")
        hex_xsb.grid(row=1, column=0, sticky="ew")
        hex_box.rowconfigure(0, weight=1)
        hex_box.columnconfigure(0, weight=1)
        self.hex_text.bind("<Key>", lambda _e: "break")

        # Ladder view
        self.ladder_frame = ttk.Frame(self.view_container)
        ttk.Label(self.ladder_frame, text="라더 다이어그램 (O-DU ↔ O-RU)", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        self.ladder_canvas = tk.Canvas(self.ladder_frame, bg="white", highlightthickness=1, highlightbackground="#ccc")
        self.ladder_canvas.pack(fill=tk.BOTH, expand=True, pady=4)
        self.ladder_canvas.bind("<Configure>", lambda e: self._draw_ladder())

        # C/U-plane view
        self.cu_frame = ttk.Frame(self.view_container)
        ttk.Label(self.cu_frame, text="C/U-Plane 연동 (Section ID Mapping)", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        self.cu_canvas = tk.Canvas(self.cu_frame, bg="white", highlightthickness=1, highlightbackground="#ccc")
        self.cu_canvas.pack(fill=tk.BOTH, expand=True, pady=4)
        self.cu_canvas.bind("<Configure>", lambda e: self._draw_cu_plane())

        # Bottom status bar
        status = ttk.Frame(self, padding=(12, 6))
        status.pack(fill=tk.X, side=tk.BOTTOM)

        nav = ttk.Frame(status)
        nav.pack(side=tk.LEFT)
        ttk.Button(nav, text="◀", width=3, command=self._prev_mode).pack(side=tk.LEFT)
        ttk.Button(nav, text="▶", width=3, command=self._next_mode).pack(side=tk.LEFT, padx=4)

        self.mode_label = ttk.Label(status, text="", font=("Segoe UI", 10))
        self.mode_label.pack(side=tk.LEFT, padx=12)

        ttk.Label(status, text="분석 로직 (Keysight Std)", foreground="gray").pack(side=tk.LEFT, padx=20)

        self.selected_label = ttk.Label(status, text="선택된 패킷: 없음")
        self.selected_label.pack(side=tk.RIGHT)

        self.load_status_label = ttk.Label(status, text="", foreground="#555555")
        self.load_status_label.pack(side=tk.RIGHT, padx=12)

        self._switch_mode(0)

    def _open_criteria_tab(self) -> None:
        self._criteria_tab_open = True
        self._hex_tab_crit.pack(side=tk.LEFT, padx=(4, 0))
        self._open_criteria_btn.pack_forget()
        self.criteria_panel.pack(fill=tk.X, pady=(0, 4), before=self.hex_box)
        self._hex_tab_crit_lbl.configure(bg="#e8f0fe", relief=tk.SOLID, borderwidth=1)
        if self.selected_packet:
            self._fill_criteria_tab(self.selected_packet)

    def _close_criteria_tab(self) -> None:
        self._criteria_tab_open = False
        self._hex_tab_crit.pack_forget()
        self.criteria_panel.pack_forget()
        self._open_criteria_btn.pack(side=tk.RIGHT)
        self._hex_tab_crit_lbl.configure(bg="#f0f0f0", relief=tk.FLAT, borderwidth=0)

    def _fill_criteria_tab(self, pkt: ParsedPacket | None) -> None:
        self.criteria_tree.delete(*self.criteria_tree.get_children())
        if pkt is not None:
            pkt._all_packets = self.packets  # type: ignore[attr-defined]
        for msg_num, rule, result, tag in get_criteria_tab_rows(pkt):
            label = f"Msg{msg_num}" if msg_num else "—"
            self.criteria_tree.insert("", tk.END, values=(label, rule, result), tags=(tag,))
        if pkt is not None and pkt.index in self._uplane_msg2_index_set:
            detail = self._uplane_msg2_reasons.get(pkt.index, "")
            self.criteria_tree.insert(
                "", tk.END,
                values=("—", f"Msg2 예상 U-Plane: {detail}", "—"),
                tags=("info",),
            )
        for label, rule, result, tag in get_advanced_criteria_rows(pkt, self.oran_version.get()):
            self.criteria_tree.insert("", tk.END, values=(label, rule, result), tags=(tag,))

    def _refresh_uplane_msg2_search(self) -> None:
        cands = find_dl_uplane_msg2_candidates(self.packets, self._uplane_msg2_max_rb)
        self._uplane_msg2_indices = [p.index for p, _ in cands]
        self._uplane_msg2_index_set = set(self._uplane_msg2_indices)
        self._uplane_msg2_reasons = {p.index: detail for p, detail in cands}
        self._uplane_msg2_nav = 0

    def _open_uplane_msg2_search(self) -> None:
        if not self.packets:
            messagebox.showwarning("알림", "먼저 PCAP 파일을 열어주세요.")
            return
        dlg = tk.Toplevel(self)
        dlg.title("Msg2 예상 U-Plane 검색")
        dlg.transient(self)
        dlg.resizable(False, False)
        ttk.Label(
            dlg,
            text="DL U-Plane(eCPRI 0x00) + Downlink\n"
            "Info 컬럼 PRB(헤더 numPRB) ≤ 입력값 — 압축 포함",
            padding=(16, 12),
        ).pack()
        row = ttk.Frame(dlg, padding=(16, 0))
        row.pack(fill=tk.X)
        ttk.Label(row, text="최대 PRB:").pack(side=tk.LEFT)
        spin = ttk.Spinbox(row, from_=1, to=273, width=6)
        spin.delete(0, tk.END)
        spin.insert(0, str(self._uplane_msg2_max_rb))
        spin.pack(side=tk.LEFT, padx=8)
        ttk.Label(row, text="(1 PRB ≈ 336B, IQ≤max×336)").pack(side=tk.LEFT)
        result_lbl = ttk.Label(dlg, text="", padding=(16, 8))
        result_lbl.pack()

        def _read_max_rb() -> int | None:
            try:
                return int(str(spin.get()).strip())
            except ValueError:
                return None

        def do_search() -> None:
            max_rb = _read_max_rb()
            if max_rb is None or max_rb < 1:
                messagebox.showwarning("입력 오류", "PRB는 1 이상이어야 합니다.", parent=dlg)
                return
            self._uplane_msg2_max_rb = max_rb
            self._refresh_uplane_msg2_search()
            n = len(self._uplane_msg2_indices)
            dl_u = sum(
                1
                for p in self.packets
                if p.is_ecpri and p.ecpri_type == 0x00 and p.is_uplink is False
            )
            result_lbl.configure(
                text=f"후보 {n:,}건 / DL U-Plane {dl_u:,}건 (헤더 PRB ≤ {max_rb})",
                foreground="#155724" if n and n < dl_u else ("#856404" if n else "#c62828"),
            )

        def apply_filter() -> None:
            do_search()
            if not self._uplane_msg2_indices:
                messagebox.showinfo(
                    "검색 결과",
                    format_uplane_msg2_search_summary([], self._uplane_msg2_max_rb),
                    parent=dlg,
                )
                return
            self.filter_rach_only.set(False)
            self.filter_orphan_msg2.set(False)
            self.filter_uplane_msg2.set(True)
            self._list_page = 0
            self._populate_packet_list()
            dlg.destroy()
            self.rach_status_banner.configure(
                text=f"Msg2 U-Plane 필터: ≤{self._uplane_msg2_max_rb} PRB, {len(self._uplane_msg2_indices):,}건",
                foreground="#1565c0",
            )

        btn_row = ttk.Frame(dlg, padding=(16, 8))
        btn_row.pack()
        ttk.Button(btn_row, text="검색", command=do_search).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="목록 필터 적용", command=apply_filter).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="닫기", command=dlg.destroy).pack(side=tk.LEFT, padx=4)
        do_search()

    def _jump_next_uplane_msg2(self) -> None:
        if not self._uplane_msg2_indices:
            self._open_uplane_msg2_search()
            return
        if not self.filter_uplane_msg2.get():
            self.filter_uplane_msg2.set(True)
            self.filter_rach_only.set(False)
            self.filter_orphan_msg2.set(False)
            self._on_uplane_msg2_filter_changed()
        idx = self._uplane_msg2_indices[self._uplane_msg2_nav % len(self._uplane_msg2_indices)]
        self._uplane_msg2_nav += 1
        reason = self._uplane_msg2_reasons.get(idx, "")
        pos = self._uplane_msg2_nav % len(self._uplane_msg2_indices)
        self.rach_status_banner.configure(
            text=f"Msg2 U-Plane #{pos}/{len(self._uplane_msg2_indices)} — {reason}",
            foreground="#1565c0",
        )
        self._select_packet_by_index(idx, flash=True, tag="msg2_uplane")

    def _on_uplane_msg2_filter_changed(self) -> None:
        if self.filter_uplane_msg2.get():
            self.filter_rach_only.set(False)
            self.filter_orphan_msg2.set(False)
            if not self._uplane_msg2_indices:
                self._refresh_uplane_msg2_search()
        self._list_page = 0
        self._populate_packet_list()

    def _open_advanced_analysis(self) -> None:
        if not self.packets:
            messagebox.showwarning("알림", "먼저 PCAP 파일을 열어주세요.")
            return
        AdvancedAnalysisDialog(self)

    def _apply_hex_highlights(self, pkt: ParsedPacket) -> None:
        for hl in compute_hex_highlights(pkt):
            self.hex_text.tag_configure(hl.tag, background=hl.bg)
            for start, end in _hex_index_range(hl.start, hl.end):
                self.hex_text.tag_add(hl.tag, start, end)

    @property
    def pcap_path(self) -> str | None:
        return self.pcap_paths[0] if self.pcap_paths else None

    def _format_load_paths(self, paths: list[str]) -> str:
        if not paths:
            return ""
        if len(paths) == 1:
            return paths[0]
        names = [os.path.basename(p) for p in paths]
        if len(names) <= 3:
            return f"{len(paths)}개 파일: " + ", ".join(names)
        return f"{len(paths)}개 파일: {names[0]}, {names[1]}, … (+{len(paths) - 2})"

    def _show_welcome(self) -> None:
        self.proto_tree.insert(
            "",
            tk.END,
            text="PCAP 파일을 열어 분석을 시작하세요. (Ctrl+클릭으로 여러 파일 선택 가능)",
            values=(),
        )
        self._fill_criteria_tab(None)

    def _set_loading_ui(self, loading: bool) -> None:
        self._loading = loading
        state = tk.DISABLED if loading else tk.NORMAL
        self._open_btn.configure(state=state)

    def _show_load_dialog(self, paths: list[str]) -> None:
        if self._load_dialog and self._load_dialog.winfo_exists():
            self._load_dialog.destroy()
        dlg = tk.Toplevel(self)
        dlg.title("PCAP 로드 중")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        total_mb = sum(os.path.getsize(p) for p in paths) / (1024 * 1024)
        if len(paths) == 1:
            head = f"로드 중: {os.path.basename(paths[0])}\n({total_mb:.1f} MB)"
        else:
            head = f"로드 중: {len(paths)}개 파일 (합계 {total_mb:.1f} MB)\n{self._format_load_paths(paths)}"
        self._load_title_label = ttk.Label(dlg, text=head, padding=(16, 12))
        self._load_title_label.pack()
        self._load_progress = ttk.Progressbar(dlg, mode="indeterminate", length=320)
        self._load_progress.pack(padx=16, pady=(0, 8))
        self._load_progress.start(12)
        self._load_phase_label = ttk.Label(dlg, text="패킷 메타데이터 읽는 중…", padding=(16, 0))
        self._load_phase_label.pack()
        self._load_count_label = ttk.Label(dlg, text="0 패킷", padding=(16, 4))
        self._load_count_label.pack()
        ttk.Button(dlg, text="취소", command=self._cancel_load).pack(pady=(8, 12))
        self._load_dialog = dlg

    def _close_load_dialog(self) -> None:
        if self._load_dialog and self._load_dialog.winfo_exists():
            if hasattr(self, "_load_progress"):
                self._load_progress.stop()
            self._load_dialog.grab_release()
            self._load_dialog.destroy()
        self._load_dialog = None

    def _cancel_load(self) -> None:
        self._load_cancel.set()
        self.load_status_label.configure(text="취소 중…")

    def _open_pcap(self) -> None:
        if self._loading:
            return
        paths = list(filedialog.askopenfilenames(
            title="PCAP 파일 선택 (여러 개 선택 가능)",
            filetypes=[
                ("PCAP files", "*.pcap *.pcapng *.cap"),
                ("All files", "*.*"),
            ],
        ))
        if not paths:
            return
        try:
            total_size = sum(os.path.getsize(p) for p in paths)
        except OSError as exc:
            messagebox.showerror("오류", f"파일 크기 확인 실패:\n{exc}")
            return
        if total_size >= LARGE_FILE_WARN_BYTES:
            size_gb = total_size / (1024 ** 3)
            file_note = f"{len(paths)}개 파일, 합계 {size_gb:.2f} GB"
            if not messagebox.askyesno(
                "대용량 파일",
                f"{file_note}\n\n"
                "백그라운드 스트리밍으로 로드합니다.\n"
                "목록은 페이지 단위로 표시됩니다.\n계속할까요?",
            ):
                return
        self._load_cancel.clear()
        while not self._load_queue.empty():
            try:
                self._load_queue.get_nowait()
            except queue.Empty:
                break
        self._set_loading_ui(True)
        self._show_load_dialog(paths)
        threading.Thread(
            target=self._load_worker,
            args=(paths,),
            daemon=True,
        ).start()
        self.after(100, self._poll_load_queue)

    def _load_worker(self, paths: list[str]) -> None:
        try:
            def progress_cb(count: int, phase: str, *extra) -> None:
                self._load_queue.put(("progress", count, phase, *extra))

            load_stats: dict[str, int] = {}
            packets = parse_pcaps(
                paths,
                progress_cb=progress_cb,
                cancel_event=self._load_cancel,
                load_stats=load_stats,
            )
            if self._load_cancel.is_set():
                raise InterruptedError("사용자가 로드를 취소했습니다.")
            self._load_queue.put(("progress", len(packets), "rach"))
            sequences = finalize_pcap_load(packets)
            if self._load_cancel.is_set():
                raise InterruptedError("사용자가 로드를 취소했습니다.")
            self._load_queue.put(("done", packets, sequences, paths, load_stats.get("ptp_skipped", 0)))
        except Exception as exc:
            self._load_queue.put(("error", exc))

    def _poll_load_queue(self) -> None:
        if not self._loading:
            return
        try:
            while True:
                msg = self._load_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    count, phase = msg[1], msg[2]
                    if self._load_dialog and self._load_dialog.winfo_exists():
                        if phase == "rach":
                            text = "RACH 분석 중…"
                        elif len(msg) >= 6 and msg[3] and phase == "parse":
                            file_i, file_n, fname = msg[3], msg[4], msg[5]
                            text = f"파일 {file_i}/{file_n}: {fname} 읽는 중…"
                        else:
                            text = "패킷 메타데이터 읽는 중…"
                        self._load_phase_label.configure(text=text)
                        self._load_count_label.configure(text=f"{count:,} 패킷")
                    self.load_status_label.configure(text=f"로드 중… {count:,} 패킷")
                elif kind == "done":
                    packets, sequences, paths = msg[1], msg[2], msg[3]
                    ptp_skipped = msg[4] if len(msg) > 4 else 0
                    self._on_pcap_loaded(packets, sequences, paths, ptp_skipped=ptp_skipped)
                    return
                elif kind == "error":
                    self._on_pcap_load_error(msg[1])
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_load_queue)

    def _on_pcap_loaded(
        self,
        packets: list[ParsedPacket],
        sequences: list[RachSequence],
        paths: list[str],
        *,
        ptp_skipped: int = 0,
    ) -> None:
        self._close_load_dialog()
        self._set_loading_ui(False)
        if self._materialized_index is not None and 0 <= self._materialized_index < len(self.packets):
            dematerialize_packet(self.packets[self._materialized_index])
        self.packets = packets
        self.pcap_paths = list(paths)
        self.du_ip, self.ru_ip = infer_du_ru_ips(packets)
        self.rach_sequences = sequences
        self._refresh_msg2_issues()
        self._refresh_uplane_msg2_search()
        self._materialized_index = None
        self.selected_packet = None
        self._list_page = 0
        failed = [s for s in self.rach_sequences if s.is_failed]
        self.current_sequence_idx = self.rach_sequences.index(failed[0]) if failed else 0
        self._update_seq_label()
        self._populate_packet_list()
        self._update_rach_panel()
        self._switch_mode(self.mode_index)
        self.proto_tree.delete(*self.proto_tree.get_children())
        self.hex_text.delete("1.0", tk.END)
        fail_n = sum(1 for s in self.rach_sequences if s.is_failed)
        ok_n = sum(1 for s in self.rach_sequences if s.is_complete)
        summary = f"{len(self.packets):,}개 패킷"
        if ptp_skipped:
            summary += f" | PTP(0x88F7) {ptp_skipped:,}건 제외"
        if len(paths) > 1:
            summary += f" | {len(paths)}개 PCAP 병합"
        if self.du_ip and self.ru_ip:
            summary += f" | DU={self.du_ip} RU={self.ru_ip}"
        elif self.du_ip:
            summary += f" | DU={self.du_ip}"
        if fail_n:
            summary += f" | RACH 실패 {fail_n}건"
        if ok_n:
            summary += f" | 성공 {ok_n}건"
        self.load_status_label.configure(text=summary)
        path_note = self._format_load_paths(paths)
        if fail_n and len(self.packets) < 200_000:
            messagebox.showwarning("PCAP 로드", f"{summary}\n{path_note}")
        elif len(self.packets) < 200_000:
            messagebox.showinfo("완료", f"{summary}\n{path_note}")

    def _on_pcap_load_error(self, exc: Exception) -> None:
        self._close_load_dialog()
        self._set_loading_ui(False)
        self.load_status_label.configure(text="")
        if isinstance(exc, InterruptedError):
            messagebox.showinfo("취소", str(exc))
        else:
            messagebox.showerror("오류", f"PCAP 로드 실패:\n{exc}")

    def _refresh_msg2_issues(self) -> None:
        report = collect_msg2_sequence_issues(self.packets, self.rach_sequences)
        self._orphan_msg2_indices = [p.index for p in report.orphan_msg2]
        self._orphan_msg2_index_set = set(self._orphan_msg2_indices)
        self._orphan_msg2_reasons = report.reasons
        self._seq_fail_msg2 = report.seq_fail_msg2
        self._orphan_msg2_nav = 0
        self._seq_fail_msg2_nav = 0
        self._msg2_gap_report = analyze_msg2_gap(self.packets, self.rach_sequences)
        self._hidden_msg2_indices = [p.index for p, _ in self._msg2_gap_report.hidden_candidates]
        self._hidden_msg2_reasons = {
            p.index: why for p, why in self._msg2_gap_report.hidden_candidates
        }
        self._hidden_msg2_nav = 0
        self._update_msg2_issue_bar()

    def _update_msg2_issue_bar(self) -> None:
        n_orphan = len(self._orphan_msg2_indices)
        n_fail = len(self._seq_fail_msg2)
        gap = self._msg2_gap_report
        n_hidden = len(self._hidden_msg2_indices) if gap else 0
        if hasattr(self, "orphan_msg2_label"):
            self.orphan_msg2_label.configure(
                text=f"미포함 Msg2: {n_orphan:,}건 — Msg2 타입이나 시퀀스 미태깅",
                foreground="#dc3545" if n_orphan else "gray",
            )
        if hasattr(self, "seq_fail_msg2_label"):
            extra = ""
            if gap and gap.strict_msg2 == 0 and (gap.rt_st6_dl > 0 or n_hidden > 0):
                extra = f" | 거의 Msg2 {n_hidden:,}건"
            elif gap and gap.strict_msg2 == 0 and gap.seq_fail_msg2 > 0:
                extra = " | GUI Msg2 0건"
            self.seq_fail_msg2_label.configure(
                text=f"시퀀스 Msg2실패: {n_fail:,}건 — 슬롯에 Msg1 있으나 Msg2 없음{extra}",
                foreground="#856404" if n_fail else "gray",
            )

    def _show_msg2_gap_dialog(self) -> None:
        if not self.packets:
            messagebox.showwarning("알림", "먼저 PCAP 파일을 열어주세요.")
            return
        if self._msg2_gap_report is None:
            self._msg2_gap_report = analyze_msg2_gap(self.packets, self.rach_sequences)
        messagebox.showinfo("Msg2 가시성 진단", format_msg2_gap_report(self._msg2_gap_report))

    def _jump_next_hidden_msg2(self) -> None:
        if not self._hidden_msg2_indices:
            self._show_msg2_gap_dialog()
            return
        idx = self._hidden_msg2_indices[self._hidden_msg2_nav % len(self._hidden_msg2_indices)]
        self._hidden_msg2_nav += 1
        reason = self._hidden_msg2_reasons.get(idx, "")
        pos = self._hidden_msg2_nav % len(self._hidden_msg2_indices)
        self.rach_status_banner.configure(
            text=f"거의 Msg2 후보 #{pos}/{len(self._hidden_msg2_indices)} — {reason}",
            foreground="#e65100",
        )
        if self.filter_orphan_msg2.get():
            self.filter_orphan_msg2.set(False)
            self._on_orphan_msg2_filter_changed()
        self._select_packet_by_index(idx, flash=True, tag="msg2_orphan")

    def _rebuild_list_indices(self) -> None:
        if self.filter_orphan_msg2.get():
            self._list_indices = list(self._orphan_msg2_indices)
        elif self.filter_uplane_msg2.get():
            self._list_indices = list(self._uplane_msg2_indices)
        elif self.filter_rach_only.get():
            self._list_indices = [p.index for p in self.packets if p.rach_msg]
        else:
            self._list_indices = list(range(len(self.packets)))
        self._use_pagination = len(self._list_indices) > LIST_PAGE_SIZE
        if not self._use_pagination:
            self._list_page = 0
        else:
            max_page = max(0, (len(self._list_indices) - 1) // LIST_PAGE_SIZE)
            self._list_page = min(self._list_page, max_page)

    def _update_page_label(self) -> None:
        total = len(self._list_indices)
        if total == 0:
            self.page_label.configure(text="패킷 없음")
            self._page_prev_btn.state(["disabled"])
            self._page_next_btn.state(["disabled"])
            return
        if not self._use_pagination:
            self.page_label.configure(text=f"전체 {total:,}개")
            self._page_prev_btn.state(["disabled"])
            self._page_next_btn.state(["disabled"])
            return
        pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
        start = self._list_page * LIST_PAGE_SIZE
        end = min(start + LIST_PAGE_SIZE, total)
        self.page_label.configure(
            text=f"페이지 {self._list_page + 1}/{pages}  (#{start + 1:,}–#{end:,} / {total:,})",
        )
        self._page_prev_btn.state([] if self._list_page > 0 else ["disabled"])
        self._page_next_btn.state([] if self._list_page < pages - 1 else ["disabled"])

    def _prev_list_page(self) -> None:
        if self._list_page > 0:
            self._list_page -= 1
            self._populate_packet_list()

    def _next_list_page(self) -> None:
        pages = max(1, (len(self._list_indices) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
        if self._list_page < pages - 1:
            self._list_page += 1
            self._populate_packet_list()

    def _cancel_populate_job(self) -> None:
        if self._populate_job:
            self.after_cancel(self._populate_job)
            self._populate_job = None

    def _col_anchor(self, col: str) -> str:
        anc = PACKET_LIST_COL_DEFS[col][2]
        return tk.CENTER if anc == "center" else tk.W

    def _setup_packet_tree_columns(self) -> None:
        self.packet_tree["displaycolumns"] = PACKET_LIST_COLUMNS
        for col in PACKET_LIST_COLUMNS:
            title, _w, _a = PACKET_LIST_COL_DEFS[col]
            self.packet_tree.heading(col, text=title, anchor=tk.CENTER)
        self._apply_packet_column_layout()

    def _apply_packet_column_layout(self) -> None:
        order = tuple(self.packet_tree["displaycolumns"])
        last = order[-1] if order else ""
        for col in PACKET_LIST_COLUMNS:
            w = max(24, self._packet_col_widths.get(col, PACKET_LIST_COL_DEFS[col][1]))
            self.packet_tree.column(
                col,
                width=w,
                minwidth=24,
                stretch=(col == last),
                anchor=self._col_anchor(col),
            )

    def _save_packet_column_widths(self) -> None:
        for col in PACKET_LIST_COLUMNS:
            try:
                self._packet_col_widths[col] = int(self.packet_tree.column(col, "width"))
            except tk.TclError:
                pass

    def _display_column_at(self, event: tk.Event) -> str | None:
        col_id = self.packet_tree.identify_column(event.x)
        if not col_id or col_id == "#0":
            return None
        idx = int(col_id.lstrip("#")) - 1
        display = list(self.packet_tree["displaycolumns"])
        if 0 <= idx < len(display):
            return display[idx]
        return None

    def _bind_packet_tree_column_controls(self) -> None:
        tree = self.packet_tree
        tree.bind("<ButtonPress-1>", self._on_packet_col_press, add=True)
        tree.bind("<ButtonRelease-1>", self._on_packet_col_release, add=True)
        tree.bind("<Button-3>", self._on_packet_col_menu)

    def _on_packet_col_press(self, event: tk.Event) -> None:
        if self.packet_tree.identify_region(event.x, event.y) == "heading":
            self._col_drag_src = self._display_column_at(event)

    def _on_packet_col_release(self, event: tk.Event) -> None:
        region = self.packet_tree.identify_region(event.x, event.y)
        if region == "heading" and self._col_drag_src:
            target = self._display_column_at(event)
            if target and target != self._col_drag_src:
                self._reorder_packet_column(self._col_drag_src, target)
        self._col_drag_src = None
        if region in ("heading", "separator"):
            self._save_packet_column_widths()
            self._apply_packet_column_layout()

    def _reorder_packet_column(self, src: str, target: str) -> None:
        order = list(self.packet_tree["displaycolumns"])
        if src not in order or target not in order:
            return
        order.remove(src)
        order.insert(order.index(target), src)
        self.packet_tree["displaycolumns"] = order
        self._apply_packet_column_layout()

    def _move_packet_column(self, col: str, direction: int) -> None:
        order = list(self.packet_tree["displaycolumns"])
        if col not in order:
            return
        i = order.index(col)
        j = i + direction
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            self.packet_tree["displaycolumns"] = order
            self._apply_packet_column_layout()

    def _reset_packet_columns(self) -> None:
        self._packet_col_widths = {c: PACKET_LIST_COL_DEFS[c][1] for c in PACKET_LIST_COLUMNS}
        self.packet_tree["displaycolumns"] = PACKET_LIST_COLUMNS
        self._setup_packet_tree_columns()

    def _on_packet_col_menu(self, event: tk.Event) -> None:
        if self.packet_tree.identify_region(event.x, event.y) != "heading":
            return
        col = self._display_column_at(event)
        if not col:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="◀ 왼쪽으로", command=lambda: self._move_packet_column(col, -1))
        menu.add_command(label="오른쪽으로 ▶", command=lambda: self._move_packet_column(col, 1))
        menu.add_separator()
        menu.add_command(label="컬럼 초기화", command=self._reset_packet_columns)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _packet_row_values(self, p: ParsedPacket) -> tuple[Any, ...]:
        t = datetime.fromtimestamp(p.timestamp).strftime("%H:%M:%S.%f")[:-3] if p.timestamp else ""
        rach_col = ""
        if p.rach_msg:
            rach_col = f"Msg{p.rach_msg}"
            if p.rach_source == "sequence":
                rach_col += "?"
            if p.rach_sequence_id is not None:
                rach_col += f"/S{p.rach_sequence_id + 1}"
        elif p.index in self._orphan_msg2_index_set:
            rach_col = "Msg2≠"
        elif p.index in self._uplane_msg2_index_set:
            detail = self._uplane_msg2_reasons.get(p.index, "")
            rb_part = detail.split("PRB")[0].strip() if detail else ""
            rach_col = f"M2-U{rb_part}" if rb_part else "M2-U"
        sym = f"0x{p.symbol_id:02x}" if p.symbol_id is not None else ""
        by_col = {
            "no": p.index + 1,
            "source": os.path.basename(p.pcap_source) if p.pcap_source else "",
            "rach": rach_col,
            "eaxc": p.eaxc_id or "",
            "length": p.length,
            "direction": p.direction or (
                "Uplink" if p.is_uplink else ("Downlink" if p.is_uplink is False else "")
            ),
            "flow": p.flow or "",
            "frame": p.frame_id if p.frame_id is not None else "",
            "subframe": p.subframe_id if p.subframe_id is not None else "",
            "slot": p.slot_id if p.slot_id is not None else "",
            "symbol": sym,
            "info": p.info_text or "",
            "time": t,
        }
        return tuple(by_col[c] for c in PACKET_LIST_COLUMNS)

    def _insert_packet_row(self, p: ParsedPacket) -> None:
        tags: tuple[str, ...] = ()
        if p.index in self._orphan_msg2_index_set:
            tags = ("msg2_orphan",)
        elif p.index in self._uplane_msg2_index_set:
            tags = ("msg2_uplane",)
        elif p.rach_msg:
            tag = f"msg{p.rach_msg}"
            if p.rach_source == "sequence":
                tag += "_guess"
            tags = (tag,)
        self.packet_tree.insert(
            "", tk.END,
            iid=str(p.index),
            values=self._packet_row_values(p),
            tags=tags,
        )

    def _populate_packet_list(self) -> None:
        self._cancel_populate_job()
        self._rebuild_list_indices()
        self.packet_tree.delete(*self.packet_tree.get_children())
        self._update_page_label()
        if not self._list_indices:
            return
        if self._use_pagination:
            start = self._list_page * LIST_PAGE_SIZE
            end = min(start + LIST_PAGE_SIZE, len(self._list_indices))
            indices = self._list_indices[start:end]
        else:
            indices = self._list_indices
        self._populate_indices_batch(indices, 0)

    def _populate_indices_batch(self, indices: list[int], offset: int) -> None:
        end = min(offset + LIST_INSERT_BATCH, len(indices))
        for idx in indices[offset:end]:
            self._insert_packet_row(self.packets[idx])
        if end < len(indices):
            self._populate_job = self.after(1, lambda: self._populate_indices_batch(indices, end))
        else:
            self._populate_job = None
            for msg in (1, 2, 3, 4):
                self.packet_tree.tag_configure(f"msg{msg}", background=RACH_MSG_DEFS[msg]["color"])
                self.packet_tree.tag_configure(
                    f"msg{msg}_guess",
                    background=RACH_MSG_DEFS[msg]["color"],
                    foreground="#666666",
                )
            self.packet_tree.tag_configure("msg2_orphan", background="#ffb74d", foreground="#4a2600")
            self.packet_tree.tag_configure("msg2_uplane", background="#90caf9", foreground="#0d47a1")

    def _ensure_packet_visible(self, index: int) -> None:
        if not self._use_pagination:
            return
        try:
            pos = self._list_indices.index(index)
        except ValueError:
            return
        page = pos // LIST_PAGE_SIZE
        if page != self._list_page:
            self._list_page = page
            self._populate_packet_list()

    def _ensure_packet_materialized(self, pkt: ParsedPacket) -> None:
        src_path = pkt.pcap_source or self.pcap_path
        if not src_path:
            return
        if self._materialized_index is not None and self._materialized_index != pkt.index:
            if 0 <= self._materialized_index < len(self.packets):
                dematerialize_packet(self.packets[self._materialized_index])
        materialize_packet(pkt, src_path)
        self._materialized_index = pkt.index

    def _on_rach_filter_changed(self) -> None:
        if self.filter_rach_only.get():
            self.filter_orphan_msg2.set(False)
            self.filter_uplane_msg2.set(False)
        self._list_page = 0
        self._populate_packet_list()

    def _on_orphan_msg2_filter_changed(self) -> None:
        if self.filter_orphan_msg2.get():
            self.filter_rach_only.set(False)
            self.filter_uplane_msg2.set(False)
        self._list_page = 0
        self._populate_packet_list()

    def _on_filter_changed(self) -> None:
        self._on_rach_filter_changed()

    def _refresh_sequences(self) -> None:
        self.rach_sequences = finalize_pcap_load(self.packets)
        self._refresh_msg2_issues()
        failed = [s for s in self.rach_sequences if s.is_failed]
        if failed:
            self.current_sequence_idx = self.rach_sequences.index(failed[0])
        else:
            self.current_sequence_idx = 0
        self._update_seq_label()

    def _sequence_pool(self) -> list[RachSequence]:
        return self.rach_sequences

    def _update_seq_label(self) -> None:
        pool = self._sequence_pool()
        if not pool:
            self.seq_label.configure(text="없음")
            return
        idx = min(self.current_sequence_idx, len(pool) - 1)
        self.current_sequence_idx = idx
        seq = pool[idx]
        self.seq_label.configure(text=f"{idx + 1}/{len(pool)} ({seq.status_text})")

    def _current_sequence(self) -> RachSequence | None:
        pool = self._sequence_pool()
        if not pool or self.current_sequence_idx >= len(pool):
            return None
        return pool[self.current_sequence_idx]

    def _current_seq_id(self) -> int | None:
        seq = self._current_sequence()
        return seq.seq_id if seq else None

    def _current_rach_map(self) -> dict[int, ParsedPacket]:
        seq = self._current_sequence()
        if seq:
            return {k: v for k, v in seq.msgs.items()}
        return get_rach_packets(self.packets)

    def _prev_sequence(self) -> None:
        pool = self._sequence_pool()
        if len(pool) <= 1:
            return
        self.current_sequence_idx = (self.current_sequence_idx - 1) % len(pool)
        self._update_seq_label()
        self._update_rach_panel()
        self._draw_ladder()

    def _next_sequence(self) -> None:
        pool = self._sequence_pool()
        if len(pool) <= 1:
            return
        self.current_sequence_idx = (self.current_sequence_idx + 1) % len(pool)
        self._update_seq_label()
        self._update_rach_panel()
        self._draw_ladder()

    def _rach_source_label(self, p: ParsedPacket) -> str:
        if p.rach_source == "oran":
            return "O-RAN"
        if p.rach_source == "sequence":
            return "추정"
        if p.rach_source in ("keyword", "section"):
            return "확실"
        return ""

    def _update_rach_panel(self) -> None:
        self._rach = self._current_rach_map()
        seq = self._current_sequence()

        if seq and seq.is_failed:
            self.rach_status_banner.configure(
                text=f"⚠ O-RAN 실패 — {seq.status_text}  (Msg2 없이 Msg3/4 불가)",
                foreground="#dc3545",
            )
        elif seq and seq.is_complete:
            self.rach_status_banner.configure(
                text="✓ O-RAN MSG1~4 완료",
                foreground="#155724",
            )
        else:
            self.rach_status_banner.configure(text="")

        for msg in (1, 2, 3, 4):
            if msg in self._rach:
                p = self._rach[msg]
                src_lbl = self._rach_source_label(p)
                extra = f" [{src_lbl}]" if src_lbl else ""
                seq_hint = ""
                if p.rach_sequence_id is not None and len(self.rach_sequences) > 1:
                    seq_hint = f" S{p.rach_sequence_id + 1}"
                self.rach_labels[msg].configure(
                    text=f"패킷 #{p.index + 1}{seq_hint}  {p.flow or '?'}  {p.src}→{p.dst}{extra}  [클릭하여 이동]",
                    foreground="#155724" if src_lbl != "추정" else "#856404",
                )
                self.rach_jump_btns[msg].state(["!disabled"])
            elif seq and seq.failed_at_msg == msg:
                self.rach_labels[msg].configure(
                    text="FAIL — 미수신 (RACH 중단)",
                    foreground="#dc3545",
                )
                self.rach_jump_btns[msg].state(["disabled"])
            elif seq and seq.is_failed and msg > (seq.failed_at_msg or 99):
                self.rach_labels[msg].configure(
                    text="— (도달하지 못함)",
                    foreground="#999999",
                )
                self.rach_jump_btns[msg].state(["disabled"])
            else:
                self.rach_labels[msg].configure(text="미탐지", foreground="#856404")
                self.rach_jump_btns[msg].state(["disabled"])

    def _scan_rach(self) -> None:
        if not self.packets:
            messagebox.showwarning("알림", "먼저 PCAP 파일을 열어주세요.")
            return

        self.rach_sequences = finalize_pcap_load(self.packets)
        self._refresh_msg2_issues()
        failed = [s for s in self.rach_sequences if s.is_failed]
        if failed:
            self.current_sequence_idx = self.rach_sequences.index(failed[0])
        else:
            self.current_sequence_idx = 0
        self._update_seq_label()
        self._populate_packet_list()
        self._update_rach_panel()
        self._draw_ladder()
        self._draw_cu_plane()

        ok = sum(1 for s in self.rach_sequences if s.is_complete)
        fail = sum(1 for s in self.rach_sequences if s.is_failed)
        lines = [f"성공 {ok}건 / 실패 {fail}건 / 전체 {len(self.rach_sequences)}건", ""]
        pool = self.rach_sequences
        for si, seq in enumerate(pool[:10]):
            lines.append(f"--- 시퀀스 {si + 1} [{seq.status_text}] ---")
            for msg in (1, 2, 3, 4):
                if msg in seq.msgs:
                    p = seq.msgs[msg]
                    lines.append(f"  Msg{msg}: 패킷 #{p.index + 1} ({self._rach_source_label(p) or '?'})")
                elif seq.failed_at_msg == msg:
                    lines.append(f"  Msg{msg}: *** FAIL — 미수신 ***")
                else:
                    lines.append(f"  Msg{msg}: —")
        if len(pool) > 10:
            lines.append(f"... 외 {len(pool) - 10}건")
        if fail:
            messagebox.showwarning("RACH 스캔 결과", "\n".join(lines))
        else:
            messagebox.showinfo("RACH 스캔 결과", "\n".join(lines))

    def _on_packet_select(self, _event: tk.Event | None = None) -> None:
        sel = self.packet_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.packets):
            self.selected_packet = self.packets[idx]
            self.selected_label.configure(text=f"선택된 패킷: #{idx + 1}")
            self._fill_proto_tree(self.selected_packet)
            self._show_hex(self.selected_packet)
            self._draw_ladder()
            self._draw_cu_plane()

    def _fill_proto_tree(self, pkt: ParsedPacket) -> None:
        self.proto_tree.delete(*self.proto_tree.get_children())
        try:
            self._ensure_packet_materialized(pkt)
        except Exception as exc:
            self.proto_tree.insert("", tk.END, text=f"패킷 로드 실패: {exc}", values=())
            return
        if pkt.tree is None:
            self.proto_tree.insert("", tk.END, text="(프로토콜 트리 없음)", values=())
            return
        self._insert_tree_node("", pkt.tree)

    def _show_hex(self, pkt: ParsedPacket) -> None:
        if self._criteria_tab_open:
            self._fill_criteria_tab(pkt)
        self.hex_text.delete("1.0", tk.END)
        try:
            self._ensure_packet_materialized(pkt)
            self.hex_text.insert("1.0", pkt.raw_hex)
            self._apply_hex_highlights(pkt)
        except Exception as exc:
            self.hex_text.insert("1.0", f"패킷 로드 실패:\n{exc}")

    def _insert_tree_node(self, parent: str, node: TreeNode) -> str:
        text = node.label if not node.value else f"{node.label}: {node.value}"
        iid = self.proto_tree.insert(parent, tk.END, text=text, open=True)
        for child in node.children:
            self._insert_tree_node(iid, child)
        return iid

    def _prev_mode(self) -> None:
        self._switch_mode((self.mode_index - 1) % len(ANALYSIS_MODES))

    def _next_mode(self) -> None:
        self._switch_mode((self.mode_index + 1) % len(ANALYSIS_MODES))

    def _switch_mode(self, index: int) -> None:
        self.mode_index = index
        for f in (self.tree_frame, self.ladder_frame, self.cu_frame):
            f.pack_forget()
        _, label = ANALYSIS_MODES[index]
        self.mode_label.configure(text=f"현재 분석 모드: {label}")
        frames = [self.tree_frame, self.ladder_frame, self.cu_frame]
        frames[index].pack(fill=tk.BOTH, expand=True)
        if index == 1:
            self._draw_ladder()
        elif index == 2:
            self._draw_cu_plane()

    def _draw_ladder(self) -> None:
        c = self.ladder_canvas
        c.delete("all")
        w = c.winfo_width() or 600
        h = c.winfo_height() or 400
        margin = 80
        left_x, right_x = margin, w - margin
        c.create_text(w // 2, 20, text="Random Access Procedure (RACH)", font=("Segoe UI", 12, "bold"))

        # Entity lines
        for x, name in ((left_x, "O-DU"), (right_x, "O-RU")):
            c.create_line(x, 50, x, h - 40, fill="#333", width=2)
            c.create_text(x, 38, text=name, font=("Segoe UI", 11, "bold"))

        self._rach = self._current_rach_map()
        seq = self._current_sequence()
        y_start = 90
        y_step = max(70, (h - 130) // 5)

        for i, msg in enumerate((1, 2, 3, 4)):
            info = RACH_MSG_DEFS[msg]
            y = y_start + i * y_step
            pkt = self._rach.get(msg)
            is_failed = seq and seq.failed_at_msg == msg
            is_selected = pkt and self.selected_packet and pkt.index == self.selected_packet.index

            if is_failed:
                color = "#dc3545"
            elif pkt:
                color = "#28a745"
            else:
                color = "#ccc"
            if is_selected:
                color = "#007bff"

            if "O-RU →" in info["direction"]:
                x1, x2 = right_x, left_x
            else:
                x1, x2 = left_x, right_x

            if is_failed:
                c.create_line(x1, y, x2, y, fill=color, width=2, dash=(6, 4))
                c.create_text((x1 + x2) // 2, y - 14, text=f"{info['short']} — FAIL", font=("Segoe UI", 10, "bold"), fill=color)
                c.create_text((x1 + x2) // 2, y + 14, text="미수신", font=("Segoe UI", 9), fill=color)
            else:
                c.create_line(x1, y, x2, y, fill=color, width=2, arrow=tk.LAST)
                c.create_text((x1 + x2) // 2, y - 14, text=info["short"], font=("Segoe UI", 10, "bold"))
                sub = f"패킷 #{pkt.index + 1} ({self._rach_source_label(pkt)})" if pkt else "미탐지"
                c.create_text((x1 + x2) // 2, y + 14, text=sub, font=("Segoe UI", 9), fill="#666")

            if pkt:
                tag = f"rach_{msg}"
                c.create_rectangle(x1 - 5, y - 5, x2 + 5, y + 5, outline="", fill="", tags=(tag,))
                c.tag_bind(tag, "<Button-1>", lambda e, idx=pkt.index: self._select_packet_by_index(idx))

    def _jump_next_orphan_msg2(self) -> None:
        if not self._orphan_msg2_indices:
            messagebox.showinfo(
                "Msg2 미포함",
                "Msg2 타입으로 식별되었으나 시퀀스에 태깅되지 않은 패킷이 없습니다.",
            )
            return
        if not self.filter_orphan_msg2.get():
            self.filter_orphan_msg2.set(True)
            self.filter_rach_only.set(False)
            self._on_orphan_msg2_filter_changed()
        idx = self._orphan_msg2_indices[self._orphan_msg2_nav % len(self._orphan_msg2_indices)]
        self._orphan_msg2_nav += 1
        reason = self._orphan_msg2_reasons.get(idx, "")
        pos = self._orphan_msg2_nav % len(self._orphan_msg2_indices)
        self.rach_status_banner.configure(
            text=f"미포함 Msg2 #{pos}/{len(self._orphan_msg2_indices)} — {reason}",
            foreground="#dc3545",
        )
        self._select_packet_by_index(idx, flash=True, tag="msg2_orphan")

    def _jump_next_seq_fail_msg2(self) -> None:
        if not self._seq_fail_msg2:
            messagebox.showinfo(
                "시퀀스 Msg2 실패",
                "Msg1은 있으나 같은 슬롯에 Msg2가 없어 시퀀스가 끊긴 경우가 없습니다.",
            )
            return
        seq = self._seq_fail_msg2[self._seq_fail_msg2_nav % len(self._seq_fail_msg2)]
        self._seq_fail_msg2_nav += 1
        if seq in self.rach_sequences:
            self.current_sequence_idx = self.rach_sequences.index(seq)
            self._update_seq_label()
            self._update_rach_panel()
        pos = self._seq_fail_msg2_nav % len(self._seq_fail_msg2)
        if 1 in seq.msgs:
            p = seq.msgs[1]
            tk = timing_key(p)
            slot = f"F{tk[0]}/SF{tk[1]}/Slot{tk[2]}" if tk else "?"
            self.rach_status_banner.configure(
                text=f"시퀀스 Msg2실패 #{pos}/{len(self._seq_fail_msg2)} — {slot}에 Msg1만 있음",
                foreground="#856404",
            )
            if self.filter_orphan_msg2.get():
                self.filter_orphan_msg2.set(False)
                self._on_orphan_msg2_filter_changed()
            self._select_packet_by_index(p.index, flash=True, tag="msg1")
        else:
            messagebox.showinfo("시퀀스 Msg2 실패", f"시퀀스 #{pos}: Msg1 패킷 없음")

    def _jump_to_rach(self, msg: int) -> None:
        self._rach = self._current_rach_map()
        pkt = self._rach.get(msg)
        if not pkt:
            messagebox.showinfo("RACH", f"Msg{msg} 패킷이 아직 탐지되지 않았습니다.\n'RACH 메시지 스캔'을 실행해 보세요.")
            return
        if self.filter_rach_only.get():
            self.filter_rach_only.set(False)
            self._on_filter_changed()
        self._select_packet_by_index(pkt.index, flash=True)

    def _select_packet_by_index(self, index: int, flash: bool = False, tag: str | None = None) -> None:
        self._ensure_packet_visible(index)
        iid = str(index)
        if not self.packet_tree.exists(iid):
            return
        self.packet_tree.selection_set(iid)
        self.packet_tree.focus(iid)
        self.packet_tree.see(iid)
        self._on_packet_select()
        if flash and tag:
            highlight = "#ff6b6b"
            if tag == "msg2_orphan":
                normal = "#ffb74d"
            elif tag == "msg2_uplane":
                normal = "#90caf9"
            elif tag == "msg1":
                normal = RACH_MSG_DEFS[1]["color"]
            else:
                normal = RACH_MSG_DEFS.get(2, {}).get("color", "#a8e6a3")
            self.packet_tree.tag_configure(tag, background=highlight)
            self.after(400, lambda t=tag, n=normal: self.packet_tree.tag_configure(t, background=n))
        elif flash and self.packets[index].rach_msg:
            msg = self.packets[index].rach_msg
            tag = f"msg{msg}"
            highlight = "#ff6b6b"
            normal = RACH_MSG_DEFS[msg]["color"]
            self.packet_tree.tag_configure(tag, background=highlight)
            self.after(400, lambda: self.packet_tree.tag_configure(tag, background=normal))

    def _draw_cu_plane(self) -> None:
        c = self.cu_canvas
        c.delete("all")
        w = c.winfo_width() or 600
        h = c.winfo_height() or 400

        ctrl_x, ctrl_y = w * 0.25, h * 0.35
        user_x, user_y = w * 0.65, h * 0.65
        box_w, box_h = 160, 70

        def round_box(x, y, text, fill):
            c.create_rectangle(x, y, x + box_w, y + box_h, fill=fill, outline="#666", width=1)
            c.create_text(x + box_w // 2, y + box_h // 2, text=text, font=("Segoe UI", 10, "bold"))

        round_box(ctrl_x, ctrl_y, "Control Plane\n(Type 1 / eCPRI RT)", "#d4edda")
        round_box(user_x, user_y, "User Plane\n(Data / IQ)", "#fff3cd")

        # Section ID links from control packets
        control_pkts = [p for p in self.packets if p.plane == "control" and p.section_type is not None]
        user_pkts = [p for p in self.packets if p.plane == "user" and p.section_type is not None]

        section_map: dict[int, list[ParsedPacket]] = {}
        for p in control_pkts:
            sid = p.section_type
            if sid is not None:
                section_map.setdefault(sid, []).append(p)

        c.create_line(
            ctrl_x + box_w, ctrl_y + box_h // 2,
            user_x, user_y + box_h // 2,
            dash=(6, 4), fill="#666", width=1,
        )
        c.create_text((ctrl_x + box_w + user_x) // 2, (ctrl_y + user_y) // 2 - 12,
                      text="Section ID Mapping", font=("Segoe UI", 10, "italic"))

        # List mappings
        y_list = 30
        c.create_text(20, y_list, anchor=tk.W, text="Section Type → 패킷:", font=("Segoe UI", 9, "bold"))
        y_list += 20
        shown = 0
        for sid, pkts in sorted(section_map.items()):
            if shown >= 8:
                break
            names = ", ".join(f"#{p.index + 1}" for p in pkts[:3])
            c.create_text(20, y_list, anchor=tk.W, text=f"  Type {sid}: {names}", font=("Consolas", 9))
            y_list += 16
            shown += 1

        if self.selected_packet and self.selected_packet.section_type is not None:
            c.create_text(
                w // 2, h - 30,
                text=f"선택 패킷 Section Type: {self.selected_packet.section_type}",
                font=("Segoe UI", 10),
                fill="#007bff",
            )


class AdvancedAnalysisDialog(tk.Toplevel):
    """심화 MSG 분석 — 캡처 진단 / 분포 / 시간 패턴 / 휴리스틱 / Msg2 진단."""

    def __init__(self, app: ProtocolAnalyzerApp) -> None:
        super().__init__(app)
        self.app = app
        self.title("심화 MSG 분석")
        self.geometry("720x560")
        self.minsize(560, 420)
        self.transient(app)

        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="O-RAN 버전:").pack(side=tk.LEFT)
        ver_cb = ttk.Combobox(
            top, textvariable=app.oran_version, values=["1.0", "2.0", "3.0"],
            width=6, state="readonly",
        )
        ver_cb.pack(side=tk.LEFT, padx=4)
        ver_cb.bind("<<ComboboxSelected>>", lambda _e: self._run_analysis())
        self._status = ttk.Label(top, text="분석 중…", foreground="#555")
        self._status.pack(side=tk.LEFT, padx=12)
        ttk.Button(top, text="다시 분석", command=self._run_analysis).pack(side=tk.RIGHT)
        ttk.Button(top, text="닫기", command=self.destroy).pack(side=tk.RIGHT, padx=4)

        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._texts: dict[str, tk.Text] = {}
        for key, title in (
            ("checklist", "분석 프로세스"),
            ("capture", "캡처 진단"),
            ("distribution", "MSG 분포"),
            ("temporal", "시간 패턴"),
            ("validation", "휴리스틱 검증"),
            ("msg2", "MSG2 진단"),
            ("selected", "선택 패킷"),
            ("full", "전체 리포트"),
        ):
            frame = ttk.Frame(self._notebook)
            self._notebook.add(frame, text=title)
            txt = tk.Text(frame, font=("Consolas", 9), wrap=tk.WORD, padx=8, pady=8)
            sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            txt.bind("<Key>", lambda _e: "break")
            self._texts[key] = txt

        self._queue: queue.Queue = queue.Queue()
        self._analysis_gen = 0
        self._poll_after_id: str | None = None
        self._run_analysis()

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _run_analysis(self) -> None:
        if self._poll_after_id:
            try:
                self.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        self._drain_queue()
        self._analysis_gen += 1
        gen = self._analysis_gen

        # 메인 스레드에서 시퀀스·Msg2 이슈 최신화 후 분석
        self.app.rach_sequences = finalize_pcap_load(self.app.packets)
        self.app._refresh_msg2_issues()

        self._status.configure(text="분석 중…")
        for txt in self._texts.values():
            txt.configure(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.insert("1.0", "분석 중…")
            txt.configure(state=tk.DISABLED)
        threading.Thread(target=self._worker, args=(gen,), daemon=True).start()
        self._poll_after_id = self.after(150, lambda: self._poll(gen))

    def _worker(self, gen: int) -> None:
        try:
            fsize = None
            if self.app.pcap_paths:
                try:
                    fsize = sum(os.path.getsize(p) for p in self.app.pcap_paths)
                except OSError:
                    pass
            report = run_full_advanced_analysis(
                self.app.packets,
                self.app.rach_sequences,
                pcap_path=self.app.pcap_path,
                pcap_paths=self.app.pcap_paths,
                file_size=fsize,
                selected=self.app.selected_packet,
                oran_version=self.app.oran_version.get(),
            )
            self._queue.put(("ok", report, gen))
        except Exception as exc:
            self._queue.put(("err", exc, gen))

    def _poll(self, gen: int) -> None:
        self._poll_after_id = None
        try:
            kind, data, result_gen = self._queue.get_nowait()
            if result_gen != self._analysis_gen:
                if self.winfo_exists():
                    self._poll_after_id = self.after(150, lambda: self._poll(gen))
                return
            if kind == "ok":
                self._show_report(data)
                return
            messagebox.showerror("분석 오류", str(data), parent=self)
            self._status.configure(text="오류")
            return
        except queue.Empty:
            pass
        except ValueError:
            pass
        if self.winfo_exists():
            self._poll_after_id = self.after(150, lambda: self._poll(gen))

    def _set_text(self, key: str, content: str) -> None:
        txt = self._texts[key]
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)
        txt.insert("1.0", content)
        txt.configure(state=tk.DISABLED)

    def _show_report(self, report: AdvancedAnalysisReport) -> None:
        self.app._adv_report = report
        self._status.configure(
            text=f"완료 {report.analyzed_at} | {report.packet_count:,}패킷 | O-RAN {report.oran_version}",
        )
        h = report.capture_health
        cap_lines = [
            f"파일: {h.get('pcap_paths_label') or h.get('pcap_path') or '—'}",
            f"크기: {(h.get('file_size') or 0) / (1024**2):.1f} MB" if h.get("file_size") else "",
            f"총 패킷: {h.get('total', 0):,}",
            f"eCPRI: {h.get('ecpri_count', 0):,}",
            f"O-RAN 헤더: {h.get('oran_header_count', 0):,}",
            f"캡처 시간: {h.get('duration_sec', 0):.2f}s",
            "",
            "[체크]",
        ]
        for name, ok, detail in h.get("checks", []):
            cap_lines.append(f"  {'✓' if ok else '✗'} {name}: {detail}")
        if h.get("source_distribution") and len(h["source_distribution"]) > 1:
            cap_lines.extend(["", "[소스별 패킷 수]"])
            for name, cnt in h["source_distribution"].items():
                cap_lines.append(f"  {name}: {cnt:,}")
        if h.get("issues"):
            cap_lines.extend(["", "[이슈]"] + [f"  ⚠ {x}" for x in h["issues"]])
        self._set_text("capture", "\n".join(cap_lines))

        cl = report.process_checklist
        self._set_text(
            "checklist",
            "\n".join(
                f"{'✓' if ok else '✗' if ok is False else '?'} {step}\n    → {detail}"
                for step, ok, detail in cl
            ),
        )

        d = report.msg_distribution
        dist_lines = [f"샘플 패킷: {d.get('sample_size', 0):,}", "", "[eCPRI 타입]"]
        dist_lines.extend(f"  {k}: {v:,}" for k, v in d.get("ecpri_type_counts", {}).items())
        dist_lines.extend(["", "[strict 식별]"])
        dist_lines.extend(f"  {k}: {v:,}" for k, v in d.get("identified_msg_counts", {}).items())
        dist_lines.extend(["", "[심화 식별]"])
        dist_lines.extend(f"  {k}: {v:,}" for k, v in d.get("advanced_label_counts", {}).items())
        dist_lines.extend(["", "[페이로드 크기]"])
        for k, st in d.get("payload_size_stats", {}).items():
            if st:
                dist_lines.append(f"  {k}: min={st['min']} avg={st['avg']} max={st['max']}")
        self._set_text("distribution", "\n".join(dist_lines))

        t_lines = []
        for k, v in report.temporal.items():
            t_lines.append(f"{k}:")
            t_lines.append(f"  기대: {v.get('expected', '')}")
            if v.get("sample_count"):
                t_lines.append(f"  평균 간격: {v.get('average_interval_ms')}ms (σ={v.get('std_deviation_ms')}ms)")
            else:
                t_lines.append("  샘플 부족")
            t_lines.append("")
        self._set_text("temporal", "\n".join(t_lines))

        val = report.validation.get("rules", {})
        self._set_text(
            "validation",
            "\n".join(
                f"{'✓' if r.get('valid') else '✗' if r.get('valid') is False else '?'}"
                f" {r['rule']}\n    {r.get('detail', '')}\n"
                for r in val.values()
            ),
        )

        m2_lines = [
            f"분석 시각: {report.analyzed_at}  |  패킷: {report.packet_count:,}  |  O-RAN {report.oran_version}",
            "",
        ]
        for c in report.msg2_diagnosis:
            star = " ★ 해당 가능" if c.get("likely") else ""
            m2_lines.append(f"[{c['id']}] {c['title']}{star}")
            m2_lines.append(f"  증상: {c['symptom']}")
            m2_lines.append(f"  확인: {c['check']}")
            m2_lines.append(f"  조치: {c['action']}")
            m2_lines.append("")
        if report.msg2_gap_text:
            m2_lines.extend(["", report.msg2_gap_text])
        self._set_text("msg2", "\n".join(m2_lines))

        sp = report.selected_packet
        if sp:
            dbg = sp.get("debug_info", {})
            dbg_lines = "\n".join(f"  {k}: {v}" for k, v in dbg.items())
            sel_txt = (
                f"심화: {sp.get('msg_type')} (신뢰도 {sp.get('confidence')}%)\n"
                f"strict: Msg{sp.get('strict_msg_num')}\n"
                f"일치: {sp.get('agreement')}\n\n"
                f"[debug]\n{dbg_lines}"
            )
        else:
            sel_txt = "선택된 패킷 없음 — 목록에서 패킷을 선택한 뒤 다시 분석하세요."
        self._set_text("selected", sel_txt)
        self._set_text("full", format_advanced_report(report))

        if self.app._criteria_tab_open and self.app.selected_packet:
            self.app._fill_criteria_tab(self.app.selected_packet)


def main() -> None:
    if PcapReader is None:
        messagebox.showerror(
            "의존성 오류",
            "scapy 패키지가 필요합니다.\n\npip install scapy",
        )
        return
    app = ProtocolAnalyzerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
