import os
import re
from datetime import datetime
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import paramiko
import stat
from posixpath import join as posix_join, dirname as posix_dirname
import time
import xml.etree.ElementTree as ET
import json
import base64
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

class MPlaneAnalyzerApp:
    """O-RAN M-Plane Conformance Analyzer - 분석 및 감시 애플리케이션"""
    
    # 상수 정의 (유지보수 용이성 향상)
    SETTINGS_FILE = "netconf_settings.json"
    REMOTE_LOGS_DIR = "RemoteLogs"
    REPORTS_DIR = "Reports"
    SSH_TIMEOUT = 10
    MONITORING_INTERVAL = 2  # 초
    REMOTE_MONITORING_INTERVAL = 3  # 초
    
    # UI 색상 정의
    COLOR_PASS = "#4CAF50"
    COLOR_FAIL = "#f44336"
    COLOR_UNKNOWN = "#FFC107"
    COLOR_ACTIVE = "#00BCD4"
    
    def __init__(self, root):
        self.root = root
        self.root.title("O-RAN M-Plane Conformance Analyzer")
        self.root.geometry("1100x850")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # ===== 모니터링 상태 =====
        self.is_monitoring = False
        self.monitoring_thread = None
        self.loaded_files = []

        self.is_folder_monitoring = False
        self.folder_monitoring_thread = None
        self.monitored_folder_path = ""
        self.processed_files_in_folder = set()
        self.processed_files_sizes = {}

        self.is_remote_folder_monitoring = False
        self.remote_folder_monitoring_thread = None
        self.remote_monitored_folder_path = ""
        self.remote_processed_files = set()
        
        self.ssh_client = None
        self.sftp_client = None

        # ===== 키워드 매핑 =====
        self.keyword_to_test_map = {
            "software-download": "Software Management (다운로드)",
            "software-install": "Software Management (설치)",
            "software-activate": "Software Management (활성화)",
            "active-alarms": "Fault Management (알람 검증)",
            "o-ran-sync": "S-Plane (PTP 동기화 설정)",
            "o-ran-uplane-conf": "C/U-Plane (Endpoint 설정)",
            "o-ran-mplane-int": "M-Plane (네트워크 인터페이스)",
            "get-config": "Configuration Management (조회)",
            "edit-config": "Configuration Management (설정)"
        }

        # ===== 테스트 항목 =====
        self.test_items = [
            "M-Plane: Startup & Call Home",
            "M-Plane: NETCONF Capability (hello)",
            "M-Plane: Supervision",
            "M-Plane: User Account",
            "Config Management: <get> / <get-config>",
            "Config Management: edit-config (rollback)",
            "Config Management: State Change (admin/oper)",
            "Config Management: Subscription & Notification",
            "Software Management: Down/Install/Activate",
            "C/U-Plane: Full Configuration",
            "Fault Management",
            "Log Management: Troubleshooting Log",
            "Generic: RPC Error Validation",
        ]
        self.item_vars = {}
        self.test_item_history = {}
        self.tree_item_details = {}
        self.test_item_last_status = {}  # 👈 각 항목의 마지막 상태 저장

        self.build_ui()

    def on_closing(self):
        """앱 종료 시 자원 정리"""
        self.is_monitoring = False
        self.is_folder_monitoring = False
        self.is_remote_folder_monitoring = False
        
        # SSH/SFTP 안전하게 종료
        if self.sftp_client:
            try: 
                self.sftp_client.close()
            except Exception:
                pass
        if self.ssh_client:
            try: 
                self.ssh_client.close()
            except Exception:
                pass
            
        self.root.destroy()

    def build_ui(self):
        """메인 UI 구성"""
        # 좌측 패널
        left_frame = tk.Frame(self.root, width=350, bg="#f5f5f5", relief="sunken", bd=1)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)
        left_frame.pack_propagate(False)

        # 우측 패널
        right_frame = tk.Frame(self.root)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # ========== 좌측: Conformance Test 항목 선택 ==========
        tk.Label(left_frame, text="✅ Conformance Test 항목", 
                font=("Arial", 11, "bold"), bg="#f5f5f5").pack(anchor=tk.W, pady=(10, 5), padx=10)

        btn_frame = tk.Frame(left_frame, bg="#f5f5f5")
        btn_frame.pack(fill=tk.X, padx=10, pady=2)
        tk.Button(btn_frame, text="전체 선택", command=self.select_all_items, width=12).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="전체 해제", command=self.clear_all_items, width=12).pack(side=tk.LEFT, padx=2)

        items_frame = tk.Frame(left_frame, bg="white", relief="solid", bd=1)
        items_frame.pack(fill=tk.X, padx=10, pady=5)

        for item in self.test_items:
            var = tk.BooleanVar(value=True)
            self.item_vars[item] = var
            cb = tk.Checkbutton(items_frame, text=item, variable=var, bg="white", anchor=tk.W, font=("Arial", 9))
            cb.pack(fill=tk.X, padx=5, pady=2)

        # ========== 좌측: 로그 파일 로드 ==========
        tk.Label(left_frame, text="📂 로그 파일 목록", 
                font=("Arial", 11, "bold"), bg="#f5f5f5").pack(anchor=tk.W, pady=(20, 5), padx=10)

        tk.Button(left_frame, text="+ 로컬 로그 파일 불러오기", command=self.load_files, 
                 bg="#2196F3", fg="white", font=("Arial", 9, "bold")).pack(fill=tk.X, padx=10, pady=2)
        self.scp_btn = tk.Button(left_frame, text="... 리눅스 PC에서 불러오기 (SCP)", 
                                command=self.load_remote_file_scp, bg="#009688", fg="white")
        self.scp_btn.pack(fill=tk.X, padx=10, pady=2)
        tk.Button(left_frame, text="목록 비우기", command=self.clear_files, 
                 bg="#f44336", fg="white").pack(fill=tk.X, padx=10, pady=(2,5))

        list_frame = tk.Frame(left_frame)
        self.file_listbox = tk.Listbox(list_frame, font=("Arial", 9), selectmode=tk.EXTENDED)
        list_scroll = ttk.Scrollbar(list_frame, command=self.file_listbox.yview)
        self.file_listbox.configure(yscrollcommand=list_scroll.set)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ========== 좌측: 분석 실행 버튼 (하단 고정) ==========
        bottom_btn_frame = tk.Frame(left_frame, bg="#f5f5f5")
        bottom_btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 10))

        folder_btn_frame = tk.Frame(bottom_btn_frame, bg="#f5f5f5")
        folder_btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))
        
        self.folder_monitor_btn = tk.Button(folder_btn_frame, text="📂 로컬 폴더 감시", 
                                           command=self.toggle_folder_monitoring, 
                                           bg="#673AB7", fg="white", font=("Arial", 9, "bold"), height=2)
        self.folder_monitor_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        
        self.remote_folder_monitor_btn = tk.Button(folder_btn_frame, text="🌐 원격 폴더 감시", 
                                                   command=self.toggle_remote_folder_monitoring, 
                                                   bg="#3F51B5", fg="white", font=("Arial", 9, "bold"), height=2)
        self.remote_folder_monitor_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        analysis_btn_frame = tk.Frame(bottom_btn_frame, bg="#f5f5f5")
        analysis_btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(10, 5))

        self.run_btn = tk.Button(analysis_btn_frame, text="▶ 전체 로그 분석 (cat)", 
                                command=self.run_analysis, bg=self.COLOR_PASS, fg="white", 
                                font=("Arial", 10, "bold"), height=2)
        self.run_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        
        self.tail_btn = tk.Button(analysis_btn_frame, text="▷ 실시간 분석 (tail -f)", 
                                 command=self.toggle_realtime_analysis, bg="#FF9800", fg="white", 
                                 font=("Arial", 10, "bold"), height=2)
        self.tail_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ========== 우측: 분석 결과 및 리포트 저장 ==========
        header_frame = tk.Frame(right_frame)
        header_frame.pack(fill=tk.X, pady=5)
        tk.Label(header_frame, text="📊 분석 결과 창", font=("Arial", 11, "bold")).pack(side=tk.LEFT)

        right_header_buttons = tk.Frame(header_frame)
        right_header_buttons.pack(side=tk.RIGHT)

        tk.Button(right_header_buttons, text="🗑️ 결과 초기화", command=self.clear_analysis_results, 
                 bg="#757575", fg="white", font=("Arial", 10, "bold"), padx=10).pack(side=tk.LEFT, padx=(0, 5))
        tk.Button(right_header_buttons, text="💾 리포트 파일로 저장", command=self.export_report, 
                 bg="#9C27B0", fg="white", font=("Arial", 10, "bold"), padx=10).pack(side=tk.LEFT)

        paned_window = ttk.PanedWindow(right_frame, orient=tk.VERTICAL)
        paned_window.pack(fill=tk.BOTH, expand=True)

        # 상단: 요약 Treeview
        summary_frame = ttk.Frame(paned_window, height=250)
        paned_window.add(summary_frame, weight=2)

        summary_scroll_y = ttk.Scrollbar(summary_frame, orient=tk.VERTICAL)
        self.summary_tree = ttk.Treeview(
            summary_frame,
            columns=("item", "result", "summary"),
            show="headings",
            yscrollcommand=summary_scroll_y.set
        )
        summary_scroll_y.config(command=self.summary_tree.yview)

        self.summary_tree.heading("item", text="Test Item")
        self.summary_tree.heading("result", text="Result")
        self.summary_tree.heading("summary", text="Summary")
        self.summary_tree.column("item", width=250, anchor='w')
        self.summary_tree.column("result", width=80, anchor='center')
        self.summary_tree.column("summary", width=400, anchor='w')
        summary_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.summary_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.summary_tree.tag_configure("pass", foreground=self.COLOR_PASS)
        self.summary_tree.tag_configure("fail", foreground=self.COLOR_FAIL)
        self.summary_tree.tag_configure("unknown", foreground=self.COLOR_UNKNOWN)
        self.summary_tree.tag_configure("active", foreground=self.COLOR_ACTIVE)
        self.summary_tree.bind("<<TreeviewSelect>>", self._on_summary_select)

        # 하단: 상세 로그 Text
        detail_frame = ttk.Frame(paned_window, height=300)
        paned_window.add(detail_frame, weight=1)

        self.result_text = tk.Text(detail_frame, wrap=tk.WORD, font=("Consolas", 10), 
                                   bg="#1e1e1e", fg="#BDBDBD", insertbackground="white")
        text_scroll = ttk.Scrollbar(detail_frame, command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.result_text.tag_configure("pass", foreground=self.COLOR_PASS)
        self.result_text.tag_configure("fail", foreground=self.COLOR_FAIL)
        self.result_text.tag_configure("unknown", foreground=self.COLOR_UNKNOWN)
        self.result_text.tag_configure("error", foreground="#FF9800")
        self.result_text.tag_configure("header", foreground="#29B6F6", font=("Consolas", 10, "bold"))
        self.result_text.tag_configure("info", foreground="#BDBDBD")

    def select_all_items(self):
        """모든 항목 선택"""
        for var in self.item_vars.values():
            var.set(True)

    def clear_all_items(self):
        """모든 항목 해제"""
        for var in self.item_vars.values():
            var.set(False)

    def load_files(self):
        """로컬 파일 로드"""
        files = filedialog.askopenfilenames(
            title="NETCONF 로그 파일 선택 (다중 선택 가능)",
            filetypes=(("Log files", "*.log *.txt *.xml"), ("All files", "*.*"))
        )
        if files:
            for f in files:
                if f not in self.loaded_files:
                    self.loaded_files.append(f)
                    self.file_listbox.insert(tk.END, os.path.basename(f))

    def clear_files(self):
        """파일 목록 비우기"""
        self.loaded_files.clear()
        self.file_listbox.delete(0, tk.END)

    def clear_analysis_results(self):
        """분석 결과창 초기화"""
        if not self.summary_tree.get_children() and not self.result_text.get("1.0", "end-1c"):
            return
            
        self.test_item_history.clear()
        self.test_item_last_status.clear()  # 👈 상태 추적 초기화
        if messagebox.askyesno("결과 초기화", "분석 결과창의 모든 내용을 지우시겠습니까?"):
            self.summary_tree.delete(*self.summary_tree.get_children())
            self.tree_item_details.clear()
            self.result_text.delete("1.0", tk.END)

    def load_remote_file_scp(self):
        """SCP를 통해 원격 파일 다운로드"""
        dialog = ScpDialog(self.root)
        self.root.wait_window(dialog)

        if dialog.result and not dialog.result.get('is_folder'):
            details = dialog.result
            self.scp_btn.config(state=tk.DISABLED, text="⏳ 다운로드 중...")
            threading.Thread(target=self._scp_download_thread, args=(details,), daemon=True).start()
        elif dialog.result and dialog.result.get('is_folder'):
            messagebox.showwarning("파일 선택", "파일 선택 모드에서는 폴더가 아닌 파일을 선택해 주세요.")

    def _scp_download_thread(self, details):
        """백그라운드 SCP 다운로드"""
        hostname = details['host']
        username = details['user']
        password = details['password']
        remote_path = details['remote_path']

        os.makedirs(self.REMOTE_LOGS_DIR, exist_ok=True)
        local_path = os.path.join(self.REMOTE_LOGS_DIR, os.path.basename(remote_path))

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname, port=22, username=username, password=password, timeout=self.SSH_TIMEOUT)

            with ssh.open_sftp() as sftp:
                sftp.get(remote_path, local_path)

            ssh.close()
            self.root.after(0, self._on_scp_download_success, local_path)

        except Exception as e:
            msg = f"다운로드 중 오류가 발생했습니다:\n{e}"
            self.root.after(0, self._on_scp_download_fail, "다운로드 오류", msg)

    def _on_scp_download_success(self, local_path):
        """SCP 다운로드 성공"""
        self.scp_btn.config(state=tk.NORMAL, text="... 리눅스 PC에서 불러오기 (SCP)")
        if local_path not in self.loaded_files:
            self.loaded_files.append(local_path)
            self.file_listbox.insert(tk.END, f"[REMOTE] {os.path.basename(local_path)}")
            self.file_listbox.selection_clear(0, tk.END)
            self.file_listbox.selection_set(tk.END)
            self.file_listbox.see(tk.END)
        messagebox.showinfo("성공", f"파일을 성공적으로 다운로드했습니다.\n로컬 경로: {os.path.abspath(local_path)}")

    def _on_scp_download_fail(self, title, message):
        """SCP 다운로드 실패"""
        self.scp_btn.config(state=tk.NORMAL, text="... 리눅스 PC에서 불러오기 (SCP)")
        messagebox.showerror(title, message)

    def identify_test_category(self, xml_text: str) -> str:
        """XML 텍스트에서 테스트 카테고리 식별"""
        xml_lower = xml_text.lower()
        detected_tests = sorted([test_name for keyword, test_name in self.keyword_to_test_map.items() 
                               if keyword in xml_lower])
        return ", ".join(detected_tests) if detected_tests else "알 수 없는 일반 RPC 시험"

    def _on_summary_select(self, event):
        """요약 테이블 항목 선택"""
        selected_iids = self.summary_tree.selection()
        if not selected_iids:
            return
        
        selected_iid = selected_iids[0]
        item_history_obj = self.tree_item_details.get(selected_iid)
        
        if item_history_obj:
            self._render_detailed_view(item_history_obj)
        else:
            self.result_text.delete("1.0", tk.END)
            self.result_text.insert(tk.END, "상세 정보가 없습니다.\n", 'info')

    def _render_detailed_view(self, item_history_obj):
        """상세 뷰 렌더링"""
        self.result_text.delete("1.0", tk.END)
        
        if isinstance(item_history_obj, dict):
            status = item_history_obj.get('latest_status', 'UNKNOWN')
            total_runs = item_history_obj.get('total_runs', 0)
            pass_count = item_history_obj.get('pass_count', 0)
            fail_count = item_history_obj.get('fail_count', 0)
            active_count = item_history_obj.get('active_count', 0)
            unknown_count = item_history_obj.get('unknown_count', 0)
            error_count = item_history_obj.get('error_count', 0)
            run_history = item_history_obj.get('run_history', [])
            
            # 헤더 출력
            self.result_text.insert(tk.END, "=" * 80 + "\n", 'header')
            self.result_text.insert(tk.END, "[ 분석 항목 상세 이력 ]\n", 'header')
            self.result_text.insert(tk.END, "=" * 80 + "\n\n", 'header')
            
            # 통계 출력
            status_tag = 'pass' if status == 'PASS' else 'fail' if status == 'FAIL' else 'unknown'
            self.result_text.insert(tk.END, f"최종 상태: {status}\n", status_tag)
            self.result_text.insert(tk.END, f"총 실행 횟수: {total_runs}\n", 'info')
            self.result_text.insert(tk.END, f"  - PASS: {pass_count}\n", 'pass')
            self.result_text.insert(tk.END, f"  - FAIL: {fail_count}\n", 'fail')
            self.result_text.insert(tk.END, f"  - ACTIVE: {active_count}\n", 'unknown')
            self.result_text.insert(tk.END, f"  - UNKNOWN: {unknown_count}\n", 'unknown')
            self.result_text.insert(tk.END, f"  - ERROR: {error_count}\n", 'error')
            self.result_text.insert(tk.END, "\n" + "=" * 80 + "\n", 'header')
            
            # 최근 실행 이력 출력 (최대 5개)
            if run_history:
                self.result_text.insert(tk.END, "\n[ 최근 실행 이력 (최대 5개) ]\n\n", 'header')
                for i, run in enumerate(reversed(run_history[-5:]), 1):
                    run_timestamp = run.get('run_timestamp', datetime.now())
                    run_status = run.get('status', 'UNKNOWN')
                    summary_msg = run.get('summary_msg', '')
                    file_id = run.get('file_identifier', 'Unknown')
                    detailed_parts = run.get('detailed_report_parts', [])
                    
                    self.result_text.insert(tk.END, f"\n--- [ 실행 #{len(run_history) - i + 1} ] ---\n", 'header')
                    self.result_text.insert(tk.END, f"시간: {run_timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n", 'info')
                    self.result_text.insert(tk.END, f"파일: {file_id}\n", 'info')
                    
                    run_status_tag = 'pass' if run_status == 'PASS' else 'fail' if run_status == 'FAIL' else 'unknown'
                    self.result_text.insert(tk.END, f"결과: {run_status} - {summary_msg}\n\n", run_status_tag)
                    
                    for part in detailed_parts:
                        text = part.get('text', '')
                        tag = part.get('tag', 'info')
                        self.result_text.insert(tk.END, text, tag)
            else:
                self.result_text.insert(tk.END, "\n아직 실행 이력이 없습니다.\n", 'unknown')
        else:
            self.result_text.insert(tk.END, "상세 정보를 표시할 수 없습니다.\n", 'error')

    def _update_summary_tree(self, summary_data_list):
        """요약 테이블 갱신 (존재하면 업데이트, 없으면 추가)"""
        existing_items = self.summary_tree.get_children()
        existing_map = {}
        for iid in existing_items:
            item_text = self.summary_tree.item(iid, "values")[0]
            existing_map[item_text] = iid
            
        for summary_data in summary_data_list:
            item_text = summary_data.get('item', 'N/A')
            result_text = summary_data.get('result', 'N/A')
            summary_text = summary_data.get('summary', '')
            item_history_obj = summary_data.get('details', {})
            
            tag = result_text.lower()
            if item_text in existing_map:
                iid = existing_map[item_text]
                self.summary_tree.item(iid, values=(item_text, result_text, summary_text), tags=(tag,))
                self.tree_item_details[iid] = item_history_obj
            else:
                iid = self.summary_tree.insert("", tk.END, values=(item_text, result_text, summary_text), tags=(tag,))
                self.tree_item_details[iid] = item_history_obj
                
        # 선택된 항목 동기화
        selected_iids = self.summary_tree.selection()
        if selected_iids:
            selected_iid = selected_iids[0]
            if selected_iid in self.tree_item_details:
                self._render_detailed_view(self.tree_item_details[selected_iid])

    def _update_ui_after_analysis(self, all_analysis_results):
        """분석 완료 후 UI 업데이트"""
        self._update_summary_tree(all_analysis_results)
        self._restore_buttons_after_analysis()

    def run_analysis(self):
        """분석 시작 (cat 기능)"""
        if not self.loaded_files:
            messagebox.showwarning("파일 없음", "먼저 로그 파일을 하나 이상 불러와 주세요.")
            return

        selected_items = [item for item, var in self.item_vars.items() if var.get()]
        if not selected_items:
            messagebox.showwarning("항목 선택", "분석할 Conformance Test 항목을 최소 1개 이상 선택해 주세요.")
            return
        
        files_to_analyze = self.loaded_files

        self.summary_tree.delete(*self.summary_tree.get_children())
        self.tree_item_details.clear()
        self.result_text.delete("1.0", tk.END)

        # 대기 상태 표시
        initial_data = []
        for file_path in files_to_analyze:
            for item in selected_items:
                initial_data.append({
                    'item': f"[{os.path.basename(file_path)}] {item}",
                    'result': 'WAITING',
                    'summary': '분석 대기 중...',
                    'details': {
                        "latest_status": "WAITING",
                        "total_runs": 0,
                        "pass_count": 0,
                        "fail_count": 0,
                        "active_count": 0,
                        "unknown_count": 0,
                        "error_count": 0,
                        "run_history": []
                    }
                })
        self._update_summary_tree(initial_data)

        self.tail_btn.config(state=tk.DISABLED)
        self.folder_monitor_btn.config(state=tk.DISABLED)
        self.remote_folder_monitor_btn.config(state=tk.DISABLED)
        self.run_btn.config(state=tk.DISABLED, text="⏳ 분석 진행 중...", bg="#999999")

        threading.Thread(target=self._analysis_thread, args=(files_to_analyze, selected_items), daemon=True).start()

    def toggle_realtime_analysis(self):
        """실시간 분석 토글"""
        if self.is_monitoring:
            self.stop_realtime_analysis()
        else:
            self.start_realtime_analysis()

    def start_realtime_analysis(self):
        """실시간 분석 시작"""
        selected_indices = self.file_listbox.curselection()
        if not selected_indices:
            messagebox.showwarning("파일 선택", "실시간 분석을 시작할 로그 파일을 목록에서 하나 선택해주세요.")
            return
        if len(selected_indices) > 1:
            messagebox.showwarning("파일 선택", "실시간 분석은 한 번에 하나의 파일만 가능합니다.")
            return

        selected_items = [item for item, var in self.item_vars.items() if var.get()]
        if not selected_items:
            messagebox.showwarning("항목 선택", "분석할 Conformance Test 항목을 최소 1개 이상 선택해 주세요.")
            return

        file_path = self.loaded_files[selected_indices[0]]
        
        self.is_monitoring = True
        self.result_text.delete("1.0", tk.END)
        self.summary_tree.delete(*self.summary_tree.get_children())
        self.tree_item_details.clear()

        self.test_item_history.clear()
        self.test_item_last_status.clear()  # 👈 상태 추적 초기화
        initial_data = []
        for item in selected_items:
            self.test_item_history[item] = {
                "latest_status": "WAITING", "total_runs": 0, "pass_count": 0, "fail_count": 0,
                "active_count": 0, "unknown_count": 0, "error_count": 0, "run_history": []
            }
            initial_data.append({
                'item': item, 
                'result': 'WAITING', 
                'summary': '실시간 모니터링 대기 중...', 
                'details': self.test_item_history[item]
            })
        self._update_summary_tree(initial_data)

        self.run_btn.config(state=tk.DISABLED)
        self.folder_monitor_btn.config(state=tk.DISABLED)
        self.remote_folder_monitor_btn.config(state=tk.DISABLED)
        self.tail_btn.config(text="■ 분석 중지", bg=self.COLOR_FAIL)

        self.monitoring_thread = threading.Thread(target=self._realtime_analysis_thread, args=(file_path, selected_items), daemon=True)
        self.monitoring_thread.start()

    def stop_realtime_analysis(self):
        """실시간 분석 중지"""
        self.is_monitoring = False
        self._restore_buttons_after_analysis()
        self.tail_btn.config(text="▷ 실시간 분석 (tail -f)", bg="#FF9800")

    def _realtime_analysis_thread(self, file_path, selected_items):
        """tail -f 유사 실시간 분석 스레드"""
        self.root.after(0, self.result_text.insert, tk.END, 
                       f"========== 실시간 분석 시작: {os.path.basename(file_path)} ==========\n")
        
        try:
            f = open(file_path, 'r', encoding='utf-8', errors='ignore')
            current_inode = os.stat(file_path).st_ino
            accumulated_content = ""
        except FileNotFoundError:
            self.root.after(0, self.result_text.insert, tk.END, "오류: 파일을 찾을 수 없습니다. 분석을 중지합니다.\n", "error")
            self.root.after(0, self.stop_realtime_analysis)
            return

        while self.is_monitoring:
            try:
                if not os.path.exists(file_path) or os.stat(file_path).st_ino != current_inode:
                    self.root.after(0, self.result_text.insert, tk.END, 
                                   f"\n[!] 로그 파일이 변경(Rotation)되었습니다. 새 파일을 읽습니다...\n", "info")
                    f.close()
                    f = open(file_path, 'r', encoding='utf-8', errors='ignore')
                    current_inode = os.stat(file_path).st_ino
                    accumulated_content = ""
                
                new_lines = f.read()
                if new_lines:
                    accumulated_content += new_lines
                    analysis_summaries = self._analyze_content(
                        accumulated_content, 
                        selected_items, 
                        os.path.basename(file_path)
                    )
                    if analysis_summaries:
                        self.root.after(0, self._update_summary_tree, analysis_summaries)
                time.sleep(self.MONITORING_INTERVAL)
            except Exception as e:
                self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 파일 읽기 오류: {e}\n", "error")
                time.sleep(2)
        
        f.close()
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 실시간 분석이 중지되었습니다. ==========\n")

    def toggle_folder_monitoring(self):
        """로컬 폴더 감시 토글"""
        if self.is_folder_monitoring:
            self.stop_folder_monitoring()
        else:
            self.start_folder_monitoring()

    def start_folder_monitoring(self):
        """로컬 폴더 감시 시작"""
        selected_items = [item for item, var in self.item_vars.items() if var.get()]
        if not selected_items:
            messagebox.showwarning("항목 선택", "분석할 Conformance Test 항목을 최소 1개 이상 선택해 주세요.")
            return

        folder_path = filedialog.askdirectory(title="감시할 폴더를 선택하세요")
        if not folder_path:
            return

        self.monitored_folder_path = folder_path
        self.processed_files_sizes = {}
        for f in os.listdir(self.monitored_folder_path):
            p = os.path.join(self.monitored_folder_path, f)
            if os.path.isfile(p):
                try:
                    self.processed_files_sizes[f] = os.path.getsize(p)
                except Exception:
                    pass

        self.is_folder_monitoring = True
        self.summary_tree.delete(*self.summary_tree.get_children())
        self.result_text.delete("1.0", tk.END)
        self.test_item_history.clear()
        self.test_item_last_status.clear()  # 👈 상태 추적 초기화
        self.tree_item_details.clear()
        
        self.run_btn.config(state=tk.DISABLED)
        self.tail_btn.config(state=tk.DISABLED)
        self.remote_folder_monitor_btn.config(state=tk.DISABLED)
        self.folder_monitor_btn.config(text="■ 폴더 감시 중지", bg=self.COLOR_FAIL)

        self.folder_monitoring_thread = threading.Thread(target=self._folder_monitoring_thread, args=(selected_items,), daemon=True)
        self.folder_monitoring_thread.start()

    def stop_folder_monitoring(self):
        """로컬 폴더 감시 중지"""
        self.is_folder_monitoring = False
        self._restore_buttons_after_analysis()
        self.folder_monitor_btn.config(text="📂 로컬 폴더 감시", bg="#673AB7")

    def _folder_monitoring_thread(self, selected_items):
        """백그라운드 로컬 폴더 감시 스레드"""
        self.root.after(0, self.result_text.insert, tk.END, 
                       f"========== 폴더 실시간 감시 시작: {self.monitored_folder_path} ==========\n")

        while self.is_folder_monitoring:
            try:
                current_files = [f for f in os.listdir(self.monitored_folder_path) 
                               if os.path.isfile(os.path.join(self.monitored_folder_path, f))]

                for filename in current_files:
                    file_path = os.path.join(self.monitored_folder_path, filename)
                    try:
                        current_size = os.path.getsize(file_path)
                    except OSError:
                        continue
                        
                    if filename not in self.processed_files_sizes or current_size > self.processed_files_sizes[filename]:
                        if filename not in self.processed_files_sizes:
                            self.root.after(0, self.result_text.insert, tk.END, f"\n\n▶ 신규 파일 감지: {filename}\n", "header")
                            
                        self.processed_files_sizes[filename] = current_size
                        
                        try:
                            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                            analysis_summaries = self._analyze_content(content, selected_items, filename) if content else []
                            if analysis_summaries:
                                self.root.after(0, self._update_summary_tree, analysis_summaries)
                        except Exception as e:
                            self.root.after(0, self.result_text.insert, tk.END, f"   파일 읽기/분석 실패 -> {str(e)}\n", "error")
                
                time.sleep(self.MONITORING_INTERVAL)
            except FileNotFoundError:
                self.root.after(0, self.result_text.insert, tk.END, "\n[!] 감시 폴더를 찾을 수 없습니다. 감시를 중지합니다.\n", "error")
                self.root.after(0, self.stop_folder_monitoring)
                break
            except Exception as e:
                self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 폴더 감시 중 오류 발생: {e}\n", "error")
                time.sleep(5)
        
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 폴더 감시가 중지되었습니다. ==========\n")

    def toggle_remote_folder_monitoring(self):
        """원격 폴더 감시 토글"""
        if self.is_remote_folder_monitoring:
            self.stop_remote_folder_monitoring()
        else:
            self.start_remote_folder_monitoring()

    def start_remote_folder_monitoring(self):
        """원격 폴더 감시 시작"""
        selected_items = [item for item, var in self.item_vars.items() if var.get()]
        if not selected_items:
            messagebox.showwarning("항목 선택", "분석할 Conformance Test 항목을 최소 1개 이상 선택해 주세요.")
            return

        dialog = ScpDialog(self.root)
        self.root.wait_window(dialog)

        if dialog.result and dialog.result.get('is_folder'):
            details = dialog.result
            self.remote_monitored_folder_path = details['remote_path']
            
            self.is_remote_folder_monitoring = True
            self.summary_tree.delete(*self.summary_tree.get_children())
            self.result_text.delete("1.0", tk.END)
            self.test_item_history.clear()
            self.test_item_last_status.clear()  # 👈 상태 추적 초기화
            self.tree_item_details.clear()
            
            self.run_btn.config(state=tk.DISABLED)
            self.tail_btn.config(state=tk.DISABLED)
            self.folder_monitor_btn.config(state=tk.DISABLED)
            self.remote_folder_monitor_btn.config(text="■ 원격 폴더 감시 중지", bg=self.COLOR_FAIL)

            self.remote_folder_monitoring_thread = threading.Thread(target=self._remote_folder_monitoring_thread, 
                                                                   args=(details, selected_items), daemon=True)
            self.remote_folder_monitoring_thread.start()
        elif dialog.result and not dialog.result.get('is_folder'):
            details = dialog.result
            self.remote_monitored_folder_path = details['remote_path']
            
            self.is_remote_folder_monitoring = True
            self.summary_tree.delete(*self.summary_tree.get_children())
            self.result_text.delete("1.0", tk.END)
            self.test_item_history.clear()
            self.test_item_last_status.clear()  # 👈 상태 추적 초기화
            self.tree_item_details.clear()
            
            self.run_btn.config(state=tk.DISABLED)
            self.tail_btn.config(state=tk.DISABLED)
            self.folder_monitor_btn.config(state=tk.DISABLED)
            self.remote_folder_monitor_btn.config(text="■ 원격 파일 감시 중지", bg=self.COLOR_FAIL)

            self.remote_folder_monitoring_thread = threading.Thread(target=self._remote_file_monitoring_thread, 
                                                                   args=(details, selected_items), daemon=True)
            self.remote_folder_monitoring_thread.start()

    def stop_remote_folder_monitoring(self):
        """원격 폴더 감시 중지"""
        self.is_remote_folder_monitoring = False
        self._restore_buttons_after_analysis()
        self.remote_folder_monitor_btn.config(text="🌐 원격 폴더 감시", bg="#3F51B5")
        if self.sftp_client:
            try: 
                self.sftp_client.close()
            except Exception:
                pass
        if self.ssh_client:
            try: 
                self.ssh_client.close()
            except Exception:
                pass
        self.sftp_client = None
        self.ssh_client = None

    def _remote_file_monitoring_thread(self, details, selected_items):
        """원격 파일 감시 스레드 (tail -f)"""
        self.root.after(0, self.result_text.insert, tk.END, 
                       f"========== 원격 파일 실시간 감시 시작: {details['remote_path']} ==========\n")
        
        last_size = 0
        
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_client.connect(details['host'], port=22, username=details['user'], 
                                   password=details['password'], timeout=self.SSH_TIMEOUT)
            self.sftp_client = self.ssh_client.open_sftp()
            
            try:
                last_size = self.sftp_client.stat(details['remote_path']).st_size
            except FileNotFoundError:
                self.root.after(0, self.result_text.insert, tk.END, 
                               f"\n[!] 파일을 찾을 수 없습니다. 감시를 시작할 수 없습니다.\n", "error")
                self.root.after(0, self.stop_remote_folder_monitoring)
                return

        except Exception as e:
            self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 원격 서버 접속 실패: {e}\n", "error")
            self.root.after(0, self.stop_remote_folder_monitoring)
            return

        accumulated_content = ""

        while self.is_remote_folder_monitoring:
            try:
                current_size = self.sftp_client.stat(details['remote_path']).st_size
                
                if current_size > last_size:
                    with self.sftp_client.open(details['remote_path'], 'rb') as f:
                        f.seek(last_size)
                        new_content_bytes = f.read(current_size - last_size)
                        new_content = new_content_bytes.decode('utf-8', errors='ignore')
                    
                    last_size = current_size
                    accumulated_content += new_content
                    
                    analysis_summaries = self._analyze_content(accumulated_content, selected_items, 
                                                              os.path.basename(details['remote_path'])) if accumulated_content else []
                    
                    if analysis_summaries:
                        self.root.after(0, self._update_summary_tree, analysis_summaries)

                elif current_size < last_size:
                    self.root.after(0, self.result_text.insert, tk.END, 
                                   f"\n[!] 원격 파일이 잘렸거나 교체되었습니다. 처음부터 다시 읽습니다.\n", "info")
                    last_size = 0
                    accumulated_content = ""
                    self.root.after(0, self.clear_analysis_results)

                time.sleep(self.REMOTE_MONITORING_INTERVAL)
            except Exception as e:
                if not self.is_remote_folder_monitoring: 
                    break
                self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 원격 파일 감시 중 오류 발생: {e}\n", "error")
                time.sleep(5)
        
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 원격 파일 감시가 중지되었습니다. ==========\n")

    def _remote_folder_monitoring_thread(self, details, selected_items):
        """원격 폴더 감시 스레드"""
        self.root.after(0, self.result_text.insert, tk.END, 
                       f"========== 원격 폴더 실시간 감시 시작: {details['remote_path']} ==========\n")
        
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_client.connect(details['host'], port=22, username=details['user'], 
                                   password=details['password'], timeout=self.SSH_TIMEOUT)
            self.sftp_client = self.ssh_client.open_sftp()
            
            initial_items = self.sftp_client.listdir_attr(details['remote_path'])
            file_sizes = {item.filename: item.st_size for item in initial_items 
                         if not stat.S_ISDIR(item.st_mode)}
            
        except Exception as e:
            self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 원격 서버 접속 실패: {e}\n", "error")
            self.root.after(0, self.stop_remote_folder_monitoring)
            return

        while self.is_remote_folder_monitoring:
            try:
                current_items = self.sftp_client.listdir_attr(details['remote_path'])
                for item in current_items:
                    if stat.S_ISDIR(item.st_mode): 
                        continue
                    
                    filename = item.filename
                    current_size = item.st_size
                    
                    if filename not in file_sizes or current_size > file_sizes[filename]:
                        if filename not in file_sizes:
                            self.root.after(0, self.result_text.insert, tk.END, 
                                           f"\n\n▶ 원격 신규 파일 감지: {filename}\n", "header")
                            
                        file_sizes[filename] = current_size
                        remote_file_path = posix_join(details['remote_path'], filename)
                        
                        try:
                            with self.sftp_client.open(remote_file_path, 'r') as f:
                                content = f.read().decode('utf-8', errors='ignore')
                            
                            analysis_summaries = self._analyze_content(content, selected_items, filename) if content else []
                            if analysis_summaries:
                                self.root.after(0, self._update_summary_tree, analysis_summaries)
                                
                        except Exception as e:
                            self.root.after(0, self.result_text.insert, tk.END, f"   파일 읽기/분석 실패 -> {str(e)}\n", "error")
                        
                time.sleep(self.REMOTE_MONITORING_INTERVAL)
            except Exception as e:
                if self.is_remote_folder_monitoring:
                    self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 원격 폴더 감시 중 오류 발생: {e}\n", "error")
                    time.sleep(5)
                else:
                    break
                    
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 원격 폴더 감시가 중지되었습니다. ==========\n")

    def _analysis_thread(self, files_to_analyze, selected_items):
        """실제 분석 스레드 (cat 기능)"""
        all_analysis_results = []

        for file_path in files_to_analyze:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                
                analysis_summaries = self._analyze_content(content, selected_items, os.path.basename(file_path))
                
                for summary in analysis_summaries:
                    summary['item'] = f"[{os.path.basename(file_path)}] {summary['item']}"
                all_analysis_results.extend(analysis_summaries)

            except Exception as e:
                all_analysis_results.append({
                    'item': f"[{os.path.basename(file_path)}] File Read Error",
                    'result': 'ERROR',
                    'summary': f"Failed to read or parse file.",
                    'details': {
                        "latest_status": "ERROR",
                        "total_runs": 1,
                        "pass_count": 0,
                        "fail_count": 0,
                        "active_count": 0,
                        "unknown_count": 0,
                        "error_count": 1,
                        "run_history": [{
                            'run_timestamp': datetime.now(),
                            'status': 'ERROR',
                            'summary_msg': str(e),
                            'detailed_report_parts': [
                                {'text': f"{str(e)}\n", 'tag': 'error'}
                            ],
                            'file_identifier': os.path.basename(file_path)
                        }]
                    }
                })

        self.root.after(0, self._update_ui_after_analysis, all_analysis_results)

    def _extract_xml_blocks(self, content: str) -> List[Tuple]:
        """로그에서 XML 블록 추출 (최적화됨)"""
        blocks = []
        pattern = re.compile(r'<(rpc-reply|rpc|hello|notification|install)[^>]*>', re.IGNORECASE)
        
        for match in pattern.finditer(content):
            tag_name = match.group(1)
            start_idx = match.start()
            end_tag = f'</{tag_name}>'
            end_idx = content.find(end_tag, start_idx)
            
            if end_idx == -1 and content[match.end()-2:match.end()] == '/>':
                xml_str = content[start_idx:match.end()]
            elif end_idx != -1:
                xml_str = content[start_idx:end_idx + len(end_tag)]
            else:
                continue
            
            # xmlns 속성 제거
            xml_str_clean = re.sub(r'\s*xmlns(?::[a-zA-Z0-9\-]*)?="[^"]*"', '', xml_str)
            
            try:
                root = ET.fromstring(xml_str_clean)
                blocks.append((root, xml_str))
            except ET.ParseError:
                pass
        
        return blocks

    def _analyze_content(self, content: str, selected_items: List[str], file_identifier: str = None) -> List[Dict]:
        """콘텐츠 분석 (최적화: 캐싱된 분석 함수 사용)"""
        if file_identifier is None:
            file_identifier = "General"
        
        all_results = []

        # 분석 함수 매핑 (조기 정의)
        test_runners = {
            "M-Plane: Startup & Call Home": self._check_startup_call_home,
            "M-Plane: NETCONF Capability (hello)": self._check_hello_exchange,
            "M-Plane: Supervision": self._check_supervision,
            "M-Plane: User Account": self._check_user_account,
            "Config Management: <get> / <get-config>": self._check_get_config,
            "Config Management: edit-config (rollback)": self._check_edit_config_rollback,
            "Config Management: State Change (admin/oper)": self._check_state_change,
            "Config Management: Subscription & Notification": self._check_subscription_notification,
            "C/U-Plane: Full Configuration": self._check_cu_plane_config,
            "Log Management: Troubleshooting Log": self._check_log_management,
            "Generic: RPC Error Validation": self._check_generic_rpc_errors,
            "Software Management: Down/Install/Activate": self._check_sw_management,
            "Fault Management": self._check_fault_management,
        }

        for item in selected_items:
            if item not in test_runners:
                continue
            
            try:
                result_dict = test_runners[item](content)
                
                status = result_dict.get('status', 'UNKNOWN')
                summary_msg = result_dict.get('summary_msg', '')
                detailed_report_parts = result_dict.get('detailed_report_parts', [])
                run_timestamp = result_dict.get('run_timestamp', datetime.now())
                
                # ===== 상태 변화 감지 (추가) =====
                # 이전 상태와 비교
                prev_status = self.test_item_last_status.get(item)
                should_increment_counter = (prev_status != status)
                
                # 이전 상태 업데이트
                self.test_item_last_status[item] = status
                # ===== 끝 =====
                
                # 이력 추적
                if item not in self.test_item_history:
                    self.test_item_history[item] = {
                        "latest_status": status,
                        "total_runs": 0,
                        "pass_count": 0,
                        "fail_count": 0,
                        "active_count": 0,
                        "unknown_count": 0,
                        "error_count": 0,
                        "run_history": []
                    }
                
                history = self.test_item_history[item]
                history["latest_status"] = status
                
                # ===== 상태가 변경될 때만 total_runs 증가 (수정) =====
                if should_increment_counter:
                    history["total_runs"] += 1
                # ===== 끝 =====
                
                # ===== 상태별 카운트 (수정) =====
                if should_increment_counter:
                    status_map = {
                        'PASS': 'pass_count',
                        'FAIL': 'fail_count',
                        'ACTIVE': 'active_count',
                        'UNKNOWN': 'unknown_count',
                        'ERROR': 'error_count'
                    }
                    if status in status_map:
                        history[status_map[status]] += 1
                # ===== 끝 =====
                
                # ===== 이력 저장 (수정) =====
                if should_increment_counter:
                    history["run_history"].append({
                        'run_timestamp': run_timestamp,
                        'status': status,
                        'summary_msg': summary_msg,
                        'detailed_report_parts': detailed_report_parts,
                        'file_identifier': file_identifier
                    })
                    if len(history["run_history"]) > 10:
                        history["run_history"].pop(0)
                # ===== 끝 =====
                
                all_results.append({
                    'item': item,
                    'result': status,
                    'summary': summary_msg,
                    'details': history
                })
                    
            except Exception as e:
                all_results.append({
                    'item': item,
                    'result': 'ERROR',
                    'summary': 'Analysis function failed.',
                    'details': {
                        "latest_status": "ERROR",
                        "total_runs": 1,
                        "pass_count": 0,
                        "fail_count": 0,
                        "active_count": 0,
                        "unknown_count": 0,
                        "error_count": 1,
                        "run_history": [{
                            'run_timestamp': datetime.now(),
                            'status': 'ERROR',
                            'summary_msg': str(e),
                            'detailed_report_parts': [
                                {'text': f"An error occurred: {str(e)}\n", 'tag': 'error'}
                            ],
                            'file_identifier': file_identifier
                        }]
                    }
                })
        
        if not all_results:
            all_results.append({
                'item': "General",
                'result': "UNKNOWN",
                'summary': "No relevant logs found for selected items.",
                'details': {
                    "latest_status": "UNKNOWN",
                    "total_runs": 0,
                    "pass_count": 0,
                    "fail_count": 0,
                    "active_count": 0,
                    "unknown_count": 0,
                    "error_count": 0,
                    "run_history": []
                }
            })
            
        return all_results

    # ===== 분석 함수들 =====
    
    def _check_startup_call_home(self, content: str) -> Dict:
        """Startup & Call Home 검증"""
        report = [{'text': "\n--- [ M-Plane: Startup & Call Home ] ---\n", 'tag': 'header'}]
        xml_blocks = self._extract_xml_blocks(content)
        
        has_callhome = any(x in content.lower() for x in ["call-home", "callhome"])
        hellos = [xml_str for root, xml_str in xml_blocks if root.tag.endswith('hello')]
        
        if has_callhome or hellos:
            report.append({'text': f"   Result: [ PASS ] - Found Startup & Call Home evidence.\n", 'tag': 'pass'})
            status = 'PASS'
            
            if hellos:
                report.append({'text': f"   -> 교환된 <hello> 메시지 ({len(hellos)}건):\n", 'tag': 'info'})
                for idx, h in enumerate(hellos[:2]):
                    report.append({'text': f"      [Hello #{idx+1}]\n{h.strip()}\n", 'tag': 'info'})
                if len(hellos) > 2:
                     report.append({'text': f"      ... (외 {len(hellos)-2}건 생략)\n", 'tag': 'info'})
        else:
            report.append({'text': "   -> call-home: Not found\n", 'tag': 'unknown'})
            report.append({'text': "   -> <hello>: Not found\n", 'tag': 'unknown'})
            report.append({'text': "   Result: [ UNKNOWN ] - No Startup & Call Home evidence found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': "Startup & Call Home analysis complete.",
            'detailed_report_parts': report,
            'run_timestamp': datetime.now()
        }

    def _check_user_account(self, content: str) -> Dict:
        """User Account 검증"""
        report = [{'text': "\n--- [ M-Plane: User Account ] ---\n", 'tag': 'header'}]
        
        if any(x in content.lower() for x in ["user", "password", "account"]):
            report.append({'text': f"   Result: [ PASS ] - Found User Account management evidence.\n", 'tag': 'pass'})
            status = 'PASS'
        else:
            report.append({'text': "   -> user / password / account: Not found\n", 'tag': 'unknown'})
            report.append({'text': "   Result: [ UNKNOWN ] - No User Account evidence found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': "User Account analysis complete.",
            'detailed_report_parts': report,
            'run_timestamp': datetime.now()
        }

    def _check_edit_config_rollback(self, content: str) -> Dict:
        """edit-config rollback 검증 (개선됨)"""
        report = [{'text': "\n--- [ Config Management: edit-config (rollback) ] ---\n", 'tag': 'header'}]
        xml_blocks = self._extract_xml_blocks(content)
        
        edits = []
        rollback_found = False
        edit_config_success = False
        edit_config_error = False
        
        for root, xml_str in xml_blocks:
            xml_lower = xml_str.lower()
            
            if any(elem.tag.endswith('edit-config') for elem in root.iter()):
                edits.append((root, xml_str))
                
                # rollback-on-error 확인
                for elem in root.iter():
                    if elem.tag.endswith('error-option') and 'rollback' in (elem.text or '').lower():
                        rollback_found = True
                        break
                
                if not rollback_found and 'rollback-on-error' in xml_lower:
                    rollback_found = True
                
                if not rollback_found and '--error rollback' in xml_lower:
                    rollback_found = True
            
            # edit-config 응답 상태 확인
            if root.tag.endswith('rpc-reply'):
                has_error = any('rpc-error' in elem.tag for elem in root.iter())
                
                if not has_error:
                    edit_config_success = True
                elif has_error:
                    edit_config_error = True
        
        # 판정 로직
        status = 'UNKNOWN'
        summary_msg = ""
        
        if edits:
            report.append({'text': f"   Found <edit-config> RPC(s): {len(edits)}\n", 'tag': 'info'})
            
            if edit_config_success:
                report.append({'text': f"   Result: [ PASS ] - <edit-config> executed successfully.\n", 'tag': 'pass'})
                status = 'PASS'
                summary_msg = "edit-config executed successfully"
                
                if rollback_found:
                    report.append({'text': f"   -> Additional: 'rollback-on-error' option is configured.\n", 'tag': 'info'})
                else:
                    report.append({'text': f"   -> Note: 'rollback-on-error' option not configured (optional).\n", 'tag': 'info'})
            
            elif edit_config_error:
                if rollback_found:
                    report.append({'text': f"   Result: [ PASS ] - Rollback-on-error option applied.\n", 'tag': 'pass'})
                    status = 'PASS'
                    summary_msg = "Rollback mechanism verified"
                else:
                    report.append({'text': f"   Result: [ FAIL ] - No rollback-on-error option found.\n", 'tag': 'fail'})
                    status = 'FAIL'
                    summary_msg = "edit-config failed without rollback protection"
            else:
                if rollback_found:
                    report.append({'text': f"   Result: [ PASS ] - 'rollback-on-error' option configured.\n", 'tag': 'pass'})
                    status = 'PASS'
                    summary_msg = "rollback-on-error option configured"
                else:
                    report.append({'text': f"   Result: [ PASS ] - <edit-config> RPC found.\n", 'tag': 'pass'})
                    status = 'PASS'
                    summary_msg = "edit-config executed without errors"
            
            # 상세 정보
            for idx, (root, xml_str) in enumerate(edits):
                report.append({'text': f"\n      [Edit-Config RPC #{idx+1}]\n", 'tag': 'header'})
                report.append({'text': f"{xml_str.strip()}\n", 'tag': 'info'})
        else:
            report.append({'text': "   Result: [ UNKNOWN ] - No <edit-config> RPC found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
            summary_msg = "No edit-config found"
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report,
            'run_timestamp': datetime.now()
        }

    def _check_state_change(self, content: str) -> Dict:
        """State Change 검증"""
        report_parts = [{'text': "\n--- [ Config Management: State Change ] ---\n", 'tag': 'header'}]
        status = 'UNKNOWN'
        summary_msg = "No State Change evidence found."
        
        admin_states = re.findall(r'<admin-state[^>]*>([^<]+)</admin-state>', content, re.IGNORECASE)
        avail_states = re.findall(r'<availability-state[^>]*>([^<]+)</availability-state>', content, re.IGNORECASE)
        usage_states = re.findall(r'<usage-state[^>]*>([^<]+)</usage-state>', content, re.IGNORECASE)
        
        found_any = False
        
        if admin_states:
            found_any = True
            unique_admin = set(s.strip().lower() for s in admin_states)
            report_parts.append({'text': f"   -> [ PASS ] Found admin-state: {', '.join(unique_admin)}\n", 'tag': 'pass'})
        
        if avail_states:
            found_any = True
            unique_avail = set(s.strip().upper() for s in avail_states)
            report_parts.append({'text': f"   -> [ PASS ] Found availability-state: {', '.join(unique_avail)}\n", 'tag': 'pass'})

        if usage_states:
            found_any = True
            unique_usage = set(s.strip().lower() for s in usage_states)
            report_parts.append({'text': f"   -> [ PASS ] Found usage-state: {', '.join(unique_usage)}\n", 'tag': 'pass'})
                    
        if not found_any and any(x in content.lower() for x in ["admin-state", "oper-state"]):
            report_parts.append({'text': f"   Result: [ PASS ] - Found State Change evidence.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = "State Change evidence found."
        elif found_any:
            report_parts.append({'text': f"   Result: [ PASS ] - State Change evidence verified.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = "State Change evidence verified."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No State Change evidence found.\n", 'tag': 'unknown'})
            
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_subscription_notification(self, content: str) -> Dict:
        """Subscription & Notification 검증"""
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        subs = [xml_str for root, xml_str in xml_blocks if any(elem.tag.endswith('create-subscription') for elem in root.iter())]
        notifs = [xml_str for root, xml_str in xml_blocks if root.tag.endswith('notification')]
        status = 'UNKNOWN'
        summary_msg = "No Subscription or Notification evidence found."

        report_parts.append({'text': "\n--- [ Config Management: Subscription & Notification ] ---\n", 'tag': 'header'})
        
        if notifs:
            last_notif = notifs[-1]
            timestamp_match = re.search(r'<eventTime>([^<]+)</eventTime>', last_notif)
            timestamp = timestamp_match.group(1) if timestamp_match else "N/A"
            report_parts.append({'text': f"   Result: [ ACTIVE ] - Receiving notifications. Last: {timestamp}\n", 'tag': 'pass'})
            status = 'ACTIVE'
            summary_msg = f"Receiving notifications. Last: {timestamp}"
            
        elif subs:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(subs)} subscription RPC(s).\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(subs)} subscription RPC(s)."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No subscriptions or notifications found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_hello_exchange(self, content: str) -> Dict:
        """Hello Exchange 검증"""
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        hellos = [xml_str for root, xml_str in xml_blocks if root.tag.endswith('hello')]
        status = 'UNKNOWN'
        summary_msg = "No <hello> messages found."

        report_parts.append({'text': "\n--- [ M-Plane: NETCONF Capability (hello) ] ---\n", 'tag': 'header'})
        
        if hellos:
            report_parts.append({'text': f"   Result: [ PASS ] - <hello> exchange detected ({len(hellos)} messages).\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"<hello> exchange detected ({len(hellos)} messages)."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No <hello> message found.\n", 'tag': 'unknown'})
            
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_supervision(self, content: str) -> Dict:
        """Supervision 검증"""
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        supervisions = [xml_str for root, xml_str in xml_blocks if any('supervision' in elem.tag for elem in root.iter())]
        status = 'UNKNOWN'
        summary_msg = "No supervision messages found."

        report_parts.append({'text': "\n--- [ M-Plane: Supervision ] ---\n", 'tag': 'header'})
        
        if supervisions:
            last_msg = supervisions[-1]
            timestamp_match = re.search(r'<eventTime>([^<]+)</eventTime>', last_msg)
            timestamp = timestamp_match.group(1) if timestamp_match else "N/A"
            
            report_parts.append({'text': f"   Result: [ PASS ] - Supervision is active. Last: {timestamp}\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Supervision is active. Last: {timestamp}"
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No supervision messages found.\n", 'tag': 'unknown'})
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_get_config(self, content: str) -> Dict:
        """Get/Get-Config 검증"""
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        gets = [(root, xml_str) for root, xml_str in xml_blocks 
               if any(elem.tag.endswith(('get', 'get-config')) for elem in root.iter())]
        status = 'UNKNOWN'
        summary_msg = "No <get> or <get-config> found."

        report_parts.append({'text': "\n--- [ Config Management: <get> / <get-config> ] ---\n", 'tag': 'header'})
        
        if gets:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(gets)} <get> / <get-config> RPC(s).\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(gets)} RPC(s)."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No <get> or <get-config> found.\n", 'tag': 'unknown'})
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_cu_plane_config(self, content: str) -> Dict:
        """C/U-Plane Configuration 검증"""
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        cu_configs = [xml_str for root, xml_str in xml_blocks 
                     if any('uplane-conf' in elem.tag for elem in root.iter())]
        status = 'UNKNOWN'
        summary_msg = "No C/U-Plane configuration found."

        report_parts.append({'text': "\n--- [ C/U-Plane: Full Configuration ] ---\n", 'tag': 'header'})
        
        if cu_configs:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(cu_configs)} configuration messages.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(cu_configs)} configuration messages."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No C/U-Plane configuration found.\n", 'tag': 'unknown'})
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_log_management(self, content: str) -> Dict:
        """Log Management 검증"""
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        logs = [xml_str for root, xml_str in xml_blocks 
               if any('troubleshooting' in elem.tag for elem in root.iter())]
        status = 'UNKNOWN'
        summary_msg = "No Troubleshooting Log messages found."

        report_parts.append({'text': "\n--- [ Log Management: Troubleshooting Log ] ---\n", 'tag': 'header'})
        
        if logs:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(logs)} log messages.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(logs)} log messages."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No troubleshooting logs found.\n", 'tag': 'unknown'})
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_generic_rpc_errors(self, content: str) -> Dict:
        """RPC Error 검증"""
        report_parts = []
        status = 'UNKNOWN'
        summary_msg = "No <rpc-error> instances found."
        
        xml_blocks = self._extract_xml_blocks(content)
        error_replies = [(root, xml_str) for root, xml_str in xml_blocks 
                        if any('rpc-error' in elem.tag for elem in root.iter())]
                
        report_parts.append({'text': "\n--- [ Generic: RPC Error Validation ] ---\n", 'tag': 'header'})
        
        if error_replies:
            report_parts.append({'text': f"   Result: [ FAIL ] - Found {len(error_replies)} <rpc-error>(s).\n", 'tag': 'fail'})
            status = 'FAIL'
            summary_msg = f"Found {len(error_replies)} <rpc-error>(s)."
        else:
            report_parts.append({'text': "   Result: [ PASS ] - No <rpc-error> instances found.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = "No <rpc-error> instances found."
            
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_sw_management(self, content: str) -> Dict:
        """Software Management 검증"""
        report_parts = []
        final_status = 'UNKNOWN'
        summary_msg = "No Software Management RPCs found."
        found_activity = False

        xml_blocks = self._extract_xml_blocks(content)

        download_rpcs = [xml_str for root, xml_str in xml_blocks 
                        if any('software-download' in elem.tag for elem in root.iter())]
        install_rpcs = [xml_str for root, xml_str in xml_blocks 
                       if any('software-install' in elem.tag for elem in root.iter())]
        activate_rpcs = [xml_str for root, xml_str in xml_blocks 
                        if any('software-activate' in elem.tag for elem in root.iter())]
        
        report_parts.append({'text': "\n--- [ Software Management: Down/Install/Activate ] ---\n", 'tag': 'header'})
        
        if download_rpcs or install_rpcs or activate_rpcs:
            found_activity = True
            report_parts.append({'text': f"   Download RPCs: {len(download_rpcs)}\n", 'tag': 'info'})
            report_parts.append({'text': f"   Install RPCs: {len(install_rpcs)}\n", 'tag': 'info'})
            report_parts.append({'text': f"   Activate RPCs: {len(activate_rpcs)}\n", 'tag': 'info'})
            report_parts.append({'text': f"   Result: [ PASS ] - Software Management activity detected.\n", 'tag': 'pass'})
            final_status = 'PASS'
            summary_msg = "Software Management activity detected."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No Software Management RPCs found.\n", 'tag': 'unknown'})

        return {
            'status': final_status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_fault_management(self, content: str) -> Dict:
        """Fault Management 검증"""
        report_parts = []
        status = 'UNKNOWN'
        summary_msg = "No alarm notifications found in log."
        
        xml_blocks = self._extract_xml_blocks(content)
        alarm_notifs = [xml_str for root, xml_str in xml_blocks 
                       if root.tag.endswith('notification') and 
                          any('alarm' in elem.tag for elem in root.iter())]

        report_parts.append({'text': "\n--- [ Fault Management: Alarm Validation ] ---\n", 'tag': 'header'})
        
        if alarm_notifs:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(alarm_notifs)} alarm(s).\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(alarm_notifs)} alarm(s)."
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No alarm notifications found.\n", 'tag': 'unknown'})
            
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _restore_buttons_after_analysis(self):
        """분석 완료 후 버튼 복구"""
        self.run_btn.config(state=tk.NORMAL, text="▶ 전체 로그 분석 (cat)", bg=self.COLOR_PASS)
        self.tail_btn.config(state=tk.NORMAL)
        if hasattr(self, 'folder_monitor_btn'):
            self.folder_monitor_btn.config(state=tk.NORMAL)
        if hasattr(self, 'remote_folder_monitor_btn'):
            self.remote_folder_monitor_btn.config(state=tk.NORMAL)

    def export_report(self):
        """분석 결과를 파일로 저장"""
        if not self.test_item_history:
            messagebox.showwarning("경고", "저장할 분석 결과가 없습니다.")
            return
        
        if self.is_monitoring:
            if not messagebox.askyesno("확인", "실시간 분석이 진행 중입니다.\n지금까지의 내용만 저장하시겠습니까?"):
                return

        os.makedirs(self.REPORTS_DIR, exist_ok=True)
        
        report_filename = f"MPlane_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        report_path = os.path.join(self.REPORTS_DIR, report_filename)

        report_header = "=" * 80 + "\n"
        report_header += "O-RAN M-Plane Conformance Test Report\n"
        report_header += "=" * 80 + "\n"
        report_header += f"Analysis Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        report_header += "=" * 80 + "\n\n"

        summary_section = "[ Test Summary ]\n"
        summary_section += "-" * 80 + "\n"

        for item_name, history in self.test_item_history.items():
            summary_section += f"{item_name:<50} | {history['latest_status']:<10}\n"

        summary_section += "-" * 80 + "\n\n"

        try:
            with open(report_path, 'w', encoding='utf-8') as rf:
                rf.write(report_header + summary_section)
            
            messagebox.showinfo("저장 완료", f"리포트가 저장되었습니다.\n경로: {os.path.abspath(report_path)}")
                
        except Exception as e:
            messagebox.showerror("저장 실패", f"오류: {e}")


class ScpDialog(tk.Toplevel):
    """SCP 원격 파일/폴더 탐색 대화상자"""
    
    def __init__(self, parent):
        super().__init__(parent)
        self.title("SCP 원격 파일 탐색기")
        self.geometry("550x650")
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.result = None
        self.ssh = None
        self.sftp = None
        
        # ===== 버그 수정: conn_frame을 먼저 생성 =====
        self.conn_frame = tk.Frame(self)
        self.conn_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # 연결 정보 입력
        conn_fields = {
            "host": "서버 주소 (IP)",
            "user": "사용자 이름",
            "password": "비밀번호"
        }
        self.entries = {}

        self.settings_file = "netconf_settings.json"

        tk.Label(self.conn_frame, text="원격 서버 접속 정보", font=("Arial", 12, "bold")).pack(anchor="w", pady=(0, 15))
        
        for key, label_text in conn_fields.items():
            row = tk.Frame(self.conn_frame)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=label_text, width=15, anchor="w").pack(side="left")
            entry = tk.Entry(row, show="*" if key == "password" else "", width=35)
            entry.pack(side="left", fill="x", expand=True)
            entry.bind("<Return>", lambda event: self._connect_to_server())
            self.entries[key] = entry

        self._load_settings()

        self.conn_status_label = tk.Label(self.conn_frame, text="", fg="blue")
        self.conn_status_label.pack(pady=10)

        btn_frame = tk.Frame(self.conn_frame, pady=20)
        btn_frame.pack(fill="x")
        self.connect_btn = tk.Button(btn_frame, text="서버 접속", command=self._connect_to_server, 
                                    bg="#009688", fg="white", width=12, height=2)
        self.connect_btn.pack(side="left", expand=True, padx=5)
        tk.Button(btn_frame, text="취소", command=self._on_close, width=12, height=2).pack(side="left", expand=True, padx=5)
        
        # ===== 파일 탐색기 프레임 (초기에는 숨김) =====
        self.browser_frame = tk.Frame(self, padx=10, pady=10)

        path_frame = tk.Frame(self.browser_frame)
        path_frame.pack(fill='x', pady=5)
        self.current_path_label = tk.Label(path_frame, text="Current Path: /", anchor='w', bg='#e0e0e0', relief='sunken')
        self.current_path_label.pack(side='left', fill='x', expand=True)
        tk.Button(path_frame, text="↑", command=self._navigate_up, font=("", 10, "bold")).pack(side='left', padx=5)

        list_frame = tk.Frame(self.browser_frame)
        list_frame.pack(fill='both', expand=True)
        self.file_listbox = tk.Listbox(list_frame, font=("Consolas", 10))
        self.file_listbox.bind("<Double-1>", self._on_listbox_dclick)
        sb = ttk.Scrollbar(list_frame, command=self.file_listbox.yview)
        self.file_listbox.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.file_listbox.pack(side='left', fill='both', expand=True)

        browser_btn_frame = tk.Frame(self.browser_frame, pady=10)
        browser_btn_frame.pack(fill='x')
        tk.Button(browser_btn_frame, text="파일 감시 선택", command=self._on_select_file, 
                 bg="#4CAF50", fg="white", height=2).pack(side='left', expand=True, padx=5)
        tk.Button(browser_btn_frame, text="현재 폴더 선택", command=self._on_select_folder, 
                 bg="#2196F3", fg="white", height=2).pack(side='left', expand=True, padx=5)
        tk.Button(browser_btn_frame, text="취소", command=self._on_close, height=2).pack(side='left', expand=True, padx=5)

    def _load_settings(self):
        """설정 로드"""
        try:
            if os.path.exists("netconf_settings.json"):
                with open("netconf_settings.json", 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    if host := settings.get("last_remote_monitor_host"):
                        self.entries["host"].insert(0, host)
                    if user := settings.get("last_remote_monitor_user"):
                        self.entries["user"].insert(0, user)
                    if pwd := settings.get("last_remote_monitor_password"):
                        try:
                            self.entries["password"].insert(0, base64.b64decode(pwd).decode('utf-8'))
                        except:
                            pass
        except Exception:
            pass

    def _save_settings(self):
        """설정 저장"""
        try:
            settings = {}
            settings["last_remote_monitor_host"] = self.entries["host"].get()
            settings["last_remote_monitor_user"] = self.entries["user"].get()
            settings["last_remote_monitor_password"] = base64.b64encode(
                self.entries["password"].get().encode('utf-8')).decode('utf-8')
            
            with open("netconf_settings.json", 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
        except Exception:
            pass

    def _connect_to_server(self):
        """서버 접속"""
        if not all(entry.get() for entry in self.entries.values()):
            messagebox.showwarning("입력 오류", "모든 접속 정보를 입력해야 합니다.", parent=self)
            return
        self._save_settings()
        self.connect_btn.config(state=tk.DISABLED)
        self.conn_status_label.config(text="서버에 접속 중입니다...")
        self.update_idletasks()
        threading.Thread(target=self._ssh_connect_thread, daemon=True).start()

    def _ssh_connect_thread(self):
        """SSH 접속 스레드"""
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(
                hostname=self.entries['host'].get(),
                port=22,
                username=self.entries['user'].get(),                
                password=self.entries['password'].get(),
                timeout=10
            )
            self.sftp = self.ssh.open_sftp()
            self.after(0, self._show_browser_ui)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("접속 실패", str(e), parent=self))
            self.after(0, lambda: self.connect_btn.config(state=tk.NORMAL))

    def _show_browser_ui(self):
        """파일 탐색기 UI 표시"""
        self.conn_frame.pack_forget()
        self.title(f"SCP 원격 파일 탐색기 - {self.entries['host'].get()}")
        self.browser_frame.pack(fill="both", expand=True)
        
        home_dir = self.sftp.normalize('.')
        self._list_remote_path(home_dir)

    def _list_remote_path(self, path: str):
        """원격 경로 목록 조회"""
        try:
            self.file_listbox.delete(0, tk.END)
            self.current_path_label.config(text=f"Path: {path}")
            
            items = self.sftp.listdir_attr(path)
            dirs = sorted([item for item in items if stat.S_ISDIR(item.st_mode)], 
                         key=lambda i: i.filename.lower())
            files = sorted([item for item in items if not stat.S_ISDIR(item.st_mode)], 
                          key=lambda i: i.filename.lower())

            self.file_listbox.insert(tk.END, "[..] (상위 폴더로)")
            self.file_listbox.itemconfig(tk.END, {'fg': 'blue'})

            for d in dirs:
                self.file_listbox.insert(tk.END, f"[DIR] {d.filename}")
                self.file_listbox.itemconfig(tk.END, {'fg': 'navy'})
            for f in files:
                self.file_listbox.insert(tk.END, f.filename)

        except Exception as e:
            messagebox.showerror("경로 오류", f"경로를 읽을 수 없습니다: {e}", parent=self)

    def _on_listbox_dclick(self, event):
        """리스트박스 더블클릭"""
        if not self.file_listbox.curselection(): 
            return
        selected_item = self.file_listbox.get(self.file_listbox.curselection())
        current_path = self.current_path_label.cget("text").split("Path: ")[1]

        if selected_item.startswith("[DIR]"):
            dir_name = selected_item.split("[DIR] ")[1]
            new_path = posix_join(current_path, dir_name)
            self._list_remote_path(new_path)
        elif selected_item.startswith("[..]"):
            self._navigate_up()

    def _navigate_up(self):
        """상위 폴더로 이동"""
        current_path = self.current_path_label.cget("text").split("Path: ")[1]
        parent_path = posix_dirname(current_path)
        if not parent_path: 
            parent_path = "/"
        self._list_remote_path(parent_path)

    def _on_select_file(self):
        """파일 선택"""
        if not self.file_listbox.curselection():
            messagebox.showwarning("선택 오류", "감시할 파일을 선택해주세요.", parent=self)
            return
        
        selected_item = self.file_listbox.get(self.file_listbox.curselection())
        if selected_item.startswith("["):
            messagebox.showwarning("선택 오류", "파일만 감시할 수 있습니다.", parent=self)
            return

        current_path = self.current_path_label.cget("text").split("Path: ")[1]
        self.result = {
            'host': self.entries['host'].get(),
            'user': self.entries['user'].get(),
            'password': self.entries['password'].get(),
            'remote_path': posix_join(current_path, selected_item),
            'is_folder': False
        }
        self._on_close()

    def _on_select_folder(self):
        """폴더 선택"""
        current_path = self.current_path_label.cget("text").split("Path: ")[1]
        self.result = {
            'host': self.entries['host'].get(),
            'user': self.entries['user'].get(),
            'password': self.entries['password'].get(),
            'remote_path': current_path,
            'is_folder': True
        }
        self._on_close()

    def _on_close(self):
        """창 종료"""
        if self.sftp: 
            self.sftp.close()
        if self.ssh: 
            self.ssh.close()
        self.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MPlaneAnalyzerApp(root)
    root.mainloop()