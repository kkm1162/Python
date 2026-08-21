"""
O-RAN Fronthaul MSG1~4 식별 (eCPRI + Section Type + Direction)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oran_fronthaul import expected_flow_for_msg

PASS_BG = "#c8e6c9"
FAIL_BG = "#ffcdd2"
INFO_BG = "#bbdefb"
NEUTRAL_BG = "#e0e0e0"

ORAN_MSG_SECTION = {
    0: "U-Plane Type-0",
    1: "Most DL/UL channels",
    2: "PRACH / mixed-numerology",
    3: "PRACH / mixed-numerology",
    4: "Reserved",
    5: "UE scheduling",
    6: "C-Plane RAN Scheduling",
}

ORAN_MSG_LABELS = {
    1: {"name": "Msg1 (U-Plane Data)", "short": "U-Plane", "color": "#ffd699"},
    2: {"name": "Msg2 (DL Scheduling)", "short": "DL Sched", "color": "#a8e6a3"},
    3: {"name": "Msg3 (UL Scheduling)", "short": "UL Sched", "color": "#9ecfff"},
    4: {"name": "Msg4 (ACK/NACK)", "short": "ACK/NACK", "color": "#d9b3ff"},
}

MSG1_MIN_PAYLOAD = 256
MSG4_MAX_PAYLOAD = 128

ORAN_MSG_RULES: dict[int, str] = {
    1: f"eCPRI 0x00 (IQ Data) + Section Type 0~5, 또는 payload ≥ {MSG1_MIN_PAYLOAD}B",
    2: "eCPRI 0x02 (RT Control) + Section Type 6 + Direction DL + Fronthaul DU→RU",
    3: "eCPRI 0x02 (RT Control) + Section Type 6 + Direction UL + Fronthaul DU→RU",
    4: "eCPRI 0x04 (ACK/NACK) + Fronthaul DU→RU",
}

ORAN_PICK_RULES: dict[int, str] = {
    1: "같은 슬롯 Msg1 후보 중 payload 가장 큰 패킷 1개",
    2: "같은 슬롯 Msg2 후보 중 가장 빠른(시간상 첫) 패킷 1개",
    3: "같은 슬롯 Msg3 후보 중 가장 빠른 패킷 1개",
    4: "같은 슬롯 Msg4 후보 중 payload 가장 작은 패킷 1개",
}


def format_msg_criteria_header() -> str:
    lines = [
        "=== O-RAN MSG1~4 판별 (2단계) ===",
        "",
        "[1단계] 패킷 타입 — 이 패킷이 어떤 Msg 종류인지",
    ]
    for m in (1, 2, 3, 4):
        lines.append(f"  Msg{m}: {ORAN_MSG_RULES[m]}")
    lines.extend([
        "",
        "  ※ U-Plane(eCPRI 0x00)은 Msg1 후보뿐. Msg2/3는 C-Plane(0x02).",
        "  ※ Data Direction = 공중 인터페이스 DL/UL. Fronthaul(DU→RU)은 별도 컬럼.",
        "",
        "[2단계] 슬롯 대표 — 같은 Frame/Subframe/Slot 안에서 시퀀스에 1개만 태깅",
    ])
    for m in (1, 2, 3, 4):
        lines.append(f"  Msg{m}: {ORAN_PICK_RULES[m]}")
    lines.extend([
        "",
        "  ※ Msg1→2→3→4 순서. Msg2 없으면 Msg3/4 미태깅.",
        "  ※ Msg1 조건 맞아도 슬롯 대표가 아니면 목록에 Msg 표시 없음.",
    ])
    return "\n".join(lines)


def _pick_best_for_msg(candidates: list[Any], msg_num: int) -> Any | None:
    if not candidates:
        return None
    if msg_num == 1:
        return max(candidates, key=lambda p: getattr(p, "ecpri_payload_size", 0) or p.length)
    if msg_num == 4:
        return min(candidates, key=lambda p: getattr(p, "ecpri_payload_size", 0) or p.length)
    return min(candidates, key=lambda p: p.timestamp)


def _slot_peers(p: Any, all_packets: list[Any]) -> list[Any]:
    tk = timing_key(p)
    if tk is None:
        return []
    return [x for x in all_packets if timing_key(x) == tk]


def format_slot_competition(p: Any, all_packets: list[Any]) -> str:
    """같은 슬롯에서 왜 이 패킷이(또는 isn't) 시퀀스 대표인지."""
    peers = _slot_peers(p, all_packets)
    tk = timing_key(p)
    if not peers or tk is None:
        return "=== 슬롯 경쟁 ===\n  Frame/Subframe/Slot 정보 없음 — 슬롯 그룹 불가"

    lines = [
        f"=== 슬롯 경쟁 (F{tk[0]}/SF{tk[1]}/Slot{tk[2]}, 총 {len(peers)}패킷) ===",
    ]
    by_msg: dict[int, list[Any]] = {i: [] for i in range(1, 5)}
    for peer in peers:
        m, _ = packet_oran_msg(peer)
        if m is not None:
            by_msg[m].append(peer)

    tagged_msg = getattr(p, "rach_msg", None)
    for m in (1, 2, 3, 4):
        cands = by_msg[m]
        if not cands:
            lines.append(f"  Msg{m} 후보: 없음")
            continue
        winner = _pick_best_for_msg(cands, m)
        win_idx = winner.index if winner else -1
        lines.append(f"  Msg{m} 후보 {len(cands)}개 → {ORAN_PICK_RULES[m]}")
        for c in sorted(cands, key=lambda x: x.index):
            pay = getattr(c, "ecpri_payload_size", 0) or c.length
            is_winner = c.index == win_idx
            is_this = c.index == p.index
            star = "★ 시퀀스 선택" if is_winner else "  "
            you = " ← 지금 패킷" if is_this else ""
            lines.append(f"    {star} #{c.index + 1}  {pay}B{you}")

    lines.append("")
    identified, _ = packet_oran_msg(p)
    if tagged_msg:
        if p.index == (_pick_best_for_msg(by_msg.get(tagged_msg, []), tagged_msg) or p).index:
            lines.append(
                f"  → 이 패킷: Msg{tagged_msg} 시퀀스 대표 (S{getattr(p, 'rach_sequence_id', 0) + 1})"
            )
        else:
            lines.append(f"  → 이 패킷: Msg{tagged_msg}로 태깅됨")
    elif identified:
        win = _pick_best_for_msg(by_msg.get(identified, []), identified)
        if win and win.index != p.index:
            lines.append(
                f"  → 이 패킷: Msg{identified} 조건은 맞지만, "
                f"슬롯 대표는 #{win.index + 1} (선택 규칙 때문)"
            )
        else:
            lines.append(f"  → 이 패킷: Msg{identified} 타입이나 시퀀스 미포함 (Msg2 실패 등)")
    else:
        lines.append("  → 이 패킷: Msg1~4 타입 아님 (일반 eCPRI/U-Plane traffic)")
    return "\n".join(lines)


def _field_summary(p: Any) -> str:
    et = p.ecpri_type
    et_s = f"0x{et:02x}" if et is not None else "—"
    st = p.section_type if p.section_type is not None else "—"
    ul = "UL" if p.is_uplink else ("DL" if p.is_uplink is False else "—")
    pay = getattr(p, "ecpri_payload_size", 0) or max(0, p.length - 42)
    fid = getattr(p, "frame_id", None)
    timing = f"F{fid}/SF{getattr(p, 'subframe_id', '—')}/Slot{getattr(p, 'slot_id', '—')}" if fid is not None else "—"
    flow = getattr(p, "flow", "") or "—"
    ep = f"{getattr(p, 'src', '?')}→{getattr(p, 'dst', '?')}"
    return f"eCPRI={et_s}  Section={st}  Dir={ul}  Flow={flow}  {ep}  Payload={pay}B  Timing={timing}"


def _flow_ok_for_msg(p: Any, msg_num: int) -> tuple[bool, str]:
    expected = expected_flow_for_msg(msg_num)
    flow = getattr(p, "flow", "") or ""
    if not expected:
        return True, ""
    if flow == expected:
        return True, f"Fronthaul {flow}"
    if not flow:
        return True, "Fronthaul 미확인"
    return False, f"Fronthaul {flow} (기대: {expected})"


def _check_msg1(p: Any) -> tuple[bool, str]:
    if not p.is_ecpri or p.ecpri_type != 0x00:
        return False, "eCPRI type ≠ 0x00"
    pay = getattr(p, "ecpri_payload_size", 0) or max(0, p.length - 42)
    if p.section_type is not None and 0 <= p.section_type <= 5:
        return True, f"Section Type {p.section_type} (0~5)"
    if pay >= MSG1_MIN_PAYLOAD:
        return True, f"payload {pay}B ≥ {MSG1_MIN_PAYLOAD}B"
    return False, f"Section {p.section_type}, payload {pay}B 부족"


def _check_msg2(p: Any) -> tuple[bool, str]:
    if p.ecpri_type != 0x02:
        return False, f"eCPRI type {p.ecpri_type} ≠ 0x02"
    if p.section_type != 6:
        return False, f"Section {p.section_type} ≠ 6"
    if p.is_uplink is not False:
        return False, "Direction ≠ DL"
    flow_ok, flow_detail = _flow_ok_for_msg(p, 2)
    if not flow_ok:
        return False, f"Section 6 + DL, {flow_detail}"
    return True, f"Section 6 + DL, {flow_detail or 'DU→RU'}"


def _check_msg3(p: Any) -> tuple[bool, str]:
    if p.ecpri_type != 0x02:
        return False, f"eCPRI type {p.ecpri_type} ≠ 0x02"
    if p.section_type != 6:
        return False, f"Section {p.section_type} ≠ 6"
    if p.is_uplink is not True:
        return False, "Direction ≠ UL"
    flow_ok, flow_detail = _flow_ok_for_msg(p, 3)
    if not flow_ok:
        return False, f"Section 6 + UL, {flow_detail}"
    return True, f"Section 6 + UL, {flow_detail or 'DU→RU'}"


def _check_msg4(p: Any) -> tuple[bool, str]:
    if p.ecpri_type != 0x04:
        return False, f"eCPRI type {p.ecpri_type} ≠ 0x04"
    flow_ok, flow_detail = _flow_ok_for_msg(p, 4)
    if not flow_ok:
        return False, flow_detail
    return True, flow_detail or "eCPRI 0x04"


_MSG_CHECKERS = {1: _check_msg1, 2: _check_msg2, 3: _check_msg3, 4: _check_msg4}


def format_packet_msg_analysis(p: Any | None) -> str:
    """선택 패킷에 대한 조건별 충족 여부."""
    if p is None:
        return "=== 선택 패킷 ===\n  (패킷을 선택하세요)"
    lines = ["=== 선택 패킷 분석 ===", f"  {_field_summary(p)}", ""]
    identified, _ = packet_oran_msg(p)
    for m in (1, 2, 3, 4):
        ok, detail = _MSG_CHECKERS[m](p)
        mark = "✓" if ok else "✗"
        match_tag = " ← 식별됨" if identified == m else ""
        lines.append(f"  Msg{m} {mark}  {detail}{match_tag}")
    lines.append("")
    if identified:
        lines.append(f"  식별: Msg{identified} ({ORAN_MSG_LABELS[identified]['name']})")
    else:
        lines.append("  식별: 해당 없음")
    tagged = getattr(p, "rach_msg", None)
    if tagged:
        sid = getattr(p, "rach_sequence_id", None)
        s = f" S{sid + 1}" if sid is not None else ""
        lines.append(f"  시퀀스 태그: Msg{tagged}{s}")
    elif identified:
        lines.append("  시퀀스 태그: 없음 (아래 슬롯 경쟁 참고)")
    else:
        lines.append("  시퀀스 태그: 없음")
    return "\n".join(lines)


@dataclass
class HexHighlight:
    start: int
    end: int
    bg: str
    tag: str


def _field_passes_for_msg(p: Any, msg_num: int, field: str) -> bool | None:
    """필드 단위 판정. None이면 해당 Msg와 무관한 정보 필드."""
    if field == "ecpri_type":
        if msg_num == 1:
            return p.ecpri_type == 0x00
        if msg_num in (2, 3):
            return p.ecpri_type == 0x02
        if msg_num == 4:
            return p.ecpri_type == 0x04
    elif field == "section_type":
        if msg_num == 1:
            return p.section_type is not None and 0 <= p.section_type <= 5
        if msg_num in (2, 3):
            return p.section_type == 6
    elif field == "direction":
        if msg_num == 2:
            return p.is_uplink is False
        if msg_num == 3:
            return p.is_uplink is True
    return None


def _highlight_color(p: Any, field: str, related_msgs: tuple[int, ...]) -> str:
    if not related_msgs:
        return INFO_BG
    identified, _ = packet_oran_msg(p)
    for m in related_msgs:
        sub = _field_passes_for_msg(p, m, field)
        if sub is None:
            continue
        if identified == m and sub:
            return ORAN_MSG_LABELS[m]["color"]
        if not sub:
            return FAIL_BG
    if identified in related_msgs:
        return ORAN_MSG_LABELS[identified]["color"]
    for m in related_msgs:
        sub = _field_passes_for_msg(p, m, field)
        if sub:
            return ORAN_MSG_LABELS[m]["color"]
    return NEUTRAL_BG


def compute_hex_highlights(p: Any) -> list[HexHighlight]:
    """판정에 쓰이는 바이트 구간 → Hex 뷰 배경색."""
    eo = getattr(p, "ecpri_offset", None)
    if eo is None or eo < 0:
        return []
    oo = getattr(p, "oran_offset", None)
    out: list[HexHighlight] = []
    tag_i = 0

    def add(start: int, end: int, field: str, related: tuple[int, ...]) -> None:
        nonlocal tag_i
        if end <= start:
            return
        tag_i += 1
        out.append(HexHighlight(start, end, _highlight_color(p, field, related), f"hl_{tag_i}"))

    add(eo + 1, eo + 2, "ecpri_type", (1, 2, 3, 4))
    add(eo + 4, eo + 6, "eaxc", ())
    if oo is not None:
        add(oo + 0, oo + 1, "direction", (2, 3))
        add(oo + 1, oo + 2, "frame", ())
        add(oo + 2, oo + 4, "timing", ())
        add(oo + 4, oo + 5, "section_type", (1, 2, 3))
        add(oo + 5, oo + 7, "section_id", ())
        add(oo + 7, oo + 8, "start_prb", ())
        raw = getattr(p, "raw_bytes", b"")
        if len(raw) > oo + 10:
            add(oo + 10, oo + 11, "num_prb", ())
    return out


def get_criteria_tab_rows(p: Any | None) -> list[tuple[int, str, str, str]]:
    """판단 조건 탭용 (Msg, 조건, 결과, tag)."""
    if p is None:
        return []
    rows: list[tuple[int, str, str, str]] = []
    identified, _ = packet_oran_msg(p)
    for m in (1, 2, 3, 4):
        ok, _ = _MSG_CHECKERS[m](p)
        mark = "✓" if ok else "✗"
        tag = "pass" if ok else "fail"
        if identified == m:
            mark += " ★"
        rows.append((m, ORAN_MSG_RULES[m], mark, tag))
    tagged = getattr(p, "rach_msg", None)
    if tagged:
        sid = getattr(p, "rach_sequence_id", None)
        s = f" S{sid + 1}" if sid is not None else ""
        rows.append((0, f"시퀀스 태그: Msg{tagged}{s}", "✓", "pass"))
    elif identified:
        rows.append((0, "시퀀스 태그 없음 (슬롯 대표 미선정)", "—", "neutral"))
    flow = getattr(p, "flow", "") or ""
    if flow:
        rows.append((0, f"Fronthaul 흐름: {flow} ({p.src}→{p.dst})", "—", "info"))
    reason = explain_orphan_msg2(p, getattr(p, "_all_packets", None))
    if reason:
        rows.append((0, f"시퀀스 미포함 Msg2: {reason}", "⚠", "fail"))
    return rows


@dataclass
class Msg2IssueReport:
    orphan_msg2: list[Any] = field(default_factory=list)
    seq_fail_msg2: list[Any] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)


