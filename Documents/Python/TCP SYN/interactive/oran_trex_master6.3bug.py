#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
O-RAN O-RU DDoS Validation System v6.2
- 기존 기능 유지 중심 확장 버전
- Linux 서버 기준 경로/명령 처리 안정화
- Packet Size Mode 추가 (Fixed / Standard Random / Jumbo Random)
- Mutation / Randomization Options 추가
- Reachability Monitor 추가 (fping 우선, ping fallback)
- Random Size Mode 시 평균값 기반 Usage 계산

v6.2 patched
- Reachability Monitor 원격 무한루프 제거
- TRex play 실행을 nohup + PID 방식으로 변경
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import paramiko
import threading
import json
import os
import time
import base64
import re
import shlex
import ipaddress
import posixpath
import traceback

CONFIG_FILE = "oran_ru_config_v6.json"
SSH_TIMEOUT = 30
SSH_CONNECT_TIMEOUT = 10
TREX_STARTUP_TIMEOUT = 30
STATS_MAX_LINES = 200
FPING_MAX_LINES = 300


class ORanValidationGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("O-RAN O-RU DDoS Validation System v6.2")
        self.root.geometry("1280x980")

        self.ssh_client = None
        self.trex_server_ssh = None
        self.monitor_running = False
        self.fping_running = False
        self.trex_ready = False

        self.ssh_lock = threading.Lock()
        self.monitor_lock = threading.Lock()
        self.fping_lock = threading.Lock()

        self.reachability_thread = None
        self.reachability_stop_event = threading.Event()

        self.trex_remote_pid = None
        self.trex_pid_file = "/tmp/oran_trex_run.pid"
        self.trex_log_file = "/tmp/oran_trex_run.log"
        self.trex_script_file = "/tmp/run_trex.py"

        self.style = ttk.Style()
        self.style.theme_use("clam")

        self._create_notebook()
        self._load_config()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _safe_ui(self, func, *args, **kwargs):
        self.root.after(0, lambda: func(*args, **kwargs))

    def _safe_ssh_exec(self, command, password=None, timeout=SSH_TIMEOUT):
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

                output = stdout.read().decode("utf-8", errors="replace")
                error = stderr.read().decode("utf-8", errors="replace")
                return output, error
            except Exception as e:
                return "", str(e)

    def _is_valid_ip(self, value):
        try:
            ipaddress.ip_address(value.strip())
            return True
        except Exception:
            return False

    def _is_valid_mac(self, value):
        return bool(re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value.strip()))

    def _is_linux_abs_path(self, value):
        value = value.strip()
        if not value:
            return False
        if "\\" in value:
            return False
        return value.startswith("/")

    def _sanitize_remote_filename(self, name):
        name = name.strip()
        if not name:
            raise ValueError("PCAP 파일명이 비어 있습니다.")
        if "/" in name or "\\" in name:
            raise ValueError("PCAP 파일명에는 경로 구분자를 포함할 수 없습니다.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError("PCAP 파일명은 영문/숫자/._- 만 사용할 수 있습니다.")
        if name.startswith("."):
            raise ValueError("PCAP 파일명은 점(.)으로 시작할 수 없습니다.")
        if not name.lower().endswith(".pcap"):
            name += ".pcap"
        return name

    def _validate_remote_path(self, path_value, label):
        path_value = path_value.strip()
        if not self._is_linux_abs_path(path_value):
            raise ValueError(f"{label}: Linux 절대경로만 허용됩니다.")
        dangerous_patterns = ["..", "~", ";", "|", "&", "$", "`", ">", "<"]
        for pat in dangerous_patterns:
            if pat in path_value:
                raise ValueError(f"{label}: 허용되지 않는 경로 문자가 포함되어 있습니다. ({pat})")
        normalized = posixpath.normpath(path_value)
        if not normalized.startswith("/"):
            raise ValueError(f"{label}: 올바른 Linux 절대경로가 아닙니다.")
        return normalized

    def _quote_remote(self, value):
        return shlex.quote(value)

    def _get_pkt_mode_average_size(self):
        mode = self.combo_pkt_mode.get().strip()
        if mode == "Standard Random":
            return (64 + 1500) / 2
        if mode == "Jumbo Random":
            return (64 + 9000) / 2
        return None

    def _toggle_mutation_options(self):
        enabled = self.var_mutation_enable.get()
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in self.mutation_widgets:
            widget.config(state=state)

    def _validate_inputs(self, step=1):
        errors = []

        server_ip = self.ent_server_ip.get().strip()
        ssh_user = self.ent_ssh_user.get().strip()
        trex_path = self.ent_trex_path.get().strip()
        pcap_path = self.ent_pcap_path.get().strip()
        pcap_name = self.ent_pcap_name.get().strip()
        attack_type = self.combo_attack.get().strip()

        if not server_ip or not self._is_valid_ip(server_ip):
            errors.append("Server IP: 올바른 IP 주소를 입력해야 합니다.")

        if not ssh_user:
            errors.append("SSH User: 필수 입력값입니다.")

        try:
            self._validate_remote_path(trex_path, "TRex Path")
        except ValueError as e:
            errors.append(str(e))

        try:
            self._validate_remote_path(pcap_path, "PCAP Save Path")
        except ValueError as e:
            errors.append(str(e))

        try:
            self._sanitize_remote_filename(pcap_name)
        except ValueError as e:
            errors.append(str(e))

        if not attack_type:
            errors.append("Test Type: 시험 유형을 선택해야 합니다.")

        src_mac = self.ent_src_mac.get().strip()
        dst_mac = self.ent_dst_mac.get().strip()

        if src_mac and not self._is_valid_mac(src_mac):
            errors.append("Attacker MAC: 올바른 MAC 주소 형식이 아닙니다.")
        if dst_mac and not self._is_valid_mac(dst_mac):
            errors.append("O-RU MAC: 올바른 MAC 주소 형식이 아닙니다.")

        atype = attack_type.upper()
        is_l2 = any(k in atype for k in ["U-PLANE", "C-PLANE", "PRACH", "PTP"])

        if not is_l2:
            src_ip = self.ent_src_ip.get().strip()
            dst_ip = self.ent_dst_ip.get().strip()
            if not src_ip or not self._is_valid_ip(src_ip):
                errors.append("Attacker IP: 올바른 IP 주소를 입력해야 합니다.")
            if not dst_ip or not self._is_valid_ip(dst_ip):
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

        pkt_mode = self.combo_pkt_mode.get().strip()
        if pkt_mode not in ["Fixed", "Standard Random", "Jumbo Random"]:
            errors.append("Packet Size Mode: 유효한 모드를 선택해야 합니다.")

        if pkt_mode == "Fixed":
            try:
                pkt_size = int(self.ent_pkt_size.get())
                if not (64 <= pkt_size <= 1518):
                    errors.append("Packet Size: 64~1518 바이트 사이여야 합니다.")
            except ValueError:
                errors.append("Packet Size: 숫자만 입력 가능합니다.")

        try:
            rate = float(self.ent_rate.get())
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
                duration_min = float(self.ent_duration_min.get())
                if duration_min < 0:
                    errors.append("Transmission Duration: 0 또는 양수여야 합니다.")
            except ValueError:
                errors.append("Transmission Duration: 숫자만 입력 가능합니다.")

        if errors:
            messagebox.showerror("입력값 검증 오류", "\n".join(errors))
            return False
        return True

    def _create_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tab_server = ttk.Frame(self.notebook, padding=10)
        self.tab_ru_attack = ttk.Frame(self.notebook, padding=10)
        self.tab_control = ttk.Frame(self.notebook, padding=10)
        self.tab_validation = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_server, text=" 1. 서버 설정 ")
        self.notebook.add(self.tab_ru_attack, text=" 2. RU & 검증 설정 ")
        self.notebook.add(self.tab_control, text=" 3. TRex 트래픽 제어소 ")
        self.notebook.add(self.tab_validation, text=" 4. 판정 및 검증 ")

        self._build_server_tab()
        self._build_ru_attack_tab()
        self._build_control_tab()
        self._build_validation_tab()

    def _build_server_tab(self):
        frame = ttk.LabelFrame(self.tab_server, text="Linux Server Configuration", padding=20)
        frame.pack(fill=tk.X, padx=10, pady=10)

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
        self.ent_trex_path = ttk.Entry(frame, width=40)
        self.ent_trex_path.grid(row=3, column=1, padx=10, pady=5)

        ttk.Label(frame, text="TRex Port (NIC):").grid(row=4, column=0, sticky="w", pady=5)
        self.combo_trex_port = ttk.Combobox(frame, values=["0", "1", "0,1"], width=37, state="readonly")
        self.combo_trex_port.grid(row=4, column=1, padx=10, pady=5)

        ttk.Label(frame, text="TRex Cores (-c):").grid(row=5, column=0, sticky="w", pady=5)
        self.combo_trex_cores = ttk.Combobox(frame, values=["2", "4", "6"], width=37, state="readonly")
        self.combo_trex_cores.grid(row=5, column=1, padx=10, pady=5)

        self.btn_connect = tk.Button(
            frame,
            text="서버 연결 및 TRex 엔진 구동",
            bg="#3498db",
            fg="white",
            font=("Malgun Gothic", 10, "bold"),
            command=self.connect_server
        )
        self.btn_connect.grid(row=6, column=0, columnspan=2, pady=20, ipadx=20, ipady=5)

        self.lbl_server_status = ttk.Label(frame, text="상태: 연결 대기 중...", foreground="gray")
        self.lbl_server_status.grid(row=7, column=0, columnspan=2)

    def _build_ru_attack_tab(self):
        ru_frame = ttk.LabelFrame(self.tab_ru_attack, text="Target RU Network Configuration", padding=15)
        ru_frame.pack(fill=tk.X, padx=10, pady=5)

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

        atk_frame = ttk.LabelFrame(self.tab_ru_attack, text="Step 1: 검증용 패킷 블록(PCAP) 생성 설정", padding=15)
        atk_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        ttk.Label(atk_frame, text="Test Type:").grid(row=0, column=0, sticky="w", pady=5)

        attack_types = [
            "eCPRI U-Plane (대역폭/RRC 과부하)",
            "eCPRI C-Plane (제어 평면 마비)",
            "PRACH Spoofing (무선 자원 고갈)",
            "F1-U GTP-U (비정상 패킷 필터링)",
            "PTP/IEEE 1588 (동기화 교란)",
            "NETCONF Session (관리망 마비)"
        ]

        self.combo_attack = ttk.Combobox(atk_frame, width=38, state="readonly", values=attack_types)
        self.combo_attack.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        self.combo_attack.bind("<<ComboboxSelected>>", self._update_test_description)

        ttk.Label(atk_frame, text="Packet Size Mode:").grid(row=1, column=0, sticky="w", pady=5)
        self.combo_pkt_mode = ttk.Combobox(
            atk_frame,
            width=20,
            state="readonly",
            values=["Fixed", "Standard Random", "Jumbo Random"]
        )
        self.combo_pkt_mode.grid(row=1, column=1, padx=10, pady=5, sticky="w")
        self.combo_pkt_mode.bind("<<ComboboxSelected>>", self._on_pkt_mode_changed)

        ttk.Label(atk_frame, text="Packet Size (Bytes):").grid(row=2, column=0, sticky="w", pady=5)
        self.ent_pkt_size = ttk.Entry(atk_frame, width=15)
        self.ent_pkt_size.grid(row=2, column=1, padx=10, pady=5, sticky="w")
        self.ent_pkt_size.bind("<KeyRelease>", self._calculate_pps)

        ttk.Label(atk_frame, text="Line Rate (Gbps):").grid(row=3, column=0, sticky="w", pady=5)
        self.ent_rate = ttk.Entry(atk_frame, width=15)
        self.ent_rate.grid(row=3, column=1, padx=10, pady=5, sticky="w")
        self.ent_rate.bind("<KeyRelease>", self._calculate_pps)

        ttk.Label(atk_frame, text="Packet Length (ms):").grid(row=4, column=0, sticky="w", pady=5)
        self.ent_pcap_ms = ttk.Entry(atk_frame, width=15)
        self.ent_pcap_ms.grid(row=4, column=1, padx=10, pady=5, sticky="w")

        mut_frame = ttk.LabelFrame(atk_frame, text="Mutation / Randomization Options", padding=10)
        mut_frame.grid(row=5, column=0, columnspan=2, pady=10, sticky="ew")

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
        self.chk_invalid_length = ttk.Checkbutton(mut_frame, text="Invalid Length Field", variable=self.var_invalid_length)
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

        sim_frame = ttk.LabelFrame(atk_frame, text="Expected Throughput Simulation", padding="10")
        sim_frame.grid(row=6, column=0, columnspan=2, pady=10, sticky="ew")

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

        ttk.Label(atk_frame, text="[시험 설정 가이드]").grid(row=0, column=2, sticky="nw", padx=20)

        self.txt_desc = tk.Text(
            atk_frame,
            width=55,
            height=16,
            bg="#e8f6f3",
            font=("Malgun Gothic", 9),
            wrap=tk.WORD
        )
        self.txt_desc.grid(row=1, column=2, rowspan=6, padx=20, pady=5, sticky="nsew")
        self.txt_desc.config(state=tk.DISABLED)

        action_frame = ttk.Frame(atk_frame)
        action_frame.grid(row=7, column=0, columnspan=3, pady=15, sticky="ew")

        self.btn_build_pcap = tk.Button(
            action_frame,
            text="Step 1: 검증용 패킷 생성 (PCAP Build)",
            bg="#f39c12",
            fg="white",
            font=("Malgun Gothic", 11, "bold"),
            command=self.build_pcap
        )
        self.btn_build_pcap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=20, ipady=10)

        self._toggle_mutation_options()

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

        ctrl_frame = ttk.LabelFrame(self.tab_control, text="Step 2: 트래픽 인가 통제 (Traffic TX Control)", padding=15)
        ctrl_frame.pack(fill=tk.X, padx=10, pady=10)

        dur_frame = ttk.Frame(ctrl_frame)
        dur_frame.pack(fill=tk.X, pady=10)

        ttk.Label(
            dur_frame,
            text="Transmission Duration (Min):",
            font=("Malgun Gothic", 10, "bold")
        ).pack(side=tk.LEFT, padx=5)

        self.ent_duration_min = ttk.Entry(dur_frame, width=15)
        self.ent_duration_min.pack(side=tk.LEFT, padx=10)

        ttk.Label(
            dur_frame,
            text="(0 입력 시 중지 버튼을 누르기 전까지 무한 전송 모드로 동작합니다)",
            foreground="blue"
        ).pack(side=tk.LEFT)

        control_split = ttk.Frame(ctrl_frame)
        control_split.pack(fill=tk.X, pady=10)

        btn_frame = ttk.Frame(control_split)
        btn_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        self.btn_play = tk.Button(
            btn_frame,
            text="Start TX",
            bg="#27ae60",
            fg="white",
            font=("Arial", 16, "bold"),
            command=self.play_traffic
        )
        self.btn_play.pack(side=tk.TOP, expand=True, fill=tk.X, padx=10, pady=5, ipady=12)

        self.btn_stop = tk.Button(
            btn_frame,
            text="Stop TX",
            bg="#c0392b",
            fg="white",
            font=("Arial", 16, "bold"),
            command=self.stop_traffic
        )
        self.btn_stop.pack(side=tk.TOP, expand=True, fill=tk.X, padx=10, pady=5, ipady=12)

        reach_frame = ttk.LabelFrame(control_split, text="Reachability Monitor", padding=10)
        reach_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        top_reach = ttk.Frame(reach_frame)
        top_reach.pack(fill=tk.X, pady=5)

        ttk.Label(top_reach, text="Target IP:").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        self.ent_fping_target = ttk.Entry(top_reach, width=18)
        self.ent_fping_target.grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(top_reach, text="Interval (sec):").grid(row=0, column=2, sticky="w", padx=5, pady=3)
        self.ent_fping_interval = ttk.Entry(top_reach, width=10)
        self.ent_fping_interval.grid(row=0, column=3, padx=5, pady=3)

        ttk.Label(top_reach, text="Payload Size:").grid(row=0, column=4, sticky="w", padx=5, pady=3)
        self.ent_fping_size = ttk.Entry(top_reach, width=10)
        self.ent_fping_size.grid(row=0, column=5, padx=5, pady=3)

        btn_reach = ttk.Frame(reach_frame)
        btn_reach.pack(fill=tk.X, pady=5)

        self.btn_fping_start = ttk.Button(btn_reach, text="Start Monitor", command=self.start_reachability_monitor)
        self.btn_fping_start.pack(side=tk.LEFT, padx=5)

        self.btn_fping_stop = ttk.Button(btn_reach, text="Stop Monitor", command=self.stop_reachability_monitor)
        self.btn_fping_stop.pack(side=tk.LEFT, padx=5)

        stat_split = ttk.PanedWindow(self.tab_control, orient=tk.HORIZONTAL)
        stat_split.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        stat_frame = ttk.LabelFrame(stat_split, text="실시간 DPDK 엔진 통계 모니터 (Real-time Stats)", padding=10)
        self.txt_stats = scrolledtext.ScrolledText(
            stat_frame,
            bg="black",
            fg="#00ff00",
            font=("Consolas", 11)
        )
        self.txt_stats.pack(fill=tk.BOTH, expand=True)

        fping_frame = ttk.LabelFrame(stat_split, text="Reachability Monitor Output", padding=10)
        self.txt_fping = scrolledtext.ScrolledText(
            fping_frame,
            bg="#111111",
            fg="#00d7ff",
            font=("Consolas", 10)
        )
        self.txt_fping.pack(fill=tk.BOTH, expand=True)

        stat_split.add(stat_frame, weight=1)
        stat_split.add(fping_frame, weight=1)

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

        self.chk_var1 = tk.BooleanVar(value=False)
        self.chk_var2 = tk.BooleanVar(value=False)
        self.chk_var3 = tk.BooleanVar(value=False)
        self.chk_var4 = tk.BooleanVar(value=False)

        ttk.Checkbutton(
            check_frame,
            text="1. [시스템 로그] UART 콘솔에 OOM(Out of Memory), Kernel Panic 등 치명적 에러가 발생하지 않음",
            variable=self.chk_var1
        ).pack(anchor="w", pady=8, padx=10)

        ttk.Checkbutton(
            check_frame,
            text="2. [관리 제어망] NMS에서 O-RU로 NETCONF / SSH 통신이 응답하며 제어권이 살아있음",
            variable=self.chk_var2
        ).pack(anchor="w", pady=8, padx=10)

        ttk.Checkbutton(
            check_frame,
            text="3. [프론트홀망] O-DU와의 PTP Clock Lock 상태 및 C/U-Plane 세션이 정상 복구됨",
            variable=self.chk_var3
        ).pack(anchor="w", pady=8, padx=10)

        ttk.Checkbutton(
            check_frame,
            text="4. [RF 무선망] Spectrum Analyzer 확인 시, 안테나 Tx 출력 파형이 공격 이전 정상 파형으로 돌아옴",
            variable=self.chk_var4
        ).pack(anchor="w", pady=8, padx=10)

        result_frame = ttk.Frame(check_frame)
        result_frame.pack(fill=tk.X, pady=25)

        self.btn_judge = tk.Button(
            result_frame,
            text="최종 판정 결과 산출",
            bg="#2c3e50",
            fg="white",
            font=("Malgun Gothic", 12, "bold"),
            command=self._evaluate_test_result
        )
        self.btn_judge.pack(side=tk.LEFT, padx=10, ipady=5, ipadx=10)

        self.lbl_final_result = ttk.Label(
            result_frame,
            text="체크리스트 작성 후 버튼을 클릭하세요.",
            font=("Malgun Gothic", 16, "bold"),
            foreground="gray"
        )
        self.lbl_final_result.pack(side=tk.LEFT, padx=20)

    def _evaluate_test_result(self):
        if self.chk_var1.get() and self.chk_var2.get() and self.chk_var3.get() and self.chk_var4.get():
            self.lbl_final_result.config(text="최종 판정: PASS (정상 방어 및 복구 완료)", foreground="blue")
        else:
            self.lbl_final_result.config(text="최종 판정: FAIL (치명적 비정상 발견 - 펌웨어 점검 요망)", foreground="red")

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

            trex_path_q = self._quote_remote(trex_path)
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
        threading.Thread(target=self._connect_server_bg, daemon=True).start()

    def _connect_server_bg(self):
        try:
            self.trex_ready = False
            self._safe_ui(self.lbl_server_status.config, text="상태: 연결 및 TRex 구성 중...", foreground="orange")

            if not self._validate_inputs(step=1):
                self._safe_ui(self.lbl_server_status.config, text="상태: 입력값 확인 필요", foreground="red")
                return

            ip = self.ent_server_ip.get().strip()
            user = self.ent_ssh_user.get().strip()
            pw = self.ent_ssh_pw.get()
            trex_path = self._validate_remote_path(self.ent_trex_path.get(), "TRex Path")
            selected_cores = self.combo_trex_cores.get().strip()

            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_client.connect(ip, username=user, password=pw, timeout=SSH_CONNECT_TIMEOUT)

            check_cmd = f"test -d {self._quote_remote(trex_path)} && echo OK || echo FAIL"
            out, err = self._safe_ssh_exec(check_cmd, timeout=10)
            if "OK" not in out:
                self._safe_ui(self.lbl_server_status.config, text="상태: TRex 경로 확인 실패", foreground="red")
                self._safe_ui(messagebox.showerror, "경로 오류", f"서버에서 TRex Path를 찾을 수 없습니다.\n{trex_path}")
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

        except Exception as e:
            self._safe_ui(self.lbl_server_status.config, text="상태: 연결 실패", foreground="red")
            self._safe_ui(messagebox.showerror, "Error", f"서버 연결에 실패했습니다.\n{e}")

    def _start_stats_stream(self):
        if not self.trex_ready or not self.ssh_client:
            return

        server_ip = self.ent_server_ip.get().strip()
        pw = self.ent_ssh_pw.get()
        trex_path = self._validate_remote_path(self.ent_trex_path.get(), "TRex Path")
        port_val = self.combo_trex_port.get().strip()
        port_list_str = "[0,1]" if "," in port_val else f"[{port_val}]"

        with self.monitor_lock:
            self.monitor_running = True

        stream_cmd = f"""
import sys, time
sys.path.insert(0, {repr(trex_path + "/automation/trex_control_plane/interactive")})
try:
    from trex.stl.api import STLClient
    c = STLClient(server={repr(server_ip)})
    connected = False
    while True:
        try:
            if not connected:
                c.connect()
                connected = True
                print("[INFO] TRex RPC connected")
                sys.stdout.flush()

            stats = c.get_stats(ports={port_list_str})
            global_stats = stats.get('global', {{}})

            for p in {port_list_str}:
                port_stats = stats.get(p, {{}})
                tx_bps = port_stats.get('tx_bps', 0)
                rx_bps = port_stats.get('rx_bps', 0)
                tx_pps = port_stats.get('tx_pps', 0)
                tx_pkts = port_stats.get('tx_pkts', 0)
                cpu_util = global_stats.get('cpu_util', 0)
                q_full = global_stats.get('queue_full', 0)

                print("-" * 60)
                print(f"Port {{p}} Stats:")
                print(f"  TX BPS       : {{tx_bps/1000000000:.3f}} Gbps")
                print(f"  RX BPS       : {{rx_bps/1000000000:.3f}} Gbps")
                print(f"  TX PPS       : {{tx_pps/1000000:.2f}} Mpps")
                print(f"  TX Packets   : {{tx_pkts}}")
                print(f"  CPU Util     : {{cpu_util:.1f}}%")
                print(f"  Queue Full   : {{q_full}}")

            sys.stdout.flush()
        except Exception as api_e:
            err_str = str(api_e)
            if "refused" in err_str.lower() or "connection" in err_str.lower():
                print("[WAIT] TRex RPC waiting")
                connected = False
                try:
                    c.disconnect()
                except Exception:
                    pass
            else:
                print(f"[ERROR] {{err_str[:80]}}")
            sys.stdout.flush()
        time.sleep(2)
except Exception as e:
    print(f"[FATAL] {{str(e)[:120]}}")
"""
        b64_mon = base64.b64encode(stream_cmd.encode("utf-8")).decode("ascii")
        remote_script = "/tmp/mon_stream.py"

        write_cmd = f"printf '%s' {self._quote_remote(b64_mon)} | base64 -d > {self._quote_remote(remote_script)}"
        self._safe_ssh_exec(write_cmd, password=pw)

        check_out, check_err = self._safe_ssh_exec(
            f"test -f {self._quote_remote(remote_script)} && echo OK || echo FAIL",
            password=pw
        )
        if "OK" not in check_out:
            self._update_status_text("모니터 스크립트 생성 실패")
            return

        def stream_loop():
            try:
                stdin, stdout, stderr = self.ssh_client.exec_command(
                    f"sudo -S python3 -u {self._quote_remote(remote_script)}",
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
                        self._safe_ui(self._update_status_text, clean_line)

            except Exception as e:
                self._safe_ui(self._update_status_text, f"모니터 에러: {str(e)[:80]}")

        threading.Thread(target=stream_loop, daemon=True).start()
        self._update_status_text("통계 모니터 시작됨")

    def _update_status_text(self, text):
        self.txt_stats.config(state=tk.NORMAL)
        lines = int(self.txt_stats.index("end-1c").split(".")[0])

        if lines > STATS_MAX_LINES:
            self.txt_stats.delete("1.0", "30.0")

        self.txt_stats.insert(tk.END, f"{text}\n")
        self.txt_stats.see(tk.END)
        self.txt_stats.config(state=tk.DISABLED)

    def _update_fping_text(self, text):
        self.txt_fping.config(state=tk.NORMAL)
        lines = int(self.txt_fping.index("end-1c").split(".")[0])

        if lines > FPING_MAX_LINES:
            self.txt_fping.delete("1.0", "40.0")

        self.txt_fping.insert(tk.END, f"{text}\n")
        self.txt_fping.see(tk.END)
        self.txt_fping.config(state=tk.DISABLED)

    def _refresh_pcap_list(self):
        if not self.ssh_client:
            return

        sftp = None
        try:
            sftp = self.ssh_client.open_sftp()
            pcap_dir = self._validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path")

            try:
                sftp.chdir(pcap_dir)
                files = [f for f in sftp.listdir() if f.endswith(".pcap")]
                self.list_pcap_files.delete(0, tk.END)
                for f in sorted(files, reverse=True):
                    self.list_pcap_files.insert(tk.END, f)
            except IOError:
                pw = self.ent_ssh_pw.get()
                mkdir_cmd = f"sudo -S mkdir -p {self._quote_remote(pcap_dir)}"
                self._safe_ssh_exec(mkdir_cmd, password=pw)
                self.list_pcap_files.delete(0, tk.END)

        except Exception:
            pass
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
            base_dir = self._validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path")

            success_count = 0
            for f in selected_files:
                try:
                    safe_file = self._sanitize_remote_filename(f)
                    target_file = posixpath.join(base_dir, safe_file)
                    cmd_list = ["sudo", "-S", "rm", "-f", target_file]
                    safe_cmd = " ".join(shlex.quote(arg) for arg in cmd_list)
                    out, err = self._safe_ssh_exec(safe_cmd, password=pw)
                    if not err or "No such file" in err or out is not None:
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
            desc += "O-RU IP를 정확히 기입하고 Port를 830으로 일치시키십시오."

        desc += "\n\n[추가 옵션 가이드]\n"
        desc += "- Standard Random: 64~1500 바이트 범위를 3등분하여 랜덤 크기 생성\n"
        desc += "- Jumbo Random: 64~9000 바이트 범위를 3등분하여 랜덤 크기 생성\n"
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
            elif "GTP" in atype:
                self.ent_dst_port.delete(0, tk.END)
                self.ent_dst_port.insert(0, "2152")

        safe_name = re.sub(r"[^a-zA-Z0-9]", "_", atype)
        safe_name = re.sub(r"_+", "_", safe_name).strip("_") + ".pcap"

        self.ent_pcap_name.delete(0, tk.END)
        self.ent_pcap_name.insert(0, safe_name)

        atype_upper = atype.upper()
        is_ecpri = any(k in atype_upper for k in ["U-PLANE", "C-PLANE", "PRACH"])
        is_l3l4 = any(k in atype_upper for k in ["NETCONF", "GTP"])

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

        self._calculate_pps(None)

    def _calculate_pps(self, event=None):
        try:
            rate_str = self.ent_rate.get().strip()
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
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 1번 탭에서 서버를 연결해 주세요.")
            return

        if not self._validate_inputs(step=1):
            return

        threading.Thread(target=self._build_pcap_bg, daemon=True).start()

    def _build_pcap_bg(self):
        try:
            pkt_mode = self.combo_pkt_mode.get().strip()

            config = {
                "attack_type": self.combo_attack.get(),
                "src_mac": self.ent_src_mac.get().strip(),
                "dst_mac": self.ent_dst_mac.get().strip(),
                "src_ip": self.ent_src_ip.get().strip(),
                "dst_ip": self.ent_dst_ip.get().strip(),
                "dst_port": self.ent_dst_port.get().strip(),
                "vlan_id": self.ent_vlan_id.get().strip(),
                "pkt_mode": pkt_mode,
                "spoofing": self.var_mutation_enable.get(),
                "mutation_enable": self.var_mutation_enable.get(),
                "rand_mac": self.var_rand_mac.get(),
                "rand_ip": self.var_rand_ip.get(),
                "rand_vlan": self.var_rand_vlan.get(),
                "rand_ethertype": self.var_rand_ethertype.get(),
                "malformed_ecpri": self.var_malformed_ecpri.get(),
                "invalid_length": self.var_invalid_length.get(),
                "rand_l4_port": self.var_rand_l4_port.get(),
                "pkt_size": self.ent_pkt_size.get().strip() if pkt_mode == "Fixed" else "",
                "rate": self.ent_rate.get().strip(),
                "pcap_ms": self.ent_pcap_ms.get().strip(),
                "pcap_path": self._validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path"),
                "pcap_name": self._sanitize_remote_filename(self.ent_pcap_name.get())
            }

            pw = self.ent_ssh_pw.get()
            b64_config = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")

            builder_script = r"""import sys, json, base64, os, struct, random
from scapy.all import Ether, Dot1Q, IP, TCP, UDP, Raw, PcapWriter

def random_ip():
    return ".".join(str(random.randint(1, 254)) for _ in range(4))

def pick_random_size(min_size, max_size):
    span = max_size - min_size + 1
    third = max(1, span // 3)

    b1_start = min_size
    b1_end = min(max_size, min_size + third - 1)

    b2_start = min(max_size, b1_end + 1)
    b2_end = min(max_size, b2_start + third - 1)

    b3_start = min(max_size, b2_end + 1)
    b3_end = max_size

    buckets = []
    if b1_start <= b1_end:
        buckets.append((b1_start, b1_end))
    if b2_start <= b2_end:
        buckets.append((b2_start, b2_end))
    if b3_start <= b3_end:
        buckets.append((b3_start, b3_end))

    selected = random.choice(buckets)
    return random.randint(selected[0], selected[1])

def resolve_pkt_size(config):
    mode = config.get('pkt_mode', 'Fixed')
    if mode == 'Standard Random':
        return pick_random_size(64, 1500)
    elif mode == 'Jumbo Random':
        return pick_random_size(64, 9000)
    return int(config.get('pkt_size', 64))

def build_packet(config, pkt_size):
    atype = config.get('attack_type', '').upper()
    src_mac = config.get('src_mac', '00:00:00:00:00:01')
    dst_mac = config.get('dst_mac', 'ff:ff:ff:ff:ff:ff')
    vlan_id = config.get('vlan_id', '')
    has_vlan = bool(vlan_id and str(vlan_id).strip().isdigit())
    l2_len = 18 if has_vlan else 14
    ecpri_payload_len = max(0, pkt_size - l2_len - 4)

    if has_vlan:
        vid = int(str(vlan_id).strip())
        l2_ecpri = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid, type=0xAEFE)
        l2_ptp = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid, type=0x88F7)
        l2_ip = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid)
    else:
        l2_ecpri = Ether(src=src_mac, dst=dst_mac, type=0xAEFE)
        l2_ptp = Ether(src=src_mac, dst=dst_mac, type=0x88F7)
        l2_ip = Ether(src=src_mac, dst=dst_mac)

    if 'PRACH' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        rtc_seq = struct.pack('!HH', 0x0001, 0x0000)
        oran_hdr = b'\x00\x00\x00\x00\x01\x03'
        pad_len = max(0, ecpri_payload_len - len(rtc_seq) - len(oran_hdr))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + rtc_seq + oran_hdr + (b'\x00' * pad_len))

    elif 'C-PLANE' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        rtc_seq = struct.pack('!HH', 0x0001, 0x0000)
        oran_hdr = b'\x80\x00\x00\x00\x01\x01'
        pad_len = max(0, ecpri_payload_len - len(rtc_seq) - len(oran_hdr))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + rtc_seq + oran_hdr + (b'\x00' * pad_len))

    elif 'U-PLANE' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x00, ecpri_payload_len)
        pc_seq = struct.pack('!HH', 0x0001, 0x0000)
        pad_len = max(0, ecpri_payload_len - len(pc_seq))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + pc_seq + (b'\x00' * pad_len))

    elif 'PTP' in atype:
        ptp_hdr = b'\x00\x02\x00\x2c' + b'\x00' * 40
        pad_len = max(0, pkt_size - l2_len - len(ptp_hdr))
        pkt = l2_ptp / Raw(load=ptp_hdr + (b'\x00' * pad_len))

    elif 'NETCONF' in atype or 'TCP' in atype:
        src_ip = config.get('src_ip', '192.168.11.100')
        dst_ip = config.get('dst_ip', '192.168.11.2')
        dst_port = int(config.get('dst_port', 830) or 830)
        base_pkt = l2_ip / IP(src=src_ip, dst=dst_ip) / TCP(dport=dst_port, flags='S')
        pad_len = max(0, pkt_size - len(base_pkt))
        pkt = base_pkt / Raw(load=b'\x00' * pad_len)

    elif 'GTP' in atype:
        src_ip = config.get('src_ip', '192.168.11.100')
        dst_ip = config.get('dst_ip', '192.168.11.2')
        base_pkt = l2_ip / IP(src=src_ip, dst=dst_ip) / UDP(dport=2152) / Raw(b'\x30\xff\x00\x14\x00\x00\x00\x00')
        pad_len = max(0, pkt_size - len(base_pkt))
        pkt = base_pkt / Raw(load=b'\x00' * pad_len)

    else:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        pkt = l2_ecpri / Raw(load=ecpri_hdr + (b'\x00' * ecpri_payload_len))

    return pkt

def apply_mutations(pkt, config):
    if not config.get('mutation_enable'):
        return pkt

    rand_mac = config.get('rand_mac')
    rand_ip_flag = config.get('rand_ip')
    rand_vlan = config.get('rand_vlan')
    rand_ethertype = config.get('rand_ethertype')
    malformed_ecpri = config.get('malformed_ecpri')
    invalid_length = config.get('invalid_length')
    rand_l4_port = config.get('rand_l4_port')

    if rand_mac and pkt.haslayer(Ether):
        pkt[Ether].src = "02:%02x:%02x:%02x:%02x:%02x" % (
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255)
        )

    if rand_vlan and pkt.haslayer(Dot1Q):
        pkt[Dot1Q].vlan = random.randint(1, 4094)

    if rand_ethertype:
        if pkt.haslayer(Dot1Q):
            pkt[Dot1Q].type = random.choice([0x1234, 0x88B5, 0x9999, 0xFFFF])
        elif pkt.haslayer(Ether):
            pkt[Ether].type = random.choice([0x1234, 0x88B5, 0x9999, 0xFFFF])

    if rand_ip_flag and pkt.haslayer(IP):
        pkt[IP].src = random_ip()
        del pkt[IP].chksum

    if rand_l4_port:
        if pkt.haslayer(TCP):
            pkt[TCP].sport = random.randint(1024, 65535)
            del pkt[TCP].chksum
        elif pkt.haslayer(UDP):
            pkt[UDP].sport = random.randint(1024, 65535)
            if hasattr(pkt[UDP], 'chksum'):
                del pkt[UDP].chksum

    raw_bytes = bytearray(bytes(pkt))

    if malformed_ecpri and len(raw_bytes) > 20:
        offset = 18 if pkt.haslayer(Dot1Q) else 14
        if len(raw_bytes) > offset + 4:
            raw_bytes[offset] = 0xFF
            raw_bytes[offset + 1] = 0xFF

    if invalid_length and len(raw_bytes) > 20:
        offset = 18 if pkt.haslayer(Dot1Q) else 14
        if len(raw_bytes) > offset + 4:
            raw_bytes[offset + 2] = 0xFF
            raw_bytes[offset + 3] = 0xFF

    return Ether(bytes(raw_bytes))

try:
    config = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
    rate_gbps = float(config['rate'])
    pcap_ms = float(config['pcap_ms'])

    sample_size = resolve_pkt_size(config)
    bytes_per_ms = (rate_gbps * 1000000000 / 8) / 1000
    num_pkts = int((bytes_per_ms / max(sample_size, 64)) * pcap_ms)

    if num_pkts < 1:
        num_pkts = 1

    full_path = os.path.join(config['pcap_path'], config['pcap_name'])

    if not os.path.exists(config['pcap_path']):
        os.makedirs(config['pcap_path'], exist_ok=True)

    writer = PcapWriter(full_path, append=False, sync=True)

    try:
        for _ in range(num_pkts):
            pkt_size = resolve_pkt_size(config)
            base_pkt = build_packet(config, pkt_size)
            final_pkt = apply_mutations(base_pkt, config)
            writer.write(final_pkt)
    finally:
        writer.close()

    print(json.dumps({'status': 'success', 'file': full_path, 'count': num_pkts}))

except Exception as e:
    print(json.dumps({'status': 'error', 'message': str(e)}))
"""
            b64_builder = base64.b64encode(builder_script.encode("utf-8")).decode("ascii")
            remote_builder = "/tmp/oran_builder.py"

            write_cmd = f"printf '%s' {self._quote_remote(b64_builder)} | base64 -d > {self._quote_remote(remote_builder)}"
            self._safe_ssh_exec(write_cmd, password=pw)

            check_out, check_err = self._safe_ssh_exec(
                f"test -f {self._quote_remote(remote_builder)} && echo OK || echo FAIL",
                password=pw
            )
            if "OK" not in check_out:
                self._safe_ui(messagebox.showerror, "생성 오류", "원격 빌더 스크립트 생성에 실패했습니다.")
                return

            run_cmd = f"sudo -S python3 {self._quote_remote(remote_builder)} {self._quote_remote(b64_config)}"
            out, err = self._safe_ssh_exec(run_cmd, password=pw, timeout=SSH_TIMEOUT)

            try:
                clean_json = ""
                for line in reversed(out.split("\n")):
                    if "{" in line and "}" in line:
                        clean_json = line[line.find("{"):line.rfind("}") + 1]
                        break

                res = json.loads(clean_json)
                if res.get("status") == "success":
                    pkts_created = res.get("count", 1)
                    self._safe_ui(
                        messagebox.showinfo,
                        "PCAP 생성 완료",
                        f"지정된 길이의 패킷 블록 생성이 성공적으로 완료되었습니다.\n\n경로: {res.get('file')}\n생성된 총 패킷 수: {pkts_created:,} 개"
                    )
                    self._safe_ui(self._refresh_pcap_list)
                else:
                    self._safe_ui(messagebox.showerror, "생성 오류", f"패킷 생성 중 오류가 발생했습니다.\n{res.get('message')}")
            except Exception:
                self._safe_ui(messagebox.showerror, "통신 오류", f"서버 통신 오류로 패킷 생성에 실패했습니다:\n{out}\n{err}")

        except Exception as e:
            self._safe_ui(messagebox.showerror, "예외 발생", f"실행 중 예외 발생: {str(e)}")

    def _get_selected_ports_expr(self):
        port_val = self.combo_trex_port.get().strip()
        return "[0,1]" if "," in port_val else f"[{port_val}]"

    def _read_remote_pid_file(self, pid_file):
        try:
            cmd = f"test -f {self._quote_remote(pid_file)} && cat {self._quote_remote(pid_file)} || true"
            out, err = self._safe_ssh_exec(cmd, timeout=10)
            pid = out.strip()
            if pid.isdigit():
                return pid
            return None
        except Exception:
            return None

    def _is_remote_pid_alive(self, pid):
        try:
            if not pid or not str(pid).isdigit():
                return False
            cmd = f"kill -0 {pid} >/dev/null 2>&1; echo $?"
            out, err = self._safe_ssh_exec(cmd, timeout=10)
            return out.strip() == "0"
        except Exception:
            return False

    def _stop_remote_pid(self, pid=None, pid_file=None, force=False):
        try:
            target_pid = pid
            if not target_pid and pid_file:
                target_pid = self._read_remote_pid_file(pid_file)

            if not target_pid or not str(target_pid).isdigit():
                return False

            sig = "-KILL" if force else "-TERM"
            self._safe_ssh_exec(f"kill {sig} {target_pid} >/dev/null 2>&1 || true", timeout=10)
            time.sleep(1)

            if self._is_remote_pid_alive(target_pid) and not force:
                self._safe_ssh_exec(f"kill -KILL {target_pid} >/dev/null 2>&1 || true", timeout=10)
                time.sleep(1)

            if pid_file:
                self._safe_ssh_exec(f"rm -f {self._quote_remote(pid_file)} >/dev/null 2>&1 || true", timeout=10)

            return True
        except Exception:
            return False

    def _read_remote_log_tail(self, log_file, lines=50):
        try:
            cmd = f"test -f {self._quote_remote(log_file)} && tail -n {int(lines)} {self._quote_remote(log_file)} || true"
            out, err = self._safe_ssh_exec(cmd, timeout=10)
            return (out + "\n" + err).strip()
        except Exception as e:
            return f"log read error: {e}"

    def _launch_remote_background_python(self, remote_script, sudo_password):
        remote_log = self._quote_remote(self.trex_log_file)
        remote_pid = self._quote_remote(self.trex_pid_file)
        remote_script_q = self._quote_remote(remote_script)

        safe_pw = sudo_password.replace("'", "'\"'\"'")

        cmd = (
            f"sh -c '"
            f"rm -f {remote_pid} {remote_log}; "
            f"nohup bash -lc "
            f"\"printf %s '{safe_pw}' | sudo -S -p '' python3 {remote_script_q}\" "
            f"> {remote_log} 2>&1 < /dev/null & "
            f"echo $! > {remote_pid}"
            f"'"
        )

        out, err = self._safe_ssh_exec(cmd, timeout=15)
        time.sleep(2)

        pid = self._read_remote_pid_file(self.trex_pid_file)
        if not pid:
            raise RuntimeError("원격 PID 파일 생성 실패")

        self.trex_remote_pid = pid
        return pid

    def _wait_for_trex_play_started(self, timeout=15):
        start_time = time.time()
        while time.time() - start_time < timeout:
            pid = self._read_remote_pid_file(self.trex_pid_file)
            if pid and self._is_remote_pid_alive(pid):
                log_tail = self._read_remote_log_tail(self.trex_log_file, lines=20)
                if "Traceback" not in log_tail and "[ERROR]" not in log_tail:
                    return True
            time.sleep(1)
        return False

    def play_traffic(self):
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 서버를 연결해 주십시오.")
            return

        if not self.trex_ready:
            messagebox.showwarning("경고", "TRex 엔진이 준비되지 않았습니다. 먼저 서버 연결을 확인해 주세요.")
            return

        if not self._validate_inputs(step=2):
            return

        selections = self.list_pcap_files.curselection()
        if not selections:
            messagebox.showwarning("경고", "전송할 대상 PCAP 파일을 선택해 주십시오.")
            return

        selected_pcap = self.list_pcap_files.get(selections[0])

        self.txt_stats.config(state=tk.NORMAL)
        self.txt_stats.delete("1.0", tk.END)
        self.txt_stats.config(state=tk.DISABLED)

        pw = self.ent_ssh_pw.get()
        rate_gbps = self.ent_rate.get().strip()

        duration_min = float(self.ent_duration_min.get())
        duration_sec = duration_min * 60

        target_rate_inner = f"echo '{rate_gbps} Gbps (Target)' > /tmp/trex_target_rate"
        target_rate_cmd = f"sudo -S sh -c {self._quote_remote(target_rate_inner)}"
        self._safe_ssh_exec(target_rate_cmd, password=pw)

        base_dir = self._validate_remote_path(self.ent_pcap_path.get(), "PCAP Save Path")
        safe_pcap = self._sanitize_remote_filename(selected_pcap)
        pcap_full = posixpath.join(base_dir, safe_pcap)

        port_val = self.combo_trex_port.get().strip()
        port_list_str = self._get_selected_ports_expr()
        rate_command = "100%" if rate_gbps == "25.0" else f"{rate_gbps}gbps"

        api_script = f"""
import sys
sys.path.insert(0, {repr(self._validate_remote_path(self.ent_trex_path.get(), "TRex Path") + "/automation/trex_control_plane/interactive")})
from trex.stl.api import STLClient, STLStream, STLPktBuilder, STLTXCont
from scapy.all import rdpcap

c = None
ports = {port_list_str}
try:
    c = STLClient(server='127.0.0.1')
    c.connect()
    c.acquire(ports=ports, force=True)
    c.reset(ports=ports)
    c.clear_stats()

    pkts = rdpcap({repr(pcap_full)})
    if not pkts or len(pkts) == 0:
        raise RuntimeError("PCAP 파일이 비어 있습니다.")

    pkt = pkts[0]
    stream = STLStream(packet=STLPktBuilder(pkt=pkt), mode=STLTXCont(pps=1))

    c.add_streams([stream], ports=ports)
    print("[INFO] add_streams ok")
    sys.stdout.flush()

    c.start(ports=ports, mult={repr(rate_command)}, duration={duration_sec})
    print("[INFO] start ok")
    sys.stdout.flush()

    if {duration_sec} > 0:
        c.wait_on_traffic(ports=ports)
        print("[INFO] traffic completed")
        sys.stdout.flush()

except Exception as e:
    print(f"[ERROR] {{e}}")
    sys.stdout.flush()
finally:
    try:
        if c and {duration_sec} > 0:
            c.release(ports=ports)
            c.disconnect()
    except Exception:
        pass
"""
        b64_api = base64.b64encode(api_script.encode("utf-8")).decode("ascii")
        remote_api = self.trex_script_file

        write_cmd = f"printf '%s' {self._quote_remote(b64_api)} | base64 -d > {self._quote_remote(remote_api)}"
        self._safe_ssh_exec(write_cmd, password=pw)

        check_out, check_err = self._safe_ssh_exec(
            f"test -f {self._quote_remote(remote_api)} && echo OK || echo FAIL",
            password=pw
        )
        if "OK" not in check_out:
            messagebox.showerror("전송 실패", "원격 TRex 실행 스크립트 생성에 실패했습니다.")
            return

        self._stop_remote_pid(pid_file=self.trex_pid_file, force=True)

        def run():
            try:
                pid = self._launch_remote_background_python(remote_api, pw)
                started = self._wait_for_trex_play_started(timeout=10)

                if not started:
                    log_tail = self._read_remote_log_tail(self.trex_log_file, lines=80)
                    self._safe_ui(
                        messagebox.showerror,
                        "전송 실패",
                        f"TRex play 시작 확인 실패\n\n{log_tail}"
                    )
                    return

                log_tail = self._read_remote_log_tail(self.trex_log_file, lines=20)
                if "[ERROR]" in log_tail or "Traceback" in log_tail:
                    self._safe_ui(
                        messagebox.showerror,
                        "전송 실패",
                        f"명령 수행 중 오류가 발생했습니다:\n\n{log_tail}"
                    )
            except Exception as e:
                self._safe_ui(messagebox.showerror, "전송 실패", f"실행 중 오류가 발생했습니다.\n{e}")

        threading.Thread(target=run, daemon=True).start()

        duration_msg = "중지 명령 전까지 무한 전송" if duration_sec == 0 else f"{duration_min} 분"
        messagebox.showinfo("전송 시작", f"포트 {port_val} 할당 완료.\n트래픽 인가를 시작합니다. (설정 시간: {duration_msg})")

    def stop_traffic(self):
        if not self.ssh_client:
            return

        self._update_status_text("트래픽 전송 중지 명령 하달...")
        threading.Thread(target=self._stop_bg, daemon=True).start()

    def _stop_bg(self):
        try:
            pw = self.ent_ssh_pw.get()
            stop_rate_cmd = "sudo -S sh -c " + self._quote_remote("echo '0 Gbps (STOP)' > /tmp/trex_target_rate")
            self._safe_ssh_exec(stop_rate_cmd, password=pw)

            self._stop_remote_pid(pid=self.trex_remote_pid, pid_file=self.trex_pid_file, force=False)
            self.trex_remote_pid = None

            port_list_str = self._get_selected_ports_expr()
            trex_path = self._validate_remote_path(self.ent_trex_path.get(), "TRex Path")

            stop_script = f"""
import sys
sys.path.insert(0, {repr(trex_path + "/automation/trex_control_plane/interactive")})
from trex.stl.api import STLClient

ports = {port_list_str}

try:
    c = STLClient(server='127.0.0.1')
    c.connect()
    c.acquire(ports=ports, force=True)
    c.stop(ports=ports)
    c.clear_stats()
    c.release(ports=ports)
    c.disconnect()
    print("[INFO] stop ok")
except Exception as e:
    print(f"[ERROR] {{e}}")
"""
            b64_stop = base64.b64encode(stop_script.encode("utf-8")).decode("ascii")
            remote_stop = "/tmp/stop_trex.py"
            write_cmd = f"printf '%s' {self._quote_remote(b64_stop)} | base64 -d > {self._quote_remote(remote_stop)}"
            self._safe_ssh_exec(write_cmd, password=pw)
            self._safe_ssh_exec(f"sudo -S python3 {self._quote_remote(remote_stop)}", password=pw)

        except Exception as e:
            self._safe_ui(self._update_status_text, f"중지 실패: {str(e)[:80]}")

    def _append_fping_line(self, text):
        self._safe_ui(self._update_fping_text, text)

    def _run_single_reachability_probe(self, target, interval_sec, size):
        try:
            pw = self.ent_ssh_pw.get()
            interval_ms = max(100, int(interval_sec * 1000))

            which_cmd = "command -v fping >/dev/null 2>&1 && echo FPING || echo PING"
            out, err = self._safe_ssh_exec(which_cmd, password=pw, timeout=10)
            tool = out.strip()

            if tool == "FPING":
                cmd = f"fping -D -c 1 -t {interval_ms} -b {size} {shlex.quote(target)} 2>&1"
            else:
                timeout_sec = max(1, int(interval_sec) + 1)
                cmd = f"ping -c 1 -W {timeout_sec} -s {size} {shlex.quote(target)} 2>&1"

            out, err = self._safe_ssh_exec(cmd, password=pw, timeout=max(5, int(interval_sec) + 5))
            result = (out + "\n" + err).strip()
            return result if result else f"[{time.strftime('%H:%M:%S')}] no output"
        except Exception as e:
            return f"[{time.strftime('%H:%M:%S')}] probe error: {e}"

    def start_reachability_monitor(self):
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 서버를 연결해 주세요.")
            return

        target = self.ent_fping_target.get().strip()
        interval = self.ent_fping_interval.get().strip()
        size = self.ent_fping_size.get().strip()

        if not target or not self._is_valid_ip(target):
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
            if self.fping_running:
                self._update_fping_text("Reachability monitor already running.")
                return
            self.fping_running = True

        self.reachability_stop_event.clear()
        self._update_fping_text("Reachability monitor starting...")

        self.reachability_thread = threading.Thread(target=self._reachability_monitor_bg, daemon=True)
        self.reachability_thread.start()

    def stop_reachability_monitor(self):
        with self.fping_lock:
            self.fping_running = False
        self.reachability_stop_event.set()
        self._update_fping_text("Reachability monitor stop requested.")

    def _reachability_monitor_bg(self):
        try:
            target = self.ent_fping_target.get().strip()
            interval = float(self.ent_fping_interval.get().strip())
            size = int(self.ent_fping_size.get().strip())

            pw = self.ent_ssh_pw.get()
            which_cmd = "command -v fping >/dev/null 2>&1 && echo FPING || echo PING"
            out, err = self._safe_ssh_exec(which_cmd, password=pw, timeout=10)
            has_fping = (out.strip() == "FPING")

            if has_fping:
                self._append_fping_line("fping detected on server. Using single-shot fping probe.")
            else:
                self._append_fping_line("fping not found. Using single-shot ping fallback.")

            while True:
                with self.fping_lock:
                    if not self.fping_running:
                        break

                if self.reachability_stop_event.is_set():
                    break

                result = self._run_single_reachability_probe(target, interval, size)
                for line in result.splitlines():
                    clean_line = line.strip()
                    if clean_line and not clean_line.startswith("[sudo]"):
                        self._append_fping_line(clean_line)

                end_time = time.time() + max(0.1, interval)
                while time.time() < end_time:
                    with self.fping_lock:
                        if not self.fping_running:
                            return
                    if self.reachability_stop_event.is_set():
                        return
                    time.sleep(0.1)

        except Exception as e:
            self._safe_ui(self._update_fping_text, f"Reachability monitor error: {str(e)[:100]}")
        finally:
            with self.fping_lock:
                self.fping_running = False
            self._safe_ui(self._update_fping_text, "Reachability monitor stopped.")

    def _load_config(self):
        fields = {
            "server_ip": self.ent_server_ip,
            "ssh_user": self.ent_ssh_user,
            "ssh_pw": self.ent_ssh_pw,
            "trex_path": self.ent_trex_path,
            "src_mac": self.ent_src_mac,
            "dst_mac": self.ent_dst_mac,
            "src_ip": self.ent_src_ip,
            "dst_ip": self.ent_dst_ip,
            "dst_port": self.ent_dst_port,
            "vlan_id": self.ent_vlan_id,
            "pcap_path": self.ent_pcap_path,
            "pcap_name": self.ent_pcap_name,
            "pkt_size": self.ent_pkt_size,
            "rate": self.ent_rate,
            "pcap_ms": self.ent_pcap_ms,
            "duration_min": self.ent_duration_min,
            "fping_target": self.ent_fping_target,
            "fping_interval": self.ent_fping_interval,
            "fping_size": self.ent_fping_size
        }

        defaults = {
            "server_ip": "192.168.9.249",
            "ssh_user": "slab",
            "ssh_pw": "",
            "trex_path": "/home/slab/trex/v3.08",
            "trex_port": "0",
            "trex_cores": "6",
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
            "attack_type": "eCPRI U-Plane (대역폭/RRC 과부하)",
            "mutation_enable": False,
            "rand_mac": False,
            "rand_ip": False,
            "rand_vlan": False,
            "rand_ethertype": False,
            "malformed_ecpri": False,
            "invalid_length": False,
            "rand_l4_port": False,
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
            entry.delete(0, tk.END)
            entry.insert(0, defaults.get(key, ""))

        self.combo_attack.set(defaults.get("attack_type", "eCPRI U-Plane (대역폭/RRC 과부하)"))
        self.combo_trex_port.set(defaults.get("trex_port", "0"))
        self.combo_trex_cores.set(defaults.get("trex_cores", "6"))
        self.combo_pkt_mode.set(defaults.get("pkt_mode", "Fixed"))

        self.var_mutation_enable.set(defaults.get("mutation_enable", False))
        self.var_rand_mac.set(defaults.get("rand_mac", False))
        self.var_rand_ip.set(defaults.get("rand_ip", False))
        self.var_rand_vlan.set(defaults.get("rand_vlan", False))
        self.var_rand_ethertype.set(defaults.get("rand_ethertype", False))
        self.var_malformed_ecpri.set(defaults.get("malformed_ecpri", False))
        self.var_invalid_length.set(defaults.get("invalid_length", False))
        self.var_rand_l4_port.set(defaults.get("rand_l4_port", False))

        self._toggle_mutation_options()
        self._on_pkt_mode_changed()
        self._update_test_description(None)

    def _save_config(self):
        config = {
            "server_ip": self.ent_server_ip.get(),
            "ssh_user": self.ent_ssh_user.get(),
            "ssh_pw": self.ent_ssh_pw.get(),
            "trex_path": self.ent_trex_path.get(),
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
            "rate": self.ent_rate.get(),
            "pcap_ms": self.ent_pcap_ms.get(),
            "duration_min": self.ent_duration_min.get(),
            "attack_type": self.combo_attack.get(),
            "trex_port": self.combo_trex_port.get(),
            "trex_cores": self.combo_trex_cores.get(),
            "mutation_enable": self.var_mutation_enable.get(),
            "rand_mac": self.var_rand_mac.get(),
            "rand_ip": self.var_rand_ip.get(),
            "rand_vlan": self.var_rand_vlan.get(),
            "rand_ethertype": self.var_rand_ethertype.get(),
            "malformed_ecpri": self.var_malformed_ecpri.get(),
            "invalid_length": self.var_invalid_length.get(),
            "rand_l4_port": self.var_rand_l4_port.get(),
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

        with self.monitor_lock:
            self.monitor_running = False

        with self.fping_lock:
            self.fping_running = False

        self.reachability_stop_event.set()

        try:
            self._stop_remote_pid(pid=self.trex_remote_pid, pid_file=self.trex_pid_file, force=True)
        except Exception:
            pass

        if self.ssh_client:
            try:
                self.ssh_client.close()
            except Exception:
                pass

        if self.trex_server_ssh:
            try:
                self.trex_server_ssh.close()
            except Exception:
                pass

        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ORanValidationGUI(root)
    root.mainloop()

# python -m PyInstaller --noconsole --onefile --icon="DDOS.ico" oran_trex_master.py