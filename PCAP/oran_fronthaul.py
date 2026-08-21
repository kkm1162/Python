"""
DU / RU Fronthaul 흐름 추론 (캡처 지점: DU↔RU 사이 양방향)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

# O-RAN: Msg2/Msg3 C-Plane 스케줄링은 둘 다 O-DU → O-RU (dataDirection으로 DL/UL 구분)
# Msg1 UL U-Plane 은 O-RU → O-DU


def infer_du_ru_ips(packets: list[Any]) -> tuple[str | None, str | None]:
    """패킷 패턴으로 DU / RU IP 추론."""
    du_score: Counter[str] = Counter()
    ru_score: Counter[str] = Counter()
    st6_dl_src: Counter[str] = Counter()

    for p in packets:
        if not getattr(p, "is_ecpri", False):
            continue
        src = getattr(p, "src", "") or ""
        dst = getattr(p, "dst", "") or ""
        if src in ("", "?"):
            continue

        et = p.ecpri_type
        st = p.section_type
        ul = p.is_uplink

        # Msg2 (ST6 + DL) 송신 측 = DU (가장 신뢰도 높음)
        if et == 0x02 and st == 6 and ul is False:
            st6_dl_src[src] += 1
            du_score[src] += 5

        # Msg3 (ST6 + UL) C-Plane 스케줄 — 역시 DU → RU
        if et == 0x02 and st == 6 and ul is True:
            du_score[src] += 4

        # DL U-Plane: DU → RU
        if et == 0x00 and ul is False:
            du_score[src] += 2
            if dst and dst != "?":
                ru_score[dst] += 1

        # UL U-Plane: RU → DU
        if et == 0x00 and ul is True:
            ru_score[src] += 3
            if dst and dst != "?":
                du_score[dst] += 1

        # 기타 C-Plane 대부분 DU 발신
        if et == 0x02:
            du_score[src] += 1

        # ACK/NACK (Msg4): 보통 DU → RU
        if et == 0x04:
            du_score[src] += 2

    du_ip: str | None = None
    ru_ip: str | None = None

    if st6_dl_src:
        du_ip = st6_dl_src.most_common(1)[0][0]
    elif du_score:
        du_ip = du_score.most_common(1)[0][0]

    if ru_score:
        ru_ip = ru_score.most_common(1)[0][0]

    if du_ip and ru_ip == du_ip:
        ru_candidates = [ip for ip, _ in ru_score.most_common() if ip != du_ip]
        ru_ip = ru_candidates[0] if ru_candidates else None

    if du_ip and not ru_ip:
        # DU와 통신하는 상대 IP
        peers: Counter[str] = Counter()
        for p in packets:
            if p.src == du_ip and p.dst not in ("", "?", du_ip):
                peers[p.dst] += 1
            if p.dst == du_ip and p.src not in ("", "?", du_ip):
                peers[p.src] += 1
        if peers:
            ru_ip = peers.most_common(1)[0][0]

    return du_ip, ru_ip


def packet_flow_label(p: Any, du_ip: str | None, ru_ip: str | None) -> str:
    """Ethernet 흐름: DU→RU / RU→DU (캡처 지점 기준)."""
    if not du_ip and not ru_ip:
        return ""
    src = getattr(p, "src", "") or ""
    dst = getattr(p, "dst", "") or ""
    if du_ip and ru_ip:
        if src == du_ip and dst == ru_ip:
            return "DU→RU"
        if src == ru_ip and dst == du_ip:
            return "RU→DU"
    if du_ip and src == du_ip:
        return "DU→?"
    if ru_ip and src == ru_ip:
        return "RU→?"
    if du_ip and dst == du_ip:
        return "?→DU"
    if ru_ip and dst == ru_ip:
        return "?→RU"
    return "?"


def apply_fronthaul_roles(packets: list[Any]) -> tuple[str | None, str | None]:
    """모든 패킷에 flow 라벨 부여."""
    du_ip, ru_ip = infer_du_ru_ips(packets)
    for p in packets:
        p.flow = packet_flow_label(p, du_ip, ru_ip)
    return du_ip, ru_ip


def expected_flow_for_msg(msg_num: int) -> str | None:
    """표준 O-RAN Fronthaul Ethernet 흐름."""
    if msg_num == 1:
        return None  # DL/UL 모두 가능
    if msg_num in (2, 3, 4):
        return "DU→RU"
    return None
