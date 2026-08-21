"""
DL U-Plane RB 기반 Msg2(RAR) / SIB 후보 탐색
- eCPRI 0x00 + Downlink
- numPRB / start-end 헤더 우선 (압축 U-Plane), IQ 크기는 보조·교차검증
"""

from __future__ import annotations

from typing import Any

# 1 PRB ≈ 12 subcarrier × 14 symbol × 2 byte (I/Q), 비압축 기준
UPLANE_IQ_BYTES_PER_RB = 336
# eCPRI payload 내: PC_ID(4) + O-RAN common(4) + U-Plane section 최소(~7)
UPLANE_FIXED_OVERHEAD = 15
# 비압축 IQ ↔ 헤더 numPRB 교차검증 허용 오차
IQ_SIZE_TOLERANCE_HIGH = 1.15


def uplane_ecpri_payload_bytes(p: Any) -> int:
    """eCPRI 4바이트 헤더 이후 페이로드 길이 (실제 UDP 우선)."""
    udp_len = getattr(p, "udp_payload_len", 0) or 0
    hdr_size = getattr(p, "ecpri_payload_size", 0) or 0
    if udp_len >= 4:
        from_udp = udp_len - 4
        return max(from_udp, hdr_size) if hdr_size > 0 else from_udp
    return hdr_size


def uplane_iq_byte_size(p: Any) -> int:
    pay = uplane_ecpri_payload_bytes(p)
    return max(0, pay - UPLANE_FIXED_OVERHEAD)


def rb_capacity_from_iq(iq_bytes: int) -> int:
    if iq_bytes < UPLANE_IQ_BYTES_PER_RB // 2:
        return 0
    return (iq_bytes + UPLANE_IQ_BYTES_PER_RB - 1) // UPLANE_IQ_BYTES_PER_RB


def header_rb_count(p: Any) -> int | None:
    """O-RAN U-Plane 헤더의 PRB 할당 수 (numPRB / start-end)."""
    num = getattr(p, "num_prb", None)
    start = getattr(p, "start_prb", None)
    end = getattr(p, "end_prb", None)
    span = (end - start + 1) if start is not None and end is not None and end >= start else 0

    candidates = [x for x in (num, span) if x is not None and x > 0]
    if not candidates:
        return None
    return max(candidates)


def resolve_uplane_prb(p: Any) -> tuple[int | None, str, int]:
    """
    PRB 수 결정.
    1) numPRB / start-end 헤더 (압축 포함, Wireshark Info PRB와 동일)
    2) 비압축인데 헤더가 과소 → IQ 용량으로 상향
    3) 헤더 없음 → IQ 용량만
    """
    iq_bytes = uplane_iq_byte_size(p)
    cap = rb_capacity_from_iq(iq_bytes)
    hdr = header_rb_count(p)

    if hdr is not None:
        rb = hdr
        src = "헤더PRB"
        # 비압축: 헤더가 작게 잘못 읽힌 경우 IQ로 상향 (100PRB 대역 등)
        if cap > rb + 2 and iq_bytes > rb * UPLANE_IQ_BYTES_PER_RB * IQ_SIZE_TOLERANCE_HIGH:
            rb = max(rb, cap)
            src = "헤더+IQ"
        return rb, src, iq_bytes

    if cap >= 1:
        return cap, "IQ용량", iq_bytes

    return None, "", iq_bytes


def is_dl_uplane_msg2_candidate(p: Any, max_rb: int) -> tuple[bool, str]:
    """DL U-Plane + 헤더/IQ 기준 할당 PRB ≤ max_rb."""
    if not getattr(p, "is_ecpri", False) or p.ecpri_type != 0x00:
        return False, ""
    if p.is_uplink is not False:
        return False, ""

    iq_bytes = uplane_iq_byte_size(p)
    cap = rb_capacity_from_iq(iq_bytes)

    rb, src, _ = resolve_uplane_prb(p)
    if rb is None or rb < 1:
        return False, ""

    if rb > max_rb:
        return False, ""

    # 헤더 없이 IQ만 있을 때: 비압축 상한
    hdr = header_rb_count(p)
    if hdr is None and iq_bytes > max_rb * UPLANE_IQ_BYTES_PER_RB * IQ_SIZE_TOLERANCE_HIGH:
        return False, ""

    start = getattr(p, "start_prb", None)
    end = getattr(p, "end_prb", None)
    num = getattr(p, "num_prb", None)
    prb_range = ""
    if start is not None and end is not None and end >= start:
        prb_range = f"PRB{start}-{end}"
    elif num:
        prb_range = f"numPRB={num}"

    udp_len = getattr(p, "udp_payload_len", 0) or 0
    extra = ", ".join(x for x in (prb_range, f"IQ{iq_bytes}B", f"UDP{udp_len}B", f"cap{cap}") if x)
    return True, f"{rb}PRB ({src}, {extra})"


def find_dl_uplane_msg2_candidates(
    packets: list[Any],
    max_rb: int,
) -> list[tuple[Any, str]]:
    if max_rb < 1:
        return []
    out: list[tuple[Any, str]] = []
    for p in packets:
        ok, detail = is_dl_uplane_msg2_candidate(p, max_rb)
        if ok:
            out.append((p, detail))
    return out


def format_uplane_msg2_search_summary(
    candidates: list[tuple[Any, str]],
    max_rb: int,
) -> str:
    lines = [
        f"=== Msg2/SIB DL U-Plane (≤{max_rb} PRB) ===",
        f"  후보: {len(candidates):,}건",
        "",
        "  조건:",
        "    eCPRI 0x00 + Downlink",
        "    numPRB / PRB start-end 헤더 ≤ 입력값 (압축 U-Plane 포함)",
        "    헤더 없을 때만 IQ 크기(≤max×336B) 사용",
        f"    1 PRB ≈ {UPLANE_IQ_BYTES_PER_RB}B (비압축 참고)",
    ]
    if candidates:
        lines.extend(["", "[상위 10건]"])
        for p, detail in candidates[:10]:
            lines.append(f"  #{p.index + 1}  {detail}")
    return "\n".join(lines)
