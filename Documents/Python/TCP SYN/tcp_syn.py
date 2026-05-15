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
        self.root.title("O-RAN Robustness 마스터 PCAP 생성기 (Custom Auto)")
        self.root.geometry("520x780") # 입력창 추가로 세로를 살짝 늘렸습니다.
        self.root.resizable(False, False)

        style = ttk.Style()
        style.theme_use('clam')

        self.is_cancelled = False 
        self.config_file = "oran_pcap_config.json"

        self.create_widgets()
        self.load_config()      
        self.update_ui_state()  

    def create_widgets(self):
        # 0. 시험 항목 선택
        frame_test = ttk.LabelFrame(self.root, text=" 0. Robustness 시험 항목 선택 ", padding=10)
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

        self.lbl_desc = ttk.Label(frame_test, text="", foreground="blue", justify="left", wraplength=460)
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

        self.vlan_var = tk.IntVar(value=0)
        ttk.Radiobutton(frame_vlan, text="Untagged (VLAN 없음, M-Plane용)", variable=self.vlan_var, value=0, command=self.toggle_vlan).grid(row=0, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Radiobutton(frame_vlan, text="Tagged (VLAN ID 지정, C/U-Plane용)", variable=self.vlan_var, value=1, command=self.toggle_vlan).grid(row=1, column=0, columnspan=2, sticky="w", pady=2)

        ttk.Label(frame_vlan, text="VLAN ID:").grid(row=2, column=0, sticky="e", pady=5, padx=5)
        self.vlan_id_entry = ttk.Entry(frame_vlan, state="disabled", width=15)
        self.vlan_id_entry.insert(0, "1000")
        self.vlan_id_entry.grid(row=2, column=1, sticky="w")

        # 3. 저장 및 속도 설정 (💡 커스텀 주기 입력 추가)
        frame_file = ttk.LabelFrame(self.root, text=" 3. 생성 설정 (커스텀 자동 압축 적용) ", padding=10)
        frame_file.pack(fill="x", padx=15, pady=5)

        self.duration_ms = self.add_entry(frame_file, "목표 재생 주기 (ms단위):", "10", 0)
        self.pkt_count = self.add_entry(frame_file, "해당 주기당 패킷 개수:", "100000", 1)

        ttk.Label(frame_file, text="저장 경로:").grid(row=2, column=0, sticky="e", pady=5, padx=5)
        self.file_path_var = tk.StringVar(value=os.path.join(os.getcwd(), "robustness_test.pcapng"))
        self.file_path_entry = ttk.Entry(frame_file, textvariable=self.file_path_var, width=30)
        self.file_path_entry.grid(row=2, column=1, sticky="w", pady=5, padx=5)
        
        self.btn_browse = ttk.Button(frame_file, text="경로 지정", command=self.browse_file)
        self.btn_browse.grid(row=2, column=2, padx=5)

        # 4. 실행 및 취소 버튼
        frame_action = ttk.Frame(self.root)
        frame_action.pack(pady=15)

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

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                
                self.combo_test.set(config.get("test_type", self.test_cases[0]))
                self._set_entry_value(self.src_mac, config.get("src_mac", "00:11:22:33:44:55"))
                self._set_entry_value(self.dst_mac, config.get("dst_mac", "AA:BB:CC:DD:EE:FF"))
                self._set_entry_value(self.src_ip, config.get("src_ip", "192.168.11.100"))
                self._set_entry_value(self.dst_ip, config.get("dst_ip", "192.168.11.2"))
                self._set_entry_value(self.dst_port, config.get("dst_port", "830"))
                
                self.vlan_var.set(config.get("vlan_var", 0))
                self._set_entry_value(self.vlan_id_entry, config.get("vlan_id", "1000"))
                
                # 💡 추가된 설정값 불러오기
                self._set_entry_value(self.duration_ms, config.get("duration_ms", "10"))
                self._set_entry_value(self.pkt_count, config.get("pkt_count", "100000"))
                
                self.file_path_var.set(config.get("file_path", os.path.join(os.getcwd(), "robustness_test.pcapng")))
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
            "vlan_var": self.vlan_var.get(),
            "vlan_id": self.vlan_id_entry.get(),
            "duration_ms": self.duration_ms.get(), # 💡 추가된 설정값 저장
            "pkt_count": self.pkt_count.get(),
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
            filetypes=[
                ("PCAPNG 파일 (Wireshark 기본)", "*.pcapng"),
                ("PCAP 파일 (장비 재생용/구형)", "*.pcap"),
                ("모든 파일", "*.*")
            ],
            title="패킷 저장 위치 및 포맷 선택",
            initialfile="robustness_test.pcapng"
        )
        if file_path:
            self.file_path_var.set(file_path)

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
            desc = "📌 500바이트의 더미 데이터를 보내 25G 대역폭을 마비시킵니다.\n👉 닫혀있는 임의의 Dest Port를 설정하세요."
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
        self.toggle_vlan()

    def toggle_vlan(self):
        if self.vlan_var.get() == 1:
            self.vlan_id_entry.config(state='normal')
        else:
            self.vlan_id_entry.config(state='disabled')

    def cancel_generation(self):
        self.is_cancelled = True
        self.btn_cancel.config(state='disabled')
        self.status_label.config(text="작업 중단 중... 잠시만 기다려주세요.", foreground="orange")

    def start_generation_thread(self):
        try:
            int(self.pkt_count.get())
            float(self.duration_ms.get()) # 💡 주기값도 숫자인지 검사
        except ValueError:
            messagebox.showerror('입력 오류', '패킷 개수와 주기는 숫자만 입력해야 합니다.')
            return

        if self.vlan_var.get() == 1:
            vlan_text = self.vlan_id_entry.get().strip()
            if not vlan_text.isdigit():
                messagebox.showerror('입력 오류', 'VLAN ID를 정확한 숫자로 입력해 주세요. (예: 1000)')
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
            
            # 💡 입력받은 값들 가져오기
            count = int(self.pkt_count.get())
            target_ms = float(self.duration_ms.get())
            filepath = self.file_path_var.get()

            dummy_payload = "X" * 500

            if filepath.lower().endswith('.pcapng'):
                pktdump = PcapNgWriter(filepath)
            else:
                pktdump = PcapWriter(filepath)

            base_time = time.time()
            created_count = 0

            # 💡 [핵심] 입력받은 ms 단위를 초(Seconds)로 변환하여 간격 계산
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

                if "MAC All Zero" in test_type:
                    current_smac = "00:00:00:00:00:00"
                if "출발지 IP All Zero" in test_type:
                    current_sip = "0.0.0.0"
                if "목적지 IP All Zero" in test_type:
                    current_dip = "0.0.0.0"

                eth = Ether(src=current_smac, dst=base_dmac)
                if self.vlan_var.get() == 1:
                    vid = int(self.vlan_id_entry.get())
                    eth = eth / Dot1Q(vlan=vid)

                if "IGMP" in test_type:
                    ttl_val = 1
                ip = IP(src=current_sip, dst=current_dip, ttl=ttl_val)

                if "TCP SYN" in test_type:
                    l4 = TCP(sport=sport, dport=dport, flags='S', seq=random.randint(0, 4294967295))
                    pkt = eth / ip / l4
                elif "UDP" in test_type:
                    l4 = UDP(sport=sport, dport=dport)
                    pkt = eth / ip / l4 / Raw(load=dummy_payload)
                elif "ICMP" in test_type:
                    l4 = ICMP(type=8)
                    pkt = eth / ip / l4 / Raw(load="PingFloodTest")
                elif "IGMP" in test_type:
                    l4 = igmp.IGMP() 
                    pkt = eth / ip / l4
                else:
                    l4 = UDP(sport=sport, dport=dport)
                    pkt = eth / ip / l4 / Raw(load="InvalidPacketTest")

                # 💡 계산된 완벽한 타이밍으로 패킷 시간 도장 찍기
                pkt.time = base_time + (i * time_step) 
                pktdump.write(pkt)
                created_count += 1
                
                if created_count % 50000 == 0:
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