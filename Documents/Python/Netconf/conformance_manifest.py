"""
O-RAN M-Plane 3.1.x 시험 스크립트 매핑 (로컬 ./conformance/*.sh).
GUI는 CONFORMANCE_SPEC_ROWS 순서로 표시하며, 로컬에 파일이 있는 항목만 보여 줍니다.
3.1.8.0 / 3.1.8.0_1 은 목록에 없음 — 3180 은 3.1.8.1 직전(실패 시 3180_1 재시도), 3180_1 은 3.1.8.6 종료·시험 중지 후 자동 실행합니다.
3.1.8.x(3.1.8.1–3.1.8.6)는 GUI에서 하나만 선택해도 전체가 연동 선택·일괄 실행됩니다.
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
    ((3, 1, 6, 2), "3.1.6.2", "conformance_3162.sh", "3.1.6 O-RU Software Update", "O-RU Software Update (negative case)"),
    ((3, 1, 6, 1), "3.1.6.1", "conformance_3161.sh", "3.1.6 O-RU Software Update", "O-RU Software Update and Install (positive case)"),
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
CONFORMANCE_SCRIPT_POST_3180_1 = "conformance_3180_1_init_user.sh"
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

# 표 참조(3.1.x.x) → 상세 설명창 [시험 설명] (기술 문서 톤, 쉬운 표현)
CONFORMANCE_SPEC_DESCRIPTIONS_KO: dict[str, str] = {
    "3.1.1.1": (
        "3.1.1.1 (Call Home 연결): O-RU가 서버로 역방향(Call Home) SSH·NETCONF 세션을 "
        "수립하고, 허용된 계정으로 인증에 성공하는지 확인합니다."
    ),
    "3.1.1.2": (
        "3.1.1.2 (인증 실패): 잘못된 ID/비밀번호로 Call Home 시 인증이 거부되고 "
        "NETCONF 세션이 수립되지 않는지 확인하는 부정(Negative) 시험입니다."
    ),
    "3.1.2.1": (
        "3.1.2.1 (이벤트 구독): Call Home 로그인 후 NETCONF notification "
        "구독(create-subscription)이 정상 응답(OK)으로 완료되는지 확인합니다."
    ),
    "3.1.3.1": (
        "3.1.3.1 (연결 감시·정상): O-RU supervision notification 수신 시 "
        "watchdog-reset RPC로 응답하여 세션이 유지되는지 확인하는 정(Positive) 시험입니다."
    ),
    "3.1.3.2": (
        "3.1.3.2 (연결 감시·부정): watchdog-reset을 의도적으로 중단했을 때 O-RU가 "
        "supervision 실패를 감지하고 세션을 종료하는지 확인하는 부정(Negative) 시험입니다."
    ),
    "3.1.4.1": (
        "3.1.4.1 (YANG 라이브러리·전체): 필터 없이 yang-library 등을 조회하여 "
        "구현 모듈·revision이 O-RAN M-Plane 요구사항과 일치하는지 확인합니다."
    ),
    "3.1.4.2": (
        "3.1.4.2 (필터 조회): subtree/xpath 필터로 지정한 노드만 반환되고 "
        "요청 범위 밖 데이터가 포함되지 않는지 확인합니다."
    ),
    "3.1.5.1": (
        "3.1.5.1 (알람 notification): 외부 장비(예: L2 스위치)로 링크 장애·복구를 유발한 뒤 "
        "alarm notification(Occur/Clear)이 NETCONF로 수신되는지 확인합니다."
    ),
    "3.1.5.2": (
        "3.1.5.2 (활성 알람 조회): 알람이 활성화된 상태에서 active-alarm-list "
        "조회 시 해당 알람이 응답에 포함되는지 확인합니다."
    ),
    "3.1.6.1": (
        "3.1.6.1 (S/W 다운로드·설치): 원격 SFTP의 정상 패키지를 download·install하여 "
        "non-running 슬롯 상태가 VALID로 보고되는지 확인합니다. "
        "Conformance 설정(⚙)에서 3.1.6.1 전용 PKG를 지정합니다."
    ),
    "3.1.6.2": (
        "3.1.6.2 (S/W 설치 부정): download 후 install 시 INTEGRITY_ERROR 등으로 "
        "설치가 거부되는지 확인합니다. 3.1.6.2 설정(⚙)에서 3.1.6.1과 다른 PKG(손상·부정용)를 지정합니다."
    ),
    "3.1.7.1": (
        "3.1.7.1 (S/W 활성화): install 완료 슬롯에 activate를 수행했을 때 "
        "재부팅 없이 active 슬롯으로 전환되는지 확인합니다."
    ),
    "3.1.8.0-prep": (
        "3.1.8.0 (NACM 초기화): oranuser(Call Home)·pre 모드로 NACM 정리. "
        "실패 시 3.1.8.0_1(sudouser, 동일 로직)으로 재시도합니다."
    ),
    "3.1.8.0.1-prep": (
        "3.1.8.0_1: 3.1.8.0 과 동일 스크립트·NACM 처리, Call Home만 sudouser. "
        "pre=3.1.8.1 직전, post=3.1.8.6 종료·중지 후(GUI가 mode 지정)."
    ),
    "3.1.8.1": (
        "3.1.8.1 (계정·NACM 생성): sudo 권한으로 nmsuser·fmpmuser 등 역할별 계정을 "
        "o-ran-usermgmt에 생성하고 NACM 그룹에 올바르게 등록되는지 확인하는 정(Positive) 시험입니다."
    ),
    "3.1.8.2": (
        "3.1.8.2 (비밀번호 노출 차단): sudouser로 user-mgmt 조회 시 password 노드가 "
        "응답에 노출되지 않는지 확인하는 부정(Negative) 시험입니다."
    ),
    "3.1.8.3": (
        "3.1.8.3 (NMS 권한 부정): nmsuser로 사용자 생성 등 권한 밖 edit 시 "
        "error-tag access-denied가 반환되는지 확인합니다."
    ),
    "3.1.8.4": (
        "3.1.8.4 (FM-PM 권한 부정): fmpmuser로 인터페이스(VLAN·MAC 등) 변경 edit 시 "
        "access-denied로 거부되는지 확인합니다."
    ),
    "3.1.8.5": (
        "3.1.8.5 (SWM 권한 부정): swmuser로 FM 알람 목록 등 권한 밖 데이터 조회 시 "
        "허용 범위를 벗어난 정보가 반환되지 않는지 확인합니다."
    ),
    "3.1.8.6": (
        "3.1.8.6 (계층 계정·다중 세션): sudouser로 oranuser2·oranuser3를 추가하고 "
        "각 계정 Call Home·NACM 그룹이 기대와 일치하는지 확인하는 정(Positive) 시험입니다."
    ),
    "3.1.10.1": (
        "3.1.10.1 (U-Plane 설정): CU-Plane·processing-element·PDSCH/PUSCH/PRACH 등 "
        "U-Plane 구성이 적용되고 상태가 ACTIVE로 전환되는지 확인하는 정(Positive) 시험입니다."
    ),
    "3.1.10.2": (
        "3.1.10.2 (U-Plane 설정 부정): CC 간 eAxC-ID 중복 등 잘못된 구성 시 "
        "O-RU가 오류를 보고하고 설정을 거부하는지 확인하는 부정(Negative) 시험입니다."
    ),
    "3.1.12.1": (
        "3.1.12.1 (트러블슈팅 로그): troubleshooting 로그 생성·원격(SFTP) 업로드 "
        "절차가 완료되는지 확인합니다."
    ),
    "3.1.12.2": (
        "3.1.12.2 (트레이스 로그): trace 수집 시작·중지 시 생성된 로그 파일이 "
        "순차적으로 원격 서버에 업로드되는지 확인합니다."
    ),
    "3.1.13.1": (
        "3.1.13.1 (이더넷 연결 모니터링): Ethernet connectivity monitoring(LBM/LBR) "
        "요청·응답으로 링크 건전성을 확인합니다."
    ),
}