def explain_orphan_msg2(p: Any, all_packets: list[Any] | None) -> str | None:
    """Msg2 타입이나 시퀀스에 미포함인 이유. 해당 없으면 None."""
    m, _ = packet_oran_msg(p)
    if m != 2 or getattr(p, "rach_msg", None) is not None:
        return None
    if not all_packets:
        return "시퀀스에 태깅되지 않음"
    tk = timing_key(p)
    if tk is None:
        return "Frame/Subframe/Slot 없음 — 슬롯 그룹 불가"
    peers = [x for x in all_packets if timing_key(x) == tk]
    has_msg1 = any(packet_oran_msg(x)[0] == 1 for x in peers)
    if not has_msg1:
        return f"같은 슬롯 F{tk[0]}/SF{tk[1]}/Slot{tk[2]}에 Msg1 없음"
    cands = [x for x in peers if packet_oran_msg(x)[0] == 2]
    if len(cands) > 1:
        winner = _pick_best_for_msg(cands, 2)
        if winner and winner.index != p.index:
            return (
                f"슬롯 내 Msg2 후보 {len(cands)}개 — "
                f"#{winner.index + 1}만 시퀀스 선택 (가장 빠른 패킷)"
            )
    return "Msg2 식별됨, 시퀀스 미태깅"


