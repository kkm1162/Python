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

class MPlaneAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("O-RAN M-Plane Conformance Analyzer")
        self.root.geometry("1100x850")
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

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

        self.build_ui()

    def on_closing(self):
        """앱 종료 시 자원 정리"""
        self.is_monitoring = False
        self.is_folder_monitoring = False
        self.is_remote_folder_monitoring = False
        
        if self.sftp_client:
            try: 
                self.sftp_client.close()
            except: 
                pass
        if self.ssh_client:
            try: 
                self.ssh_client.close()
            except: 
                pass
            
        self.root.destroy()

    def build_ui(self):
        """메인 UI 구성"""
        left_frame = tk.Frame(self.root, width=350, bg="#f5f5f5", relief="sunken", bd=1)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)
        left_frame.pack_propagate(False)

        right_frame = tk.Frame(self.root)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # ==========================================
        # 좌측 패널: 1. Conformance Test 항목 선택
        # ==========================================
        tk.Label(left_frame, text="✅ Conformance Test 항목", font=("Arial", 11, "bold"), bg="#f5f5f5").pack(anchor=tk.W, pady=(10, 5), padx=10)

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

        # ==========================================
        # 좌측 패널: 2. 로그 파일 로드
        # ==========================================
        tk.Label(left_frame, text="📂 로그 파일 목록", font=("Arial", 11, "bold"), bg="#f5f5f5").pack(anchor=tk.W, pady=(20, 5), padx=10)

        tk.Button(left_frame, text="+ 로컬 로그 파일 불러오기", command=self.load_files, bg="#2196F3", fg="white", font=("Arial", 9, "bold")).pack(fill=tk.X, padx=10, pady=2)
        self.scp_btn = tk.Button(left_frame, text="... 리눅스 PC에서 불러오기 (SCP)", command=self.load_remote_file_scp, bg="#009688", fg="white")
        self.scp_btn.pack(fill=tk.X, padx=10, pady=2)
        tk.Button(left_frame, text="목록 비우기", command=self.clear_files, bg="#f44336", fg="white").pack(fill=tk.X, padx=10, pady=(2,5))

        list_frame = tk.Frame(left_frame)

        self.file_listbox = tk.Listbox(list_frame, font=("Arial", 9), selectmode=tk.EXTENDED)
        list_scroll = ttk.Scrollbar(list_frame, command=self.file_listbox.yview)
        self.file_listbox.configure(yscrollcommand=list_scroll.set)

        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ==========================================
        # 좌측 패널: 3. 분석 실행 버튼 (하단 고정)
        # ==========================================
        bottom_btn_frame = tk.Frame(left_frame, bg="#f5f5f5")
        bottom_btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 10))

        folder_btn_frame = tk.Frame(bottom_btn_frame, bg="#f5f5f5")
        folder_btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))
        
        self.folder_monitor_btn = tk.Button(folder_btn_frame, text="📂 로컬 폴더 감시", command=self.toggle_folder_monitoring, bg="#673AB7", fg="white", font=("Arial", 9, "bold"), height=2)
        self.folder_monitor_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self.remote_folder_monitor_btn = tk.Button(folder_btn_frame, text="🌐 원격 폴더 감시", command=self.toggle_remote_folder_monitoring, bg="#3F51B5", fg="white", font=("Arial", 9, "bold"), height=2)
        self.remote_folder_monitor_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        analysis_btn_frame = tk.Frame(bottom_btn_frame, bg="#f5f5f5")
        analysis_btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(10, 5))

        self.run_btn = tk.Button(analysis_btn_frame, text="▶ 전체 로그 분석 (cat)", command=self.run_analysis, bg="#4CAF50", fg="white", font=("Arial", 10, "bold"), height=2)
        self.run_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self.tail_btn = tk.Button(analysis_btn_frame, text="▷ 실시간 분석 (tail -f)", command=self.toggle_realtime_analysis, bg="#FF9800", fg="white", font=("Arial", 10, "bold"), height=2)
        self.tail_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ==========================================
        # 우측 패널: 분석 결과 및 리포트 저장
        # ==========================================
        header_frame = tk.Frame(right_frame)
        header_frame.pack(fill=tk.X, pady=5)
        tk.Label(header_frame, text="📊 분석 결과 창", font=("Arial", 11, "bold")).pack(side=tk.LEFT)

        right_header_buttons = tk.Frame(header_frame)
        right_header_buttons.pack(side=tk.RIGHT)

        tk.Button(right_header_buttons, text="🗑️ 결과 초기화", command=self.clear_analysis_results, bg="#757575", fg="white", font=("Arial", 10, "bold"), padx=10).pack(side=tk.LEFT, padx=(0, 5))
        tk.Button(right_header_buttons, text="💾 리포트 파일로 저장", command=self.export_report, bg="#9C27B0", fg="white", font=("Arial", 10, "bold"), padx=10).pack(side=tk.LEFT)

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
        self.summary_tree.tag_configure("pass", foreground="#7CFC00")
        self.summary_tree.tag_configure("fail", foreground="#FF4500")
        self.summary_tree.tag_configure("unknown", foreground="#FFA000")
        self.summary_tree.tag_configure("active", foreground="#00BCD4")
        self.summary_tree.bind("<<TreeviewSelect>>", self._on_summary_select)

        # 하단: 상세 로그 Text
        detail_frame = ttk.Frame(paned_window, height=300)
        paned_window.add(detail_frame, weight=1)

        self.result_text = tk.Text(detail_frame, wrap=tk.WORD, font=("Consolas", 10), bg="#1e1e1e", fg="#BDBDBD", insertbackground="white")
        text_scroll = ttk.Scrollbar(detail_frame, command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.result_text.tag_configure("pass", foreground="#4CAF50")
        self.result_text.tag_configure("fail", foreground="#F44336")
        self.result_text.tag_configure("unknown", foreground="#FFC107")
        self.result_text.tag_configure("error", foreground="#FF9800")
        self.result_text.tag_configure("header", foreground="#29B6F6", font=("Consolas", 10, "bold"))
        self.result_text.tag_configure("info", foreground="#BDBDBD")

    def select_all_items(self):
        for var in self.item_vars.values():
            var.set(True)

    def clear_all_items(self):
        for var in self.item_vars.values():
            var.set(False)

    def load_files(self):
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
        self.loaded_files.clear()
        self.file_listbox.delete(0, tk.END)

    def clear_analysis_results(self):
        """분석 결과창의 요약 테이블과 상세 로그를 모두 초기화합니다."""
        if not self.summary_tree.get_children() and not self.result_text.get("1.0", "end-1c"):
            return
            
        self.test_item_history.clear()
        if messagebox.askyesno("결과 초기화", "분석 결과창의 모든 내용을 지우시겠습니까?"):
            self.summary_tree.delete(*self.summary_tree.get_children())
            self.tree_item_details.clear()
            self.result_text.delete("1.0", tk.END)

    def load_remote_file_scp(self):
        """SCP를 통해 원격 파일을 다운로드하는 대화상자를 엽니다."""
        dialog = ScpDialog(self.root)
        self.root.wait_window(dialog)

        if dialog.result and not dialog.result.get('is_folder'):
            details = dialog.result
            self.scp_btn.config(state=tk.DISABLED, text="⏳ 다운로드 중...")
            threading.Thread(target=self._scp_download_thread, args=(details,), daemon=True).start()
        elif dialog.result and dialog.result.get('is_folder'):
            messagebox.showwarning("파일 선택", "파일 선택 모드에서는 폴더가 아닌 파일을 선택해 주세요.")

    def _scp_download_thread(self, details):
        """백그라운드에서 SCP 파일 다운로드를 처리하는 스레드."""
        hostname = details['host']
        username = details['user']
        password = details['password']
        remote_path = details['remote_path']

        local_dir = "RemoteLogs"
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, os.path.basename(remote_path))

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname, port=22, username=username, password=password, timeout=10)

            with ssh.open_sftp() as sftp:
                sftp.get(remote_path, local_path)

            ssh.close()
            self.root.after(0, self._on_scp_download_success, local_path)

        except Exception as e:
            msg = f"다운로드 중 오류가 발생했습니다:\n{e}"
            self.root.after(0, self._on_scp_download_fail, "다운로드 오류", msg)

    def _on_scp_download_success(self, local_path):
        self.scp_btn.config(state=tk.NORMAL, text="... 리눅스 PC에서 불러오기 (SCP)")
        if local_path not in self.loaded_files:
            self.loaded_files.append(local_path)
            self.file_listbox.insert(tk.END, f"[REMOTE] {os.path.basename(local_path)}")
            self.file_listbox.selection_clear(0, tk.END)
            self.file_listbox.selection_set(tk.END)
            self.file_listbox.see(tk.END)
        messagebox.showinfo("성공", f"파일을 성공적으로 다운로드했습니다.\n로컬 경로: {os.path.abspath(local_path)}")

    def _on_scp_download_fail(self, title, message):
        self.scp_btn.config(state=tk.NORMAL, text="... 리눅스 PC에서 불러오기 (SCP)")
        messagebox.showerror(title, message)

    def identify_test_category(self, xml_text: str) -> str:
        xml_lower = xml_text.lower()
        detected_tests = sorted([test_name for keyword, test_name in self.keyword_to_test_map.items() if keyword in xml_lower])
        return ", ".join(detected_tests) if detected_tests else "알 수 없는 일반 RPC 시험"

    def _on_summary_select(self, event):
        """요약 테이블에서 항목 선택 시, 상세 로그를 하단에 표시합니다."""
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
        """상세 뷰 렌더링 메서드"""
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
        """분석 결과를 받아 요약 테이블(Treeview)을 갱신합니다. (존재하면 업데이트, 없으면 추가)"""
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
                
        # 선택된 항목이 있다면 상세 텍스트뷰 동기화
        selected_iids = self.summary_tree.selection()
        if selected_iids:
            selected_iid = selected_iids[0]
            if selected_iid in self.tree_item_details:
                self._render_detailed_view(self.tree_item_details[selected_iid])

    def _update_ui_after_analysis(self, all_analysis_results):
        """분석 완료 후 UI 업데이트 및 버튼 복구"""
        self._update_summary_tree(all_analysis_results)
        self._restore_buttons_after_analysis()

    def run_analysis(self):
        """분석 시작 전 UI 처리 및 스레드 실행 (cat 기능)"""
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

        # 3. 분석 시작 시 선택된 시험 항목을 미리 보여줌
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
        if self.is_monitoring:
            self.stop_realtime_analysis()
        else:
            self.start_realtime_analysis()

    def start_realtime_analysis(self):
        """실시간 분석(tail -f) 시작"""
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
        self.tail_btn.config(text="■ 분석 중지", bg="#f44336")

        self.monitoring_thread = threading.Thread(target=self._realtime_analysis_thread, args=(file_path, selected_items), daemon=True)
        self.monitoring_thread.start()

    def stop_realtime_analysis(self):
        self.is_monitoring = False
        self._restore_buttons_after_analysis()
        self.tail_btn.config(text="▷ 실시간 분석 (tail -f)", bg="#FF9800")

    def _realtime_analysis_thread(self, file_path, selected_items):
        """tail -F 와 유사하게 파일 변경을 감지하고 실시간으로 분석하는 스레드"""
        self.root.after(0, self.result_text.insert, tk.END, f"========== 실시간 분석 시작: {os.path.basename(file_path)} ==========\n")
        
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
                    self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 로그 파일이 변경(Rotation)되었습니다. 새 파일을 읽습니다...\n", "info")
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
                time.sleep(1)
            except Exception as e:
                self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 파일 읽기 오류: {e}\n", "error")
                time.sleep(2)
        
        f.close()
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 실시간 분석이 중지되었습니다. ==========\n")

    def toggle_folder_monitoring(self):
        """폴더 실시간 감시 시작/중지 토글"""
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
        self.tree_item_details.clear()
        
        self.run_btn.config(state=tk.DISABLED)
        self.tail_btn.config(state=tk.DISABLED)
        self.remote_folder_monitor_btn.config(state=tk.DISABLED)
        self.folder_monitor_btn.config(text="■ 폴더 감시 중지", bg="#f44336")

        self.folder_monitoring_thread = threading.Thread(target=self._folder_monitoring_thread, args=(selected_items,), daemon=True)
        self.folder_monitoring_thread.start()

    def stop_folder_monitoring(self):
        """로컬 폴더 감시 중지"""
        self.is_folder_monitoring = False
        self._restore_buttons_after_analysis()
        self.folder_monitor_btn.config(text="📂 로컬 폴더 감시", bg="#673AB7")

    def _folder_monitoring_thread(self, selected_items):
        """백그라운드에서 로컬 폴더를 감시하고 새로 생성된 파일을 분석하는 스레드"""
        self.root.after(0, self.result_text.insert, tk.END, f"========== 폴더 실시간 감시 시작: {self.monitored_folder_path} ==========\n")

        while self.is_folder_monitoring:
            try:
                current_files = [f for f in os.listdir(self.monitored_folder_path) if os.path.isfile(os.path.join(self.monitored_folder_path, f))]

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
                            analysis_summaries = self._analyze_content(
                                content, 
                                selected_items, 
                                filename
                            ) if content else []
                            if analysis_summaries:
                                self.root.after(0, self._update_summary_tree, analysis_summaries)
                        except Exception as e:
                            self.root.after(0, self.result_text.insert, tk.END, f"   파일 읽기/분석 실패 -> {str(e)}\n", "error")
                
                time.sleep(2)
            except FileNotFoundError:
                self.root.after(0, self.result_text.insert, tk.END, "\n[!] 감시 폴더를 찾을 수 없습니다. 감시를 중지합니다.\n", "error")
                self.root.after(0, self.stop_folder_monitoring)
                break
            except Exception as e:
                self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 폴더 감시 중 오류 발생: {e}\n", "error")
                time.sleep(5)
        
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 폴더 감시가 중지되었습니다. ==========\n")

    def toggle_remote_folder_monitoring(self):
        """원격 폴더 실시간 감시 시작/중지 토글"""
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
            self.tree_item_details.clear()
            
            self.run_btn.config(state=tk.DISABLED)
            self.tail_btn.config(state=tk.DISABLED)
            self.folder_monitor_btn.config(state=tk.DISABLED)
            self.remote_folder_monitor_btn.config(text="■ 원격 폴더 감시 중지", bg="#f44336")

            self.remote_folder_monitoring_thread = threading.Thread(target=self._remote_folder_monitoring_thread, args=(details, selected_items), daemon=True)
            self.remote_folder_monitoring_thread.start()
        elif dialog.result and not dialog.result.get('is_folder'): # 파일 감시
            details = dialog.result
            self.remote_monitored_folder_path = details['remote_path']
            
            self.is_remote_folder_monitoring = True
            self.summary_tree.delete(*self.summary_tree.get_children())
            self.result_text.delete("1.0", tk.END)
            self.test_item_history.clear()
            self.tree_item_details.clear()
            
            self.run_btn.config(state=tk.DISABLED)
            self.tail_btn.config(state=tk.DISABLED)
            self.folder_monitor_btn.config(state=tk.DISABLED)
            self.remote_folder_monitor_btn.config(text="■ 원격 파일 감시 중지", bg="#f44336")

            self.remote_folder_monitoring_thread = threading.Thread(target=self._remote_file_monitoring_thread, args=(details, selected_items), daemon=True)
            self.remote_folder_monitoring_thread.start()

    def stop_remote_folder_monitoring(self):
        """원격 폴더 감시 중지"""
        self.is_remote_folder_monitoring = False
        self._restore_buttons_after_analysis()
        self.remote_folder_monitor_btn.config(text="🌐 원격 폴더 감시", bg="#3F51B5")
        if self.sftp_client:
            try: 
                self.sftp_client.close()
            except: 
                pass
        if self.ssh_client:
            try: 
                self.ssh_client.close()
            except: 
                pass
        self.sftp_client = None
        self.ssh_client = None

    def _remote_file_monitoring_thread(self, details, selected_items):
        """백그라운드에서 원격 파일을 감시하고 변경분을 분석하는 스레드 (tail -f)"""
        self.root.after(0, self.result_text.insert, tk.END, f"========== 원격 파일 실시간 감시 시작: {details['remote_path']} ==========\n")
        
        last_size = 0
        
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_client.connect(details['host'], port=22, username=details['user'], password=details['password'], timeout=10)
            self.sftp_client = self.ssh_client.open_sftp()
            
            try:
                last_size = self.sftp_client.stat(details['remote_path']).st_size
            except FileNotFoundError:
                self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 파일을 찾을 수 없습니다. 감시를 시작할 수 없습니다.\n", "error")
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
                    
                    analysis_summaries = self._analyze_content(
                        accumulated_content, 
                        selected_items, 
                        os.path.basename(details['remote_path'])
                    ) if accumulated_content else []
                    
                    if analysis_summaries:
                        self.root.after(0, self._update_summary_tree, analysis_summaries)

                elif current_size < last_size:
                    self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 원격 파일이 잘렸거나 교체되었습니다. 처음부터 다시 읽습니다.\n", "info")
                    last_size = 0
                    accumulated_content = ""
                    self.root.after(0, self.clear_analysis_results)

                time.sleep(2)
            except Exception as e:
                if not self.is_remote_folder_monitoring: break
                self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 원격 파일 감시 중 오류 발생: {e}\n", "error")
                time.sleep(5)
        
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 원격 파일 감시가 중지되었습니다. ==========\n")

    def _remote_folder_monitoring_thread(self, details, selected_items):
        """백그라운드에서 원격 폴더를 감시하고 새로 생성된 파일을 분석하는 스레드"""
        self.root.after(0, self.result_text.insert, tk.END, f"========== 원격 폴더 실시간 감시 시작: {details['remote_path']} ==========\n")
        
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh_client.connect(details['host'], port=22, username=details['user'], password=details['password'], timeout=10)
            self.sftp_client = self.ssh_client.open_sftp()
            
            initial_items = self.sftp_client.listdir_attr(details['remote_path'])
            file_sizes = {item.filename: item.st_size for item in initial_items if not stat.S_ISDIR(item.st_mode)}
            
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
                            self.root.after(0, self.result_text.insert, tk.END, f"\n\n▶ 원격 신규 파일 감지: {filename}\n", "header")
                            
                        file_sizes[filename] = current_size
                        remote_file_path = posix_join(details['remote_path'], filename)
                        
                        try:
                            with self.sftp_client.open(remote_file_path, 'r') as f:
                                content = f.read().decode('utf-8', errors='ignore')
                            
                            analysis_summaries = self._analyze_content(
                                content, 
                                selected_items, 
                                filename
                            ) if content else []
                            if analysis_summaries:
                                self.root.after(0, self._update_summary_tree, analysis_summaries)
                                
                        except Exception as e:
                            self.root.after(0, self.result_text.insert, tk.END, f"   파일 읽기/분석 실패 -> {str(e)}\n", "error")
                        
                time.sleep(3)
            except Exception as e:
                if self.is_remote_folder_monitoring:
                    self.root.after(0, self.result_text.insert, tk.END, f"\n[!] 원격 폴더 감시 중 오류 발생: {e}\n", "error")
                    time.sleep(5)
                else:
                    break
                    
        self.root.after(0, self.result_text.insert, tk.END, "\n========== 원격 폴더 감시가 중지되었습니다. ==========\n")

    def _analysis_thread(self, files_to_analyze, selected_items):
        """실제 분석을 담당하는 백그라운드 스레드 (cat 기능)"""
        all_analysis_results = []

        for file_path in files_to_analyze:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                
                analysis_summaries = self._analyze_content(
                    content, 
                    selected_items, 
                    os.path.basename(file_path)
                )
                
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

    def _extract_xml_blocks(self, content: str) -> list:
        """로그 텍스트에서 안전하게 XML 블록들을 추출"""
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
                
            xml_str_clean = re.sub(r'xmlns="[^"]+"', '', xml_str) 
            xml_str_clean = re.sub(r'xmlns:[a-zA-Z0-9\-]+="[^"]+"', '', xml_str_clean)
            try:
                root = ET.fromstring(xml_str_clean)
                blocks.append((root, xml_str))
            except ET.ParseError:
                pass
        return blocks

    def _analyze_content(self, content: str, selected_items: list, file_identifier: str = None) -> list:
        """
        주어진 텍스트 블록(content)을 분석하여 결과 리포트(딕셔너리 리스트)를 생성합니다.
        
        Args:
            content: 분석할 로그 텍스트
            selected_items: 선택된 시험 항목 목록
            file_identifier: 파일명 또는 식별자 (기본값: "General")
        
        Returns:
            Treeview가 요구하는 {'item', 'result', 'summary', 'details'} 포맷의 리스트
        """
        if file_identifier is None:
            file_identifier = "General"
        
        all_results = []

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
            if item in test_runners:
                try:
                    result_dict = test_runners[item](content)
                    
                    status = result_dict.get('status', 'UNKNOWN')
                    summary_msg = result_dict.get('summary_msg', '')
                    detailed_report_parts = result_dict.get('detailed_report_parts', [])
                    run_timestamp = result_dict.get('run_timestamp', datetime.now())
                    
                    # 이력 추적 (test_item_history 업데이트)
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
                    history["total_runs"] += 1
                    
                    # 상태별 카운트 증가
                    if status == 'PASS':
                        history["pass_count"] += 1
                    elif status == 'FAIL':
                        history["fail_count"] += 1
                    elif status == 'ACTIVE':
                        history["active_count"] += 1
                    elif status == 'UNKNOWN':
                        history["unknown_count"] += 1
                    elif status == 'ERROR':
                        history["error_count"] += 1
                    
                    # 실행 이력 저장 (최대 10개 유지)
                    history["run_history"].append({
                        'run_timestamp': run_timestamp,
                        'status': status,
                        'summary_msg': summary_msg,
                        'detailed_report_parts': detailed_report_parts,
                        'file_identifier': file_identifier
                    })
                    if len(history["run_history"]) > 10:
                        history["run_history"].pop(0)
                    
                    # 결과 추가
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

    def _check_startup_call_home(self, content: str) -> dict:
        report = [{'text': "\n--- [ M-Plane: Startup & Call Home ] ---\n", 'tag': 'header'}]
        xml_blocks = self._extract_xml_blocks(content)
        
        has_callhome = "call-home" in content.lower() or "callhome" in content.lower() or "call home" in content.lower()
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

    def _check_user_account(self, content: str) -> dict:
        report = [{'text': "\n--- [ M-Plane: User Account ] ---\n", 'tag': 'header'}]
        if "user" in content.lower() or "password" in content.lower() or "account" in content.lower():
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

    def _check_edit_config_rollback(self, content: str) -> dict:
        """
        ⭐ [개선] edit-config 롤백 메커니즘 검증
        
        판정 로직:
        1. edit-config 성공 + rollback 옵션 있음 → PASS (안전한 설정)
        2. edit-config 성공 + rollback 옵션 없음 → PASS (성공적으로 적용됨)
        3. edit-config 실패 + rollback 옵션 있음 → PASS (롤백 메커니즘 작동)
        4. edit-config 실패 + rollback 옵션 없음 → FAIL (보호 메커니즘 부재)
        """
        report = [{'text': "\n--- [ Config Management: edit-config (rollback) ] ---\n", 'tag': 'header'}]
        xml_blocks = self._extract_xml_blocks(content)
        
        edits = []
        rollback_found = False
        edit_config_success = False
        edit_config_error = False
        
        for root, xml_str in xml_blocks:
            xml_lower = xml_str.lower()
            
            # 1. <edit-config> RPC 찾기
            if any(elem.tag.endswith('edit-config') for elem in root.iter()):
                edits.append((root, xml_str))
                
                # 2. rollback-on-error 옵션 확인 (여러 방법)
                # 2-1. 정확한 XML 요소 확인
                for elem in root.iter():
                    if elem.tag.endswith('error-option') and 'rollback' in (elem.text or '').lower():
                        rollback_found = True
                        break
                
                # 2-2. XML 텍스트에서 직접 확인 (대소문자 무시)
                if not rollback_found and 'rollback-on-error' in xml_lower:
                    rollback_found = True
                
                # 2-3. netopeer2-cli --error rollback 옵션도 인정
                if not rollback_found and '--error rollback' in xml_lower:
                    rollback_found = True
            
            # 3. edit-config 응답 상태 확인
            if root.tag.endswith('rpc-reply'):
                # rpc-reply에 error가 없으면 성공
                has_error = any('rpc-error' in elem.tag for elem in root.iter())
                
                if not has_error:
                    edit_config_success = True
                elif has_error:
                    edit_config_error = True
        
        # 분석 결과 판정
        status = 'UNKNOWN'
        summary_msg = ""
        
        if edits:
            report.append({'text': f"   Found <edit-config> RPC(s): {len(edits)}\n", 'tag': 'info'})
            
            if edit_config_success:
                # edit-config가 성공했으면 PASS
                report.append({'text': f"   Result: [ PASS ] - <edit-config> executed successfully.\n", 'tag': 'pass'})
                status = 'PASS'
                summary_msg = "edit-config executed successfully"
                
                if rollback_found:
                    report.append({'text': f"   -> Additional: 'rollback-on-error' option is configured.\n", 'tag': 'info'})
                else:
                    report.append({'text': f"   -> Note: 'rollback-on-error' option not configured (optional for successful execution).\n", 'tag': 'info'})
            
            elif edit_config_error:
                # edit-config가 실패했을 때
                if rollback_found:
                    report.append({'text': f"   Result: [ PASS ] - <edit-config> failed but rollback-on-error option applied.\n", 'tag': 'pass'})
                    status = 'PASS'
                    summary_msg = "Rollback mechanism verified"
                else:
                    report.append({'text': f"   Result: [ FAIL ] - <edit-config> failed and no rollback-on-error option found.\n", 'tag': 'fail'})
                    status = 'FAIL'
                    summary_msg = "edit-config failed without rollback protection"
            
            else:
                # 응답 상태를 확인할 수 없으면
                if rollback_found:
                    report.append({'text': f"   Result: [ PASS ] - 'rollback-on-error' option is configured.\n", 'tag': 'pass'})
                    status = 'PASS'
                    summary_msg = "rollback-on-error option configured"
                else:
                    report.append({'text': f"   Result: [ PASS ] - <edit-config> RPC found (no errors in log).\n", 'tag': 'pass'})
                    status = 'PASS'
                    summary_msg = "edit-config executed without errors"
            
            # 상세 정보
            for idx, (root, xml_str) in enumerate(edits):
                report.append({'text': f"\n      [Edit-Config RPC #{idx+1}]\n", 'tag': 'header'})
                report.append({'text': f"{xml_str.strip()}\n", 'tag': 'info'})
        
        else:
            report.append({'text': "   Result: [ UNKNOWN ] - No <edit-config> RPC found in log.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
            summary_msg = "No edit-config found"
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report,
            'run_timestamp': datetime.now()
        }

    def _check_state_change(self, content: str) -> dict:
        report_parts = [{'text': "\n--- [ Config Management: State Change (admin/oper/availability/usage) ] ---\n", 'tag': 'header'}]
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
            if 'locked' in unique_admin:
                report_parts.append({'text': f"      * Note: 'locked' state found. Verify all I/O signals are restricted.\n", 'tag': 'info'})
        else:
            report_parts.append({'text': f"   -> [ UNKNOWN ] admin-state: Not found\n", 'tag': 'unknown'})
        
        if avail_states:
            found_any = True
            unique_avail = set(s.strip().upper() for s in avail_states)
            report_parts.append({'text': f"   -> [ PASS ] Found availability-state: {', '.join(unique_avail)}\n", 'tag': 'pass'})
            for state in unique_avail:
                if state == 'FAULTY':
                    report_parts.append({'text': f"      * Note: FAULTY - Check if oper-state is disabled or critical alarm exists.\n", 'tag': 'info'})
                elif state == 'DEGRADED':
                    report_parts.append({'text': f"      * Note: DEGRADED - Check if major alarm exists.\n", 'tag': 'info'})
                elif state == 'NORMAL':
                    report_parts.append({'text': f"      * Note: NORMAL - No alarms or minor alarm only.\n", 'tag': 'info'})
                elif state == 'UNKNOWN':
                    report_parts.append({'text': f"      * Note: UNKNOWN - RU reset and shared config not set.\n", 'tag': 'info'})
        else:
            report_parts.append({'text': f"   -> [ UNKNOWN ] availability-state: Not found\n", 'tag': 'unknown'})

        if usage_states:
            found_any = True
            unique_usage = set(s.strip().lower() for s in usage_states)
            report_parts.append({'text': f"   -> [ PASS ] Found usage-state: {', '.join(unique_usage)}\n", 'tag': 'pass'})
            for state in unique_usage:
                if state == 'idle':
                    report_parts.append({'text': f"      * Note: idle - No carrier info in RU.\n", 'tag': 'info'})
                elif state == 'active':
                    report_parts.append({'text': f"      * Note: active - UL/DL info in 1+ carriers.\n", 'tag': 'info'})
                elif state == 'busy':
                    report_parts.append({'text': f"      * Note: busy - UL/DL info in all carriers.\n", 'tag': 'info'})
        else:
            report_parts.append({'text': f"   -> [ UNKNOWN ] usage-state: Not found\n", 'tag': 'unknown'})
                    
        if not found_any and ("admin-state" in content.lower() or "oper-state" in content.lower()):
            report_parts.append({'text': f"   Result: [ PASS ] - Found State Change (admin/oper) evidence (by keyword).\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = "State Change (admin/oper) evidence found by keyword."
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

    def _check_subscription_notification(self, content: str) -> dict:
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
            report_parts.append({'text': f"   Result: [ ACTIVE ] - Receiving notifications. Last seen: {timestamp}\n", 'tag': 'pass'})
            status = 'ACTIVE'
            summary_msg = f"Receiving notifications. Last seen: {timestamp}"
            for idx, notif_msg in enumerate(notifs):
                report_parts.append({'text': f"      [Notification Evidence #{idx+1}]\n{notif_msg.strip()}\n", 'tag': 'info'})
        elif subs:
            report_parts.append({'text': f"   Result: [ PASS ] - Found <create-subscription> RPC ({len(subs)} found), but no notifications yet.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(subs)} <create-subscription> RPC(s), but no notifications yet."
            for idx, sub_msg in enumerate(subs):
                report_parts.append({'text': f"      [Subscription RPC #{idx+1}]\n{sub_msg.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   -> <create-subscription>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   -> <notification>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No Subscription or Notification evidence found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_hello_exchange(self, content: str) -> dict:
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
            for idx, h in enumerate(hellos):
                report_parts.append({'text': f"      [Hello Message #{idx+1}]\n{h.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No <hello> message found in the log.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
            summary_msg = "No <hello> messages found."
            
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_supervision(self, content: str) -> dict:
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
            
            report_parts.append({'text': f"   Result: [ PASS ] - Supervision is active. Last seen: {timestamp}\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Supervision is active. Last seen: {timestamp}"
            for idx, msg in enumerate(supervisions):
                report_parts.append({'text': f"      [Supervision Evidence #{idx+1}]\n{msg.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   -> <supervision>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No supervision messages found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_get_config(self, content: str) -> dict:
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        gets = [(root, xml_str) for root, xml_str in xml_blocks if any(elem.tag.endswith('get') or elem.tag.endswith('get-config') for elem in root.iter())]
        status = 'UNKNOWN'
        summary_msg = "No <get> or <get-config> found."

        report_parts.append({'text': "\n--- [ Config Management: <get> / <get-config> ] ---\n", 'tag': 'header'})
        if gets:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(gets)} <get> / <get-config> RPC(s).\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(gets)} <get> / <get-config> RPC(s)."
            
            for idx, (g_root, g_str) in enumerate(gets):
                report_parts.append({'text': f"      [요청 #{idx+1} (<get> 또는 <get-config>)]\n{g_str.strip()}\n", 'tag': 'info'})
                
                msg_id = g_root.get('message-id')
                if msg_id:
                     replies = [r_str for r_root, r_str in xml_blocks if r_root.tag.endswith('rpc-reply') and r_root.get('message-id') == msg_id]
                     if replies:
                         report_parts.append({'text': f"      [응답 #{idx+1} (message-id: {msg_id})]\n{replies[0].strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   -> <get>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   -> <get-config>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No <get> or <get-config> found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_cu_plane_config(self, content: str) -> dict:
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        cu_configs = [xml_str for root, xml_str in xml_blocks if any('uplane-conf' in elem.tag for elem in root.iter())]
        status = 'UNKNOWN'
        summary_msg = "No C/U-Plane configuration found."

        report_parts.append({'text': "\n--- [ C/U-Plane: Full Configuration ] ---\n", 'tag': 'header'})
        if cu_configs:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(cu_configs)} C/U-Plane configuration messages.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(cu_configs)} C/U-Plane configuration messages."
            for idx, config_msg in enumerate(cu_configs):
                report_parts.append({'text': f"      [C/U-Plane Config #{idx+1}]\n{config_msg.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   -> <uplane-conf>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No C/U-Plane configuration found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_log_management(self, content: str) -> dict:
        report_parts = []
        xml_blocks = self._extract_xml_blocks(content)
        logs = [xml_str for root, xml_str in xml_blocks if any('troubleshooting' in elem.tag for elem in root.iter())]
        status = 'UNKNOWN'
        summary_msg = "No Troubleshooting Log messages found."

        report_parts.append({'text': "\n--- [ Log Management: Troubleshooting Log ] ---\n", 'tag': 'header'})
        if logs:
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(logs)} Troubleshooting Log messages.\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(logs)} Troubleshooting Log messages."
            for idx, log_msg in enumerate(logs):
                report_parts.append({'text': f"      [Troubleshooting Log #{idx+1}]\n{log_msg.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   -> <troubleshooting>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No Troubleshooting Log messages found.\n", 'tag': 'unknown'})
            status = 'UNKNOWN'
        
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_generic_rpc_errors(self, content: str) -> dict:
        report_parts = []
        status = 'UNKNOWN'
        summary_msg = "No <rpc-error> instances found."
        
        xml_blocks = self._extract_xml_blocks(content)
        error_replies = []
        for root, xml_str in xml_blocks:
            has_error = any('rpc-error' in elem.tag for elem in root.iter())
            if has_error:
                error_replies.append((root, xml_str))
                
        report_parts.append({'text': "\n--- [ Generic: RPC Error Validation ] ---\n", 'tag': 'header'})
        if error_replies:
            report_parts.append({'text': f"   Result: [ FAIL ] - Found {len(error_replies)} <rpc-error> instance(s).\n", 'tag': 'fail'})
            status = 'FAIL'
            summary_msg = f"Found {len(error_replies)} <rpc-error> instance(s)."
            for i, (root, err_str) in enumerate(error_replies, 1):
                has_type = any('error-type' in elem.tag for elem in root.iter())
                has_tag = any('error-tag' in elem.tag for elem in root.iter())
                has_severity = any('error-severity' in elem.tag for elem in root.iter())
                
                missing = []
                if not has_type: 
                    missing.append('error-type')
                if not has_tag: 
                    missing.append('error-tag')
                if not has_severity: 
                    missing.append('error-severity')
                
                category = self.identify_test_category(err_str)
                report_parts.append({'text': f"     -> Category: {category}\n", 'tag': 'info'})
                
                if missing:
                    report_parts.append({'text': f"     -> [ Warning ] Missing RFC6241 mandatory fields: {', '.join(missing)}\n", 'tag': 'error'})
                else:
                    report_parts.append({'text': f"     -> [ PASS ] Contains mandatory fields (error-type, error-tag, error-severity).\n", 'tag': 'pass'})
                    
                report_parts.append({'text': f"     [Error Evidence #{i}]\n{err_str.strip()}\n\n", 'tag': 'info'})
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

    def _check_sw_management(self, content: str) -> dict:
        report_parts = []
        final_status = 'UNKNOWN'
        summary_msg = "No Software Management RPCs (download, install, activate) found."
        found_activity = False

        xml_blocks = self._extract_xml_blocks(content)

        # --- 1. Download Phase ---
        download_rpcs = [xml_str for root, xml_str in xml_blocks if any('software-download' in elem.tag for elem in root.iter())]
        download_events = [xml_str for root, xml_str in xml_blocks if 'notification' in root.tag and any('download-event' in elem.tag for elem in root.iter())]
        
        report_parts.append({'text': "\n--- [ Software Management: Down/Install/Activate ] ---\n", 'tag': 'header'})
        if download_rpcs or download_events:
            found_activity = True
            report_parts.append({'text': f"   [Download] Found {len(download_rpcs)} RPC(s) and {len(download_events)} Event(s).\n", 'tag': 'info'})
            if download_rpcs and not download_events:
                report_parts.append({'text': f"   -> Result: [ FAIL ] - Download RPC found, but no 'download-event' notification detected.\n", 'tag': 'fail'})
                final_status = 'FAIL'
            elif download_events:
                if any('failed' in evt.lower() for evt in download_events):
                    report_parts.append({'text': f"   -> Result: [ FAIL ] - Download FAILED event detected.\n", 'tag': 'fail'})
                    final_status = 'FAIL'
                elif any('completed' in evt.lower() for evt in download_events):
                    report_parts.append({'text': f"   -> Result: [ PASS ] - Download COMPLETED event detected.\n", 'tag': 'pass'})
                    if final_status != 'FAIL': 
                        final_status = 'PASS'
                else:
                    report_parts.append({'text': f"   -> Result: [ UNKNOWN ] - Download event found, but status is unclear.\n", 'tag': 'unknown'})
                for idx, msg in enumerate(download_events):
                    report_parts.append({'text': f"      [Download Event #{idx+1}]\n{msg.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   [Download] No RPC or Event found.\n", 'tag': 'unknown'})

        # --- 2. Install Phase ---
        install_rpcs = [xml_str for root, xml_str in xml_blocks if any('software-install' in elem.tag for elem in root.iter())]
        install_events = [xml_str for root, xml_str in xml_blocks if 'notification' in root.tag and any('install-event' in elem.tag for elem in root.iter())]

        if install_rpcs or install_events:
            found_activity = True
            report_parts.append({'text': f"   [Install] Found {len(install_rpcs)} RPC(s) and {len(install_events)} Event(s).\n", 'tag': 'info'})
            if install_rpcs and not install_events:
                report_parts.append({'text': f"   -> Result: [ FAIL ] - Install RPC found, but no 'install-event' notification detected.\n", 'tag': 'fail'})
                final_status = 'FAIL'
            elif install_events:
                if any('failed' in evt.lower() for evt in install_events):
                    report_parts.append({'text': f"   -> Result: [ FAIL ] - Install FAILED event detected.\n", 'tag': 'fail'})
                    final_status = 'FAIL'
                elif any('completed' in evt.lower() for evt in install_events):
                    report_parts.append({'text': f"   -> Result: [ PASS ] - Install COMPLETED event detected.\n", 'tag': 'pass'})
                    if final_status != 'FAIL': 
                        final_status = 'PASS'
                else:
                    report_parts.append({'text': f"   -> Result: [ UNKNOWN ] - Install event found, but status is unclear.\n", 'tag': 'unknown'})
                for idx, msg in enumerate(install_events):
                    report_parts.append({'text': f"      [Install Event #{idx+1}]\n{msg.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   [Install] No RPC or Event found.\n", 'tag': 'unknown'})

        # --- 3. Activate Phase ---
        activate_rpcs = [xml_str for root, xml_str in xml_blocks if any('software-activate' in elem.tag for elem in root.iter())]
        activate_events = [xml_str for root, xml_str in xml_blocks if 'notification' in root.tag and any('activation-event' in elem.tag for elem in root.iter())]

        if activate_rpcs or activate_events:
            found_activity = True
            report_parts.append({'text': f"   [Activate] Found {len(activate_rpcs)} RPC(s) and {len(activate_events)} Event(s).\n", 'tag': 'info'})
            if activate_rpcs and not activate_events:
                report_parts.append({'text': f"   -> Result: [ FAIL ] - Activate RPC found, but no 'activation-event' notification detected.\n", 'tag': 'fail'})
                final_status = 'FAIL'
            elif activate_events:
                report_parts.append({'text': f"   -> Result: [ PASS ] - Activation event detected.\n", 'tag': 'pass'})
                if final_status != 'FAIL': 
                    final_status = 'PASS'
                for idx, msg in enumerate(activate_events):
                    report_parts.append({'text': f"      [Activate Event #{idx+1}]\n{msg.strip()}\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   [Activate] No RPC or Event found.\n", 'tag': 'unknown'})

        if not found_activity:
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No Software Management RPCs (download, install, activate) found.\n", 'tag': 'unknown'})
        elif final_status == 'UNKNOWN':
            final_status = 'PASS'
            summary_msg = "Software Management activity detected."
        else:
            summary_msg = f"Software Management: {final_status} overall."

        return {
            'status': final_status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _check_fault_management(self, content: str) -> dict:
        report_parts = []
        status = 'UNKNOWN'
        summary_msg = "No alarm notifications found in log."
        
        xml_blocks = self._extract_xml_blocks(content)
        alarm_notifs = []
        for root, xml_str in xml_blocks:
            if root.tag.endswith('notification'):
                if any('alarm-notification' in elem.tag or 'alarm-event' in elem.tag for elem in root.iter()):
                    alarm_notifs.append(xml_str)

        report_parts.append({'text': "\n--- [ Fault Management: Alarm Notification Validation ] ---\n", 'tag': 'header'})
        if alarm_notifs:
            last_alarm = alarm_notifs[-1]
            timestamp_match = re.search(r'<eventTime>([^<]+)</eventTime>', last_alarm)
            timestamp = timestamp_match.group(1) if timestamp_match else "N/A"
            
            report_parts.append({'text': f"   Result: [ PASS ] - Found {len(alarm_notifs)} alarm(s). Last seen: {timestamp}\n", 'tag': 'pass'})
            status = 'PASS'
            summary_msg = f"Found {len(alarm_notifs)} alarm(s). Last seen: {timestamp}"
            for idx, alarm_msg in enumerate(alarm_notifs):
                report_parts.append({'text': f"     [Alarm Evidence #{idx+1}]\n{alarm_msg.strip()}\n\n", 'tag': 'info'})
        else:
            report_parts.append({'text': "   -> <alarm-notification> / <alarm-event>: Not found\n", 'tag': 'unknown'})
            report_parts.append({'text': "   Result: [ UNKNOWN ] - No alarm notifications found in log.\n", 'tag': 'unknown'})
            
        return {
            'status': status,
            'summary_msg': summary_msg,
            'detailed_report_parts': report_parts,
            'run_timestamp': datetime.now()
        }

    def _restore_buttons_after_analysis(self):
        """분석 완료 후 UI 버튼들 원상복구"""
        self.run_btn.config(state=tk.NORMAL, text="▶ 전체 로그 분석 (cat)", bg="#4CAF50")
        self.tail_btn.config(state=tk.NORMAL)
        if hasattr(self, 'folder_monitor_btn'):
            self.folder_monitor_btn.config(state=tk.NORMAL)
        if hasattr(self, 'remote_folder_monitor_btn'):
            self.remote_folder_monitor_btn.config(state=tk.NORMAL)

    def export_report(self):
        """화면에 뜬 분석 결과를 실제 파일로 저장"""
        if not self.test_item_history:
            messagebox.showwarning("경고", "저장할 분석 결과가 없습니다. 먼저 분석을 실행해 주세요.")
            return
        
        if self.is_monitoring:
            if not messagebox.askyesno("확인", "실시간 분석이 진행 중입니다.\n지금까지의 내용만 리포트로 저장하시겠습니까?"):
                return

        output_dir = "Reports"
        os.makedirs(output_dir, exist_ok=True)
        
        report_filename = f"MPlane_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        report_path = os.path.join(output_dir, report_filename)

        # 리포트 포맷팅 (요약 + 상세)
        report_header = "=" * 80 + "\n"
        report_header += "                 O-RAN M-Plane Conformance Test Report\n"
        report_header += "=" * 80 + "\n"
        report_header += f"Analysis Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        report_header += "=" * 80 + "\n\n"

        summary_section = "[ Test Summary ]\n"
        summary_section += "-" * 80 + "\n"
        summary_section += f"{'Test Item':<45} | {'Result':<10} | {'Summary'}\n"
        summary_section += "-" * 80 + "\n"

        # Generate summary section from the aggregated history
        for item_name, history in self.test_item_history.items():
            if item_name in [item for item, var in self.item_vars.items() if var.get()]:
                summary_msg = f"{history['latest_status']} (Total: {history['total_runs']}, P:{history['pass_count']}, F:{history['fail_count']}, A:{history['active_count']})"
                
                display_item = item_name
                if len(display_item) > 43:
                    display_item = "..." + display_item[-40:]
                    
                summary_section += f"{display_item:<45} | {history['latest_status']:<10} | {summary_msg}\n"

        summary_section += "-" * 80 + "\n\n"
        
        detailed_section = "[ Detailed Logs (Last 10 Runs per Item) ]\n" + "-" * 80 + "\n"
        for item_name, history in self.test_item_history.items():
            if item_name in [item for item, var in self.item_vars.items() if var.get()]:
                detailed_section += f"\n--- [ {item_name} - 상세 분석 결과 ] ---\n"
                detailed_section += f"  최종 상태: {history['latest_status']}\n"
                detailed_section += f"  총 실행 횟수: {history['total_runs']}\n"
                detailed_section += f"  PASS: {history['pass_count']}\n"
                detailed_section += f"  FAIL: {history['fail_count']}\n"
                detailed_section += f"  ACTIVE: {history['active_count']}\n"
                detailed_section += f"  UNKNOWN: {history['unknown_count']}\n"
                detailed_section += f"  ERROR: {history['error_count']}\n"
                detailed_section += "\n--- [ 최근 10회 실행 로그 ] ---\n"

                for i, run in enumerate(reversed(history['run_history'])):
                    detailed_section += f"\n--- [ 실행 #{history['total_runs'] - i} - {run['run_timestamp'].strftime('%Y-%m-%d %H:%M:%S')} ({run['file_identifier']}) ] ---\n"
                    detailed_section += f"  결과: {run['status']} - {run['summary_msg']}\n"
                    for part in run['detailed_report_parts']:
                        detailed_section += part.get('text', '')
                    detailed_section += "\n"
                detailed_section += "\n"
        
        full_report = report_header + summary_section + detailed_section

        try:
            with open(report_path, 'w', encoding='utf-8') as rf:
                rf.write(full_report)
            
            messagebox.showinfo("저장 완료", f"리포트가 성공적으로 저장되었습니다.\n\n경로: {os.path.abspath(report_path)}")
            
            if sys.platform == "win32":
                os.startfile(os.path.abspath(output_dir))
            elif sys.platform == "darwin":
                subprocess.call(["open", os.path.abspath(output_dir)])
            else:
                subprocess.call(["xdg-open", os.path.abspath(output_dir)])
                
        except Exception as e:
            messagebox.showerror("저장 실패", f"리포트를 저장하는 중 오류가 발생했습니다:\n{e}")


class ScpDialog(tk.Toplevel):
    """WinSCP와 유사한 원격 파일/폴더 탐색 및 다운로드 대화상자."""
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
        self.profiles = []

        # --- 1. 접속 정보 입력 프레임 ---
        self.conn_frame = tk.Frame(self, padx=20, pady=20)
        self.conn_frame.pack(fill="both", expand=True)

        # --- Profile Management ---
        profile_frame = tk.Frame(self.conn_frame)
        profile_frame.pack(fill="x", pady=(0, 10))
        tk.Label(profile_frame, text="접속 프로필", width=15, anchor="w").pack(side="left")
        self.profile_combo = ttk.Combobox(profile_frame, state="readonly")
        self.profile_combo.pack(side="left", fill="x", expand=True)
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_select)

        conn_fields = {
            "host": "서버 주소 (IP)",
            "user": "사용자 이름",
            "password": "비밀번호"
        }
        self.entries = {}

        self.settings_file = "netconf_settings.json"
        
        for key, label_text in conn_fields.items():
            row = tk.Frame(self.conn_frame)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=label_text, width=15, anchor="w").pack(side="left")
            entry = tk.Entry(row, show="*" if key == "password" else "", width=35)
            entry.pack(side="left", fill="x", expand=True)
            entry.bind("<Return>", lambda event: self._connect_to_server())
            self.entries[key] = entry

        self._load_settings()

        # --- Profile Buttons ---
        profile_btn_frame = tk.Frame(self.conn_frame)
        profile_btn_frame.pack(fill="x", pady=10)
        tk.Button(profile_btn_frame, text="현재 정보로 프로필 저장", command=self._save_current_profile, bg="#8BC34A", fg="white").pack(side="left", expand=True, padx=5)
        tk.Button(profile_btn_frame, text="선택 프로필 삭제", command=self._delete_selected_profile, bg="#FF7043", fg="white").pack(side="left", expand=True, padx=5)
        
        self.conn_status_label = tk.Label(self.conn_frame, text="", fg="blue")
        self.conn_status_label.pack(pady=10)

        btn_frame = tk.Frame(self.conn_frame, pady=20)
        btn_frame.pack(fill="x")
        self.connect_btn = tk.Button(btn_frame, text="서버 접속", command=self._connect_to_server, bg="#009688", fg="white", width=12, height=2)
        self.connect_btn.pack(side="left", expand=True, padx=5)
        tk.Button(btn_frame, text="취소", command=self._on_close, width=12, height=2).pack(side="left", expand=True, padx=5)

        # --- 2. 파일 탐색기 프레임 (초기에는 숨김) ---
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
        tk.Button(browser_btn_frame, text="파일 감시 선택", command=self._on_select_file, bg="#4CAF50", fg="white", height=2).pack(side='left', expand=True, padx=5)
        tk.Button(browser_btn_frame, text="현재 폴더 선택", command=self._on_select_folder, bg="#2196F3", fg="white", height=2).pack(side='left', expand=True, padx=5)
        tk.Button(browser_btn_frame, text="취소", command=self._on_close, height=2).pack(side='left', expand=True, padx=5)

    def _load_settings(self):
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_settings(self):
        try:
            settings = self._load_settings()
            settings["last_remote_monitor_host"] = self.entries["host"].get()
            settings["last_remote_monitor_user"] = self.entries["user"].get()
            settings["last_remote_monitor_password"] = base64.b64encode(self.entries["password"].get().encode('utf-8')).decode('utf-8')
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
        except Exception:
            pass

    def _save_path_setting(self, path):
        try:
            settings = self._load_settings()
            settings["last_remote_monitor_path"] = path
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
        except Exception:
            pass

    def _connect_to_server(self):
        """서버 접속 시도 (UI 멈춤 방지를 위해 스레드 사용)"""
        if not all(entry.get() for entry in self.entries.values()):
            messagebox.showwarning("입력 오류", "모든 접속 정보를 입력해야 합니다.", parent=self)
            return

        settings = self._load_settings()
        settings["last_used_profile_name"] = self.profile_combo.get()
        self._save_settings_file(settings)

        self.connect_btn.config(state=tk.DISABLED)
        self.conn_status_label.config(text="서버에 접속 중입니다...")
        self.update_idletasks()
        threading.Thread(target=self._ssh_connect_thread, daemon=True).start()

    def _ssh_connect_thread(self):
        """백그라운드 SSH 접속 스레드"""
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
            self.after(0, self.conn_status_label.config, {"text": "접속 실패. 다시 시도해주세요."})
            self.after(0, self.connect_btn.config, {"state": tk.NORMAL})

    def _show_browser_ui(self):
        """접속 성공 시, 파일 탐색기 UI로 전환"""
        self.conn_frame.pack_forget()
        self.title(f"SCP 원격 파일 탐색기 - {self.entries['host'].get()}")
        self.browser_frame.pack(fill="both", expand=True)
        
        settings = self._load_settings()
        if saved_path := settings.get("last_remote_monitor_path", ""):
            try:
                self.sftp.stat(saved_path)
                self._list_remote_path(saved_path)
                return
            except Exception:
                pass
                
        home_dir = self.sftp.normalize('.')
        self._list_remote_path(home_dir)

    def _list_remote_path(self, path):
        """SFTP를 통해 원격 경로의 파일/디렉토리 목록을 가져와 표시"""
        try:
            self.file_listbox.delete(0, tk.END)
            self.current_path_label.config(text=f"Path: {path}")
            
            items = self.sftp.listdir_attr(path)
            dirs = sorted([item for item in items if stat.S_ISDIR(item.st_mode)], key=lambda i: i.filename.lower())
            files = sorted([item for item in items if not stat.S_ISDIR(item.st_mode)], key=lambda i: i.filename.lower())

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
        """리스트박스 더블클릭 이벤트 처리"""
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
        else:
            self._on_select_file()

    def _navigate_up(self):
        """상위 폴더로 이동"""
        current_path = self.current_path_label.cget("text").split("Path: ")[1]
        parent_path = posix_dirname(current_path)
        if not parent_path: 
            parent_path = "/"
        self._list_remote_path(parent_path)

    def _on_select_file(self):
        """선택된 파일을 감시 대상으로 설정하고 창을 닫음"""
        if not self.file_listbox.curselection():
            messagebox.showwarning("선택 오류", "감시할 파일을 선택해주세요.", parent=self)
            return
        
        selected_item = self.file_listbox.get(self.file_listbox.curselection())
        if selected_item.startswith("["):
            messagebox.showwarning("선택 오류", "파일만 감시할 수 있습니다. 폴더 감시를 원하시면 '현재 폴더 선택'을 이용하세요.", parent=self)
            return

        current_path = self.current_path_label.cget("text").split("Path: ")[1]
        self._save_path_setting(current_path)
        self.result = {
            'host': self.entries['host'].get(),
            'user': self.entries['user'].get(),
            'password': self.entries['password'].get(),
            'remote_path': posix_join(current_path, selected_item),
            'is_folder': False
        }
        self._on_close()

    def _on_select_folder(self):
        """현재 경로를 원격 폴더로 설정하고 창을 닫음"""
        current_path = self.current_path_label.cget("text").split("Path: ")[1]
        self._save_path_setting(current_path)
        self.result = {
            'host': self.entries['host'].get(),
            'user': self.entries['user'].get(),
            'password': self.entries['password'].get(),
            'remote_path': current_path,
            'is_folder': True
        }
        self._on_close()

    def _on_close(self):
        """창이 닫힐 때 SSH 연결을 안전하게 종료"""
        if self.sftp: 
            self.sftp.close()
        if self.ssh: 
            self.ssh.close()
        self.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MPlaneAnalyzerApp(root)
    root.mainloop()