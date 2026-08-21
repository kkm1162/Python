"""
O-RAN MSG 심화 식별 · 캡처 진단 · 통계 · 휴리스틱 검증
"""

from __future__ import annotations

import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from oran_msg import (
    MSG1_MIN_PAYLOAD,
    MSG4_MAX_PAYLOAD,
    ORAN_MSG_LABELS,
    analyze_msg2_gap,
    collect_msg2_sequence_issues,
    format_msg2_gap_report,
    packet_oran_msg,
)

# eCPRI / O-RAN fronthaul UDP 포트 (일반적)
ECPRI_UDP_PORTS = {3148, 4789, 3152, 16384, 16385, 16386, 16387}

ORAN_VERSION_OFFSETS: dict[str, dict[str, int]] = {
    "1.0": {
        "oran_base": 8,
        "section_type_offset": 12,
        "direction_offset": 8,
    },
    "2.0": {
        "oran_base": 8,
        "section_type_offset": 12,
        "direction_offset": 9,
    },
    "3.0": {
        "oran_base": 8,
        "section_type_offset": 12,
        "direction_offset": 9,
        "compression_flag_offset": 9,
    },
}

MSG_LABEL_TO_NUM = {
    "MSG1": 1,
    "MSG2_DL": 2,
    "MSG2": 2,
    "MSG3_UL": 3,
    "MSG3": 3,
    "MSG4": 4,
    "MSG4_VARIANT": 4,
}

ANALYSIS_SAMPLE_LIMIT = 100_000


def get_msg_field_offsets(oran_version: str = "3.0") -> dict[str, int]:
    return dict(ORAN_VERSION_OFFSETS.get(oran_version, ORAN_VERSION_OFFSETS["3.0"]))


def _parse_ecpri_header(payload: bytes) -> dict[str, Any] | None:
    if len(payload) < 4:
        return None
    b0 = payload[0]
    version = (b0 >> 4) & 0x0F
    concat = b0 & 0x01
    msg_type = payload[1]
    payload_size = int.from_bytes(payload[2:4], byteorder="big") & 0x3FFF
    return {
        "version": version,
        "concat": concat,
        "msg_type": msg_type,
        "payload_size": payload_size,
        "header_valid": version in (0, 1, 2, 3, 4),
    }


def _oran_direction_from_byte(header_byte: int) -> tuple[bool, str]:
    """O-RAN dataDirection: bit7=0 → Uplink, bit7=1 → Downlink."""
    is_uplink = ((header_byte >> 7) & 1) == 0
    return is_uplink, ("Uplink" if is_uplink else "Downlink")


def parse_with_version_awareness(
    payload: bytes,
    oran_version: str = "3.0",
) -> dict[str, Any]:
    """버전별 오프셋으로 Section Type / Direction 추출 (폴백 파싱)."""
    offsets = get_msg_field_offsets(oran_version)
    out: dict[str, Any] = {"oran_version": oran_version, "parsed": False}
    st_off = offsets.get("section_type_offset", 12)
    dir_off = offsets.get("direction_offset", 9)
    if len(payload) > st_off:
        out["section_type"] = (payload[st_off] >> 4) & 0x0F
        out["parsed"] = True
    if len(payload) > dir_off:
        is_uplink, direction = _oran_direction_from_byte(payload[dir_off])
        out["is_uplink"] = is_uplink
        out["direction"] = "UL" if is_uplink else "DL"
    comp_off = offsets.get("compression_flag_offset")
    if comp_off is not None and len(payload) > comp_off:
        out["compression_hint"] = bool(payload[comp_off] & 0x40)
    return out