def collect_msg2_sequence_issues(
    packets: list[Any],
    sequences: list[Any] | None = None,
) -> Msg2IssueReport:
    """미포함 Msg2 패킷 + Msg2 단계에서 끊긴 시퀀스."""
    orphan: list[Any] = []
    reasons: dict[int, str] = {}
    for p in packets:
        m, _ = packet_oran_msg(p)
        if m == 2 and getattr(p, "rach_msg", None) is None:
            orphan.append(p)
            reasons[p.index] = explain_orphan_msg2(p, packets) or "시퀀스 미포함"
    seq_fail = [
        s for s in (sequences or [])
        if getattr(s, "failed_at_msg", None) == 2
    ]
    return Msg2IssueReport(orphan_msg2=orphan, seq_fail_msg2=seq_fail, reasons=reasons)


@dataclass
class Msg2GapReport:
    """DU는 Msg2 보냈다 vs GUI/시퀀스에서 Msg2가 안 보일 때 계층별 진단."""
    ecpri_rt: int = 0
    rt_st6: int = 0
    rt_st6_dl: int = 0
    rt_st6_ul: int = 0
    rt_st5_dl: int = 0
    strict_msg2: int = 0
    tagged_msg2: int = 0
    msg1_candidates: int = 0
    seq_fail_msg2: int = 0
    hidden_candidates: list[tuple[Any, str]] = field(default_factory=list)
    integration_hints: list[str] = field(default_factory=list)


