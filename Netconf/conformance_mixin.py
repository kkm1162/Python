"""Conformance tab: remote SSH upload/run, ORU JSON merge, /var/tmp paths — mixed into CallhomeGUI."""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk
from typing import Any

import conformance_manifest as _conf_manifest

_ALARM_TEST_FIELDS: list[dict[str, Any]] = [
    {
        "key": "l2sw_ip",
        "label": "L2SW IP",
        "default": "",
        "hint": "L2 스위치 IP 주소",
        "env_var": "L2SW_IP",
        "wide": False,
    },
    {
        "key": "l2sw_id",
        "label": "L2SW ID",
        "default": "",
        "hint": "L2 스위치 로그인 ID",
        "env_var": "L2SW_ID",
        "wide": False,
    },
    {
        "key": "l2sw_pw",
        "label": "L2SW PW",
        "default": "",
        "hint": "L2 스위치 로그인 PW",
        "env_var": "L2SW_PW",
        "wide": False,
    },
    {
        "key": "alarm_off_cmds",
        "label": "OFF 명령어",
        "default": "",
        "hint": "쉼표+공백(, ) 구분, 순차 실행",
        "env_var": "ALARM_OFF_CMDS",
        "wide": True,
    },
    {
        "key": "alarm_on_cmds",
        "label": "ON 명령어",
        "default": "",
        "hint": "쉼표+공백(, ) 구분, 순차 실행",
        "env_var": "ALARM_ON_CMDS",
        "wide": True,
    },
    {
        "key": "alarm_timeout_sec",
        "label": "Timeout(초)",
        "default": "300",
        "hint": "notification 대기 시간",
        "env_var": "ALARM_TIMEOUT_SEC",
        "wide": False,
    },
]

_SWM_ACTIVATE_GUARD_FIELDS: list[dict[str, Any]] = [
    {
        "key": "activate_get_guard_sec",
        "label": "Activate→GET guard (sec)",
        "default": "5",
        "hint": "software-activate 완료 후 active 슬롯 GET 전 대기(초). 반영이 느린 RU는 10~30 권장",
        "env_var": "ACTIVATE_GET_GUARD_SEC",
        "wide": False,
    },
]

_SWM_TEST_FIELDS: list[dict[str, Any]] = [
    {
        "key": "swm_pkg_path",
        "label": "PKG 파일 (로컬)",
        "default": "",
        "hint": "로컬 PC의 SW 패키지 (.EXT / .zip)",
        "env_var": None,
        "wide": True,
        "file_picker": True,
        "file_picker_title": "SW 패키지 선택 (EXT / ZIP)",
        "file_types": [
            ("EXT / ZIP package", "*.EXT *.ext *.zip *.ZIP"),
            ("ZIP files", "*.zip *.ZIP"),
            ("EXT files", "*.EXT *.ext"),
            ("All files", "*.*"),
        ],
    },
    {
        "key": "swm_server_ip",
        "label": "Server IP",
        "default": "",
        "hint": "O-RU가 다운로드할 SFTP 서버 IP (보통 LOCAL_IP와 동일)",
        "env_var": "SWM_SERVER_IP",
        "wide": False,
    },
    {
        "key": "swm_server_id",
        "label": "Server ID",
        "default": "root",
        "hint": "SFTP 서버 로그인 ID",
        "env_var": "SWM_SERVER_ID",
        "wide": False,
    },
    {
        "key": "swm_server_pw",
        "label": "Server PW",
        "default": "",
        "hint": "SFTP 서버 로그인 PW",
        "env_var": "SWM_SERVER_PW",
        "wide": False,
    },
]


