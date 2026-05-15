"""Conformance tab: remote SSH upload/run, ORU JSON merge, /var/tmp paths — mixed into CallhomeGUI."""

from __future__ import annotations

import base64
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
from datetime import datetime
from pathlib import Path, PurePosixPath
from tkinter import messagebox, ttk
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
        "hint": "쉼표(,) 구분, 순차 실행",
        "env_var": "ALARM_OFF_CMDS",
        "wide": True,
    },
    {
        "key": "alarm_on_cmds",
        "label": "ON 명령어",
        "default": "",
        "hint": "쉼표(,) 구분, 순차 실행",
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

_SWM_TEST_FIELDS: list[dict[str, Any]] = [
    {
        "key": "swm_pkg_path",
        "label": "PKG 파일 (로컬)",
        "default": "",
        "hint": "로컬 PC의 SW 패키지 파일 경로",
        "env_var": None,
        "wide": True,
        "file_picker": True,
        "file_types": [("Package files", "*.pkg *.tar.gz *.bin *.img"), ("All files", "*.*")],
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

_CONFORMANCE_PER_TEST_SCHEMA: dict[str, dict[str, Any]] = {
    "conformance_3131.sh": {
        "title": "3.1.3.1 M-Plane Supervision (positive)",
        "fields": [
            {
                "key": "supervision_cycles",
                "label": "Supervision 반복 횟수",
                "default": "3",
                "hint": "알림 N회 수신 + watchdog 전송 후 PASS",
                "env_var": "SUPERVISION_NEEDED",
            },
        ],
    },
    "conformance_3132.sh": {
        "title": "3.1.3.2 M-Plane Supervision (negative)",
        "fields": [
            {
                "key": "supervision_cycles",
                "label": "초기 Supervision 횟수",
                "default": "1",
                "hint": "알림 N회 후 watchdog 중단 → 세션 끊김 확인",
                "env_var": "SUPERVISION_NEEDED",
            },
            {
                "key": "post_reset_wait_sec",
                "label": "시험 후 ORU 리셋 대기(초)",
                "default": "360",
                "hint": "ORU 재부팅 후 Call Home 대기 (연속 실행 시)",
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
        "shared_with": "conformance_3162.sh",
        "fields": _SWM_TEST_FIELDS,
    },
    "conformance_3162.sh": {
        "title": "3.1.6.2 O-RU Software Update (negative)",
        "shared_with": "conformance_3161.sh",
        "fields": _SWM_TEST_FIELDS,
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
        return m

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
    ) -> int:
        """원격에서 스크립트 1개 실행. 취소 시 -2, 그 외 원격 exit code. stdout/stderr는 host_log_path에 tee."""
        cfg_payload = self._conformance_effective_config_json_text()
        cfg_b2 = cfg_payload.encode("utf-8")
        sftp.putfo(io.BytesIO(cfg_b2), cfg_remote, len(cfg_b2))
        log_line(f"refreshed ORU config on host (실행 직전 Settings 반영, {len(cfg_payload)} bytes)")
        envp = self._conformance_bash_env_exports(opts)
        per_test_envp = self._conformance_per_test_env_exports(fname)
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
                        log_line(s.rstrip("\n"))
                    got = True
                if ch.recv_stderr_ready():
                    b = ch.recv_stderr(4096)
                    s = b.decode(errors="replace")
                    if s:
                        log_line(s.rstrip("\n"))
                    got = True
                if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                    break
                if not got:
                    time.sleep(0.12)
            rc = ch.recv_exit_status()
            st = "PASS" if rc == 0 else "FAIL"
            log_line(f"---- END {fname} exit={rc} [{st}] ----")
            return int(rc)
        finally:
            with self._conformance_run_transport_lock:
                self._conformance_run_script_channel = None

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
                "PORT": gv("NETCONF_PORT"),
                "PRODUCT-CODE": gv("PRODUCT"),
                "CLI-ID": gv("CLI-ID"),
                "CLI-PW": gv("CLI-PW"),
                "LOCAL-IF": gv("LOCAL_IF"),
            }
        }
        # Include software-management settings from per-test config (3.1.6.x)
        swm_settings = (
            self._conformance_per_test_settings.get("conformance_3161.sh")
            or self._conformance_per_test_settings.get("conformance_3162.sh")
            or {}
        )
        swm_pw = (swm_settings.get("swm_server_pw") or "").strip()
        swm_ip = (swm_settings.get("swm_server_ip") or "").strip()
        swm_id = (swm_settings.get("swm_server_id") or "root").strip()
        swm_pkg = (swm_settings.get("swm_pkg_path") or "").strip()
        if swm_pkg and swm_ip:
            pkg_filename = os.path.basename(swm_pkg)
            swm_path = f"sftp://{swm_id}@{swm_ip}/tmp/netconf_PKG/{pkg_filename}"
            obj["software-management"] = {
                "path": swm_path,
                "password": swm_pw,
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

    def _conformance_effective_config_json_text(self) -> str:
        """ORU JSON for remote --config: always from current Settings (GUI) only."""
        gui_txt = self._conformance_build_management_config_json()
        gui_obj = json.loads(gui_txt)
        self._conformance_apply_config_stubs(gui_obj)
        gui_obj = self._conformance_strip_json_nulls(gui_obj)
        return json.dumps(gui_obj, ensure_ascii=True, indent=2)

    def _conformance_bash_env_exports(self, opts: ConformanceRunOptions) -> str:
        parts: list[str] = []
        for key in ("USER", "PASSWORD", "ALLOWED_IP", "LOCAL_IP", "CALLHOME_PORT", "NETCONF_PORT", "PRODUCT", "LOG_PATH"):
            var = self.fields.get(key)
            if var is None:
                continue
            val = var.get().strip()
            parts.append(f"export {key}={shlex.quote(val)}")
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

    def _conformance_refresh_last_run_cache_from_progress(self) -> None:
        by_script: dict[str, Any] = {}
        for fname, ent in self._conformance_progress.items():
            if isinstance(ent, dict) and ent.get("rc") is not None:
                by_script[fname] = {"rc": ent.get("rc"), "status": ent.get("status")}
        if not by_script:
            self._conformance_last_run_snapshot_cache = None
            return
        self._conformance_last_run_snapshot_cache = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "by_script": by_script,
            "summary": self._conformance_summarize_pass_fail_counts(by_script),
        }

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
        tree = getattr(self, "conformance_list_tree", None)
        if tree is not None:
            for fname, _r, _en in self._conformance_test_rows():
                if not tree.exists(fname):
                    continue
                lp = self._conformance_script_local_path(fname)
                st = "Ready" if lp is not None else "miss"
                try:
                    tree.set(fname, "local", st)
                except tk.TclError:
                    pass
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
                    prev = bs.get(fname)
                    if isinstance(prev, dict) and prev.get("rc") is not None:
                        rc, st = prev.get("rc"), str(prev.get("status") or "").upper()
                        if rc == -2 or st == "STOP":
                            text, tag = "STOP", "res_stop"
                        elif rc == 0 or st == "PASS":
                            text, tag = "PASS", "res_pass"
                        elif isinstance(rc, int) and rc != 0:
                            text, tag = (f"FAIL ({rc})", "res_fail")
                        else:
                            text, tag = (st or "—", "res_mixed")
                tree.set(fname, "result", text)
                row_tag = self._conformance_row_parity.get(fname, "row_even")
                tree.item(fname, tags=(tag, row_tag))
            except tk.TclError:
                pass

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
        self, fnames: list[str], cred: tuple[str, str, str, str, str], opts: ConformanceRunOptions
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
            cfg_payload = self._conformance_effective_config_json_text()
        except Exception as exc:
            self.after(0, lambda e=str(exc): messagebox.showerror("Conformance", f"설정 JSON 오류:\n{e}"))
            self.after(0, self._conformance_run_finished)
            return

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
            cfg_bytes = cfg_payload.encode("utf-8")
            sftp.putfo(io.BytesIO(cfg_bytes), cfg_remote, len(cfg_bytes))
            try:
                sftp.chmod(cfg_remote, 0o644)
            except OSError:
                pass
            log_line(f"merged ORU config (현재 Settings 반영) -> {cfg_remote} ({len(cfg_payload)} bytes)")

            to_upload: list[str] = []
            for fn in fnames:
                if fn not in to_upload:
                    to_upload.append(fn)
            pre_m = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")
            t8 = getattr(_conf_manifest, "CONFORMANCE_SCRIPTS_318X", frozenset())
            if pre_m and any(f in t8 for f in fnames):
                if self._conformance_script_local_path(pre_m) and pre_m not in to_upload:
                    to_upload.insert(0, pre_m)

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
                log_line(f"uploaded {fname}")

            spec_map = self._conformance_spec_ref_map()
            pre_3180_done = False
            three8 = getattr(_conf_manifest, "CONFORMANCE_SCRIPTS_318X", frozenset())
            pre_b = getattr(_conf_manifest, "CONFORMANCE_SCRIPT_PRE_3180", "")

            for fname in fnames:
                if self._conformance_cancel_event.is_set():
                    log_line("사용자 중지로 중단")
                    self._conformance_progress[fname] = {"rc": -2, "status": "STOP"}
                    self.after(0, self._conformance_refresh_row_result_labels)
                    break
                if fname in three8 and not pre_3180_done and pre_b:
                    lp_pre = self._conformance_script_local_path(pre_b)
                    if lp_pre:
                        log_line(f"---- PRE 3.1.8.x → {pre_b} (3.1.8.0 사전 단계) ----")
                        host_pre = self._conformance_host_run_log_path(pre_b)
                        self._conformance_active_host_log = host_pre
                        self._conformance_last_host_log = host_pre
                        self.after(0, self._refresh_log_target_hint_line)
                        self._conformance_detail_lines[pre_b] = []
                        self._conformance_detail_capture_key = pre_b
                        self._conformance_detail_run_started_wall[pre_b] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self._conformance_detail_run_started_mono[pre_b] = time.monotonic()
                        try:
                            rc_pre = self._conformance_exec_remote_script(
                                client,
                                sftp,
                                pre_b,
                                opts,
                                remote_dir,
                                cfg_remote,
                                spec_map.get(pre_b, "3.1.8.0-prep"),
                                host_pre,
                                log_line,
                            )
                        finally:
                            self._conformance_detail_capture_key = None
                            self._conformance_detail_run_ended_wall[pre_b] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            self._conformance_detail_run_ended_mono[pre_b] = time.monotonic()
                        if rc_pre == -2:
                            self._conformance_progress[fname] = {"rc": -2, "status": "STOP"}
                            self.after(0, self._conformance_refresh_row_result_labels)
                            break
                        if rc_pre != 0:
                            log_line(f"사전 단계 실패 (exit {rc_pre}). 3.1.8.x 실행을 중단합니다.")
                            self._conformance_progress[fname] = {"rc": rc_pre, "status": "FAIL"}
                            self.after(0, self._conformance_refresh_row_result_labels)
                            break
                    else:
                        log_line(f"WARN: 사전 스크립트 없음 ({pre_b}), 3.1.8.x 는 그대로 시도합니다.")
                    pre_3180_done = True

                self._conformance_progress[fname] = {"rc": None, "status": "RUN"}
                self.after(0, self._conformance_refresh_row_result_labels)

                # SWM tests: upload PKG to remote /tmp/netconf_PKG/
                if not self._conformance_swm_upload_pkg(sftp, fname, log_line):
                    self._conformance_progress[fname] = {"rc": 1, "status": "FAIL"}
                    self.after(0, self._conformance_refresh_row_result_labels)
                    continue

                spec_ref = spec_map.get(fname, "")
                host_log = self._conformance_host_run_log_path(fname)
                self._conformance_active_host_log = host_log
                self._conformance_last_host_log = host_log
                self.after(0, self._refresh_log_target_hint_line)
                self._conformance_detail_lines[fname] = []
                self._conformance_detail_capture_key = fname
                self._conformance_detail_run_started_wall[fname] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._conformance_detail_run_started_mono[fname] = time.monotonic()
                try:
                    rc = self._conformance_exec_remote_script(
                        client, sftp, fname, opts, remote_dir, cfg_remote, spec_ref, host_log, log_line
                    )
                finally:
                    self._conformance_detail_capture_key = None
                    self._conformance_detail_run_ended_wall[fname] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._conformance_detail_run_ended_mono[fname] = time.monotonic()
                if rc == -2:
                    self._conformance_progress[fname] = {"rc": -2, "status": "STOP"}
                    self.after(0, self._conformance_refresh_row_result_labels)
                    break
                st = "PASS" if rc == 0 else "FAIL"
                self._conformance_progress[fname] = {"rc": rc, "status": st}
                self.after(0, self._conformance_refresh_row_result_labels)

                if fname == "conformance_3132.sh" and fnames.index(fname) < len(fnames) - 1:
                    wait_s = 360
                    try:
                        wait_s = int(self._conformance_get_per_test_val(fname, "post_reset_wait_sec") or "360")
                    except (ValueError, TypeError):
                        wait_s = 360
                    if wait_s > 0:
                        log_line(f"3.1.3.2 완료 → ORU 리셋 대기 {wait_s}초 ({wait_s // 60}분 {wait_s % 60}초)")
                        for elapsed in range(wait_s):
                            if self._conformance_cancel_event.is_set():
                                log_line("ORU 리셋 대기 중 사용자 중지")
                                break
                            if elapsed > 0 and elapsed % 30 == 0:
                                log_line(f"ORU 리셋 대기 중… {elapsed}/{wait_s}초")
                            time.sleep(1)
                        else:
                            log_line(f"ORU 리셋 대기 {wait_s}초 완료, 다음 시험 진행")
            try:
                sftp.close()
            except Exception:
                pass
        except Exception as exc:
            log_line(f"RUN ERROR: {exc}")
            self.after(0, lambda e=str(exc): messagebox.showerror("Conformance", str(e)))
        finally:
            if client is not None:
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
        out.append("[진행 시각]")
        out.append(f"  시작(로컬 시각): {sw}")
        out.append(f"  종료(로컬 시각): {ew}")
        out.append(f"  경과: {elapsed or '—'}")
        out.append("")
        out.append("[GUI에 적용된 타임아웃·간격 (Conformance 탭 / Settings)]")
        out.append(f"  NETCONF_RPC_TIMEOUT: {opts.netconf_rpc_timeout.strip() or '30'} s")
        out.append(f"  NETCONF_IDLE_TIMEOUT: {opts.netconf_idle_timeout.strip() or '120'} s")
        out.append(f"  SUPERVISION_INTERVAL: {opts.supervision_interval.strip() or '60'} s")
        out.append(f"  SUPERVISION_RESET_CYCLES: {(opts.supervision_reset_cycles or '').strip() or '30'}")
        out.append(f"  SUPERVISION_NEGATIVE_FAIL_ON_CYCLE: {(opts.supervision_negative_fail_on_cycle or '').strip() or '3'}")
        out.append(f"  CONN_DELAY: {opts.conn_delay.strip() or '3'} s")
        out.append(f"  post_listen_wait: {(opts.post_listen_wait_sec or '').strip() or '0'} s")
        out.append("")
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

    def _conformance_select_all_checked(self) -> None:
        for bv in self.conformance_check_vars.values():
            bv.set(True)

    def _conformance_clear_all_checked(self) -> None:
        for bv in self.conformance_check_vars.values():
            bv.set(False)

    # ── per-test settings ──────────────────────────────────────────

    def _conformance_get_per_test_val(self, fname: str, key: str) -> str:
        schema = _CONFORMANCE_PER_TEST_SCHEMA.get(fname)
        if not schema:
            return ""
        stored = self._conformance_per_test_settings.get(fname, {}).get(key)
        if stored is not None:
            return stored
        for f in schema["fields"]:
            if f["key"] == key:
                return f["default"]
        return ""

    def _conformance_per_test_env_exports(self, fname: str) -> str:
        schema = _CONFORMANCE_PER_TEST_SCHEMA.get(fname)
        if not schema:
            return ""
        settings = self._conformance_per_test_settings.get(fname, {})
        parts: list[str] = []
        for field in schema["fields"]:
            env_var = field.get("env_var")
            if not env_var:
                continue
            val = (settings.get(field["key"]) or "").strip() or field["default"]
            parts.append(f"export {env_var}={shlex.quote(val)}")
        # For SWM tests: build SW_PKG_REMOTE_PATH from per-test settings
        if fname in ("conformance_3161.sh", "conformance_3162.sh"):
            swm_env = self._conformance_swm_env_export(fname)
            if swm_env:
                parts.append(swm_env)
        return " ; ".join(parts) + (" ; " if parts else "")

    def _conformance_swm_env_export(self, fname: str) -> str:
        """Build SW_PKG_REMOTE_PATH env var for 3.1.6.x tests."""
        settings = self._conformance_per_test_settings.get(fname, {})
        pkg_path = (settings.get("swm_pkg_path") or "").strip()
        server_ip = (settings.get("swm_server_ip") or "").strip()
        server_id = (settings.get("swm_server_id") or "root").strip()
        if not pkg_path or not server_ip:
            return ""
        pkg_filename = os.path.basename(pkg_path)
        remote_pkg_path = f"/tmp/netconf_PKG/{pkg_filename}"
        url = f"sftp://{server_id}@{server_ip}{remote_pkg_path}"
        return f"export SW_PKG_REMOTE_PATH={shlex.quote(url)}"

    def _conformance_swm_upload_pkg(
        self, sftp: Any, fname: str, log_line: Any
    ) -> bool:
        """Upload local PKG to remote /tmp/netconf_PKG/ for 3.1.6.x tests."""
        if fname not in ("conformance_3161.sh", "conformance_3162.sh"):
            return True
        settings = self._conformance_per_test_settings.get(fname, {})
        pkg_path = (settings.get("swm_pkg_path") or "").strip()
        if not pkg_path:
            log_line("[WARN] SWM PKG 경로가 설정되지 않았습니다. 설정창에서 지정하세요.")
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
            log_line(f"PKG 업로드 시작: {pkg_filename} → {remote_path}")
            sftp.put(pkg_path, remote_path)
            try:
                sftp.chmod(remote_path, 0o644)
            except OSError:
                pass
            fsize = os.path.getsize(pkg_path)
            log_line(f"PKG 업로드 완료: {remote_path} ({fsize:,} bytes)")
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
        cur = self._conformance_per_test_settings.get(fname, {})
        for i, field in enumerate(schema["fields"], start=1):
            ttk.Label(fr, text=field["label"]).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            sv = tk.StringVar(value=cur.get(field["key"]) or field["default"])
            w = 40 if field.get("wide") else 12
            if field.get("file_picker"):
                entry_fr = ttk.Frame(fr)
                entry_fr.grid(row=i, column=1, sticky="we", pady=2)
                ent = ttk.Entry(entry_fr, textvariable=sv, width=w - 6)
                ent.pack(side="left", fill="x", expand=True)
                ftypes = field.get("file_types", [("All files", "*.*")])

                def _browse(s=sv, ft=ftypes) -> None:
                    from tkinter import filedialog
                    p = filedialog.askopenfilename(filetypes=ft)
                    if p:
                        s.set(p)

                ttk.Button(entry_fr, text="선택…", command=_browse, width=6).pack(side="left", padx=(4, 0))
            else:
                ent = ttk.Entry(fr, textvariable=sv, width=w)
                ent.grid(row=i, column=1, sticky="we", pady=2)
            entries[field["key"]] = sv
            if field.get("hint"):
                ttk.Label(fr, text=field["hint"], foreground="#64748b", font=("", 8)).grid(
                    row=i, column=2, sticky="w", padx=(6, 0), pady=2
                )

        def _apply() -> None:
            vals: dict[str, str] = {}
            for key, sv in entries.items():
                v = sv.get().strip()
                if v:
                    vals[key] = v
            self._conformance_per_test_settings[fname] = vals
            shared = schema.get("shared_with")
            if shared:
                self._conformance_per_test_settings[shared] = dict(vals)
            try:
                self._on_any_setting_changed()
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
        self._conformance_run_labels.clear()
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
                "3.1.8.x 실행 전에는 3.1.8.0 사전 단계 스크립트가 자동으로 한 번 실행됩니다. "
                "실행 출력(stdout/stderr)은 메인 화면 하단 로그 창에 표시됩니다. "
                "표에서 행을 더블클릭하면 해당 항목의 STEP·원인·타임아웃 요약 상세 창이 열립니다(약 2초마다 갱신)."
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

        mid = ttk.Frame(parent)
        mid.pack(fill="both", expand=True, padx=4, pady=(0, 2))
        tree_fr = ttk.Frame(mid)
        tree_fr.pack(fill="both", expand=True)
        cols = ("pick", "script", "ref", "summary", "local", "config", "result")
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
        tree.heading("script", text="스크립트")
        tree.column("script", width=200, anchor="w", stretch=False)
        tree.heading("ref", text="표 참조")
        tree.column("ref", width=88, anchor="center", stretch=False)
        tree.heading("summary", text="개요")
        tree.column("summary", width=420, anchor="w", stretch=True)
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
                    bv.set(not bv.get())
            elif col == "#6":
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

        nrows = len(self._conformance_test_rows())
        tree.configure(height=min(28, max(8, nrows)))

        for idx, (fname, ref, summ) in enumerate(self._conformance_test_rows()):
            bv = tk.BooleanVar(value=False)
            self.conformance_check_vars[fname] = bv
            lp = self._conformance_script_local_path(fname)
            loc = "Ready" if lp is not None else "miss"
            pick = "☐"
            cfg_mark = "⚙" if fname in _CONFORMANCE_PER_TEST_SCHEMA else ""
            row_tag = "row_even" if idx % 2 == 0 else "row_odd"
            self._conformance_row_parity[fname] = row_tag
            tree.insert("", "end", iid=fname, values=(pick, fname, ref, summ, loc, cfg_mark, "—"), tags=("res_idle", row_tag))

            def _on_bv_write(*_a: Any, fn: str = fname) -> None:
                self._conformance_sync_tree_pick(fn)
                try:
                    self._on_any_setting_changed()
                except Exception:
                    pass

            try:
                bv.trace_add("write", _on_bv_write)
            except Exception:
                pass

        self.after(0, self._conformance_refresh_row_result_labels)

    def _conformance_run_checked(self) -> None:
        cred = self._conformance_collect_ssh_settings()
        if cred is None:
            return
        to_run = [fn for fn, bv in self.conformance_check_vars.items() if bv.get()]
        if not to_run:
            messagebox.showwarning("Conformance", "실행할 항목을 하나 이상 선택하세요.")
            return
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
        opts = self._conformance_default_run_options()
        self._conformance_progress.clear()
        self._conformance_cancel_event.clear()
        self._conformance_run_busy = True
        self._conformance_run_active_targets = set(to_run)
        self.after(0, self._refresh_log_target_hint_line)
        self.append_log(
            f"[Conformance-run] 선택 항목 실행 시작: {', '.join(to_run)} (출력은 이 로그 창에 표시, ORU 설정은 업로드 직전에 반영)\n"
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
        threading.Thread(target=self._conformance_run_worker, args=(to_run, cred, opts), daemon=True).start()

    def _conformance_stop_run(self) -> None:
        self._conformance_cancel_event.set()
        with self._conformance_run_transport_lock:
            ch = self._conformance_run_script_channel
            cli = self._conformance_run_ssh_client
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
        self.after(0, self._refresh_log_target_hint_line)
        self.after(100, self._save_current_config)

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