def analyze_msg2_gap(
    packets: list[Any],
    sequences: list[Any] | None = None,
) -> Msg2GapReport:
    """Msg2가 '없는지' vs '식별/시퀀스만 실패인지' 계층별 분해."""
    rep = Msg2GapReport()
    rep.seq_fail_msg2 = sum(
        1 for s in (sequences or []) if getattr(s, "failed_at_msg", None) == 2
    )
    for p in packets:
        if p.ecpri_type == 0x02:
            rep.ecpri_rt += 1
        if p.ecpri_type == 0x02 and p.section_type == 6:
            rep.rt_st6 += 1
            if p.is_uplink is False:
                rep.rt_st6_dl += 1
            elif p.is_uplink is True:
                rep.rt_st6_ul += 1
        if p.ecpri_type == 0x02 and p.section_type == 5 and p.is_uplink is False:
            rep.rt_st5_dl += 1
        m, src = packet_oran_msg(p)
        if m == 1:
            rep.msg1_candidates += 1
        if m == 2:
            rep.strict_msg2 += 1
            if getattr(p, "rach_msg", None) == 2:
                rep.tagged_msg2 += 1
            if src == "oran_relaxed":
                rep.hidden_candidates.append((p, "Direction 미확인 — DU→RU ST6를 Msg2로 추정"))
            continue
        elif p.ecpri_type == 0x02 and p.section_type == 6:
            why: list[str] = []
            if p.is_uplink is True:
                why.append("Direction=UL (DL이어야 Msg2)")
            elif p.is_uplink is None:
                why.append("Direction 미파싱")
            flow = getattr(p, "flow", "") or ""
            if flow == "RU→DU":
                why.append("Fronthaul RU→DU (DU→RU 기대)")
            elif not flow:
                why.append("Fronthaul 미확인")
            if why:
                rep.hidden_candidates.append((p, "; ".join(why)))
        elif p.ecpri_type == 0x02 and p.section_type == 5 and p.is_uplink is False:
            rep.hidden_candidates.append((p, "ST5 DL — 일부 DU는 UE sched로 Msg2에 해당"))

    hints: list[str] = []
    if rep.ecpri_rt == 0:
        hints.append(
            "캡처에 C-Plane(eCPRI 0x02)이 없음 — DU↔RU 사이 미러링·VLAN·포트 필터 확인"
        )
    elif rep.rt_st6 == 0 and rep.rt_st5_dl == 0:
        hints.append(
            "C-Plane은 있으나 Section 6/5 DL 없음 — DU가 스케줄링을 안 보내거나 헤더 파싱 오프셋 의심"
        )
    elif rep.rt_st6_dl > 0 and rep.strict_msg2 == 0:
        hints.append(
            f"ST6+DL {rep.rt_st6_dl}건 보이나 Msg2 식별 0건 — dataDirection·Fronthaul·Section 파싱 확인"
        )
    elif rep.rt_st6 > 0 and rep.rt_st6_dl == 0:
        hints.append(
            "ST6는 있으나 DL 없음(UL만) — Msg3 스케줄만 보이고 Msg2 DL 스케줄은 미수신 가능"
        )
    if rep.strict_msg2 > 0 and rep.tagged_msg2 == 0:
        hints.append(
            "Msg2로 식별된 패킷은 있으나 시퀀스 태깅 0건 — 우리 RU의 Msg1(UL U-Plane) 또는 슬롯 타이밍 불일치"
        )
    if rep.seq_fail_msg2 > 0 and rep.msg1_candidates > 0:
        hints.append(
            f"시퀀스 Msg2 실패 {rep.seq_fail_msg2}건 — 슬롯에 Msg1은 있으나 같은 슬롯 Msg2 없음 "
            "(DU가 해당 슬롯에 DL 스케줄 미전송 또는 다른 Frame/Slot)"
        )
    if rep.strict_msg2 == 0 and rep.seq_fail_msg2 > 0:
        hints.append(
            "전형적 연동 증상: DU는 보냈다 하나 GUI Msg2 0건 + 시퀀스 실패 — "
            "벤더 RU PCAP과 Section/Direction/타이밍 hex 비교 권장"
        )
    rep.integration_hints = hints
    return rep


