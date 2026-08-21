# -*- coding: utf-8 -*-
"""O-RAN O-RU DDoS Validation GUI (refactored from monolithic oran_trex_master)."""

import base64
import datetime
import json
import os
import posixpath
import re
import shlex
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, scrolledtext, ttk

import paramiko

from oran_validation.constants import (
    CONFIG_FILE,
    FPING_MAX_LINES,
    SSH_CONNECT_TIMEOUT,
    SSH_TIMEOUT,
    TREX_STARTUP_TIMEOUT,
)
from oran_validation import validators
from oran_validation.remote_pcap_builder import PCAP_BUILDER_SCRIPT
from oran_validation.remote_trex_scripts import (
    link_check_script,
    play_traffic_stl_script,
    stats_monitor_script,
    stop_traffic_script,
)


class ORanValidationGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("O-RAN O-RU DDoS Validation System v6.2")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        target_w, target_h = 1280, 1060
        if screen_w < target_w + 40 or screen_h < target_h + 80:
            try:
                # 작은 해상도에서는 자동 최대화로 잘림을 줄인다.
                self.root.state("zoomed")
            except Exception:
                fit_w = max(980, screen_w - 40)
                fit_h = max(760, screen_h - 80)
                self.root.geometry(f"{fit_w}x{fit_h}")
        else:
            self.root.geometry(f"{target_w}x{target_h}")

        self.ssh_client = None
        self.trex_server_ssh = None
        self.monitor_ssh_client = None
        self.monitor_running = False
        self.fping_running = False
        self.trex_ready = False
        self.fixed_trex_path = "/home/slab/trex/v3.08"
        self.ping_window = None
        self.txt_ping_window = None
        self.var_fping_target = tk.StringVar(value="192.168.11.2")
        self.var_fping_interval = tk.StringVar(value="1")
        self.var_fping_size = tk.StringVar(value="56")

        self.ssh_lock = threading.Lock()
        self.monitor_lock = threading.Lock()
        self.fping_lock = threading.Lock()
        self.connect_lock = threading.Lock()
        self.connect_in_progress = False
        self._active_tx_port = 0
        self.stats_widgets = {}
        self.latest_port_stats = {}
        self.tx_requested_by_port = {0: False, 1: False}
        self.link_poll_interval_ms = 5000
        self.status_line_count = 300
        self.monitor_status_lines = deque(maxlen=self.status_line_count)
        self.auto_switch_lock = threading.Lock()
        self.auto_run_stop = {0: threading.Event(), 1: threading.Event()}
        self.auto_run_thread = {0: None, 1: None}
        self.auto_next_index = {0: 0, 1: 0}
        self.auto_order_by_port = {0: [], 1: []}
        self.auto_order_editor = None
        self.pcap_build_thread = None
        self.pcap_build_stop = threading.Event()
        # Preset Batch Mode: MIN 항목 제거, MAX는 문구를 제거(동작은 tier=max로 유지)
        self.preset_defs = [
            {"key": "fixed64_19", "label": "Fixed 64B", "tier": "max", "pkt_mode": "Fixed", "pkt_size": "64", "rate": "19.0"},
            {"key": "fixed9000_10_b", "label": "Fixed 9000B", "tier": "max", "pkt_mode": "Fixed", "pkt_size": "9000", "rate": "22.0"},
            {"key": "std_random_22", "label": "Standard Random", "tier": "max", "pkt_mode": "Standard Random", "pkt_size": "", "rate": "22.0"},
            {"key": "jumbo_random_22", "label": "Jumbo Random", "tier": "max", "pkt_mode": "Jumbo Random", "pkt_size": "", "rate": "22.0"},

            # 추가 프리셋
            {
                "key": "netconf_64_untag_830",
                "label": "Netconf Session 64B unTag 830",
                "tier": "max",
                "file_tag": "netconf64_untag830",
                "pkt_mode": "Fixed",
                "pkt_size": "64",
                "rate": "22.0",
                "attack_type": "NETCONF Session (관리망 마비)",
                "dst_port": "830",
                "vlan_id": "__UNTAG__",
            },
            {
                "key": "netconf_64_tag_830",
                "label": "Netconf Session 64B Tag 830",
                "tier": "max",
                "file_tag": "netconf64_tag830",
                "pkt_mode": "Fixed",
                "pkt_size": "64",
                "rate": "22.0",
                "attack_type": "NETCONF Session (관리망 마비)",
                "dst_port": "830",
                "vlan_id": "__USE_GUI_VLAN__",
            },
            {
                "key": "gtpu_64",
                "label": "F1-U GTP-U 64B",
                "tier": "max",
                "file_tag": "gtpu64",
                "pkt_mode": "Fixed",
                "pkt_size": "64",
                "rate": "22.0",
                "attack_type": "F1-U GTP-U (비정상 패킷 필터링)",
                "vlan_id": "__UNTAG__",
            },
            {
                "key": "random_pkt_64_all_mut",
                "label": "Random Packet 64B",
                "tier": "max",
                "file_tag": "randompkt64_allmut",
                "pkt_mode": "Fixed",
                "pkt_size": "64",
                "rate": "22.0",
                # 공격 타입은 U-Plane 기반 + mutation 전부 ON으로 랜덤/비정상 패킷을 만든다.
                "attack_type": "eCPRI U-Plane (대역폭/RRC 과부하)",
                "mutation_enable": True,
                "rand_mac": True,
                "rand_ip": True,
                "rand_vlan": True,
                "rand_ethertype": True,
                "malformed_ecpri": True,
                "invalid_length": True,
                "rand_l4_port": True,
            },
        ]

        self.style = ttk.Style()
        self.style.theme_use("clam")

        self._create_notebook()
        self._load_config()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _safe_ui(self, func, *args, **kwargs):
        self.root.after(0, lambda: func(*args, **kwargs))

    def _looks_like_ssh_socket_error(self, text):
        t = str(text or "")
        return any(k in t for k in ["Socket is closed", "SSH session not active", "Channel closed", "EOF", "timed out"])

    def _safe_ssh_exec(self, command, password=None, timeout=SSH_TIMEOUT):
        def _exec_once():
            if not self.ssh_client:
                return "", "SSH client is not connected"
            with self.ssh_lock:
                try:
                    stdin, stdout, stderr = self.ssh_client.exec_command(
                        command,
                        get_pty=True,
                        timeout=timeout
                    )
                    if password:
                        stdin.write(password + "\n")
                        stdin.flush()

                    chan = stdout.channel
                    # exec_command의 timeout은 connect/handshake에만 적용되는 경우가 있어,
                    # 채널 read에도 별도 타임아웃을 강제한다.
                    eff_timeout = SSH_TIMEOUT if timeout is None else timeout
                    deadline = time.time() + float(eff_timeout)
                    out_chunks = []
                    err_chunks = []
                    while True:
                        # stderr
                        while chan.recv_stderr_ready():
                            err_chunks.append(chan.recv_stderr(4096))
                        # stdout
                        while chan.recv_ready():
                            out_chunks.append(chan.recv(4096))

                        if chan.exit_status_ready():
                            # 남은 버퍼를 한 번 더 비운다
                            while chan.recv_ready():
                                out_chunks.append(chan.recv(4096))
                            while chan.recv_stderr_ready():
                                err_chunks.append(chan.recv_stderr(4096))
                            break

                        if time.time() > deadline:
                            raise TimeoutError(f"SSH command timeout after {eff_timeout}s")
                        time.sleep(0.05)

                    output = b"".join(out_chunks).decode("utf-8", errors="replace")
                    error = b"".join(err_chunks).decode("utf-8", errors="replace")
                    return output, error
                except Exception as e:
                    return "", str(e)

        out, err = _exec_once()
        if self._looks_like_ssh_socket_error(err):
            if self._reconnect_main_ssh_client():
                out2, err2 = _exec_once()
                return out2, err2
        return out, err

    def _safe_ssh_exec_exit(self, command, password=None, timeout=SSH_TIMEOUT):
        """
        (stdout, stderr, exit_status). exit_status 0 = 명령 성공(일반).
        -1 = 연결 실패 또는 Paramiko 오류.
        """
        def _exec_once():
            if not self.ssh_client:
                return "", "SSH client is not connected", -1
            with self.ssh_lock:
                try:
                    stdin, stdout, stderr = self.ssh_client.exec_command(
                        command,
                        get_pty=True,
                        timeout=timeout
                    )
                    if password:
                        stdin.write(password + "\n")
                        stdin.flush()

                    chan = stdout.channel
                    eff_timeout = SSH_TIMEOUT if timeout is None else timeout
                    deadline = time.time() + float(eff_timeout)
                    out_chunks = []
                    err_chunks = []
                    while True:
                        while chan.recv_stderr_ready():
                            err_chunks.append(chan.recv_stderr(4096))
                        while chan.recv_ready():
                            out_chunks.append(chan.recv(4096))

                        if chan.exit_status_ready():
                            while chan.recv_ready():
                                out_chunks.append(chan.recv(4096))
                            while chan.recv_stderr_ready():
                                err_chunks.append(chan.recv_stderr(4096))
                            break

                        if time.time() > deadline:
                            raise TimeoutError(f"SSH command timeout after {eff_timeout}s")
                        time.sleep(0.05)

                    out = b"".join(out_chunks).decode("utf-8", errors="replace")
                    err = b"".join(err_chunks).decode("utf-8", errors="replace")
                    exit_status = chan.recv_exit_status()
                    return out, err, exit_status
                except Exception as e:
                    return "", str(e), -1

        out, err, st = _exec_once()
        if st == -1 and self._looks_like_ssh_socket_error(err):
            if self._reconnect_main_ssh_client():
                return _exec_once()
        return out, err, st

    def _reconnect_main_ssh_client(self):
        """재연결이 필요한 경우 메인 SSH 세션만 다시 연결한다."""
        try:
            with self.ssh_lock:
                old = self.ssh_client
                self.ssh_client = None
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass

                ip = self.ent_server_ip.get().strip()
                user = self.ent_ssh_user.get().strip()
                pw = self.ent_ssh_pw.get()
                cli = paramiko.SSHClient()
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cli.connect(ip, username=user, password=pw, timeout=SSH_CONNECT_TIMEOUT)
                self.ssh_client = cli
                self._apply_ssh_keepalive(cli)
                return True
        except Exception:
            return False

    def _ssh_transport_active(self):
        try:
            cli = self.ssh_client
            if not cli:
                return False
            t = cli.get_transport()
            return t is not None and t.is_active()
        except Exception:
            return False

    def _apply_ssh_keepalive(self, client):
        """유휴 구간(NAT/방화벽 idle timeout)에서 세션이 끊기는 빈도를 줄인다."""
        try:
            t = client.get_transport()
            if t:
                t.set_keepalive(30)
        except Exception:
            pass

    def _ensure_main_ssh_connected(self, attempts=4, delay_sec=2.5, log_ctx=""):
        """
        ssh_client가 없거나 transport가 죽었을 때 재시도.
        순차 Auto처럼 항목 간 대기가 길 때 서버/중간 장비가 idle로 끊은 뒤에도 복구한다.
        """
        ip = (self.ent_server_ip.get() or "").strip()
        user = (self.ent_ssh_user.get() or "").strip()
        if not ip or not user:
            return False
        for attempt in range(1, attempts + 1):
            if self._ssh_transport_active():
                return True
            if self._reconnect_main_ssh_client():
                if log_ctx:
                    self._record_event(f"{log_ctx} SSH 재연결 성공 ({attempt}/{attempts})")
                return True
            if log_ctx:
                self._record_event(f"{log_ctx} SSH 재연결 실패 ({attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(delay_sec)
        return False

    def _close_ssh_sessions(self):
        """기존 SSH 클라이언트를 닫고 모니터 스레드가 쓰는 플래그를 내린 뒤(재연결·종료 공통)."""
        for p in [0, 1]:
            if hasattr(self, "auto_run_stop"):
                self.auto_run_stop[p].set()
            if hasattr(self, "lbl_auto_state_by_port"):
                self._safe_ui(self._set_auto_state, p, "대기", "gray")

        with self.monitor_lock:
            self.monitor_running = False
        with self.fping_lock:
            self.fping_running = False

        self.trex_ready = False
        if hasattr(self, "lbl_phy_link_by_port"):
            for p in [0, 1]:
                lbl = self.lbl_phy_link_by_port.get(p)
                if lbl is not None:
                    self._safe_ui(lbl.config, text="N/A", foreground="gray")
        if hasattr(self, "lbl_monitor_state_by_port"):
            self._safe_ui(self._set_monitor_state, "멈춤", None)

        main = self.ssh_client
        trex = self.trex_server_ssh
        mon = self.monitor_ssh_client
        self.ssh_client = None
        self.trex_server_ssh = None
        self.monitor_ssh_client = None

        if main is not None:
            try:
                main.close()
            except Exception:
                pass
        if trex is not None:
            try:
                trex.close()
            except Exception:
                pass
        if mon is not None:
            try:
                mon.close()
            except Exception:
                pass

    def _get_pkt_mode_average_size(self):
        mode = self.combo_pkt_mode.get().strip()
        if mode == "Standard Random":
            return (64 + 1500) / 2
        if mode == "Jumbo Random":
            return (64 + 9000) / 2
        return None

    def _selected_ports(self):
        return [0, 1]

    def _selected_port_for_tx(self, port):
        return [int(port)]

    def _get_trex_path(self):
        return validators.validate_remote_path(self.fixed_trex_path, "TRex Path")

    def _event_ts(self):
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _record_event(self, text):
        line = f"[{self._event_ts()}] {text}"
        self._update_status_text(line)
        try:
            with open("oran_autorun_events.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _toggle_mutation_options(self):
        enabled = self.var_mutation_enable.get()
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in self.mutation_widgets:
            widget.config(state=state)

    def _toggle_preset_mode(self):
        use_presets = hasattr(self, "var_use_presets") and self.var_use_presets.get()
        manual_state = tk.DISABLED if use_presets else tk.NORMAL
        # 수동 패킷 생성 파라미터만 음영 처리
        self.combo_pkt_mode.config(state="disabled" if use_presets else "readonly")
        self.ent_pkt_size.config(state=manual_state if self.combo_pkt_mode.get().strip() == "Fixed" else tk.DISABLED)
        # 프리셋 모드에서는 공통 Rate가 아닌 프리셋별 Rate를 사용한다.
        self.ent_rate.config(state=manual_state)
        self.ent_pcap_ms.config(state=manual_state)
        self.chk_mutation_enable.config(state=manual_state)
        if use_presets:
            for w in self.mutation_widgets:
                w.config(state=tk.DISABLED)
            if hasattr(self, "chk_tcp_synack_only"):
                self.chk_tcp_synack_only.config(state=tk.DISABLED)
        else:
            self._toggle_mutation_options()
            self._update_tcp_synack_option_state()

    def _update_tcp_synack_option_state(self):
        """NETCONF/TCP Test Type일 때만 SYN-ACK only 옵션을 활성화한다."""
        if not hasattr(self, "chk_tcp_synack_only"):
            return
        use_presets = hasattr(self, "var_use_presets") and self.var_use_presets.get()
        atype = self.combo_attack.get().upper() if hasattr(self, "combo_attack") else ""
        is_tcp = ("NETCONF" in atype) or ("TCP" in atype)
        if use_presets or not is_tcp:
            self.chk_tcp_synack_only.config(state=tk.DISABLED)
            if not is_tcp:
                self.var_tcp_synack_only.set(False)
        else:
            self.chk_tcp_synack_only.config(state=tk.NORMAL)

    def _validate_inputs(self, step=1):
        errors = []

        server_ip = self.ent_server_ip.get().strip()
        ssh_user = self.ent_ssh_user.get().strip()
        trex_path = self._get_trex_path()
        pcap_path = self.ent_pcap_path.get().strip()
        pcap_name = self.ent_pcap_name.get().strip()
        attack_type = self.combo_attack.get().strip()

        if not server_ip or not validators.is_valid_ip(server_ip):
            errors.append("Server IP: 올바른 IP 주소를 입력해야 합니다.")

        if not ssh_user:
            errors.append("SSH User: 필수 입력값입니다.")

        try:
            validators.validate_remote_path(trex_path, "TRex Path")
        except ValueError as e:
            errors.append(str(e))

        try:
            validators.validate_remote_path(pcap_path, "PCAP Save Path")
        except ValueError as e:
            errors.append(str(e))

        try:
            validators.sanitize_remote_filename(pcap_name)
        except ValueError as e:
            errors.append(str(e))

        if not attack_type:
            errors.append("Test Type: 시험 유형을 선택해야 합니다.")

        src_mac = self.ent_src_mac.get().strip()
        dst_mac = self.ent_dst_mac.get().strip()

        if src_mac and not validators.is_valid_mac(src_mac):
            errors.append("Attacker MAC: 올바른 MAC 주소 형식이 아닙니다.")
        if dst_mac and not validators.is_valid_mac(dst_mac):
            errors.append("O-RU MAC: 올바른 MAC 주소 형식이 아닙니다.")

        atype = attack_type.upper()
        is_l2 = any(k in atype for k in ["U-PLANE", "C-PLANE", "PRACH", "PTP"])

        if not is_l2:
            src_ip = self.ent_src_ip.get().strip()
            dst_ip = self.ent_dst_ip.get().strip()
            if not src_ip or not validators.is_valid_ip(src_ip):
                errors.append("Attacker IP: 올바른 IP 주소를 입력해야 합니다.")
            if not dst_ip or not validators.is_valid_ip(dst_ip):
                errors.append("O-RU IP: 올바른 IP 주소를 입력해야 합니다.")

            dst_port_str = self.ent_dst_port.get().strip()
            try:
                dst_port = int(dst_port_str)
                if not (1 <= dst_port <= 65535):
                    errors.append("Destination Port: 1~65535 범위여야 합니다.")
            except ValueError:
                errors.append("Destination Port: 숫자만 입력 가능합니다.")

        vlan_str = self.ent_vlan_id.get().strip()
        if vlan_str:
            try:
                vlan_id = int(vlan_str)
                if not (1 <= vlan_id <= 4094):
                    errors.append("VLAN ID: 1~4094 범위여야 합니다.")
            except ValueError:
                errors.append("VLAN ID: 숫자만 입력 가능합니다.")

        use_presets = hasattr(self, "var_use_presets") and self.var_use_presets.get() and step == 1
        if use_presets:
            selected = [k for k, v in self.preset_vars.items() if v.get()]
            if not selected:
                errors.append("Preset Mode: 최소 1개 프리셋을 선택해야 합니다.")
            for key in selected:
                try:
                    rate = float(self.preset_rate_vars[key].get().strip())
                    if not (0.1 <= rate <= 25.0):
                        errors.append(f"Preset Rate({key}): 0.1~25.0 Gbps 사이여야 합니다.")
                except ValueError:
                    errors.append(f"Preset Rate({key}): 숫자만 입력 가능합니다.")
        else:
            pkt_mode = self.combo_pkt_mode.get().strip()
            if pkt_mode not in ["Fixed", "Standard Random", "Jumbo Random"]:
                errors.append("Packet Size Mode: 유효한 모드를 선택해야 합니다.")

            # Packet Size는 PCAP 생성(step 1) 시점에만 필수 검증한다.
            # Start TX(step 2)는 이미 생성된 PCAP을 사용하므로 공란이어도 진행 가능해야 한다.
            if step == 1 and pkt_mode == "Fixed":
                try:
                    pkt_size = int(self.ent_pkt_size.get())
                    if not (64 <= pkt_size <= 9000):
                        errors.append("Packet Size: 64~9000 바이트 사이여야 합니다.")
                except ValueError:
                    errors.append("Packet Size: 숫자만 입력 가능합니다.")

            try:
                rate_src = self.var_line_rate.get().strip()
                if step != 1 and hasattr(self, "ent_rate_by_port"):
                    active_port = getattr(self, "_active_tx_port", 0)
                    rate_src = self.ent_rate_by_port.get(active_port, self.ent_rate).get().strip()
                rate = float(rate_src)
                if not (0.1 <= rate <= 25.0):
                    errors.append("Line Rate: 0.1~25.0 Gbps 사이여야 합니다.")
            except ValueError:
                errors.append("Line Rate: 숫자만 입력 가능합니다.")

        if step == 1:
            try:
                ms = float(self.ent_pcap_ms.get())
                if ms <= 0:
                    errors.append("Packet Length: 0보다 커야 합니다.")
            except ValueError:
                errors.append("Packet Length: 숫자만 입력 가능합니다.")
        else:
            try:
                duration_src = self.ent_duration_min
                if hasattr(self, "ent_duration_by_port"):
                    active_port = getattr(self, "_active_tx_port", 0)
                    duration_src = self.ent_duration_by_port.get(active_port, self.ent_duration_min)
                duration_min = float(duration_src.get())
                if duration_min < 0:
                    errors.append("Transmission Duration: 0 또는 양수여야 합니다.")
            except ValueError:
                errors.append("Transmission Duration: 숫자만 입력 가능합니다.")

        if errors:
            messagebox.showerror("입력값 검증 오류", "\n".join(errors))
            return False
        return True

    def _validate_server_inputs(self):
        errors = []
        server_ip = self.ent_server_ip.get().strip()
        ssh_user = self.ent_ssh_user.get().strip()
        trex_path = self._get_trex_path()

        if not server_ip or not validators.is_valid_ip(server_ip):
            errors.append("Server IP: 올바른 IP 주소를 입력해야 합니다.")
        if not ssh_user:
            errors.append("SSH User: 필수 입력값입니다.")
        try:
            validators.validate_remote_path(trex_path, "TRex Path")
        except ValueError as e:
            errors.append(str(e))

        if errors:
            messagebox.showerror("입력값 검증 오류", "\n".join(errors))
            return False
        return True

    def _create_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tab_setup = ttk.Frame(self.notebook, padding=10)
        self.tab_control = ttk.Frame(self.notebook, padding=10)
        self.tab_validation = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_setup, text=" 1. 서버 및 검증 설정 ")
        self.notebook.add(self.tab_control, text=" 2. TRex 트래픽 제어소 ")
        self.notebook.add(self.tab_validation, text=" 3. 판정 및 검증 ")

        self._build_server_tab()
        self._build_ru_attack_tab()
        self._build_control_tab()
        self._build_validation_tab()

    def _build_server_tab(self):
        self.setup_top_row = ttk.Frame(self.tab_setup)
        self.setup_top_row.pack(fill=tk.X, padx=10, pady=10)

        frame = ttk.LabelFrame(self.setup_top_row, text="Linux Server Configuration", padding=10)
        frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Label(frame, text="Server IP:").grid(row=0, column=0, sticky="w", pady=5)
        self.ent_server_ip = ttk.Entry(frame, width=40)
        self.ent_server_ip.grid(row=0, column=1, padx=10, pady=5)

        ttk.Label(frame, text="SSH User:").grid(row=1, column=0, sticky="w", pady=5)
        self.ent_ssh_user = ttk.Entry(frame, width=40)
        self.ent_ssh_user.grid(row=1, column=1, padx=10, pady=5)

        ttk.Label(frame, text="SSH Password:").grid(row=2, column=0, sticky="w", pady=5)
        self.ent_ssh_pw = ttk.Entry(frame, width=40, show="*")
        self.ent_ssh_pw.grid(row=2, column=1, padx=10, pady=5)

        ttk.Label(frame, text="TRex Path:").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Label(frame, text=self.fixed_trex_path, foreground="gray").grid(row=3, column=1, padx=10, pady=5, sticky="w")

        ttk.Label(frame, text="TRex NIC/코어:").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Label(
            frame,
            text="고정 설정 (Port 0,1 / 6 Cores)",
            foreground="gray",
        ).grid(row=4, column=1, padx=10, pady=5, sticky="w")

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=5, column=0, columnspan=2, pady=8, sticky="w")

        self.btn_connect = tk.Button(
            btn_row,
            text="서버 연결 및 TRex 엔진 구동",
            bg="#3498db",
            fg="white",
            font=("Malgun Gothic", 10, "bold"),
            command=self.connect_server
        )
        self.btn_connect.pack(side=tk.LEFT, ipadx=14, ipady=3)

        self.btn_ping_test = tk.Button(
            btn_row,
            text="Ping Test",
            bg="#34495e",
            fg="white",
            font=("Malgun Gothic", 10, "bold"),
            command=self.open_ping_test_window,
        )
        self.btn_ping_test.pack(side=tk.LEFT, padx=(8, 0), ipadx=10, ipady=3)

        self.lbl_server_status = ttk.Label(frame, text="상태: 연결 대기 중...", foreground="gray")
        self.lbl_server_status.grid(row=6, column=0, columnspan=2, pady=(2, 4))

        # 수동 갱신 버튼 없이 자동 주기 갱신
        self.root.after(1000, self._schedule_link_status_poll)

    def _build_ru_attack_tab(self):
        parent_top = getattr(self, "setup_top_row", self.tab_setup)
        right_wrap = ttk.Frame(parent_top)
        right_wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        link_frame = ttk.LabelFrame(right_wrap, text="Physical Link Status", padding=6)
        link_frame.pack(fill=tk.X, pady=(0, 6))
        self.lbl_phy_link_by_port = {}
        for i, p in enumerate([0, 1]):
            ttk.Label(link_frame, text=f"P{p}:").grid(row=i, column=0, sticky="w", pady=1)
            lbl = ttk.Label(link_frame, text="N/A", foreground="gray")
            lbl.grid(row=i, column=1, sticky="w", padx=6, pady=1)
            self.lbl_phy_link_by_port[p] = lbl

        ru_frame = ttk.LabelFrame(right_wrap, text="Target RU Network Configuration", padding=10)
        ru_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(ru_frame, text="Attacker MAC:").grid(row=0, column=0, sticky="w", pady=2)
        self.ent_src_mac = ttk.Entry(ru_frame, width=25)
        self.ent_src_mac.grid(row=0, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="O-RU MAC:").grid(row=0, column=2, sticky="w", pady=2, padx=(20, 0))
        self.ent_dst_mac = ttk.Entry(ru_frame, width=25)
        self.ent_dst_mac.grid(row=0, column=3, padx=10, pady=2)

        ttk.Label(ru_frame, text="Attacker IP:").grid(row=1, column=0, sticky="w", pady=2)
        self.ent_src_ip = ttk.Entry(ru_frame, width=25)
        self.ent_src_ip.grid(row=1, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="O-RU IP:").grid(row=1, column=2, sticky="w", pady=2, padx=(20, 0))
        self.ent_dst_ip = ttk.Entry(ru_frame, width=25)
        self.ent_dst_ip.grid(row=1, column=3, padx=10, pady=2)

        ttk.Label(ru_frame, text="Destination Port:").grid(row=2, column=0, sticky="w", pady=2)
        self.ent_dst_port = ttk.Entry(ru_frame, width=25)
        self.ent_dst_port.grid(row=2, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="VLAN ID (Optional):").grid(row=2, column=2, sticky="w", pady=2, padx=(20, 0))
        self.ent_vlan_id = ttk.Entry(ru_frame, width=25)
        self.ent_vlan_id.grid(row=2, column=3, padx=10, pady=2)

        ttk.Label(ru_frame, text="PCAP Save Path:").grid(row=3, column=0, sticky="w", pady=2)
        self.ent_pcap_path = ttk.Entry(ru_frame, width=25)
        self.ent_pcap_path.grid(row=3, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="PCAP File Name:").grid(row=3, column=2, sticky="w", pady=2, padx=(20, 0))
        self.ent_pcap_name = ttk.Entry(ru_frame, width=25)
        self.ent_pcap_name.grid(row=3, column=3, padx=10, pady=2)

        atk_frame = ttk.LabelFrame(self.tab_setup, text="Step 1: 검증용 패킷 블록(PCAP) 생성 설정", padding=10)
        atk_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        # 가이드 영역을 오른쪽 끝으로 밀기 위한 스페이서 컬럼
        atk_frame.columnconfigure(3, weight=1)
        atk_frame.columnconfigure(5, weight=1)
        atk_frame.columnconfigure(6, minsize=430)

        manual_frame = ttk.LabelFrame(atk_frame, text="Test Packet Settings / Mutation", padding=6)
        manual_frame.grid(row=0, column=0, rowspan=6, padx=(0, 8), pady=2, sticky="nsew")

        ttk.Label(manual_frame, text="Test Type:").grid(row=0, column=0, sticky="w", pady=1)

        attack_types = [
            "eCPRI U-Plane (대역폭/RRC 과부하)",
            "eCPRI C-Plane (제어 평면 마비)",
            "PRACH Spoofing (무선 자원 고갈)",
            "F1-U GTP-U (비정상 패킷 필터링)",
            "NETCONF Session (관리망 마비)",
            "TCP SYN Flood (세션/메모리 고갈)",
        ]

        self.combo_attack = ttk.Combobox(manual_frame, width=38, state="readonly", values=attack_types)
        self.combo_attack.grid(row=0, column=1, padx=8, pady=1, sticky="w")
        self.combo_attack.bind("<<ComboboxSelected>>", self._update_test_description)

        ttk.Label(manual_frame, text="Packet Size Mode:").grid(row=1, column=0, sticky="w", pady=1)
        self.combo_pkt_mode = ttk.Combobox(
            manual_frame,
            width=20,
            state="readonly",
            values=["Fixed", "Standard Random", "Jumbo Random"]
        )
        self.combo_pkt_mode.grid(row=1, column=1, padx=8, pady=1, sticky="w")
        self.combo_pkt_mode.bind("<<ComboboxSelected>>", self._on_pkt_mode_changed)

        ttk.Label(manual_frame, text="Packet Size (Bytes):").grid(row=2, column=0, sticky="w", pady=1)
        self.ent_pkt_size = ttk.Entry(manual_frame, width=15)
        self.ent_pkt_size.grid(row=2, column=1, padx=8, pady=1, sticky="w")
        self.ent_pkt_size.bind("<KeyRelease>", self._calculate_pps)

        ttk.Label(manual_frame, text="Line Rate (Gbps):").grid(row=3, column=0, sticky="w", pady=1)
        self.var_line_rate = tk.StringVar(value="10.0")
        self.ent_rate = ttk.Entry(manual_frame, width=15, textvariable=self.var_line_rate)
        self.ent_rate.grid(row=3, column=1, padx=8, pady=1, sticky="w")
        self.var_line_rate.trace_add("write", lambda *_: self._calculate_pps())

        ttk.Label(manual_frame, text="Packet Length (ms):").grid(row=4, column=0, sticky="w", pady=1)
        self.ent_pcap_ms = ttk.Entry(manual_frame, width=15)
        self.ent_pcap_ms.grid(row=4, column=1, padx=8, pady=1, sticky="w")

        preset_frame = ttk.LabelFrame(atk_frame, text="Preset Batch Mode", padding=6)
        preset_frame.grid(row=0, column=2, rowspan=4, padx=10, pady=2, sticky="nw")
        self.var_use_presets = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            preset_frame,
            text="프리셋 사용 (수동 패킷설정 비활성화)",
            variable=self.var_use_presets,
            command=self._toggle_preset_mode,
        ).pack(anchor="w", pady=(0, 6))
        self.preset_vars = {}
        self.preset_rate_vars = {}
        for p in self.preset_defs:
            rowf = ttk.Frame(preset_frame)
            rowf.pack(fill=tk.X, pady=0)
            var = tk.BooleanVar(value=False)
            self.preset_vars[p["key"]] = var
            ttk.Checkbutton(rowf, text=p["label"], variable=var).pack(side=tk.LEFT, anchor="w")
            rvar = tk.StringVar(value=p["rate"])
            self.preset_rate_vars[p["key"]] = rvar
            ttk.Entry(rowf, width=6, textvariable=rvar).pack(side=tk.RIGHT, padx=(6, 0))
            ttk.Label(rowf, text="Gbps", foreground="gray").pack(side=tk.RIGHT)

        guide_frame = ttk.LabelFrame(atk_frame, text="[시험 설정 가이드]", padding=6)
        guide_frame.grid(row=0, column=6, rowspan=6, padx=6, pady=2, sticky="nsew")
        self.txt_desc = tk.Text(
            guide_frame,
            width=55,
            height=8,
            bg="#e8f6f3",
            font=("Malgun Gothic", 9),
            wrap=tk.WORD
        )
        self.txt_desc.pack(fill=tk.BOTH, expand=True)
        self.txt_desc.config(state=tk.DISABLED)

        mut_frame = ttk.LabelFrame(manual_frame, text="Mutation / Randomization Options", padding=6)
        mut_frame.grid(row=5, column=0, columnspan=2, pady=(4, 0), sticky="ew")

        self.var_mutation_enable = tk.BooleanVar(value=False)
        self.chk_mutation_enable = ttk.Checkbutton(
            mut_frame,
            text="Enable Mutation / Randomization",
            variable=self.var_mutation_enable,
            command=self._toggle_mutation_options
        )
        self.chk_mutation_enable.grid(row=0, column=0, columnspan=2, sticky="w", pady=5)

        self.var_rand_mac = tk.BooleanVar(value=False)
        self.var_rand_ip = tk.BooleanVar(value=False)
        self.var_rand_vlan = tk.BooleanVar(value=False)
        self.var_rand_ethertype = tk.BooleanVar(value=False)
        self.var_malformed_ecpri = tk.BooleanVar(value=False)
        self.var_invalid_length = tk.BooleanVar(value=False)
        self.var_rand_l4_port = tk.BooleanVar(value=False)

        self.chk_rand_mac = ttk.Checkbutton(mut_frame, text="Random MAC", variable=self.var_rand_mac)
        self.chk_rand_ip = ttk.Checkbutton(mut_frame, text="Random IP", variable=self.var_rand_ip)
        self.chk_rand_vlan = ttk.Checkbutton(mut_frame, text="Random VLAN", variable=self.var_rand_vlan)
        self.chk_rand_ethertype = ttk.Checkbutton(mut_frame, text="Random Ether Type", variable=self.var_rand_ethertype)
        self.chk_malformed_ecpri = ttk.Checkbutton(mut_frame, text="Malformed eCPRI Header", variable=self.var_malformed_ecpri)
        self.chk_invalid_length = ttk.Checkbutton(
            mut_frame,
            text="Invalid Length Field (헤더 length만 변조, 프레임 크기 고정)",
            variable=self.var_invalid_length,
        )
        self.chk_rand_l4_port = ttk.Checkbutton(mut_frame, text="Random TCP/UDP Port", variable=self.var_rand_l4_port)

        self.chk_rand_mac.grid(row=1, column=0, sticky="w", padx=10, pady=3)
        self.chk_rand_ip.grid(row=2, column=0, sticky="w", padx=10, pady=3)
        self.chk_rand_vlan.grid(row=3, column=0, sticky="w", padx=10, pady=3)
        self.chk_rand_l4_port.grid(row=4, column=0, sticky="w", padx=10, pady=3)

        self.chk_rand_ethertype.grid(row=1, column=1, sticky="w", padx=10, pady=3)
        self.chk_malformed_ecpri.grid(row=2, column=1, sticky="w", padx=10, pady=3)
        self.chk_invalid_length.grid(row=3, column=1, sticky="w", padx=10, pady=3)

        self.mutation_widgets = [
            self.chk_rand_mac,
            self.chk_rand_ip,
            self.chk_rand_vlan,
            self.chk_rand_ethertype,
            self.chk_malformed_ecpri,
            self.chk_invalid_length,
            self.chk_rand_l4_port,
        ]

        # NETCONF/TCP 전용: Wireshark Conversation Completeness = SYN-ACK only
        self.var_tcp_synack_only = tk.BooleanVar(value=False)
        self.chk_tcp_synack_only = ttk.Checkbutton(
            mut_frame,
            text="TCP SYN-ACK only (Completeness: SYN-ACK=1, others=0)",
            variable=self.var_tcp_synack_only,
        )
        self.chk_tcp_synack_only.grid(row=5, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 3))

        sim_frame = ttk.LabelFrame(preset_frame, text="Expected Throughput Simulation", padding="8")
        sim_frame.pack(fill=tk.X, pady=(8, 0))

        self.lbl_max_limit = ttk.Label(
            sim_frame,
            text="L2 Line Rate Limit: -",
            foreground="purple",
            font=("Malgun Gothic", 9, "bold")
        )
        self.lbl_max_limit.pack(anchor="w", padx=5, pady=2)

        self.lbl_pps_calc = ttk.Label(sim_frame, text="Packets/sec: -", foreground="blue")
        self.lbl_pps_calc.pack(anchor="w", padx=5, pady=2)

        self.lbl_gbps_calc = ttk.Label(sim_frame, text="Expected L1 Throughput: -", foreground="blue")
        self.lbl_gbps_calc.pack(anchor="w", padx=5, pady=2)

        self.lbl_line_calc = ttk.Label(sim_frame, text="L1 Line Rate Usage: -", foreground="blue")
        self.lbl_line_calc.pack(anchor="w", padx=5, pady=2)

        self.lbl_estimate_info = ttk.Label(
            sim_frame,
            text="",
            foreground="darkgreen",
            font=("Malgun Gothic", 9)
        )
        self.lbl_estimate_info.pack(anchor="w", padx=5, pady=2)

        action_frame = ttk.Frame(atk_frame)
        action_frame.grid(row=7, column=0, columnspan=7, pady=8, sticky="ew")

        self.btn_build_pcap = tk.Button(
            action_frame,
            text="Step 1: 검증용 패킷 생성 (PCAP Build)",
            bg="#f39c12",
            fg="white",
            font=("Malgun Gothic", 11, "bold"),
            command=self.build_pcap
        )
        self.btn_build_pcap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, ipady=6)
        self.btn_stop_build_pcap = tk.Button(
            action_frame,
            text="생성 중단",
            bg="#7f8c8d",
            fg="white",
            font=("Malgun Gothic", 10, "bold"),
            command=self.stop_build_pcap,
            state=tk.DISABLED,
        )
        self.btn_stop_build_pcap.pack(side=tk.LEFT, padx=10, ipady=6)

        self._toggle_mutation_options()
        self._toggle_preset_mode()

    def _build_control_tab(self):
        pcap_frame = ttk.LabelFrame(self.tab_control, text="PCAP File Management", padding=10)
        pcap_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(pcap_frame, text="Available PCAP Files:").pack(side=tk.LEFT, padx=5)

        list_frame = ttk.Frame(pcap_frame)
        list_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        self.list_pcap_files = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=4, exportselection=False)
        self.list_pcap_files.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.list_pcap_files.yview)
        self.list_pcap_files.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.btn_refresh_pcap = ttk.Button(pcap_frame, text="Refresh Files", command=self._refresh_pcap_list)
        self.btn_refresh_pcap.pack(side=tk.LEFT, padx=5)

        self.btn_delete_pcap = ttk.Button(pcap_frame, text="Delete Selected", command=self.delete_pcap)
        self.btn_delete_pcap.pack(side=tk.LEFT, padx=5)
        self.btn_auto_order_editor = ttk.Button(
            pcap_frame,
            text="Auto Run 순서 편집(팝업)",
            command=self.open_auto_order_editor,
        )
        self.btn_auto_order_editor.pack(side=tk.LEFT, padx=5)

        ctrl_frame = ttk.LabelFrame(self.tab_control, text="Step 2: 트래픽 인가 통제 (Traffic TX Control)", padding=15)
        ctrl_frame.pack(fill=tk.X, padx=10, pady=10)

        # Auto Run 실행 방식 (MAX rate 시험을 위해 포트 순차 모드 지원)
        self.var_auto_run_mode = tk.StringVar(value="per-port")  # per-port | sequential-ports
        mode_frame = ttk.Frame(ctrl_frame)
        mode_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(mode_frame, text="Auto Run 실행 방식:", font=("Malgun Gothic", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(
            mode_frame,
            text="Port별(기존)",
            value="per-port",
            variable=self.var_auto_run_mode,
            command=self._on_auto_run_mode_changed,
        ).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Radiobutton(
            mode_frame,
            text="포트 순차(먼저 눌른 포트를 순차적으로 진행후 다음포트를 진행합니다.)",
            value="sequential-ports",
            variable=self.var_auto_run_mode,
            command=self._on_auto_run_mode_changed,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(mode_frame, text="진행상황 창 다시 열기", command=self._reopen_coord_progress_window).pack(
            side=tk.LEFT, padx=(10, 0)
        )

        per_port_frame = ttk.LabelFrame(ctrl_frame, text="Port별 TX 제어 (개별 PCAP/Rate)", padding=10)
        per_port_frame.pack(fill=tk.X, pady=10)

        self.ent_duration_by_port = {}
        self.ent_rate_by_port = {}
        self.combo_pcap_by_port = {}
        self.ent_auto_item_duration_min_by_port = {}
        self.ent_auto_guard_between_min_by_port = {}
        self.var_auto_loop_by_port = {}
        self.lbl_auto_state_by_port = {}
        self.btn_start_by_port = {}
        self.btn_stop_by_port = {}
        self.btn_auto_start_by_port = {}
        self.btn_auto_stop_by_port = {}

        # 반복 실행은 포트 간 꼬임 방지를 위해 공유한다.
        self._var_auto_loop_shared = tk.BooleanVar(value=True)

        for row, port in enumerate([0, 1]):
            p_row = ttk.LabelFrame(per_port_frame, text=f"Port {port}", padding=8)
            p_row.pack(fill=tk.X, pady=4)

            f_top = ttk.Frame(p_row)
            f_top.pack(fill=tk.X)

            ttk.Label(f_top, text="PCAP:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
            combo_pcap = ttk.Combobox(f_top, width=36, state="readonly", values=[])
            combo_pcap.grid(row=0, column=1, sticky="w", padx=4, pady=2)
            self.combo_pcap_by_port[port] = combo_pcap

            ttk.Label(f_top, text="Rate (Gbps):").grid(row=0, column=2, sticky="w", padx=8, pady=2)
            ent_rate = ttk.Entry(f_top, width=10)
            ent_rate.grid(row=0, column=3, sticky="w", padx=4, pady=2)
            self.ent_rate_by_port[port] = ent_rate

            ttk.Label(f_top, text="Duration (Min):").grid(row=0, column=4, sticky="w", padx=8, pady=2)
            ent = ttk.Entry(f_top, width=10)
            ent.grid(row=0, column=5, sticky="w", padx=4, pady=2)
            self.ent_duration_by_port[port] = ent

            ttk.Label(f_top, text="(0=무한 전송)", foreground="blue").grid(
                row=0, column=6, sticky="w", padx=8, pady=2
            )

            btn_start = tk.Button(
                f_top,
                text=f"Start P{port}",
                bg="#27ae60",
                fg="white",
                font=("Arial", 10, "bold"),
                width=12,
                command=lambda p=port: self.play_traffic_for_port(p),
            )
            btn_start.grid(row=0, column=7, padx=8, pady=2)
            self.btn_start_by_port[port] = btn_start

            btn_stop = tk.Button(
                f_top,
                text=f"Stop P{port}",
                bg="#c0392b",
                fg="white",
                font=("Arial", 10, "bold"),
                width=12,
                command=lambda p=port: self.stop_traffic_for_port(p),
            )
            btn_stop.grid(row=0, column=8, padx=4, pady=2)
            self.btn_stop_by_port[port] = btn_stop

            # Auto: 항목 절체 시간(송출 유지) + Guard(항목 사이 유휴). 순차: 시험→Guard→다음 시험.
            f_auto = ttk.Frame(p_row)
            f_auto.pack(fill=tk.X, pady=(8, 0))

            f_times = ttk.Frame(f_auto)
            f_times.pack(side=tk.LEFT, padx=(0, 12))
            ttk.Label(f_times, text="항목 절체 시간(min):").grid(row=0, column=0, sticky="w", padx=(0, 4))
            ent_item = ttk.Entry(f_times, width=8)
            ent_item.grid(row=0, column=1, sticky="w", padx=(0, 8))
            ent_item.insert(0, "30")
            self.ent_auto_item_duration_min_by_port[port] = ent_item
            ttk.Label(f_times, text="Guard Time(min):").grid(row=0, column=2, sticky="w", padx=(0, 4))
            ent_between = ttk.Entry(f_times, width=8)
            ent_between.grid(row=0, column=3, sticky="w")
            ent_between.insert(0, "5")
            self.ent_auto_guard_between_min_by_port[port] = ent_between

            ttk.Checkbutton(f_auto, text="반복 실행", variable=self._var_auto_loop_shared).pack(
                side=tk.LEFT, padx=(0, 12)
            )
            self.var_auto_loop_by_port[port] = self._var_auto_loop_shared

            lbl_auto = ttk.Label(f_auto, text="Auto: 대기", foreground="gray")
            lbl_auto.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

            btn_auto_stop = tk.Button(
                f_auto,
                text=f"Auto Stop P{port}",
                bg="#7f8c8d",
                fg="white",
                font=("Arial", 9, "bold"),
                width=12,
                command=lambda p=port: self.stop_auto_run_for_port(p),
            )
            btn_auto_stop.pack(side=tk.RIGHT, padx=(4, 0))
            self.btn_auto_stop_by_port[port] = btn_auto_stop
            btn_auto_start = tk.Button(
                f_auto,
                text=f"Auto Start P{port}",
                bg="#2c3e50",
                fg="white",
                font=("Arial", 9, "bold"),
                width=12,
                command=lambda p=port: self.start_auto_run_for_port(p),
            )
            btn_auto_start.pack(side=tk.RIGHT, padx=(8, 0))
            self.btn_auto_start_by_port[port] = btn_auto_start
            self.lbl_auto_state_by_port[port] = lbl_auto

        self._on_auto_run_mode_changed()

        # Backward-compatible field used by existing validation/config flow.
        self.ent_duration_min = self.ent_duration_by_port[0]

        control_split = ttk.Frame(ctrl_frame)
        control_split.pack(fill=tk.X, pady=10)

        # Reachability Monitor(ping/fping)은 우선 제외.
        # 단, 설정 load/save 호환을 위해 입력 위젯은 생성하되 화면에는 숨긴다.
        reach_frame = ttk.LabelFrame(control_split, text="Reachability Monitor (disabled)", padding=10)
        # pack하지 않아서 화면에는 보이지 않음

        top_reach = ttk.Frame(reach_frame)
        top_reach.pack(fill=tk.X, pady=5)

        ttk.Label(top_reach, text="Target IP:").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self.ent_fping_target = ttk.Entry(top_reach, width=18, textvariable=self.var_fping_target)
        self.ent_fping_target.grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(top_reach, text="Interval (sec):").grid(row=0, column=2, sticky="w", padx=5, pady=3)
        self.ent_fping_interval = ttk.Entry(top_reach, width=10, textvariable=self.var_fping_interval)
        self.ent_fping_interval.grid(row=0, column=3, padx=5, pady=3)

        ttk.Label(top_reach, text="Payload Size:").grid(row=0, column=4, sticky="w", padx=5, pady=3)
        self.ent_fping_size = ttk.Entry(top_reach, width=10, textvariable=self.var_fping_size)
        self.ent_fping_size.grid(row=0, column=5, padx=5, pady=3)

        btn_reach = ttk.Frame(reach_frame)
        btn_reach.pack(fill=tk.X, pady=5)

        self.btn_fping_start = ttk.Button(btn_reach, text="Start Monitor", command=self.start_reachability_monitor)
        self.btn_fping_start.pack(side=tk.LEFT, padx=5)

        self.btn_fping_stop = ttk.Button(btn_reach, text="Stop Monitor", command=self.stop_reachability_monitor)
        self.btn_fping_stop.pack(side=tk.LEFT, padx=5)

        # 기능 비활성화: 사용자 조작 방지 (화면에도 안 보이지만, 안전 차원)
        for w in (self.ent_fping_target, self.ent_fping_interval, self.ent_fping_size, self.btn_fping_start, self.btn_fping_stop):
            try:
                w.config(state=tk.DISABLED)
            except Exception:
                pass

        stat_split = ttk.PanedWindow(self.tab_control, orient=tk.HORIZONTAL)
        stat_split.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        stat_frame = ttk.LabelFrame(stat_split, text="실시간 DPDK 엔진 통계 모니터 (Real-time Stats)", padding=10)

        state_wrap = ttk.Frame(stat_frame)
        state_wrap.pack(fill=tk.X, pady=(0, 8))
        self.lbl_monitor_state_by_port = {}
        for port in [0, 1]:
            lbl = ttk.Label(
                state_wrap,
                text=f"Port {port} State: 준비중",
                foreground="blue",
                font=("Malgun Gothic", 13, "bold"),
            )
            lbl.pack(side=tk.LEFT, padx=(0, 24))
            self.lbl_monitor_state_by_port[port] = lbl

        ports_wrap = ttk.Frame(stat_frame)
        ports_wrap.pack(fill=tk.X, pady=(0, 8))

        for col, port in enumerate([0, 1]):
            port_box = ttk.LabelFrame(ports_wrap, text=f"Port {port}", padding=8)
            port_box.grid(row=0, column=col, padx=(0, 8) if col == 0 else (8, 0), sticky="nsew")
            ports_wrap.columnconfigure(col, weight=1)

            metrics = {
                "TX BPS": ttk.Label(port_box, text="0.000 Gbps", font=("Consolas", 12, "bold")),
                "RX BPS": ttk.Label(port_box, text="0.000 Gbps", font=("Consolas", 12, "bold")),
                "TX PPS": ttk.Label(port_box, text="0.00 Mpps", font=("Consolas", 12, "bold")),
                "TX Packets": ttk.Label(port_box, text="0", font=("Consolas", 12, "bold")),
                "CPU Util": ttk.Label(port_box, text="0.0%", font=("Consolas", 12, "bold")),
                "Queue Full": ttk.Label(port_box, text="0", font=("Consolas", 12, "bold")),
            }
            self.stats_widgets[port] = metrics

            for row, (name, value_lbl) in enumerate(metrics.items()):
                ttk.Label(port_box, text=f"{name}:", font=("Malgun Gothic", 10)).grid(
                    row=row, column=0, sticky="w", pady=1
                )
                value_lbl.grid(row=row, column=1, sticky="e", padx=(8, 0), pady=1)
        # 기존 "상태 정보" 창은 제거하고, 오른쪽 Monitor State Output만 사용한다.
        self.txt_status = None

        # 기존 Reachability 출력창을 "모니터 상태/이벤트 출력"으로 재사용
        fping_frame = ttk.LabelFrame(stat_split, text="Monitor State Output", padding=10)
        self.txt_fping = scrolledtext.ScrolledText(
            fping_frame,
            bg="#111111",
            fg="#00d7ff",
            font=("Consolas", 12),
            height=20,
        )
        self.txt_fping.pack(fill=tk.BOTH, expand=True)

        stat_split.add(stat_frame, weight=3)
        stat_split.add(fping_frame, weight=4)

    def _build_validation_tab(self):
        criteria_frame = ttk.LabelFrame(self.tab_validation, text="[ O-RU 검증 내성 평가 기준 (Pass / Fail) ]", padding=15)
        criteria_frame.pack(fill=tk.X, padx=10, pady=10)

        pass_desc = """PASS 조건 : 정상적인 방어 및 복구
- 트래픽 인가 중: CPU 부하 100% 도달, 통신 지연, 패킷 Drop 등은 물리적 한계로 정상입니다.
- 트래픽 중단 후: 트래픽 중단 즉시 O-RU가 스스로 버퍼를 비우고 O-DU와의 통신 및 무선 RF 방사를 정상 상태로 완벽히 복구해야 합니다."""

        fail_desc = """FAIL 조건 : 치명적 비정상 상태 발생
- 트래픽 인가 중: 장비 전원 꺼짐, 재부팅(Reboot), 시스템 멈춤(Hang), Watchdog Timeout 발생 시 불합격입니다.
- 트래픽 중단 후: 영구적으로 통신 불능에 빠져 수동으로 장비를 재부팅(Power Cycle)해야만 복구되는 경우 불합격입니다."""

        ttk.Label(
            criteria_frame,
            text=pass_desc,
            foreground="green",
            font=("Malgun Gothic", 10, "bold"),
            justify=tk.LEFT
        ).pack(anchor="w", pady=5)

        ttk.Label(
            criteria_frame,
            text=fail_desc,
            foreground="red",
            font=("Malgun Gothic", 10, "bold"),
            justify=tk.LEFT
        ).pack(anchor="w", pady=5)

        check_frame = ttk.LabelFrame(self.tab_validation, text="[ 검증 종료 후 필수 확인 체크리스트 ]", padding=15)
        check_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        checklist_text = (
            "1. [시스템 로그] UART 콘솔에 OOM(Out of Memory), Kernel Panic 등 치명적 에러가 발생하지 않음\n\n"
            "2. [관리 제어망] NMS에서 O-RU로 NETCONF / SSH 통신이 응답하며 제어권이 살아있음\n\n"
            "3. [프론트홀망] O-DU와의 PTP Clock Lock 상태 및 C/U-Plane 세션이 정상 복구됨\n\n"
            "4. [RF 무선망] Spectrum Analyzer 확인 시, 안테나 Tx 출력 파형이 공격 이전 정상 파형으로 돌아옴"
        )
        ttk.Label(
            check_frame,
            text=checklist_text,
            justify=tk.LEFT,
            font=("Malgun Gothic", 10),
            wraplength=1100,
        ).pack(anchor="w", pady=8, padx=10)

    def _on_pkt_mode_changed(self, event=None):
        mode = self.combo_pkt_mode.get().strip()
        if mode == "Fixed":
            self.ent_pkt_size.config(state=tk.NORMAL)
        else:
            self.ent_pkt_size.delete(0, tk.END)
            self.ent_pkt_size.config(state=tk.DISABLED)
        self._calculate_pps()

    def _run_trex_server_terminal(self, ip, user, pw, trex_path, cores):
        try:
            self.trex_server_ssh = paramiko.SSHClient()
            self.trex_server_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.trex_server_ssh.connect(ip, username=user, password=pw, timeout=SSH_CONNECT_TIMEOUT)

            trex_path_q = validators.quote_remote(trex_path)
            kill_cmd = "sudo -S pkill -f t-rex-64"
            stdin, stdout, stderr = self.trex_server_ssh.exec_command(kill_cmd, get_pty=True)
            stdin.write(pw + "\n")
            stdin.flush()
            time.sleep(2)

            cmd = f"cd {trex_path_q} && sudo -S ./t-rex-64 -i -c {shlex.quote(str(cores))}"
            stdin, stdout, stderr = self.trex_server_ssh.exec_command(cmd, get_pty=True)
            stdin.write(pw + "\n")
            stdin.flush()

            start_time = time.time()
            ready = False
            while time.time() - start_time < TREX_STARTUP_TIMEOUT:
                verify_cmd = "ps aux | grep t-rex-64 | grep -v grep"
                stdin2, stdout2, stderr2 = self.trex_server_ssh.exec_command(verify_cmd)
                output = stdout2.read().decode("utf-8", errors="replace").strip()
                if output:
                    ready = True
                    break
                time.sleep(2)

            if ready:
                self.trex_ready = True
                self._safe_ui(
                    self.lbl_server_status.config,
                    text="상태: TRex 엔진 동작 중",
                    foreground="green"
                )
            else:
                self.trex_ready = False
                self._safe_ui(
                    self.lbl_server_status.config,
                    text="상태: TRex 구동 실패",
                    foreground="red"
                )
                return

            for line in iter(stdout.readline, ""):
                if not line:
                    break

        except Exception as e:
            self.trex_ready = False
            self._safe_ui(
                self.lbl_server_status.config,
                text=f"상태: TRex 구동 실패 - {str(e)[:50]}",
                foreground="red"
            )

    def connect_server(self):
        with self.connect_lock:
            if self.connect_in_progress:
                self._update_status_text("이미 서버 연결/재연결 진행 중입니다. 잠시 후 다시 시도하세요.")
                return
            self.connect_in_progress = True
        threading.Thread(target=self._connect_server_bg, daemon=True).start()

    def _connect_server_bg(self):
        try:
            self.trex_ready = False
            self._safe_ui(self.lbl_server_status.config, text="상태: 연결 및 TRex 구성 중...", foreground="orange")

            if not self._validate_server_inputs():
                self._safe_ui(self.lbl_server_status.config, text="상태: 입력값 확인 필요", foreground="red")
                return

            self._close_ssh_sessions()

            ip = self.ent_server_ip.get().strip()
            user = self.ent_ssh_user.get().strip()
            pw = self.ent_ssh_pw.get()
            trex_path = self._get_trex_path()
            selected_cores = "6"

            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_client.connect(ip, username=user, password=pw, timeout=SSH_CONNECT_TIMEOUT)
            self._apply_ssh_keepalive(self.ssh_client)

            check_cmd = f"test -d {validators.quote_remote(trex_path)} && echo OK || echo FAIL"
            check_ok = False
            last_err = ""
            for attempt in range(1, 5):
                out, err, st = self._safe_ssh_exec_exit(check_cmd, timeout=12)
                if st == 0 and "OK" in (out or ""):
                    check_ok = True
                    break

                last_err = (err or out or "").strip()[:200]
                reconnect_needed = any(
                    token in (err or "")
                    for token in ["Socket is closed", "SSH session not active", "Channel closed", "EOF", "timed out"]
                )
                if reconnect_needed:
                    self._safe_ui(self._update_status_text, f"TRex Path 확인 재시도({attempt}/4): 소켓 재연결")
                    self._reconnect_main_ssh_client()
                elif attempt < 4:
                    self._safe_ui(self._update_status_text, f"TRex Path 확인 재시도({attempt}/4)")

                if attempt < 4:
                    time.sleep(min(1.5, 0.4 * attempt))

            if not check_ok:
                self._safe_ui(self.lbl_server_status.config, text="상태: TRex 경로 확인 실패", foreground="red")
                detail = f"\n세부: {last_err}" if last_err else ""
                self._safe_ui(messagebox.showerror, "경로 오류", f"서버에서 TRex Path를 찾을 수 없습니다.\n{trex_path}{detail}")
                self._close_ssh_sessions()
                return

            threading.Thread(
                target=self._run_trex_server_terminal,
                args=(ip, user, pw, trex_path, selected_cores),
                daemon=True
            ).start()

            wait_count = 0
            while not self.trex_ready and wait_count < TREX_STARTUP_TIMEOUT:
                time.sleep(1)
                wait_count += 1

            if not self.trex_ready:
                self._safe_ui(messagebox.showwarning, "경고", "TRex 엔진 구동에 실패했습니다. 서버 상태를 확인하세요.")
                return

            self._refresh_pcap_list()
            self._start_stats_stream()
            self._safe_ui(self.refresh_physical_link_status)

        except Exception as e:
            self._close_ssh_sessions()
            self._safe_ui(self.lbl_server_status.config, text="상태: 연결 실패", foreground="red")
            self._safe_ui(messagebox.showerror, "Error", f"서버 연결에 실패했습니다.\n{e}")
        finally:
            with self.connect_lock:
                self.connect_in_progress = False

    def _start_stats_stream(self):
        if not self.trex_ready or not self.ssh_client:
            return

        server_ip = self.ent_server_ip.get().strip()
        pw = self.ent_ssh_pw.get()
        trex_path = self._get_trex_path()
        ports = self._selected_ports()

        with self.monitor_lock:
            self.monitor_running = True

        self._set_monitor_state("준비중", None)

        # TRex 엔진 기동 직후 RPC가 아직 warm-up 중일 수 있어, 모니터 시작 전에 준비 상태를 확인한다.
        rpc_ready = False
        rpc_check = f"""python3 - <<'PY'
import sys
sys.path.insert(0, {repr(trex_path + "/automation/trex_control_plane/interactive")})
from trex.stl.api import STLClient
c = STLClient(server='127.0.0.1')
c.connect()
c.get_stats(ports={ports!r})
c.disconnect()
print("OK")
PY"""
        for attempt in range(1, 7):
            out, err, st = self._safe_ssh_exec_exit(rpc_check, password=pw, timeout=10)
            if st == 0 and "OK" in (out or ""):
                rpc_ready = True
                break
            self._update_status_text(f"TRex RPC 준비 대기 중... ({attempt}/6)")
            time.sleep(1.0)
        if not rpc_ready:
            detail = (err or out or "unknown")[:140]
            self._update_status_text(f"TRex RPC 준비 실패: {detail}")
            with self.monitor_lock:
                self.monitor_running = False
            return

        stream_cmd = stats_monitor_script(trex_path, server_ip, ports)

        b64_mon = base64.b64encode(stream_cmd.encode("utf-8")).decode("ascii")
        remote_script = "/tmp/mon_stream.py"

        write_cmd = f"printf '%s' {validators.quote_remote(b64_mon)} | base64 -d > {validators.quote_remote(remote_script)}"
        _, write_err, write_st = self._safe_ssh_exec_exit(write_cmd, password=pw)
        if write_st != 0 and "Socket is closed" in (write_err or ""):
            self._update_status_text("SSH 소켓 종료 감지. 모니터 재연결 시도...")
            if self._reconnect_main_ssh_client():
                _, write_err, write_st = self._safe_ssh_exec_exit(write_cmd, password=pw)
        if write_st != 0:
            self._update_status_text(f"모니터 스크립트 쓰기 실패: {write_err[:120]}")
            return

        check_cmd = f"test -f {validators.quote_remote(remote_script)} && echo OK || echo FAIL"
        check_ok = False
        last_check_err = ""
        for attempt in range(1, 4):
            check_out, check_err = self._safe_ssh_exec(check_cmd, password=pw)
            if "OK" in (check_out or ""):
                check_ok = True
                break

            last_check_err = (check_err or check_out or "").strip()
            reconnect_needed = any(
                token in (check_err or "")
                for token in ["Socket is closed", "SSH session not active", "Channel closed", "EOF", "timed out"]
            )
            if reconnect_needed and attempt < 4:
                self._update_status_text(f"모니터 파일 확인 중 소켓 종료. 재연결 후 재시도({attempt}/3)...")
                if not self._reconnect_main_ssh_client():
                    break
                _, write_err, write_st = self._safe_ssh_exec_exit(write_cmd, password=pw)
                if write_st != 0:
                    last_check_err = write_err or last_check_err
                    time.sleep(0.2 * attempt)
                    continue
            if attempt < 3:
                time.sleep(0.2 * attempt)

        if not check_ok:
            self._update_status_text(f"모니터 스크립트 생성 실패: {last_check_err[:120]}")
            return

        def stream_loop():
            mon_cli = None
            try:
                ip = self.ent_server_ip.get().strip()
                user = self.ent_ssh_user.get().strip()
                mon_cli = paramiko.SSHClient()
                mon_cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                mon_cli.connect(ip, username=user, password=pw, timeout=SSH_CONNECT_TIMEOUT)
                self.monitor_ssh_client = mon_cli

                stdin, stdout, stderr = mon_cli.exec_command(
                    f"sudo -S python3 -u {validators.quote_remote(remote_script)}",
                    get_pty=True
                )
                stdin.write(pw + "\n")
                stdin.flush()

                for line in iter(stdout.readline, ""):
                    with self.monitor_lock:
                        if not self.monitor_running:
                            break
                    clean_line = line.strip()
                    if clean_line and not clean_line.startswith("[sudo]"):
                        self._safe_ui(self._handle_monitor_line, clean_line)

            except Exception as e:
                self._safe_ui(self._update_status_text, f"모니터 에러: {str(e)[:80]}")
            finally:
                try:
                    if mon_cli is not None:
                        mon_cli.close()
                except Exception:
                    pass
                self.monitor_ssh_client = None

        threading.Thread(target=stream_loop, daemon=True).start()
        self._update_status_text("통계 모니터 시작됨")
        self._set_monitor_state("준비중", None)

    def refresh_physical_link_status(self):
        threading.Thread(target=self._refresh_physical_link_status_bg, daemon=True).start()

    def _schedule_link_status_poll(self):
        try:
            self.refresh_physical_link_status()
        finally:
            self.root.after(self.link_poll_interval_ms, self._schedule_link_status_poll)

    def _refresh_physical_link_status_bg(self):
        if not self.ssh_client:
            for p in [0, 1]:
                lbl = self.lbl_phy_link_by_port.get(p)
                if lbl is not None:
                    self._safe_ui(lbl.config, text="N/A", foreground="gray")
            return
        pw = self.ent_ssh_pw.get()
        trex_path = self._get_trex_path()
        for p in [0, 1]:
            ok, msg = self._check_port_link_before_start(p, trex_path, pw)
            text = "UP" if ok else "DOWN"
            color = "green" if ok else "red"
            lbl = self.lbl_phy_link_by_port.get(p)
            if lbl is not None:
                self._safe_ui(lbl.config, text=text, foreground=color)

    def _set_monitor_state(self, state_text, port=None):
        if not hasattr(self, "lbl_monitor_state_by_port"):
            return
        if port is None:
            for p, lbl in self.lbl_monitor_state_by_port.items():
                lbl.config(text=f"Port {p} State: {state_text}")
            return
        p = int(port)
        lbl = self.lbl_monitor_state_by_port.get(p)
        if lbl is not None:
            lbl.config(text=f"Port {p} State: {state_text}")

    def _is_port_ready(self, port: int) -> bool:
        if not hasattr(self, "lbl_monitor_state_by_port"):
            return False
        lbl = self.lbl_monitor_state_by_port.get(int(port))
        if lbl is None:
            return False
        return "ready" in lbl.cget("text").lower()

    def _update_port_stats(self, port, tx_bps, rx_bps, tx_pps, tx_pkts, cpu_util, queue_full):
        if port not in self.stats_widgets:
            return
        self.latest_port_stats[port] = {
            "tx_bps": tx_bps,
            "rx_bps": rx_bps,
            "tx_pps": tx_pps,
            "tx_pkts": tx_pkts,
            "cpu_util": cpu_util,
            "queue_full": queue_full,
            "ts": time.time(),
        }
        widgets = self.stats_widgets[port]
        widgets["TX BPS"].config(text=f"{tx_bps/1_000_000_000:.3f} Gbps")
        widgets["RX BPS"].config(text=f"{rx_bps/1_000_000_000:.3f} Gbps")
        widgets["TX PPS"].config(text=f"{tx_pps/1_000_000:.2f} Mpps")
        widgets["TX Packets"].config(text=f"{int(tx_pkts)}")
        widgets["CPU Util"].config(text=f"{cpu_util:.1f}%")
        widgets["Queue Full"].config(text=f"{int(queue_full)}")
        # 사용자가 Start를 요청한 포트만 출력중으로 전환한다.
        # Stop 이후 늦게 도착한 샘플(tx_bps>0)로 상태가 되돌아가지 않도록 막는다.
        if self.tx_requested_by_port.get(int(port), False) and tx_bps > 0:
            self._set_monitor_state("출력중", int(port))

    def _handle_monitor_line(self, line):
        if line.startswith("__STAT__|"):
            parts = line.split("|")
            if len(parts) == 8:
                try:
                    port = int(parts[1])
                    tx_bps = float(parts[2])
                    rx_bps = float(parts[3])
                    tx_pps = float(parts[4])
                    tx_pkts = float(parts[5])
                    cpu_util = float(parts[6])
                    queue_full = float(parts[7])
                    self._update_port_stats(port, tx_bps, rx_bps, tx_pps, tx_pkts, cpu_util, queue_full)
                except Exception:
                    self._update_status_text(f"모니터 파싱 오류: {line[:80]}")
            return

        if line.startswith("__STATE__|"):
            state = line.split("|", 1)[1].strip() if "|" in line else "준비중"
            self._set_monitor_state(state, None)
            self._update_status_text(f"상태 변경: {state}")
            return

        if line.startswith("__INFO__|"):
            self._update_status_text(line.split("|", 1)[1].strip())
            return

        self._update_status_text(line)

    def _append_monitor_verbose_log(self, text):
        try:
            with open("monitor_verbose_output.log", "a", encoding="utf-8") as f:
                f.write(str(text) + "\n")
        except Exception:
            pass

    def _should_display_monitor_line(self, text):
        t = str(text or "").strip()
        if not t:
            return False

        # 1) 이벤트 로그 라인(타임스탬프 포함)은 그대로 보여준다.
        if t.startswith("["):
            return True

        # 2) 단일 토큰/노이즈 출력은 화면에서 숨긴다. (예: 비밀번호 echo 흔적)
        if len(t.split()) <= 1:
            return False

        # 3) 상세 통계/디버그성 로그는 파일로만 남기고 화면에서는 숨긴다.
        noisy_tokens = [
            " log: [INFO]",
            "pcap_total=",
            "sampled=",
            "unique_pkt_lens=",
            "first-check tx_bps=",
            "start check ok tx_bps_sum=",
            "rate set warn:",
            "script write warn:",
            "TRex RPC 준비 대기 중...",
            "통계 모니터 시작됨",
            "TX 시작 요청",
            "트래픽 전송 중지 명령 하달",
        ]
        if any(tok in t for tok in noisy_tokens):
            return False

        # 4) 핵심 상태/오류성 문구는 노출한다.
        keep_tokens = [
            "[ERROR]",
            "실패",
            "재연결",
            "상태 변경:",
            "TRex RPC connected",
            "링크 DOWN",
            "링크 UP",
        ]
        if any(tok in t for tok in keep_tokens):
            return True

        # 5) 그 외 비타임스탬프 라인은 기본적으로 숨기고 파일에만 기록.
        return False

    def _update_status_text(self, text):
        self._append_monitor_verbose_log(text)
        if not self._should_display_monitor_line(text):
            return
        self.monitor_status_lines.append(text)
        # Monitor State Output(txt_fping)만 사용한다.
        if hasattr(self, "txt_fping") and self.txt_fping is not None:
            try:
                self.txt_fping.config(state=tk.NORMAL)
                self.txt_fping.delete("1.0", tk.END)
                self.txt_fping.insert(tk.END, "\n".join(self.monitor_status_lines) + "\n")
                self.txt_fping.see(tk.END)
                self.txt_fping.config(state=tk.DISABLED)
            except Exception:
                pass

    def _check_port_link_before_start(self, port, trex_path, pw):
        check_script = link_check_script(trex_path, int(port))
        b64_check = base64.b64encode(check_script.encode("utf-8")).decode("ascii")
        remote_check = f"/tmp/check_link_p{int(port)}.py"
        write_cmd = f"printf '%s' {validators.quote_remote(b64_check)} | base64 -d > {validators.quote_remote(remote_check)}"
        self._safe_ssh_exec(write_cmd, password=pw)
        run_cmd = f"sudo -S python3 {validators.quote_remote(remote_check)}"
        out, err = self._safe_ssh_exec(run_cmd, password=pw, timeout=15)

        result_line = ""
        for line in reversed(out.splitlines()):
            if "{" in line and "}" in line:
                result_line = line[line.find("{"): line.rfind("}") + 1]
                break
        if not result_line:
            return False, f"링크 체크 결과 파싱 실패: {err[:80]}"

        try:
            res = json.loads(result_line)
        except Exception:
            return False, f"링크 체크 JSON 오류: {result_line[:120]}"

        if not res.get("ok"):
            return False, f"Port {int(port)} 링크 체크 실패: {str(res.get('error', 'unknown'))[:120]}"
        link_up = bool(res.get("link_up"))
        speed = str(res.get("speed", "")).strip()
        status = str(res.get("status", "")).upper()

        # 일부 TRex/NIC 조합에서 link_up=False로 오탐이 나와도 speed/status는 정상으로 보고되는 경우가 있다.
        # speed가 존재하거나 상태가 IDLE/ACTIVE 계열이면 UP으로 간주한다.
        has_speed = speed not in ("", "-", "0", "0.0", "None", "null")
        status_looks_up = any(k in status for k in ["IDLE", "ACTIVE", "TRANSMIT", "UP"])
        effective_up = link_up or has_speed or status_looks_up

        if not effective_up:
            return False, f"Port {int(port)} 링크 DOWN (speed={speed or '-'}, status={status or '-'})"
        if not link_up and effective_up:
            return True, f"Port {int(port)} 링크 UP(오탐 보정) (speed={speed or '-'}, status={status or '-'})"
        return True, f"Port {int(port)} 링크 UP 확인 (speed={speed or '-'}, status={status or '-'})"

    def _update_fping_text(self, text):
        # 하위 호환: 기존 ping/fping 출력 경로는 모니터 상태로 합류
        line = str(text)
        self._update_status_text(line)
        if self.txt_ping_window is not None:
            try:
                self.txt_ping_window.config(state=tk.NORMAL)
                self.txt_ping_window.insert(tk.END, line + "\n")
                self.txt_ping_window.see(tk.END)
                line_cnt = int(float(self.txt_ping_window.index("end-1c").split(".")[0]))
                if line_cnt > FPING_MAX_LINES:
                    self.txt_ping_window.delete("1.0", f"{line_cnt - FPING_MAX_LINES + 1}.0")
                self.txt_ping_window.config(state=tk.DISABLED)
            except Exception:
                pass

    def _close_ping_window(self):
        if self.ping_window is None:
            return
        try:
            self.ping_window.destroy()
        except Exception:
            pass
        self.ping_window = None
        self.txt_ping_window = None

    def open_ping_test_window(self):
        if self.ping_window is not None and self.ping_window.winfo_exists():
            self.ping_window.lift()
            self.ping_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("Ping Test Console")
        win.geometry("900x520")
        win.minsize(760, 440)
        win.transient(self.root)
        self.ping_window = win
        win.protocol("WM_DELETE_WINDOW", self._close_ping_window)

        top = ttk.LabelFrame(win, text="접속 및 대상 설정", padding=10)
        top.pack(fill=tk.X, padx=10, pady=(10, 6))

        ttk.Label(top, text="Target IP").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(top, width=22, textvariable=self.var_fping_target).grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(top, text="Interval(sec)").grid(row=0, column=2, sticky="w", padx=(14, 6), pady=3)
        ttk.Entry(top, width=10, textvariable=self.var_fping_interval).grid(row=0, column=3, sticky="w", pady=3)

        ttk.Label(top, text="Payload").grid(row=0, column=4, sticky="w", padx=(14, 6), pady=3)
        ttk.Entry(top, width=10, textvariable=self.var_fping_size).grid(row=0, column=5, sticky="w", pady=3)

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=10, pady=6)

        ttk.Button(btns, text="서버 접속/재접속", command=self.connect_server).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Ping Start", command=self.start_reachability_monitor).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Ping Stop", command=self.stop_reachability_monitor).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="닫기", command=self._close_ping_window).pack(side=tk.RIGHT)

        output_frame = ttk.LabelFrame(win, text="Ping Output", padding=8)
        output_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.txt_ping_window = scrolledtext.ScrolledText(
            output_frame,
            bg="#0f111a",
            fg="#9be9a8",
            font=("Consolas", 11),
            height=16,
        )
        self.txt_ping_window.pack(fill=tk.BOTH, expand=True)
        self.txt_ping_window.config(state=tk.DISABLED)

        self._update_fping_text("Ping Test 창이 열렸습니다. 서버 접속 후 Ping Start를 누르세요.")

    def _refresh_pcap_list(self):
        if not self.ssh_client:
            if not self._reconnect_main_ssh_client():
                self._update_status_text("PCAP 조회 실패: SSH 미연결 상태입니다.")
                return

        sftp = None
        try:
            sftp = self.ssh_client.open_sftp()
            pcap_dir = validators.validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path")

            try:
                sftp.chdir(pcap_dir)
                files = [f for f in sftp.listdir() if f.lower().endswith(".pcap")]
                self.list_pcap_files.delete(0, tk.END)
                for f in sorted(files, reverse=True):
                    self.list_pcap_files.insert(tk.END, f)
                sorted_files = sorted(files, reverse=True)
                for p in [0, 1]:
                    combo = self.combo_pcap_by_port.get(p)
                    if combo is None:
                        continue
                    combo["values"] = sorted_files
                    if sorted_files and not combo.get().strip():
                        combo.set(sorted_files[0])
                for port in [0, 1]:
                    self.auto_order_by_port[port] = [n for n in self.auto_order_by_port.get(port, []) if n in sorted_files]
            except IOError:
                pw = self.ent_ssh_pw.get()
                mkdir_cmd = f"sudo -S mkdir -p {validators.quote_remote(pcap_dir)}"
                self._safe_ssh_exec(mkdir_cmd, password=pw)
                self.list_pcap_files.delete(0, tk.END)
                self._update_status_text(f"PCAP 경로가 없어 생성했습니다: {pcap_dir}")

        except Exception as e:
            self._update_status_text(f"PCAP 조회 오류: {str(e)[:160]}")
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass

    def delete_pcap(self):
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 서버를 연결해 주세요.")
            return

        selections = self.list_pcap_files.curselection()
        if not selections:
            messagebox.showwarning("경고", "삭제할 PCAP 파일을 선택해 주세요.\n(다중 선택: Ctrl + 클릭 또는 Shift + 클릭)")
            return

        selected_files = [self.list_pcap_files.get(i) for i in selections]

        if messagebox.askyesno("삭제 확인", f"선택한 {len(selected_files)}개의 파일을 서버에서 완전히 삭제하시겠습니까?"):
            pw = self.ent_ssh_pw.get()
            base_dir = validators.validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path")

            success_count = 0
            for f in selected_files:
                try:
                    safe_file = validators.sanitize_remote_filename(f)
                    target_file = posixpath.join(base_dir, safe_file)
                    cmd_list = ["sudo", "-S", "rm", "-f", target_file]
                    safe_cmd = " ".join(shlex.quote(arg) for arg in cmd_list)
                    _, _, st = self._safe_ssh_exec_exit(safe_cmd, password=pw)
                    if st == 0:
                        success_count += 1
                except Exception:
                    pass

            messagebox.showinfo("삭제 완료", f"총 {success_count}개의 파일이 삭제되었습니다.")
            self._refresh_pcap_list()

    def _update_test_description(self, event):
        atype = self.combo_attack.get()
        desc = "■ O-RU 검증 목적 및 파라미터 설정 가이드\n\n"

        if "U-Plane" in atype:
            desc += "본 항목은 설정에 따라 상이한 물리적/논리적 부하 환경을 조성합니다.\n\n"
            desc += "① 대역폭 과부하 시험 (Bandwidth Depletion)\n"
            desc += "   - 목적: O-RU 수신 버퍼 한계 점검 및 광 트랜시버(SFP) 발열 특성 검증\n"
            desc += "   - 설정: Size = 1500, Rate = 24.0 (Gbps)\n\n"
            desc += "② 패킷 처리율(PPS) 과부하 시험 (Interrupt Storm)\n"
            desc += "   - 목적: 대량의 인터럽트를 발생시켜 CPU 연산 한계 및 시스템 로직 행(Hang) 검증\n"
            desc += "   - 설정: Size = 64, Rate = 18.0 (Gbps)"
        elif "GTP" in atype:
            desc += "[비인가 프로토콜 예외 처리(Filtering) 검증]\n\n"
            desc += "O-RU 프론트홀 규격외 프로토콜(GTP-U/UDP) 패킷을 와이어 스피드로 인가합니다.\n\n"
            desc += "   - 목적: O-RU 하드웨어/소프트웨어 스택의 비인가 패킷 Drop 처리 성능 확인\n"
            desc += "   - 불합격 기준: 예외 패킷 처리 중 리소스 점유율 상승 및 시스템 다운 발생\n"
            desc += "   - 설정: Size = 64~256, Rate = 24.0 (Gbps)"
        elif "PRACH" in atype:
            desc += "[무선(RF) 자원 스케줄링 고갈 검증]\n\n"
            desc += "O-DU로 위장하여 다수의 가상 단말 접속 허가(Section Type 3) 메시지를 전송합니다.\n\n"
            desc += "   - 목적: O-RU 내부 FPGA/DSP의 무선 수신 대기열 메모리 마비 유도\n"
            desc += "   - 설정: O-RU MAC 정상, Size = 64"
        elif "C-Plane" in atype:
            desc += "정상 규격의 제어 메시지(Section Type 1)를 대량 전송하여 O-RU 제어 평면 부하를 유도합니다.\n"
            desc += "L2 필터링 차단을 방지하기 위해 O-RU MAC/VLAN 정보는 정상 규격으로 인가하십시오."
        elif "PTP" in atype:
            desc += "PTP 시간 동기화 메시지를 대량 전송하여 O-RU의 Clock Synchronization 로직을 교란합니다.\n"
            desc += "다수의 Master Clock 인가를 위해 Mutation 옵션을 병행할 수 있습니다."
        elif "NETCONF" in atype:
            desc += "관리 평면(M-Plane)에 대량의 TCP 세션 연결을 요청하여 O-RU 제어 리소스를 고갈시킵니다.\n"
            desc += "O-RU IP를 정확히 기입하고 Port를 830으로 일치시키십시오.\n"
            desc += "- TCP SYN-ACK only: Wireshark Conversation Completeness에서 SYN-ACK만 1,\n"
            desc += "  나머지(RST/FIN/Data/ACK/SYN)는 0이 되도록 SYN+ACK(Len=0) 패킷을 생성합니다."
        elif "TCP SYN" in atype:
            desc += "[TCP SYN Flood / Half-Open 세션 고갈 검증]\n\n"
            desc += "실제 TCP SYN과 동일한 구조의 패킷을 대량 전송합니다.\n"
            desc += "  - Flags: SYN (또는 SYN-ACK only 옵션)\n"
            desc += "  - Window: 14600\n"
            desc += "  - Options: MSS=1460, SACK permitted, Timestamp, NOP, Window scale=4\n"
            desc += "  - TCP payload 없음 (Len=0), sport/seq는 패킷마다 랜덤\n\n"
            desc += "대상 IP/Port를 DUT 서비스 포트에 맞게 설정하십시오.\n"
            desc += "권장 Packet Size: 74 (Ether+IP+TCP options, Fixed 모드 참고용)."

        desc += "\n\n[추가 옵션 가이드]\n"
        desc += "- Standard Random: 64~1500 바이트 범위를 3등분하여 랜덤 크기 생성\n"
        desc += "- Jumbo Random: 64~9000 바이트 범위를 3등분하여 랜덤 크기 생성\n"
        desc += "- Invalid Length Field: 캡처 프레임 크기는 그대로 두고,\n"
        desc += "  IP/UDP/eCPRI 헤더의 length 값만 실제와 다르게 변조합니다.\n"
        desc += "  (프레임 길이 자체를 바꾸려면 Packet Size Mode를 Random으로 설정)\n"
        desc += "- Mutation / Randomization 옵션은 프로토콜 및 파서 견고성 검증에 사용됩니다.\n"
        desc += "- Jumbo Random 사용 시 DUT/TRex/NIC의 MTU 설정을 사전에 확인하십시오."

        self.txt_desc.config(state=tk.NORMAL)
        self.txt_desc.delete(1.0, tk.END)
        self.txt_desc.insert(tk.END, desc)
        self.txt_desc.config(state=tk.DISABLED)

        l2_attacks = ["U-Plane", "C-Plane", "PRACH", "PTP"]
        is_l2 = any(k in atype for k in l2_attacks)

        self.ent_src_ip.config(state=tk.NORMAL)
        self.ent_dst_ip.config(state=tk.NORMAL)
        self.ent_dst_port.config(state=tk.NORMAL)

        if is_l2:
            self.ent_src_ip.config(state="disabled")
            self.ent_dst_ip.config(state="disabled")
            self.ent_dst_port.config(state="disabled")
        else:
            if "NETCONF" in atype:
                self.ent_dst_port.delete(0, tk.END)
                self.ent_dst_port.insert(0, "830")
            elif "TCP SYN" in atype:
                # 예시 SYN 구조 기준으로 일반 서비스 포트/크기 기본값
                cur_port = self.ent_dst_port.get().strip()
                if cur_port in ("", "830", "2152"):
                    self.ent_dst_port.delete(0, tk.END)
                    self.ent_dst_port.insert(0, "80")
                if self.combo_pkt_mode.get().strip() == "Fixed":
                    cur_size = self.ent_pkt_size.get().strip()
                    if cur_size in ("", "64"):
                        self.ent_pkt_size.config(state=tk.NORMAL)
                        self.ent_pkt_size.delete(0, tk.END)
                        self.ent_pkt_size.insert(0, "74")
            elif "GTP" in atype:
                self.ent_dst_port.delete(0, tk.END)
                self.ent_dst_port.insert(0, "2152")

        safe_name = re.sub(r"[^a-zA-Z0-9]", "_", atype)
        safe_name = re.sub(r"_+", "_", safe_name).strip("_") + ".pcap"

        self.ent_pcap_name.delete(0, tk.END)
        self.ent_pcap_name.insert(0, safe_name)

        atype_upper = atype.upper()
        is_ecpri = any(k in atype_upper for k in ["U-PLANE", "C-PLANE", "PRACH"])
        is_l3l4 = any(k in atype_upper for k in ["NETCONF", "GTP", "TCP SYN"])

        if is_ecpri:
            self.chk_malformed_ecpri.config(state=tk.NORMAL if self.var_mutation_enable.get() else tk.DISABLED)
            self.chk_invalid_length.config(state=tk.NORMAL if self.var_mutation_enable.get() else tk.DISABLED)
            self.chk_rand_l4_port.config(state=tk.DISABLED)
        elif is_l3l4:
            self.chk_rand_l4_port.config(state=tk.NORMAL if self.var_mutation_enable.get() else tk.DISABLED)
            self.chk_malformed_ecpri.config(state=tk.DISABLED)
            self.chk_invalid_length.config(state=tk.NORMAL if self.var_mutation_enable.get() else tk.DISABLED)
        else:
            self.chk_malformed_ecpri.config(state=tk.DISABLED)
            self.chk_rand_l4_port.config(state=tk.DISABLED)
            self.chk_invalid_length.config(state=tk.NORMAL if self.var_mutation_enable.get() else tk.DISABLED)

        self._update_tcp_synack_option_state()
        self._calculate_pps(None)

    def _calculate_pps(self, event=None):
        try:
            rate_str = self.var_line_rate.get().strip()
            mode = self.combo_pkt_mode.get().strip()

            if not rate_str:
                raise ValueError

            rate_gbps = float(rate_str)
            if rate_gbps < 0:
                raise ValueError

            estimate_mode = False
            if mode == "Fixed":
                pkt_str = self.ent_pkt_size.get().strip()
                if not pkt_str:
                    raise ValueError
                pkt_size = int(pkt_str)
                if pkt_size <= 0:
                    raise ValueError
            elif mode == "Standard Random":
                pkt_size = (64 + 1500) / 2
                estimate_mode = True
            elif mode == "Jumbo Random":
                pkt_size = (64 + 9000) / 2
                estimate_mode = True
            else:
                raise ValueError

            frame_size = pkt_size + 20
            max_l2_rate = 25.0 * (pkt_size / frame_size)
            pps = (rate_gbps * 1_000_000_000) / (pkt_size * 8)
            l1_gbps = (pps * frame_size * 8) / 1_000_000_000
            usage = (l1_gbps / 25.0) * 100
            pkts_per_ms = pps / 1000

            suffix = " (Avg Estimate)" if estimate_mode else ""

            self.lbl_max_limit.config(text=f"L2 Line Rate Limit{suffix}: {max_l2_rate:.2f} Gbps")
            self.lbl_pps_calc.config(text=f"Packets/sec{suffix}: {pps:,.0f} pps (1ms당 약 {pkts_per_ms:,.0f} 개)")
            self.lbl_gbps_calc.config(text=f"Expected L1 Throughput{suffix}: {l1_gbps:.2f} Gbps")
            self.lbl_line_calc.config(text=f"L1 Line Rate Usage{suffix}: {usage:.1f}% (of 25G)")

            if estimate_mode:
                self.lbl_estimate_info.config(text="랜덤 크기 모드 활성화 - 위 값은 평균 패킷 크기 기반 추정치입니다.")
            else:
                self.lbl_estimate_info.config(text="")

            if usage > 100:
                self.lbl_line_calc.config(foreground="red")
            else:
                self.lbl_line_calc.config(foreground="blue")

        except ValueError:
            self.lbl_max_limit.config(text="L2 Line Rate Limit: -")
            self.lbl_pps_calc.config(text="Packets/sec: -")
            self.lbl_gbps_calc.config(text="Expected L1 Throughput: -")
            self.lbl_line_calc.config(text="L1 Line Rate Usage: -")
            self.lbl_estimate_info.config(text="")

    def build_pcap(self):
        if self.pcap_build_thread is not None and self.pcap_build_thread.is_alive():
            messagebox.showinfo("안내", "PCAP 생성이 이미 진행 중입니다.")
            return
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 1번 탭에서 서버를 연결해 주세요.")
            return

        if not self._validate_inputs(step=1):
            return

        self.pcap_build_stop.clear()
        self.btn_build_pcap.config(state=tk.DISABLED)
        self.btn_stop_build_pcap.config(state=tk.NORMAL)
        self.pcap_build_thread = threading.Thread(target=self._build_pcap_bg, daemon=True)
        self.pcap_build_thread.start()

    def _kill_remote_pcap_builder_best_effort(self):
        # ssh_lock에 묶이지 않도록 독립 세션으로 종료 시도한다.
        try:
            ip = self.ent_server_ip.get().strip()
            user = self.ent_ssh_user.get().strip()
            pw = self.ent_ssh_pw.get()
            if not (ip and user):
                return
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(ip, username=user, password=pw, timeout=SSH_CONNECT_TIMEOUT)
            kill_cmd = "pkill -f '/tmp/oran_builder.py' || true"
            stdin, stdout, stderr = cli.exec_command(f"sudo -S sh -c {validators.quote_remote(kill_cmd)}", get_pty=True, timeout=8)
            if pw:
                stdin.write(pw + "\n")
                stdin.flush()
            try:
                stdout.channel.recv_exit_status()
            except Exception:
                pass
            try:
                cli.close()
            except Exception:
                pass
        except Exception:
            pass

    def stop_build_pcap(self):
        # 생성 중단 요청: 플래그 + 원격 빌더 프로세스 종료 시도
        self.pcap_build_stop.set()
        self._update_status_text("PCAP 생성 중단 요청")
        self._record_event("PCAP 생성 중단 요청")
        self.btn_stop_build_pcap.config(state=tk.DISABLED)
        threading.Thread(target=self._kill_remote_pcap_builder_best_effort, daemon=True).start()

    def _build_pcap_bg(self):
        try:
            pkt_mode = self.combo_pkt_mode.get().strip()
            def _build_size_pattern(mode, fixed_size=""):
                mode = str(mode or "").strip()
                if mode == "Standard Random":
                    return [64, 423, 782, 1141, 1500]
                if mode == "Jumbo Random":
                    return [64, 2298, 4532, 6766, 9000]
                try:
                    val = int(str(fixed_size).strip() or "64")
                except Exception:
                    val = 64
                return [max(64, val)]

            common = {
                "attack_type": self.combo_attack.get(),
                "src_mac": self.ent_src_mac.get().strip(),
                "dst_mac": self.ent_dst_mac.get().strip(),
                "src_ip": self.ent_src_ip.get().strip(),
                "dst_ip": self.ent_dst_ip.get().strip(),
                "dst_port": self.ent_dst_port.get().strip(),
                "vlan_id": self.ent_vlan_id.get().strip(),
                "spoofing": self.var_mutation_enable.get(),
                "mutation_enable": self.var_mutation_enable.get(),
                "rand_mac": self.var_rand_mac.get(),
                "rand_ip": self.var_rand_ip.get(),
                "rand_vlan": self.var_rand_vlan.get(),
                "rand_ethertype": self.var_rand_ethertype.get(),
                "malformed_ecpri": self.var_malformed_ecpri.get(),
                "invalid_length": self.var_invalid_length.get(),
                "rand_l4_port": self.var_rand_l4_port.get(),
                "tcp_synack_only": self.var_tcp_synack_only.get(),
                "pcap_ms": self.ent_pcap_ms.get().strip(),
                "pcap_path": validators.validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path"),
            }

            pw = self.ent_ssh_pw.get()
            builder_script = PCAP_BUILDER_SCRIPT
            b64_builder = base64.b64encode(builder_script.encode("utf-8")).decode("ascii")
            remote_builder = "/tmp/oran_builder.py"

            write_cmd = f"printf '%s' {validators.quote_remote(b64_builder)} | base64 -d > {validators.quote_remote(remote_builder)}"
            write_err = ""
            write_st = -1
            for w_try in range(1, 6):
                if self.pcap_build_stop.is_set():
                    self._safe_ui(messagebox.showinfo, "중단", "PCAP 생성이 중단되었습니다.")
                    return
                if not self.ssh_client and not self._reconnect_main_ssh_client():
                    write_err = "SSH 재연결 실패"
                    write_st = -1
                else:
                    _, write_err, write_st = self._safe_ssh_exec_exit(write_cmd, password=pw, timeout=60)

                if write_st == 0:
                    break

                err_hint = (write_err or "")
                reconnect_needed = any(
                    token in err_hint for token in ["Socket is closed", "SSH session not active", "Channel closed", "EOF", "timed out"]
                )
                if reconnect_needed:
                    self._record_event(f"PCAP 빌더 write 재시도({w_try}/5): 소켓 재연결 시도")
                    self._reconnect_main_ssh_client()
                if w_try < 5:
                    time.sleep(min(2.0, 0.4 * w_try))
            if write_st != 0:
                self._safe_ui(messagebox.showerror, "생성 오류", f"원격 빌더 쓰기 실패:\n{write_err[:200]}")
                return

            check_out, check_err = self._safe_ssh_exec(
                f"test -f {validators.quote_remote(remote_builder)} && echo OK || echo FAIL",
                password=pw
            )
            if "Socket is closed" in (check_err or ""):
                if self._reconnect_main_ssh_client():
                    self._record_event("PCAP 빌더 check 단계: SSH 재연결 후 재시도")
                    check_out, check_err = self._safe_ssh_exec(
                        f"test -f {validators.quote_remote(remote_builder)} && echo OK || echo FAIL",
                        password=pw
                    )
            if "OK" not in check_out:
                self._safe_ui(messagebox.showerror, "생성 오류", f"원격 빌더 스크립트 생성에 실패했습니다.\n{check_err[:200]}")
                return

            def run_builder(one_cfg):
                if self.pcap_build_stop.is_set():
                    return {"status": "cancelled", "message": "사용자 요청으로 중단됨"}
                def _estimate_pkt_size_for_timeout(cfg):
                    mode = str(cfg.get("pkt_mode", "Fixed")).strip()
                    if mode == "Standard Random":
                        return (64 + 1500) / 2
                    if mode == "Jumbo Random":
                        return (64 + 9000) / 2
                    try:
                        return max(64, float(cfg.get("pkt_size", 64)))
                    except Exception:
                        return 64.0

                def _calc_builder_timeout(cfg):
                    # 대용량/고속 PCAP 생성 시 30초 기본값으로는 간헐 타임아웃이 발생할 수 있다.
                    try:
                        rate_gbps = float(cfg.get("rate", "10") or 10)
                        pcap_ms = float(cfg.get("pcap_ms", "1") or 1)
                    except Exception:
                        return 90
                    est_pkt = _estimate_pkt_size_for_timeout(cfg)
                    bytes_per_ms = (rate_gbps * 1_000_000_000 / 8) / 1000
                    est_num_pkts = int((bytes_per_ms / max(est_pkt, 64.0)) * pcap_ms)
                    est_num_pkts = max(est_num_pkts, 1)
                    timeout_sec = 30 + int(est_num_pkts / 120_000)
                    return max(60, min(600, timeout_sec))

                b64_config = base64.b64encode(json.dumps(one_cfg).encode("utf-8")).decode("ascii")
                run_cmd = f"sudo -S python3 {validators.quote_remote(remote_builder)} {validators.quote_remote(b64_config)}"
                last_msg = ""
                timeout_sec = _calc_builder_timeout(one_cfg)
                for attempt in range(1, 6):
                    if self.pcap_build_stop.is_set():
                        return {"status": "cancelled", "message": "사용자 요청으로 중단됨"}
                    # run 직전에도 세션 상태를 확인해 소켓 단절을 조기에 회복한다.
                    if not self.ssh_client and not self._reconnect_main_ssh_client():
                        last_msg = "SSH 재연결 실패"
                        if attempt < 5:
                            time.sleep(0.5)
                            continue
                        return {"status": "error", "message": last_msg}
                    out, err, st = self._safe_ssh_exec_exit(run_cmd, password=pw, timeout=timeout_sec)
                    merged = f"{out}\n{err}".strip()
                    clean_json = ""
                    for line in reversed(merged.splitlines()):
                        if "{" in line and "}" in line:
                            clean_json = line[line.find("{"):line.rfind("}") + 1]
                            break
                    if clean_json:
                        try:
                            return json.loads(clean_json)
                        except Exception as parse_err:
                            last_msg = f"JSON 파싱 오류: {str(parse_err)[:120]} / raw={clean_json[:180]}"
                    else:
                        last_msg = merged[:240] if merged else f"응답 없음(exit={st})"

                    retry_hint = (err or "") + "\n" + (out or "")
                    reconnect_needed = any(
                        token in retry_hint
                        for token in ["Socket is closed", "timed out", "EOF", "Channel closed", "SSH session not active"]
                    )
                    if reconnect_needed and self._reconnect_main_ssh_client():
                        self._record_event(
                            f"PCAP 빌더 재시도({attempt}/5): SSH 재연결 완료 timeout={timeout_sec}s"
                        )
                        continue
                    if attempt < 5:
                        backoff = min(2.5, 0.5 * attempt)
                        self._record_event(
                            f"PCAP 빌더 재시도({attempt}/5): {last_msg[:120]} (sleep={backoff}s)"
                        )
                        time.sleep(backoff)
                return {"status": "error", "message": f"PCAP 빌더 실행 실패(5회 재시도): {last_msg}"}

            use_presets = hasattr(self, "var_use_presets") and self.var_use_presets.get()
            if use_presets:
                dst_mac = self.ent_dst_mac.get().strip().replace(":", "").replace("-", "").lower() or "oru"
                selected_defs = [p for p in self.preset_defs if self.preset_vars[p["key"]].get()]
                created = []
                for p in selected_defs:
                    if self.pcap_build_stop.is_set():
                        self._safe_ui(messagebox.showinfo, "중단", "PCAP 생성이 중단되었습니다.")
                        return
                    one = dict(common)
                    # 프리셋별 필드 오버라이드(attack_type/vlan/port/mutation 등)
                    def _resolve_override(v, *, key_name=""):
                        if v == "__UNTAG__":
                            return ""
                        if v == "__USE_GUI_VLAN__":
                            base_vlan = str(common.get("vlan_id", "") or "").strip()
                            return base_vlan if base_vlan else "100"
                        return v

                    override_keys = [
                        "attack_type",
                        "dst_port",
                        "vlan_id",
                        "mutation_enable",
                        "rand_mac",
                        "rand_ip",
                        "rand_vlan",
                        "rand_ethertype",
                        "malformed_ecpri",
                        "invalid_length",
                        "rand_l4_port",
                    ]
                    for k in override_keys:
                        if k in p:
                            one[k] = _resolve_override(p.get(k), key_name=k)

                    # 프리셋별 실제 attack_type 기준으로 prefix 산정
                    atype_upper = str(one.get("attack_type", "")).upper()
                    if "U-PLANE" in atype_upper:
                        attack_prefix = "uplane"
                    elif "C-PLANE" in atype_upper:
                        attack_prefix = "cplane"
                    elif "PRACH" in atype_upper:
                        attack_prefix = "prach"
                    elif "GTP" in atype_upper:
                        attack_prefix = "udp"
                    elif "TCP SYN" in atype_upper:
                        attack_prefix = "tcpsyn"
                    else:
                        # NETCONF/TCP 계열 기본값
                        attack_prefix = "tcp"

                    one["pkt_mode"] = p["pkt_mode"]
                    one["pkt_size"] = p["pkt_size"] if p["pkt_mode"] == "Fixed" else ""
                    one["size_pattern"] = _build_size_pattern(one["pkt_mode"], one["pkt_size"])
                    one["rate"] = self.preset_rate_vars[p["key"]].get().strip()
                    # 파일명은 key(고정 10G 등) 대신 실제 입력 rate를 반영한다.
                    def _mode_tag(pkt_mode: str, pkt_size: str) -> str:
                        m = str(pkt_mode or "").strip()
                        if m == "Fixed":
                            return f"fixed{str(pkt_size or '64').strip()}"
                        if m == "Standard Random":
                            return "std_random"
                        if m == "Jumbo Random":
                            return "jumbo_random"
                        return re.sub(r"\\s+", "_", m.lower()) or "mode"

                    rate_tag = str(one["rate"] or "").strip().lower()
                    rate_tag = rate_tag.replace("gbps", "").replace("g", "").strip()
                    # 22.0 -> 22, 22.5 -> 22p5
                    if rate_tag.endswith(".0"):
                        rate_tag = rate_tag[:-2]
                    rate_tag = rate_tag.replace(".", "p")
                    tag = str(p.get("file_tag", "")).strip() or _mode_tag(one["pkt_mode"], one["pkt_size"])
                    # a/b 같은 임의 구분자 대신 MIN/MAX 의미를 파일명에 넣는다.
                    tier = str(p.get("tier", "") or "").strip().lower()
                    if not tier:
                        label_lower = str(p.get("label", "")).lower()
                        tier = "min" if "(min)" in label_lower else ("max" if "(max)" in label_lower else "mid")
                    tier_segment = "" if tier == "max" else f"_{tier}"
                    one["pcap_name"] = validators.sanitize_remote_filename(
                        f"{dst_mac}_{attack_prefix}_{tag}{tier_segment}_{rate_tag}g.pcap"
                    )
                    res = run_builder(one)
                    if res.get("status") == "cancelled":
                        self._safe_ui(messagebox.showinfo, "중단", "PCAP 생성이 중단되었습니다.")
                        return
                    if res.get("status") != "success":
                        self._safe_ui(messagebox.showerror, "생성 오류", f"[{p['label']}] {res.get('message')}")
                        return
                    created.append(f"{p['label']} -> {res.get('file')}")
                self._safe_ui(
                    messagebox.showinfo,
                    "PCAP 일괄 생성 완료",
                    "선택한 프리셋 PCAP 생성이 완료되었습니다.\n\n" + "\n".join(created),
                )
                self._safe_ui(self._refresh_pcap_list)
            else:
                one = dict(common)
                one["pkt_mode"] = pkt_mode
                one["pkt_size"] = self.ent_pkt_size.get().strip() if pkt_mode == "Fixed" else ""
                one["size_pattern"] = _build_size_pattern(one["pkt_mode"], one["pkt_size"])
                one["rate"] = self.var_line_rate.get().strip()
                # 수동 생성도 PRESET과 동일한 파일명 규칙을 사용(사용자 지정 이름이 있으면 유지)
                manual_name = (self.ent_pcap_name.get() or "").strip()
                # 사용자가 "직접 이름을 의도적으로 고정"한 경우만 유지하고,
                # 기본/자동으로 생성된 형태(이전 규칙 포함)는 현재 설정 기준으로 다시 생성한다.
                auto_name_regex = re.compile(
                    r"^[0-9a-f]{4,}_(uplane|cplane|prach|udp|tcp|tcpsyn)_(fixed\d+|std_random|jumbo_random|[a-z0-9_]+?)(?:_(manual|min|mid))?_[0-9p]+g\.pcap$",
                    re.IGNORECASE,
                )
                default_like = (
                    (not manual_name)
                    or (manual_name.lower() in ["attack_target.pcap", "attack_target"])
                    or bool(auto_name_regex.match(manual_name))
                )

                if default_like:
                    dst_mac = self.ent_dst_mac.get().strip().replace(":", "").replace("-", "").lower() or "oru"
                    atype_upper = self.combo_attack.get().upper()
                    if "U-PLANE" in atype_upper:
                        attack_prefix = "uplane"
                    elif "C-PLANE" in atype_upper:
                        attack_prefix = "cplane"
                    elif "PRACH" in atype_upper:
                        attack_prefix = "prach"
                    elif "GTP" in atype_upper:
                        attack_prefix = "udp"
                    elif "TCP SYN" in atype_upper:
                        attack_prefix = "tcpsyn"
                    else:
                        attack_prefix = "tcp"

                    def _mode_tag(pkt_mode2: str, pkt_size2: str) -> str:
                        m2 = str(pkt_mode2 or "").strip()
                        if m2 == "Fixed":
                            return f"fixed{str(pkt_size2 or '64').strip()}"
                        if m2 == "Standard Random":
                            return "std_random"
                        if m2 == "Jumbo Random":
                            return "jumbo_random"
                        return re.sub(r"\\s+", "_", m2.lower()) or "mode"

                    rate_tag = str(one["rate"] or "").strip().lower()
                    rate_tag = rate_tag.replace("gbps", "").replace("g", "").strip()
                    # 22.0 -> 22, 22.5 -> 22p5
                    if rate_tag.endswith(".0"):
                        rate_tag = rate_tag[:-2]
                    rate_tag = rate_tag.replace(".", "p")
                    tag = _mode_tag(one["pkt_mode"], one["pkt_size"])
                    tier = "manual"
                    gen = f"{dst_mac}_{attack_prefix}_{tag}_{tier}_{rate_tag}g.pcap"
                    manual_name = gen
                    # UI에도 반영
                    self._safe_ui(self.ent_pcap_name.delete, 0, tk.END)
                    self._safe_ui(self.ent_pcap_name.insert, 0, manual_name)

                one["pcap_name"] = validators.sanitize_remote_filename(manual_name)
                res = run_builder(one)
                if res.get("status") == "cancelled":
                    self._safe_ui(messagebox.showinfo, "중단", "PCAP 생성이 중단되었습니다.")
                    return
                if res.get("status") == "success":
                    pkts_created = res.get("count", 1)
                    used_pattern = res.get("size_pattern", one.get("size_pattern", []))
                    pattern_text = ", ".join(str(v) for v in used_pattern) if used_pattern else "-"
                    self._safe_ui(
                        messagebox.showinfo,
                        "PCAP 생성 완료",
                        f"지정된 길이의 패킷 블록 생성이 성공적으로 완료되었습니다.\n\n경로: {res.get('file')}\n생성된 총 패킷 수: {pkts_created:,} 개\n사이즈 패턴: [{pattern_text}]"
                    )
                    self._safe_ui(self._refresh_pcap_list)
                else:
                    self._safe_ui(messagebox.showerror, "생성 오류", f"패킷 생성 중 오류가 발생했습니다.\n{res.get('message')}")

        except Exception as e:
            self._safe_ui(messagebox.showerror, "예외 발생", f"실행 중 예외 발생: {str(e)}")
        finally:
            self.pcap_build_thread = None
            self.pcap_build_stop.clear()
            self._safe_ui(self.btn_build_pcap.config, state=tk.NORMAL)
            self._safe_ui(self.btn_stop_build_pcap.config, state=tk.DISABLED)

    def _set_auto_state(self, port, text, color="gray"):
        lbl = self.lbl_auto_state_by_port.get(int(port))
        if lbl is not None:
            lbl.config(text=f"Auto: {text}", foreground=color)

    def _move_listbox_up(self, lb):
        indexes = list(lb.curselection())
        if not indexes or indexes[0] == 0:
            return
        values = list(lb.get(0, tk.END))
        for i in indexes:
            values[i - 1], values[i] = values[i], values[i - 1]
        lb.delete(0, tk.END)
        for v in values:
            lb.insert(tk.END, v)
        lb.selection_clear(0, tk.END)
        for i in [idx - 1 for idx in indexes]:
            lb.selection_set(i)

    def _move_listbox_down(self, lb):
        indexes = list(lb.curselection())
        values = list(lb.get(0, tk.END))
        if not indexes or indexes[-1] >= len(values) - 1:
            return
        for i in reversed(indexes):
            values[i + 1], values[i] = values[i], values[i + 1]
        lb.delete(0, tk.END)
        for v in values:
            lb.insert(tk.END, v)
        lb.selection_clear(0, tk.END)
        for i in [idx + 1 for idx in indexes]:
            lb.selection_set(i)

    def open_auto_order_editor(self):
        if self.auto_order_editor is not None and self.auto_order_editor.winfo_exists():
            self.auto_order_editor.lift()
            return

        win = tk.Toplevel(self.root)
        win.title("Auto Run 순서 편집")
        # 좌/중앙/Port0/Port1 4영역 버튼이 잘리지 않도록 기본 폭을 넉넉히 잡는다.
        win.geometry("1160x560")
        win.minsize(1120, 520)
        win.resizable(True, True)
        self.auto_order_editor = win

        ttk.Label(win, text="왼쪽 목록에서 PCAP을 선택해 Port 0/1 순서 목록으로 추가하세요.").pack(anchor="w", padx=10, pady=(10, 4))

        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        left = ttk.LabelFrame(body, text="Available PCAP", padding=8)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_available = tk.Listbox(left, selectmode=tk.EXTENDED, exportselection=False)
        lb_available.pack(fill=tk.BOTH, expand=True)
        for name in self.list_pcap_files.get(0, tk.END):
            lb_available.insert(tk.END, name)

        center = ttk.Frame(body)
        center.pack(side=tk.LEFT, fill=tk.Y, padx=8)

        port_lists = {}
        for port in [0, 1]:
            pf = ttk.LabelFrame(body, text=f"Port {port} 실행 순서", padding=8)
            pf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0 if port == 0 else 6, 0))
            lb = tk.Listbox(pf, selectmode=tk.EXTENDED, exportselection=False)
            lb.pack(fill=tk.BOTH, expand=True)
            for name in self.auto_order_by_port.get(port, []):
                lb.insert(tk.END, name)
            port_lists[port] = lb

            ctrl = ttk.Frame(pf)
            ctrl.pack(fill=tk.X, pady=(6, 0))
            ttk.Button(ctrl, text="제거", width=8, command=lambda p=port: [port_lists[p].delete(i) for i in reversed(port_lists[p].curselection())]).pack(side=tk.LEFT, padx=2)
            ttk.Button(ctrl, text="위", width=8, command=lambda p=port: self._move_listbox_up(port_lists[p])).pack(side=tk.LEFT, padx=2)
            ttk.Button(ctrl, text="아래", width=8, command=lambda p=port: self._move_listbox_down(port_lists[p])).pack(side=tk.LEFT, padx=2)
            ttk.Button(ctrl, text="초기화", width=8, command=lambda p=port: port_lists[p].delete(0, tk.END)).pack(side=tk.LEFT, padx=2)

        def add_to_port(port):
            lb = port_lists[port]
            existing = list(lb.get(0, tk.END))
            for i in lb_available.curselection():
                name = lb_available.get(i)
                if name not in existing:
                    lb.insert(tk.END, name)
                    existing.append(name)

        ttk.Button(center, text="-> Port 0", command=lambda: add_to_port(0)).pack(fill=tk.X, pady=4)
        ttk.Button(center, text="-> Port 1", command=lambda: add_to_port(1)).pack(fill=tk.X, pady=4)

        footer = ttk.Frame(win)
        footer.pack(fill=tk.X, padx=10, pady=(0, 10))

        def save_and_close():
            self.auto_order_by_port[0] = list(port_lists[0].get(0, tk.END))
            self.auto_order_by_port[1] = list(port_lists[1].get(0, tk.END))
            self._record_event(
                f"AUTO 순서 저장 P0={len(self.auto_order_by_port[0])}개 P1={len(self.auto_order_by_port[1])}개"
            )
            win.destroy()

        ttk.Button(footer, text="저장 후 닫기", command=save_and_close).pack(side=tk.RIGHT, padx=4)
        ttk.Button(footer, text="취소", command=win.destroy).pack(side=tk.RIGHT, padx=4)

    def _get_selected_pcaps_for_auto(self, port):
        ordered = list(self.auto_order_by_port.get(int(port), []))
        if ordered:
            return ordered
        indexes = self.list_pcap_files.curselection()
        return [self.list_pcap_files.get(i) for i in indexes]

    def _infer_rate_from_pcap_name(self, pcap_name):
        name = str(pcap_name or "").lower()
        for p in self.preset_defs:
            key = str(p.get("key", "")).lower()
            if key and key in name:
                return str(p.get("rate", "")).strip()
        # 새 파일명 규칙: *_<rate>g.pcap 또는 *_<rate>gbps.pcap (rate는 22 / 22p0 등)
        m = re.search(r"_(\d+(?:p\d+)?)g(?:bps)?\.pcap$", name)
        if m:
            raw = m.group(1).replace("p", ".")
            return raw
        return ""

    def _on_auto_run_mode_changed(self):
        """
        실수 방지용 제어:
        - per-port: Auto Start 비활성, 수동 Start 활성
        - sequential-ports: 수동 Start 비활성, Auto Start 활성
        """
        mode_val = self.var_auto_run_mode.get() if hasattr(self, "var_auto_run_mode") else "per-port"
        is_seq = mode_val == "sequential-ports"
        for p in [0, 1]:
            btn_start = self.btn_start_by_port.get(p) if hasattr(self, "btn_start_by_port") else None
            btn_stop = self.btn_stop_by_port.get(p) if hasattr(self, "btn_stop_by_port") else None
            btn_auto = self.btn_auto_start_by_port.get(p) if hasattr(self, "btn_auto_start_by_port") else None
            btn_auto_stop = self.btn_auto_stop_by_port.get(p) if hasattr(self, "btn_auto_stop_by_port") else None
            if btn_start is not None:
                btn_start.config(state=tk.DISABLED if is_seq else tk.NORMAL)
            if btn_stop is not None:
                btn_stop.config(state=tk.DISABLED if is_seq else tk.NORMAL)
            if btn_auto is not None:
                btn_auto.config(state=tk.NORMAL if is_seq else tk.DISABLED)
            if btn_auto_stop is not None:
                btn_auto_stop.config(state=tk.NORMAL if is_seq else tk.DISABLED)

    def _reopen_coord_progress_window(self):
        if not getattr(self, "_coord_running", False):
            messagebox.showinfo("안내", "현재 실행 중인 순차 진행이 없어 진행상황 창을 열 수 없습니다.")
            return
        args = getattr(self, "_coord_progress_args", None)
        if not args:
            messagebox.showinfo("안내", "진행상황 정보를 복구할 수 없습니다. 순차 실행을 다시 시작해 주세요.")
            return
        self._open_coord_progress_window(*args)

    def start_auto_run_for_port(self, port):
        port = int(port)
        mode = getattr(self, "var_auto_run_mode", None)
        mode_val = mode.get() if mode is not None else "per-port"
        if mode_val != "sequential-ports":
            messagebox.showinfo("안내", "Port별(기존) 모드에서는 Auto Start를 사용할 수 없습니다. 포트 순차 모드로 전환해 주세요.")
            return
        # 순차 모드가 이미 동작 중이면 반대 포트 Start를 막고 안내한다.
        if getattr(self, "_coord_running", False):
            messagebox.showinfo("안내", "순차모드가 동작중입니다.")
            return
        return self.start_auto_run_coordinated(start_port=port, mode=mode_val)

    def _derive_port_specific_pcap(self, port: int, base_pcap_name: str) -> str:
        """
        Auto Run 목록이 한 포트 기준 파일명(예: 000a..._uplane_xxx.pcap)만 있어도,
        각 포트 콤보에서 prefix(첫 토큰)를 가져와 suffix를 결합해 포트별 파일명을 유도한다.

        추가로, 포트별로 PCAP 파일명 규칙이 소폭 다른 경우(예: ...19p0g vs ...19g) 서버에 실제 존재하는 파일을 선택한다.
        """
        try:
            base = str(base_pcap_name or "").strip()
            if "_" not in base:
                return base

            suffix = base.split("_", 1)[1]
            combo_val = (self.combo_pcap_by_port.get(int(port)).get() or "").strip()
            prefix = combo_val.split("_", 1)[0] if "_" in combo_val else ""
            if not prefix:
                return base

            primary = f"{prefix}_{suffix}"

            # 후보 확장: 동일 suffix 내에서 자주 생기는 표기 차이를 커버한다.
            candidates = []
            for cand in [primary, base]:
                if cand and cand not in candidates:
                    candidates.append(cand)

            low_suf = suffix.lower()
            # 정수형 rate 표기 차이: ..._22p0g vs ..._22g
            if low_suf.endswith("p0g.pcap"):
                alt = suffix[:-len("p0g.pcap")] + "g.pcap"
                alt_full = f"{prefix}_{alt}"
                if alt_full not in candidates:
                    candidates.append(alt_full)

            # '_max_' 세그먼트 유무 차이 (예: ..._max_19p0g vs ..._19g)
            alt_suffixes = []
            if "_max_" in suffix:
                stripped = suffix.replace("_max_", "_", 1)
                if stripped != suffix:
                    alt_suffixes.append(stripped)
            # ..._max_19p0g.pcap -> ..._19g.pcap
            m = re.match(r"^(.*)_max_(\d+)p0g\.pcap$", suffix, re.I)
            if m:
                alt_suffixes.append(f"{m.group(1)}_{m.group(2)}g.pcap")
            for alt_suf in alt_suffixes:
                alt_full = f"{prefix}_{alt_suf}"
                if alt_full not in candidates:
                    candidates.append(alt_full)

            values = list(self.combo_pcap_by_port.get(int(port))["values"] or [])
            # 리스트에 존재하면 최우선
            for cand in list(candidates):
                if cand in values:
                    return cand

            # 서버 실존 파일 우선 선택 (SSH 가능할 때만)
            try:
                base_dir = validators.validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path")
                pw = self.ent_ssh_pw.get()

                def _exists(fname: str) -> bool:
                    safe = validators.sanitize_remote_filename(fname)
                    full = posixpath.join(base_dir, safe)
                    out, _, st = self._safe_ssh_exec_exit(
                        f"test -f {validators.quote_remote(full)} && echo OK || echo FAIL",
                        password=pw,
                        timeout=6,
                    )
                    return st == 0 and "OK" in (out or "")

                for cand in candidates:
                    if _exists(cand):
                        return cand
            except Exception:
                pass

            return primary
        except Exception:
            return str(base_pcap_name or "").strip()

    def start_auto_run_coordinated(self, start_port: int, mode: str):
        """
        coordinated Auto Run:
        - sequential-ports: 먼저 누른 포트 시험 종료 후 다음 포트 시험 진행 (MAX rate 환경 대응).
          항목당 흐름: stop → start → 항목 절체 시간만큼 송출 → stop → Guard Time만큼 대기 → 다음 항목.
        """
        start_port = int(start_port)
        first_port = int(start_port)
        second_port = 1 - first_port
        first_pcaps = self._get_selected_pcaps_for_auto(first_port)
        second_pcaps = self._get_selected_pcaps_for_auto(second_port)
        if not first_pcaps or not second_pcaps:
            messagebox.showwarning("선택 필요", "순차 모드는 Port 0/1 모두 Auto Run용 PCAP을 1개 이상 선택해 주세요.")
            return

        try:
            item_dur_min = float(self.ent_auto_item_duration_min_by_port[start_port].get().strip())
            guard_between_min = float(self.ent_auto_guard_between_min_by_port[start_port].get().strip())
            if item_dur_min < 0 or guard_between_min < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "입력 오류",
                f"Port {start_port} 항목 절체 시간·Guard Time(min)은 0 이상의 숫자여야 합니다.",
            )
            return

        # 기존 per-port 쓰레드가 돌고 있으면 중복 실행 방지
        for p in [0, 1]:
            th = self.auto_run_thread.get(p)
            if th is not None and th.is_alive():
                messagebox.showinfo("안내", "순차모드가 동작중입니다.")
                return

        # stop flag는 두 포트 모두 공유해서 사용
        for p in [0, 1]:
            self.auto_run_stop[p].clear()
            self.auto_next_index[p] = 0
            self._set_auto_state(p, "실행중", "green")

        # coordinated run state (for UI / stop)
        self._coord_running = True
        self._coord_stop = threading.Event()
        self._coord_stop.clear()
        self._coord_start_port = start_port
        self._coord_other_port = second_port
        # 순차 모드는 "시작 포트 전체 + 반대 포트 전체"를 1사이클로 본다.
        self._coord_total_items = len(first_pcaps) + len(second_pcaps)
        self._coord_item_index = 0
        self._coord_phase = "준비"
        self._coord_started_at = time.time()
        self._coord_do_loop = self._var_auto_loop_shared.get() if hasattr(self, "_var_auto_loop_shared") else True

        self._record_event(
            f"COORD AUTO RUN 시작 mode={mode} 항목절체_min={item_dur_min} guard간격_min={guard_between_min} "
            f"files_p0={len(first_pcaps)} files_p1={len(second_pcaps)}"
        )

        self._coord_progress_args = (mode, item_dur_min, guard_between_min, start_port)
        self._coord_progress_owner = "coord"
        self._safe_ui(
            self._open_coord_progress_window, mode, item_dur_min, guard_between_min, start_port
        )

        def _coord_loop():
            item_wait_sec = max(0.0, float(item_dur_min) * 60.0)
            between_wait_sec = max(0.0, float(guard_between_min) * 60.0)
            do_loop = self._var_auto_loop_shared.get() if hasattr(self, "_var_auto_loop_shared") else True
            try:
                while True:
                    if self._coord_stop.is_set():
                        break
                    first_port = int(start_port)
                    second_port = 1 - int(start_port)
                    phase_plan = [(first_port, "first", first_pcaps), (second_port, "second", second_pcaps)]
                    prog_idx = 0

                    # 사용자 요청 동작: P0(또는 시작포트) 전체 항목 완료 후, 반대 포트 전체 항목 진행
                    for phase_port, phase_name, phase_pcaps in phase_plan:
                        if self._coord_stop.is_set():
                            break
                        if phase_name == "second":
                            self._record_event(f"COORD AUTO P{first_port} 전체 항목 완료 -> P{second_port} 진행")

                        for idx, base_name in enumerate(phase_pcaps, 1):
                            if self._coord_stop.is_set():
                                break

                            pcap_cur = self._derive_port_specific_pcap(phase_port, base_name)
                            prog_idx += 1
                            self._coord_item_index = prog_idx
                            self._record_event(
                                f"COORD AUTO 시험 항목 -> phase=P{phase_port} idx={idx}/{len(phase_pcaps)} pcap={pcap_cur}"
                            )

                            with self.auto_switch_lock:
                                # 해당 포트만 stop/start 순환
                                self.stop_traffic_for_port(phase_port, show_dialog=False, reason_text="auto-switch")

                                rate_cur = self._infer_rate_from_pcap_name(pcap_cur)
                                if rate_cur:
                                    self._safe_ui(self.ent_rate_by_port[phase_port].delete, 0, tk.END)
                                    self._safe_ui(self.ent_rate_by_port[phase_port].insert, 0, rate_cur)

                                self._coord_phase = f"RUN P{phase_port} {idx}/{len(phase_pcaps)}"
                                self._safe_ui(self.combo_pcap_by_port[phase_port].set, pcap_cur)
                                ok = self.play_traffic_for_port(
                                    phase_port,
                                    selected_pcap_override=pcap_cur,
                                    rate_override=rate_cur or None,
                                    show_dialog=False,
                                    reason_text="auto-switch",
                                )
                                if not ok:
                                    self._record_event(f"COORD AUTO 시작 실패 (sequential) port={phase_port} idx={idx}")
                                    return

                                if item_wait_sec > 0:
                                    self._record_event(
                                        f"COORD 항목 절체(송출 유지) {item_wait_sec:.1f}초 ({item_dur_min:.3f}분) "
                                        f"port={phase_port} idx={idx}"
                                    )
                                self._coord_phase = f"항목절체 P{phase_port} {idx}/{len(phase_pcaps)}"
                                waited = 0.0
                                while waited < item_wait_sec and not self._coord_stop.is_set():
                                    time.sleep(0.2)
                                    waited += 0.2
                                if self._coord_stop.is_set():
                                    break

                                self.stop_traffic_for_port(phase_port, show_dialog=False, reason_text="auto-switch")
                                self._record_event(f"COORD AUTO P{phase_port} idx={idx} 송출 종료")

                            if between_wait_sec > 0 and not self._coord_stop.is_set():
                                self._coord_phase = f"Guard P{phase_port} {idx}/{len(phase_pcaps)}"
                                self._record_event(
                                    f"COORD Guard Time(항목 간) {between_wait_sec:.1f}초 ({guard_between_min:.3f}분) "
                                    f"port={phase_port} idx={idx}"
                                )
                                waited_b = 0.0
                                while waited_b < between_wait_sec and not self._coord_stop.is_set():
                                    time.sleep(0.2)
                                    waited_b += 0.2

                    if do_loop:
                        continue
                    break
            finally:
                for p in [0, 1]:
                    self._safe_ui(self._set_auto_state, p, "대기", "gray")
                self._coord_phase = "종료"
                self._coord_running = False
                self._coord_progress_args = None
                self._coord_progress_owner = None
                self._safe_ui(self._close_coord_progress_window)

        th = threading.Thread(target=_coord_loop, daemon=True)
        # thread handle은 port 0에 대표로 보관
        self.auto_run_thread[start_port] = th
        th.start()
        return
    def stop_auto_run_for_port(self, port):
        port = int(port)
        mode_val = self.var_auto_run_mode.get() if hasattr(self, "var_auto_run_mode") else "per-port"
        if mode_val != "sequential-ports" and not getattr(self, "_coord_running", False):
            th0 = self.auto_run_thread.get(0)
            th1 = self.auto_run_thread.get(1)
            running = any(th is not None and th.is_alive() for th in (th0, th1))
            if not running:
                messagebox.showinfo("안내", "Port별(기존) 모드에서는 Auto Stop을 사용할 수 없습니다.")
                return
        # 순차(coordinated) 모드에서는 어느 포트에서 누르더라도 순차 전체를 중지한다.
        if getattr(self, "_coord_running", False):
            self._record_event(f"COORD AUTO RUN 중지 요청 -> port={port} (순차 전체 정지)")
            try:
                if hasattr(self, "_coord_stop") and self._coord_stop is not None:
                    self._coord_stop.set()
            except Exception:
                pass
            # 현재 송출 중일 수 있는 양쪽 포트를 모두 정지한다.
            for p in [0, 1]:
                self.auto_run_stop[p].set()
                self.stop_traffic_for_port(p, show_dialog=False, reason_text="auto-stop")
                self._set_auto_state(p, "중지 요청", "orange")
            # 순차 대표 스레드(start_port 기준) 종료를 짧게 대기
            th0 = self.auto_run_thread.get(0)
            th1 = self.auto_run_thread.get(1)
            th = th0 if (th0 is not None and th0.is_alive()) else th1
            if th is not None and th.is_alive():
                th.join(timeout=3.0)
            self._set_auto_state(0, "중지됨", "gray")
            self._set_auto_state(1, "중지됨", "gray")
            self._record_event("COORD AUTO RUN 중지 완료")
            return
        self.auto_run_stop[port].set()
        self._set_auto_state(0, "중지 요청", "orange")
        self._set_auto_state(1, "중지 요청", "orange")
        self._record_event(f"Port {port} AUTO RUN 중지 요청")
        # Auto 루프만 멈추면 현재 TX는 남아있을 수 있어 즉시 stop도 같이 수행한다.
        self.stop_traffic_for_port(port, show_dialog=False, reason_text="auto-stop")
        th = self.auto_run_thread.get(port)
        if th is not None and th.is_alive():
            th.join(timeout=3.0)
        self._set_auto_state(0, "중지됨", "gray")
        self._set_auto_state(1, "중지됨", "gray")
        self._record_event(f"Port {port} AUTO RUN 중지 완료")

    def _open_coord_progress_window(
        self, mode: str, item_dur_min: float, guard_between_min: float, start_port: int
    ):
        # 진행창은 1개만 유지
        if hasattr(self, "_coord_progress_win") and self._coord_progress_win is not None:
            try:
                if self._coord_progress_win.winfo_exists():
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        win.title("포트 순차 진행 상황")
        win.geometry("520x240")
        win.resizable(False, False)
        self._coord_progress_win = win

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        self._coord_prog_var_title = tk.StringVar(value="포트 순차(진행상황)")
        self._coord_prog_var_line1 = tk.StringVar(value="-")
        self._coord_prog_var_line2 = tk.StringVar(value="-")
        self._coord_prog_var_line3 = tk.StringVar(value="-")

        ttk.Label(frm, textvariable=self._coord_prog_var_title, font=("Malgun Gothic", 12, "bold")).pack(anchor="w")
        ttk.Separator(frm).pack(fill=tk.X, pady=8)
        ttk.Label(frm, textvariable=self._coord_prog_var_line1, font=("Consolas", 11)).pack(anchor="w", pady=2)
        ttk.Label(frm, textvariable=self._coord_prog_var_line2, font=("Consolas", 11)).pack(anchor="w", pady=2)
        ttk.Label(frm, textvariable=self._coord_prog_var_line3, font=("Consolas", 11)).pack(anchor="w", pady=2)

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btns, text="닫기", command=lambda: self._close_coord_progress_window()).pack(side=tk.RIGHT)

        # 추정 총 시간(반복 실행이면 무한)
        try:
            item_sec = max(0.0, float(item_dur_min) * 60.0)
        except Exception:
            item_sec = 0.0
        try:
            between_sec = max(0.0, float(guard_between_min) * 60.0)
        except Exception:
            between_sec = 0.0

        # 순차 모드: 항목 절체(송출) + Guard(항목 간) per item.
        per_item = item_sec + between_sec
        total_items = int(getattr(self, "_coord_total_items", 0) or 0)
        do_loop = bool(getattr(self, "_coord_do_loop", False))
        self._coord_est_total_sec = None if do_loop or total_items <= 0 else (per_item * total_items)

        # 주기 업데이트
        self._coord_progress_update_job = None
        self._coord_progress_tick()

    def _coord_progress_tick(self):
        if not (hasattr(self, "_coord_progress_win") and self._coord_progress_win is not None):
            return
        try:
            if not self._coord_progress_win.winfo_exists():
                return
        except Exception:
            return

        started_at = float(getattr(self, "_coord_started_at", time.time()) or time.time())
        elapsed = max(0.0, time.time() - started_at)
        total_items = int(getattr(self, "_coord_total_items", 0) or 0)
        cur = int(getattr(self, "_coord_item_index", 0) or 0)
        phase = str(getattr(self, "_coord_phase", "-") or "-")
        sp = int(getattr(self, "_coord_start_port", 0) or 0)
        op = 1 - sp

        if self._coord_est_total_sec is None:
            est_txt = "예상 총시간: (반복 실행/무한)"
        else:
            est_txt = f"예상 총시간: {self._format_hms(self._coord_est_total_sec)}"

        self._coord_prog_var_line1.set(f"진행: {cur}/{total_items}  (시작포트: P{sp} → P{op})")
        self._coord_prog_var_line2.set(f"현재 단계: {phase}")
        self._coord_prog_var_line3.set(f"{est_txt} / 경과: {self._format_hms(elapsed)}")

        # 0.5초마다 갱신
        try:
            self._coord_progress_update_job = self.root.after(500, self._coord_progress_tick)
        except Exception:
            pass

    def _close_coord_progress_window(self):
        try:
            if hasattr(self, "_coord_progress_update_job") and self._coord_progress_update_job is not None:
                try:
                    self.root.after_cancel(self._coord_progress_update_job)
                except Exception:
                    pass
                self._coord_progress_update_job = None
        except Exception:
            pass

        try:
            win = getattr(self, "_coord_progress_win", None)
            if win is not None and win.winfo_exists():
                win.destroy()
        except Exception:
            pass
        self._coord_progress_win = None

    def _format_hms(self, seconds: float) -> str:
        try:
            s = int(round(float(seconds)))
        except Exception:
            s = 0
        h = s // 3600
        m = (s % 3600) // 60
        ss = s % 60
        if h > 0:
            return f"{h:d}:{m:02d}:{ss:02d}"
        return f"{m:d}:{ss:02d}"

    def _auto_run_loop(self, port, selected_pcaps, item_dur_min, guard_between_min):
        port = int(port)
        item_wait_sec = max(0.0, float(item_dur_min) * 60.0)
        between_wait_sec = max(0.0, float(guard_between_min) * 60.0)
        do_loop = self.var_auto_loop_by_port[port].get()

        try:
            while not self.auto_run_stop[port].is_set():
                idx = self.auto_next_index[port] % len(selected_pcaps)
                pcap_name = selected_pcaps[idx]
                self.auto_next_index[port] += 1
                self._coord_item_index = idx + 1
                self._safe_ui(self.combo_pcap_by_port[port].set, pcap_name)

                # 동시 절체 충돌 방지: 포트 간 절체는 반드시 순차 처리
                with self.auto_switch_lock:
                    if self.auto_run_stop[port].is_set():
                        break
                    self._record_event(f"Port {port} AUTO 절체 시작 -> {pcap_name}")
                    self.stop_traffic_for_port(port, show_dialog=False, reason_text="auto-switch")
                    # stop 요청이 절체 타이밍에 들어오면 start를 건너뛴다.
                    if self.auto_run_stop[port].is_set():
                        self._record_event(f"Port {port} AUTO 절체 취소(stop 요청 감지) -> {pcap_name}")
                        break
                    auto_rate = self._infer_rate_from_pcap_name(pcap_name)
                    if auto_rate:
                        self._safe_ui(self.ent_rate_by_port[port].delete, 0, tk.END)
                        self._safe_ui(self.ent_rate_by_port[port].insert, 0, auto_rate)
                        self._record_event(f"Port {port} AUTO rate 적용 {auto_rate}Gbps ({pcap_name})")
                    ok = self.play_traffic_for_port(
                        port,
                        selected_pcap_override=pcap_name,
                        rate_override=auto_rate or None,
                        show_dialog=False,
                        reason_text="auto-switch",
                    )
                    if not ok:
                        self._record_event(f"Port {port} AUTO 절체 실패 -> {pcap_name}")
                        self._safe_ui(self._set_auto_state, port, "오류(중지)", "red")
                        return
                    self._coord_phase = f"RUN P{port} {idx + 1}/{len(selected_pcaps)}"
                    self._record_event(f"Port {port} AUTO 송출 시작 -> {pcap_name}")
                    if item_wait_sec > 0:
                        self._record_event(
                            f"Port {port} 항목 절체(송출 유지) {item_wait_sec:.1f}초 ({float(item_dur_min):.3f}분)"
                        )
                    self._coord_phase = f"항목절체 P{port} {idx + 1}/{len(selected_pcaps)}"
                    guard_sleep_step = 0.2
                    waited_item = 0.0
                    while waited_item < item_wait_sec and not self.auto_run_stop[port].is_set():
                        time.sleep(min(guard_sleep_step, max(0.0, item_wait_sec - waited_item)))
                        waited_item += guard_sleep_step
                    if self.auto_run_stop[port].is_set():
                        break
                    self.stop_traffic_for_port(port, show_dialog=False, reason_text="auto-switch")
                    self._record_event(f"Port {port} AUTO 송출 종료 -> {pcap_name}")

                if between_wait_sec > 0 and not self.auto_run_stop[port].is_set():
                    self._coord_phase = f"Guard P{port} {idx + 1}/{len(selected_pcaps)}"
                    self._record_event(
                        f"Port {port} Guard Time(항목 간) {between_wait_sec:.1f}초 ({float(guard_between_min):.3f}분)"
                    )
                    waited_b = 0.0
                    while waited_b < between_wait_sec and not self.auto_run_stop[port].is_set():
                        time.sleep(0.2)
                        waited_b += 0.2

                if not do_loop and self.auto_next_index[port] >= len(selected_pcaps):
                    self._record_event(f"Port {port} AUTO RUN 완료(선택 파일 1회 실행)")
                    break
                if do_loop and self.auto_next_index[port] >= len(selected_pcaps):
                    self.auto_next_index[port] = 0
        finally:
            # 반복 실행이 아니면, 마지막 항목에서 TX가 남아있지 않도록 종료 시 Stop을 한 번 더 보장한다.
            if not do_loop:
                try:
                    self.stop_traffic_for_port(int(port), show_dialog=False, reason_text="auto-stop")
                except Exception:
                    pass
            self._coord_phase = "종료"
            self._safe_ui(self._set_auto_state, 0, "대기", "gray")
            self._safe_ui(self._set_auto_state, 1, "대기", "gray")
            if getattr(self, "_coord_progress_owner", "") == f"per-port-{port}":
                self._coord_progress_owner = None
                self._safe_ui(self._close_coord_progress_window)

    def play_traffic_for_port(
        self,
        port,
        selected_pcap_override=None,
        rate_override=None,
        show_dialog=True,
        reason_text="manual",
    ):
        if reason_text == "manual":
            mode_val = self.var_auto_run_mode.get() if hasattr(self, "var_auto_run_mode") else "per-port"
            if mode_val == "sequential-ports":
                if show_dialog:
                    messagebox.showinfo("안내", "포트 순차 모드에서는 수동 Start를 사용할 수 없습니다.")
                self._record_event(f"Port {int(port)} TX 시작 차단(순차 모드 수동 Start 제한)")
                return False

        if not self._ssh_transport_active():
            auto_sw = reason_text == "auto-switch"
            if not self._ensure_main_ssh_connected(
                attempts=8 if auto_sw else 2,
                delay_sec=3.0 if auto_sw else 1.5,
                log_ctx=(f"Port {int(port)} TX(auto)" if auto_sw else ""),
            ):
                if show_dialog:
                    messagebox.showwarning("경고", "먼저 서버를 연결해 주십시오.")
                self._record_event(f"Port {int(port)} TX 시작 실패(SSH 미연결) reason={reason_text}")
                return False

        if not self.trex_ready:
            if show_dialog:
                messagebox.showwarning("경고", "TRex 엔진이 준비되지 않았습니다. 먼저 서버 연결을 확인해 주세요.")
            self._record_event(f"Port {int(port)} TX 시작 실패(TRex 미준비) reason={reason_text}")
            return False
        if not self._is_port_ready(int(port)):
            # auto-switch는 stop 직후 start를 연계 실행하므로 상태 반영 지연을 짧게 흡수한다.
            if reason_text == "auto-switch":
                wait_deadline = time.time() + 6.0
                while time.time() < wait_deadline and not self._is_port_ready(int(port)):
                    time.sleep(0.2)
                if self._is_port_ready(int(port)):
                    self._record_event(f"Port {int(port)} AUTO 절체 ready 대기 후 시작 재개")
                else:
                    self._record_event(
                        f"Port {int(port)} TX 시작 실패(port state not ready, auto-wait timeout) reason={reason_text}"
                    )
                    return False
            else:
                if show_dialog:
                    messagebox.showwarning(
                        "대기 필요",
                        f"Port {int(port)} 상태가 ready가 아닙니다.\nready 상태가 된 후 Start를 진행해 주세요.",
                    )
                self._record_event(f"Port {int(port)} TX 시작 실패(port state not ready) reason={reason_text}")
                return False

        self._active_tx_port = int(port)
        if not self._validate_inputs(step=2):
            self._record_event(f"Port {int(port)} TX 시작 실패(입력 검증 실패) reason={reason_text}")
            return False

        selections = self.list_pcap_files.curselection()
        selected_pcap = (selected_pcap_override or "").strip() or self.combo_pcap_by_port[int(port)].get().strip()
        if not selected_pcap:
            # 하위 호환: 콤보 미선택 시 기존 리스트 선택을 사용
            if not selections:
                if show_dialog:
                    messagebox.showwarning("경고", f"Port {int(port)} 전송용 PCAP 파일을 선택해 주십시오.")
                self._record_event(f"Port {int(port)} TX 시작 실패(PCAP 미선택) reason={reason_text}")
                return False
            selected_pcap = self.list_pcap_files.get(selections[0])
        elif selections:
            # 리스트 선택이 있어도 포트별 콤보를 우선 사용
            pass

        self.tx_requested_by_port[int(port)] = True
        self._set_monitor_state("출력 요청중", int(port))
        self._update_status_text(f"Port {int(port)} TX 시작 요청")

        try:
            pw = self.ent_ssh_pw.get()
            rate_gbps = str(rate_override).strip() if rate_override is not None else self.ent_rate_by_port[int(port)].get().strip()
            if not rate_gbps:
                if show_dialog:
                    messagebox.showwarning("입력 오류", f"Port {int(port)} Rate (Gbps)를 입력해 주세요.")
                self._record_event(f"Port {int(port)} TX 시작 실패(Rate 미입력) reason={reason_text}")
                return False

            duration_min = float(self.ent_duration_by_port[int(port)].get())
            duration_sec = -1 if duration_min == 0 else duration_min * 60

            target_rate_inner = f"echo '{rate_gbps} Gbps (Target P{int(port)})' > /tmp/trex_target_rate_p{int(port)}"
            target_rate_cmd = f"sudo -S sh -c {validators.quote_remote(target_rate_inner)}"
            _, rate_err = self._safe_ssh_exec(target_rate_cmd, password=pw, timeout=8)
            if rate_err and rate_err.strip():
                self._safe_ui(self._update_status_text, f"Port {int(port)} rate set warn: {rate_err[:120]}")

            base_dir = validators.validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path")
            safe_pcap = validators.sanitize_remote_filename(selected_pcap)
            pcap_full = posixpath.join(base_dir, safe_pcap)

            ports = self._selected_port_for_tx(port)
            port_val = ",".join(str(p) for p in ports)
            rate_command = "100%" if rate_gbps == "25.0" else f"{rate_gbps}gbps"

            trex_path = self._get_trex_path()
            ok_link, link_msg = self._check_port_link_before_start(port, trex_path, pw)
            self._update_status_text(link_msg)
            if not ok_link:
                # 일부 환경에서 get_port_info()가 false negative를 반환하므로 경고만 표시하고 진행한다.
                if show_dialog:
                    messagebox.showwarning(
                        "링크 경고",
                        f"{link_msg}\n\n링크 체크 결과를 무시하고 전송을 계속 시도합니다.",
                    )
                self._set_monitor_state("링크 경고(계속 진행)", int(port))

            self._record_event(
                f"Port {int(port)} 시험 시작 reason={reason_text} pcap={selected_pcap} rate={rate_gbps}Gbps duration_min={duration_min}"
            )

            api_script = play_traffic_stl_script(
                trex_path,
                pcap_full,
                ports,
                rate_command,
                duration_sec,
            )

            b64_api = base64.b64encode(api_script.encode("utf-8")).decode("ascii")
            remote_api = f"/tmp/run_trex_p{int(port)}.py"

            write_cmd = f"printf '%s' {validators.quote_remote(b64_api)} | base64 -d > {validators.quote_remote(remote_api)}"
            _, werr = self._safe_ssh_exec(write_cmd, password=pw, timeout=12)
            if werr and werr.strip():
                self._safe_ui(self._update_status_text, f"Port {int(port)} script write warn: {werr[:140]}")
        except Exception as e:
            self.tx_requested_by_port[int(port)] = False
            self._record_event(f"Port {int(port)} TX 준비 단계 예외 reason={reason_text}: {str(e)[:120]}")
            self._set_monitor_state("ready", int(port))
            if show_dialog:
                messagebox.showerror("전송 실패", f"TX 준비 단계에서 오류가 발생했습니다.\n{str(e)[:200]}")
            return False

        def run():
            try:
                log_path = f"/tmp/run_trex_p{int(port)}.log"
                exec_cmd = (
                    "sudo -S sh -c "
                    + validators.quote_remote(
                        f"python3 -u {remote_api} 2>&1 | tee {log_path}"
                    )
                )
                # play_traffic_stl_script는 이제 즉시 반환하므로 포그라운드 실행으로 결과를 확실히 수집한다.
                log_out, _ = self._safe_ssh_exec(exec_cmd, password=pw, timeout=25)
                if log_out.strip():
                    # 마지막 핵심 로그를 상태창에 남겨 원인 파악을 쉽게 한다.
                    lines = [ln.strip() for ln in log_out.splitlines() if ln.strip()]
                    tail = lines[-3:] if len(lines) >= 3 else lines
                    for ln in tail:
                        self._safe_ui(self._update_status_text, f"Port {int(port)} log: {ln[:160]}")
                if "[ERROR]" in log_out:
                    self.tx_requested_by_port[int(port)] = False
                    self._safe_ui(
                        self._update_status_text,
                        f"Port {int(port)} 시작 실패: {log_out.splitlines()[-1][:120]}",
                    )
                    if show_dialog:
                        self._safe_ui(
                            messagebox.showerror,
                            "전송 실패",
                            f"Port {int(port)} 전송 시작 실패\n\n{log_out}",
                        )
                    self._safe_ui(
                        self._record_event,
                        f"Port {int(port)} 시험 시작 실패 reason={reason_text} pcap={selected_pcap}",
                    )
                    self._safe_ui(self._set_monitor_state, "ready", int(port))
                    return

                self._safe_ui(
                    self._update_status_text,
                    f"Port {int(port)} TX 시작 요청 완료. 상세 오류: {log_path}",
                )
                time.sleep(3.0)
                latest = self.latest_port_stats.get(int(port), {})
                tx_bps_now = float(latest.get("tx_bps", 0.0))
                sample_age = time.time() - float(latest.get("ts", 0.0) or 0.0)
                if sample_age < 10 and tx_bps_now <= 0.0:
                    self._safe_ui(
                        self._update_status_text,
                        f"Port {int(port)} TX=0 관측(초기 샘플). 모니터 지연/재연결일 수 있으니 추이를 더 확인하세요.",
                    )
                    self._safe_ui(self._set_monitor_state, "미출력(확인 필요)", int(port))
            except Exception as e:
                self.tx_requested_by_port[int(port)] = False
                if show_dialog:
                    self._safe_ui(messagebox.showerror, "전송 실패", f"실행 중 오류가 발생했습니다.\n{e}")
                self._safe_ui(self._record_event, f"Port {int(port)} 시험 시작 예외 reason={reason_text}: {str(e)[:120]}")
                self._safe_ui(self._set_monitor_state, "ready", int(port))

        threading.Thread(target=run, daemon=True).start()

        duration_msg = "중지 명령 전까지 무한 전송" if duration_sec == -1 else f"{duration_min} 분"
        if show_dialog:
            messagebox.showinfo("전송 시작", f"포트 {port_val} 할당 완료.\n트래픽 인가를 시작합니다. (설정 시간: {duration_msg})")
        self._set_monitor_state("출력 확인 대기", int(port))
        return True

    def play_traffic(self):
        ports = [0, 1]
        if len(ports) != 1:
            messagebox.showwarning("안내", "포트별 제어는 Port 0/1 Control에서 사용하세요.")
            return
        self.play_traffic_for_port(ports[0])

    def stop_traffic_for_port(self, port, show_dialog=False, reason_text="manual"):
        if reason_text == "manual":
            mode_val = self.var_auto_run_mode.get() if hasattr(self, "var_auto_run_mode") else "per-port"
            if mode_val == "sequential-ports":
                if show_dialog:
                    messagebox.showinfo("안내", "포트 순차 모드에서는 수동 Stop을 사용할 수 없습니다.")
                self._record_event(f"Port {int(port)} 시험 종료 차단(순차 모드 수동 Stop 제한)")
                return False

        if not self._ssh_transport_active():
            auto_sw = reason_text == "auto-switch"
            self._ensure_main_ssh_connected(
                attempts=8 if auto_sw else 2,
                delay_sec=3.0 if auto_sw else 1.5,
                log_ctx=(f"Port {int(port)} Stop(auto)" if auto_sw else ""),
            )
        if not self._ssh_transport_active():
            self._record_event(f"Port {int(port)} 시험 종료 실패(SSH 미연결) reason={reason_text}")
            return False

        # 사용자가 수동으로 Stop을 누르면 Auto Run 재시작 루프도 함께 차단한다.
        if reason_text == "manual":
            self.auto_run_stop[int(port)].set()
            self._set_auto_state(int(port), "중지됨", "gray")
            self._record_event(f"Port {int(port)} AUTO RUN 동시 중지(reason={reason_text})")

        self.tx_requested_by_port[int(port)] = False
        self._update_status_text(f"Port {int(port)} 트래픽 전송 중지 명령 하달...")
        self._record_event(f"Port {int(port)} 시험 종료 요청 reason={reason_text}")
        self._set_monitor_state("멈춤 요청", int(port))
        threading.Thread(target=self._stop_bg, args=(int(port),), daemon=True).start()
        return True

    def stop_traffic(self):
        ports = [0, 1]
        if len(ports) != 1:
            messagebox.showwarning("안내", "포트별 제어는 Port 0/1 Control에서 사용하세요.")
            return
        self.stop_traffic_for_port(ports[0])

    def _stop_bg(self, port):
        try:
            pw = self.ent_ssh_pw.get()
            stop_rate_cmd = "sudo -S sh -c " + validators.quote_remote(
                f"echo '0 Gbps (STOP P{int(port)})' > /tmp/trex_target_rate_p{int(port)}"
            )
            self._safe_ssh_exec(stop_rate_cmd, password=pw)

            ports = [int(port)]
            trex_path = self._get_trex_path()

            stop_script = stop_traffic_script(trex_path, ports)

            b64_stop = base64.b64encode(stop_script.encode("utf-8")).decode("ascii")
            remote_stop = f"/tmp/stop_trex_p{int(port)}.py"
            write_cmd = f"printf '%s' {validators.quote_remote(b64_stop)} | base64 -d > {validators.quote_remote(remote_stop)}"
            self._safe_ssh_exec(write_cmd, password=pw)
            self._safe_ssh_exec(f"sudo -S python3 {validators.quote_remote(remote_stop)}", password=pw)
            self._safe_ui(self._set_monitor_state, "ready", int(port))
            self._safe_ui(self._record_event, f"Port {int(port)} 시험 종료 완료")

        except Exception as e:
            self._safe_ui(self._update_status_text, f"중지 실패: {str(e)[:80]}")
            self._safe_ui(self._set_monitor_state, "ready", int(port))
            self._safe_ui(self._record_event, f"Port {int(port)} 시험 종료 실패: {str(e)[:120]}")

    def start_reachability_monitor(self):
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 서버를 연결해 주세요.")
            return

        target = self.ent_fping_target.get().strip()
        interval = self.ent_fping_interval.get().strip()
        size = self.ent_fping_size.get().strip()

        if not target or not validators.is_valid_ip(target):
            messagebox.showerror("입력 오류", "Reachability Target IP가 올바르지 않습니다.")
            return

        try:
            interval_val = float(interval)
            if interval_val <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("입력 오류", "Interval은 0보다 큰 숫자여야 합니다.")
            return

        try:
            size_val = int(size)
            if size_val < 0 or size_val > 65500:
                raise ValueError
        except ValueError:
            messagebox.showerror("입력 오류", "Payload Size는 0~65500 범위의 숫자여야 합니다.")
            return

        with self.fping_lock:
            self.fping_running = True

        self._update_fping_text("Reachability monitor starting...")
        threading.Thread(target=self._reachability_monitor_bg, daemon=True).start()

    def stop_reachability_monitor(self):
        with self.fping_lock:
            self.fping_running = False
        self._update_fping_text("Reachability monitor stop requested.")

    def _reachability_monitor_bg(self):
        try:
            target = self.ent_fping_target.get().strip()
            interval = float(self.ent_fping_interval.get().strip())
            size = int(self.ent_fping_size.get().strip())
            pw = self.ent_ssh_pw.get()

            interval_ms = max(100, int(interval * 1000))

            which_cmd = "which fping || true"
            out, err = self._safe_ssh_exec(which_cmd, password=pw, timeout=10)
            has_fping = bool(out.strip())

            if has_fping:
                self._update_fping_text("fping detected on server. Using fping monitor.")
                monitor_cmd = f"""
while true; do
  fping -D -c 1 -p {interval_ms} -b {size} {shlex.quote(target)} 2>&1
  sleep {interval}
done
"""
            else:
                self._update_fping_text("fping not found. Using ping fallback.")
                monitor_cmd = f"""
while true; do
  ping -c 1 -i {interval} -s {size} {shlex.quote(target)} 2>&1
  sleep {interval}
done
"""

            stdin, stdout, stderr = self.ssh_client.exec_command(
                f"bash -lc {validators.quote_remote(monitor_cmd)}",
                get_pty=True
            )

            for line in iter(stdout.readline, ""):
                with self.fping_lock:
                    if not self.fping_running:
                        break
                clean_line = line.strip()
                if clean_line and not clean_line.startswith("[sudo]"):
                    self._safe_ui(self._update_fping_text, clean_line)

        except Exception as e:
            self._safe_ui(self._update_fping_text, f"Reachability monitor error: {str(e)[:100]}")

    def _load_config(self):
        fields = {
            "server_ip": self.ent_server_ip,
            "ssh_user": self.ent_ssh_user,
            "ssh_pw": self.ent_ssh_pw,
            "src_mac": self.ent_src_mac,
            "dst_mac": self.ent_dst_mac,
            "src_ip": self.ent_src_ip,
            "dst_ip": self.ent_dst_ip,
            "dst_port": self.ent_dst_port,
            "vlan_id": self.ent_vlan_id,
            "pcap_path": self.ent_pcap_path,
            "pcap_name": self.ent_pcap_name,
            "pkt_size": self.ent_pkt_size,
            "pcap_ms": self.ent_pcap_ms,
            "duration_min": self.ent_duration_min,
            "duration_min_p1": self.ent_duration_by_port[1],
            "rate_p0": self.ent_rate_by_port[0],
            "rate_p1": self.ent_rate_by_port[1],
            "pcap_sel_p0": self.combo_pcap_by_port[0],
            "pcap_sel_p1": self.combo_pcap_by_port[1],
            "fping_target": self.ent_fping_target,
            "fping_interval": self.ent_fping_interval,
            "fping_size": self.ent_fping_size
        }

        defaults = {
            "server_ip": "192.168.9.249",
            "ssh_user": "slab",
            "ssh_pw": "",
            "src_mac": "00:11:22:33:44:55",
            "dst_mac": "AA:BB:CC:DD:EE:FF",
            "src_ip": "192.168.11.100",
            "dst_ip": "192.168.11.2",
            "dst_port": "830",
            "vlan_id": "",
            "pcap_path": "/tmp/pcap_output",
            "pcap_name": "attack_target.pcap",
            "pkt_size": "64",
            "pkt_mode": "Fixed",
            "rate": "10.0",
            "pcap_ms": "1.0",
            "duration_min": "0",
            "duration_min_p1": "0",
            "rate_p0": "10.0",
            "rate_p1": "10.0",
            "pcap_sel_p0": "",
            "pcap_sel_p1": "",
            "auto_item_duration_min_p0": "30",
            "auto_item_duration_min_p1": "30",
            "auto_guard_between_min_p0": "5",
            "auto_guard_between_min_p1": "5",
            "auto_loop_p0": True,
            "auto_loop_p1": True,
            "auto_order_p0": [],
            "auto_order_p1": [],
            "attack_type": "eCPRI U-Plane (대역폭/RRC 과부하)",
            "mutation_enable": False,
            "rand_mac": False,
            "rand_ip": False,
            "rand_vlan": False,
            "rand_ethertype": False,
            "malformed_ecpri": False,
            "invalid_length": False,
            "rand_l4_port": False,
            "tcp_synack_only": False,
            "fping_target": "192.168.11.2",
            "fping_interval": "1",
            "fping_size": "56"
        }

        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        defaults.update(loaded)
            except Exception:
                pass

        for key, entry in fields.items():
            val = defaults.get(key, "")
            if isinstance(entry, ttk.Combobox):
                entry.set(val)
            else:
                entry.delete(0, tk.END)
                entry.insert(0, val)

        loaded_attack = defaults.get("attack_type", "eCPRI U-Plane (대역폭/RRC 과부하)")
        valid_attacks = list(self.combo_attack.cget("values"))
        if loaded_attack not in valid_attacks:
            loaded_attack = "eCPRI U-Plane (대역폭/RRC 과부하)"
        self.combo_attack.set(loaded_attack)
        self.combo_pkt_mode.set(defaults.get("pkt_mode", "Fixed"))
        self.var_line_rate.set(defaults.get("rate", "10.0"))
        self.ent_rate_by_port[0].delete(0, tk.END)
        self.ent_rate_by_port[0].insert(0, defaults.get("rate_p0", defaults.get("rate", "10.0")))
        self.ent_rate_by_port[1].delete(0, tk.END)
        self.ent_rate_by_port[1].insert(0, defaults.get("rate_p1", defaults.get("rate", "10.0")))
        for _port in (0, 1):
            pk = f"p{_port}"
            legacy_one = defaults.get(f"auto_guard_sec_{pk}", "")
            item_v = defaults.get(f"auto_item_duration_min_{pk}", "")
            if not str(item_v).strip():
                # 예전 단일 칸(auto_guard_sec)은 항목 송출 유지 시간으로 쓰이던 경우가 많음
                item_v = legacy_one if str(legacy_one).strip() else "30"
            between_v = defaults.get(f"auto_guard_between_min_{pk}", "")
            if not str(between_v).strip():
                between_v = "5"
            self.ent_auto_item_duration_min_by_port[_port].delete(0, tk.END)
            self.ent_auto_item_duration_min_by_port[_port].insert(0, str(item_v).strip())
            self.ent_auto_guard_between_min_by_port[_port].delete(0, tk.END)
            self.ent_auto_guard_between_min_by_port[_port].insert(0, str(between_v).strip())
        # 반복 실행은 공유 변수(둘 중 하나만 저장되어도 동작)
        loop_v = defaults.get("auto_loop_p0", defaults.get("auto_loop_p1", True))
        self.var_auto_loop_by_port[0].set(bool(loop_v))
        self.var_auto_loop_by_port[1].set(bool(loop_v))
        ord0 = defaults.get("auto_order_p0", [])
        ord1 = defaults.get("auto_order_p1", [])
        self.auto_order_by_port[0] = list(ord0) if isinstance(ord0, list) else []
        self.auto_order_by_port[1] = list(ord1) if isinstance(ord1, list) else []

        self.var_mutation_enable.set(defaults.get("mutation_enable", False))
        self.var_rand_mac.set(defaults.get("rand_mac", False))
        self.var_rand_ip.set(defaults.get("rand_ip", False))
        self.var_rand_vlan.set(defaults.get("rand_vlan", False))
        self.var_rand_ethertype.set(defaults.get("rand_ethertype", False))
        self.var_malformed_ecpri.set(defaults.get("malformed_ecpri", False))
        self.var_invalid_length.set(defaults.get("invalid_length", False))
        self.var_rand_l4_port.set(defaults.get("rand_l4_port", False))
        self.var_tcp_synack_only.set(defaults.get("tcp_synack_only", False))

        self._toggle_mutation_options()
        self._on_pkt_mode_changed()
        self._update_test_description(None)

    def _save_config(self):
        config = {
            "server_ip": self.ent_server_ip.get(),
            "ssh_user": self.ent_ssh_user.get(),
            "ssh_pw": self.ent_ssh_pw.get(),
            "src_mac": self.ent_src_mac.get(),
            "dst_mac": self.ent_dst_mac.get(),
            "src_ip": self.ent_src_ip.get(),
            "dst_ip": self.ent_dst_ip.get(),
            "dst_port": self.ent_dst_port.get(),
            "vlan_id": self.ent_vlan_id.get(),
            "pcap_path": self.ent_pcap_path.get(),
            "pcap_name": self.ent_pcap_name.get(),
            "pkt_size": self.ent_pkt_size.get(),
            "pkt_mode": self.combo_pkt_mode.get(),
            "rate": self.var_line_rate.get(),
            "pcap_ms": self.ent_pcap_ms.get(),
            "duration_min": self.ent_duration_min.get(),
            "duration_min_p1": self.ent_duration_by_port[1].get(),
            "rate_p0": self.ent_rate_by_port[0].get(),
            "rate_p1": self.ent_rate_by_port[1].get(),
            "pcap_sel_p0": self.combo_pcap_by_port[0].get(),
            "pcap_sel_p1": self.combo_pcap_by_port[1].get(),
            "auto_item_duration_min_p0": self.ent_auto_item_duration_min_by_port[0].get(),
            "auto_item_duration_min_p1": self.ent_auto_item_duration_min_by_port[1].get(),
            "auto_guard_between_min_p0": self.ent_auto_guard_between_min_by_port[0].get(),
            "auto_guard_between_min_p1": self.ent_auto_guard_between_min_by_port[1].get(),
            "auto_loop_p0": self.var_auto_loop_by_port[0].get(),
            "auto_loop_p1": self.var_auto_loop_by_port[1].get(),
            "auto_order_p0": self.auto_order_by_port[0],
            "auto_order_p1": self.auto_order_by_port[1],
            "attack_type": self.combo_attack.get(),
            "mutation_enable": self.var_mutation_enable.get(),
            "rand_mac": self.var_rand_mac.get(),
            "rand_ip": self.var_rand_ip.get(),
            "rand_vlan": self.var_rand_vlan.get(),
            "rand_ethertype": self.var_rand_ethertype.get(),
            "malformed_ecpri": self.var_malformed_ecpri.get(),
            "invalid_length": self.var_invalid_length.get(),
            "rand_l4_port": self.var_rand_l4_port.get(),
            "tcp_synack_only": self.var_tcp_synack_only.get(),
            "fping_target": self.ent_fping_target.get(),
            "fping_interval": self.ent_fping_interval.get(),
            "fping_size": self.ent_fping_size.get()
        }

        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    def _on_closing(self):
        self._save_config()
        self._close_ssh_sessions()
        self.root.destroy()


def main():
    root = tk.Tk()
    ORanValidationGUI(root)
    root.mainloop()

# python -m PyInstaller --noconsole --onefile --icon="DDOS.ico" oran_trex_master.py
