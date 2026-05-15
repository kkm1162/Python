#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
O-RAN O-RU DDoS Validation System v5.5 (Stability & Deadlock Fix)
- [BugFix] PLAY 후 STOP 시 발생하는 GUI Freeze(Deadlock) 비동기 스레딩으로 완벽 해결
- [BugFix] 리스트박스(Listbox) 연동 오류 수정 및 PCAP 다중 선택(Multi-select) 일괄 삭제 완벽 구현
- [BugFix] Windows -> Linux 경로 전송 시 백슬래시(\)로 인한 삭제 실패 버그 수정
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import paramiko
import threading
import json
import os
import time
import base64
import subprocess
import re
import shlex

CONFIG_FILE = "oran_ru_config.json"

class ORanValidationGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("O-RAN O-RU DDoS Validation System v5.5")
        self.root.geometry("1100x950")
        
        self.ssh_client = None
        self.trex_server_ssh = None
        self.monitor_running = False
        self.trex_ready = False
        
        self.ssh_lock = threading.Lock()
        
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        self._create_notebook()
        self._load_config()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _safe_ssh_exec(self, command, password=None, timeout=30):
        with self.ssh_lock:
            try:
                stdin, stdout, stderr = self.ssh_client.exec_command(command, get_pty=True, timeout=timeout)
                if password:
                    stdin.write(password + "\n")
                    stdin.flush()
                
                output = stdout.read().decode('utf-8', errors='replace')
                error = stderr.read().decode('utf-8', errors='replace')
                return output, error
            except Exception as e:
                return "", str(e)

    def _validate_inputs(self):
        errors = []
        try:
            pkt_size = int(self.ent_pkt_size.get())
            if not (64 <= pkt_size <= 1518): errors.append("패킷 크기: 64~1518 바이트")
        except: errors.append("패킷 크기: 숫자 입력")
        
        try:
            rate = float(self.ent_rate.get())
            if not (0.1 <= rate <= 25.0): errors.append("속도: 0.1~25.0 Gbps")
        except: errors.append("속도: 숫자 입력")
        
        try:
            duration = int(self.ent_duration.get())
            if duration <= 0: errors.append("지속시간: 1초 이상")
        except: errors.append("지속시간: 숫자 입력")
        
        if errors:
            messagebox.showerror("입력 오류", "\n".join(errors))
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
        self.notebook.add(self.tab_ru_attack, text=" 2. RU & 공격 설정 ")
        self.notebook.add(self.tab_control, text=" 3. TRex 발사 통제소 ")
        self.notebook.add(self.tab_validation, text=" 4. ✅ 판정 및 검증 ")

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
        self.combo_trex_port = ttk.Combobox(frame, values=["0", "1", "0, 1"], width=37, state="readonly")
        self.combo_trex_port.grid(row=4, column=1, padx=10, pady=5)

        ttk.Label(frame, text="TRex Cores (-c):").grid(row=5, column=0, sticky="w", pady=5)
        self.combo_trex_cores = ttk.Combobox(frame, values=["2", "4", "6"], width=37, state="readonly")
        self.combo_trex_cores.grid(row=5, column=1, padx=10, pady=5)

        self.btn_connect = tk.Button(frame, text="서버 연결 및 TRex 엔진 구동", bg="#3498db", fg="white", 
                                     font=("Malgun Gothic", 10, "bold"), command=self.connect_server)
        self.btn_connect.grid(row=6, column=0, columnspan=2, pady=20, ipadx=20, ipady=5)

        self.lbl_server_status = ttk.Label(frame, text="상태: 연결 대기 중...", foreground="gray")
        self.lbl_server_status.grid(row=7, column=0, columnspan=2)

    def _build_ru_attack_tab(self):
        ru_frame = ttk.LabelFrame(self.tab_ru_attack, text="Target RU Network Configuration", padding=15)
        ru_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(ru_frame, text="Attacker MAC:").grid(row=0, column=0, sticky="w", pady=2)
        self.ent_src_mac = ttk.Entry(ru_frame, width=25)
        self.ent_src_mac.grid(row=0, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="O-RU MAC:").grid(row=0, column=2, sticky="w", pady=2, padx=(20,0))
        self.ent_dst_mac = ttk.Entry(ru_frame, width=25)
        self.ent_dst_mac.grid(row=0, column=3, padx=10, pady=2)

        ttk.Label(ru_frame, text="Attacker IP:").grid(row=1, column=0, sticky="w", pady=2)
        self.ent_src_ip = ttk.Entry(ru_frame, width=25)
        self.ent_src_ip.grid(row=1, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="O-RU IP:").grid(row=1, column=2, sticky="w", pady=2, padx=(20,0))
        self.ent_dst_ip = ttk.Entry(ru_frame, width=25)
        self.ent_dst_ip.grid(row=1, column=3, padx=10, pady=2)

        ttk.Label(ru_frame, text="Destination Port:").grid(row=2, column=0, sticky="w", pady=2)
        self.ent_dst_port = ttk.Entry(ru_frame, width=25)
        self.ent_dst_port.grid(row=2, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="VLAN ID (Optional):").grid(row=2, column=2, sticky="w", pady=2, padx=(20,0))
        self.ent_vlan_id = ttk.Entry(ru_frame, width=25)
        self.ent_vlan_id.grid(row=2, column=3, padx=10, pady=2)

        ttk.Label(ru_frame, text="PCAP Save Path:").grid(row=3, column=0, sticky="w", pady=2)
        self.ent_pcap_path = ttk.Entry(ru_frame, width=25)
        self.ent_pcap_path.grid(row=3, column=1, padx=10, pady=2)

        ttk.Label(ru_frame, text="PCAP File Name:").grid(row=3, column=2, sticky="w", pady=2, padx=(20,0))
        self.ent_pcap_name = ttk.Entry(ru_frame, width=25)
        self.ent_pcap_name.grid(row=3, column=3, padx=10, pady=2)

        atk_frame = ttk.LabelFrame(self.tab_ru_attack, text="DDoS Attack Parameters", padding=15)
        atk_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        ttk.Label(atk_frame, text="Attack Type:").grid(row=0, column=0, sticky="w", pady=5)
        
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

        ttk.Label(atk_frame, text="Packet Size (Bytes):").grid(row=1, column=0, sticky="w", pady=5)
        self.ent_pkt_size = ttk.Entry(atk_frame, width=15)
        self.ent_pkt_size.grid(row=1, column=1, padx=10, pady=5, sticky="w")
        self.ent_pkt_size.bind("<KeyRelease>", self._calculate_pps)
        
        ttk.Label(atk_frame, text="Line Rate (Gbps):").grid(row=2, column=0, sticky="w", pady=5)
        self.ent_rate = ttk.Entry(atk_frame, width=15)
        self.ent_rate.grid(row=2, column=1, padx=10, pady=5, sticky="w")
        self.ent_rate.bind("<KeyRelease>", self._calculate_pps)
        
        ttk.Label(atk_frame, text="Duration (Sec):").grid(row=3, column=0, sticky="w", pady=5)
        self.ent_duration = ttk.Entry(atk_frame, width=15)
        self.ent_duration.grid(row=3, column=1, padx=10, pady=5, sticky="w")

        sim_frame = ttk.LabelFrame(atk_frame, text="Expected Throughput Simulation", padding="10")
        sim_frame.grid(row=4, column=0, columnspan=2, pady=10, sticky="ew")

        self.lbl_max_limit = ttk.Label(sim_frame, text="L2 Line Rate Limit: -", foreground="purple", font=("Malgun Gothic", 9, "bold"))
        self.lbl_max_limit.pack(anchor="w", padx=5, pady=2)

        self.lbl_pps_calc = ttk.Label(sim_frame, text="Packets/sec: -", foreground="blue")
        self.lbl_pps_calc.pack(anchor="w", padx=5, pady=2)
        
        self.lbl_gbps_calc = ttk.Label(sim_frame, text="Expected L1 Throughput: -", foreground="blue")
        self.lbl_gbps_calc.pack(anchor="w", padx=5, pady=2)
        
        self.lbl_line_calc = ttk.Label(sim_frame, text="L1 Line Rate Usage: -", foreground="blue")
        self.lbl_line_calc.pack(anchor="w", padx=5, pady=2)

        ttk.Label(atk_frame, text="[시험 설정 가이드]").grid(row=0, column=2, sticky="nw", padx=20)
        
        self.txt_desc = tk.Text(atk_frame, width=55, height=12, bg="#e8f6f3", font=("Malgun Gothic", 9), wrap=tk.WORD)
        self.txt_desc.grid(row=1, column=2, rowspan=4, padx=20, pady=5, sticky="nsew")
        self.txt_desc.config(state=tk.DISABLED)

        action_frame = ttk.Frame(atk_frame)
        action_frame.grid(row=5, column=0, columnspan=3, pady=15, sticky="ew")

        self.btn_build_pcap = tk.Button(action_frame, text="1단계: 공격 패킷 생성 (PCAP Build)", 
                                        bg="#f39c12", fg="white", font=("Malgun Gothic", 11, "bold"), 
                                        command=self.build_pcap)
        self.btn_build_pcap.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=20, ipady=10)

    def _build_control_tab(self):
        pcap_frame = ttk.LabelFrame(self.tab_control, text="PCAP File Management", padding=10)
        pcap_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(pcap_frame, text="Available PCAP Files:").pack(side=tk.LEFT, padx=5)
        
        # ✅ 다중 선택(Multi-select) 리스트박스 적용
        list_frame = ttk.Frame(pcap_frame)
        list_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        self.list_pcap_files = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=4, exportselection=False)
        self.list_pcap_files.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.list_pcap_files.yview)
        self.list_pcap_files.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.btn_refresh_pcap = ttk.Button(pcap_frame, text="🔄 Refresh Files", command=self._refresh_pcap_list)
        self.btn_refresh_pcap.pack(side=tk.LEFT, padx=5)

        # ✅ 일괄 삭제 버튼
        self.btn_delete_pcap = ttk.Button(pcap_frame, text="🗑️ Delete Selected", command=self.delete_pcap)
        self.btn_delete_pcap.pack(side=tk.LEFT, padx=5)

        ctrl_frame = ttk.Frame(self.tab_control)
        ctrl_frame.pack(fill=tk.X, padx=10, pady=10)

        spoof_frame = ttk.Frame(ctrl_frame)
        spoof_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.var_spoofing = tk.BooleanVar(value=True)
        self.chk_spoofing = ttk.Checkbutton(spoof_frame, text="🛡️ Enable Source Spoofing (무작위 MAC 주소로 장비 회피)", variable=self.var_spoofing)
        self.chk_spoofing.pack(side=tk.LEFT, padx=5)

        btn_frame = ttk.Frame(ctrl_frame)
        btn_frame.pack(fill=tk.X)

        self.btn_play = tk.Button(btn_frame, text="▶ 2단계: 실제 트래픽 발사 (PLAY)", 
                                  bg="#27ae60", fg="white", font=("Arial", 16, "bold"), 
                                  command=self.play_traffic)
        self.btn_play.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=10, ipady=15)

        self.btn_stop = tk.Button(btn_frame, text="⏹ 발사 긴급 중지 (STOP)", 
                                  bg="#c0392b", fg="white", font=("Arial", 16, "bold"), 
                                  command=self.stop_traffic)
        self.btn_stop.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=10, ipady=15)

        stat_frame = ttk.LabelFrame(self.tab_control, text="실시간 DPDK 엔진 통계 모니터 (자동 스트리밍)", padding=10)
        stat_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.txt_stats = scrolledtext.ScrolledText(stat_frame, bg="black", fg="#00ff00", font=("Consolas", 11))
        self.txt_stats.pack(fill=tk.BOTH, expand=True)

    def _build_validation_tab(self):
        criteria_frame = ttk.LabelFrame(self.tab_validation, text="[ O-RU DDoS 내성 평가 기준 (Pass / Fail) ]", padding=15)
        criteria_frame.pack(fill=tk.X, padx=10, pady=10)

        pass_desc = """✅ 합격 (PASS) 조건 : 정상적인 방어 및 복구
- 공격 중: CPU 부하 100% 도달, 통신 지연, 패킷 Drop 등은 물리적 한계로 정상입니다.
- 공격 후: 트래픽 중단 즉시 O-RU가 스스로 버퍼를 비우고 O-DU와의 통신 및 무선 RF 방사를 정상 상태로 완벽히 복구해야 합니다."""

        fail_desc = """❌ 불합격 (FAIL) 조건 : 치명적 비정상 상태
- 공격 중: 장비 전원 꺼짐, 재부팅(Reboot), 시스템 멈춤(Hang), Watchdog Timeout 발생 시 불합격입니다.
- 공격 후: 트래픽이 멈췄음에도 영구적으로 통신 불능에 빠져 수동으로 장비를 껐다 켜야만 복구되는 경우 불합격입니다."""

        ttk.Label(criteria_frame, text=pass_desc, foreground="green", font=("Malgun Gothic", 10, "bold"), justify=tk.LEFT).pack(anchor="w", pady=5)
        ttk.Label(criteria_frame, text=fail_desc, foreground="red", font=("Malgun Gothic", 10, "bold"), justify=tk.LEFT).pack(anchor="w", pady=5)

        check_frame = ttk.LabelFrame(self.tab_validation, text="[ 발사 종료 후 필수 확인 체크리스트 ]", padding=15)
        check_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.chk_var1 = tk.BooleanVar(value=False)
        self.chk_var2 = tk.BooleanVar(value=False)
        self.chk_var3 = tk.BooleanVar(value=False)
        self.chk_var4 = tk.BooleanVar(value=False)

        ttk.Checkbutton(check_frame, text="1. [시스템 로그] UART 콘솔에 OOM(Out of Memory), Kernel Panic 등 치명적 에러가 발생하지 않음", variable=self.chk_var1).pack(anchor="w", pady=8, padx=10)
        ttk.Checkbutton(check_frame, text="2. [관리 제어망] NMS에서 O-RU로 NETCONF / SSH 통신이 응답하며 제어권이 살아있음", variable=self.chk_var2).pack(anchor="w", pady=8, padx=10)
        ttk.Checkbutton(check_frame, text="3. [프론트홀망] O-DU와의 PTP Clock Lock 상태 및 C/U-Plane 세션이 정상 복구됨", variable=self.chk_var3).pack(anchor="w", pady=8, padx=10)
        ttk.Checkbutton(check_frame, text="4. [RF 무선망] Spectrum Analyzer 확인 시, 안테나 Tx 출력 파형이 공격 이전 정상 파형으로 돌아옴", variable=self.chk_var4).pack(anchor="w", pady=8, padx=10)

        result_frame = ttk.Frame(check_frame)
        result_frame.pack(fill=tk.X, pady=25)

        self.btn_judge = tk.Button(result_frame, text="✅ 최종 판정 결과 산출", bg="#2c3e50", fg="white", font=("Malgun Gothic", 12, "bold"), command=self._evaluate_test_result)
        self.btn_judge.pack(side=tk.LEFT, padx=10, ipady=5, ipadx=10)

        self.lbl_final_result = ttk.Label(result_frame, text="체크리스트 작성 후 버튼을 클릭하세요.", font=("Malgun Gothic", 16, "bold"), foreground="gray")
        self.lbl_final_result.pack(side=tk.LEFT, padx=20)

    def _evaluate_test_result(self):
        if self.chk_var1.get() and self.chk_var2.get() and self.chk_var3.get() and self.chk_var4.get():
            self.lbl_final_result.config(text="✅ 최종 판정: PASS (정상 방어 및 복구 완료)", foreground="blue")
        else:
            self.lbl_final_result.config(text="❌ 최종 판정: FAIL (치명적 비정상 발견 - 펌웨어 점검 요망)", foreground="red")

    def _run_trex_server_terminal(self, ip, user, pw, trex_path, cores):
        try:
            self.trex_server_ssh = paramiko.SSHClient()
            self.trex_server_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.trex_server_ssh.connect(ip, username=user, password=pw, timeout=10)
            
            stdin, stdout, stderr = self.trex_server_ssh.exec_command(f"sudo -S pkill -f 't-rex-64'", get_pty=True)
            stdin.write(pw + "\n")
            stdin.flush()
            time.sleep(2)
            
            cmd = f"cd {trex_path} && sudo -S ./t-rex-64 -i -c {cores}"
            stdin, stdout, stderr = self.trex_server_ssh.exec_command(cmd, get_pty=True)
            stdin.write(pw + "\n")
            stdin.flush()
            
            time.sleep(5)
            verify_cmd = "ps aux | grep 't-rex-64' | grep -v grep"
            stdin2, stdout2, stderr2 = self.trex_server_ssh.exec_command(verify_cmd)
            output = stdout2.read().decode().strip()
            
            if output:
                self.trex_ready = True
                self.root.after(0, lambda: self.lbl_server_status.config(text="상태: TRex 엔진 동작 중 ✅", foreground="green"))
            else:
                self.root.after(0, lambda: self.lbl_server_status.config(text="상태: TRex 구동 실패 ❌", foreground="red"))
                return
            for line in iter(stdout.readline, ""): pass
        except Exception as e:
            self.root.after(0, lambda: self.lbl_server_status.config(text=f"상태: 터미널 실패 - {str(e)[:30]} ❌", foreground="red"))

    def connect_server(self):
        threading.Thread(target=self._connect_server_bg, daemon=True).start()

    def _connect_server_bg(self):
        try:
            self.lbl_server_status.config(text="상태: 연결 및 TRex 구성 중...", foreground="orange")
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_client.connect(self.ent_server_ip.get(), username=self.ent_ssh_user.get(), password=self.ent_ssh_pw.get(), timeout=5)
            
            ip, user, pw = self.ent_server_ip.get(), self.ent_ssh_user.get(), self.ent_ssh_pw.get()
            trex_path, selected_cores = self.ent_trex_path.get(), self.combo_trex_cores.get()
            
            threading.Thread(target=self._run_trex_server_terminal, args=(ip, user, pw, trex_path, selected_cores), daemon=True).start()
            
            wait_count = 0
            while not self.trex_ready and wait_count < 15:
                time.sleep(1)
                wait_count += 1
            if not self.trex_ready:
                messagebox.showwarning("경고", "TRex 엔진 구동에 실패했습니다. 서버 상태를 확인하세요.")
                return
            self._refresh_pcap_list()
            self._start_stats_stream()
        except Exception as e:
            self.lbl_server_status.config(text="상태: 연결 실패 ❌", foreground="red")
            messagebox.showerror("Error", f"서버 연결에 실패했습니다.\n{e}")

    def _start_stats_stream(self):
        if not self.trex_ready: return
        server_ip, pw, trex_path = self.ent_server_ip.get(), self.ent_ssh_pw.get(), self.ent_trex_path.get()
        port_val = self.combo_trex_port.get()
        port_list_str = "[0, 1]" if "," in port_val else f"[{port_val}]"
        
        self.monitor_running = True
        stream_cmd = f"""
import sys, time, os
sys.path.insert(0, '{trex_path}/automation/trex_control_plane/interactive')
try:
    from trex.stl.api import STLClient
    c = STLClient(server='{server_ip}')
    connected = False
    while True:
        try:
            if not connected:
                c.connect()
                connected = True
                print("[INFO] TRex RPC 연결 성공!")
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
                print("[WAIT] TRex RPC 연결 대기 중...")
                connected = False
                try: c.disconnect()
                except: pass
            else: print(f"[ERROR] {{err_str[:50]}}")
            sys.stdout.flush()
        time.sleep(2)
except Exception as e: print(f"[FATAL] {{str(e)[:80]}}")
"""
        b64_mon = base64.b64encode(stream_cmd.encode()).decode()
        
        # 모니터링 백그라운드 스레드는 안전락(Lock) 없이 별도 실행
        self.ssh_client.exec_command(f"echo '{b64_mon}' | base64 -d > /tmp/mon_stream.py")
        
        def stream_loop():
            try:
                stdin, stdout, stderr = self.ssh_client.exec_command("sudo -S python3 -u /tmp/mon_stream.py", get_pty=True)
                stdin.write(pw + "\n")
                stdin.flush()
                for line in iter(stdout.readline, ""):
                    if not self.monitor_running: break
                    clean_line = line.strip()
                    if clean_line and not clean_line.startswith("[sudo]"):
                        self.root.after(0, self._update_status_text, clean_line)
            except Exception as e: self.root.after(0, self._update_status_text, f"❌ 모니터 에러: {str(e)[:50]}")
        
        threading.Thread(target=stream_loop, daemon=True).start()
        self._update_status_text("✅ 통계 모니터 시작됨")

    def _update_status_text(self, text):
        self.txt_stats.config(state=tk.NORMAL)
        lines = int(self.txt_stats.index('end-1c').split('.')[0])
        if lines > 200: self.txt_stats.delete('1.0', '30.0')
        self.txt_stats.insert(tk.END, f"{text}\n")
        self.txt_stats.see(tk.END)
        self.txt_stats.config(state=tk.DISABLED)

    # ✅ 리스트박스로 완벽하게 교체된 새로고침 로직
    def _refresh_pcap_list(self):
        if not self.ssh_client: return
        try:
            sftp = self.ssh_client.open_sftp()
            pcap_dir = self.ent_pcap_path.get()
            try:
                sftp.chdir(pcap_dir)
                files = [f for f in sftp.listdir() if f.endswith('.pcap')]
                self.list_pcap_files.delete(0, tk.END)
                for f in sorted(files, reverse=True):
                    self.list_pcap_files.insert(tk.END, f)
            except IOError:
                pw = self.ent_ssh_pw.get()
                self._safe_ssh_exec(f"sudo -S mkdir -p {pcap_dir}", password=pw)
                self.list_pcap_files.delete(0, tk.END)
            sftp.close()
        except Exception: pass

    # ✅ PCAP 파일 일괄 삭제 완벽 수정 (Listbox 연동 및 리눅스 절대경로 매핑)
    def delete_pcap(self):
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 서버를 연결해 주세요.")
            return

        selections = self.list_pcap_files.curselection()
        if not selections:
            messagebox.showwarning("경고", "삭제할 PCAP 파일을 먼저 클릭해서 선택해 주세요.\n(다중 선택: Ctrl + 클릭 또는 Shift + 클릭)")
            return
            
        selected_files = [self.list_pcap_files.get(i) for i in selections]
        
        if messagebox.askyesno("삭제 확인", f"선택한 {len(selected_files)}개의 파일을 서버에서 완전히 삭제하시겠습니까?"):
            pw = self.ent_ssh_pw.get()
            base_dir = self.ent_pcap_path.get().rstrip('/')
            
            success_count = 0
            for f in selected_files:
                # Windows의 \\ 경로가 넘어가지 않도록 완전한 Linux / 포맷으로 고정
                target_file = f"{base_dir}/{f}"
                
                cmd_list = ["sudo", "-S", "rm", "-f", target_file]
                safe_cmd = " ".join(shlex.quote(arg) for arg in cmd_list)
                
                out, err = self._safe_ssh_exec(safe_cmd, password=pw)
                if not err or "No such file" not in err:
                    success_count += 1
                    
            messagebox.showinfo("삭제 완료", f"총 {success_count}개의 파일이 깔끔하게 삭제되었습니다.")
            self._refresh_pcap_list()

    def _update_test_description(self, event):
        atype = self.combo_attack.get()
        desc = "■ O-RU 시험 목적 및 추천 설정 가이드\n\n"
        
        if "U-Plane" in atype:
            desc += "이 시험은 설정에 따라 완전히 다른 2가지 극한 스트레스를 줍니다.\n\n"
            desc += "① 소화불량 시험 (대역폭 마비 - 덤프트럭)\n"
            desc += "   - 목적: O-RU 메모리 꽉 채우기 및 광모듈 발열 한계\n"
            desc += "   - 설정: Size = 1500, Rate = 24.0 (Gbps)\n\n"
            desc += "② 과로사 시험 (RRC 접속 폭주 - 오토바이 톨게이트)\n"
            desc += "   - 목적: 초당 수천만 번 인터럽트를 발생시켜 CPU 연산 폭발 유도\n"
            desc += "   - 설정: Size = 64, Rate = 18.0 (Gbps)"
        elif "GTP" in atype:
            desc += "[비정상 패킷 예외처리 능력 필터링 시험]\n\n"
            desc += "O-RU가 모르는 엉뚱한 코어망 프로토콜(GTP-U/UDP)을 엄청나게 던집니다. (마치 주소 잘못 쓴 택배 무한 반송)\n\n"
            desc += "   - 목적: O-RU가 엉뚱한 패킷을 까보지 않고 즉시 버리는지(방어력) 확인\n"
            desc += "   - 불합격: 쓰레기 패킷을 해석하려다 CPU가 100%를 치고 뻗어버림\n"
            desc += "   - 설정: Size = 64~256, Rate = 24.0 (Gbps)"
        elif "PRACH" in atype:
            desc += "[무선(RF) 자원 스케줄링 고갈 시험]\n\n"
            desc += "O-DU로 위장하여 가짜 단말기들의 접속 허가 명령(Section Type 3)을 마구 내립니다.\n\n"
            desc += "   - 목적: O-RU 내부 FPGA/DSP의 무선 수신 대기열 메모리 마비 유도\n"
            desc += "   - 설정: O-RU MAC 정상, Size = 64, Spoofing = On"
        elif "C-Plane" in atype:
            desc += "정상적인 제어 메시지(Section Type 1)를 무한 전송하여 O-RU 제어 평면을 꽉 채웁니다.\n"
            desc += "L2 보안 차단을 피하기 위해 O-RU MAC/VLAN은 반드시 '정상 값'을 입력하세요."
        elif "PTP" in atype:
            desc += "PTP 시간 동기화 메시지를 대량 전송하여 O-RU의 Clock Sync를 교란합니다.\n"
            desc += "다수의 마스터 클럭인 척 위장하기 위해 Spoofing 옵션을 켜주세요."
        elif "NETCONF" in atype:
            desc += "관리망(M-Plane)으로 수만 개의 TCP 세션을 요청하여 O-RU 관리 리소스를 고갈시킵니다.\n"
            desc += "O-RU IP를 정상 입력하고 Port를 830으로 맞추세요."

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

        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', atype)
        safe_name = re.sub(r'_+', '_', safe_name).strip('_') + ".pcap"
        
        self.ent_pcap_name.delete(0, tk.END)
        self.ent_pcap_name.insert(0, safe_name)
        self._calculate_pps(None)

    def _calculate_pps(self, event=None):
        try:
            rate_str = self.ent_rate.get().strip()
            pkt_str = self.ent_pkt_size.get().strip()
            if not rate_str or not pkt_str: raise ValueError
            rate_gbps = float(rate_str)
            pkt_size = int(pkt_str)
            if pkt_size <= 0 or rate_gbps < 0: raise ValueError

            frame_size = pkt_size + 20 
            max_l2_rate = 25.0 * (pkt_size / frame_size)
            pps = (rate_gbps * 1_000_000_000) / (pkt_size * 8)
            l1_gbps = (pps * frame_size * 8) / 1_000_000_000
            usage = (l1_gbps / 25.0) * 100
            pkts_per_ms = pps / 1000
            
            self.lbl_max_limit.config(text=f"L2 Line Rate Limit: {max_l2_rate:.2f} Gbps (현재 패킷 기준 최대치)")
            self.lbl_pps_calc.config(text=f"Packets/sec: {pps:,.0f} pps (1ms당 약 {pkts_per_ms:,.0f} 개)")
            self.lbl_gbps_calc.config(text=f"Expected L1 Throughput: {l1_gbps:.2f} Gbps")
            self.lbl_line_calc.config(text=f"L1 Line Rate Usage: {usage:.1f}% (of 25G)")
            
            if usage > 100: self.lbl_line_calc.config(foreground="red")
            else: self.lbl_line_calc.config(foreground="blue")
        except ValueError:
            self.lbl_max_limit.config(text="L2 Line Rate Limit: -")
            self.lbl_pps_calc.config(text="Packets/sec: -")
            self.lbl_gbps_calc.config(text="Expected L1 Throughput: -")
            self.lbl_line_calc.config(text="L1 Line Rate Usage: -")

    def build_pcap(self):
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 1번 탭에서 서버를 연결해 주세요.")
            return
            
        if not self._validate_inputs():
            return
            
        threading.Thread(target=self._build_pcap_bg, daemon=True).start()

    def _build_pcap_bg(self):
        try:
            config = {
                "attack_type": self.combo_attack.get(),
                "src_mac": self.ent_src_mac.get(),
                "dst_mac": self.ent_dst_mac.get(),
                "src_ip": self.ent_src_ip.get(),
                "dst_ip": self.ent_dst_ip.get(),
                "dst_port": self.ent_dst_port.get(),
                "vlan_id": self.ent_vlan_id.get(),
                "spoofing": self.var_spoofing.get(),
                "pkt_size": self.ent_pkt_size.get(),
                "pcap_path": self.ent_pcap_path.get(),
                "pcap_name": self.ent_pcap_name.get() 
            }
            
            pw = self.ent_ssh_pw.get()
            b64_config = base64.b64encode(json.dumps(config).encode()).decode()
            
            builder_script = r"""import sys, json, base64, os, struct
from scapy.all import *
def build_packet(config):
    atype = config.get('attack_type', '').upper()
    src_mac = config.get('src_mac', '00:00:00:00:00:01')
    dst_mac = config.get('dst_mac', 'ff:ff:ff:ff:ff:ff')
    vlan_id = config.get('vlan_id', '')
    pkt_size = int(config.get('pkt_size', 64))

    has_vlan = bool(vlan_id and str(vlan_id).strip().isdigit())
    l2_len = 18 if has_vlan else 14
    
    ecpri_payload_len = max(0, pkt_size - l2_len - 4)

    if has_vlan:
        vid = int(str(vlan_id).strip())
        l2_ecpri = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid, type=0xAEFE)
        l2_ptp   = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid, type=0x88F7)
        l2_ip    = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid)
    else:
        l2_ecpri = Ether(src=src_mac, dst=dst_mac, type=0xAEFE)
        l2_ptp   = Ether(src=src_mac, dst=dst_mac, type=0x88F7)
        l2_ip    = Ether(src=src_mac, dst=dst_mac)

    if 'PRACH' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        rtc_seq = struct.pack('!HH', 0x0001, 0x0000) # RTC_ID, SEQ_ID
        oran_hdr = b'\x00\x00\x00\x00\x01\x03'
        pad_len = max(0, ecpri_payload_len - len(rtc_seq) - len(oran_hdr))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + rtc_seq + oran_hdr + (b'\x00' * pad_len))
        
    elif 'C-PLANE' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        rtc_seq = struct.pack('!HH', 0x0001, 0x0000) # RTC_ID, SEQ_ID
        oran_hdr = b'\x80\x00\x00\x00\x01\x01'
        pad_len = max(0, ecpri_payload_len - len(rtc_seq) - len(oran_hdr))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + rtc_seq + oran_hdr + (b'\x00' * pad_len))
        
    elif 'U-PLANE' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x00, ecpri_payload_len)
        pc_seq = struct.pack('!HH', 0x0001, 0x0000) # PC_ID, SEQ_ID
        pad_len = max(0, ecpri_payload_len - len(pc_seq))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + pc_seq + (b'\x00' * pad_len))
        
    elif 'PTP' in atype:
        ptp_hdr = b'\x00\x02\x00\x2c' + b'\x00'*40
        pad_len = max(0, pkt_size - l2_len - len(ptp_hdr))
        pkt = l2_ptp / Raw(load=ptp_hdr + (b'\x00' * pad_len))
        
    elif 'NETCONF' in atype or 'TCP' in atype:
        src_ip = config.get('src_ip', '192.168.11.100')
        dst_ip = config.get('dst_ip', '192.168.11.2')
        dst_port = int(config.get('dst_port', 830))
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

try:
    config = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
    pcap_path, pcap_name = config.get('pcap_path', '/tmp'), config.get('pcap_name', 'attack.pcap')
    full_path = os.path.join(pcap_path, pcap_name)
    if not os.path.exists(pcap_path): os.makedirs(pcap_path)
    wrpcap(full_path, build_packet(config))
    print(json.dumps({'status': 'success', 'file': full_path}))
except Exception as e:
    print(json.dumps({'status': 'error', 'message': str(e)}))
"""
            b64_builder = base64.b64encode(builder_script.encode()).decode()
            
            self._safe_ssh_exec(f"echo '{b64_builder}' | base64 -d > /tmp/oran_attack_builder.py", password=pw)
            time.sleep(0.5)
            
            cmd = f"sudo -S python3 /tmp/oran_attack_builder.py {b64_config}"
            output, error = self._safe_ssh_exec(cmd, password=pw)
            
            try:
                clean_json = ""
                for line in reversed(output.split('\n')):
                    if '{' in line and '}' in line:
                        clean_json = line[line.find('{'):line.rfind('}')+1]
                        break
                
                res = json.loads(clean_json)
                if res.get("status") == "success":
                    messagebox.showinfo("성공", f"PCAP 파일이 완벽하게 생성되었습니다!\n저장 경로: {res.get('file')}")
                    self._refresh_pcap_list()
                else:
                    messagebox.showerror("실패", f"PCAP 생성 중 오류가 발생했습니다.\n{res.get('message')}")
            except:
                messagebox.showerror("실패", f"서버 오류로 PCAP 생성에 실패했습니다:\n{output}\n{error}")
                
        except Exception as e:
            messagebox.showerror("오류", f"실행 중 에러 발생: {str(e)}")

    def play_traffic(self):
        if not self.ssh_client:
            messagebox.showwarning("경고", "먼저 서버를 연결해 주세요.")
            return
            
        if not self._validate_inputs():
            return

        selections = self.list_pcap_files.curselection()
        if not selections: 
            messagebox.showwarning("경고", "발사할 PCAP 파일을 선택해 주세요.")
            return
            
        selected_pcap = self.list_pcap_files.get(selections[0]) # 다중 선택 시 최상단 파일 1개만 발사

        self.txt_stats.config(state=tk.NORMAL)
        self.txt_stats.delete('1.0', tk.END)
        self.txt_stats.config(state=tk.DISABLED)
        
        pw = self.ent_ssh_pw.get()
        target_rate = self.ent_rate.get()
        
        self._safe_ssh_exec(f"sudo -S sh -c \"echo '{target_rate} Gbps (목표치)' > /tmp/trex_target_rate\"", password=pw)

        # 리눅스 전용 절대경로 슬래시 처리
        pcap_target = f"{self.ent_pcap_path.get().rstrip('/')}/{selected_pcap}"
        duration = self.ent_duration.get()
        port_val = self.combo_trex_port.get()
        port_list_str = "[0, 1]" if "," in port_val else f"[{port_val}]"
        rate_command = "100%" if target_rate.strip() == "25.0" else f"{target_rate}gbps"
        
        spoof_flag = "True" if self.var_spoofing.get() else "False"
        
        api_script = f"""
import sys, time
sys.path.insert(0, '{self.ent_trex_path.get()}/automation/trex_control_plane/interactive')
from trex.stl.api import *
from scapy.all import rdpcap

try:
    pkts = rdpcap('{pcap_target}')
    raw_pkt = pkts[0]
    
    vm_cmds = []
    if {spoof_flag}:
        vm_cmds.append(STLVmFlowVar(name="mac_rand", min_value=1, max_value=0xffffffff, size=4, op="random"))
        vm_cmds.append(STLVmWrFlowVar(fv_name="mac_rand", pkt_offset=8))
        
    if vm_cmds:
        pkt_builder = STLPktBuilder(pkt=raw_pkt, vm=STLScVmRaw(vm_cmds))
    else:
        pkt_builder = STLPktBuilder(pkt=raw_pkt)

    c = STLClient(server='127.0.0.1')
    c.connect()
    
    ports_to_fire = {port_list_str}
    c.acquire(ports=ports_to_fire, force=True)
    c.reset(ports=ports_to_fire)
    c.clear_stats()
    
    stream = STLStream(packet=pkt_builder, mode=STLTXCont(pps=1))
    for p in ports_to_fire: 
        c.add_streams([stream], ports=[p])
        
    c.start(ports=ports_to_fire, mult='{rate_command}', duration={duration})
    c.wait_on_traffic(ports=ports_to_fire)
    
except Exception as e: 
    print(f"[ERROR] {{str(e)}}")
finally:
    try: 
        c.release(ports=ports_to_fire)
        c.disconnect()
    except: pass
"""
        b64_script = base64.b64encode(api_script.encode()).decode()
        self._safe_ssh_exec(f"echo '{b64_script}' | base64 -d > /tmp/run_fire.py", password=pw)
        
        def run():
            # 🛑 [Deadlock Fix] 발사 스레드는 자물쇠(ssh_lock)를 쥐지 않도록 일반 명령어로 실행
            try:
                stdin, stdout, stderr = self.ssh_client.exec_command("sudo -S python3 /tmp/run_fire.py", get_pty=True)
                stdin.write(pw + "\n")
                stdin.flush()
                
                error_msg = ""
                for line in iter(stdout.readline, ""):
                    clean_line = line.strip()
                    if clean_line and not clean_line.startswith("[sudo]"):
                        if "[ERROR]" in clean_line or "Exception" in clean_line:
                            error_msg += clean_line + "\n"
                if error_msg:
                    self.root.after(0, messagebox.showerror, "발사 실패 (TRex 에러)", f"에러가 발생하여 중단되었습니다:\n\n{error_msg}")
            except Exception as e:
                pass
                
        threading.Thread(target=run, daemon=True).start()
        messagebox.showinfo("발사 확인", f"포트 {port_val}에서 트래픽 발사를 시작합니다! (Spoofing: {spoof_flag})")

    def stop_traffic(self):
        if not self.ssh_client: return
        self._update_status_text("▶ 발사 중지 명령 전송...")
        
        # 🛑 [Deadlock Fix] STOP 명령도 메인 GUI를 얼리지 않도록 비동기 스레드로 던집니다.
        threading.Thread(target=self._stop_bg, daemon=True).start()
        
    def _stop_bg(self):
        pw = self.ent_ssh_pw.get()
        self._safe_ssh_exec(f"sudo -S sh -c \"echo '0 Gbps (중지됨)' > /tmp/trex_target_rate\"", password=pw)
        
        port_val = self.combo_trex_port.get()
        port_list_str = "[0, 1]" if "," in port_val else f"[{port_val}]"
        
        stop_script = f"""
import sys
sys.path.insert(0, '{self.ent_trex_path.get()}/automation/trex_control_plane/interactive')
try:
    from trex.stl.api import STLClient
    c = STLClient(server='127.0.0.1')
    c.connect()
    ports_to_stop = {port_list_str}
    c.acquire(ports=ports_to_stop, force=True)
    c.stop(ports=ports_to_stop)
    c.clear_stats()  
    c.release(ports=ports_to_stop)
    c.disconnect()
except Exception as e: print(f"[ERROR] {{str(e)}}")
"""
        b64_stop = base64.b64encode(stop_script.encode()).decode()
        self._safe_ssh_exec(f"echo '{b64_stop}' | base64 -d > /tmp/stop_fire.py", password=pw)
        self._safe_ssh_exec("sudo -S python3 /tmp/stop_fire.py", password=pw)

    def _get_ui_fields(self):
        return {
            "server_ip": self.ent_server_ip, "ssh_user": self.ent_ssh_user, "ssh_pw": self.ent_ssh_pw,
            "trex_path": self.ent_trex_path, "src_mac": self.ent_src_mac, "dst_mac": self.ent_dst_mac,
            "src_ip": self.ent_src_ip, "dst_ip": self.ent_dst_ip, "dst_port": self.ent_dst_port,
            "vlan_id": self.ent_vlan_id,
            "pcap_path": self.ent_pcap_path, "pcap_name": self.ent_pcap_name,
            "pkt_size": self.ent_pkt_size, "rate": self.ent_rate, "duration": self.ent_duration
        }

    def _load_config(self):
        fields = self._get_ui_fields()
        defaults = {
            "server_ip": "192.168.9.249", "ssh_user": "slab", "trex_path": "/home/slab/trex/v3.08", 
            "trex_port": "0", "trex_cores": "6",
            "src_mac": "00:11:22:33:44:55", "dst_mac": "AA:BB:CC:DD:EE:FF",
            "src_ip": "192.168.11.100", "dst_ip": "192.168.11.2", "dst_port": "830",
            "vlan_id": "", "spoofing": True,
            "pcap_path": "/tmp/pcap_output", "pcap_name": "attack_target.pcap",
            "pkt_size": "64", "rate": "10.0", "duration": "60", "attack_type": "eCPRI U-Plane (대역폭/RRC 과부하)"
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f: defaults.update(json.load(f))
            except: pass

        for key, entry in fields.items(): entry.insert(0, defaults.get(key, ""))
            
        self.combo_attack.set(defaults.get("attack_type", "eCPRI U-Plane (대역폭/RRC 과부하)"))
        self.combo_trex_port.set(defaults.get("trex_port", "0"))
        self.combo_trex_cores.set(defaults.get("trex_cores", "6"))
        self.var_spoofing.set(defaults.get("spoofing", True))
        
        self._update_test_description(None)

    def _save_config(self):
        fields = self._get_ui_fields()
        config = {k: v.get() for k, v in fields.items()}
        config["attack_type"] = self.combo_attack.get()
        config["trex_port"] = self.combo_trex_port.get()
        config["trex_cores"] = self.combo_trex_cores.get()
        config["spoofing"] = self.var_spoofing.get()
        try:
            with open(CONFIG_FILE, "w") as f: json.dump(config, f, indent=4)
        except: pass

    def _on_closing(self):
        self._save_config()
        self.monitor_running = False
        if self.ssh_client: self.ssh_client.close()
        if self.trex_server_ssh: self.trex_server_ssh.close()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = ORanValidationGUI(root)
    root.mainloop()

# python -m PyInstaller --noconsole --onefile --icon="DDOS.ico" oran_trex_master.py