def format_msg2_gap_report(rep: Msg2GapReport) -> str:
    lines = [
        "=== Msg2 가시성 진단 (DU 보냄 vs GUI/시퀀스) ===",
        "",
        "[계층별 카운트]",
        f"  C-Plane eCPRI 0x02     : {rep.ecpri_rt:,}",
        f"  Section 6 (C-Plane)    : {rep.rt_st6:,}",
        f"    └ DL (Msg2 후보)     : {rep.rt_st6_dl:,}",
        f"    └ UL (Msg3 후보)     : {rep.rt_st6_ul:,}",
        f"  Section 5 DL (대안)    : {rep.rt_st5_dl:,}",
        f"  Msg1 후보              : {rep.msg1_candidates:,}",
        f"  Msg2 식별 (strict)     : {rep.strict_msg2:,}",
        f"  Msg2 시퀀스 태깅       : {rep.tagged_msg2:,}",
        f"  시퀀스 Msg2 실패       : {rep.seq_fail_msg2:,}",
        f"  숨은/거의 Msg2 후보    : {len(rep.hidden_candidates):,}",
    ]
    if rep.integration_hints:
        lines.extend(["", "[연동 힌트]"])
        for h in rep.integration_hints:
            lines.append(f"  • {h}")
    if rep.hidden_candidates:
        lines.extend(["", "[거의 Msg2 후보 상위 5건]"])
        for p, why in rep.hidden_candidates[:5]:
            lines.append(f"  #{p.index + 1}  {why}")
    return "\n".join(lines)