def _swm_test_fields(pkg_hint: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in _SWM_TEST_FIELDS:
        nf = dict(f)
        if nf.get("key") == "swm_pkg_path":
            nf["hint"] = pkg_hint
        out.append(nf)
    return out


_SWM_FIELDS_3161 = _swm_test_fields("3.1.6.1용 정상 SW 패키지 (valid PKG)")
_SWM_FIELDS_3162 = _swm_test_fields(
    "3.1.6.2용 부정 시험 PKG (손상·무결성 오류 유발, 3161과 다른 파일 권장)"
)

_IFACE_TO_DU_FIELDS: list[dict[str, Any]] = [
    {
        "key": "to_du_if_name",
        "label": "To-DU interface (O-RU NETCONF)",
        "default": "",
        "hint": "O-RU YANG interface 이름 (NETCONF GET). 보통 sys.",
        "env_var": None,
        "wide": False,
    },
    {
        "key": "to_du_vlan",
        "label": "VLAN ID",
        "default": "1",
        "hint": "O-RU vlan-id / ethping -v",
        "env_var": None,
        "wide": False,
    },
]

_IFACE_ODU_MAC_FIELD: list[dict[str, Any]] = [
    {
        "key": "odu_mac",
        "label": "O-DU MAC",
        "default": "",
        "hint": "processing-element o-du-mac-address. 비우면 Settings M-Plane O-DU MAC",
        "env_var": None,
        "wide": False,
    },
]

_IFACE_TEST_FIELDS: list[dict[str, Any]] = _IFACE_TO_DU_FIELDS + _IFACE_ODU_MAC_FIELD

# 3.1.13.1 LBM only — 3.1.10.x (31101/31102)는 M-Plane Excel만 사용
_LBM31131_FIELDS: list[dict[str, Any]] = [
    {
        "key": "server_nic",
        "label": "Server NIC (miniDU, ethping -i)",
        "default": "",
        "hint": "miniDU fronthaul (예: dasan). 비우면 Settings → Server NIC. VLAN은 아래 VLAN ID.",
        "env_var": None,
        "wide": False,
    },
] + _IFACE_TEST_FIELDS

_CONFORMANCE_LBM_SCRIPT = "conformance_31131.sh"

_CONFORMANCE_INTERFACE_SHARED_KEY = "conformance_interface_shared"
_CONFORMANCE_3110X_SHARED_KEY = "conformance_3110x_shared"
_CONFORMANCE_318X_SHARED_KEY = "conformance_318x_shared"
_CONFORMANCE_3112X_SHARED_KEY = "conformance_3112x_shared"

_318X_TEST_FIELDS: list[dict[str, Any]] = [
    {
        "key": "conf_v11_mode",
        "label": "Conformance 사양",
        "default": "after",
        "hint": "3.1.8.1 / 3180 사전 단계의 oranuser@o-ran.org NACM 기대값",
        "wide": True,
        "choices": [
            ("before", "v11.0 이전 — oranuser@o-ran.org 미포함"),
            ("after", "v11.0 이후 — oranuser@o-ran.org 포함"),
        ],
    },
] + _IFACE_TO_DU_FIELDS

_CONFORMANCE_ORU_BOOST_SCRIPT = "oru_show_system_boost.sh"
_CONFORMANCE_3112X_SCRIPTS: frozenset[str] = frozenset(
    {"conformance_31121.sh", "conformance_31122.sh"}
)

_LOG_3112X_FIELDS: list[dict[str, Any]] = [
    {
        "key": "oru_log_boost_enable",
        "label": "O-RU log 부스트",
        "default": "0",
        "hint": "start-trace 직후부터 1초 간격 show system (켜면 trace가 빨리 끝나 last=true 조기 가능). 수동 시험과 동일하게 하려면 끄기",
        "choices": [
            ("1", "사용"),
            ("0", "사용 안 함"),
        ],
    },
    {
        "key": "oru_cli_id",
        "label": "O-RU SSH ID",
        "default": "",
        "hint": "비우면 Settings → CLI-ID",
        "env_var": None,
        "wide": False,
    },
    {
        "key": "oru_cli_pw",
        "label": "O-RU SSH PW",
        "default": "",
        "hint": "비우면 Settings → CLI-PW",
        "env_var": None,
        "wide": False,
        "password": True,
    },
    {
        "key": "file_server_ip",
        "label": "SFTP server IP",
        "default": "",
        "hint": "RU가 업로드할 SFTP 호스트 (비우면 LOCAL_IP)",
        "env_var": None,
        "wide": False,
    },
    {
        "key": "file_server_id",
        "label": "SFTP user ID",
        "default": "solid",
        "hint": "remote-file-path: sftp://ID@host/...",
        "env_var": None,
        "wide": False,
    },
    {
        "key": "file_server_pw",
        "label": "SFTP password",
        "default": "",
        "hint": "file-upload password (ACORN: nested leaf)",
        "env_var": None,
        "wide": False,
    },
    {
        "key": "local_log_prefix",
        "label": "RU local log prefix",
        "default": "O-RAN/log",
        "hint": "local-logical-file-path = {prefix}/{notification 파일명}",
        "env_var": None,
        "wide": True,
    },
    {
        "key": "remote_upload_dir",
        "label": "SFTP save directory",
        "default": "/tmp",
        "hint": "remote-file-path 및 PASS 시 파일 존재 확인 경로 (miniDU)",
        "env_var": None,
        "wide": True,
    },
]

_CONFORMANCE_INTERFACE_SCRIPTS: frozenset[str] = frozenset(
    {
        "conformance_3184.sh",
        "conformance_31131.sh",
    }
)

_CONFORMANCE_MPLANE_SCRIPTS: frozenset[str] = frozenset(
    {
        "conformance_31101.sh",
        "conformance_31102.sh",
    }
)

_CONFORMANCE_HELPER_SCRIPTS: tuple[str, ...] = (
    "conformance_mplane_xlsx_common.sh",
    "conformance_315x_common.sh",
    "conformance_netpeer_uplane_init.sh",
    "conformance_oru_reboot.sh",
    "conformance_oru_reboot_v6.sh",
)

_CONFORMANCE_315X_SCRIPTS: frozenset[str] = frozenset(
    {
        "conformance_3151.sh",
        "conformance_3152.sh",
    }
)

_CONFORMANCE_315X_COMMON = "conformance_315x_common.sh"

# Static XML only — do not import mplane_conformance here (pulls openpyxl).
_MPLANE_REMOTE_TEMPLATE_DIR = "/var/tmp/conformance/mplane_templates"

# netopeer2-cli scripts that source conformance_netpeer_uplane_init.sh + mplane_templates
_CONFORMANCE_NETPEER_UPLANE_INIT_SCRIPTS: frozenset[str] = frozenset(
    {
        _CONFORMANCE_LBM_SCRIPT,
    }
)

_MPLANE_3110X_FIELDS: list[dict[str, Any]] = [
    {
        "key": "mplane_xlsx_path",
        "label": "M-Plane Excel (.xlsx)",
        "default": "",
        "hint": "Control-Sheet + PDSCH/PUSCH/PRACH/ACTIVE. To-DU interface·VLAN·O-DU MAC은 Excel에서 로드.",
        "env_var": None,
        "wide": True,
        "file_picker": True,
        "file_types": [("Excel workbook", "*.xlsx"), ("All files", "*.*")],
    },
]

_CONFORMANCE_PER_TEST_SCHEMA: dict[str, dict[str, Any]] = {
    "conformance_3131.sh": {
        "title": "3.1.3.1 M-Plane Supervision (positive)",
        "fields": [
            {
                "key": "supervision_cycles",
                "label": "Supervision 반복 횟수",
                "default": "30",
                "hint": "알림+watchdog N회 유지 후 PASS (빈칸이면 상단 SUPERVISION_RESET_CYCLES)",
                "env_var": "SUPERVISION_NEEDED",
            },
        ],
    },
    "conformance_3132.sh": {
        "title": "3.1.3.2 M-Plane Supervision (negative)",
        "fields": [
            {
                "key": "supervision_cycles",
                "label": "Watchdog 유지 횟수",
                "default": "30",
                "hint": "N회 watchdog 후 중단 → RU 세션 실패(EOF) 유도. 빈칸이면 SUPERVISION_RESET_CYCLES",
                "env_var": "SUPERVISION_NEEDED",
            },
            {
                "key": "post_reset_wait_sec",
                "label": "시험 후 ORU 리셋 대기(초)",
                "default": "360",
                "hint": "ORU 재부팅 후 Call Home 대기 (연속 실행 시). 비우면 상단「재부팅 대기(초)」사용",
                "env_var": None,
            },
        ],
    },
    "conformance_3151.sh": {
        "title": "3.1.5.1 Alarm Notification",
        "shared_with": "conformance_3152.sh",
        "fields": _ALARM_TEST_FIELDS,
    },
    "conformance_3152.sh": {
        "title": "3.1.5.2 Active Alarm List",
        "shared_with": "conformance_3151.sh",
        "fields": _ALARM_TEST_FIELDS,
    },
    "conformance_3161.sh": {
        "title": "3.1.6.1 O-RU Software Update (positive)",
        "fields": _SWM_FIELDS_3161,
    },
    "conformance_3162.sh": {
        "title": "3.1.6.2 O-RU Software Update (negative)",
        "fields": _SWM_FIELDS_3162,
    },
    "conformance_3170.sh": {
        "title": "3.1.7.1 Software Activation (no reset)",
        "fields": _SWM_ACTIVATE_GUARD_FIELDS,
    },
    "conformance_31101.sh": {
        "title": "3.1.10.1 U-Plane (M-Plane xlsx)",
        "settings_key": _CONFORMANCE_3110X_SHARED_KEY,
        "shared_with": "conformance_31102.sh",
        "fields": _MPLANE_3110X_FIELDS,
    },
    "conformance_31102.sh": {
        "title": "3.1.10.2 U-Plane (M-Plane xlsx, duplicate eAxC)",
        "settings_key": _CONFORMANCE_3110X_SHARED_KEY,
        "shared_with": "conformance_31101.sh",
        "fields": _MPLANE_3110X_FIELDS,
    },
    "conformance_3181.sh": {
        "title": "3.1.8.x 공통 (Access Control)",
        "settings_key": _CONFORMANCE_318X_SHARED_KEY,
        "shared_with": "conformance_3186.sh",
        "fields": _318X_TEST_FIELDS,
    },
    "conformance_3182.sh": {
        "title": "3.1.8.x 공통 (Access Control)",
        "settings_key": _CONFORMANCE_318X_SHARED_KEY,
        "shared_with": "conformance_3181.sh",
        "fields": _318X_TEST_FIELDS,
    },
    "conformance_3183.sh": {
        "title": "3.1.8.x 공통 (Access Control)",
        "settings_key": _CONFORMANCE_318X_SHARED_KEY,
        "shared_with": "conformance_3181.sh",
        "fields": _318X_TEST_FIELDS,
    },
    "conformance_3184.sh": {
        "title": "3.1.8.x 공통 (Access Control)",
        "settings_key": _CONFORMANCE_318X_SHARED_KEY,
        "shared_with": "conformance_3181.sh",
        "fields": _318X_TEST_FIELDS,
    },
    "conformance_3185.sh": {
        "title": "3.1.8.x 공통 (Access Control)",
        "settings_key": _CONFORMANCE_318X_SHARED_KEY,
        "shared_with": "conformance_3181.sh",
        "fields": _318X_TEST_FIELDS,
    },
    "conformance_3186.sh": {
        "title": "3.1.8.x 공통 (Access Control)",
        "settings_key": _CONFORMANCE_318X_SHARED_KEY,
        "shared_with": "conformance_3181.sh",
        "fields": _318X_TEST_FIELDS,
    },
    "conformance_31121.sh": {
        "title": "3.1.12.1 Log Management",
        "settings_key": _CONFORMANCE_3112X_SHARED_KEY,
        "shared_with": "conformance_31122.sh",
        "fields": _LOG_3112X_FIELDS,
    },
    "conformance_31122.sh": {
        "title": "3.1.12.2 Trace",
        "settings_key": _CONFORMANCE_3112X_SHARED_KEY,
        "shared_with": "conformance_31121.sh",
        "fields": _LOG_3112X_FIELDS
        + [
            {
                "key": "trace_oru_end_timeout_sec",
                "label": "ORU trace 종료 대기(초)",
                "default": "600",
                "hint": "GUI는 stop-trace 미전송. O-RU가 스스로 last=true 보낼 때까지 대기(초).",
                "env_var": "TRACE_ORU_END_TIMEOUT",
                "wide": False,
            },
        ],
    },
    "conformance_31131.sh": {
        "title": "3.1.13.1 Connectivity (To-DU interface)",
        "settings_key": _CONFORMANCE_INTERFACE_SHARED_KEY,
        "fields": _LBM31131_FIELDS,
    },
}


def _conformance_bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


@dataclass(frozen=True)
class ConformanceRunOptions:
    """Remote run parameters; ORU JSON is rebuilt from Settings at upload/run time."""

    remote_dir: str
    netconf_rpc_timeout: str
    netconf_idle_timeout: str
    supervision_interval: str
    supervision_reset_cycles: str
    supervision_negative_fail_on_cycle: str
    conn_delay: str
    post_listen_wait_sec: str


class ConformanceMixin:
    """Tk methods mixed into CallhomeGUI (expects self.fields, self.after, self.append_log, …)."""

    def _conformance_scripts_318x(self) -> frozenset[str]:
        return getattr(_conf_manifest, "CONFORMANCE_SCRIPTS_318X", frozenset())

    def _conformance_ordered_318x_local(self) -> list[str]:
        """표 순서의 3.1.8.1–3.1.8.6 (로컬에 있는 항목만)."""
        three8 = self._conformance_scripts_318x()
        out: list[str] = []
        for r in getattr(_conf_manifest, "CONFORMANCE_SPEC_ROWS", ()) or ():
            if len(r) >= 3 and r[2] in three8 and self._conformance_script_local_path(r[2]) is not None:
                out.append(r[2])
        return out

    def _conformance_expand_run_list(self, fnames: list[str]) -> list[str]:
        """3.1.8.x 중 하나라도 선택되면 3.1.8.1–3.1.8.6 전체를 실행 목록에 넣는다."""
        suite = self._conformance_ordered_318x_local()
        if not suite:
            return fnames
        suite_set = set(suite)
        if not any(f in suite_set for f in fnames):
            return fnames
        out: list[str] = []
        inserted = False
        for f in fnames:
            if f in suite_set:
                if not inserted:
                    out.extend(suite)
                    inserted = True
            elif f not in out:
                out.append(f)
        return out

    def _conformance_set_318x_linked_check(self, value: bool) -> None:
        """3.1.8.1–3.1.8.6 체크박스를 동일 상태로 맞춘다."""
        if getattr(self, "_conformance_318x_link_busy", False):
            return
        self._conformance_318x_link_busy = True
        try:
            for sn in self._conformance_ordered_318x_local():
                bv = self.conformance_check_vars.get(sn)
                if bv is not None:
                    bv.set(value)
        finally:
            self._conformance_318x_link_busy = False

    def _conformance_gui_script_order(self) -> list[str]:
        """Conformance 표에 보이는 순서(위→아래). 실행·일부 선택 모두 이 순서를 따른다."""
        return [fname for fname, _ref, _en in self._conformance_test_rows()]

    def _conformance_order_run_list(self, fnames: list[str]) -> list[str]:
        """선택된 항목만 GUI 표 순서(위→아래)로 정렬."""
        gui_order = self._conformance_gui_script_order()
        fn_set = set(fnames)
        ordered = [f for f in gui_order if f in fn_set]
        tail = [f for f in fnames if f not in set(gui_order)]
        return ordered + tail

    def _conformance_is_318x_script(self, fname: str) -> bool:
        pre = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")
        return fname in self._conformance_scripts_318x() or fname == pre

    def _conformance_settings_store_key(self, fname: str) -> str:
        schema = _CONFORMANCE_PER_TEST_SCHEMA.get(fname)
        if schema and schema.get("settings_key"):
            return str(schema["settings_key"])
        return fname

    def _conformance_3180_script_path(self, *, post_cleanup: bool = False) -> str:
        """3.1.8.0 NACM 스크립트. 사후 정리용 3180_1 이 없으면 3180_init_user 를 사용."""
        if post_cleanup:
            post1 = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_POST_3180_1", "")
            if post1 and self._conformance_script_local_path(post1):
                return post1
        pre = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")
        return pre

    def _conformance_run_3180_step(
        self,
        client: Any,
        sftp: Any,
        script_b: str,
        opts: ConformanceRunOptions,
        remote_dir: str,
        cfg_remote: str,
        spec_map: dict[str, str],
        log_line: Any,
        phase: str,
        *,
        abort_suite_on_fail: bool = False,
        force_despite_cancel: bool = False,
        anchor_fname: str = "",
    ) -> tuple[int | None, bool]:
        """3.1.8.0(conformance_3180_init_user.sh 등) 실행. (rc, abort_suite)."""
        if not script_b:
            return None, False
        lp = self._conformance_script_local_path(script_b)
        if not lp:
            log_line(f"WARN: 3.1.8.0 스크립트 없음 ({script_b}), 단계를 건너뜁니다.")
            return None, False

        phase_labels = {
            "pre_3181": "3.1.8.1 직전",
            "pre_3181_fallback": "3.1.8.1 직전 (3180_1 대체)",
            "post_3186": "3.1.8.6 종료 후",
            "post_stop": "3.1.8.x 중지·미완료 후",
        }
        label = phase_labels.get(phase, phase)

        restore_cancel = False
        if force_despite_cancel and self._conformance_cancel_event.is_set():
            self._conformance_cancel_event.clear()
            restore_cancel = True

        log_line(f"---- 3.1.8.0 → {script_b} ({label}) ----")
        host = self._conformance_host_run_log_path(script_b)
        self._conformance_active_host_log = host
        self._conformance_last_host_log = host
        self.after(0, self._refresh_log_target_hint_line)
        self._conformance_detail_lines[script_b] = []
        self._conformance_detail_capture_key = script_b
        self._conformance_detail_run_started_wall[script_b] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conformance_detail_run_started_mono[script_b] = time.monotonic()
        spec_ref = spec_map.get(script_b, "3.1.8.0-prep")
        rc: int
        try:
            rc = self._conformance_exec_remote_script(
                client,
                sftp,
                script_b,
                opts,
                remote_dir,
                cfg_remote,
                spec_ref,
                host,
                log_line,
            )
        finally:
            self._conformance_detail_capture_key = None
            self._conformance_detail_run_ended_wall[script_b] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._conformance_detail_run_ended_mono[script_b] = time.monotonic()
            if restore_cancel:
                self._conformance_cancel_event.set()

        if abort_suite_on_fail:
            if rc == -2:
                if anchor_fname:
                    self._conformance_progress[anchor_fname] = {"rc": -2, "status": "STOP"}
                    self.after(0, self._conformance_refresh_row_result_labels)
                return rc, True
            if rc != 0:
                log_line(f"사전 단계 실패 (exit {rc}). 3.1.8.x 실행을 중단합니다.")
                if anchor_fname:
                    self._conformance_progress[anchor_fname] = {"rc": rc, "status": "FAIL"}
                    self.after(0, self._conformance_refresh_row_result_labels)
                return rc, True
        elif rc != 0:
            log_line(f"3.1.8.0 ({label}) 종료 exit={rc} (3.1.8.x 일괄 실행은 계속)")

        return rc, False

    def _conformance_run_pre_3180_before_318x(
        self,
        client: Any,
        sftp: Any,
        pre_script: str,
        opts: ConformanceRunOptions,
        remote_dir: str,
        cfg_remote: str,
        spec_map: dict[str, str],
        log_line: Any,
        *,
        anchor_fname: str,
    ) -> tuple[int | None, bool]:
        """3.1.8.0(oranuser) 실패 시 3.1.8.0_1(sudouser) 재시도. (rc, abort_suite)."""
        rc, _ = self._conformance_run_3180_step(
            client,
            sftp,
            pre_script,
            opts,
            remote_dir,
            cfg_remote,
            spec_map,
            log_line,
            "pre_3181",
            anchor_fname=anchor_fname,
        )
        if rc == -2:
            self._conformance_progress[anchor_fname] = {"rc": -2, "status": "STOP"}
            self.after(0, self._conformance_refresh_row_result_labels)
            return rc, True
        if rc in (None, 0):
            return rc, False
        fallback = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_POST_3180_1", "")
        if (
            fallback
            and fallback != pre_script
            and self._conformance_script_local_path(fallback)
        ):
            log_line(
                f"[3.1.8.0] 실패 (exit {rc}) → 3.1.8.0.1 ({fallback}, sudouser) 로 재시도합니다."
            )
            rc2, abort_suite = self._conformance_run_3180_step(
                client,
                sftp,
                fallback,
                opts,
                remote_dir,
                cfg_remote,
                spec_map,
                log_line,
                "pre_3181_fallback",
                abort_suite_on_fail=True,
                anchor_fname=anchor_fname,
            )
            return rc2, abort_suite
        log_line(f"사전 단계 실패 (exit {rc}). 3.1.8.x 실행을 중단합니다.")
        self._conformance_progress[anchor_fname] = {"rc": rc, "status": "FAIL"}
        self.after(0, self._conformance_refresh_row_result_labels)
        return rc, True

    def _conformance_local_dir(self) -> Path:
        d = _conformance_bundle_root() / "conformance"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _conformance_candidate_dirs(self) -> list[Path]:
        raw: list[Path] = []
        env = os.environ.get("NETCONF_CONFORMANCE_DIR", "").strip()
        if env:
            raw.append(Path(env).expanduser())
        raw.append(_conformance_bundle_root() / "conformance")
        meip = getattr(sys, "_MEIPASS", None)
        if meip:
            raw.append(Path(meip) / "conformance")
        raw.append(Path.cwd() / "conformance")
        seen: set[str] = set()
        out: list[Path] = []
        for p in raw:
            ps = str(p)
            if ps in seen:
                continue
            seen.add(ps)
            out.append(p)
        return out

    def _conformance_script_local_path(self, fname: str) -> Path | None:
        for d in self._conformance_candidate_dirs():
            p = d / fname
            if p.is_file():
                return p
        return None

    def _conformance_test_rows(self) -> list[tuple[str, str, str]]:
        """O-RAN 3.1 표 순서. 로컬 없으면 생략. (스크립트, 표 참조, 개요)."""
        rows: list[tuple[str, str, str]] = []
        for r in getattr(_conf_manifest, "CONFORMANCE_SPEC_ROWS", ()) or ():
            _sk, ref, script, _section, desc_en = r
            if self._conformance_script_local_path(script) is None:
                continue
            en = desc_en if len(desc_en) <= 96 else desc_en[:93].rstrip() + "…"
            rows.append((script, ref, en))
        return rows

    def _conformance_all_sync_script_names(self) -> list[str]:
        """동기화 시 업로드: 표시 항목 + 3.1.8 사전 스크립트 + CONFORMANCE_TESTS(예: RU-smoke)."""
        seen: set[str] = set()
        out: list[str] = []
        for script, _ref, _en in self._conformance_test_rows():
            if script not in seen:
                seen.add(script)
                out.append(script)
        pre = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")
        if pre and self._conformance_script_local_path(pre) and pre not in seen:
            seen.add(pre)
            out.append(pre)
        for _sec, fn, _d in getattr(_conf_manifest, "CONFORMANCE_TESTS", ()) or ():
            if self._conformance_script_local_path(fn) and fn not in seen:
                seen.add(fn)
                out.append(fn)
        for fn in _CONFORMANCE_HELPER_SCRIPTS:
            if self._conformance_script_local_path(fn) and fn not in seen:
                seen.add(fn)
                out.append(fn)
        if self._conformance_script_local_path(_CONFORMANCE_ORU_BOOST_SCRIPT) and _CONFORMANCE_ORU_BOOST_SCRIPT not in seen:
            seen.add(_CONFORMANCE_ORU_BOOST_SCRIPT)
            out.append(_CONFORMANCE_ORU_BOOST_SCRIPT)
        post1 = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_POST_3180_1", "")
        if post1 and self._conformance_script_local_path(post1) and post1 not in seen:
            seen.add(post1)
            out.append(post1)
        return out

    @staticmethod
    def _conformance_spec_ref_map() -> dict[str, str]:
        m: dict[str, str] = {}
        for row in getattr(_conf_manifest, "CONFORMANCE_SPEC_ROWS", ()) or ():
            _, ref, script, _, _ = row
            m[script] = ref
        pre = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")
        if pre:
            m[pre] = "3.1.8.0-prep"
        post1 = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_POST_3180_1", "")
        if post1:
            m[post1] = "3.1.8.0.1-prep"
        return m

    @staticmethod
    def _conformance_spec_description_ko(ref: str) -> str:
        descs = getattr(_conf_manifest, "CONFORMANCE_SPEC_DESCRIPTIONS_KO", None)
        if not isinstance(descs, dict):
            return ""
        return str(descs.get(ref, "") or "").strip()

    def _conformance_host_run_log_path(self, script_fname: str) -> str:
        """원격 tee 로그: CONFORMANCE_REMOTE_DIR/logs/<PRODUCT>/ (일반 사용자 쓰기 가능)."""
        pv = ""
        try:
            v = self.fields.get("PRODUCT")
            if v is not None:
                pv = str(v.get()).strip()
        except Exception:
            pass
        safe = re.sub(r"[^0-9A-Za-z._-]+", "_", pv).strip("_") or "UNKNOWN"
        ts = datetime.now().strftime("%y%m%d_%H%M%S")
        stem = Path(script_fname).stem
        name = f"CONF_{safe}_{ts}_{stem}.log"
        base = PurePosixPath(_conf_manifest.CONFORMANCE_REMOTE_DIR) / "logs"
        return str(base / safe / name)

    def _conformance_exec_remote_script(
        self,
        client: Any,
        sftp: Any,
        fname: str,
        opts: ConformanceRunOptions,
        remote_dir: str,
        cfg_remote: str,
        spec_ref: str,
        host_log_path: str,
        log_line: Any,
        *,
        oru_boost_defer_trigger: str | None = None,
    ) -> int:
        """원격에서 스크립트 1개 실행. 취소 시 -2, 그 외 원격 exit code. stdout/stderr는 host_log_path에 tee."""
        if not self._conformance_prepare_mplane_bundle(fname, log_line):
            return 1
        cfg_payload = self._conformance_effective_config_json_text(for_script=fname)
        cfg_b2 = cfg_payload.encode("utf-8")
        sftp.putfo(io.BytesIO(cfg_b2), cfg_remote, len(cfg_b2))
        log_line(f"refreshed ORU config on host (실행 직전 Settings 반영, {len(cfg_payload)} bytes)")
        if fname == _CONFORMANCE_LBM_SCRIPT:
            try:
                _mc = json.loads(cfg_payload).get("management-configurations") or {}
                _nic = str(_mc.get("LOCAL-IF") or "").strip()
                log_line(f"Server NIC (LOCAL-IF): {_nic or '(not set)'}")
            except Exception:
                pass
        envp = self._conformance_bash_env_exports(opts, fname)
        per_test_envp = self._conformance_per_test_env_exports(fname)
        if fname in ("conformance_3131.sh", "conformance_3132.sh"):
            _sn = self._conformance_resolve_supervision_needed(fname, opts)
            # 항상 최종 확정값을 다시 export (전역 30 vs 시험⚙ 3 혼선 방지)
            per_test_envp += f"export SUPERVISION_NEEDED={shlex.quote(_sn)} ; "
            if fname == "conformance_3131.sh":
                log_line(
                    f"[INFO] 3.1.3.1 SUPERVISION_NEEDED={_sn} "
                    f"(⚙ 반복 횟수 / 없으면 SUPERVISION_RESET_CYCLES) — "
                    f"N회 watchdog 후 PASS·종료"
                )
            else:
                log_line(
                    f"[INFO] 3.1.3.2 SUPERVISION_NEEDED={_sn} "
                    f"(⚙ Watchdog 유지 횟수 / 없으면 SUPERVISION_RESET_CYCLES) — "
                    f"N회 후 watchdog 중단 → 세션 실패(EOF) 유도"
                )
        rp_q = shlex.quote(f"{remote_dir}/{fname}")
        cfg_q = shlex.quote(cfg_remote)
        log_q = shlex.quote(host_log_path)
        dir_q = shlex.quote(str(PurePosixPath(host_log_path).parent))
        runner = (
            f"{envp}{per_test_envp}"
            f"export CONFORMANCE_SCRIPT_BASENAME={shlex.quote(fname)} ; "
            f"export CONFORMANCE_SPEC_REF={shlex.quote(spec_ref)} ; "
            f"export CONFORMANCE_HOST_LOG={shlex.quote(host_log_path)} ; "
            f"chmod +x {rp_q} 2>/dev/null ; bash {rp_q} --config {cfg_q}"
        )
        # 프로세스 치환 `exec > >(tee …)` 는 종료 시 tee/백그라운드 때문에 exit=0 인데도 rc=1 이 되는 경우가 있어
        # subshell + tee + PIPESTATUS 로 스크립트 종료 코드만 반영한다.
        # tee 는 `( runner ) | tee` 로만 묶고, 앞선 mkdir 등과 && 로 이어지지 않게 해 PIPESTATUS[0]이 항상 runner 가 되게 한다.
        # `; exit ${PIPESTATUS[0]}` 만 두면 일부 bash/PTY 조합에서 PIPESTATUS 가 비거나 tee 쪽으로 밀려 rc=1 이 될 수 있어
        # 한 줄 안에서 대입 후 exit 한다.
        wrapped = (
            f"set -o pipefail; "
            f"mkdir -p {dir_q} && : > {log_q} && chmod 0644 {log_q} || exit 1; "
            f"( {runner} ) 2>&1 | tee -a {log_q}; "
            "_cf_rc=${PIPESTATUS[0]}; "
            'exit "${_cf_rc:-0}"'
        )
        cmd = "bash -lc " + shlex.quote(wrapped)
        log_line(f"remote host log file: {host_log_path}")
        log_line(f"---- START {fname} ----")
        ch: Any = None
        try:
            _stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
            ch = stdout.channel
            with self._conformance_run_transport_lock:
                self._conformance_run_script_channel = ch
            while True:
                if self._conformance_cancel_event.is_set():
                    try:
                        ch.close()
                    except Exception:
                        pass
                    log_line(f"---- ABORT {fname} ----")
                    return -2
                got = False
                if ch.recv_ready():
                    b = ch.recv(4096)
                    s = b.decode(errors="replace")
                    if s:
                        for _line in s.splitlines():
                            _ln = _line.rstrip("\n")
                            if _ln:
                                log_line(_ln)
                            if (
                                oru_boost_defer_trigger
                                and oru_boost_defer_trigger in _ln
                                and not getattr(self, "_conformance_oru_boost_active", False)
                                and self._conformance_oru_boost_enabled(fname)
                            ):
                                if self._conformance_start_oru_show_system_boost(
                                    client, sftp, remote_dir, fname, log_line
                                ):
                                    log_line(
                                        f"[O-RU boost] deferred start (after: {oru_boost_defer_trigger!r})"
                                    )
                    got = True
                if ch.recv_stderr_ready():
                    b = ch.recv_stderr(4096)
                    s = b.decode(errors="replace")
                    if s:
                        for _line in s.splitlines():
                            _ln = _line.rstrip("\n")
                            if _ln:
                                log_line(_ln)
                    got = True
                if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                    break
                if not got:
                    time.sleep(0.12)
            rc = ch.recv_exit_status()
            st = "PASS" if rc == 0 else "FAIL"
            log_line(f"---- END {fname} exit={rc} [{st}] ----")
            if fname in ("conformance_31121.sh", "conformance_31122.sh"):
                self._conformance_verify_log_upload_on_host(sftp, fname, host_log_path, log_line)
            return int(rc)
        finally:
            with self._conformance_run_transport_lock:
                self._conformance_run_script_channel = None

    def _conformance_3112x_settings(self) -> dict[str, str]:
        store = self._conformance_per_test_settings.get(_CONFORMANCE_3112X_SHARED_KEY, {})
        if not store:
            store = self._conformance_per_test_settings.get("conformance_31121.sh", {})
        return {
            "remote_upload_dir": (store.get("remote_upload_dir") or "/tmp").strip() or "/tmp",
        }

    def _conformance_oru_boost_enabled(self, fname: str) -> bool:
        if fname not in _CONFORMANCE_3112X_SCRIPTS:
            return False
        mode = (self._conformance_get_per_test_val(fname, "oru_log_boost_enable") or "1").strip()
        return mode != "0"

    def _conformance_oru_boost_credentials(self, fname: str) -> tuple[str, str, str] | None:
        host = ""
        try:
            host = str(self.fields.get("ALLOWED_IP", tk.StringVar()).get()).strip()  # type: ignore[union-attr]
        except Exception:
            pass
        if not host:
            return None
        oru_id = self._conformance_get_per_test_val(fname, "oru_cli_id").strip()
        oru_pw = self._conformance_get_per_test_val(fname, "oru_cli_pw").strip()
        if not oru_id:
            try:
                oru_id = str(self.fields.get("CLI-ID", tk.StringVar()).get()).strip()  # type: ignore[union-attr]
            except Exception:
                oru_id = ""
        if not oru_pw:
            try:
                oru_pw = str(self.fields.get("CLI-PW", tk.StringVar()).get()).strip()  # type: ignore[union-attr]
            except Exception:
                oru_pw = ""
        if not oru_id or not oru_pw:
            return None
        return host, oru_id, oru_pw

    def _conformance_upload_oru_boost_script(self, sftp: Any, remote_dir: str, log_line: Any) -> bool:
        lp = self._conformance_script_local_path(_CONFORMANCE_ORU_BOOST_SCRIPT)
        if lp is None:
            log_line(f"[ERROR] 로컬 부스트 스크립트 없음: {_CONFORMANCE_ORU_BOOST_SCRIPT}")
            return False
        rp = f"{remote_dir.rstrip('/')}/{_CONFORMANCE_ORU_BOOST_SCRIPT}"
        try:
            text = lp.read_text(encoding="utf-8", errors="replace")
            data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            sftp.putfo(io.BytesIO(data), rp, len(data))
            sftp.chmod(rp, 0o755)
            return True
        except Exception as exc:
            log_line(f"[ERROR] O-RU 부스트 스크립트 업로드 실패: {exc}")
            return False

    def _conformance_stop_oru_show_system_boost(self, client: Any, remote_dir: str, log_line: Any) -> None:
        was_active = bool(getattr(self, "_conformance_oru_boost_active", False))
        self._conformance_oru_boost_active = False
        self._conformance_oru_boost_remote_dir = None
        if client is None:
            return
        try:
            tr = client.get_transport()
            if tr is None or not tr.is_active():
                return
        except Exception:
            return
        rd = remote_dir.rstrip("/")
        pid_file = f"{rd}/.oru_boost.pid"
        pf = shlex.quote(pid_file)
        script_pat = shlex.quote(_CONFORMANCE_ORU_BOOST_SCRIPT)
        cred = self._conformance_oru_boost_credentials("conformance_31122.sh")
        extra_kill = ""
        if cred:
            host, oru_id, _pw = cred
            extra_kill = (
                f"pkill -f {shlex.quote(f'sshpass.*{oru_id}@{host}')} 2>/dev/null || true; "
                f"pkill -f {shlex.quote(f'{oru_id}@{host}')} 2>/dev/null || true; "
            )
        cmd = (
            f"if [[ -f {pf} ]]; then "
            f"pid=$(cat {pf} 2>/dev/null); "
            f"if [[ -n \"$pid\" ]]; then "
            f"kill -TERM -- -\"$pid\" 2>/dev/null || kill -TERM \"$pid\" 2>/dev/null || true; "
            f"sleep 0.3; "
            f"kill -KILL -- -\"$pid\" 2>/dev/null || kill -KILL \"$pid\" 2>/dev/null || true; "
            f"fi; "
            f"rm -f {pf}; "
            f"fi; "
            f"pkill -TERM -f {script_pat} 2>/dev/null || true; "
            f"sleep 0.2; "
            f"pkill -KILL -f {script_pat} 2>/dev/null || true; "
            f"{extra_kill}"
        )
        try:
            _stdin, _stdout, _stderr = client.exec_command(f"bash -lc {shlex.quote(cmd)}")
            _stdout.channel.recv_exit_status()
            if was_active:
                log_line("[O-RU boost] stopped show system loop")
            else:
                log_line("[O-RU boost] orphan cleanup (show system loop)")
        except Exception as exc:
            log_line(f"[WARN] O-RU boost stop: {exc}")

    def _conformance_start_oru_show_system_boost(
        self, client: Any, sftp: Any, remote_dir: str, fname: str, log_line: Any
    ) -> bool:
        if not self._conformance_oru_boost_enabled(fname):
            return True
        cred = self._conformance_oru_boost_credentials(fname)
        if cred is None:
            log_line(
                "[WARN] O-RU log 부스트: ALLOWED_IP·O-RU ID/PW(또는 Settings CLI-ID/CLI-PW)가 필요합니다 — 건너뜀"
            )
            return True
        host, oru_id, oru_pw = cred
        if not self._conformance_upload_oru_boost_script(sftp, remote_dir, log_line):
            return False
        self._conformance_stop_oru_show_system_boost(client, remote_dir, log_line)
        rp = f"{remote_dir.rstrip('/')}/{_CONFORMANCE_ORU_BOOST_SCRIPT}"
        pid_file = f"{remote_dir.rstrip('/')}/.oru_boost.pid"
        log_path = "/var/tmp/oru_show_system_boost.log"
        runner = (
            f"export ORU_BOOST_IP={shlex.quote(host)} "
            f"ORU_BOOST_ID={shlex.quote(oru_id)} "
            f"ORU_BOOST_PW={shlex.quote(oru_pw)} "
            f"ORU_BOOST_INTERVAL=1; "
            f"setsid bash {shlex.quote(rp)} >>{shlex.quote(log_path)} 2>&1 & "
            f"echo $! > {shlex.quote(pid_file)}"
        )
        try:
            _stdin, _stdout, _stderr = client.exec_command(f"bash -lc {shlex.quote(runner)}")
            rc = _stdout.channel.recv_exit_status()
            if rc != 0:
                err = _stderr.read().decode(errors="replace").strip()
                log_line(f"[ERROR] O-RU boost start failed (rc={rc}){': ' + err if err else ''}")
                return False
            pid = _stdout.read().decode(errors="replace").strip()
            self._conformance_oru_boost_active = True
            self._conformance_oru_boost_remote_dir = remote_dir.rstrip("/")
            log_line(
                f"[O-RU boost] started: vtysh show system every 1s → {oru_id}@{host}"
                + (f" (pid {pid})" if pid else "")
            )
            return True
        except Exception as exc:
            log_line(f"[ERROR] O-RU boost start: {exc}")
            return False

    def _conformance_verify_log_upload_on_host(
        self,
        sftp: Any,
        fname: str,
        host_log_path: str,
        log_line: Any,
    ) -> None:
        """After 3.1.12.x run, confirm uploaded file exists under configured SFTP save directory."""
        remote_dir = self._conformance_3112x_settings()["remote_upload_dir"].rstrip("/") or "/tmp"
        log_line(f"[GUI] SFTP save directory check: {remote_dir}/")
        text = ""
        try:
            with sftp.open(host_log_path, "r") as hf:
                raw = hf.read()
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception as exc:
            log_line(f"[GUI] Upload verify: cannot read host log ({exc})")
            return
        receivers = re.findall(r"SFTP receiver check:\s*(\S+)", text)
        receiver = receivers[-1].strip() if receivers else ""
        if not receiver:
            m = re.search(r"Step 4\.\s*File Upload\s*\(\s*([^)\s]+)\s*\)", text)
            if m:
                receiver = f"{remote_dir}/{m.group(1).strip()}"
        if not receiver:
            log_line("[GUI] Upload verify: no receiver path in log (Step 4 / SFTP receiver check)")
            return
        if not receiver.startswith("/"):
            receiver = f"{remote_dir}/{receiver.lstrip('/')}"
        try:
            st = sftp.stat(receiver)
            size = getattr(st, "st_size", None)
            log_line(f"[GUI] Upload verify OK: {receiver}" + (f" ({size} bytes)" if size is not None else ""))
        except OSError as exc:
            log_line(f"[GUI] Upload verify FAIL: missing {receiver} ({exc})")
            try:
                names = sftp.listdir(remote_dir)
                preview = ", ".join(sorted(names)[:12])
                if len(names) > 12:
                    preview += f", … (+{len(names) - 12})"
                log_line(f"[GUI]   listing {remote_dir}: {preview or '(empty)'}")
            except OSError as exc2:
                log_line(f"[GUI]   cannot list {remote_dir}: {exc2}")

    def _conformance_build_management_config_json(self) -> str:
        f = self.fields

        def gv(key: str, default: str = "") -> str:
            var = f.get(key)
            if var is None:
                return default
            return str(var.get()).strip()

        obj: dict[str, Any] = {
            "management-configurations": {
                "NETCONF-ID": gv("USER"),
                "NETCONF-PW": gv("PASSWORD"),
                "SERVER-IP": gv("ALLOWED_IP"),
                "LOCAL-IP": gv("LOCAL_IP"),
                "SERVER-IP-V6": gv("ALLOWED_IP_V6"),
                "LOCAL-IP-V6": gv("LOCAL_IP_V6"),
                "PORT": gv("NETCONF_PORT"),
                "PRODUCT-CODE": gv("PRODUCT"),
                "CLI-ID": gv("CLI-ID"),
                "CLI-PW": gv("CLI-PW"),
                "LOCAL-IF": gv("LOCAL_IF"),
            }
        }
        return json.dumps(obj, ensure_ascii=True, indent=2)

    @staticmethod
    def _conformance_strip_json_nulls(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: ConformanceMixin._conformance_strip_json_nulls(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [ConformanceMixin._conformance_strip_json_nulls(x) for x in obj]
        if obj is None:
            return ""
        return obj

    def _conformance_apply_config_stubs(self, root: dict[str, Any]) -> None:
        if not isinstance(root, dict):
            return
        if root.get("interface-configurations") is None:
            root["interface-configurations"] = {
                "to-DU-interface": {"enable": False, "name": {}, "vlan": {}},
            }
        if root.get("processing-element-configurations") is None:
            root["processing-element-configurations"] = {
                "to-DU-processing-element": {"enable": False, "name": {}, "ODUMAC": {}},
            }
        ic = root.get("interface-configurations")
        if isinstance(ic, dict) and ic.get("to-DU-interface") is None:
            ic["to-DU-interface"] = {"enable": False, "name": {}, "vlan": {}}
        pe = root.get("processing-element-configurations")
        if isinstance(pe, dict) and pe.get("to-DU-processing-element") is None:
            pe["to-DU-processing-element"] = {"enable": False, "name": {}, "ODUMAC": {}}

    def _conformance_swm_settings_script(self, for_script: str) -> str:
        """software-management JSON에 쓸 3.1.6.x 설정 소스 스크립트."""
        if for_script in ("conformance_3161.sh", "conformance_3162.sh"):
            return for_script
        if for_script == "conformance_3170.sh":
            if (self._conformance_get_per_test_val("conformance_3161.sh", "swm_pkg_path") or "").strip():
                return "conformance_3161.sh"
            if (self._conformance_get_per_test_val("conformance_3162.sh", "swm_pkg_path") or "").strip():
                return "conformance_3162.sh"
        return ""

    def _conformance_apply_swm_from_per_test(self, root: dict[str, Any], for_script: str | None) -> None:
        if not isinstance(root, dict) or not for_script:
            return
        swm_src = self._conformance_swm_settings_script(for_script)
        if not swm_src:
            return
        swm_pw = self._conformance_get_per_test_val(swm_src, "swm_server_pw").strip()
        swm_ip = self._conformance_get_per_test_val(swm_src, "swm_server_ip").strip()
        swm_id = self._conformance_get_per_test_val(swm_src, "swm_server_id").strip() or "root"
        swm_pkg = self._conformance_get_per_test_val(swm_src, "swm_pkg_path").strip()
        swm_obj: dict[str, Any] = {}
        if swm_pkg and swm_ip:
            pkg_filename = os.path.basename(swm_pkg)
            swm_obj["path"] = f"sftp://{swm_id}@{swm_ip}/tmp/netconf_PKG/{pkg_filename}"
            swm_obj["password"] = swm_pw
        guard = self._conformance_get_per_test_val("conformance_3170.sh", "activate_get_guard_sec").strip()
        if guard:
            swm_obj["activate-get-guard-sec"] = guard
        if swm_obj:
            root["software-management"] = swm_obj
        elif "software-management" in root:
            del root["software-management"]

    def _conformance_effective_config_json_text(self, for_script: str | None = None) -> str:
        """ORU JSON for remote --config: Settings + per-test (M-Plane xlsx, interface, SWM)."""
        gui_txt = self._conformance_build_management_config_json()
        gui_obj = json.loads(gui_txt)
        if for_script:
            self._conformance_apply_server_nic_to_management(gui_obj, for_script)
            self._conformance_apply_interface_from_per_test(gui_obj, for_script)
            self._conformance_apply_mplane_from_bundle(gui_obj, for_script)
            self._conformance_apply_log_fileserver_from_per_test(gui_obj, for_script)
            self._conformance_apply_swm_from_per_test(gui_obj, for_script)
        self._conformance_apply_config_stubs(gui_obj)
        gui_obj = self._conformance_strip_json_nulls(gui_obj)
        return json.dumps(gui_obj, ensure_ascii=True, indent=2)

    def _conformance_mplane_bundle_cache(self) -> dict[str, Any]:
        if not hasattr(self, "_conformance_mplane_bundles"):
            self._conformance_mplane_bundles = {}  # type: ignore[attr-defined]
        return self._conformance_mplane_bundles  # type: ignore[attr-defined]

    def _conformance_resolve_mplane_xlsx_path(self, fname: str) -> str:
        path = self._conformance_get_per_test_val(fname, "mplane_xlsx_path").strip()
        if not path:
            try:
                gv = getattr(self, "mplane_xlsx_path", None)
                if gv is not None:
                    path = str(gv.get()).strip()
            except Exception:
                pass
        if not path:
            return ""
        try:
            norm = getattr(self, "_normalize_mplane_workbook_path", None)
            if callable(norm):
                return str(norm(path)).strip()
        except Exception:
            pass
        return str(Path(path).expanduser().resolve())

    def _conformance_prepare_mplane_bundle(self, fname: str, log_line: Any) -> bool:
        if fname not in _CONFORMANCE_MPLANE_SCRIPTS:
            return True
        cache = self._conformance_mplane_bundle_cache()
        cache.pop(fname, None)
        xlsx = self._conformance_resolve_mplane_xlsx_path(fname)
        if not xlsx:
            log_line("[INFO] M-Plane xlsx 미지정 — miniDU 템플릿(레거시) 경로")
            return True
        if not os.path.isfile(xlsx):
            log_line(f"[ERROR] M-Plane xlsx 없음: {xlsx}")
            return False
        try:
            import mplane_conformance as mc

            bundle = mc.prepare_mplane_conformance_bundle(
                xlsx,
                duplicate_eaxc=(fname == "conformance_31102.sh"),
            )
        except Exception as exc:
            log_line(f"[ERROR] M-Plane xlsx 로드 실패: {exc}")
            return False
        cache[fname] = bundle
        log_line(f"M-Plane xlsx 준비: {xlsx} ({len(bundle.remote_files)} RPC)")
        for w in bundle.warnings[:12]:
            log_line(f"  [M-Plane] {w}")
        if len(bundle.warnings) > 12:
            log_line(f"  [M-Plane] … 외 {len(bundle.warnings) - 12}건")
        return True

    def _conformance_apply_mplane_from_bundle(self, root: dict[str, Any], for_script: str | None) -> None:
        if not for_script or for_script not in _CONFORMANCE_MPLANE_SCRIPTS:
            return
        bundle = self._conformance_mplane_bundle_cache().get(for_script)
        if bundle is None:
            return
        try:
            import mplane_conformance as mc

            root.update(mc.mplane_config_json_entries(bundle))
        except Exception:
            pass

    def _conformance_mplane_templates_local_dir(self) -> Path:
        return _conformance_bundle_root() / "conformance" / "mplane_templates"

    def _conformance_upload_mplane_templates(self, sftp: Any, log_line: Any) -> None:
        tpl_root = self._conformance_mplane_templates_local_dir()
        if not tpl_root.is_dir():
            return
        remote_tpl = _MPLANE_REMOTE_TEMPLATE_DIR
        uploaded = 0
        try:
            for local_path in sorted(tpl_root.rglob("*")):
                if not local_path.is_file():
                    continue
                rel = local_path.relative_to(tpl_root).as_posix()
                remote_path = f"{remote_tpl}/{rel}"
                parent = str(PurePosixPath(remote_path).parent)
                parts = parent.strip("/").split("/")
                cur = ""
                for p in parts:
                    cur = f"{cur}/{p}" if cur else f"/{p}"
                    try:
                        sftp.stat(cur)
                    except OSError:
                        sftp.mkdir(cur)
                sftp.put(str(local_path), remote_path)
                try:
                    sftp.chmod(remote_path, 0o644)
                except OSError:
                    pass
                uploaded += 1
                log_line(f"M-Plane template 업로드: {rel} → {remote_path}")
            if uploaded:
                log_line(f"M-Plane templates: {uploaded} file(s) → {remote_tpl}")
        except Exception as exc:
            log_line(f"WARN M-Plane template upload: {exc}")

    def _conformance_upload_mplane_helper_scripts(self, sftp: Any, remote_dir: str, log_line: Any) -> bool:
        for helper in _CONFORMANCE_HELPER_SCRIPTS:
            lp = self._conformance_script_local_path(helper)
            if lp is None:
                log_line(f"[ERROR] 로컬 helper 없음: {helper}")
                return False
            rp = f"{remote_dir}/{helper}"
            try:
                text = lp.read_text(encoding="utf-8", errors="replace")
                data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
                sftp.putfo(io.BytesIO(data), rp, len(data))
                sftp.chmod(rp, 0o755)
                try:
                    import hashlib

                    _md5 = hashlib.md5(data).hexdigest()[:8]
                    log_line(f"uploaded {helper} (md5:{_md5})")
                except Exception:
                    log_line(f"uploaded {helper}")
            except Exception as exc:
                log_line(f"[ERROR] helper 업로드 실패 ({helper}): {exc}")
                return False
        return True

    def _conformance_upload_mplane_assets(
        self, sftp: Any, fname: str, remote_dir: str, log_line: Any
    ) -> bool:
        if fname not in _CONFORMANCE_MPLANE_SCRIPTS:
            return True
        if not self._conformance_upload_mplane_helper_scripts(sftp, remote_dir, log_line):
            return False
        self._conformance_upload_mplane_templates(sftp, log_line)
        bundle = self._conformance_mplane_bundle_cache().get(fname)
        if bundle is None:
            return True
        try:
            import mplane_conformance as mc
            import mplane_control as _mp

            rpc_dir = mc.MPLANE_REMOTE_RPC_DIR
            parts = rpc_dir.strip("/").split("/")
            cur = ""
            for p in parts:
                cur = f"{cur}/{p}" if cur else f"/{p}"
                try:
                    sftp.stat(cur)
                except OSError:
                    sftp.mkdir(cur)

            for step in _mp.SEND_ORDER:
                rpath = bundle.remote_files.get(step)
                body = (bundle.rpc.get(step) or "").strip()
                if not rpath or not body:
                    continue
                data = body.encode("utf-8")
                sftp.putfo(io.BytesIO(data), rpath, len(data))
                try:
                    sftp.chmod(rpath, 0o644)
                except OSError:
                    pass
                log_line(f"M-Plane RPC 업로드: {step} → {rpath}")
            dup_path = bundle.remote_files.get("PDSCH-duplicate-eaxc")
            dup_body = (bundle.duplicate_pdsch_rpc or "").strip()
            if dup_path and dup_body:
                data = dup_body.encode("utf-8")
                sftp.putfo(io.BytesIO(data), dup_path, len(data))
                try:
                    sftp.chmod(dup_path, 0o644)
                except OSError:
                    pass
                log_line(f"M-Plane RPC 업로드: PDSCH-duplicate-eaxc → {dup_path}")
            return True
        except Exception as exc:
            log_line(f"[ERROR] M-Plane RPC 업로드 실패: {exc}")
            return False

    def _conformance_apply_log_fileserver_from_per_test(
        self, root: dict[str, Any], for_script: str | None
    ) -> None:
        if not for_script or for_script not in ("conformance_31121.sh", "conformance_31122.sh"):
            return
        store = self._conformance_per_test_settings.get(_CONFORMANCE_3112X_SHARED_KEY, {})
        if not store:
            store = self._conformance_per_test_settings.get(for_script, {})
        mc = root.setdefault("management-configurations", {})
        if not isinstance(mc, dict):
            return
        fs_ip = (store.get("file_server_ip") or "").strip()
        fs_id = (store.get("file_server_id") or "").strip()
        fs_pw = (store.get("file_server_pw") or "").strip()
        log_prefix = (store.get("local_log_prefix") or "O-RAN/log").strip()
        remote_dir = (store.get("remote_upload_dir") or "/tmp").strip() or "/tmp"
        if fs_ip:
            mc["FileServer-IP"] = fs_ip
        if fs_id:
            mc["FileServer-ID"] = fs_id
        if fs_pw:
            mc["FileServer-PW"] = fs_pw
        oru_id = (store.get("oru_cli_id") or "").strip()
        oru_pw = (store.get("oru_cli_pw") or "").strip()
        if oru_id:
            mc["CLI-ID"] = oru_id
        if oru_pw:
            mc["CLI-PW"] = oru_pw
        if log_prefix:
            mc["local-log-prefix"] = log_prefix.rstrip("/")
        mc["remote-upload-dir"] = remote_dir.rstrip("/") or "/tmp"

    def _conformance_odu_mac_for_interface_test(self, for_script: str) -> str:
        """O-DU MAC for interface/PE scripts: Excel bundle → per-test → Settings M-Plane."""
        if for_script in _CONFORMANCE_MPLANE_SCRIPTS:
            bundle = self._conformance_mplane_bundle_cache().get(for_script)
            if bundle is not None:
                mac = str(bundle.merged.get("odu_mac") or "").strip()
                if mac:
                    return mac
        mac = self._conformance_get_per_test_val(for_script, "odu_mac").strip()
        if mac:
            return mac
        mf = getattr(self, "mplane_fields", None) or {}
        rec = mf.get("odu_mac")
        if rec is not None:
            mac = str(rec.get()).strip()
            if mac:
                return mac
        return ""

    def _conformance_resolve_server_nic(self, for_script: str | None = None) -> str:
        """miniDU ethping -i (31131 only): per-test server_nic → Settings LOCAL_IF."""
        if for_script == _CONFORMANCE_LBM_SCRIPT:
            nic = self._conformance_get_per_test_val(for_script, "server_nic").strip()
            if nic:
                return nic
            store = self._conformance_per_test_settings.get(_CONFORMANCE_INTERFACE_SHARED_KEY, {})
            nic = str(store.get("server_nic") or "").strip()
            if nic:
                return nic
        var = self.fields.get("LOCAL_IF")
        if var is not None:
            return str(var.get()).strip()
        return ""

    def _conformance_apply_server_nic_to_management(
        self, root: dict[str, Any], for_script: str | None
    ) -> None:
        if not isinstance(root, dict) or not for_script:
            return
        if for_script != _CONFORMANCE_LBM_SCRIPT:
            return
        nic = self._conformance_resolve_server_nic(for_script)
        if not nic:
            return
        mc = root.setdefault("management-configurations", {})
        if isinstance(mc, dict):
            mc["LOCAL-IF"] = nic

    def _conformance_apply_interface_from_per_test(self, root: dict[str, Any], for_script: str | None) -> None:
        if not isinstance(root, dict) or not for_script:
            return
        if for_script not in _CONFORMANCE_INTERFACE_SCRIPTS:
            return
        ifname = self._conformance_get_per_test_val(for_script, "to_du_if_name").strip()
        if not ifname:
            return
        vlan = self._conformance_get_per_test_val(for_script, "to_du_vlan").strip() or "1"
        bundle = (
            self._conformance_mplane_bundle_cache().get(for_script)
            if for_script in _CONFORMANCE_MPLANE_SCRIPTS
            else None
        )
        pe_name = ifname
        if bundle is not None:
            pe_name = str(bundle.merged.get("pe_name") or pe_name).strip() or pe_name
        odu_mac = self._conformance_odu_mac_for_interface_test(for_script)
        iface_entry = {"0": ifname}
        vlan_entry = {"0": vlan}
        root["interface-configurations"] = {
            "to-DU-interface": {
                "enable": True,
                "name": [iface_entry],
                "vlan": [vlan_entry],
            },
        }
        root["processing-element-configurations"] = {
            "to-DU-processing-element": {
                "enable": True,
                "name": [{"0": pe_name}],
                "ODUMAC": [{"0": odu_mac}],
            },
        }

    def _conformance_bash_env_exports(self, opts: ConformanceRunOptions, fname: str | None = None) -> str:
        parts: list[str] = []
        for key in (
            "USER",
            "PASSWORD",
            "ALLOWED_IP",
            "LOCAL_IP",
            "LOCAL_IF",
            "CALLHOME_PORT",
            "NETCONF_PORT",
            "PRODUCT",
            "LOG_PATH",
        ):
            var = self.fields.get(key)
            if var is None:
                continue
            val = var.get().strip()
            parts.append(f"export {key}={shlex.quote(val)}")
        if fname == _CONFORMANCE_LBM_SCRIPT:
            parts = [p for p in parts if not p.startswith("export LOCAL_IF=")]
            parts.append(f"export LOCAL_IF={shlex.quote(self._conformance_resolve_server_nic(fname))}")
        parts.append(f"export NETCONF_RPC_TIMEOUT={shlex.quote(opts.netconf_rpc_timeout.strip() or '30')}")
        parts.append(f"export NETCONF_IDLE_TIMEOUT={shlex.quote(opts.netconf_idle_timeout.strip() or '120')}")
        parts.append(f"export SUPERVISION_INTERVAL={shlex.quote(opts.supervision_interval.strip() or '60')}")
        _src = (opts.supervision_reset_cycles or "").strip() or "30"
        parts.append(f"export SUPERVISION_RESET_CYCLES={shlex.quote(_src)}")
        _nfc = (opts.supervision_negative_fail_on_cycle or "").strip() or "3"
        parts.append(f"export SUPERVISION_NEGATIVE_FAIL_ON_CYCLE={shlex.quote(_nfc)}")
        parts.append(f"export CONN_DELAY={shlex.quote(opts.conn_delay.strip() or '3')}")
        _plw = (opts.post_listen_wait_sec or "").strip() or "0"
        if not re.fullmatch(r"[0-9]+", _plw):
            _plw = "0"
        _plw_n = min(int(_plw), 600)
        parts.append(f"export CONFORMANCE_POST_LISTEN_WAIT={shlex.quote(str(_plw_n))}")
        parts.append("export NETCONF_TMP=/var/tmp/netconf_tmp")
        parts.append("export CONFORMANCE_REMOTE_DIR=/var/tmp/conformance")
        parts.append("export SUPERVISION_RESET=/var/tmp/netconf_tmp/supervision_reset.xml")
        parts.append("export NETCONF_CONTROL_FIFO=/var/tmp/netconf_tmp/netconf_control.fifo")
        parts.append("export CMD_LOCK_FILE=/var/tmp/netconf_tmp/netconf_cmd.lock")
        if fname in _CONFORMANCE_MPLANE_SCRIPTS:
            gui_running = bool(getattr(self, "is_running", False))
            gui_session = bool(getattr(self, "session_established", False))
            if gui_running and gui_session:
                parts.append("export CONFORMANCE_GUI_NETCONF=1")
                gui_log_dir = ""
                try:
                    lp = self.fields.get("LOG_PATH")
                    if lp is not None:
                        gui_log_dir = str(lp.get()).strip()
                except Exception:
                    gui_log_dir = ""
                if gui_log_dir:
                    parts.append(f"export CONFORMANCE_GUI_LOG_DIR={shlex.quote(gui_log_dir)}")
                parts.append(
                    "export CONFORMANCE_GUI_NETCONF_HINT="
                    + shlex.quote("GUI Start 후 Conformance 실행 → M-Plane 은 Netconf Client FIFO (edit-config)")
                )
        return " ; ".join(parts) + (" ; " if parts else "")

    def _conformance_remote_prepare_netconf_tmp(self, client: Any, log_line: Any) -> None:
        body = b'<supervision-watchdog-reset xmlns="urn:o-ran:supervision:1.0"/>\n'
        b64 = base64.b64encode(body).decode("ascii")
        p1 = shlex.quote("/var/tmp/netconf_tmp/supervision_reset.xml")
        p2 = shlex.quote("/var/tmp/netconf_tmp/watchdog.xml")
        cmd = (
            "mkdir -p /var/tmp/netconf_tmp/edit /var/tmp/netconf_tmp/get /var/tmp/conformance && "
            f"echo {shlex.quote(b64)} | base64 -d > {p1} && cp -f {p1} {p2}"
        )
        try:
            _sin, _sout, _serr = client.exec_command(cmd)
            rc = _sout.channel.recv_exit_status()
            if rc != 0:
                err = _serr.read().decode(errors="ignore").strip()
                log_line(f"WARN netconf_tmp prepare rc={rc}: {err}")
            else:
                log_line("/var/tmp/netconf_tmp + supervision XML 준비됨")
        except Exception as exc:
            log_line(f"WARN netconf_tmp prepare: {exc}")

    def _conformance_collect_ssh_settings(self) -> tuple[str, str, str, str, str] | None:
        u, h, p, pw, key = self._remote_conn()
        if not u or not h or not p:
            messagebox.showwarning("Conformance", "Settings 탭에서 SSH_USER / SSH_HOST / SSH_PORT 를 채워 주세요.")
            return None
        return (u, h, p, pw, key)

    def _conformance_log_lines_to_gui(self, tag: str, msg: str) -> None:
        """append_log는 줄 단위로 _should_hide_line 적용 — 멀티라인 수신 시 줄마다 태그를 붙인다."""
        if not msg:
            return
        prefix = f"[{tag}] "
        for piece in msg.splitlines():
            self.after(0, self.append_log, f"{prefix}{piece}\n")

    @staticmethod
    def _conformance_summarize_pass_fail_counts(by_script: dict[str, Any]) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "INFO": 0, "STOP": 0}
        for ent in by_script.values():
            if not isinstance(ent, dict):
                continue
            st = str(ent.get("status") or "").upper()
            if st in out:
                out[st] += 1
            rc = ent.get("rc")
            if rc == -2:
                out["STOP"] += 1
            elif isinstance(rc, int) and rc != 0 and st != "FAIL":
                out["FAIL"] += 1
        return out

    @staticmethod
    def _conformance_format_pass_fail_counts_ko(summary: dict[str, int]) -> str:
        parts = []
        for k in ("PASS", "FAIL", "INFO", "STOP"):
            if summary.get(k):
                parts.append(f"{k} {summary[k]}")
        return ", ".join(parts) if parts else "기록 없음"

    @staticmethod
    def _conformance_new_session_run_stats() -> dict[str, Any]:
        empty = {"PASS": 0, "FAIL": 0, "STOP": 0}
        return {
            "repeat": dict(empty),
            "manual_repeat": dict(empty),
            "by_script": {},
        }

    @staticmethod
    def _conformance_session_result_bucket(rc: Any, status: Any) -> str:
        st = str(status or "").upper()
        try:
            rc_i = int(rc)
        except (TypeError, ValueError):
            rc_i = None
        if rc_i == -2 or st == "STOP":
            return "STOP"
        if rc_i == 0 or st == "PASS":
            return "PASS"
        return "FAIL"

    def _conformance_reset_session_run_stats(self, reason: str = "") -> None:
        self._conformance_session_run_stats = self._conformance_new_session_run_stats()
        self.after(0, self._conformance_refresh_results_summary_window)
        if reason:
            try:
                self.append_log(f"[Conformance] 세션 집계 초기화 ({reason})\n")
            except Exception:
                pass

    def _conformance_reset_session_run_stats_from_ui(self) -> None:
        """Clear cumulative PASS/FAIL counts in 전체 결과 (repeat / manual repeat)."""
        if not messagebox.askyesno(
            "Conformance",
            "반복시험·수동 반복시험 PASS/FAIL 누적 횟수를 초기화할까요?\n"
            "(「최종 결과」열은 그대로 둡니다.)",
        ):
            return
        self._conformance_reset_session_run_stats("사용자 초기화")
        messagebox.showinfo("Conformance", "누적 횟수를 초기화했습니다.")

    def _conformance_record_session_run_result(self, fname: str, rc: Any, status: Any) -> None:
        mode = getattr(self, "_conformance_run_stats_mode", None)
        if mode not in ("repeat", "manual_repeat"):
            return
        bucket = self._conformance_session_result_bucket(rc, status)
        stats = getattr(self, "_conformance_session_run_stats", None)
        if not isinstance(stats, dict):
            self._conformance_session_run_stats = self._conformance_new_session_run_stats()
            stats = self._conformance_session_run_stats
        totals = stats.setdefault(mode, {"PASS": 0, "FAIL": 0, "STOP": 0})
        totals[bucket] = int(totals.get(bucket, 0)) + 1
        by_script = stats.setdefault("by_script", {})
        ent = by_script.setdefault(
            fname,
            {
                "repeat": {"PASS": 0, "FAIL": 0, "STOP": 0},
                "manual_repeat": {"PASS": 0, "FAIL": 0, "STOP": 0},
                "history": [],
            },
        )
        ent_mode = ent.setdefault(mode, {"PASS": 0, "FAIL": 0, "STOP": 0})
        ent_mode[bucket] = int(ent_mode.get(bucket, 0)) + 1
        hist = ent.setdefault("history", [])
        if isinstance(hist, list):
            hist.append(
                {
                    "mode": mode,
                    "status": bucket,
                    "rc": rc,
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            # Keep recent history bounded
            if len(hist) > 200:
                del hist[:-200]
        self.after(0, self._conformance_refresh_results_summary_window)

    def _conformance_format_session_pf_cell(self, fname: str, mode: str) -> str:
        stats = getattr(self, "_conformance_session_run_stats", None)
        if not isinstance(stats, dict):
            return "—"
        by_script = stats.get("by_script")
        if not isinstance(by_script, dict):
            return "—"
        ent = by_script.get(fname)
        if not isinstance(ent, dict):
            return "—"
        counts = ent.get(mode)
        if not isinstance(counts, dict):
            return "—"
        p = int(counts.get("PASS", 0))
        f = int(counts.get("FAIL", 0))
        s = int(counts.get("STOP", 0))
        total = p + f + s
        if not total:
            return "—"
        parts = [f"PASS {p}" if p else "", f"FAIL {f}" if f else "", f"STOP {s}" if s else ""]
        summary = ", ".join(x for x in parts if x)
        # Compact per-run trail so repeats are visible (not only last result)
        hist = ent.get("history")
        trail = ""
        if isinstance(hist, list) and hist:
            mode_hist = [str(h.get("status") or "") for h in hist if isinstance(h, dict) and h.get("mode") == mode]
            if mode_hist:
                # Show last up to 12 outcomes: P/F/S
                short = "".join(
                    "P" if x == "PASS" else ("F" if x == "FAIL" else ("S" if x == "STOP" else "?"))
                    for x in mode_hist[-12:]
                )
                if len(mode_hist) > 12:
                    short = "…" + short
                trail = f" [{short}]"
        return f"{summary}{trail}"

    def _conformance_format_session_run_stats_line(self) -> str:
        stats = getattr(self, "_conformance_session_run_stats", None)
        if not isinstance(stats, dict):
            return ""

        def _one(mode: str, label: str) -> str:
            counts = stats.get(mode)
            if not isinstance(counts, dict):
                counts = {}
            p = int(counts.get("PASS", 0))
            f = int(counts.get("FAIL", 0))
            s = int(counts.get("STOP", 0))
            if not (p or f or s):
                return f"{label}: 없음"
            parts = [f"PASS {p}" if p else "", f"FAIL {f}" if f else "", f"STOP {s}" if s else ""]
            return f"{label}: " + ", ".join(x for x in parts if x)

        return "  |  ".join((_one("repeat", "반복시험"), _one("manual_repeat", "수동 반복시험")))

    def _conformance_refresh_last_run_cache_from_progress(self) -> None:
        by_script: dict[str, Any] = {}
        for fname, ent in self._conformance_progress.items():
            if isinstance(ent, dict) and ent.get("rc") is not None:
                by_script[fname] = {"rc": ent.get("rc"), "status": ent.get("status")}
                self._conformance_commit_final_result(fname, ent.get("rc"), ent.get("status"))
        if not by_script:
            self._conformance_last_run_snapshot_cache = None
            return
        self._conformance_last_run_snapshot_cache = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "by_script": by_script,
            "summary": self._conformance_summarize_pass_fail_counts(by_script),
        }

    def _conformance_meta_for_script(self, fname: str) -> tuple[str, str]:
        ref = self._conformance_spec_ref_map().get(fname, "—")
        summ = ""
        for fn, r, s in self._conformance_test_rows():
            if fn == fname:
                return r, s
        return ref, summ

    def _conformance_commit_final_result(self, fname: str, rc: Any, status: Any) -> None:
        if rc is None:
            return
        try:
            rc_i = int(rc)
        except (TypeError, ValueError):
            return
        ref, summ = self._conformance_meta_for_script(fname)
        st = str(status or "").upper()
        store = getattr(self, "_conformance_final_results", None)
        if not isinstance(store, dict):
            self._conformance_final_results = {}
            store = self._conformance_final_results
        updated_at = datetime.now().isoformat(timespec="seconds")
        entry: dict[str, Any] = {
            "rc": rc_i,
            "status": st,
            "updated_at": updated_at,
            "ref": ref,
            "summary": summ,
            "judgement_summary": self._conformance_step_judgement_summary(fname),
            "step_judgements": self._conformance_collect_step_judgements(fname),
        }
        # 3.1.5.1/2: 전체 결과 「기록 시각」에 상세 Sync 천이 시간 기록
        if fname in ("conformance_3151.sh", "conformance_3152.sh"):
            sync_ts = self._conformance_sync_transition_record_time(fname)
            if sync_ts:
                entry["sync_transition_time"] = sync_ts
        store[fname] = entry
        self.after(0, self._conformance_refresh_results_summary_window)

    def _conformance_collect_step_judgements(self, fname: str) -> list[dict[str, str]]:
        """Collect STEP-level verdict/evidence lines from captured logs."""
        lk = getattr(self, "_conformance_detail_lock", None)
        lines: list[str] = []
        if lk is not None:
            with lk:
                lines = list(self._conformance_detail_lines.get(fname, ()))
        else:
            lines = list(getattr(self, "_conformance_detail_lines", {}).get(fname, ()))

        out: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        def _add(step: str, verdict: str, evidence: str) -> None:
            step_s = str(step or "").strip() or "-"
            verdict_s = str(verdict or "").strip().upper() or "INFO"
            evidence_s = str(evidence or "").strip()
            key = (step_s, verdict_s, evidence_s)
            if not evidence_s or key in seen:
                return
            seen.add(key)
            out.append({"step": step_s, "verdict": verdict_s, "evidence": evidence_s})

        for ln in lines:
            sl = self._conformance_detail_strip_run_tag(ln)
            m_std = re.search(r"\[(OK|NOK)\]\s*STEP\s*(\d+)\.\s*(.+)", sl, re.I)
            if m_std:
                _add(m_std.group(2), "PASS" if m_std.group(1).upper() == "OK" else "FAIL", sl)
                continue
            m_alt = re.search(
                r"STEP\s*(\d+)\.\s*(?:CallHome|Subscription|Supervision)\s*:\s*(\S+)",
                sl,
                re.I,
            )
            if m_alt:
                token = m_alt.group(2).upper()
                _add(m_alt.group(1), "PASS" if token == "OK" else "FAIL", sl)

        if not out:
            for ln in lines:
                sl = self._conformance_detail_strip_run_tag(ln)
                if "[FAIL]" in sl or "RUN ERROR:" in sl:
                    _add("-", "FAIL", sl)

        return out

    def _conformance_format_step_judgement_line(
        self, entry: dict[str, str], *, max_evidence: int = 100
    ) -> str:
        ev = str(entry.get("evidence") or "").strip()
        if len(ev) > max_evidence:
            ev = ev[:max_evidence].rstrip() + "..."
        return f"STEP {entry.get('step', '-')} {entry.get('verdict', 'INFO')}: {ev}"

    def _conformance_step_judgement_summary(self, fname: str) -> str:
        entries = self._conformance_collect_step_judgements(fname)
        if not entries:
            return "판단 근거 없음"
        return "\n".join(self._conformance_format_step_judgement_line(e) for e in entries)

    def _conformance_detail_lines_for(self, fname: str) -> list[str]:
        lk = getattr(self, "_conformance_detail_lock", None)
        if lk is not None:
            with lk:
                return list(self._conformance_detail_lines.get(fname, ()))
        return list(getattr(self, "_conformance_detail_lines", {}).get(fname, ()))

    @staticmethod
    def _conformance_fmt_elapsed_hms(from_t: datetime | None, to_t: datetime | None) -> str:
        if from_t is None or to_t is None:
            return "—"
        sec = int((to_t - from_t).total_seconds())
        sign = "-" if sec < 0 else ""
        sec = abs(sec)
        hh = sec // 3600
        mm = (sec % 3600) // 60
        ss = sec % 60
        return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}"

    @staticmethod
    def _conformance_norm_sync_state(value: str) -> str:
        return re.sub(r"[\s_\-]+", "", (value or "").strip().upper())

    @staticmethod
    def _conformance_nearest_event_time(text: str, pos: int) -> str | None:
        chunk = text[max(0, pos - 8000):pos]
        hits = re.findall(r"<eventTime>([^<]+)</eventTime>", chunk, re.I)
        return hits[-1].strip() if hits else None

    @classmethod
    def _conformance_parse_sync_from_xml_blob(
        cls, blob: str
    ) -> tuple[datetime | None, datetime | None, datetime | None]:
        """Parse HOLDOVER/FREERUN/ALARM_OCCUR times from raw NETCONF notification XML."""
        if not blob.strip():
            return None, None, None
        norm = cls._conformance_norm_sync_state
        nearest = cls._conformance_nearest_event_time
        first: dict[str, datetime | None] = {"HOLDOVER": None, "FREERUN": None}
        alarm_t: datetime | None = None

        for m in re.finditer(r"<notification\b[^>]*>([\s\S]*?)</notification>", blob, re.I):
            block = m.group(1)
            etm = re.search(r"<eventTime>([^<]+)</eventTime>", block, re.I)
            if not etm:
                continue
            ts = cls._conformance_detail_extract_ts(etm.group(1).strip())
            if ts is None:
                continue
            pl = block.lower()
            if "synchronization-state-change" in pl or "sync-state" in pl:
                sm = re.search(r"<sync-state(?:\s[^>]*)?>\s*([^<]+?)\s*</sync-state>", block, re.I)
                if sm:
                    st = norm(sm.group(1))
                    if st in first and first[st] is None:
                        first[st] = ts
            if first["FREERUN"] is None and "ptp-state-change" in pl:
                pm = re.search(r"<ptp-state(?:\s[^>]*)?>\s*([^<]+?)\s*</ptp-state>", block, re.I)
                if pm and norm(pm.group(1)) == "FREERUN":
                    first["FREERUN"] = ts
            if alarm_t is None and "fault-id" in pl:
                if re.search(r"<is-cleared>\s*false\s*</is-cleared>", block, re.I):
                    alarm_t = ts

        for m in re.finditer(r"<sync-state(?:\s[^>]*)?>\s*([^<]+?)\s*</sync-state>", blob, re.I):
            st = norm(m.group(1))
            if st not in first or first[st] is not None:
                continue
            ts_raw = nearest(blob, m.start())
            if not ts_raw:
                continue
            ts = cls._conformance_detail_extract_ts(ts_raw)
            if ts is not None:
                first[st] = ts

        if first["FREERUN"] is None:
            for m in re.finditer(r"<ptp-state(?:\s[^>]*)?>\s*([^<]+?)\s*</ptp-state>", blob, re.I):
                if norm(m.group(1)) != "FREERUN":
                    continue
                ts_raw = nearest(blob, m.start())
                if not ts_raw:
                    continue
                ts = cls._conformance_detail_extract_ts(ts_raw)
                if ts is not None:
                    first["FREERUN"] = ts
                    break

        if alarm_t is None:
            for m in re.finditer(
                r"<is-cleared>\s*false\s*</is-cleared>",
                blob,
                re.I,
            ):
                ts_raw = nearest(blob, m.start())
                if not ts_raw:
                    continue
                ts = cls._conformance_detail_extract_ts(ts_raw)
                if ts is not None:
                    alarm_t = ts
                    break

        return first["HOLDOVER"], first["FREERUN"], alarm_t

    def _conformance_parse_sync_event_times(
        self, lines: list[str]
    ) -> tuple[datetime | None, datetime | None, datetime | None]:
        """Parse [TIME] markers first, then fall back to raw notification XML in the buffer."""
        holdover_t: datetime | None = None
        freerun_t: datetime | None = None
        alarm_t: datetime | None = None
        for raw in lines:
            s = self._conformance_detail_strip_run_tag(raw)
            m_hold = re.search(r"\[TIME\]\s*HOLDOVER_EVENT_TIME\s*=\s*(\S+)", s, re.I)
            if m_hold:
                holdover_t = holdover_t or self._conformance_detail_extract_ts(m_hold.group(1))
            m_free = re.search(r"\[TIME\]\s*FREERUN_EVENT_TIME\s*=\s*(\S+)", s, re.I)
            if m_free:
                freerun_t = freerun_t or self._conformance_detail_extract_ts(m_free.group(1))
            m_alarm = re.search(r"\[TIME\]\s*ALARM_OCCUR_EVENT_TIME\s*=\s*(\S+)", s, re.I)
            if m_alarm:
                alarm_t = alarm_t or self._conformance_detail_extract_ts(m_alarm.group(1))

        if holdover_t is None or freerun_t is None or alarm_t is None:
            blob = "\n".join(self._conformance_detail_strip_run_tag(ln) for ln in lines)
            h2, f2, a2 = self._conformance_parse_sync_from_xml_blob(blob)
            holdover_t = holdover_t or h2
            freerun_t = freerun_t or f2
            alarm_t = alarm_t or a2
        return holdover_t, freerun_t, alarm_t

    def _conformance_sync_transition_record_time(self, fname: str, lines: list[str] | None = None) -> str:
        """Compact Sync 천이 TIME for 3.1.5.1/2 기록 시각 column (e.g. H→F 00:01:23 / H→A 00:01:40)."""
        if fname not in ("conformance_3151.sh", "conformance_3152.sh"):
            return ""
        src = lines if lines is not None else self._conformance_detail_lines_for(fname)
        holdover_t, freerun_t, alarm_t = self._conformance_parse_sync_event_times(src)
        if holdover_t is None and freerun_t is None and alarm_t is None:
            return ""
        hf = self._conformance_fmt_elapsed_hms(holdover_t, freerun_t)
        ha = self._conformance_fmt_elapsed_hms(holdover_t, alarm_t)
        return f"H→F {hf} / H→A {ha}"

    def _conformance_result_record_time_cell(self, fname: str, ent: dict[str, Any] | None) -> str:
        """Value for 전체 결과 「기록 시각」. 3.1.5.1/2 use Sync 천이 TIME when available."""
        if isinstance(ent, dict):
            sync_ts = str(ent.get("sync_transition_time") or "").strip()
            if sync_ts:
                return sync_ts
        if fname in ("conformance_3151.sh", "conformance_3152.sh"):
            live = self._conformance_sync_transition_record_time(fname)
            if live:
                return live
        if isinstance(ent, dict):
            return str(ent.get("updated_at") or "—")
        return "—"

    def _conformance_apply_final_results_from_config(self, raw: Any) -> None:
        if not isinstance(raw, dict) or not raw:
            self._conformance_final_results = {}
            self.after(0, self._conformance_refresh_results_summary_window)
            return
        try:
            self._conformance_final_results = json.loads(json.dumps(raw))
        except Exception:
            self._conformance_final_results = {}
        self.after(0, self._conformance_refresh_results_summary_window)

    @staticmethod
    def _conformance_result_label(rc: Any, status: str) -> tuple[str, str]:
        st = str(status or "").upper()
        if rc == -2 or st == "STOP":
            return "STOP", "res_stop"
        if rc == 0 or st == "PASS":
            return "PASS", "res_pass"
        if st == "FAIL" or (isinstance(rc, int) and rc not in (0, -2)):
            return (f"FAIL ({rc})" if isinstance(rc, int) else "FAIL"), "res_fail"
        return (st or "—", "res_mixed")

    def _conformance_summarize_final_results(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "STOP": 0, "NONE": 0, "RUN": 0, "WAIT": 0}
        active = getattr(self, "_conformance_run_active_targets", None)
        busy = bool(getattr(self, "_conformance_run_busy", False))
        for fname, _ref, _summ in self._conformance_test_rows():
            pr = self._conformance_progress.get(fname)
            if isinstance(pr, dict) and pr.get("status") == "RUN" and pr.get("rc") is None:
                out["RUN"] += 1
                continue
            if isinstance(active, set) and fname in active and busy:
                ent = self._conformance_final_results.get(fname)
                if not (isinstance(ent, dict) and ent.get("rc") is not None):
                    out["WAIT"] += 1
                    continue
            ent = self._conformance_final_results.get(fname)
            if not isinstance(ent, dict) or ent.get("rc") is None:
                out["NONE"] += 1
                continue
            rc, st = ent.get("rc"), str(ent.get("status") or "").upper()
            if rc == -2 or st == "STOP":
                out["STOP"] += 1
            elif rc == 0 or st == "PASS":
                out["PASS"] += 1
            else:
                out["FAIL"] += 1
        return out

    def _conformance_format_final_results_summary_line(self) -> str:
        s = self._conformance_summarize_final_results()
        parts = []
        if s.get("PASS"):
            parts.append(f"PASS {s['PASS']}")
        if s.get("FAIL"):
            parts.append(f"FAIL {s['FAIL']}")
        if s.get("STOP"):
            parts.append(f"STOP {s['STOP']}")
        if s.get("RUN"):
            parts.append(f"실행 중 {s['RUN']}")
        if s.get("WAIT"):
            parts.append(f"대기 {s['WAIT']}")
        if s.get("NONE"):
            parts.append(f"미실행 {s['NONE']}")
        line = ", ".join(parts) if parts else "기록 없음"
        latest = ""
        for ent in self._conformance_final_results.values():
            if isinstance(ent, dict):
                ts = str(ent.get("updated_at") or "").strip()
                if ts and (not latest or ts > latest):
                    latest = ts
        if latest:
            line += f"  |  최종 기록: {latest}"
        session_line = self._conformance_format_session_run_stats_line()
        if session_line:
            line += f"  |  {session_line}"
        return line

    def _conformance_results_summary_script_data(
        self,
        fname: str,
        ref: str,
        summ: str,
        *,
        active: Any,
        busy: bool,
    ) -> dict[str, Any]:
        """Build live summary data for one script (tree + Excel share this)."""
        ent = self._conformance_final_results.get(fname)
        pr = self._conformance_progress.get(fname)
        text = "미실행"
        tag = "res_idle"
        record_time = "—"
        updated_at = "—"

        if isinstance(pr, dict) and pr.get("status") == "RUN" and pr.get("rc") is None:
            text, tag = "실행 중", "res_run"
        elif isinstance(active, set) and fname in active and busy:
            if isinstance(ent, dict) and ent.get("rc") is not None:
                text, tag = self._conformance_result_label(ent.get("rc"), ent.get("status"))
                record_time = self._conformance_result_record_time_cell(fname, ent)
                updated_at = str(ent.get("updated_at") or "—")
            else:
                text, tag = "대기", "res_wait"
        elif isinstance(ent, dict) and ent.get("rc") is not None:
            text, tag = self._conformance_result_label(ent.get("rc"), ent.get("status"))
            record_time = self._conformance_result_record_time_cell(fname, ent)
            updated_at = str(ent.get("updated_at") or "—")
            ref = str(ent.get("ref") or ref)
            summ = str(ent.get("summary") or summ)

        step_entries: list[dict[str, str]] = []
        if isinstance(ent, dict):
            raw_steps = ent.get("step_judgements")
            if isinstance(raw_steps, list):
                step_entries = [se for se in raw_steps if isinstance(se, dict)]
        if not step_entries:
            step_entries = self._conformance_collect_step_judgements(fname)

        step_lines = [self._conformance_format_step_judgement_line(se) for se in step_entries]
        jtxt = ""
        if isinstance(ent, dict):
            jtxt = str(ent.get("judgement_summary") or "")
        if not jtxt:
            jtxt = self._conformance_step_judgement_summary(fname)
        if not step_lines and " | " in jtxt:
            step_lines = [p.strip() for p in jtxt.split(" | ") if p.strip()]

        return {
            "ref": ref,
            "script": fname,
            "summary": summ,
            "result": text,
            "result_tag": tag,
            "record_time": record_time,
            "updated_at": updated_at,
            "repeat_pf": self._conformance_format_session_pf_cell(fname, "repeat"),
            "manual_pf": self._conformance_format_session_pf_cell(fname, "manual_repeat"),
            "step_lines": step_lines,
            "step_entries": step_entries,
            "judgement_fallback": jtxt,
        }

    def _conformance_iter_summary_display_rows(
        self,
    ) -> list[tuple[dict[str, Any], str, bool]]:
        """Rows for tree/Excel: (script_data, judgement_cell, is_main_row)."""
        active = getattr(self, "_conformance_run_active_targets", None)
        busy = bool(getattr(self, "_conformance_run_busy", False))
        out: list[tuple[dict[str, Any], str, bool]] = []
        for fname, ref, summ in self._conformance_test_rows():
            data = self._conformance_results_summary_script_data(fname, ref, summ, active=active, busy=busy)
            step_lines = data["step_lines"]
            fallback = str(data.get("judgement_fallback") or "")
            if len(step_lines) <= 1:
                out.append((data, step_lines[0] if step_lines else fallback, True))
                continue
            out.append((data, step_lines[0], True))
            for line in step_lines[1:]:
                out.append((data, line, False))
        return out

    def _conformance_refresh_results_summary_window(self) -> None:
        tree = getattr(self, "conformance_results_summary_tree", None)
        if tree is None:
            return
        try:
            win = getattr(self, "_conformance_results_summary_win", None)
            if win is None or not win.winfo_exists():
                return
        except tk.TclError:
            return
        summ_var = getattr(self, "conformance_results_summary_summary_var", None)
        if summ_var is not None:
            summ_var.set(self._conformance_format_final_results_summary_line())
        for iid in tree.get_children(""):
            tree.delete(iid)
        row_idx = 0
        step_sub_idx: dict[str, int] = {}
        for data, judgement, is_main in self._conformance_iter_summary_display_rows():
            tag = data["result_tag"]
            fname = data["script"]
            row_tag = "row_even" if row_idx % 2 == 0 else "row_odd"
            if is_main:
                tree.insert(
                    "",
                    "end",
                    iid=fname,
                    values=(
                        data["ref"],
                        fname,
                        data["summary"],
                        judgement,
                        data["repeat_pf"],
                        data["manual_pf"],
                        data["result"],
                        data["record_time"],
                    ),
                    tags=(tag, row_tag),
                )
                step_sub_idx[fname] = 0
            else:
                step_sub_idx[fname] = step_sub_idx.get(fname, 0) + 1
                tree.insert(
                    "",
                    "end",
                    iid=f"{fname}#step{step_sub_idx[fname]}",
                    values=("", "", "", judgement, "", "", "", ""),
                    tags=(tag, row_tag),
                )
            row_idx += 1

    def _conformance_close_results_summary_window(self) -> None:
        w = getattr(self, "_conformance_results_summary_win", None)
        self._conformance_results_summary_win = None
        self.conformance_results_summary_tree = None
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def _conformance_open_results_summary_window(self) -> None:
        w = getattr(self, "_conformance_results_summary_win", None)
        if w is not None:
            try:
                if w.winfo_exists():
                    w.lift()
                    self._conformance_refresh_results_summary_window()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self)
        win.title("Conformance 전체 결과 (최종)")
        win.geometry("1480x680")
        self._conformance_results_summary_win = win
        self.conformance_results_summary_summary_var = tk.StringVar(value="")
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        ttk.Label(
            top,
            text="항목별 최종 결과입니다. 시험이 끝나면 PASS/FAIL/STOP으로 갱신되며, 설정 JSON에도 저장됩니다. "
            "반복시험/수동 반복시험 PASS·FAIL 횟수는 GUI 실행 후 누적됩니다(재부팅·반복해도 유지). "
            "「최종 결과」열은 가장 최근 1회 판정입니다.",
            foreground="#475569",
            wraplength=900,
            justify="left",
        ).pack(anchor="w")
        ttk.Label(top, textvariable=self.conformance_results_summary_summary_var, foreground="#0f766e").pack(
            anchor="w", pady=(6, 0)
        )
        body = ttk.Frame(win, padding=(8, 0, 8, 8))
        body.pack(fill="both", expand=True)
        cols = ("ref", "script", "summary", "judgement", "repeat_pf", "manual_pf", "result", "updated")
        tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        self.conformance_results_summary_tree = tree
        tree.heading("ref", text="표 참조")
        tree.column("ref", width=88, anchor="center", stretch=False)
        tree.heading("script", text="스크립트")
        tree.column("script", width=180, anchor="w", stretch=False)
        tree.heading("summary", text="개요")
        tree.column("summary", width=240, anchor="w", stretch=False)
        tree.heading("judgement", text="STEP 판단 (한 줄씩)")
        tree.column("judgement", width=360, anchor="w", stretch=True)
        tree.heading("repeat_pf", text="반복시험 누적 (P/F)")
        tree.column("repeat_pf", width=160, anchor="center", stretch=False)
        tree.heading("manual_pf", text="수동 반복 누적 (P/F)")
        tree.column("manual_pf", width=160, anchor="center", stretch=False)
        tree.heading("result", text="최종 결과(최근1회)")
        tree.column("result", width=120, anchor="center", stretch=False)
        tree.heading("updated", text="기록 시각")
        tree.column("updated", width=220, anchor="center", stretch=False)
        for tag in ("res_idle", "res_wait", "res_run", "res_pass", "res_fail", "res_stop", "res_mixed"):
            tree.tag_configure(tag, foreground={
                "res_idle": "#94a3b8",
                "res_wait": "#ca8a04",
                "res_run": "#d97706",
                "res_pass": "#15803d",
                "res_fail": "#b91c1c",
                "res_stop": "#64748b",
                "res_mixed": "#334155",
            }.get(tag, "#334155"))
        tree.tag_configure("row_even", background="#ffffff")
        tree.tag_configure("row_odd", background="#f0f4f8")
        ys = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        bf = ttk.Frame(win, padding=8)
        bf.pack(fill="x")
        ttk.Button(bf, text="새로고침", command=self._conformance_refresh_results_summary_window).pack(side="left")
        ttk.Button(
            bf,
            text="누적 초기화",
            command=self._conformance_reset_session_run_stats_from_ui,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(bf, text="Excel 저장", command=self._conformance_export_results_excel).pack(side="left", padx=(8, 0))
        ttk.Button(bf, text="닫기", command=self._conformance_close_results_summary_window).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", self._conformance_close_results_summary_window)
        self._conformance_refresh_results_summary_window()

    def _conformance_export_results_excel(self) -> None:
        """Export conformance final results and step judgements to Excel."""
        try:
            from openpyxl import Workbook
        except Exception as exc:
            messagebox.showerror("Conformance", f"openpyxl import failed:\n{exc}")
            return

        product = ""
        try:
            fv = getattr(self, "fields", {}).get("PRODUCT")
            product = (fv.get() if fv is not None else "") or ""
        except Exception:
            product = ""
        product = re.sub(r"[^0-9A-Za-z._-]+", "_", product.strip()) or "PRODUCT"
        date_s = datetime.now().strftime("%Y%m%d")
        default_name = f"{product}_{date_s}.xlsx"
        out_path = filedialog.asksaveasfilename(
            title="Conformance 결과 저장",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
        )
        if not out_path:
            return

        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"
        ws.append(
            [
                "Spec Ref",
                "Script",
                "Summary",
                "STEP Judgement",
                "Repeat Test (P/F)",
                "Manual Repeat Test (P/F)",
                "Result",
                "Record Time",
                "Completed At",
            ]
        )

        step_rows: list[list[str]] = [["Spec Ref", "Script", "Step", "Verdict", "Evidence"]]

        for data, judgement, is_main in self._conformance_iter_summary_display_rows():
            if is_main:
                ws.append(
                    [
                        data["ref"],
                        data["script"],
                        data["summary"],
                        judgement,
                        data["repeat_pf"],
                        data["manual_pf"],
                        data["result"],
                        data["record_time"],
                        data["updated_at"],
                    ]
                )
            else:
                ws.append(["", "", "", judgement, "", "", "", "", ""])

        active = getattr(self, "_conformance_run_active_targets", None)
        busy = bool(getattr(self, "_conformance_run_busy", False))
        for fname, ref, summ in self._conformance_test_rows():
            data = self._conformance_results_summary_script_data(fname, ref, summ, active=active, busy=busy)
            for se in data["step_entries"]:
                if not isinstance(se, dict):
                    continue
                step_rows.append(
                    [
                        data["ref"],
                        fname,
                        str(se.get("step", "-")),
                        str(se.get("verdict", "INFO")),
                        str(se.get("evidence", "")),
                    ]
                )

        ws2 = wb.create_sheet("Step Judgements")
        for row in step_rows:
            ws2.append(row)

        try:
            wb.save(out_path)
        except Exception as exc:
            messagebox.showerror("Conformance", f"Excel 저장 실패:\n{exc}")
            return
        self.append_log(f"[GUI] Conformance result excel saved: {out_path}\n")
        messagebox.showinfo("Conformance", f"저장 완료:\n{out_path}")

    def _conformance_apply_last_run_from_config(self, raw: Any) -> None:
        if raw is None or (isinstance(raw, dict) and not raw.get("by_script")):
            self._conformance_last_run_snapshot_cache = None
            if hasattr(self, "conformance_last_run_hint_var"):
                self.conformance_last_run_hint_var.set("")
            self.after(0, self._conformance_refresh_row_result_labels)
            return
        try:
            self._conformance_last_run_snapshot_cache = json.loads(json.dumps(raw))
        except Exception:
            self._conformance_last_run_snapshot_cache = None
            self.after(0, self._conformance_refresh_row_result_labels)
            return
        snap = self._conformance_last_run_snapshot_cache
        if isinstance(snap, dict):
            bs = snap.get("by_script")
            summ = snap.get("summary")
            if not isinstance(summ, dict) and isinstance(bs, dict):
                summ = self._conformance_summarize_pass_fail_counts(bs)
            cnt = self._conformance_format_pass_fail_counts_ko(summ) if isinstance(summ, dict) else ""
            ts = str(snap.get("saved_at") or "").strip()
            if hasattr(self, "conformance_last_run_hint_var"):
                if ts and cnt:
                    self.conformance_last_run_hint_var.set(f"저장된 마지막 실행 요약: {cnt} (시각 {ts})")
                elif cnt:
                    self.conformance_last_run_hint_var.set(f"저장된 마지막 실행 요약: {cnt}")
                else:
                    self.conformance_last_run_hint_var.set("저장된 마지막 Conformance 실행 기록을 불러왔습니다.")
        self.after(0, self._conformance_refresh_row_result_labels)

    def _conformance_attach_check_var_trace(self, fname: str, bv: tk.BooleanVar) -> None:
        def _on_bv_write(*_a: Any, fn: str = fname) -> None:
            if (
                fn in self._conformance_scripts_318x()
                and not getattr(self, "_conformance_318x_link_busy", False)
            ):
                bv_local = self.conformance_check_vars.get(fn)
                if bv_local is not None:
                    self._conformance_set_318x_linked_check(bool(bv_local.get()))
            self._conformance_sync_tree_pick(fn)
            try:
                self._on_any_setting_changed()
            except Exception:
                pass

        try:
            bv.trace_add("write", _on_bv_write)
        except Exception:
            pass

    def _conformance_rebuild_list_tree(self) -> None:
        """Fill or refresh the Conformance table from local ./conformance scripts."""
        tree = getattr(self, "conformance_list_tree", None)
        if tree is None:
            return
        if not hasattr(self, "conformance_reboot_vars") or self.conformance_reboot_vars is None:
            self.conformance_reboot_vars = {}
        rows = self._conformance_test_rows()
        expected_ids = {fname for fname, _ref, _summ in rows}
        existing_ids = set(tree.get_children(""))
        if existing_ids == expected_ids:
            for fname, _ref, _summ in rows:
                if not tree.exists(fname):
                    continue
                lp = self._conformance_script_local_path(fname)
                st = "Ready" if lp is not None else "miss"
                try:
                    tree.set(fname, "local", st)
                    bv = self.conformance_check_vars.get(fname)
                    if bv is not None:
                        tree.set(fname, "pick", "☑" if bv.get() else "☐")
                    rbv = self.conformance_reboot_vars.get(fname)
                    if rbv is not None:
                        tree.set(fname, "reboot", "☑" if rbv.get() else "☐")
                except tk.TclError:
                    pass
            return

        saved_checks = {
            fn: bool(bv.get())
            for fn, bv in self.conformance_check_vars.items()
            if tree.exists(fn) or fn in expected_ids
        }
        saved_reboots = {
            fn: bool(bv.get())
            for fn, bv in self.conformance_reboot_vars.items()
            if tree.exists(fn) or fn in expected_ids
        }
        for iid in list(tree.get_children("")):
            try:
                tree.delete(iid)
            except tk.TclError:
                pass
        self.conformance_check_vars.clear()
        self.conformance_reboot_vars.clear()
        if not hasattr(self, "_conformance_row_parity") or self._conformance_row_parity is None:
            self._conformance_row_parity = {}
        self._conformance_row_parity.clear()

        tree.configure(height=min(28, max(8, len(rows) or 8)))
        for idx, (fname, ref, summ) in enumerate(rows):
            bv = tk.BooleanVar(value=saved_checks.get(fname, False))
            self.conformance_check_vars[fname] = bv
            rbv = tk.BooleanVar(value=saved_reboots.get(fname, False))
            self.conformance_reboot_vars[fname] = rbv
            lp = self._conformance_script_local_path(fname)
            loc = "Ready" if lp is not None else "miss"
            pick = "☑" if bv.get() else "☐"
            reboot = "☑" if rbv.get() else "☐"
            cfg_mark = "⚙" if fname in _CONFORMANCE_PER_TEST_SCHEMA else ""
            row_tag = "row_even" if idx % 2 == 0 else "row_odd"
            self._conformance_row_parity[fname] = row_tag
            tree.insert(
                "",
                "end",
                iid=fname,
                values=(pick, reboot, fname, ref, summ, loc, cfg_mark, "—"),
                tags=("res_idle", row_tag),
            )
            self._conformance_attach_check_var_trace(fname, bv)

    def _conformance_refresh_status_labels(self) -> None:
        if self.conformance_path_hint_var is not None:
            n = len(self._conformance_test_rows())
            rd = self.conformance_run_remote_dir_var.get().strip() or _conf_manifest.CONFORMANCE_REMOTE_DIR
            pv = ""
            try:
                v = self.fields.get("PRODUCT")
                if v is not None:
                    pv = str(v.get()).strip()
            except Exception:
                pass
            safe = re.sub(r"[^0-9A-Za-z._-]+", "_", pv).strip("_") or "UNKNOWN"
            self.conformance_path_hint_var.set(
                f"로컬: {self._conformance_local_dir()}  |  원격: {rd}  |  시험 {n}건  |  "
                f"스크립트 작업 경로: /var/tmp/netconf_tmp/ (edit, get, FIFO, XML)\n"
                f"원격 세션 전체 로그(tee): {_conf_manifest.CONFORMANCE_REMOTE_DIR}/logs/{safe}/CONF_{safe}_<yymmdd_HHMMSS>_<스크립트>.log"
            )
        self._conformance_rebuild_list_tree()
        self._conformance_refresh_row_result_labels()

    def _conformance_refresh_row_result_labels(self) -> None:
        snap = getattr(self, "_conformance_last_run_snapshot_cache", None)
        bs: dict[str, Any] = {}
        if isinstance(snap, dict):
            raw_bs = snap.get("by_script")
            if isinstance(raw_bs, dict):
                bs = raw_bs
        active = getattr(self, "_conformance_run_active_targets", None)
        busy = bool(getattr(self, "_conformance_run_busy", False))
        tree = getattr(self, "conformance_list_tree", None)
        if tree is None:
            return
        for fname, _r, _en in self._conformance_test_rows():
            if not tree.exists(fname):
                continue
            try:
                text, tag = "—", "res_idle"
                pr = self._conformance_progress.get(fname)
                if isinstance(pr, dict) and pr.get("status") == "RUN" and pr.get("rc") is None:
                    text, tag = "실행 중", "res_run"
                elif isinstance(pr, dict) and pr.get("rc") is not None:
                    rc, st = pr.get("rc"), str(pr.get("status") or "").upper()
                    if rc == -2 or st == "STOP":
                        text, tag = "STOP", "res_stop"
                    elif rc == 0 or st == "PASS":
                        text, tag = "PASS", "res_pass"
                    elif st == "FAIL" or (isinstance(rc, int) and rc != 0):
                        text, tag = (f"FAIL ({rc})", "res_fail")
                    else:
                        text, tag = (st or "—", "res_mixed")
                elif isinstance(active, set) and fname in active and busy:
                    text, tag = "대기", "res_wait"
                else:
                    fin = self._conformance_final_results.get(fname)
                    if isinstance(fin, dict) and fin.get("rc") is not None:
                        text, tag = self._conformance_result_label(fin.get("rc"), fin.get("status"))
                    else:
                        prev = bs.get(fname)
                        if isinstance(prev, dict) and prev.get("rc") is not None:
                            text, tag = self._conformance_result_label(prev.get("rc"), prev.get("status"))
                        else:
                            text, tag = "—", "res_idle"
                tree.set(fname, "result", text)
                row_tag = self._conformance_row_parity.get(fname, "row_even")
                tree.item(fname, tags=(tag, row_tag))
            except tk.TclError:
                pass
        self.after(0, self._conformance_refresh_results_summary_window)

    def _conformance_parse_repeat_count(self) -> int | None:
        """Return repeat count: 0=infinite, 1+=finite. None if invalid."""
        raw = self.conformance_run_repeat_var.get().strip()
        if not raw:
            return 1
        try:
            n = int(raw)
        except ValueError:
            return None
        if n < 0:
            return None
        return n

    def _conformance_parse_reboot_wait_sec(self) -> int:
        """Seconds to wait after ORU reboot/reset before next test."""
        var = getattr(self, "conformance_reboot_wait_var", None)
        raw = (var.get().strip() if var is not None else "") or "360"
        try:
            n = int(raw)
        except ValueError:
            return 360
        return max(0, n)

    def _conformance_reboot_checked(self, fname: str) -> bool:
        bv = getattr(self, "conformance_reboot_vars", {}).get(fname)
        try:
            return bool(bv.get()) if bv is not None else False
        except Exception:
            return False

    def _conformance_wait_reboot(
        self, wait_s: int, log_line: Any, *, label: str = "ORU 재부팅"
    ) -> bool:
        """Block until wait_s elapses. Return False if user cancelled."""
        if wait_s <= 0:
            log_line(f"{label} 대기 0초 — 즉시 다음 단계")
            return True
        log_line(f"{label} 대기 {wait_s}초 ({wait_s // 60}분 {wait_s % 60}초)")
        for elapsed in range(wait_s):
            if self._conformance_cancel_event.is_set():
                log_line(f"{label} 대기 중 사용자 중지")
                return False
            if elapsed > 0 and elapsed % 30 == 0:
                log_line(f"{label} 대기 중… {elapsed}/{wait_s}초")
            time.sleep(1)
        log_line(f"{label} 대기 {wait_s}초 완료")
        # Do not reset PASS/FAIL session counters here — repeat/reboot cycles must accumulate.
        return True

    def _conformance_trigger_oru_reboot(
        self,
        client: Any,
        sftp: Any,
        opts: ConformanceRunOptions,
        remote_dir: str,
        cfg_remote: str,
        log_line: Any,
    ) -> bool:
        """Send o-ran-operations <reset/>. Prefer Start FIFO; else dedicated Call Home helper script."""
        reset_body = '<reset xmlns="urn:o-ran:operations:1.0"/>\n'
        remote_path = "/var/tmp/netconf_tmp/edit/oru_gui_reset.xml"
        fifo = "/var/tmp/netconf_tmp/netconf_control.fifo"
        cmd = f"user-rpc --content {remote_path}"

        # --- Fast path: miniDU Start session still alive ---
        session_ok = bool(getattr(self, "is_running", False)) and (
            getattr(self, "session_established", False) or getattr(self, "manual_send_ready", False)
        )
        if session_ok:
            try:
                client.exec_command("mkdir -p /var/tmp/netconf_tmp/edit")
                with sftp.file(remote_path, "w") as fh:
                    fh.write(reset_body)
                try:
                    sftp.chmod(remote_path, 0o644)
                except OSError:
                    pass
            except Exception as exc:
                log_line(f"[WARN] 재부팅 RPC 파일 준비 실패(FIFO 경로): {exc}")
            else:
                sent = False
                try:
                    # timeout avoids hang when FIFO has no reader
                    sh = (
                        "if [ -p "
                        + shlex.quote(fifo)
                        + " ]; then timeout 3 bash -c "
                        + shlex.quote(f"printf '%s\\n' {cmd} > {fifo}")
                        + " && echo __OK__; else echo __MISSING__; fi"
                    )
                    _stdin, _stdout, _stderr = client.exec_command(sh)
                    out = (_stdout.read() or b"").decode(errors="ignore").strip()
                    if "__OK__" in out:
                        log_line(f"ORU 재부팅 RPC 전송 (FIFO): {cmd}")
                        sent = True
                    else:
                        log_line(f"[WARN] FIFO 전송 불가 ({out or 'empty'}) — Start 채널 재시도")
                except Exception as exc:
                    log_line(f"[WARN] FIFO 재부팅 전송 실패: {exc}")

                if not sent:

                    def _send() -> None:
                        try:
                            self._send_scheduler_payload(cmd)  # type: ignore[attr-defined]
                        except Exception as e:
                            err.append(str(e))
                        finally:
                            done.set()

                    done = threading.Event()
                    err: list[str] = []
                    self.after(0, _send)
                    if done.wait(timeout=20) and not err:
                        log_line(f"ORU 재부팅 RPC 전송 (Start 세션): {cmd}")
                        sent = True
                    elif err:
                        log_line(f"[WARN] Start 세션 재부팅 전송 실패: {err[0]}")

                if sent:
                    log_line("FIFO 전송 시도 완료 — Call Home helper로 reset을 확정 전송합니다")
                    # Do not return here: conformance tests often leave Start/FIFO session stale.

        # --- Reliable path: Call Home helper (owns listen port, sends reset) ---
        helper = "conformance_oru_reboot.sh"
        lp = self._conformance_script_local_path(helper)
        if lp is None:
            # Always look next to other conformance scripts even if not listed in test rows
            cand = self._conformance_local_dir() / helper
            if cand.is_file():
                lp = cand
        if lp is None:
            log_line(f"[ERROR] 재부팅 헬퍼 없음: {helper} (./conformance 에 필요)")
            return False

        rp = f"{remote_dir.rstrip('/')}/{helper}"
        try:
            sftp.put(str(lp), rp)
            try:
                sftp.chmod(rp, 0o755)
            except OSError:
                pass
            log_line(f"uploaded {helper}")
        except Exception as exc:
            log_line(f"[ERROR] 재부팅 헬퍼 업로드 실패: {exc}")
            return False

        # Refresh ORU JSON so helper sees current Settings
        try:
            cfg_payload = self._conformance_effective_config_json_text()
            cfg_bytes = cfg_payload.encode("utf-8")
            sftp.putfo(io.BytesIO(cfg_bytes), cfg_remote, len(cfg_bytes))
        except Exception as exc:
            log_line(f"[WARN] 재부팅용 config 갱신 실패(기존 config 사용): {exc}")

        envp = self._conformance_bash_env_exports(opts, None)
        host_log = self._conformance_host_run_log_path(helper)
        dir_q = shlex.quote(str(PurePosixPath(host_log).parent))
        log_q = shlex.quote(host_log)
        rp_q = shlex.quote(rp)
        cfg_q = shlex.quote(cfg_remote)
        listen_to = 90
        try:
            # M-Plane ⚙ reset_listen_sec 있으면 사용
            gf = getattr(self, "_guardrails_gf", None)
            if callable(gf):
                listen_to = max(30, int(gf("reset_listen_sec", "90") or "90"))
        except Exception:
            listen_to = 90
        runner = (
            f"{envp}"
            f"export CONFORMANCE_SCRIPT_BASENAME={shlex.quote(helper)} ; "
            f"export CALLHOME_LISTEN_TIMEOUT={int(listen_to)} ; "
            f"chmod +x {rp_q} 2>/dev/null ; bash {rp_q} --config {cfg_q}"
        )
        wrapped = (
            f"set -o pipefail; "
            f"mkdir -p {dir_q} && : > {log_q} && chmod 0644 {log_q} || exit 1; "
            f"( {runner} ) 2>&1 | tee -a {log_q}; "
            "_cf_rc=${PIPESTATUS[0]}; "
            'exit "${_cf_rc:-0}"'
        )
        cmd_remote = "bash -lc " + shlex.quote(wrapped)
        log_line(f"---- START {helper} (ORU reset) ----")
        log_line(f"remote host log file: {host_log}")
        log_line(
            "[INFO] 순서: CallHome 로그인 성공 → "
            "<reset xmlns=\"urn:o-ran:operations:1.0\"/> 전송. "
            f"listen≤{listen_to}s (이 단계 전에는 reset 미전송)"
        )
        try:
            _stdin, stdout, stderr = client.exec_command(cmd_remote, get_pty=True)
            ch = stdout.channel
            with self._conformance_run_transport_lock:
                self._conformance_run_script_channel = ch
            t_listen0 = time.monotonic()
            hard_cap = float(listen_to) + 45.0
            while not ch.exit_status_ready():
                cancel = bool(self._conformance_cancel_event.is_set())
                try:
                    if getattr(self, "_guardrails_cancel", None) is not None:
                        cancel = cancel or bool(self._guardrails_cancel.is_set())
                except Exception:
                    pass
                if cancel:
                    try:
                        ch.close()
                    except Exception:
                        pass
                    try:
                        client.exec_command(
                            f"pkill -f {shlex.quote(helper)} 2>/dev/null; "
                            f"fuser -k 4334/tcp 2>/dev/null || true"
                        )
                    except Exception:
                        pass
                    log_line("재부팅 헬퍼 실행 중 사용자 중지")
                    with self._conformance_run_transport_lock:
                        self._conformance_run_script_channel = None
                    return False
                if time.monotonic() - t_listen0 > hard_cap:
                    try:
                        ch.close()
                    except Exception:
                        pass
                    log_line(
                        f"재부팅 헬퍼 타임아웃 ({hard_cap:.0f}s) — "
                        "CallHome 미수신. LOCAL_IP:4334 / ALLOWED_IP·iptables 확인"
                    )
                    with self._conformance_run_transport_lock:
                        self._conformance_run_script_channel = None
                    return False
                if ch.recv_ready():
                    chunk = ch.recv(4096).decode(errors="ignore")
                    if chunk:
                        for line in chunk.splitlines():
                            log_line(line)
                else:
                    time.sleep(0.1)
            # drain remaining
            try:
                rem = stdout.read().decode(errors="ignore")
                for line in rem.splitlines():
                    log_line(line)
            except Exception:
                pass
            rc = ch.recv_exit_status()
            log_line(f"---- END {helper} exit={rc} ----")
            with self._conformance_run_transport_lock:
                self._conformance_run_script_channel = None
            if rc == 0:
                log_line("ORU reset RPC 전송 완료 (Call Home helper)")
                return True
            log_line(f"[WARN] 재부팅 헬퍼 실패 exit={rc}")
            return False
        except Exception as exc:
            log_line(f"[ERROR] 재부팅 헬퍼 실행 실패: {exc}")
            with self._conformance_run_transport_lock:
                self._conformance_run_script_channel = None
            return False

    def _conformance_maybe_reboot_after_test(
        self,
        fname: str,
        ordered_fnames: list[str],
        client: Any,
        sftp: Any,
        opts: ConformanceRunOptions,
        remote_dir: str,
        cfg_remote: str,
        log_line: Any,
    ) -> bool:
        """After a finished test: optional reboot+wait. Return True if run should abort."""
        try:
            idx = ordered_fnames.index(fname)
        except ValueError:
            idx = -1
        has_next = 0 <= idx < len(ordered_fnames) - 1
        want_reboot = self._conformance_reboot_checked(fname)

        if want_reboot:
            log_line(
                f"{fname} 완료 → ORU 재부팅 "
                + ("후 다음 선택 시험 진행" if has_next else "후 이번 반복 종료(다음 선택 없음)")
            )
            ok = self._conformance_trigger_oru_reboot(
                client, sftp, opts, remote_dir, cfg_remote, log_line
            )
            if not ok:
                log_line("[WARN] 재부팅 RPC 전송 실패 — 대기만 진행합니다")
            wait_s = self._conformance_parse_reboot_wait_sec()
            if fname == "conformance_3132.sh":
                try:
                    per = int(self._conformance_get_per_test_val(fname, "post_reset_wait_sec") or "")
                    if per >= 0:
                        wait_s = per
                except (ValueError, TypeError):
                    pass
            return not self._conformance_wait_reboot(wait_s, log_line, label="ORU 재부팅")

        if fname == "conformance_3132.sh" and has_next:
            wait_s = self._conformance_parse_reboot_wait_sec()
            try:
                per = int(self._conformance_get_per_test_val(fname, "post_reset_wait_sec") or "")
                if per >= 0:
                    wait_s = per
            except (ValueError, TypeError):
                pass
            if wait_s > 0:
                return not self._conformance_wait_reboot(wait_s, log_line, label="ORU 리셋")
        return False

    def _conformance_default_run_options(self) -> ConformanceRunOptions:
        rd = (self.conformance_run_remote_dir_var.get().strip() or _conf_manifest.CONFORMANCE_REMOTE_DIR).rstrip("/")
        return ConformanceRunOptions(
            remote_dir=rd,
            netconf_rpc_timeout=self.conformance_run_rpc_timeout_var.get(),
            netconf_idle_timeout=self.conformance_run_idle_timeout_var.get(),
            supervision_interval=self.conformance_run_supervision_interval_var.get(),
            supervision_reset_cycles=self.conformance_run_supervision_reset_cycles_var.get(),
            supervision_negative_fail_on_cycle=self.conformance_run_supervision_negative_fail_cycle_var.get(),
            conn_delay=self.conformance_run_conn_delay_var.get(),
            post_listen_wait_sec=self.conformance_post_listen_wait_var.get(),
        )

    def _conformance_sync_worker(self, force: bool, cred: tuple[str, str, str, str, str], silent: bool) -> None:
        ssh_user, ssh_host, ssh_port, ssh_password, key_path = cred
        remote_dir = (
            self.conformance_run_remote_dir_var.get().strip() or _conf_manifest.CONFORMANCE_REMOTE_DIR
        ).rstrip("/")

        def log_line(msg: str) -> None:
            self._conformance_log_lines_to_gui("Conformance-sync", msg)

        try:
            import paramiko  # type: ignore
        except Exception:
            if silent:
                self._conformance_auto_sync_scheduled = False
            else:
                self.after(0, lambda: messagebox.showerror("Conformance", "pip install paramiko 가 필요합니다."))
            return

        client: Any = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=ssh_host,
                port=int(ssh_port),
                username=ssh_user,
                password=ssh_password if ssh_password else None,
                key_filename=key_path if key_path else None,
                timeout=15,
                auth_timeout=15,
                banner_timeout=15,
                look_for_keys=not bool(ssh_password),
                allow_agent=True,
            )
            _stdin, _stdout, _stderr = client.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")
            if _stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(_stderr.read().decode(errors="ignore").strip() or "mkdir failed")
            self._conformance_remote_prepare_netconf_tmp(client, log_line)
            sftp = client.open_sftp()
            for fname in self._conformance_all_sync_script_names():
                lp = self._conformance_script_local_path(fname)
                if lp is None:
                    log_line(f"skip (no local): {fname}")
                    continue
                rp = f"{remote_dir}/{fname}"
                try:
                    if not force:
                        try:
                            sftp.stat(rp)
                            log_line(f"skip (remote exists): {fname}")
                            continue
                        except OSError:
                            pass
                    sftp.put(str(lp), rp)
                    try:
                        sftp.chmod(rp, 0o755)
                    except OSError:
                        pass
                    log_line(f"uploaded {fname}")
                except Exception as exc:
                    log_line(f"FAILED {fname}: {exc}")
            try:
                self._conformance_upload_mplane_helper_scripts(sftp, remote_dir, log_line)
                self._conformance_upload_mplane_templates(sftp, log_line)
            except Exception as exc:
                log_line(f"WARN M-Plane helper/template sync: {exc}")
            sftp.close()
            if not silent:
                self.after(0, lambda: messagebox.showinfo("Conformance", "스크립트 동기화 완료. 로그를 확인하세요."))
            else:
                log_line("auto-sync done")
        except Exception as exc:
            log_line(f"SYNC ERROR: {exc}")
            if not silent:
                self.after(0, lambda e=str(exc): messagebox.showerror("Conformance", f"동기화 실패:\n{e}"))
        finally:
            self._conformance_auto_sync_scheduled = False
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _conformance_run_worker(
        self,
        fnames: list[str],
        cred: tuple[str, str, str, str, str],
        opts: ConformanceRunOptions,
        repeat_count: int = 1,
    ) -> None:
        ssh_user, ssh_host, ssh_port, ssh_password, key_path = cred
        remote_dir = opts.remote_dir.rstrip("/")
        cfg_name = _conf_manifest.CONFORMANCE_REMOTE_GUI_CONFIG_NAME
        cfg_remote = f"{remote_dir}/{cfg_name}"

        def log_line(msg: str) -> None:
            self._conformance_log_lines_to_gui("Conformance-run", msg)
            self._conformance_detail_buffer_append(getattr(self, "_conformance_detail_capture_key", None), msg)

        try:
            import paramiko  # type: ignore
        except Exception:
            self.after(0, lambda: messagebox.showerror("Conformance", "pip install paramiko 가 필요합니다."))
            self.after(0, self._conformance_run_finished)
            return

        client: Any = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=ssh_host,
                port=int(ssh_port),
                username=ssh_user,
                password=ssh_password if ssh_password else None,
                key_filename=key_path if key_path else None,
                timeout=20,
                auth_timeout=20,
                banner_timeout=20,
                look_for_keys=not bool(ssh_password),
                allow_agent=True,
            )
            with self._conformance_run_transport_lock:
                self._conformance_run_ssh_client = client

            _stdin, _stdout, _stderr = client.exec_command(f"mkdir -p {shlex.quote(remote_dir)}")
            if _stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(_stderr.read().decode(errors="ignore").strip() or "mkdir failed")

            self._conformance_remote_prepare_netconf_tmp(client, log_line)

            sftp = client.open_sftp()

            to_upload: list[str] = []
            for fn in fnames:
                if fn not in to_upload:
                    to_upload.append(fn)
            pre_m = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")
            post_m = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_POST_3180_1", "")
            t8 = getattr(_conf_manifest, "CONFORMANCE_SCRIPTS_318X", frozenset())
            if any(f in t8 for f in fnames):
                for extra in (pre_m, post_m):
                    if extra and self._conformance_script_local_path(extra) and extra not in to_upload:
                        to_upload.insert(0, extra)

            for fname in to_upload:
                lp = self._conformance_script_local_path(fname)
                if lp is None:
                    raise RuntimeError(f"로컬 스크립트 없음: {fname}")
                rp = f"{remote_dir}/{fname}"
                sftp.put(str(lp), rp)
                try:
                    sftp.chmod(rp, 0o755)
                except OSError:
                    pass
                try:
                    import hashlib

                    _md5 = hashlib.md5(lp.read_bytes()).hexdigest()[:8]
                    log_line(f"uploaded {fname} (md5:{_md5})")
                except Exception:
                    log_line(f"uploaded {fname}")

            if any(f in _CONFORMANCE_NETPEER_UPLANE_INIT_SCRIPTS for f in fnames):
                if not self._conformance_upload_mplane_helper_scripts(sftp, remote_dir, log_line):
                    raise RuntimeError("U-Plane init helper 업로드 실패")
                self._conformance_upload_mplane_templates(sftp, log_line)

            spec_map = self._conformance_spec_ref_map()
            expanded_fnames = self._conformance_expand_run_list(list(fnames))
            ordered_fnames = self._conformance_order_run_list(expanded_fnames)
            if ordered_fnames != expanded_fnames:
                log_line("실행 순서: " + ", ".join(ordered_fnames))
            three8 = getattr(_conf_manifest, "CONFORMANCE_SCRIPTS_318X", frozenset())
            pre_b = self._conformance_3180_script_path(post_cleanup=False)
            post_b = self._conformance_3180_script_path(post_cleanup=True)
            ran_318x_pass = any(f in three8 for f in ordered_fnames)
            post_3180_after_3186 = False
            abort_all = False
            iteration = 0

            while True:
                iteration += 1
                if self._conformance_cancel_event.is_set():
                    log_line("사용자 중지로 중단")
                    abort_all = True
                    break

                if repeat_count == 0:
                    log_line(f"=== Conformance 반복 {iteration} (0=무한) ===")
                elif repeat_count > 1:
                    log_line(f"=== Conformance 반복 {iteration}/{repeat_count} ===")

                try:
                    cfg_payload = self._conformance_effective_config_json_text()
                except Exception as exc:
                    log_line(f"설정 JSON 오류 (반복 {iteration}): {exc}")
                    break

                cfg_bytes = cfg_payload.encode("utf-8")
                sftp.putfo(io.BytesIO(cfg_bytes), cfg_remote, len(cfg_bytes))
                try:
                    sftp.chmod(cfg_remote, 0o644)
                except OSError:
                    pass
                log_line(
                    f"merged ORU config (현재 Settings 반영) -> {cfg_remote} ({len(cfg_payload)} bytes)"
                )

                for fname in ordered_fnames:
                    if self._conformance_cancel_event.is_set():
                        log_line("사용자 중지로 중단")
                        self._conformance_progress[fname] = {"rc": -2, "status": "STOP"}
                        self._conformance_record_session_run_result(fname, -2, "STOP")
                        self._conformance_commit_final_result(fname, -2, "STOP")
                        self.after(0, self._conformance_refresh_row_result_labels)
                        abort_all = True
                        break
                    if fname == "conformance_3181.sh" and pre_b and ran_318x_pass:
                        _rc_pre, abort_suite = self._conformance_run_pre_3180_before_318x(
                            client,
                            sftp,
                            pre_b,
                            opts,
                            remote_dir,
                            cfg_remote,
                            spec_map,
                            log_line,
                            anchor_fname=fname,
                        )
                        if abort_suite:
                            abort_all = True
                            break

                    self._conformance_progress[fname] = {"rc": None, "status": "RUN"}
                    self.after(0, self._conformance_refresh_row_result_labels)

                    # SWM tests: upload PKG to remote /tmp/netconf_PKG/
                    if not self._conformance_swm_upload_pkg(sftp, fname, log_line):
                        self._conformance_progress[fname] = {"rc": 1, "status": "FAIL"}
                        self._conformance_record_session_run_result(fname, 1, "FAIL")
                        self._conformance_commit_final_result(fname, 1, "FAIL")
                        self.after(0, self._conformance_refresh_row_result_labels)
                        log_line(f"FAIL — 후속·반복 시험 중단 ({fname}, SWM PKG 업로드)")
                        self._conformance_maybe_reboot_after_test(
                            fname, ordered_fnames, client, sftp, opts, remote_dir, cfg_remote, log_line
                        )
                        abort_all = True
                        break

                    if not self._conformance_prepare_mplane_bundle(fname, log_line):
                        self._conformance_progress[fname] = {"rc": 1, "status": "FAIL"}
                        self._conformance_record_session_run_result(fname, 1, "FAIL")
                        self._conformance_commit_final_result(fname, 1, "FAIL")
                        self.after(0, self._conformance_refresh_row_result_labels)
                        log_line(f"FAIL — 후속·반복 시험 중단 ({fname}, M-Plane bundle)")
                        self._conformance_maybe_reboot_after_test(
                            fname, ordered_fnames, client, sftp, opts, remote_dir, cfg_remote, log_line
                        )
                        abort_all = True
                        break
                    if fname in _CONFORMANCE_MPLANE_SCRIPTS and getattr(self, "is_running", False) and getattr(
                        self, "session_established", False
                    ):
                        log_line("M-Plane: GUI Netconf 세션 사용 (Start 활성 → FIFO edit-config 경로)")
                    if not self._conformance_upload_mplane_assets(sftp, fname, remote_dir, log_line):
                        self._conformance_progress[fname] = {"rc": 1, "status": "FAIL"}
                        self._conformance_record_session_run_result(fname, 1, "FAIL")
                        self._conformance_commit_final_result(fname, 1, "FAIL")
                        self.after(0, self._conformance_refresh_row_result_labels)
                        log_line(f"FAIL — 후속·반복 시험 중단 ({fname}, M-Plane assets)")
                        self._conformance_maybe_reboot_after_test(
                            fname, ordered_fnames, client, sftp, opts, remote_dir, cfg_remote, log_line
                        )
                        abort_all = True
                        break

                    spec_ref = spec_map.get(fname, "")
                    host_log = self._conformance_host_run_log_path(fname)
                    self._conformance_active_host_log = host_log
                    self._conformance_last_host_log = host_log
                    self.after(0, self._refresh_log_target_hint_line)
                    self._conformance_detail_lines[fname] = []
                    self._conformance_detail_capture_key = fname
                    self._conformance_detail_run_started_wall[fname] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._conformance_detail_run_started_mono[fname] = time.monotonic()
                    boost_defer: str | None = None
                    if fname == "conformance_31122.sh" and self._conformance_oru_boost_enabled(fname):
                        boost_defer = "Wait for Trace-log generated"
                    try:
                        rc = self._conformance_exec_remote_script(
                            client,
                            sftp,
                            fname,
                            opts,
                            remote_dir,
                            cfg_remote,
                            spec_ref,
                            host_log,
                            log_line,
                            oru_boost_defer_trigger=boost_defer,
                        )
                    finally:
                        if getattr(self, "_conformance_oru_boost_active", False):
                            self._conformance_stop_oru_show_system_boost(client, remote_dir, log_line)
                        self._conformance_detail_capture_key = None
                        self._conformance_detail_run_ended_wall[fname] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self._conformance_detail_run_ended_mono[fname] = time.monotonic()
                    if rc == -2:
                        self._conformance_progress[fname] = {"rc": -2, "status": "STOP"}
                        self._conformance_record_session_run_result(fname, -2, "STOP")
                        self._conformance_commit_final_result(fname, -2, "STOP")
                        self.after(0, self._conformance_refresh_row_result_labels)
                        abort_all = True
                        break
                    st = "PASS" if rc == 0 else "FAIL"
                    self._conformance_progress[fname] = {"rc": rc, "status": st}
                    self._conformance_record_session_run_result(fname, rc, st)
                    self._conformance_commit_final_result(fname, rc, st)
                    self.after(0, self._conformance_refresh_row_result_labels)

                    if fname == "conformance_3186.sh" and post_b and ran_318x_pass:
                        cleanup_script = post_b
                        self._conformance_run_3180_step(
                            client,
                            sftp,
                            cleanup_script,
                            opts,
                            remote_dir,
                            cfg_remote,
                            spec_map,
                            log_line,
                            "post_3186",
                            force_despite_cancel=True,
                        )
                        post_3180_after_3186 = True

                    if st == "FAIL":
                        log_line(
                            f"FAIL — 후속·반복 시험 중단 ({fname}, exit={rc}, 반복 {iteration}"
                            + ("" if repeat_count == 0 else f"/{repeat_count}")
                            + ")"
                        )
                        self._conformance_maybe_reboot_after_test(
                            fname, ordered_fnames, client, sftp, opts, remote_dir, cfg_remote, log_line
                        )
                        abort_all = True
                        break

                    if self._conformance_maybe_reboot_after_test(
                        fname, ordered_fnames, client, sftp, opts, remote_dir, cfg_remote, log_line
                    ):
                        abort_all = True
                        break

                if abort_all or self._conformance_cancel_event.is_set():
                    break
                if repeat_count == 1:
                    break
                if repeat_count > 1 and iteration >= repeat_count:
                    break
                if repeat_count == 0 or iteration < repeat_count:
                    log_line(
                        f"다음 반복 준비 (완료 {iteration}"
                        + ("" if repeat_count == 0 else f"/{repeat_count}")
                        + ")"
                    )
                # repeat_count == 0 → loop until cancel (or FAIL above sets abort_all)

            if (
                ran_318x_pass
                and post_b
                and not post_3180_after_3186
                and (abort_all or self._conformance_cancel_event.is_set())
            ):
                try:
                    tr = client.get_transport() if client is not None else None
                    ssh_up = tr is not None and tr.is_active()
                except Exception:
                    ssh_up = False
                if not ssh_up:
                    log_line("3.1.8.0 (중지 후 정리) 건너뜀: SSH 세션이 이미 종료됨")
                else:
                    self._conformance_run_3180_step(
                        client,
                        sftp,
                        post_b,
                        opts,
                        remote_dir,
                        cfg_remote,
                        spec_map,
                        log_line,
                        "post_stop",
                        force_despite_cancel=True,
                    )

            try:
                sftp.close()
            except Exception:
                pass
        except Exception as exc:
            log_line(f"RUN ERROR: {exc}")
            self.after(0, lambda e=str(exc): messagebox.showerror("Conformance", str(e)))
        finally:
            if client is not None:
                rd_boost = getattr(self, "_conformance_oru_boost_remote_dir", None) or opts.remote_dir.rstrip("/")
                try:
                    self._conformance_stop_oru_show_system_boost(client, rd_boost, log_line)
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass
            with self._conformance_run_transport_lock:
                self._conformance_run_ssh_client = None
            self.after(0, self._conformance_run_finished)

    def _conformance_detail_buffer_append(self, key: str | None, msg: str) -> None:
        if not key or not msg:
            return
        lk = getattr(self, "_conformance_detail_lock", None)
        if lk is None:
            return
        with lk:
            buf = self._conformance_detail_lines.setdefault(key, [])
            buf.append(msg)
            if len(buf) > 12000:
                del buf[:4000]

    @staticmethod
    def _conformance_detail_strip_run_tag(line: str) -> str:
        s = line.strip()
        if s.startswith("[Conformance-run] "):
            return s[len("[Conformance-run] ") :].strip()
        return s

    def _conformance_detail_first_line_matching(self, lines: list[str], *substrings: str) -> str | None:
        for raw in lines:
            s = self._conformance_detail_strip_run_tag(raw)
            if all(sub in s for sub in substrings):
                return s
        return None

    def _conformance_detail_first_line_regex(self, lines: list[str], pattern: str) -> str | None:
        rx = re.compile(pattern, re.I)
        for raw in lines:
            s = self._conformance_detail_strip_run_tag(raw)
            if rx.search(s):
                return s[:800] + ("…" if len(s) > 800 else "")
        return None

    def _conformance_detail_find_notification_lines(self, lines: list[str], step_desc: str) -> list[str]:
        """Find notification lines relevant to a step description."""
        results: list[str] = []
        keywords: list[str] = []
        desc_low = step_desc.lower()
        if "holdover" in desc_low:
            keywords = ["HOLDOVER", "synchronization-state-change"]
        elif "freerun" in desc_low:
            keywords = ["FREERUN", "synchronization-state-change"]
        elif "locked" in desc_low:
            keywords = ["LOCKED", "synchronization-state-change"]
        elif "alarm" in desc_low and "clear" in desc_low:
            keywords = ["is-cleared>true", "fault-id"]
        elif "alarm" in desc_low:
            keywords = ["is-cleared>false", "fault-id", "alarm-notif"]
        elif "sync" in desc_low:
            keywords = ["synchronization-state-change", "sync-state"]
        elif "supervision" in desc_low:
            keywords = ["supervision-notification"]
        if not keywords:
            return results
        for raw in lines:
            s = self._conformance_detail_strip_run_tag(raw)
            if any(kw in s for kw in keywords):
                if s.startswith("nc DEBUG:") or s.startswith("nc VERBOSE:"):
                    continue
                trimmed = s[:300] + ("…" if len(s) > 300 else "")
                if trimmed not in results:
                    results.append(trimmed)
        return results

    @staticmethod
    def _conformance_detail_extract_ts(line: str) -> datetime | None:
        s = line.strip()
        m_iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", s)
        if m_iso:
            try:
                return datetime.strptime(m_iso.group(1), "%Y-%m-%dT%H:%M:%S")
            except Exception:
                return None
        m_wall = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", s)
        if m_wall:
            try:
                return datetime.strptime(m_wall.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
        return None

    def _conformance_detail_sync_history(self, fname: str, lines: list[str]) -> list[str]:
        hist: list[str] = []
        l2sw_cmd_re = re.compile(r"^\[L2SW\](?:\[[^\]]+\])?\s*>>>\s*(.+)$", re.I)
        cmd_lines: list[str] = []
        cmd_times: list[datetime] = []
        holdover_t, freerun_t, alarm_t = self._conformance_parse_sync_event_times(lines)

        for raw in lines:
            s = self._conformance_detail_strip_run_tag(raw)
            t = self._conformance_detail_extract_ts(s)
            m_cmd = l2sw_cmd_re.match(s)
            if m_cmd:
                cmd_lines.append(s)
                if t is not None:
                    cmd_times.append(t)

        def _fmt_server_time(dt: datetime | None) -> str:
            return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"

        hist.append("[Sync 상태 이력 (서버시간)]")
        if cmd_lines:
            hist.append("  1) L2SW 설정 명령 진행")
            for cl in cmd_lines[:8]:
                hist.append(f"     - {cl}")
            if len(cmd_lines) > 8:
                hist.append(f"     - ... ({len(cmd_lines) - 8}줄 생략)")
            if cmd_times:
                c0 = min(cmd_times)
                c1h = c0 + timedelta(hours=1)
                hist.append(
                    f"     - 첫 명령 서버시간: {_fmt_server_time(c0)}  "
                    f"(+1h 종료: {_fmt_server_time(c1h)})"
                )
        else:
            hist.append("  1) L2SW 설정 명령 진행: 미진행")

        if holdover_t is not None:
            hist.append(f"  2) HOLDOVER 변경 서버시간: {_fmt_server_time(holdover_t)}")
        else:
            hist.append("  2) HOLDOVER 변경: 미진행")
        if freerun_t is not None:
            hist.append(f"  3) FREERUN 변경 서버시간: {_fmt_server_time(freerun_t)}")
        else:
            hist.append("  3) FREERUN 변경: 미진행")
        if alarm_t is not None:
            hist.append(f"  4) Alarm noti 수신 서버시간: {_fmt_server_time(alarm_t)}")
        else:
            hist.append("  4) Alarm noti 수신: 미진행")
        # 3.1.5.1 요청: HOLDOVER 이후 FREERUN/Alarm 까지의 천이 시간(경과) 표시
        def _fmt_elapsed(from_t: datetime | None, to_t: datetime | None) -> str:
            if from_t is None or to_t is None:
                return "—"
            sec = int((to_t - from_t).total_seconds())
            sign = "-" if sec < 0 else ""
            sec = abs(sec)
            hh = sec // 3600
            mm = (sec % 3600) // 60
            ss = sec % 60
            return f"{sign}{hh:02d}:{mm:02d}:{ss:02d} ({'-' if sign else ''}{sec}s)"

        if fname in ("conformance_3151.sh", "conformance_3152.sh"):
            hist.append(
                "  5) HOLDOVER → FREERUN 천이 시간: "
                + _fmt_elapsed(holdover_t, freerun_t)
            )
            hist.append(
                "  6) HOLDOVER → Alarm 발생 시간: "
                + _fmt_elapsed(holdover_t, alarm_t)
            )
        hist.append("")
        return hist

    def _conformance_format_detail_report(self, fname: str, opts: ConformanceRunOptions) -> str:
        lk = getattr(self, "_conformance_detail_lock", None)
        lines: list[str] = []
        if lk is not None:
            with lk:
                lines = list(self._conformance_detail_lines.get(fname, ()))
        blob = "\n".join(lines)
        ref = self._conformance_spec_ref_map().get(fname, "—")
        sw = self._conformance_detail_run_started_wall.get(fname, "—")
        ew = self._conformance_detail_run_ended_wall.get(fname, "—")
        t0 = self._conformance_detail_run_started_mono.get(fname)
        t1 = self._conformance_detail_run_ended_mono.get(fname)
        elapsed = ""
        if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
            elapsed = f"{(t1 - t0):.1f} 초"
        elif isinstance(t0, (int, float)):
            elapsed = f"{(time.monotonic() - t0):.1f} 초 (진행 중)"
        pr = self._conformance_progress.get(fname) or {}
        rc = pr.get("rc")
        st = str(pr.get("status") or "—")
        out: list[str] = []
        out.append("═" * 56)
        out.append(f" 시험 스크립트: {fname}")
        out.append(f" 표 참조: {ref}")
        out.append("═" * 56)
        out.append("")
        spec_desc = self._conformance_spec_description_ko(ref)
        if spec_desc:
            out.append("[시험 설명]")
            out.append(f"  {spec_desc}")
            out.append("")
        out.append("[진행 시각]")
        out.append(f"  시작(로컬 시각): {sw}")
        out.append(f"  종료(로컬 시각): {ew}")
        out.append(f"  경과: {elapsed or '—'}")
        out.append("")
        out.append("[GUI에 적용된 타임아웃·간격 (Conformance 탭 / Settings)]")
        out.append(f"  NETCONF_RPC_TIMEOUT: {opts.netconf_rpc_timeout.strip() or '30'} s")
        out.append(f"  NETCONF_IDLE_TIMEOUT: {opts.netconf_idle_timeout.strip() or '120'} s")
        out.append(f"  SUPERVISION_INTERVAL: {opts.supervision_interval.strip() or '60'} s")
        out.append(f"  SUPERVISION_RESET_CYCLES (전역 fallback): {(opts.supervision_reset_cycles or '').strip() or '30'}")
        if fname in ("conformance_3131.sh", "conformance_3132.sh"):
            try:
                _sn = self._conformance_resolve_supervision_needed(fname, opts)
            except Exception:
                _sn = "—"
            out.append(f"  SUPERVISION_NEEDED (실제 반복): {_sn}")
        out.append(
            "  SUPERVISION_NEGATIVE_FAIL_ON_CYCLE: "
            f"{(opts.supervision_negative_fail_on_cycle or '').strip() or '3'} (미사용·무시)"
        )
        out.append(f"  CONN_DELAY: {opts.conn_delay.strip() or '3'} s")
        out.append(f"  post_listen_wait: {(opts.post_listen_wait_sec or '').strip() or '0'} s")
        out.append("")
        if fname in ("conformance_3151.sh", "conformance_3152.sh"):
            out.extend(self._conformance_detail_sync_history(fname, lines))
        out.append("[STEP·PASS/FAIL 원인 분석]")
        step_blocks: list[str] = []

        def _q(label: str, text: str | None) -> str:
            if text:
                return f"    {label}\n      {text}"
            return f"    {label}\n      (캡처 버퍼에 해당 줄 없음)"

        m1 = re.search(r"STEP\s*1\.\s*CallHome\s*:\s*(\w+)", blob, re.I)
        if m1:
            v = m1.group(1).upper()
            ln_s1 = self._conformance_detail_first_line_regex(lines, r"STEP\s*1\.\s*CallHome\s*:")
            ln_accept = self._conformance_detail_first_line_matching(lines, "Accepted a connection on")
            if v == "OK":
                parts = ["  【STEP 1 Call Home】 PASS", _q("스크립트/요약 줄", ln_s1)]
                if ln_accept:
                    parts.append(_q("netopeer 로그(일부)", ln_accept))
                step_blocks.append("\n".join(parts))
            else:
                parts = ["  【STEP 1 Call Home】 FAIL", _q("스크립트/요약 줄", ln_s1)]
                if ln_accept:
                    parts.append(_q("netopeer 로그(일부)", ln_accept))
                step_blocks.append("\n".join(parts))

        is_3112_neg = fname == "conformance_3112.sh"
        if is_3112_neg and "STEP 2." in blob:
            neg_ok = re.search(r"\[OK\]\s*STEP\s*2\.", blob, re.I) and re.search(
                r"Failed to login|incorrect\s+username", blob, re.I
            )
            neg_nok = re.search(r"\[NOK\]\s*STEP\s*2", blob, re.I)
            ln_step2 = self._conformance_detail_first_line_regex(lines, r"\[(?:OK|NOK)\]\s*STEP\s*2\.")
            ln_nok2_3112 = self._conformance_detail_first_line_regex(lines, r"\[NOK\]\s*STEP\s*2\.")
            ln_auth = self._conformance_detail_first_line_matching(lines, "Authentication successful")
            ln_fail = self._conformance_detail_first_line_matching(lines, "[FAIL]")
            if neg_ok:
                parts = [
                    "  【STEP 2 부정(3.1.1.2)】 PASS",
                    _q("스크립트/요약 줄", ln_step2),
                ]
                if ln_auth:
                    parts.append(_q("로그에 함께 잡힌 줄(참고)", ln_auth))
                step_blocks.append("\n".join(parts))
            elif neg_nok:
                step_blocks.append(
                    "  【STEP 2 부정(3.1.1.2)】 FAIL\n" + _q("스크립트/요약 줄", ln_step2 or ln_nok2_3112)
                )
            elif ln_fail:
                step_blocks.append("  【STEP 2 부정(3.1.1.2)】 FAIL\n" + _q("[FAIL] 줄", ln_fail))
            elif ln_auth:
                step_blocks.append("  【STEP 2 부정(3.1.1.2)】 FAIL\n" + _q("캡처 줄", ln_auth))
            else:
                step_blocks.append(
                    "  【STEP 2 부정(3.1.1.2)】 (판단용 패턴 줄 없음)\n"
                    + _q("STEP 2 근처(있으면)", ln_step2)
                )
        elif "STEP 2." in blob:
            ln_ok2 = self._conformance_detail_first_line_regex(lines, r"\[OK\]\s*STEP\s*2\.")
            ln_nok2 = self._conformance_detail_first_line_regex(lines, r"\[NOK\]\s*STEP\s*2\.")
            ln_auth = self._conformance_detail_first_line_matching(lines, "Authentication successful")
            if ln_nok2:
                parts = ["  【STEP 2 로그인】 FAIL", _q("스크립트/요약 줄", ln_nok2)]
                if ln_auth:
                    parts.append(_q("인증 성공 줄(있으면)", ln_auth))
                step_blocks.append("\n".join(parts))
            elif ln_ok2 or ln_auth:
                parts = ["  【STEP 2 로그인】 PASS"]
                if ln_ok2:
                    parts.append(_q("스크립트/요약 줄", ln_ok2))
                if ln_auth:
                    parts.append(_q("인증 성공 줄", ln_auth))
                if len(parts) == 1:
                    parts.append(_q("STEP 2 관련 줄", self._conformance_detail_first_line_regex(lines, r"STEP\s*2\.")))
                step_blocks.append("\n".join(parts))

        seen_steps: set[int] = {1, 2}
        for ln in lines:
            sl = self._conformance_detail_strip_run_tag(ln)
            m_step = re.search(r"\[(OK|NOK)\]\s*STEP\s*(\d+)\.", sl, re.I)
            if not m_step:
                m_step2 = re.search(
                    r"STEP\s*(\d+)\.\s*(?:Subscription|Supervision|CallHome)\s*:\s*(\S+)", sl, re.I
                )
                if m_step2:
                    sn = int(m_step2.group(1))
                    if sn in seen_steps:
                        continue
                    seen_steps.add(sn)
                    vv = m_step2.group(2).upper()
                    verdict = "PASS" if vv == "OK" else "FAIL"
                    ln_c = self._conformance_detail_first_line_regex(lines, rf"STEP\s*{sn}\.\s*Criteria")
                    parts = [f"  【STEP {sn}】 {verdict}"]
                    if ln_c:
                        parts.append(f"    Criteria 줄\n      {ln_c}")
                    parts.append(f"    결과 줄\n      {sl}")
                    noti_desc = sl
                    noti_lines = self._conformance_detail_find_notification_lines(lines, noti_desc)
                    if noti_lines:
                        parts.append(f"    notification 정보")
                        for nl in noti_lines[:3]:
                            parts.append(f"      {nl}")
                    step_blocks.append("\n".join(parts))
                continue
            sn = int(m_step.group(2))
            if sn in seen_steps:
                continue
            seen_steps.add(sn)
            vv = m_step.group(1).upper()
            verdict = "PASS" if vv == "OK" else "FAIL"
            desc = re.sub(r"^\[(?:OK|NOK)\]\s*STEP\s*\d+\.\s*", "", sl).strip()
            parts = [f"  【STEP {sn}】 {verdict}"]
            parts.append(f"    스크립트 줄\n      {sl}")
            noti_lines = self._conformance_detail_find_notification_lines(lines, desc)
            if noti_lines:
                parts.append(f"    notification 정보")
                for nl in noti_lines[:3]:
                    parts.append(f"      {nl}")
            step_blocks.append("\n".join(parts))

        if fname == "conformance_3162.sh":
            m_swm_bad = re.search(
                r"<install-event\b[^>]*>[\s\S]*?<status>\s*(?:COMPLETED|VALID)\s*</status>",
                blob,
                re.I,
            )
            if m_swm_bad:
                step_blocks.append(
                    "  【3.1.6.2 install-event】 FAIL\n"
                    "      부정 PKG인데 install이 성공(COMPLETED/VALID)한 것으로 보입니다."
                )

        if "[FAIL]" in blob:
            seen_fail: set[str] = set()
            for ln in lines:
                sl = self._conformance_detail_strip_run_tag(ln)
                if "[FAIL]" in sl and sl not in seen_fail:
                    seen_fail.add(sl)
                    step_blocks.append(f"  【스크립트 FAIL 표시】\n      {sl}")
        if "RUN ERROR:" in blob:
            for ln in lines:
                sl = self._conformance_detail_strip_run_tag(ln)
                if "RUN ERROR:" in sl:
                    step_blocks.append(f"  【실행 오류】\n      {sl}")
        if not step_blocks:
            step_blocks.append("  (캡처에서 STEP 요약 줄을 찾지 못했습니다. 아래 원본 캡처를 참고하세요.)")
        out.extend(step_blocks)
        out.append("")
        mlog = re.search(r"remote host log file:\s*(\S+)", blob)
        if mlog:
            out.append("[원격 tee 세션 로그]")
            out.append(f"  {mlog.group(1)}")
            out.append("")
        mend = re.search(r"----\s*END\s+\S+\s+exit=(-?\d+)", blob)
        out.append("[GUI가 수신한 종료 코드]")
        if mend:
            erc = int(mend.group(1))
            out.append(f"  exit={erc}  →  {'PASS(0)' if erc == 0 else 'FAIL' if erc != -2 else 'STOP'}")
        else:
            out.append(f"  progress.status={st}  rc={rc!s}")
        out.append("")
        out.append("[원본 캡처 (마지막 120줄)]")
        out.append("-" * 56)
        tail = lines[-120:] if len(lines) > 120 else lines
        out.extend(tail if tail else ["  (캡처 없음 — 아직 이 항목을 실행하지 않았거나 버퍼가 비어 있습니다.)"])
        out.append("")
        return "\n".join(out)

    def _conformance_refresh_one_item_detail(self, fname: str) -> None:
        win = self._conformance_item_detail_wins.get(fname)
        tw = self._conformance_item_detail_texts.get(fname)
        if win is None or tw is None:
            return
        try:
            if not win.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            opts = self._conformance_default_run_options()
            body = self._conformance_format_detail_report(fname, opts)
            if self._conformance_item_detail_last_body.get(fname) == body:
                return
            self._conformance_item_detail_last_body[fname] = body
            tw.configure(state="normal")
            tw.delete("1.0", "end")
            tw.insert("1.0", body)
            tw.configure(state="disabled")
        except tk.TclError:
            pass

    def _conformance_schedule_item_detail_refresh(self, fname: str) -> None:
        old = self._conformance_item_detail_refresh_jobs.get(fname)
        if old:
            try:
                self.after_cancel(old)
            except Exception:
                pass
            self._conformance_item_detail_refresh_jobs[fname] = None

        def tick() -> None:
            self._conformance_refresh_one_item_detail(fname)
            w = self._conformance_item_detail_wins.get(fname)
            if w is None:
                self._conformance_item_detail_refresh_jobs[fname] = None
                return
            try:
                if not w.winfo_exists():
                    self._conformance_item_detail_refresh_jobs[fname] = None
                    return
            except tk.TclError:
                self._conformance_item_detail_refresh_jobs[fname] = None
                return
            self._conformance_item_detail_refresh_jobs[fname] = self.after(2000, tick)

        self._conformance_refresh_one_item_detail(fname)
        self._conformance_item_detail_refresh_jobs[fname] = self.after(2000, tick)

    def _conformance_close_item_detail(self, fname: str) -> None:
        jid = self._conformance_item_detail_refresh_jobs.pop(fname, None)
        if jid:
            try:
                self.after_cancel(jid)
            except Exception:
                pass
        self._conformance_item_detail_last_body.pop(fname, None)
        w = self._conformance_item_detail_wins.pop(fname, None)
        self._conformance_item_detail_texts.pop(fname, None)
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def _conformance_on_tree_double_click_detail(self, evt: tk.Event) -> None:
        tree = getattr(self, "conformance_list_tree", None)
        if tree is None:
            return
        row = tree.identify_row(evt.y)
        if not row:
            return
        self._conformance_open_item_detail_gui(row)

    def _conformance_open_item_detail_gui(self, fname: str) -> None:
        wins = self._conformance_item_detail_wins
        texts = self._conformance_item_detail_texts
        w = wins.get(fname)
        if w is not None:
            try:
                if w.winfo_exists():
                    w.deiconify()
                    w.lift()
                    self._conformance_schedule_item_detail_refresh(fname)
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self)
        win.title(f"Conformance 상세 — {fname}")
        win.geometry("920x640")
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        ttk.Label(
            top,
            text="표 행을 더블클릭하면 이 창이 열립니다. 내용은 약 2초마다 갱신됩니다.",
            foreground="#64748b",
        ).pack(anchor="w")
        body_fr = ttk.LabelFrame(win, text="진행·STEP·원인 (분석)", padding=6)
        body_fr.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        txt = tk.Text(body_fr, wrap="word", height=28)
        self._apply_code_text_theme(txt)
        ys = ttk.Scrollbar(body_fr, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        txt.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        body_fr.rowconfigure(0, weight=1)
        body_fr.columnconfigure(0, weight=1)
        txt.configure(state="disabled")
        bf = ttk.Frame(win, padding=(8, 0, 8, 8))
        bf.pack(fill="x")
        ttk.Button(bf, text="닫기", command=lambda fn=fname: self._conformance_close_item_detail(fn)).pack(side="right")
        ttk.Button(bf, text="지금 새로고침", command=lambda fn=fname: self._conformance_refresh_one_item_detail(fn)).pack(
            side="right", padx=(0, 8)
        )
        wins[fname] = win
        texts[fname] = txt

        def _on_del() -> None:
            self._conformance_close_item_detail(fname)

        win.protocol("WM_DELETE_WINDOW", _on_del)
        self._conformance_schedule_item_detail_refresh(fname)

    def _conformance_open_local_folder(self) -> None:
        d = self._conformance_local_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(d))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as exc:
            messagebox.showerror("Conformance", str(exc))

    def _conformance_sync_tree_pick(self, fname: str, *_args: Any) -> None:
        tree = getattr(self, "conformance_list_tree", None)
        bv = self.conformance_check_vars.get(fname)
        if tree is None or bv is None:
            return
        try:
            if tree.exists(fname):
                tree.set(fname, "pick", "☑" if bv.get() else "☐")
        except tk.TclError:
            pass

    def _conformance_sync_tree_reboot(self, fname: str, *_args: Any) -> None:
        tree = getattr(self, "conformance_list_tree", None)
        rbv = getattr(self, "conformance_reboot_vars", {}).get(fname)
        if tree is None or rbv is None:
            return
        try:
            if tree.exists(fname):
                tree.set(fname, "reboot", "☑" if rbv.get() else "☐")
        except tk.TclError:
            pass

    def _conformance_select_all_checked(self) -> None:
        for bv in self.conformance_check_vars.values():
            bv.set(True)

    def _conformance_clear_all_checked(self) -> None:
        for bv in self.conformance_check_vars.values():
            bv.set(False)

    # ── per-test settings ──────────────────────────────────────────

    def _conformance_reconcile_per_test_settings(self) -> None:
        """Merge per-script / shared keys so dialogs and runs see the same values."""
        for fname, schema in _CONFORMANCE_PER_TEST_SCHEMA.items():
            sk = str(schema.get("settings_key") or fname)
            keys: list[str] = [sk, fname]
            shared = schema.get("shared_with")
            if shared:
                keys.append(str(shared))
            merged: dict[str, str] = {}
            for k in keys:
                cur = self._conformance_per_test_settings.get(k)
                if isinstance(cur, dict):
                    merged.update({str(kk): str(vv) for kk, vv in cur.items()})
            if not merged:
                continue
            for k in keys:
                self._conformance_per_test_settings[k] = dict(merged)

    def _conformance_field_value_from_entry(
        self, field: dict[str, Any], sv: tk.StringVar
    ) -> str:
        v = sv.get().strip()
        choices = field.get("choices")
        if choices:
            labels = [c[1] for c in choices]
            values = [c[0] for c in choices]
            if v in labels:
                return values[labels.index(v)]
            if v in values:
                return v
            if values:
                return values[0]
        if field.get("file_picker") and v:
            try:
                return str(Path(v).expanduser().resolve())
            except OSError:
                return v
        return v

    def _conformance_write_per_test_settings(
        self, fname: str, schema: dict[str, Any], vals: dict[str, str]
    ) -> None:
        store_key = str(schema.get("settings_key") or fname)
        clean = {str(k): str(v) for k, v in vals.items()}
        self._conformance_per_test_settings[store_key] = dict(clean)
        self._conformance_per_test_settings[fname] = dict(clean)
        shared = schema.get("shared_with")
        if shared:
            self._conformance_per_test_settings[str(shared)] = dict(clean)

    def _conformance_persist_per_test_settings_to_disk(self) -> None:
        try:
            self._save_current_config()
        except Exception as exc:
            try:
                self.append_log(f"[GUI] Conformance 항목 설정 저장 실패: {exc}\n")
            except Exception:
                pass

    def _conformance_get_per_test_val(self, fname: str, key: str) -> str:
        schema = _CONFORMANCE_PER_TEST_SCHEMA.get(fname)
        if not schema:
            return ""
        keys_to_try: list[str] = []
        sk = schema.get("settings_key")
        if sk:
            keys_to_try.append(str(sk))
        keys_to_try.append(fname)
        shared = schema.get("shared_with")
        if shared:
            keys_to_try.append(str(shared))
        for store_key in keys_to_try:
            stored = self._conformance_per_test_settings.get(store_key, {}).get(key)
            if stored is not None:
                return stored
        for f in schema["fields"]:
            if f["key"] == key:
                return f["default"]
        return ""

    def _conformance_resolve_supervision_needed(
        self, fname: str, opts: ConformanceRunOptions | None = None
    ) -> str:
        """3131/3132 실제 반복 횟수. 시험⚙ → 전역 RESET_CYCLES → 기본 30.

        SUPERVISION_NEGATIVE_FAIL_ON_CYCLE(기본 3)은 과거 잔재로 횟수에 쓰지 않음.
        """
        per = (self._conformance_get_per_test_val(fname, "supervision_cycles") or "").strip()
        if re.fullmatch(r"[0-9]+", per) and int(per) > 0:
            # 예전 기본값 3 이 남아 있고 전역이 30 이면 전역을 따름 (의도: 지정 횟수=전역 30)
            if per == "3":
                g = ""
                if opts is not None:
                    g = (opts.supervision_reset_cycles or "").strip()
                if not g:
                    try:
                        g = (self.conformance_run_supervision_reset_cycles_var.get() or "").strip()
                    except Exception:
                        g = ""
                if re.fullmatch(r"[0-9]+", g) and int(g) > 3:
                    return g
            return per
        g = ""
        if opts is not None:
            g = (opts.supervision_reset_cycles or "").strip()
        if not g:
            try:
                g = (self.conformance_run_supervision_reset_cycles_var.get() or "").strip()
            except Exception:
                g = ""
        if re.fullmatch(r"[0-9]+", g) and int(g) > 0:
            return g
        return "30"

    def _conformance_per_test_env_exports(self, fname: str) -> str:
        pre = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")
        if pre and fname == pre:
            fname = "conformance_3181.sh"
        schema = _CONFORMANCE_PER_TEST_SCHEMA.get(fname)
        if not schema:
            return ""
        store_key = self._conformance_settings_store_key(fname)
        settings = dict(self._conformance_per_test_settings.get(store_key, {}))
        if not settings:
            settings = dict(self._conformance_per_test_settings.get(fname, {}))
        parts: list[str] = []
        for field in schema["fields"]:
            env_var = field.get("env_var")
            if not env_var:
                continue
            val = (settings.get(field["key"]) or "").strip() or str(field.get("default") or "")
            parts.append(f"export {env_var}={shlex.quote(val)}")
        if self._conformance_is_318x_script(fname):
            mode = (settings.get("conf_v11_mode") or "after").strip()
            v11 = "1" if mode == "after" else "0"
            parts.append(f"export CONFORMANCE_V11_ORANUSER_AT_DOMAIN={shlex.quote(v11)}")
        # For SWM tests: build SW_PKG_REMOTE_PATH from per-test settings
        if fname in ("conformance_3161.sh", "conformance_3162.sh"):
            swm_env = self._conformance_swm_env_export(fname)
            if swm_env:
                parts.append(swm_env)
        if fname == "conformance_31122.sh" and self._conformance_oru_boost_enabled(fname):
            parts.append("export ORU_LOG_BOOST=1")
        return " ; ".join(parts) + (" ; " if parts else "")

    def _conformance_swm_env_export(self, fname: str) -> str:
        """Build SW_PKG_REMOTE_PATH env var for 3.1.6.x tests."""
        pkg_path = self._conformance_get_per_test_val(fname, "swm_pkg_path").strip()
        server_ip = self._conformance_get_per_test_val(fname, "swm_server_ip").strip()
        server_id = self._conformance_get_per_test_val(fname, "swm_server_id").strip() or "root"
        if not pkg_path or not server_ip:
            return ""
        pkg_filename = os.path.basename(pkg_path)
        remote_pkg_path = f"/tmp/netconf_PKG/{pkg_filename}"
        url = f"sftp://{server_id}@{server_ip}{remote_pkg_path}"
        return f"export SW_PKG_REMOTE_PATH={shlex.quote(url)}"

    def _conformance_swm_remote_pkg_stat(self, sftp: Any, remote_path: str) -> int | None:
        """Return remote regular-file size, or None if missing/not a file."""
        import stat as stat_mod

        try:
            attrs = sftp.stat(remote_path)
        except OSError:
            return None
        if not stat_mod.S_ISREG(attrs.st_mode):
            return None
        return int(attrs.st_size)

    def _conformance_swm_log_remote_pkg_dir(self, sftp: Any, remote_dir: str, log_line: Any) -> None:
        try:
            names = sorted(sftp.listdir(remote_dir))
        except OSError as exc:
            log_line(f"[WARN] PKG 서버 목록 조회 실패 ({remote_dir}): {exc}")
            return
        if not names:
            log_line(f"PKG 서버 목록 ({remote_dir}): (empty)")
            return
        log_line(f"PKG 서버 목록 ({remote_dir}): {', '.join(names)}")

    def _conformance_swm_upload_pkg(
        self, sftp: Any, fname: str, log_line: Any
    ) -> bool:
        """Upload local PKG to remote /tmp/netconf_PKG/ for 3.1.6.x tests."""
        if fname not in ("conformance_3161.sh", "conformance_3162.sh"):
            return True
        pkg_path = self._conformance_get_per_test_val(fname, "swm_pkg_path").strip()
        if not pkg_path:
            log_line(f"[WARN] {fname} PKG 경로가 설정되지 않았습니다. 해당 항목 설정(⚙)에서 지정하세요.")
            return True
        if not os.path.isfile(pkg_path):
            log_line(f"[ERROR] 로컬 PKG 파일 없음: {pkg_path}")
            return False
        pkg_filename = os.path.basename(pkg_path)
        remote_dir = "/tmp/netconf_PKG"
        remote_path = f"{remote_dir}/{pkg_filename}"
        try:
            try:
                sftp.stat(remote_dir)
            except OSError:
                sftp.mkdir(remote_dir)
            self._conformance_swm_log_remote_pkg_dir(sftp, remote_dir, log_line)
            local_size = os.path.getsize(pkg_path)
            remote_size = self._conformance_swm_remote_pkg_stat(sftp, remote_path)
            if remote_size is not None and remote_size == local_size:
                log_line(
                    f"PKG 업로드 생략: 서버에 동일 파일 있음 "
                    f"({remote_path}, {remote_size:,} bytes)"
                )
                return True
            if remote_size is not None:
                log_line(
                    f"PKG 재업로드: 서버 파일 크기 불일치 "
                    f"(local={local_size:,}, remote={remote_size:,})"
                )
            else:
                log_line(f"PKG 업로드 시작: {pkg_filename} → {remote_path}")
            sftp.put(pkg_path, remote_path)
            try:
                sftp.chmod(remote_path, 0o644)
            except OSError:
                pass
            log_line(f"PKG 업로드 완료: {remote_path} ({local_size:,} bytes)")
            return True
        except Exception as exc:
            log_line(f"[ERROR] PKG 업로드 실패: {exc}")
            return False

    def _conformance_open_per_test_settings(self, fname: str) -> None:
        schema = _CONFORMANCE_PER_TEST_SCHEMA.get(fname)
        if not schema:
            return
        win = tk.Toplevel(self)  # type: ignore[arg-type]
        win.title(f"설정 — {schema['title']}")
        win.resizable(False, False)
        win.transient(self)  # type: ignore[arg-type]
        win.grab_set()

        fr = ttk.Frame(win, padding=12)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text=schema["title"], font=("", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        entries: dict[str, tk.StringVar] = {}
        store_key = str(schema.get("settings_key") or fname)
        cur = self._conformance_per_test_settings.get(store_key, {})
        if not cur:
            cur = self._conformance_per_test_settings.get(fname, {})
        for i, field in enumerate(schema["fields"], start=1):
            ttk.Label(fr, text=field["label"]).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            sv = tk.StringVar(value=cur.get(field["key"]) or field["default"])
            w = 40 if field.get("wide") else 12
            choices = field.get("choices")
            if choices:
                labels = [c[1] for c in choices]
                values = [c[0] for c in choices]
                cur_val = cur.get(field["key"]) or field["default"]
                if cur_val not in values and values:
                    cur_val = values[0]
                sv.set(cur_val)
                cb = ttk.Combobox(
                    fr,
                    values=labels,
                    width=max(28, w),
                    state="readonly",
                )
                cb.grid(row=i, column=1, sticky="we", pady=2)
                try:
                    cb.current(values.index(cur_val))
                except ValueError:
                    cb.current(0)

                def _on_choice(_evt: Any = None, cbox=cb, lbls=labels, vals=values, s=sv) -> None:
                    idx = cbox.current()
                    if 0 <= idx < len(vals):
                        s.set(vals[idx])

                cb.bind("<<ComboboxSelected>>", _on_choice)
            elif field.get("file_picker"):
                entry_fr = ttk.Frame(fr)
                entry_fr.grid(row=i, column=1, sticky="we", pady=2)
                ent = ttk.Entry(entry_fr, textvariable=sv, width=w - 6)
                ent.pack(side="left", fill="x", expand=True)
                ftypes = field.get("file_types", [("All files", "*.*")])

                def _browse(
                    s=sv,
                    ft=ftypes,
                    fkey=field["key"],
                    ftitle=field.get("file_picker_title"),
                ) -> None:
                    from tkinter import filedialog

                    initial = s.get().strip()
                    init_dir = ""
                    init_file = ""
                    if initial:
                        try:
                            pp = Path(initial).expanduser()
                            if pp.is_file():
                                init_dir = str(pp.parent)
                                init_file = pp.name
                            else:
                                init_dir = str(pp)
                        except OSError:
                            init_dir = ""
                    kwargs: dict[str, Any] = {
                        "filetypes": ft,
                        "initialdir": init_dir or None,
                    }
                    if ftitle:
                        kwargs["title"] = str(ftitle)
                    if init_file:
                        kwargs["initialfile"] = init_file
                    p = filedialog.askopenfilename(**kwargs)
                    if p:
                        try:
                            p = str(Path(p).expanduser().resolve())
                        except OSError:
                            pass
                        s.set(p)

                ttk.Button(entry_fr, text="선택…", command=_browse, width=6).pack(side="left", padx=(4, 0))
            else:
                show_pw = "*" if field.get("password") else ""
                ent = ttk.Entry(fr, textvariable=sv, width=w, show=show_pw)
                ent.grid(row=i, column=1, sticky="we", pady=2)
            entries[field["key"]] = sv
            if field.get("hint"):
                ttk.Label(fr, text=field["hint"], foreground="#64748b", font=("", 8)).grid(
                    row=i, column=2, sticky="w", padx=(6, 0), pady=2
                )

        def _apply() -> None:
            vals = dict(cur)
            for field in schema["fields"]:
                key = field["key"]
                sv = entries.get(key)
                if sv is None:
                    continue
                v = self._conformance_field_value_from_entry(field, sv)
                if v:
                    vals[key] = v
                else:
                    vals.pop(key, None)
            self._conformance_write_per_test_settings(fname, schema, vals)
            self._conformance_persist_per_test_settings_to_disk()
            try:
                self.append_log(
                    f"[GUI] Conformance 항목 설정 저장: {fname} ({len(vals)}개 필드)\n"
                )
            except Exception:
                pass
            win.destroy()

        def _reset() -> None:
            for field in schema["fields"]:
                sv = entries.get(field["key"])
                if sv:
                    sv.set(field["default"])

        btn_fr = ttk.Frame(fr)
        btn_fr.grid(row=len(schema["fields"]) + 1, column=0, columnspan=3, pady=(10, 0))
        ttk.Button(btn_fr, text="초기화", command=_reset, width=8).pack(side="left", padx=(0, 8))
        ttk.Button(btn_fr, text="적용", command=_apply, width=8).pack(side="left", padx=(0, 8))
        ttk.Button(btn_fr, text="취소", command=win.destroy, width=8).pack(side="left")

        win.update_idletasks()
        pw, ph = win.winfo_width(), win.winfo_height()
        sx = self.winfo_rootx() + (self.winfo_width() - pw) // 2  # type: ignore[attr-defined]
        sy = self.winfo_rooty() + (self.winfo_height() - ph) // 3  # type: ignore[attr-defined]
        win.geometry(f"+{max(0, sx)}+{max(0, sy)}")

    def _build_conformance_tab(self, parent: ttk.Frame) -> None:
        self.conformance_check_vars.clear()
        if not hasattr(self, "conformance_reboot_vars") or self.conformance_reboot_vars is None:
            self.conformance_reboot_vars = {}
        self.conformance_reboot_vars.clear()
        self._conformance_run_labels.clear()
        self._conformance_318x_link_busy = False
        self.conformance_path_hint_var = tk.StringVar(value="")
        intro = ttk.LabelFrame(parent, text="Conformance — 원격 Linux (/var/tmp)", padding=8)
        intro.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(
            intro,
            text=(
                "Settings의 SSH로 Linux에 접속해 로컬 ./conformance/*.sh 를 /var/tmp/conformance/ 에 올리고, "
                "Settings 탭의 ORU 값만 반영한 JSON을 --config 로 넘겨 실행합니다. "
                "스크립트·장비 측 작업 파일은 /var/tmp/netconf_tmp/ 만 사용하세요. "
                "목록은 O-RAN M-Plane 3.1 시험 표 순서이며, 로컬에 있는 스크립트만 표시됩니다. "
                "3.1.8.x(3.1.8.1–3.1.8.6)는 하나만 선택해도 전체가 연동 선택·일괄 실행됩니다(표 순서 3181→3186). "
                "실행 순서는 표에서 위→아래 순서이며, 일부만 체크해도 체크된 항목만 그 순서대로 진행합니다. "
                "「재부팅」을 체크하면 해당 시험 완료 후 ORU reset을 보내고 재부팅 대기(초)만큼 기다린 뒤, "
                "다음 선택 항목이 있으면 이어서 진행하고 없으면 이번 반복을 종료합니다. "
                "반복 횟수가 2 이상(또는 0=무한)이면 선택·재부팅 흐름을 횟수만큼 반복합니다. "
                "3.1.8.0 은 3.1.8.1 직전·3.1.8.6 종료 후에 실행되며, 중지해도 정리용으로 한 번 더 시도합니다. "
                "실행 출력(stdout/stderr)은 메인 화면 하단 로그 창에 표시됩니다. "
                "표에서 행을 더블클릭하면 해당 항목의 STEP·원인·타임아웃 요약 상세 창이 열립니다(약 2초마다 갱신). "
                "「전체 결과」에서 항목별 최종 PASS/FAIL/STOP을 한 번에 볼 수 있습니다."
            ),
            foreground="#475569",
            justify="left",
            wraplength=1020,
        ).pack(anchor="w")
        ttk.Label(intro, textvariable=self.conformance_last_run_hint_var, foreground="#0f766e", justify="left").pack(
            anchor="w", pady=(4, 0)
        )
        ttk.Label(intro, textvariable=self.conformance_path_hint_var, foreground="#0369a1", justify="left").pack(
            anchor="w", pady=(2, 0)
        )

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="일괄 선택", command=self._conformance_select_all_checked).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="일괄 해지", command=self._conformance_clear_all_checked).pack(side="left", padx=(0, 8))
        ttk.Button(
            bar,
            text="선택 항목 원격 실행",
            command=self._conformance_run_checked,
            style="Big.TButton",
        ).pack(side="left", padx=(0, 8))
        self.conformance_stop_btn = ttk.Button(bar, text="시험 중지", command=self._conformance_stop_run, state="disabled")
        self.conformance_stop_btn.pack(side="left", padx=(0, 8))
        ttk.Label(bar, text="반복").pack(side="left", padx=(0, 2))
        ttk.Entry(bar, textvariable=self.conformance_run_repeat_var, width=5).pack(side="left")
        ttk.Label(bar, text="(0=무한)", foreground="#64748b").pack(side="left", padx=(2, 8))
        ttk.Label(bar, text="재부팅 대기(초)").pack(side="left", padx=(0, 2))
        if not hasattr(self, "conformance_reboot_wait_var"):
            self.conformance_reboot_wait_var = tk.StringVar(value="360")
        ttk.Entry(bar, textvariable=self.conformance_reboot_wait_var, width=6).pack(side="left")
        ttk.Label(bar, text="(reset 후)", foreground="#64748b").pack(side="left", padx=(2, 8))
        self.conformance_sync_btn = ttk.Button(
            bar,
            text="스크립트 동기화(업로드)",
            command=lambda: self._conformance_sync_to_remote(force=True),
        )
        self.conformance_sync_btn.pack(side="left", padx=(0, 8))
        ttk.Checkbutton(bar, text="진단 로그", variable=self.conformance_debug_var, command=self._save_current_config).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(bar, text="로컬 폴더 열기", command=self._conformance_open_local_folder).pack(side="left", padx=(8, 0))
        ttk.Button(
            bar,
            text="전체 결과",
            command=self._conformance_open_results_summary_window,
        ).pack(side="left", padx=(8, 0))

        mid = ttk.Frame(parent)
        mid.pack(fill="both", expand=True, padx=4, pady=(0, 2))
        tree_fr = ttk.Frame(mid)
        tree_fr.pack(fill="both", expand=True)
        cols = ("pick", "reboot", "script", "ref", "summary", "local", "config", "result")
        tree = ttk.Treeview(
            tree_fr,
            columns=cols,
            show="headings",
            selectmode="none",
            takefocus=1,
        )
        self.conformance_list_tree = tree
        self.conformance_scroll_canvas = None
        tree.heading("pick", text="선택")
        tree.column("pick", width=44, anchor="center", stretch=False)
        tree.heading("reboot", text="재부팅")
        tree.column("reboot", width=52, anchor="center", stretch=False)
        tree.heading("script", text="스크립트")
        tree.column("script", width=190, anchor="w", stretch=False)
        tree.heading("ref", text="표 참조")
        tree.column("ref", width=88, anchor="center", stretch=False)
        tree.heading("summary", text="개요")
        tree.column("summary", width=380, anchor="w", stretch=True)
        tree.heading("local", text="로컬")
        tree.column("local", width=52, anchor="center", stretch=False)
        tree.heading("config", text="설정")
        tree.column("config", width=44, anchor="center", stretch=False)
        tree.heading("result", text="결과")
        tree.column("result", width=100, anchor="center", stretch=False)
        tree.tag_configure("row_even", background="#ffffff")
        tree.tag_configure("row_odd", background="#f0f4f8")
        tree.tag_configure("res_idle", foreground="#94a3b8")
        tree.tag_configure("res_wait", foreground="#ca8a04")
        tree.tag_configure("res_run", foreground="#d97706")
        tree.tag_configure("res_pass", foreground="#15803d")
        tree.tag_configure("res_fail", foreground="#b91c1c")
        tree.tag_configure("res_stop", foreground="#64748b")
        tree.tag_configure("res_mixed", foreground="#334155")
        self._conformance_row_parity: dict[str, str] = {}

        def _on_tree_click(evt: tk.Event) -> None:
            row = tree.identify_row(evt.y)
            if not row:
                return
            col = tree.identify_column(evt.x)
            if col == "#1":
                bv = self.conformance_check_vars.get(row)
                if bv is not None:
                    new_val = not bv.get()
                    if row in self._conformance_scripts_318x():
                        self._conformance_set_318x_linked_check(new_val)
                    else:
                        bv.set(new_val)
            elif col == "#2":
                rbv = getattr(self, "conformance_reboot_vars", {}).get(row)
                if rbv is not None:
                    rbv.set(not rbv.get())
                    self._conformance_sync_tree_reboot(row)
                    try:
                        self._on_any_setting_changed()
                    except Exception:
                        pass
            elif col == "#7":
                if row in _CONFORMANCE_PER_TEST_SCHEMA:
                    self._conformance_open_per_test_settings(row)

        tree.bind("<Button-1>", _on_tree_click)

        def _wheel_tree(evt: tk.Event) -> None:
            if evt.delta:
                tree.yview_scroll(int(-evt.delta / 120), "units")

        tree.bind("<MouseWheel>", _wheel_tree)
        tree.bind("<Double-1>", self._conformance_on_tree_double_click_detail)

        ys = ttk.Scrollbar(tree_fr, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        tree_fr.rowconfigure(0, weight=1)
        tree_fr.columnconfigure(0, weight=1)

        self._conformance_rebuild_list_tree()
        self.after(0, self._conformance_refresh_row_result_labels)

    def _conformance_run_checked(self) -> None:
        cred = self._conformance_collect_ssh_settings()
        if cred is None:
            return
        to_run = [
            fn
            for fn in self._conformance_gui_script_order()
            if self.conformance_check_vars.get(fn) is not None and self.conformance_check_vars[fn].get()
        ]
        if not to_run:
            messagebox.showwarning("Conformance", "실행할 항목을 하나 이상 선택하세요.")
            return
        if any(f in self._conformance_scripts_318x() for f in to_run):
            suite = self._conformance_ordered_318x_local()
            if suite:
                self._conformance_set_318x_linked_check(True)
                expanded = self._conformance_expand_run_list(to_run)
                if expanded != to_run:
                    self.append_log(
                        f"[Conformance-run] 3.1.8.x 일괄 실행: {', '.join(suite)}\n"
                    )
                to_run = expanded
        to_run = self._conformance_order_run_list(to_run)
        missing = [fn for fn in to_run if self._conformance_script_local_path(fn) is None]
        if missing:
            messagebox.showwarning(
                "Conformance",
                "로컬에서 스크립트를 찾지 못했습니다.\n" + "\n".join(missing),
            )
            return
        if self._conformance_run_busy:
            messagebox.showwarning("Conformance", "이미 실행 중입니다.")
            return
        repeat_count = self._conformance_parse_repeat_count()
        if repeat_count is None:
            messagebox.showwarning("Conformance", "반복 횟수는 0(무한) 이상의 정수로 입력하세요.")
            return
        opts = self._conformance_default_run_options()
        # Keep previous per-item results visible while rerunning a subset.
        self._conformance_cancel_event.clear()
        self._conformance_run_busy = True
        self._conformance_run_stats_mode = "repeat" if repeat_count != 1 else "manual_repeat"
        self._conformance_run_active_targets = set(to_run)
        self.after(0, self._refresh_log_target_hint_line)
        rep_note = "반복=무한" if repeat_count == 0 else (f"반복={repeat_count}회" if repeat_count > 1 else "")
        rep_suffix = f", {rep_note}" if rep_note else ""
        self.append_log(
            f"[Conformance-run] 선택 항목 실행 시작: {', '.join(to_run)} (출력은 이 로그 창에 표시, ORU 설정은 업로드 직전에 반영{rep_suffix})\n"
        )
        try:
            self.conformance_stop_btn.configure(state="normal")
        except tk.TclError:
            pass
        try:
            self.conformance_sync_btn.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass
        self.after(0, self._conformance_refresh_row_result_labels)
        threading.Thread(
            target=self._conformance_run_worker,
            args=(to_run, cred, opts, repeat_count),
            daemon=True,
        ).start()

    def _conformance_stop_run(self) -> None:
        self._conformance_cancel_event.set()
        with self._conformance_run_transport_lock:
            ch = self._conformance_run_script_channel
            cli = self._conformance_run_ssh_client
        rd_boost = getattr(self, "_conformance_oru_boost_remote_dir", None)
        if cli is not None and rd_boost:

            def _stop_boost() -> None:
                def _log(msg: str) -> None:
                    self._conformance_log_lines_to_gui("Conformance-run", msg)

                try:
                    self._conformance_stop_oru_show_system_boost(cli, rd_boost, _log)
                except Exception:
                    pass

            threading.Thread(target=_stop_boost, daemon=True).start()
        if ch is not None:
            try:
                ch.close()
            except Exception:
                pass
        if cli is not None:
            try:
                cli.close()
            except Exception:
                pass
        try:
            self.conformance_stop_btn.configure(state="disabled")
        except tk.TclError:
            pass

    def _conformance_run_finished(self) -> None:
        active = getattr(self, "_conformance_run_active_targets", None)
        if isinstance(active, set):
            for fname in active:
                ent = self._conformance_progress.get(fname)
                if not isinstance(ent, dict) or ent.get("rc") is None:
                    self._conformance_progress[fname] = {"rc": -2, "status": "STOP"}
                    self._conformance_record_session_run_result(fname, -2, "STOP")
                    self._conformance_commit_final_result(fname, -2, "STOP")
        self._conformance_run_stats_mode = None
        self._conformance_run_active_targets = None
        self._conformance_run_busy = False
        self._conformance_stop_idle_wait = False
        self._conformance_active_host_log = None
        try:
            self.conformance_stop_btn.configure(state="disabled")
        except tk.TclError:
            pass
        try:
            self.conformance_sync_btn.configure(state="normal")
        except (tk.TclError, AttributeError):
            pass
        self._conformance_refresh_row_result_labels()
        self._conformance_refresh_last_run_cache_from_progress()
        self.after(0, self._conformance_refresh_results_summary_window)
        self.after(0, self._refresh_log_target_hint_line)
        self.after(100, self._save_current_config)
        self.after(0, self._maybe_reconnect_start_after_conformance)

    def _schedule_conformance_auto_sync_once(self) -> None:
        if self._conformance_auto_sync_scheduled:
            return
        u, h, p, _pw, _k = self._remote_conn()
        if not u or not h or not p:
            return
        cred = self._conformance_collect_ssh_settings()
        if cred is None:
            return
        self._conformance_auto_sync_scheduled = True
        threading.Thread(target=self._conformance_sync_worker, args=(False, cred, True), daemon=True).start()

    def _conformance_sync_to_remote(self, force: bool) -> None:
        cred = self._conformance_collect_ssh_settings()
        if cred is None:
            return
        threading.Thread(target=self._conformance_sync_worker, args=(force, cred, False), daemon=True).start()

    def _conformance_dbg(self, msg: str) -> None:
        try:
            if bool(self.conformance_debug_var.get()):
                self.append_log(f"[Conformance-debug] {msg}\n")
        except Exception:
            pass

    def _conformance_dbg_async(self, msg: str) -> None:
        self.after(0, lambda m=msg: self._conformance_dbg(m))
