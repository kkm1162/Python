# -*- coding: utf-8 -*-
"""
O-RAN O-RU DDoS 검증 시스템 GUI (v5.0)
Windows 기반 Tkinter GUI로 Linux TRex 서버 제어
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import threading
import paramiko
import json
import os
import sys
import socket
from datetime import datetime
from enum import Enum
import subprocess
import time

# ============================================================================
# 상수 및 Enum 정의
# ============================================================================

class AttackType(Enum):
    """공격 유형 정의"""
    ECPRI_C_PLANE = "eCPRI C-Plane Flood"
    ECPRI_U_PLANE = "eCPRI U-Plane Flood"
    PTP_SYNC = "PTP/IEEE 1588 Sync Flood"
    NETCONF_EXHAUST = "NETCONF Session Exhaustion"
    RRC_SETUP = "RRC SetupRequest Flood"
    PRACH_SPOOF = "PRACH Spoofing Attack"
    F1U_GTPU_FLOOD = "F1-U GTP-U Tunnel Flood"

ATTACK_DESCRIPTIONS = {
    AttackType.ECPRI_C_PLANE: {
        "description": "eCPRI 제어 평면 패킷 대량 전송으로 O-RU의 FPGA 및 CPU 과부하 유발",
        "impact": "O-RU 응답 지연, 제어 메시지 처리 실패, 네트워크 연결 끊김"
    },
    AttackType.ECPRI_U_PLANE: {
        "description": "eCPRI 사용자 평면 데이터 패킷 대량 전송으로 O-RU의 메모리/버퍼 고갈",
        "impact": "무선 신호 품질 저하, 데이터 처리량 감소, 패킷 손실 증가"
    },
    AttackType.PTP_SYNC: {
        "description": "PTP Sync 메시지 대량 전송으로 O-RU의 시간 동기화 방해",
        "impact": "O-RU 시간 동기 오류, 무선 신호 정렬 실패, 네트워크 타이밍 붕괴"
    },
    AttackType.NETCONF_EXHAUST: {
        "description": "NETCONF 세션을 대량 생성하여 O-RU의 연결 리소스 고갈",
        "impact": "관리 인터페이스 접근 불가, 설정 변경 불가, 정상 관리 기능 장애"
    },
    AttackType.RRC_SETUP: {
        "description": "RRC SetupRequest 메시지 대량 전송으로 O-RU의 무선 리소스 고갈",
        "impact": "UE 연결 실패, 무선 신호 끊김, 서비스 가용성 저하"
    },
    AttackType.PRACH_SPOOF: {
        "description": "PRACH 프리앰블을 위장하여 O-RU의 무선 리소스 할당 과부하 유발",
        "impact": "합법적 UE의 접근 차단, 무선 스펙트럼 낭비, 네트워크 혼잡"
    },
    AttackType.F1U_GTPU_FLOOD: {
        "description": "F1-U 인터페이스의 GTP-U 터널 패킷 대량 전송으로 O-RU의 데이터 처리 과부하",
        "impact": "사용자 데이터 처리 지연, 네트워크 처리량 감소, 서비스 중단"
    }
}

# ============================================================================
# SSH 연결 클래스
# ============================================================================

class SSHClient:
    """Paramiko를 사용한 SSH 연결 관리"""
    
    def __init__(self, host, username, password, timeout=10):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.client = None
    
    def connect(self):
        """SSH 연결 시도"""
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.client.connect(
                self.host,
                username=self.username,
                password=self.password,
                timeout=self.timeout
            )
            return True, "SSH 연결 성공"
        except paramiko.AuthenticationException:
            return False, "인증 실패: 사용자명 또는 비밀번호 오류"
        except paramiko.SSHException as e:
            return False, f"SSH 오류: {str(e)}"
        except socket.timeout:
            return False, "연결 타임아웃: Linux 서버에 접근할 수 없습니다"
        except Exception as e:
            return False, f"연결 오류: {str(e)}"
    
    def execute_command(self, command):
        """명령 실행 및 결과 반환"""
        if not self.client:
            return False, "SSH 연결이 없습니다"
        
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=30)
            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')
            return True, output if output else error
        except Exception as e:
            return False, f"명령 실행 오류: {str(e)}"
    
    def upload_file(self, local_path, remote_path):
        """로컬 파일을 원격 서버에 업로드"""
        if not self.client:
            return False, "SSH 연결이 없습니다"
        
        try:
            sftp = self.client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            return True, f"파일 업로드 성공: {remote_path}"
        except Exception as e:
            return False, f"파일 업로드 오류: {str(e)}"
    
    def download_file(self, remote_path, local_path):
        """원격 서버의 파일을 로컬에 다운로드"""
        if not self.client:
            return False, "SSH 연결이 없습니다"
        
        try:
            sftp = self.client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            return True, f"파일 다운로드 성공: {local_path}"
        except Exception as e:
            return False, f"파일 다운로드 오류: {str(e)}"
    
    def read_file(self, remote_path):
        """원격 서버의 파일 내용 읽기"""
        if not self.client:
            return False, "SSH 연결이 없습니다"
        
        try:
            sftp = self.client.open_sftp()
            with sftp.file(remote_path, 'r') as f:
                content = f.read().decode('utf-8', errors='ignore')
            sftp.close()
            return True, content
        except FileNotFoundError:
            return False, f"파일을 찾을 수 없습니다: {remote_path}"
        except Exception as e:
            return False, f"파일 읽기 오류: {str(e)}"
    
    def close(self):
        """연결 종료"""
        if self.client:
            self.client.close()

# ============================================================================
# TRex 통계 모니터 클래스
# ============================================================================

class TRexStatsMonitor:
    """TRex 통계 모니터링 및 파일 기반 수집"""
    
    def __init__(self, ssh_client, stats_file_path="/tmp/trex_stats_output.txt"):
        self.ssh_client = ssh_client
        self.stats_file_path = stats_file_path
        self.stats_data = {}
    
    def get_live_stats(self):
        """파일에서 TRex 통계 읽기"""
        success, content = self.ssh_client.read_file(self.stats_file_path)
        if success:
            try:
                # 파일 내용 파싱
                lines = content.strip().split('\n')
                for line in lines:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        self.stats_data[key.strip()] = value.strip()
                return True, self.stats_data
            except Exception as e:
                return False, f"통계 파싱 오류: {str(e)}"
        else:
            return False, f"통계 파일 읽기 실패: {content}"
    
    def parse_stats(self, stats_output):
        """통계 출력 파싱"""
        parsed = {}
        try:
            lines = stats_output.strip().split('\n')
            for line in lines:
                if '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 2:
                        key = parts[0]
                        value = parts[1]
                        parsed[key] = value
            return parsed
        except Exception as e:
            print(f"파싱 오류: {e}")
            return parsed

# ============================================================================
# O-RU 응답 검증 클래스
# ============================================================================

class ORUResponseValidator:
    """O-RU의 DDoS 공격 영향도 검증"""
    
    def __init__(self, oru_ip, ssh_client=None):
        self.oru_ip = oru_ip
        self.ssh_client = ssh_client
        self.validation_results = {}
    
    def ping_test(self):
        """O-RU Ping 테스트"""
        try:
            # Windows/Linux 호환 ping 명령
            param = "-n" if sys.platform.startswith('win') else "-c"
            result = subprocess.run(
                f"ping {param} 4 {self.oru_ip}",
                shell=True,
                capture_output=True,
                timeout=10
            )
            
            if result.returncode == 0:
                output = result.stdout.decode('utf-8', errors='ignore')
                self.validation_results['ping'] = {
                    'status': 'OK',
                    'detail': f"O-RU 응답성 정상 (시간: {datetime.now().isoformat()})"
                }
                return True, "Ping 응답성 확인됨"
            else:
                self.validation_results['ping'] = {
                    'status': 'FAILED',
                    'detail': "O-RU Ping 응답 없음"
                }
                return False, "Ping 응답 없음"
        except Exception as e:
            self.validation_results['ping'] = {
                'status': 'ERROR',
                'detail': str(e)
            }
            return False, f"Ping 테스트 오류: {str(e)}"
    
    def netconf_test(self):
        """O-RU NETCONF 세션 테스트"""
        if not self.ssh_client:
            return False, "SSH 클라이언트 없음"
        
        try:
            # NETCONF 포트(830) 연결 테스트
            success, output = self.ssh_client.execute_command(
                f"timeout 5 bash -c 'echo | nc -w 1 {self.oru_ip} 830' 2>&1 && echo 'NETCONF_OK' || echo 'NETCONF_FAIL'"
            )
            
            if "NETCONF_OK" in output:
                self.validation_results['netconf'] = {
                    'status': 'OK',
                    'detail': "NETCONF 포트 응답성 정상"
                }
                return True, "NETCONF 세션 정상"
            else:
                self.validation_results['netconf'] = {
                    'status': 'FAILED',
                    'detail': "NETCONF 포트 응답 없음"
                }
                return False, "NETCONF 포트 응답 없음"
        except Exception as e:
            self.validation_results['netconf'] = {
                'status': 'ERROR',
                'detail': str(e)
            }
            return False, f"NETCONF 테스트 오류: {str(e)}"
    
    def packet_capture_test(self):
        """O-RU 패킷 캡처 및 분석"""
        if not self.ssh_client:
            return False, "SSH 클라이언트 없음"
        
        try:
            # tcpdump로 간단한 패킷 캡처 (5초)
            success, output = self.ssh_client.execute_command(
                f"timeout 5 tcpdump -i any 'host {self.oru_ip}' -c 10 2>&1 | tail -5"
            )
            
            if "packets captured" in output or len(output) > 0:
                self.validation_results['packet_capture'] = {
                    'status': 'OK',
                    'detail': "O-RU 패킷 활동 감지됨"
                }
                return True, "O-RU 패킷 활동 감지"
            else:
                self.validation_results['packet_capture'] = {
                    'status': 'WARNING',
                    'detail': "O-RU 패킷 활동 감지 안 됨"
                }
                return False, "O-RU 패킷 활동 감지 안 됨"
        except Exception as e:
            self.validation_results['packet_capture'] = {
                'status': 'ERROR',
                'detail': str(e)
            }
            return False, f"패킷 캡처 오류: {str(e)}"
    
    def get_validation_report(self):
        """검증 리포트 반환"""
        return self.validation_results

# ============================================================================
# 메인 GUI 클래스
# ============================================================================

class ORanORUValidationGUI:
    """O-RAN O-RU DDoS 검증 시스템 메인 GUI"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("O-RAN O-RU DDoS 검증 시스템 v5.0")
        self.root.geometry("1200x800")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # SSH 클라이언트 및 모니터
        self.ssh_client = None
        self.stats_monitor = None
        self.oru_validator = None
        
        # 스레드 제어
        self.monitoring_active = False
        self.pcap_monitor_active = False
        self.monitor_thread = None
        self.pcap_thread = None
        
        # 설정 로드
        self.config = self.load_config()
        
        # UI 생성
        self.create_widgets()
        self.load_config_to_ui()
    
    # ========================================================================
    # 설정 파일 관리
    # ========================================================================
    
    def load_config(self):
        """설정 파일 로드 및 기본값 설정"""
        config_file = 'oran_ru_config.json'
        default_config = {
            'linux_ip': '192.168.9.249',
            'ssh_user': 'slab',
            'ssh_password': '',
            'trex_path': '/home/slab/trex/v3.08',
            'trex_port': '0',
            'attacker_mac': '00:11:22:33:44:55',
            'oru_mac': 'AA:BB:CC:DD:EE:FF',
            'attacker_ip': '192.168.11.100',
            'oru_ip': '10.0.60.254',
            'dest_port': '830',
            'duration': '60',
            'line_rate': '10.0',
            'packet_size': '64',
            'vlan_enabled': False,
            'vlan_id': '1',
            'pcap_path': '/tmp/pcap_output',
            'selected_attack': 'ECPRI_C_PLANE'
        }
        
        try:
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    default_config.update(loaded_config)
                    return default_config
            else:
                self.save_config(default_config)
                return default_config
        except json.JSONDecodeError:
            print(f"설정 파일 형식 오류: {config_file}")
            return default_config
        except Exception as e:
            print(f"설정 파일 로드 오류: {e}")
            return default_config
    
    def save_config(self, config=None):
        """설정 파일에 저장"""
        if config is None:
            config = {
                'linux_ip': self.entry_linux_ip.get(),
                'ssh_user': self.entry_ssh_user.get(),
                'ssh_password': self.entry_ssh_password.get(),
                'trex_path': self.entry_trex_path.get(),
                'trex_port': self.trex_port_var.get(),
                'attacker_mac': self.entry_attacker_mac.get(),
                'oru_mac': self.entry_oru_mac.get(),
                'attacker_ip': self.entry_attacker_ip.get(),
                'oru_ip': self.entry_oru_ip.get(),
                'dest_port': self.entry_dest_port.get(),
                'duration': self.entry_duration.get(),
                'line_rate': self.entry_line_rate.get(),
                'packet_size': self.packet_size_var.get(),
                'vlan_enabled': self.vlan_enabled_var.get(),
                'vlan_id': self.entry_vlan_id.get(),
                'pcap_path': self.entry_pcap_path.get(),
                'selected_attack': self.attack_type_var.get()
            }
        
        try:
            with open('oran_ru_config.json', 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            print("설정이 저장되었습니다.")
        except Exception as e:
            print(f"설정 저장 오류: {e}")
    
    def load_config_to_ui(self):
        """로드된 설정을 UI에 적용"""
        self.entry_linux_ip.insert(0, self.config.get('linux_ip', '192.168.9.249'))
        self.entry_ssh_user.insert(0, self.config.get('ssh_user', 'slab'))
        self.entry_ssh_password.insert(0, self.config.get('ssh_password', ''))
        self.entry_trex_path.insert(0, self.config.get('trex_path', '/home/slab/trex/v3.08'))
        self.trex_port_var.set(self.config.get('trex_port', '0'))
        
        self.entry_attacker_mac.insert(0, self.config.get('attacker_mac', '00:11:22:33:44:55'))
        self.entry_oru_mac.insert(0, self.config.get('oru_mac', 'AA:BB:CC:DD:EE:FF'))
        self.entry_attacker_ip.insert(0, self.config.get('attacker_ip', '192.168.11.100'))
        self.entry_oru_ip.insert(0, self.config.get('oru_ip', '10.0.60.254'))
        self.entry_dest_port.insert(0, self.config.get('dest_port', '830'))
        
        self.entry_duration.insert(0, self.config.get('duration', '60'))
        self.entry_line_rate.insert(0, self.config.get('line_rate', '10.0'))
        self.packet_size_var.set(self.config.get('packet_size', '64'))
        self.vlan_enabled_var.set(self.config.get('vlan_enabled', False))
        self.entry_vlan_id.insert(0, str(self.config.get('vlan_id', '1')))
        
        self.entry_pcap_path.insert(0, self.config.get('pcap_path', '/tmp/pcap_output'))
        self.attack_type_var.set(self.config.get('selected_attack', 'ECPRI_C_PLANE'))
    
    # ========================================================================
    # UI 생성
    # ========================================================================
    
    def create_widgets(self):
        """메인 UI 생성"""
        # 노트북 (탭) 생성
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 탭 생성
        self.create_global_settings_tab()
        self.create_attack_configuration_tab()
        self.create_trex_control_tab()
        self.create_help_tab()
    
    def create_global_settings_tab(self):
        """글로벌 설정 탭"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Global Settings")
        
        # 좌측 프레임 (서버 설정)
        left_frame = ttk.LabelFrame(frame, text="Linux TRex Server Settings", padding=10)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        ttk.Label(left_frame, text="Linux IP:").grid(row=0, column=0, sticky=tk.W)
        self.entry_linux_ip = ttk.Entry(left_frame, width=30)
        self.entry_linux_ip.grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(left_frame, text="SSH User:").grid(row=1, column=0, sticky=tk.W)
        self.entry_ssh_user = ttk.Entry(left_frame, width=30)
        self.entry_ssh_user.grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(left_frame, text="SSH Password:").grid(row=2, column=0, sticky=tk.W)
        self.entry_ssh_password = ttk.Entry(left_frame, width=30, show='*')
        self.entry_ssh_password.grid(row=2, column=1, padx=5, pady=5)
        
        ttk.Label(left_frame, text="TRex Path:").grid(row=3, column=0, sticky=tk.W)
        self.entry_trex_path = ttk.Entry(left_frame, width=30)
        self.entry_trex_path.grid(row=3, column=1, padx=5, pady=5)
        
        ttk.Label(left_frame, text="TRex Port (NIC):").grid(row=4, column=0, sticky=tk.W)
        self.trex_port_var = tk.StringVar(value="0")
        self.combo_trex_port = ttk.Combobox(left_frame, textvariable=self.trex_port_var, 
                                            values=["0", "1"], state="readonly", width=28)
        self.combo_trex_port.grid(row=4, column=1, padx=5, pady=5)
        
        # 우측 프레임 (네트워크 설정)
        right_frame = ttk.LabelFrame(frame, text="Network Parameters", padding=10)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        ttk.Label(right_frame, text="Attacker MAC:").grid(row=0, column=0, sticky=tk.W)
        self.entry_attacker_mac = ttk.Entry(right_frame, width=30)
        self.entry_attacker_mac.grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(right_frame, text="O-RU MAC:").grid(row=1, column=0, sticky=tk.W)
        self.entry_oru_mac = ttk.Entry(right_frame, width=30)
        self.entry_oru_mac.grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(right_frame, text="Attacker IP:").grid(row=2, column=0, sticky=tk.W)
        self.entry_attacker_ip = ttk.Entry(right_frame, width=30)
        self.entry_attacker_ip.grid(row=2, column=1, padx=5, pady=5)
        
        ttk.Label(right_frame, text="O-RU IP:").grid(row=3, column=0, sticky=tk.W)
        self.entry_oru_ip = ttk.Entry(right_frame, width=30)
        self.entry_oru_ip.grid(row=3, column=1, padx=5, pady=5)
        
        ttk.Label(right_frame, text="Dest Port:").grid(row=4, column=0, sticky=tk.W)
        self.entry_dest_port = ttk.Entry(right_frame, width=30)
        self.entry_dest_port.grid(row=4, column=1, padx=5, pady=5)
        
        # 연결 버튼
        button_frame = ttk.Frame(frame)
        button_frame.pack(fill=tk.X, padx=5, pady=10)
        
        ttk.Button(button_frame, text="Test SSH Connection", command=self.test_ssh_connection).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Save Settings", command=self.save_settings).pack(side=tk.LEFT, padx=5)
        
        # 상태 표시
        self.label_connection_status = ttk.Label(button_frame, text="Status: Disconnected", foreground="red")
        self.label_connection_status.pack(side=tk.RIGHT, padx=5)
    
    def create_attack_configuration_tab(self):
        """공격 설정 탭"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Attack Configuration")
        
        # 상단: 공격 유형 선택
        top_frame = ttk.LabelFrame(frame, text="Attack Type", padding=10)
        top_frame.pack(fill=tk.X, padx=5, pady=5)
        
        ttk.Label(top_frame, text="Select Attack:").pack(side=tk.LEFT)
        self.attack_type_var = tk.StringVar(value="ECPRI_C_PLANE")
        self.combo_attack_type = ttk.Combobox(
            top_frame,
            textvariable=self.attack_type_var,
            values=[attack.name for attack in AttackType],
            state="readonly",
            width=30
        )
        self.combo_attack_type.pack(side=tk.LEFT, padx=10)
        self.combo_attack_type.bind("<<ComboboxSelected>>", self.on_attack_type_changed)
        
        # 공격 설명 표시
        self.text_attack_description = scrolledtext.ScrolledText(top_frame, height=4, width=80)
        self.text_attack_description.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.update_attack_description()
        
        # 중앙: 공격 파라미터
        middle_frame = ttk.LabelFrame(frame, text="Attack Parameters", padding=10)
        middle_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Duration
        ttk.Label(middle_frame, text="Duration (sec):").grid(row=0, column=0, sticky=tk.W)
        self.entry_duration = ttk.Entry(middle_frame, width=20)
        self.entry_duration.grid(row=0, column=1, padx=5, pady=5)
        
        # Line Rate
        ttk.Label(middle_frame, text="Line Rate (Gbps):").grid(row=0, column=2, sticky=tk.W)
        self.entry_line_rate = ttk.Entry(middle_frame, width=20)
        self.entry_line_rate.grid(row=0, column=3, padx=5, pady=5)
        
        # Packet Size
        ttk.Label(middle_frame, text="Packet Size:").grid(row=1, column=0, sticky=tk.W)
        self.packet_size_var = tk.StringVar(value="64")
        self.combo_packet_size = ttk.Combobox(
            middle_frame,
            textvariable=self.packet_size_var,
            values=["64", "256", "512", "1024", "9000"],
            state="readonly",
            width=18
        )
        self.combo_packet_size.grid(row=1, column=1, padx=5, pady=5)
        
        # VLAN 설정
        ttk.Label(middle_frame, text="VLAN:").grid(row=1, column=2, sticky=tk.W)
        self.vlan_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(middle_frame, text="Enabled", variable=self.vlan_enabled_var).grid(row=1, column=3, padx=5, pady=5)
        
        ttk.Label(middle_frame, text="VLAN ID:").grid(row=2, column=0, sticky=tk.W)
        self.entry_vlan_id = ttk.Entry(middle_frame, width=20)
        self.entry_vlan_id.grid(row=2, column=1, padx=5, pady=5)
        
        # PCAP 설정
        ttk.Label(middle_frame, text="PCAP Output Path:").grid(row=2, column=2, sticky=tk.W)
        self.entry_pcap_path = ttk.Entry(middle_frame, width=20)
        self.entry_pcap_path.grid(row=2, column=3, padx=5, pady=5)
        
        # 하단: 공격 실행 버튼
        bottom_frame = ttk.Frame(frame)
        bottom_frame.pack(fill=tk.X, padx=5, pady=10)
        
        ttk.Button(bottom_frame, text="Start Attack", command=self.start_attack).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Stop Attack", command=self.stop_attack).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Validate O-RU Response", command=self.validate_oru_response).pack(side=tk.LEFT, padx=5)
    
    def create_trex_control_tab(self):
        """TRex 제어 탭"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="TRex Control")
        
        # 상단: TRex 상태
        top_frame = ttk.LabelFrame(frame, text="TRex Status", padding=10)
        top_frame.pack(fill=tk.X, padx=5, pady=5)
        
        self.label_trex_status = ttk.Label(top_frame, text="Status: Unknown", foreground="gray")
        self.label_trex_status.pack(side=tk.LEFT, padx=10)
        
        ttk.Button(top_frame, text="Check TRex Status", command=self.check_trex_status).pack(side=tk.LEFT, padx=5)
        
        # 중앙: TRex 포트 통계
        middle_frame = ttk.LabelFrame(frame, text="Port Statistics", padding=10)
        middle_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 통계 표시 영역
        self.text_stats = scrolledtext.ScrolledText(middle_frame, height=20, width=100)
        self.text_stats.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 하단: 통계 새로고침
        bottom_frame = ttk.Frame(frame)
        bottom_frame.pack(fill=tk.X, padx=5, pady=10)
        
        ttk.Button(bottom_frame, text="Refresh Statistics", command=self.refresh_stats).pack(side=tk.LEFT, padx=5)
        self.auto_refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom_frame, text="Auto Refresh (5s)", variable=self.auto_refresh_var, 
                       command=self.toggle_auto_refresh).pack(side=tk.LEFT, padx=5)
        
        # PCAP 파일 목록
        pcap_frame = ttk.LabelFrame(frame, text="PCAP Files", padding=10)
        pcap_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.listbox_pcap = tk.Listbox(pcap_frame, height=5)
        self.listbox_pcap.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        ttk.Button(pcap_frame, text="Refresh PCAP List", command=self.refresh_pcap_list).pack(side=tk.LEFT, padx=5)
        ttk.Button(pcap_frame, text="Download PCAP", command=self.download_pcap).pack(side=tk.LEFT, padx=5)
    
    def create_help_tab(self):
        """도움말 탭"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Help")
        
        help_text = """
O-RAN O-RU DDoS 검증 시스템 v5.0

[시스템 구성]
- Windows GUI (Tkinter): 사용자 인터페이스 및 공격 제어
- Linux TRex 서버: 실제 패킷 생성 및 전송
- SSH 연결: Windows와 Linux 간의 통신

[공격 유형]
1. eCPRI C-Plane Flood: eCPRI 제어 평면 패킷 대량 전송
2. eCPRI U-Plane Flood: eCPRI 사용자 평면 데이터 대량 전송
3. PTP/IEEE 1588 Sync: PTP Sync 메시지 대량 전송
4. NETCONF Session Exhaustion: NETCONF 세션 대량 생성
5. RRC SetupRequest Flood: RRC SetupRequest 메시지 대량 전송
6. PRACH Spoofing: PRACH 프리앰블 위장 공격
7. F1-U GTP-U Tunnel Flood: GTP-U 터널 패킷 대량 전송

[사용 가이드]
1. Global Settings 탭에서 Linux 서버 및 네트워크 정보 입력
2. "Test SSH Connection" 버튼으로 연결 확인
3. Attack Configuration 탭에서 공격 유형 및 파라미터 설정
4. "Start Attack" 버튼으로 공격 시작
5. TRex Control 탭에서 실시간 통계 모니터링
6. "Validate O-RU Response" 버튼으로 O-RU 영향도 확인
7. "Stop Attack" 버튼으로 공격 중지

[주의사항]
- 모든 공격은 테스트 환경에서만 수행하세요
- 실제 운영 네트워크에서는 절대 사용하지 마세요
- O-RU IP 및 네트워크 정보가 정확한지 확인하세요

[지원]
기술 문제 발생 시 로그를 확인하고 관리자에게 보고하세요.
        """
        
        text_widget = scrolledtext.ScrolledText(frame, wrap=tk.WORD, padx=10, pady=10)
        text_widget.pack(fill=tk.BOTH, expand=True)
        text_widget.insert(tk.END, help_text)
        text_widget.config(state=tk.DISABLED)
    
    # ========================================================================
    # 이벤트 처리
    # ========================================================================
    
    def on_attack_type_changed(self, event=None):
        """공격 유형 변경 시 설명 업데이트"""
        self.update_attack_description()
    
    def update_attack_description(self):
        """공격 설명 업데이트"""
        selected = self.attack_type_var.get()
        try:
            attack_type = AttackType[selected]
            description = ATTACK_DESCRIPTIONS.get(attack_type, {})
            
            text = f"Description: {description.get('description', 'N/A')}\n\n"
            text += f"Impact on O-RU: {description.get('impact', 'N/A')}"
            
            self.text_attack_description.config(state=tk.NORMAL)
            self.text_attack_description.delete(1.0, tk.END)
            self.text_attack_description.insert(tk.END, text)
            self.text_attack_description.config(state=tk.DISABLED)
        except KeyError:
            pass
    
    def test_ssh_connection(self):
        """SSH 연결 테스트"""
        linux_ip = self.entry_linux_ip.get()
        ssh_user = self.entry_ssh_user.get()
        ssh_password = self.entry_ssh_password.get()
        
        if not all([linux_ip, ssh_user]):
            messagebox.showerror("입력 오류", "Linux IP와 SSH 사용자명을 입력하세요.")
            return
        
        # 별도 스레드에서 연결 시도
        def connect_thread():
            self.ssh_client = SSHClient(linux_ip, ssh_user, ssh_password)
            success, message = self.ssh_client.connect()
            
            if success:
                self.label_connection_status.config(text=f"Status: {message}", foreground="green")
                messagebox.showinfo("연결 성공", message)
                
                # 통계 모니터 초기화
                self.stats_monitor = TRexStatsMonitor(self.ssh_client)
                
                # O-RU 검증자 초기화
                self.oru_validator = ORUResponseValidator(self.entry_oru_ip.get(), self.ssh_client)
                
                # 자동으로 TRex 시작
                self.auto_start_trex()
            else:
                self.label_connection_status.config(text=f"Status: {message}", foreground="red")
                messagebox.showerror("연결 실패", message)
        
        thread = threading.Thread(target=connect_thread, daemon=True)
        thread.start()
    
    def auto_start_trex(self):
        """TRex 자동 시작"""
        if not self.ssh_client:
            return
        
        def start_trex_thread():
            trex_path = self.entry_trex_path.get()
            trex_port = self.trex_port_var.get()
            
            # TRex 시작 명령
            start_cmd = f"cd {trex_path} && sudo ./t-rex-64 -i &"
            success, output = self.ssh_client.execute_command(start_cmd)
            
            if success:
                print("TRex 시작 명령 전송됨")
                time.sleep(3)
                
                # 통계 모니터 시작
                self.start_stats_monitor()
            else:
                print(f"TRex 시작 오류: {output}")
        
        thread = threading.Thread(target=start_trex_thread, daemon=True)
        thread.start()
    
    def start_stats_monitor(self):
        """TRex 통계 모니터 시작"""
        if self.monitoring_active:
            return
        
        self.monitoring_active = True
        
        def monitor_thread_func():
            while self.monitoring_active:
                if self.ssh_client and self.stats_monitor:
                    success, stats = self.stats_monitor.get_live_stats()
                    if success:
                        # UI 업데이트 (메인 스레드에서)
                        self.root.after(0, self.update_stats_display, stats)
                
                time.sleep(5)  # 5초마다 갱신
        
        self.monitor_thread = threading.Thread(target=monitor_thread_func, daemon=True)
        self.monitor_thread.start()
    
    def update_stats_display(self, stats):
        """통계 표시 업데이트"""
        self.text_stats.config(state=tk.NORMAL)
        self.text_stats.delete(1.0, tk.END)
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.text_stats.insert(tk.END, f"Last Updated: {timestamp}\n\n")
        
        if isinstance(stats, dict):
            for key, value in stats.items():
                self.text_stats.insert(tk.END, f"{key}: {value}\n")
        else:
            self.text_stats.insert(tk.END, str(stats))
        
        self.text_stats.config(state=tk.DISABLED)
    
    def refresh_stats(self):
        """통계 수동 새로고침"""
        if self.ssh_client and self.stats_monitor:
            success, stats = self.stats_monitor.get_live_stats()
            if success:
                self.update_stats_display(stats)
            else:
                messagebox.showerror("오류", f"통계 조회 실패: {stats}")
    
    def toggle_auto_refresh(self):
        """자동 새로고침 토글"""
        if self.auto_refresh_var.get():
            self.start_stats_monitor()
        else:
            self.monitoring_active = False
    
    def refresh_pcap_list(self):
        """PCAP 파일 목록 갱신"""
        if not self.ssh_client:
            messagebox.showerror("오류", "SSH 연결이 없습니다.")
            return
        
        pcap_path = self.entry_pcap_path.get()
        success, output = self.ssh_client.execute_command(f"ls -lah {pcap_path} 2>/dev/null | grep -E '\\.pcap|\\.pcapng' | awk '{{print $NF}}'")
        
        self.listbox_pcap.delete(0, tk.END)
        if success and output:
            for line in output.strip().split('\n'):
                if line:
                    self.listbox_pcap.insert(tk.END, line)
    
    def download_pcap(self):
        """PCAP 파일 다운로드"""
        selection = self.listbox_pcap.curselection()
        if not selection:
            messagebox.showwarning("선택 없음", "다운로드할 PCAP 파일을 선택하세요.")
            return
        
        pcap_filename = self.listbox_pcap.get(selection[0])
        pcap_path = self.entry_pcap_path.get()
        remote_path = f"{pcap_path}/{pcap_filename}"
        
        local_path = filedialog.asksaveasfilename(defaultextension=".pcap")
        if local_path:
            def download_thread():
                success, message = self.ssh_client.download_file(remote_path, local_path)
                if success:
                    messagebox.showinfo("성공", f"{pcap_filename} 다운로드 완료")
                else:
                    messagebox.showerror("오류", message)
            
            thread = threading.Thread(target=download_thread, daemon=True)
            thread.start()
    
    def start_attack(self):
        """공격 시작"""
        if not self.ssh_client:
            messagebox.showerror("오류", "먼저 Linux 서버에 연결하세요.")
            return
        
        # 설정 저장
        self.save_settings()
        
        def attack_thread():
            attack_type = self.attack_type_var.get()
            duration = self.entry_duration.get()
            line_rate = self.entry_line_rate.get()
            packet_size = self.packet_size_var.get()
            
            # my_attack.py 실행 명령 생성
            trex_path = self.entry_trex_path.get()
            cmd = f"cd {trex_path} && python3 my_attack.py --attack-type {attack_type} --duration {duration} --line-rate {line_rate} --packet-size {packet_size}"
            
            success, output = self.ssh_client.execute_command(cmd)
            if success:
                messagebox.showinfo("성공", f"공격 시작: {attack_type}\n결과:\n{output[:200]}")
            else:
                messagebox.showerror("오류", f"공격 실행 오류: {output}")
        
        thread = threading.Thread(target=attack_thread, daemon=True)
        thread.start()
    
    def stop_attack(self):
        """공격 중지"""
        if not self.ssh_client:
            messagebox.showerror("오류", "SSH 연결이 없습니다.")
            return
        
        def stop_thread():
            success, output = self.ssh_client.execute_command("pkill -f 'my_attack.py' || echo 'No process running'")
            if success:
                messagebox.showinfo("성공", "공격이 중지되었습니다.")
        
        thread = threading.Thread(target=stop_thread, daemon=True)
        thread.start()
    
    def validate_oru_response(self):
        """O-RU 응답 검증"""
        if not self.oru_validator:
            messagebox.showerror("오류", "먼저 Linux 서버에 연결하세요.")
            return
        
        def validation_thread():
            results = {}
            
            # Ping 테스트
            ping_success, ping_msg = self.oru_validator.ping_test()
            results['Ping'] = ping_msg
            
            # NETCONF 테스트
            netconf_success, netconf_msg = self.oru_validator.netconf_test()
            results['NETCONF'] = netconf_msg
            
            # 패킷 캡처 테스트
            packet_success, packet_msg = self.oru_validator.packet_capture_test()
            results['Packet Capture'] = packet_msg
            
            # UI 업데이트
            validation_report = self.oru_validator.get_validation_report()
            
            report_text = "O-RU Response Validation Report\n"
            report_text += "=" * 50 + "\n"
            for test_name, result in validation_report.items():
                status = result.get('status', 'UNKNOWN')
                detail = result.get('detail', '')
                report_text += f"\n{test_name.upper()}:\n  Status: {status}\n  Detail: {detail}\n"
            
            messagebox.showinfo("검증 결과", report_text)
        
        thread = threading.Thread(target=validation_thread, daemon=True)
        thread.start()
    
    def check_trex_status(self):
        """TRex 상태 확인"""
        if not self.ssh_client:
            self.label_trex_status.config(text="Status: Not Connected", foreground="red")
            return
        
        def status_thread():
            success, output = self.ssh_client.execute_command("ps aux | grep 't-rex-64' | grep -v grep")
            
            if success and output:
                self.label_trex_status.config(text="Status: ✓ Running", foreground="green")
            else:
                self.label_trex_status.config(text="Status: ✗ Not Running", foreground="red")
        
        thread = threading.Thread(target=status_thread, daemon=True)
        thread.start()
    
    def save_settings(self):
        """설정 저장"""
        self.save_config()
        messagebox.showinfo("성공", "설정이 저장되었습니다.")
    
    def on_closing(self):
        """GUI 종료"""
        if self.ssh_client:
            # TRex 중지
            self.ssh_client.execute_command("pkill -f 't-rex-64' || echo 'TRex not running'")
            self.ssh_client.execute_command("pkill -f 'my_attack.py' || echo 'Attack not running'")
            self.ssh_client.close()
        
        # 모니터링 중지
        self.monitoring_active = False
        self.pcap_monitor_active = False
        
        self.root.destroy()
        sys.exit(0)

# ============================================================================
# 메인 실행
# ============================================================================

if __name__ == "__main__":
    root = tk.Tk()
    gui = ORanORUValidationGUI(root)
    root.mainloop()

# python -m PyInstaller --noconsole --onefile --icon="DDOS.ico" oran_trex_master.py