def identify_oran_msg(
    *,
    is_ecpri: bool,
    ecpri_type: int | None,
    section_type: int | None,
    is_uplink: bool | None,
    payload_size: int,
    total_length: int,
    flow: str = "",
) -> tuple[int | None, str]:
    if not is_ecpri or ecpri_type is None:
        return None, ""

    pay = payload_size or max(0, total_length - 42)

    if ecpri_type == 0x00:
        if section_type is not None and 0 <= section_type <= 5:
            return 1, "oran"
        if pay >= MSG1_MIN_PAYLOAD:
            return 1, "oran"
        return None, ""

    if ecpri_type == 0x02 and section_type == 6:
        # Msg2/3 C-Plane 스케줄은 표준상 DU→RU (dataDirection으로 DL/UL 구분)
        if flow == "RU→DU":
            return None, ""
        if is_uplink is False:
            return 2, "oran"
        if is_uplink is True:
            return 3, "oran"
        # 연동 이슈: Direction 비트 미파싱/오류 시 DU→RU ST6는 DL 스케줄(Msg2)로 추정
        if flow == "DU→RU":
            return 2, "oran_relaxed"
        return None, ""

    if ecpri_type == 0x04:
        if flow == "RU→DU":
            return None, ""
        return 4, "oran"

    return None, ""


def packet_oran_msg(p: Any) -> tuple[int | None, str]:
    return identify_oran_msg(
        is_ecpri=p.is_ecpri,
        ecpri_type=p.ecpri_type,
        section_type=p.section_type,
        is_uplink=p.is_uplink,
        payload_size=getattr(p, "ecpri_payload_size", 0) or 0,
        total_length=p.length,
        flow=getattr(p, "flow", "") or "",
    )


def timing_key(p: Any) -> tuple[int, int, int] | None:
    fid = getattr(p, "frame_id", None)
    if fid is None:
        return None
    return (fid, getattr(p, "subframe_id", 0) or 0, getattr(p, "slot_id", 0) or 0)
