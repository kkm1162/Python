# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from scapy.all import Ether, IP, TCP, UDP, ICMP, Dot1Q, Raw, PcapWriter, PcapNgWriter
import scapy.contrib.igmp as igmp
import random
import threading
import os
import time
import json 

class UltimatePcapGeneratorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("O-RAN DDoS Tool")
        self.root.geometry("540x980")
        self.root.resizable(False, False)

        style = ttk.Style()
        style.theme_use('clam')

        self.is_cancelled = False 
        self.config_file = "oran_pcap_config.json"

        # 💡 UI 입력 변수들
        self.var_vlan = tk.IntVar(value=0)
        self.var_size_mode = tk.IntVar(value=0)
        self.var_custom_size = tk.StringVar(value="1500")
        self.var_rand_min = tk.StringVar(value="64")
        self.var_rand_max = tk.StringVar(value="1500")
        self.var_duration = tk.StringVar(value="1")
        self.var_count = tk.StringVar(value="7000")
        self.file_path_var = tk.StringVar(value=os.path.join(os.getcwd(), "DDos_test.pcapng"))

        # 값 변경 시 대시보드 실시간 업데이트 트리거
        for var in [self.var_vlan, self.var_size_mode, self.var_custom_size, self.var_rand_min, self.var_rand_max, self.var_duration, self.var_count]:
            var.trace_add("write", self.update_traffic_calc)

        self.create_widgets()
        self.load_config()      
        self.update_ui_state()
        self.update_traffic_calc()

    def create_widgets(self):
        # 0. 시험 항목 선택
        frame_test = ttk.LabelFrame(self.root, text=" 0. DDoS 시험 항목 선택 ", padding=10)
        frame_test.pack(fill="x", padx=15, pady=5)

        self.test_cases = [
            "[DDoS] TCP SYN Flood (메모리 고갈)",
            "[DDoS] UDP Flood (대역폭 마비)",
            "[DDoS] ICMP Echo Flood (Ping 폭탄)",
            "[DDoS] IGMP Flood (TTL=1 멀티캐스트)",
            "[Invalid] 출발지 MAC All Zero (00:00...)",
            "[Invalid] 출발지 IP All Zero (0.0.0.0)",
            "[Invalid] 목적지 IP All Zero (0.0.0.0)"
        ]
        
        self.combo_test = ttk.Combobox(frame_test, values=self.test_cases, state="readonly", width=50)
        self.combo_test.set(self.test_cases[0])
        self.combo_test.pack(pady=(0, 5))
        self.combo_test.bind('<<ComboboxSelected>>', self.update_ui_state) 

        self.lbl_desc = ttk.Label(frame_test, text="", foreground="blue", justify="left", wraplength=480)
        self.lbl_desc.pack(anchor="w", pady=2)

        # 1. 장비 기본 주소
        frame_addr = ttk.LabelFrame(self.root, text=" 1. 장비 기본 주소 (MAC & IP) ", padding=10)
        frame_addr.pack(fill="x", padx=15, pady=5)

        self.src_mac = self.add_entry(frame_addr, "Source MAC:", "00:11:22:33:44:55", 0)
        self.dst_mac = self.add_entry(frame_addr, "Dest MAC (O-RU):", "AA:BB:CC:DD:EE:FF", 1)
        self.src_ip = self.add_entry(frame_addr, "Source IP:", "192.168.11.100", 2)
        self.dst_ip = self.add_entry(frame_addr, "Dest IP (O-RU):", "192.168.11.2", 3)
        self.dst_port = self.add_entry(frame_addr, "Dest Port:", "830", 4)

        # 2. VLAN 설정
        frame_vlan = ttk.LabelFrame(self.root, text=" 2. VLAN 설정 (M-Plane / C-Plane) ", padding=10)
        frame_vlan.pack(fill="x", padx=15, pady=5)

        ttk.Radiobutton(frame_vlan, text="Untagged (VLAN 없음)", variable=self.var_vlan, value=0, command=self.update_ui_state).grid(row=0, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Radiobutton(frame_vlan, text="Tagged (VLAN ID 지정)", variable=self.var_vlan, value=1, command=self.update_ui_state).grid(row=1, column=0, columnspan=2, sticky="w", pady=2)

        ttk.Label(frame_vlan, text="VLAN ID:").grid(row=2, column=0, sticky="e", pady=5, padx=5)
        self.vlan_id_entry = ttk.Entry(frame_vlan, state="disabled", width=15)
        self.vlan_id_entry.insert(0, "1000")
        self.vlan_id_entry.grid(row=2, column=1, sticky="w")

        # 3. 패킷 크기 설정
        frame_size = ttk.LabelFrame(self.root, text=" 3. 패킷 크기 (Payload Size) 설정 ", padding=10)
        frame_size.pack(fill="x", padx=15, pady=5)

        ttk.Radiobutton(frame_size, text="Standard (64 Bytes, 미달 프레임 방지)", variable=self.var_size_mode, value=0, command=self.update_ui_state).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Radiobutton(frame_size, text="Jumbo Frame (초대형 9000 Bytes)", variable=self.var_size_mode, value=1, command=self.update_ui_state).grid(row=1, column=0, sticky="w", pady=2)
        
        ttk.Radiobutton(frame_size, text="User Defined (사용자 지정 고정 크기):", variable=self.var_size_mode, value=2, command=self.update_ui_state).grid(row=2, column=0, sticky="w", pady=2)
        self.entry_custom_size = ttk.Entry(frame_size, textvariable=self.var_custom_size, width=8, state="disabled")
        self.entry_custom_size.grid(row=2, column=1, sticky="w", padx=5)

        ttk.Radiobutton(frame_size, text="Random (패킷별 무작위 크기 요동):", variable=self.var_size_mode, value=3, command=self.update_ui_state).grid(row=3, column=0, sticky="w", pady=2)
        
        frame_random = ttk.Frame(frame_size)
        frame_random.grid(row=3, column=1, sticky="w", padx=5)
        self.entry_rand_min = ttk.Entry(frame_random, textvariable=self.var_rand_min, width=5, state="disabled")
        self.entry_rand_min.pack(side="left")
        ttk.Label(frame_random, text=" ~ ").pack(side="left")
        self.entry_rand_max = ttk.Entry(frame_random, textvariable=self.var_rand_max, width=5, state="disabled")
        self.entry_rand_max.pack(side="left")

        # 4. 생성 및 속도 설정
        frame_file = ttk.LabelFrame(self.root, text=" 4. PCAP 재생 밀도 및 저장 설정 ", padding=10)
        frame_file.pack(fill="x", padx=15, pady=5)

        ttk.Label(frame_file, text="목표 재생 주기 (ms단위):").grid(row=0, column=0, sticky="e", pady=5, padx=5)
        ttk.Entry(frame_file, textvariable=self.var_duration, width=15).grid(row=0, column=1, sticky="w", pady=5, padx=5)

        ttk.Label(frame_file, text="해당 주기당 패킷 개수:").grid(row=1, column=0, sticky="e", pady=5, padx=5)
        ttk.Entry(frame_file, textvariable=self.var_count, width=15).grid(row=1, column=1, sticky="w", pady=5, padx=5)

        ttk.Label(frame_file, text="저장 경로:").grid(row=2, column=0, sticky="e", pady=5, padx=5)
        self.file_path_entry = ttk.Entry(frame_file, textvariable=self.file_path_var, width=25)
        self.file_path_entry.grid(row=2, column=1, sticky="w", pady=5, padx=5)
        
        self.btn_browse = ttk.Button(frame_file, text="경로 지정", command=self.browse_file)
        self.btn_browse.grid(row=2, column=2, padx=5)

        # 5. 실시간 전송량 대시보드
        frame_calc = ttk.LabelFrame(self.root, text=" 📊 5. 예상 전송량 대시보드 (L1 물리 계층 기준) ", padding=10)
        frame_calc.pack(fill="x", padx=15, pady=5)

        ttk.Label(frame_calc, text="초당 패킷 수 (PPS):", font=("", 10, "bold")).grid(row=0, column=0, sticky="w", pady=2)
        self.lbl_calc_pps = ttk.Label(frame_calc, text="0 PPS", font=("", 11, "bold"), foreground="blue")
        self.lbl_calc_pps.grid(row=0, column=1, sticky="e", pady=2, padx=10)

        ttk.Label(frame_calc, text="예상 대역폭 (Gbps):", font=("", 10, "bold")).grid(row=1, column=0, sticky="w", pady=2)
        self.lbl_calc_bps = ttk.Label(frame_calc, text="0.00 Gbps", font=("", 12, "bold"), foreground="green")
        self.lbl_calc_bps.grid(row=1, column=1, sticky="e", pady=2, padx=10)

        ttk.Label(frame_calc, text="25G Line Rate 점유율:", font=("", 10, "bold")).grid(row=2, column=0, sticky="w", pady=2)
        self.lbl_calc_percent = ttk.Label(frame_calc, text="0.0 %", font=("", 11, "bold"))
        self.lbl_calc_percent.grid(row=2, column=1, sticky="e", pady=2, padx=10)

        # 6. 액션 버튼
        frame_action = ttk.Frame(self.root)
        frame_action.pack(pady=10)

        self.btn_generate = ttk.Button(frame_action, text="▶ 시험용 PCAP 생성", command=self.start_generation_thread)
        self.btn_generate.grid(row=0, column=0, padx=10, ipady=8, ipadx=10)

        self.btn_cancel = ttk.Button(frame_action, text="⏹ 생성 취소", command=self.cancel_generation, state="disabled")
        self.btn_cancel.grid(row=0, column=1, padx=10, ipady=8, ipadx=10)

        self.status_label = ttk.Label(self.root, text="대기 중...", foreground="gray", font=("", 10, "bold"))
        self.status_label.pack()

    def add_entry(self, parent, label_text, default_val, row):
        ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="e", pady=5, padx=5)
        entry = ttk.Entry(parent, width=25)
        entry.insert(0, default_val)
        entry.grid(row=row, column=1, sticky="w", pady=5, padx=5)
        return entry

    def _set_entry_value(self, entry, value):
        entry.config(state='normal') 
        entry.delete(0, tk.END)
        entry.insert(0, value)

    def update_traffic_calc(self, *args):
        try:
            count = int(self.var_count.get())
            duration = float(self.var_duration.get())
            if duration <= 0 or count <= 0:
                raise ValueError

            pps = count / (duration / 1000.0)

            s_mode = self.var_size_mode.get()
            if s_mode == 0:
                base_size = 64
            elif s_mode == 1:
                base_size = 9000
            elif s_mode == 2:
                base_size = int(self.var_custom_size.get())
            elif s_mode == 3:
                min_s = int(self.var_rand_min.get())
                max_s = int(self.var_rand_max.get())
                base_size = (min_s + max_s) / 2.0

            min_len = 58 if self.var_vlan.get() == 1 else 54
            actual_size = max(base_size, min_len)

            wire_size = actual_size + 24
            bps = pps * wire_size * 8
            gbps = bps / 1_000_000_000.0
            percent = (gbps / 25.0) * 100

            self.lbl_calc_pps.config(text=f"{pps:,.0f} PPS")
            self.lbl_calc_bps.config(text=f"{gbps:.2f} Gbps")
            self.lbl_calc_percent.config(text=f"{percent:.1f} %")

            if gbps > 25.0:
                self.lbl_calc_bps.config(foreground="red")
                self.lbl_calc_percent.config(foreground="red", text=f"⚠️ {percent:.1f} % (물리적 한계 초과)")
            else:
                self.lbl_calc_bps.config(foreground="green")
                self.lbl_calc_percent.config(foreground="black", text=f"{percent:.1f} %")

        except ValueError:
            self.lbl_calc_pps.config(text="- 입력 대기 중 -")
            self.lbl_calc_bps.config(text="-", foreground="gray")
            self.lbl_calc_percent.config(text="-", foreground="gray")

    def update_ui_state(self, event=None):
        test_type = self.combo_test.get()
        self.src_mac.config(state='normal')
        self.src_ip.config(state='normal')
        self.dst_ip.config(state='normal')
        self.dst_port.config(state='normal')

        desc = ""
        if "TCP SYN" in test_type:
            desc = "📌 O-RU의 연결 대기열(메모리)을 가득 채우는 공격입니다.\n👉 Dest Port 설정이 필수입니다."
        elif "UDP" in test_type:
            desc = "📌 대역폭을 마비시킵니다. 닫혀있는 임의의 Dest Port를 설정하세요."
        elif "ICMP" in test_type:
            desc = "📌 무차별 Ping(Echo)을 보내 장비의 CPU를 괴롭힙니다.\n🚫 포트 불필요 (자동 잠금)"
            self.dst_port.config(state='disabled')
        elif "IGMP" in test_type:
            desc = "📌 TTL=1인 멀티캐스트 패킷을 쏟아부어 라우팅 오류 유발.\n🚫 포트 불필요 (자동 잠금)"
            self.dst_port.config(state='disabled')
        elif "MAC All Zero" in test_type:
            desc = "📌 Source MAC을 [00:00:00:00:00:00]으로 강제 변조합니다.\n🚫 Source MAC 창 자동 잠금"
            self.src_mac.config(state='disabled')
        elif "출발지 IP All Zero" in test_type:
            desc = "📌 Source IP를 [0.0.0.0]으로 변조하여 방어력을 확인합니다.\n🚫 Source IP 창 자동 잠금"
            self.src_ip.config(state='disabled')
        elif "목적지 IP All Zero" in test_type:
            desc = "📌 Dest IP를 [0.0.0.0]으로 쏘아 패킷 드롭 여부를 봅니다.\n🚫 Dest IP 창 자동 잠금"
            self.dst_ip.config(state='disabled')

        self.lbl_desc.config(text=desc)

        if self.var_vlan.get() == 1:
            self.vlan_id_entry.config(state='normal')
        else:
            self.vlan_id_entry.config(state='disabled')

        s_mode = self.var_size_mode.get()
        self.entry_custom_size.config(state='normal' if s_mode == 2 else 'disabled')
        self.entry_rand_min.config(state='normal' if s_mode == 3 else 'disabled')
        self.entry_rand_max.config(state='normal' if s_mode == 3 else 'disabled')

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    c = json.load(f)
                
                self.combo_test.set(c.get("test_type", self.test_cases[0]))
                self._set_entry_value(self.src_mac, c.get("src_mac", "00:11:22:33:44:55"))
                self._set_entry_value(self.dst_mac, c.get("dst_mac", "AA:BB:CC:DD:EE:FF"))
                self._set_entry_value(self.src_ip, c.get("src_ip", "192.168.11.100"))
                self._set_entry_value(self.dst_ip, c.get("dst_ip", "192.168.11.2"))
                self._set_entry_value(self.dst_port, c.get("dst_port", "830"))
                
                self.var_vlan.set(c.get("vlan_var", 0))
                self._set_entry_value(self.vlan_id_entry, c.get("vlan_id", "1000"))
                
                self.var_size_mode.set(c.get("size_mode", 0))
                self.var_custom_size.set(c.get("custom_size", "1500"))
                self.var_rand_min.set(c.get("rand_min", "64"))
                self.var_rand_max.set(c.get("rand_max", "1500"))

                self.var_duration.set(c.get("duration_ms", "1"))
                self.var_count.set(c.get("pkt_count", "7000"))
                
                self.file_path_var.set(c.get("file_path", os.path.join(os.getcwd(), "DDoS_test.pcapng")))
            except Exception as e:
                print(f"설정 불러오기 실패: {e}")

    def save_config(self):
        config = {
            "test_type": self.combo_test.get(),
            "src_mac": self.src_mac.get(),
            "dst_mac": self.dst_mac.get(),
            "src_ip": self.src_ip.get(),
            "dst_ip": self.dst_ip.get(),
            "dst_port": self.dst_port.get(),
            "vlan_var": self.var_vlan.get(),
            "vlan_id": self.vlan_id_entry.get(),
            "size_mode": self.var_size_mode.get(),
            "custom_size": self.var_custom_size.get(),
            "rand_min": self.var_rand_min.get(),
            "rand_max": self.var_rand_max.get(),
            "duration_ms": self.var_duration.get(), 
            "pkt_count": self.var_count.get(),
            "file_path": self.file_path_var.get()
        }
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"설정 저장 실패: {e}")

    def browse_file(self):
        file_path = filedialog.asksaveasfilename(
            defaultextension=".pcapng",
            filetypes=[("PCAPNG 파일", "*.pcapng"), ("PCAP 파일", "*.pcap"), ("모든 파일", "*.*")],
            title="패킷 저장 위치 및 포맷 선택",
            initialfile="DDoS_test.pcapng"
        )
        if file_path:
            self.file_path_var.set(file_path)

    def cancel_generation(self):
        self.is_cancelled = True
        self.btn_cancel.config(state='disabled')
        self.status_label.config(text="작업 중단 중... 잠시만 기다려주세요.", foreground="orange")

    def start_generation_thread(self):
        try:
            int(self.var_count.get())
            float(self.var_duration.get())
            s_mode = self.var_size_mode.get()
            if s_mode == 2: int(self.var_custom_size.get())
            if s_mode == 3: 
                int(self.var_rand_min.get())
                int(self.var_rand_max.get())
        except ValueError:
            messagebox.showerror('입력 오류', '숫자(개수, 크기, 주기) 입력란에는 숫자만 입력해야 합니다.')
            return

        if self.var_vlan.get() == 1:
            if not self.vlan_id_entry.get().strip().isdigit():
                messagebox.showerror('입력 오류', 'VLAN ID를 숫자로 입력해 주세요.')
                return

        self.save_config()
        self.is_cancelled = False
        self.btn_generate.config(state='disabled')
        self.btn_cancel.config(state='normal')
        self.status_label.config(text='PCAP 고속 스트리밍 저장 중...', foreground='blue')
        
        threading.Thread(target=self.generate_pcap, daemon=True).start()

    def generate_pcap(self):
        try:
            test_type = self.combo_test.get()
            base_smac = self.src_mac.get()
            base_dmac = self.dst_mac.get()
            base_sip = self.src_ip.get()
            base_dip = self.dst_ip.get()
            dport = int(self.dst_port.get()) if self.dst_port.instate(['!disabled']) else 0
            
            count = int(self.var_count.get())
            target_ms = float(self.var_duration.get())
            filepath = self.file_path_var.get()
            s_mode = self.var_size_mode.get()

            if filepath.lower().endswith('.pcapng'):
                pktdump = PcapNgWriter(filepath)
            else:
                pktdump = PcapWriter(filepath)

            base_time = time.time()
            created_count = 0

            target_duration_sec = target_ms / 1000.0
            time_step = target_duration_sec / count if count > 0 else 0

            for i in range(count):
                if self.is_cancelled:
                    break

                current_smac = base_smac
                current_sip = base_sip
                current_dip = base_dip
                sport = random.randint(1024, 65535)
                ttl_val = 64

                if "MAC All Zero" in test_type: current_smac = "00:00:00:00:00:00"
                if "출발지 IP All Zero" in test_type: current_sip = "0.0.0.0"
                if "목적지 IP All Zero" in test_type: current_dip = "0.0.0.0"

                eth = Ether(src=current_smac, dst=base_dmac)
                if self.var_vlan.get() == 1:
                    vid = int(self.vlan_id_entry.get())
                    eth = eth / Dot1Q(vlan=vid)

                if "IGMP" in test_type: ttl_val = 1
                ip = IP(src=current_sip, dst=current_dip, ttl=ttl_val)

                if "TCP SYN" in test_type:
                    l4 = TCP(sport=sport, dport=dport, flags='S', seq=random.randint(0, 4294967295))
                    pkt = eth / ip / l4
                elif "UDP" in test_type:
                    l4 = UDP(sport=sport, dport=dport)
                    pkt = eth / ip / l4
                elif "ICMP" in test_type:
                    l4 = ICMP(type=8)
                    pkt = eth / ip / l4
                elif "IGMP" in test_type:
                    l4 = igmp.IGMP() 
                    pkt = eth / ip / l4
                else:
                    l4 = UDP(sport=sport, dport=dport)
                    pkt = eth / ip / l4

                if s_mode == 0: target_size = 64
                elif s_mode == 1: target_size = 9000
                elif s_mode == 2: target_size = int(self.var_custom_size.get())
                else: target_size = random.randint(int(self.var_rand_min.get()), int(self.var_rand_max.get()))

                current_len = len(pkt)
                pad_len = max(0, target_size - current_len)
                
                if pad_len > 0:
                    pad = Raw(load=b'\x00' * pad_len)
                    pkt = pkt / pad

                pkt.time = base_time + (i * time_step) 
                pktdump.write(pkt)
                created_count += 1
                
                if created_count % 10000 == 0:
                    self.root.after(0, self.update_status, f"{created_count} / {count} 패킷 기록 중...")

            pktdump.close()
            
            if self.is_cancelled:
                msg = f"작업이 취소되었습니다. (총 {created_count}개 저장됨)"
                self.root.after(0, self.generation_complete, False, msg, "orange")
            else:
                msg = f"완벽하게 {target_ms}ms로 압축된 {created_count}개의 패킷 생성 완료!"
                self.root.after(0, self.generation_complete, True, msg, "green")

        except Exception as e:
            self.root.after(0, self.generation_complete, False, str(e), "red")

    def update_status(self, text):
        self.status_label.config(text=text)

    def generation_complete(self, success, msg, color_code):
        self.btn_generate.config(state='normal')
        self.btn_cancel.config(state='disabled')
        self.status_label.config(text=msg, foreground=color_code)
        
        if color_code == "red":
            messagebox.showerror('에러', msg)
        elif color_code == "green":
            messagebox.showinfo('성공', msg)
        else:
            messagebox.showwarning('취소됨', msg)

if __name__ == '__main__':
    root = tk.Tk()
    app = UltimatePcapGeneratorGUI(root)
    root.mainloop()