def advanced_oran_msg_identification(
    packet_data: bytes,
    *,
    oran_version: str = "3.0",
    packet_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    다층 필터링 + 신뢰도 점수 기반 O-RAN MSG 식별.
    packet_data: eCPRI 페이로드 (UDP payload) 또는 전체 프레임.
    """
    results: dict[str, Any] = {
        "msg_type": None,
        "msg_num": None,
        "confidence": 0,
        "debug_info": {},
    }
    info = packet_info or {}

    # 전체 프레임이면 eCPRI 오프셋 탐색
    payload = packet_data
    ecpri_off = info.get("ecpri_offset")
    if ecpri_off is not None and 0 <= ecpri_off < len(packet_data):
        payload = packet_data[ecpri_off:]
    elif len(packet_data) > 64:
        # UDP 페이로드 시작 휴리스틱: version nibble + msg type 패턴
        for i in range(min(80, len(packet_data) - 4)):
            hdr = _parse_ecpri_header(packet_data[i:])
            if hdr and hdr["header_valid"] and hdr["msg_type"] in (0, 1, 2, 3, 4, 5, 7):
                payload = packet_data[i:]
                results["debug_info"]["ecpri_search_offset"] = i
                break

    try:
        if len(payload) < 4:
            results["msg_type"] = "TOO_SHORT"
            return results

        hdr = _parse_ecpri_header(payload)
        if not hdr:
            results["msg_type"] = "NOT_ECPRI"
            return results

        version = hdr["version"]
        message_type = hdr["msg_type"]
        length = hdr["payload_size"]

        results["debug_info"].update({
            "ecpri_version": version,
            "ecpri_concat": hdr["concat"],
            "ecpri_msg_type": message_type,
            "payload_length": length,
            "total_bytes": len(payload),
        })

        if not hdr["header_valid"]:
            results["msg_type"] = "UNKNOWN_ECPRI_VERSION"
            results["confidence"] = 5
            return results

        results["confidence"] += 20

        # O-RAN 헤더 (표준 + 버전 폴백)
        version_parse = parse_with_version_awareness(payload, oran_version)
        section_type = version_parse.get("section_type")
        is_uplink = version_parse.get("is_uplink")

        # 표준 오프셋 (IQ/RT: PC_ID 4B 후 ORAN)
        if message_type in (0, 2) and len(payload) >= 12:
            oran = 8
            is_uplink, _ = _oran_direction_from_byte(payload[oran])
            if message_type == 0x02 and len(payload) > oran + 5:
                section_type = payload[oran + 5]
            elif len(payload) > oran + 4:
                section_word = int.from_bytes(payload[oran + 4 : oran + 6], "big")
                section_type = (section_word >> 12) & 0x0F
            results["debug_info"]["parse_path"] = "standard"

        if section_type is not None:
            results["debug_info"]["section_type"] = section_type
        if is_uplink is not None:
            results["debug_info"]["direction"] = "UL" if is_uplink else "DL"

        # [4단계] Message Type + Section + 길이
        if message_type == 0:
            if length >= 200 or (section_type is not None and 0 <= section_type <= 5):
                results["msg_type"] = "MSG1"
                results["msg_num"] = 1
                results["confidence"] += 40 if length >= MSG1_MIN_PAYLOAD else 25
            else:
                results["msg_type"] = "MSG1_SMALL"
                results["msg_num"] = 1
                results["confidence"] += 15
                results["debug_info"]["note"] = "소형 IQ — 비정상적일 수 있음"

        elif message_type == 2:
            results["confidence"] += 25
            if section_type == 6:
                results["confidence"] += 20
                if is_uplink is False:
                    results["msg_type"] = "MSG2_DL"
                    results["msg_num"] = 2
                    results["confidence"] += 15
                elif is_uplink is True:
                    results["msg_type"] = "MSG3_UL"
                    results["msg_num"] = 3
                    results["confidence"] += 15
                else:
                    results["msg_type"] = "MSG2_3_UNKNOWN_DIR"
                    results["confidence"] += 5
            else:
                results["msg_type"] = f"CPLANE_SEC_{section_type}"
                results["confidence"] += 10

        elif message_type == 4:
            if length <= MSG4_MAX_PAYLOAD:
                results["msg_type"] = "MSG4"
                results["msg_num"] = 4
                results["confidence"] += 50
            else:
                results["msg_type"] = "MSG4_VARIANT"
                results["msg_num"] = 4
                results["confidence"] += 30

        else:
            results["msg_type"] = f"UNKNOWN_TYPE_{message_type}"
            results["confidence"] += 5

        # packet_info 메타와 교차 검증
        if info.get("ecpri_type") is not None and info["ecpri_type"] == message_type:
            results["confidence"] = min(100, results["confidence"] + 10)
        if info.get("section_type") is not None and info["section_type"] == section_type:
            results["confidence"] = min(100, results["confidence"] + 10)

        results["confidence"] = min(100, results["confidence"])
        results["debug_info"]["confidence_score"] = results["confidence"]
        return results

    except Exception as exc:
        results["msg_type"] = "PARSE_ERROR"
        results["debug_info"]["error"] = str(exc)
        return results


def _advanced_from_metadata(p: Any, oran_version: str = "3.0") -> dict[str, Any]:
    """raw_bytes 없을 때 ParsedPacket 필드로 신뢰도 산출."""
    results: dict[str, Any] = {
        "msg_type": None,
        "msg_num": None,
        "confidence": 25,
        "debug_info": {"parse_path": "metadata_only", "oran_version": oran_version},
    }
    if not p.is_ecpri or p.ecpri_type is None:
        results["msg_type"] = "NOT_ECPRI"
        results["confidence"] = 0
        return results

    results["confidence"] += 15
    results["debug_info"]["ecpri_msg_type"] = p.ecpri_type
    results["debug_info"]["section_type"] = p.section_type
    pay = getattr(p, "ecpri_payload_size", 0) or p.length

    if p.ecpri_type == 0x00:
        results["msg_type"] = "MSG1"
        results["msg_num"] = 1
        results["confidence"] += 40 if pay >= 200 else 20
    elif p.ecpri_type == 0x02:
        results["confidence"] += 25
        if p.section_type == 6:
            results["confidence"] += 20
            if p.is_uplink is False:
                results["msg_type"] = "MSG2_DL"
                results["msg_num"] = 2
                results["confidence"] += 15
            elif p.is_uplink is True:
                results["msg_type"] = "MSG3_UL"
                results["msg_num"] = 3
                results["confidence"] += 15
            else:
                results["msg_type"] = "MSG2_3_UNKNOWN_DIR"
        else:
            results["msg_type"] = f"CPLANE_SEC_{p.section_type}"
    elif p.ecpri_type == 0x04:
        results["msg_type"] = "MSG4"
        results["msg_num"] = 4
        results["confidence"] += 45 if pay <= MSG4_MAX_PAYLOAD else 30
    else:
        results["msg_type"] = f"UNKNOWN_TYPE_{p.ecpri_type}"

    results["confidence"] = min(100, results["confidence"])
    results["debug_info"]["confidence_score"] = results["confidence"]
    strict_num, _ = packet_oran_msg(p)
    results["strict_msg_num"] = strict_num
    results["agreement"] = strict_num == results.get("msg_num") if strict_num and results.get("msg_num") else None
    return results


def advanced_identify_packet(
    p: Any,
    payload: bytes | None = None,
    *,
    oran_version: str = "3.0",
) -> dict[str, Any]:
    """ParsedPacket + 선택적 raw payload."""
    raw = payload or getattr(p, "raw_bytes", b"") or b""
    if not raw:
        return _advanced_from_metadata(p, oran_version)
    info = {
        "ecpri_offset": getattr(p, "ecpri_offset", None),
        "ecpri_type": p.ecpri_type,
        "section_type": p.section_type,
        "is_uplink": p.is_uplink,
        "length": p.length,
    }
    adv = advanced_oran_msg_identification(raw, oran_version=oran_version, packet_info=info)
    strict_num, _ = packet_oran_msg(p)
    adv["strict_msg_num"] = strict_num
    adv["agreement"] = strict_num == adv.get("msg_num") if strict_num and adv.get("msg_num") else None
    return adv


def _sample_packets(packets: list[Any], limit: int = ANALYSIS_SAMPLE_LIMIT) -> list[Any]:
    if len(packets) <= limit:
        return packets
    step = max(1, len(packets) // limit)
    return [packets[i] for i in range(0, len(packets), step)][:limit]


def analyze_capture_health(
    packets: list[Any],
    *,
    pcap_path: str | None = None,
    pcap_paths: list[str] | None = None,
    file_size: int | None = None,
) -> dict[str, Any]:
    """PCAP 캡처 품질 · 포트 · eCPRI 비율 진단."""
    total = len(packets)
    if total == 0:
        return {"total": 0, "issues": ["패킷 없음"], "checks": []}

    ecpri_n = sum(1 for p in packets if p.is_ecpri)
    ts_list = [p.timestamp for p in packets if p.timestamp]
    duration = (max(ts_list) - min(ts_list)) if len(ts_list) >= 2 else 0.0

    port_counter: Counter[tuple[int, int]] = Counter()
    ecpri_on_std_port = 0
    for p in packets:
        if not p.is_ecpri:
            continue
        sport = getattr(p, "udp_sport", None)
        dport = getattr(p, "udp_dport", None)
        if sport is not None and dport is not None:
            port_counter[(sport, dport)] += 1
            if sport in ECPRI_UDP_PORTS or dport in ECPRI_UDP_PORTS:
                ecpri_on_std_port += 1

    oran_hdr_n = sum(
        1 for p in packets
        if p.is_ecpri and p.frame_id is not None
    )

    issues: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    checks.append((
        "eCPRI 트래픽 존재",
        ecpri_n > 0,
        f"{ecpri_n:,}/{total:,} ({100 * ecpri_n / total:.1f}%)",
    ))
    if ecpri_n == 0:
        issues.append("eCPRI 패킷 없음 — NIC/미러링/포트 필터 확인")

    checks.append((
        "캡처 시간 ≥ 1초",
        duration >= 1.0,
        f"{duration:.2f}s",
    ))
    if duration < 1.0 and total > 100:
        issues.append("캡처 시간 짧음 — MSG 주기(1ms~) 놓칠 수 있음")

    if ecpri_n > 0:
        port_ok = ecpri_on_std_port > 0 or not port_counter
        checks.append((
            "eCPRI UDP 포트 (4789/16384-87 등)",
            port_ok,
            ", ".join(f"{s}→{d}({c})" for (s, d), c in port_counter.most_common(5)) or "포트 정보 없음",
        ))
        if port_counter and ecpri_on_std_port == 0:
            issues.append("비표준 UDP 포트 — VLAN/오프셋 또는 비-eCPRI 트래픽 가능")

    checks.append((
        "O-RAN 타이밍 헤더 파싱",
        oran_hdr_n > 0,
        f"{oran_hdr_n:,} 패킷",
    ))
    if ecpri_n > 0 and oran_hdr_n == 0:
        issues.append("eCPRI는 있으나 O-RAN 헤더 미파싱 — 버전/압축/VLAN 오프셋 의심")

    paths = pcap_paths or ([pcap_path] if pcap_path else [])
    paths_label = ""
    if len(paths) == 1:
        paths_label = paths[0]
    elif len(paths) > 1:
        names = [os.path.basename(p) for p in paths]
        paths_label = f"{len(paths)}개 파일: " + ", ".join(names[:5])
        if len(names) > 5:
            paths_label += f", … (+{len(names) - 5})"

    source_counts: Counter[str] = Counter()
    for p in packets:
        src = getattr(p, "pcap_source", "") or ""
        if src:
            source_counts[os.path.basename(src)] += 1

    if len(source_counts) > 1:
        checks.append((
            "다중 PCAP 병합",
            True,
            ", ".join(f"{k}({c:,})" for k, c in source_counts.most_common(8)),
        ))

    return {
        "total": total,
        "ecpri_count": ecpri_n,
        "oran_header_count": oran_hdr_n,
        "duration_sec": duration,
        "file_size": file_size,
        "pcap_path": pcap_path or (paths[0] if paths else None),
        "pcap_paths": paths,
        "pcap_paths_label": paths_label,
        "source_distribution": dict(source_counts.most_common(20)),
        "port_distribution": dict(port_counter.most_common(10)),
        "issues": issues,
        "checks": checks,
    }


def compute_msg_type_distribution(packets: list[Any]) -> dict[str, Any]:
    """eCPRI msg type · 식별 MSG · 페이로드 크기 분포."""
    ecpri_types: Counter[int] = Counter()
    identified: Counter[int] = Counter()
    size_by_type: dict[int, list[int]] = defaultdict(list)
    adv_by_label: Counter[str] = Counter()

    sample = _sample_packets(packets)
    for p in sample:
        if not p.is_ecpri:
            continue
        if p.ecpri_type is not None:
            ecpri_types[p.ecpri_type] += 1
            pay = getattr(p, "ecpri_payload_size", 0) or p.length
            if len(size_by_type[p.ecpri_type]) < 5000:
                size_by_type[p.ecpri_type].append(pay)
        m, _ = packet_oran_msg(p)
        if m is not None:
            identified[m] += 1
        adv = advanced_identify_packet(p, oran_version="3.0")
        lbl = adv.get("msg_type") or "?"
        adv_by_label[lbl] += 1

    def size_stats(vals: list[int]) -> dict[str, float]:
        if not vals:
            return {}
        return {
            "min": min(vals),
            "max": max(vals),
            "avg": round(statistics.mean(vals), 1),
            "median": round(statistics.median(vals), 1),
        }

    ecpri_names = {0: "IQ(0x00)", 2: "RT(0x02)", 4: "ACK(0x04)"}
    return {
        "sample_size": len(sample),
        "ecpri_type_counts": {
            ecpri_names.get(k, f"0x{k:02x}"): v for k, v in sorted(ecpri_types.items())
        },
        "identified_msg_counts": {f"Msg{k}": v for k, v in sorted(identified.items())},
        "advanced_label_counts": dict(adv_by_label.most_common(15)),
        "payload_size_stats": {
            ecpri_names.get(k, f"0x{k:02x}"): size_stats(v)
            for k, v in size_by_type.items()
        },
    }


def analyze_temporal_patterns(packets: list[Any]) -> dict[str, Any]:
    """MSG별 시간 간격 통계."""
    by_msg: dict[int, list[float]] = {1: [], 2: [], 3: [], 4: []}
    prev_ts: dict[int, float] = {}

    for p in _sample_packets(packets):
        m, _ = packet_oran_msg(p)
        if m is None or not p.timestamp:
            continue
        if m in prev_ts:
            dt = p.timestamp - prev_ts[m]
            if 0 < dt < 1.0 and len(by_msg[m]) < 10000:
                by_msg[m].append(dt)
        prev_ts[m] = p.timestamp

    patterns: dict[str, Any] = {}
    expected = {
        1: "1~2ms (고빈도 U-Plane)",
        2: "슬롯 단위 ~10-14ms",
        3: "슬롯 단위 ~10-14ms",
        4: "이벤트성 (불규칙)",
    }
    for m in (1, 2, 3, 4):
        intervals = by_msg[m]
        entry: dict[str, Any] = {
            "expected": expected[m],
            "sample_count": len(intervals),
        }
        if intervals:
            entry["average_interval_ms"] = round(statistics.mean(intervals) * 1000, 3)
            entry["std_deviation_ms"] = (
                round(statistics.stdev(intervals) * 1000, 3) if len(intervals) > 1 else 0
            )
        patterns[f"Msg{m}"] = entry
    return patterns


def final_validation_heuristics(packets: list[Any]) -> dict[str, Any]:
    """빈도·시퀀스 논리 휴리스틱."""
    labels: list[str] = []
    for p in _sample_packets(packets, 50_000):
        m, _ = packet_oran_msg(p)
        if m == 1:
            labels.append("MSG1")
        elif m == 2:
            labels.append("MSG2")
        elif m == 3:
            labels.append("MSG3")
        elif m == 4:
            labels.append("MSG4")

    n = len(labels) or 1
    c1 = labels.count("MSG1")
    c23 = labels.count("MSG2") + labels.count("MSG3") + labels.count("MSG4")

    rules = {
        "msg1_frequency": {
            "rule": "MSG1 비율 > 60%",
            "valid": (c1 / n) > 0.6 if labels else None,
            "detail": f"{100 * c1 / n:.1f}%",
        },
        "control_frequency": {
            "rule": "(MSG2+MSG3+MSG4) 비율 < 40%",
            "valid": (c23 / n) < 0.4 if labels else None,
            "detail": f"{100 * c23 / n:.1f}%",
        },
        "msg4_isolation": {
            "rule": "MSG4 연속 2회 이상 없음",
            "valid": not any(
                labels[i] == "MSG4" and i > 0 and labels[i - 1] == "MSG4"
                for i in range(len(labels))
            ) if labels else None,
            "detail": f"MSG4 {labels.count('MSG4')}건",
        },
        "msg2_absence": {
            "rule": "MSG2 없이 MSG3만 장기 지속 시 비정상",
            "valid": _check_msg2_absence(labels),
            "detail": _msg2_absence_detail(labels),
        },
    }
    return {"rules": rules, "sample_labels": n}


def _check_msg2_absence(labels: list[str]) -> bool | None:
    if not labels:
        return None
    streak = 0
    has_msg2 = "MSG2" in labels
    for lb in labels:
        if lb == "MSG3" and not has_msg2:
            streak += 1
            if streak > 50:
                return False
        elif lb != "MSG3":
            streak = 0
    return True


def _msg2_absence_detail(labels: list[str]) -> str:
    if "MSG2" in labels:
        return "MSG2 존재"
    if "MSG3" in labels:
        return "MSG2 없이 MSG3만 감지됨"
    return "MSG2/3 미감지"


def diagnose_missing_msg2(
    packets: list[Any],
    sequences: list[Any] | None = None,
) -> list[dict[str, str]]:
    """MSG2 부재 시나리오 진단."""
    seqs = sequences or []
    fail_msg2 = sum(1 for s in seqs if getattr(s, "failed_at_msg", None) == 2)
    ecpri_n = sum(1 for p in packets if p.is_ecpri)
    m1 = sum(1 for p in packets if packet_oran_msg(p)[0] == 1)
    m2 = sum(1 for p in packets if packet_oran_msg(p)[0] == 2)
    m3 = sum(1 for p in packets if packet_oran_msg(p)[0] == 3)
    rt_sec6 = sum(
        1 for p in packets
        if p.ecpri_type == 0x02 and p.section_type == 6 and p.is_uplink is False
    )
    rt_sec6_any = sum(
        1 for p in packets
        if p.ecpri_type == 0x02 and p.section_type == 6
    )
    gap = analyze_msg2_gap(packets, seqs)
    orphan_n = len(collect_msg2_sequence_issues(packets, seqs).orphan_msg2)

    causes = [
        {
            "id": "1",
            "title": "유휴 셀",
            "symptom": "MSG1도 없고 MSG2도 없음",
            "check": f"Msg1 후보 {m1}건, eCPRI {ecpri_n}건",
            "likely": m1 < 10 and ecpri_n < 50,
            "action": "활성 call 있는 셀에서 재캡처",
        },
        {
            "id": "2",
            "title": "캡처/필터 오류",
            "symptom": "MSG1/3/4는 많은데 MSG2만 없음",
            "check": (
                f"RT Sec6 DL raw {rt_sec6}건 (ST6 전체 {rt_sec6_any}건), "
                f"식별 Msg2 {m2}건, 거의 Msg2 {len(gap.hidden_candidates)}건"
            ),
            "likely": m1 > 100 and m2 == 0 and rt_sec6 > 0,
            "action": "eCPRI type 0x02 필터 제거 후 전체 확인",
        },
        {
            "id": "3",
            "title": "UL-Only 시나리오",
            "symptom": "MSG3만 보이고 MSG2 없음",
            "check": f"Msg3 {m3}건, Msg2 {m2}건",
            "likely": m3 > 10 and m2 == 0,
            "action": "DL 활성 시나리오 테스트",
        },
        {
            "id": "4",
            "title": "압축/헤더 오프셋",
            "symptom": "eCPRI는 있으나 Section 파싱 실패",
            "check": f"O-RAN timing 파싱 {sum(1 for p in packets if p.frame_id is not None)}건",
            "likely": ecpri_n > 0 and sum(1 for p in packets if p.frame_id is not None) == 0,
            "action": "압축 비활성화 또는 O-RAN 버전 확인",
        },
        {
            "id": "5",
            "title": "시퀀스 Msg2 실패",
            "symptom": "Msg2 타입 패킷은 있으나 시퀀스에 미포함",
            "check": (
                f"미포함 Msg2 {orphan_n}건, 시퀀스 Msg2 fail {fail_msg2}건, "
                f"ST6+DL {gap.rt_st6_dl}건 / Msg2식별 {gap.strict_msg2}건"
            ),
            "likely": (
                fail_msg2 > 0
                or orphan_n > 0
                or (rt_sec6 > m2)
                or (gap.rt_st6_dl > 0 and gap.strict_msg2 == 0)
                or m2 > sum(1 for p in packets if getattr(p, "rach_msg", None) == 2)
            ),
            "action": "슬롯 타이밍·버스트 누락 확인 — GUI 'Msg2≠' / 'S2실패' 바로가기",
        },
    ]
    return causes


def build_process_checklist(
    health: dict[str, Any],
    distribution: dict[str, Any],
    temporal: dict[str, Any],
    validation: dict[str, Any],
) -> list[tuple[str, bool | None, str]]:
    """5단계 분석 프로세스 체크리스트."""
    steps: list[tuple[str, bool | None, str]] = []
    steps.append((
        "1. eCPRI 트래픽 확인",
        health.get("ecpri_count", 0) > 0,
        f"{health.get('ecpri_count', 0):,}건",
    ))
    dist = distribution.get("ecpri_type_counts", {})
    steps.append((
        "2. 메시지 타입 분포",
        bool(dist),
        ", ".join(f"{k}:{v}" for k, v in list(dist.items())[:4]) or "—",
    ))
    t1 = temporal.get("Msg1", {})
    steps.append((
        "3. 시간 간격 (Msg1)",
        t1.get("sample_count", 0) > 10,
        f"평균 {t1.get('average_interval_ms', '—')}ms" if t1.get("sample_count") else "샘플 부족",
    ))
    rules = validation.get("rules", {})
    v1 = rules.get("msg1_frequency", {})
    steps.append((
        "4. MSG1 빈도 휴리스틱",
        v1.get("valid"),
        v1.get("detail", ""),
    ))
    v2 = rules.get("msg2_absence", {})
    steps.append((
        "5. MSG2 부재 검증",
        v2.get("valid"),
        v2.get("detail", ""),
    ))
    return steps


@dataclass
class AdvancedAnalysisReport:
    capture_health: dict[str, Any] = field(default_factory=dict)
    msg_distribution: dict[str, Any] = field(default_factory=dict)
    temporal: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    msg2_diagnosis: list[dict[str, Any]] = field(default_factory=list)
    msg2_gap_text: str = ""
    process_checklist: list[tuple[str, bool | None, str]] = field(default_factory=list)
    selected_packet: dict[str, Any] | None = None
    oran_version: str = "3.0"
    analyzed_at: str = ""
    packet_count: int = 0


def run_full_advanced_analysis(
    packets: list[Any],
    sequences: list[Any] | None = None,
    *,
    pcap_path: str | None = None,
    pcap_paths: list[str] | None = None,
    file_size: int | None = None,
    selected: Any | None = None,
    oran_version: str = "3.0",
) -> AdvancedAnalysisReport:
    from datetime import datetime

    health = analyze_capture_health(
        packets,
        pcap_path=pcap_path,
        pcap_paths=pcap_paths,
        file_size=file_size,
    )
    dist = compute_msg_type_distribution(packets)
    temporal = analyze_temporal_patterns(packets)
    validation = final_validation_heuristics(packets)
    msg2 = diagnose_missing_msg2(packets, sequences)
    gap = analyze_msg2_gap(packets, sequences)
    checklist = build_process_checklist(health, dist, temporal, validation)
    sel_adv = None
    if selected is not None:
        sel_adv = advanced_identify_packet(selected, oran_version=oran_version)
    return AdvancedAnalysisReport(
        capture_health=health,
        msg_distribution=dist,
        temporal=temporal,
        validation=validation,
        msg2_diagnosis=msg2,
        msg2_gap_text=format_msg2_gap_report(gap),
        process_checklist=checklist,
        selected_packet=sel_adv,
        oran_version=oran_version,
        analyzed_at=datetime.now().strftime("%H:%M:%S"),
        packet_count=len(packets),
    )


def format_report_section(title: str, lines: list[str]) -> str:
    return f"=== {title} ===\n" + "\n".join(lines) + "\n"


def format_advanced_report(report: AdvancedAnalysisReport) -> str:
    """텍스트 리포트 (보내기용)."""
    parts: list[str] = []
    h = report.capture_health
    parts.append(format_report_section("캡처 진단", [
        f"총 패킷: {h.get('total', 0):,}",
        f"eCPRI: {h.get('ecpri_count', 0):,}",
        f"캡처 시간: {h.get('duration_sec', 0):.2f}s",
        *[f"{'✓' if ok else '✗'} {name}: {detail}" for name, ok, detail in h.get("checks", [])],
        *[f"⚠ {x}" for x in h.get("issues", [])],
    ]))
    d = report.msg_distribution
    parts.append(format_report_section("MSG 분포", [
        f"샘플: {d.get('sample_size', 0):,}",
        *[f"  {k}: {v}" for k, v in d.get("ecpri_type_counts", {}).items()],
        *[f"  식별 {k}: {v}" for k, v in d.get("identified_msg_counts", {}).items()],
    ]))
    parts.append(format_report_section("시간 패턴", [
        *[f"  {k}: {v}" for k, v in report.temporal.items()],
    ]))
    val = report.validation.get("rules", {})
    parts.append(format_report_section("휴리스틱 검증", [
        *[f"  {'✓' if r.get('valid') else '✗' if r.get('valid') is False else '?'} {r['rule']} ({r.get('detail')})"
          for r in val.values()],
    ]))
    parts.append(format_report_section("MSG2 진단", [
        *[f"  [{c['id']}] {c['title']}: {c['check']} → {c['action']}" + (" ★" if c.get("likely") else "")
          for c in report.msg2_diagnosis],
    ]))
    if report.selected_packet:
        sp = report.selected_packet
        parts.append(format_report_section("선택 패킷 심화 식별", [
            f"  타입: {sp.get('msg_type')} (신뢰도 {sp.get('confidence')}%)",
            f"  strict: Msg{sp.get('strict_msg_num')}",
            f"  debug: {sp.get('debug_info')}",
        ]))
    return "\n".join(parts)


def get_advanced_criteria_rows(p: Any, oran_version: str = "3.0") -> list[tuple[str, str, str, str]]:
    """판단 조건 탭 — 심화 식별 행."""
    if p is None:
        return []
    adv = advanced_identify_packet(p, oran_version=oran_version)
    rows: list[tuple[str, str, str, str]] = []
    conf = adv.get("confidence", 0)
    tag = "pass" if conf >= 70 else ("fail" if conf < 40 else "neutral")
    rows.append(("심화", f"신뢰도 {conf}% → {adv.get('msg_type', '?')}", "✓" if conf >= 70 else "?", tag))
    strict = adv.get("strict_msg_num")
    agree = adv.get("agreement")
    if strict and adv.get("msg_num"):
        mark = "✓" if agree else "✗"
        rows.append(("교차", f"strict Msg{strict} vs 심화 Msg{adv.get('msg_num')}", mark, "pass" if agree else "fail"))
    dbg = adv.get("debug_info", {})
    if dbg.get("note"):
        rows.append(("참고", str(dbg["note"]), "—", "neutral"))
    return rows
