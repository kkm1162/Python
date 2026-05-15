"""
O-RAN M-Plane 3.1.x 시험 스크립트 매핑 (로컬 ./conformance/*.sh).
GUI는 CONFORMANCE_SPEC_ROWS 순서로 표시하며, 로컬에 파일이 있는 항목만 보여 줍니다.
3.1.8.0 은 목록에 없음 — 3.1.8.x 실행 전 conformance_3180_init_user.sh 를 자동 실행합니다.
"""

from __future__ import annotations

# (sort_key, spec_ref, script_basename, section_title, description_en)
# sort_key: (3,1,1,1) → 3.1.1.1, 첫 항목이 3.1.1.1
# conformance_31101.sh → 3.1.10.1 (명시 매핑)
CONFORMANCE_SPEC_ROWS: tuple[tuple[tuple[int, int, int, int], str, str, str, str], ...] = (
    ((3, 1, 1, 1), "3.1.1.1", "conformance_3111.sh", "3.1.1 Transport and Handshake Test Scenarios", "Transport and Handshake in IPv4/SSH Environment (positive case)"),
    ((3, 1, 1, 2), "3.1.1.2", "conformance_3112.sh", "3.1.1 Transport and Handshake Test Scenarios", "Transport and Handshake in IPv4/SSH Environment (negative case)"),
    ((3, 1, 2, 1), "3.1.2.1", "conformance_3121.sh", "3.1.2 Manage Alarm Requests", "Subscription to Notifications"),
    ((3, 1, 3, 1), "3.1.3.1", "conformance_3131.sh", "3.1.3 M-Plane Connection Supervision", "M-Plane connection supervision (positive case)"),
    ((3, 1, 3, 2), "3.1.3.2", "conformance_3132.sh", "3.1.3 M-Plane Connection Supervision", "M-Plane Connection Supervision (negative case)"),
    ((3, 1, 4, 1), "3.1.4.1", "conformance_3141.sh", "3.1.4 Retrieval of O-RU's information elements", "Retrieval without Filter Applied"),
    ((3, 1, 4, 2), "3.1.4.2", "conformance_3142.sh", "3.1.4 Retrieval of O-RU's information elements", "Retrieval with filter applied"),
    ((3, 1, 5, 1), "3.1.5.1", "conformance_3151.sh", "3.1.5 Fault Management", "O-RU Alarm Notification Generation"),
    ((3, 1, 5, 2), "3.1.5.2", "conformance_3152.sh", "3.1.5 Fault Management", "Retrieval of Active Alarm List"),
    ((3, 1, 6, 1), "3.1.6.1", "conformance_3161.sh", "3.1.6 O-RU Software Update", "O-RU Software Update and Install (positive case)"),
    ((3, 1, 6, 2), "3.1.6.2", "conformance_3162.sh", "3.1.6 O-RU Software Update", "O-RU Software Update (negative case)"),
    ((3, 1, 7, 1), "3.1.7.1", "conformance_3170.sh", "3.1.7 O-RU Software Activation", "Software Activation without Reset"),
    ((3, 1, 8, 1), "3.1.8.1", "conformance_3181.sh", "3.1.8 Access Control", "Sudo on Hybrid M-plane Architecture (positive case)"),
    ((3, 1, 8, 2), "3.1.8.2", "conformance_3182.sh", "3.1.8 Access Control", "Access Control Sudo (negative case)"),
    ((3, 1, 8, 3), "3.1.8.3", "conformance_3183.sh", "3.1.8 Access Control", "Access Control NMS (negative case)"),
    ((3, 1, 8, 4), "3.1.8.4", "conformance_3184.sh", "3.1.8 Access Control", "Access Control FM-PM (negative case)"),
    ((3, 1, 8, 5), "3.1.8.5", "conformance_3185.sh", "3.1.8 Access Control", "Access Control SWM (negative case)"),
    ((3, 1, 8, 6), "3.1.8.6", "conformance_3186.sh", "3.1.8 Access Control", "Sudo on Hierarchical M-plane architecture (positive case)"),
    ((3, 1, 10, 1), "3.1.10.1", "conformance_31101.sh", "3.1.10 O-RU Configurability", "O-RU configurability test (positive case)"),
    ((3, 1, 10, 2), "3.1.10.2", "conformance_31102.sh", "3.1.10 O-RU Configurability", "O-RU Configurability Test (negative case)"),
    ((3, 1, 12, 1), "3.1.12.1", "conformance_31121.sh", "3.1.12 Log Management", "Log Management Test"),
    ((3, 1, 12, 2), "3.1.12.2", "conformance_31122.sh", "3.1.12 Log Management", "Trace Test"),
    ((3, 1, 13, 1), "3.1.13.1", "conformance_31131.sh", "3.1.13 Connectivity Check", "Ethernet Connectivity Monitoring"),
)

# 3.1.8.x 사전 단계 (목록 비표시). 3.1.8.0 항목은 삭제하고 이 스크립트로 대체.
CONFORMANCE_SCRIPT_PRE_3180 = "conformance_3180_init_user.sh"
CONFORMANCE_SCRIPTS_318X: frozenset[str] = frozenset(
    {f"conformance_318{i}.sh" for i in range(1, 7)}
)

# 하위 호환·스모크 등 (O-RAN 3.1 표와 무관 시 선택 표시용 — GUI 기본 목록에는 미포함)
CONFORMANCE_TESTS: tuple[tuple[str, str, str], ...] = (
    (
        "RU-smoke",
        "conformance_ru_netconf_tmp.sh",
        "Linux /var/tmp/conformance + /var/tmp/netconf_tmp smoke (GUI ORU JSON, paths under /var/tmp only)",
    ),
)

CONFORMANCE_REMOTE_DIR = "/var/tmp/conformance"
CONFORMANCE_REMOTE_GUI_CONFIG_NAME = "_gui_management_config.json"
