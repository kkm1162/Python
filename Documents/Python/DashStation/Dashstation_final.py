import tkinter as tk
from tkinter import messagebox, simpledialog, filedialog, ttk
import subprocess
import json
import os
import sys
import time
import re
import base64
import copy
import threading
import ctypes
import platform
import tempfile
import uuid
from datetime import datetime, timedelta

# ========== [환경 설정 및 경로] ==========
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_FILE = os.path.join(BASE_DIR, "DashStation_Data.json")
MACRO_DIR = os.path.join(BASE_DIR, "Macros") 
ICON_FILE = os.path.join(BASE_DIR, "app_icon1.ico")
DEFAULT_LOG_DIR = os.path.join(os.environ.get('LOCALAPPDATA', BASE_DIR), "DashStation", "Logs")
IS_WINDOWS = platform.system() == "Windows"

if not os.path.exists(MACRO_DIR): 
    os.makedirs(MACRO_DIR)

TYPE_MAP = {"S": "SSH (TeraTerm)", "R": "RDP (Remote)", "M": "MobaXterm (BM)", "P": "Python / EXE", "C": "Serial (COM Port)"}
CONNECT_TEXT_MAP = {"S": "SSH CONNECT", "R": "RDP CONNECT", "M": "MOBA CONNECT", "P": "EXE / PY CONNECT", "C": "SERIAL CONNECT"}
INV_TYPE_MAP = {v: k for k, v in TYPE_MAP.items()}
CARD_WIDTH = 280

# ========== [보안: 비밀번호 암호화] ==========
class SecureData:
    """Windows DPAPI 기반 비밀번호 보호 (레거시 Base64 자동 호환)"""
    DPAPI_PREFIX = "dpapi:"
    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_uint32),
            ("pbData", ctypes.POINTER(ctypes.c_char))
        ]

    @staticmethod
    def _bytes_to_blob(data):
        buf = ctypes.create_string_buffer(data, len(data))
        blob = SecureData.DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        return blob, buf

    @staticmethod
    def _blob_to_bytes(blob):
        return ctypes.string_at(blob.pbData, blob.cbData)

    @staticmethod
    def _dpapi_protect(raw_bytes):
        if not IS_WINDOWS:
            raise OSError("DPAPI is only available on Windows")

        in_blob, in_buf = SecureData._bytes_to_blob(raw_bytes)
        out_blob = SecureData.DATA_BLOB()

        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            SecureData.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob)
        )
        if not ok:
            raise ctypes.WinError()

        try:
            return SecureData._blob_to_bytes(out_blob)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)

    @staticmethod
    def _dpapi_unprotect(encrypted_bytes):
        if not IS_WINDOWS:
            raise OSError("DPAPI is only available on Windows")

        in_blob, in_buf = SecureData._bytes_to_blob(encrypted_bytes)
        out_blob = SecureData.DATA_BLOB()

        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            SecureData.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob)
        )
        if not ok:
            raise ctypes.WinError()

        try:
            return SecureData._blob_to_bytes(out_blob)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)

    @staticmethod
    def encrypt_password(password):
        if not password:
            return ""
        try:
            raw = password.encode("utf-8")
            enc = SecureData._dpapi_protect(raw)
            return SecureData.DPAPI_PREFIX + base64.b64encode(enc).decode("ascii")
        except:
            # DPAPI 실패 시 레거시 포맷으로 폴백 (기존 데이터 호환)
            try:
                return base64.b64encode(password.encode("utf-8")).decode("ascii")
            except:
                return password
    
    @staticmethod
    def decrypt_password(encrypted):
        if not encrypted:
            return ""
        try:
            if encrypted.startswith(SecureData.DPAPI_PREFIX):
                payload = encrypted[len(SecureData.DPAPI_PREFIX):]
                dec = SecureData._dpapi_unprotect(base64.b64decode(payload.encode("ascii")))
                return dec.decode("utf-8")

            # 레거시 Base64 데이터 자동 호환
            return base64.b64decode(encrypted.encode("ascii")).decode("utf-8")
        except:
            return encrypted

# ========== [폴더 그룹] ==========
class FolderGroup:
    """폴더 그룹 관리"""
    def __init__(self, name):
        self.name = name
        self.color = "#1976d2"
    
    def to_dict(self):
        return {"name": self.name, "color": self.color}
    
    @staticmethod
    def from_dict(data):
        f = FolderGroup(data.get("name", "Untitled"))
        f.color = data.get("color", "#1976d2")
        return f

# ========== [매크로 값 파싱] ==========
def parse_macro_value(action, value):
    """매크로 값 파싱 - 액션별 처리"""
    parts = value.split(':')
    
    if "문구" in action:
        try:
            return {
                'target': parts[0],
                'timeout': int(parts[1]) if len(parts) > 1 else 10,
                'error_msg': parts[2] if len(parts) > 2 else f"에러: {parts[0]}"
            }
        except (ValueError, IndexError):
            return {'target': parts[0], 'timeout': 10, 'error_msg': f"에러: {parts[0]}"}
    
    elif "대기" in action:
        try:
            return {'delay': float(parts[0])}
        except (ValueError, IndexError):
            return {'delay': 1.0}
    else:
        return {'value': value}

def normalize_pause_seconds(value, default=1):
    """TTL pause용 정수 초 값으로 정규화"""
    try:
        seconds = int(float(str(value).strip()))
        return max(0, seconds)
    except (ValueError, TypeError):
        return default

def format_pause_display_value(value):
    """UI/JSON 표시용 pause 값 정규화 (예: 5.0 -> 5)"""
    try:
        n = float(str(value).strip())
        if n.is_integer():
            return str(int(n))
        return str(n)
    except (ValueError, TypeError):
        return str(value)

def read_text_with_fallback(file_path):
    """텍스트 파일 인코딩을 자동 감지해 읽기"""
    with open(file_path, "rb") as f:
        raw = f.read()

    # UTF-8 계열을 먼저 시도해 한글 깨짐(모지바케) 가능성을 줄인다.
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", errors="replace")

# ========== [TTL 파일 파싱 - 강화버전] ==========
def parse_ttl_file(file_path):
    """
    TeraTerm 매크로 파일(.ttl)을 파싱하여 단계별 액션으로 변환
    다양한 형식과 스타일을 자동으로 인식
    """
    content = read_text_with_fallback(file_path)
    
    steps = []
    
    # ========== [1단계] 주석 제거 ==========
    lines = []
    for line in content.split('\n'):
        if ';' in line:
            line = line[:line.index(';')]
        if '//' in line:
            line = line[:line.index('//')]
        line = line.rstrip()
        if line: 
            lines.append(line)
    
    # ========== [2단계] 여러 줄 명령 결합 ==========
    combined_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while (line.count("'") % 2 == 1 or line.count('"') % 2 == 1) and i + 1 < len(lines):
            i += 1
            line += ' ' + lines[i]
        combined_lines.append(line)
        i += 1
    
    # ========== [3단계] 명령 파싱 ==========
    i = 0
    context = {
        'last_timeout': 10,
        'last_wait_target': '',
        'in_if_block': False
    }
    
    while i < len(combined_lines):
        line = combined_lines[i].strip().lower()
        original_line = combined_lines[i]
        
        if re.match(r'^sendln\s*', line):
            match = re.search(r"sendln\s+['\"`]([^'\"`]*)['\"`]", original_line, re.IGNORECASE)
            if match:
                value = match.group(1)
                value = value.replace("''", "'").replace('""', '"').replace("\\\\", "\\")
                steps.append({'action': '입력 (sendln)', 'value': value})
            else:
                steps.append({'action': '입력 (sendln)', 'value': ''})
            i += 1
        
        elif re.match(r'^pause\s+', line):
            match = re.search(r'pause\s+([\d.]+)', line)
            if match:
                delay = float(match.group(1))
                steps.append({'action': '대기 (pause)', 'value': format_pause_display_value(delay)})
            i += 1
        
        elif re.match(r'^timeout\s*=', line):
            match = re.search(r'timeout\s*=\s*([\d]+)', line)
            if match:
                context['last_timeout'] = int(match.group(1))
            i += 1
        
        elif re.match(r'^wait\s+', line) and 'goto' not in line:
            phrases = re.findall(r"['\"`]([^'\"`]*)['\"`]", original_line)
            if phrases:
                target = phrases[0]
                context['last_wait_target'] = target
                steps.append({
                    'action': '문구 대기 (wait)',
                    'value': f"{target}:{context['last_timeout']}"
                })
            i += 1
        
        elif re.match(r'^if\s+result', line):
            if 'goto loop' in line.lower():
                steps.append({
                    'action': '문구 확인 후 진행',
                    'value': f"{context['last_wait_target']}:{context['last_timeout']}"
                })
            elif 'messagebox' in line.lower() or (i+1 < len(combined_lines) and 'messagebox' in combined_lines[i+1].lower()):
                error_msg = "에러 발생"
                search_lines = [original_line]
                if i+1 < len(combined_lines):
                    search_lines.append(combined_lines[i+1])
                for search_line in search_lines:
                    msg_match = re.search(r"messagebox\s+['\"`]([^'\"`]*)['\"`]", search_line, re.IGNORECASE)
                    if msg_match:
                        error_msg = msg_match.group(1)
                        break
                steps.append({
                    'action': '문구 확인 후 종료',
                    'value': f"{context['last_wait_target']}:{context['last_timeout']}:{error_msg}"
                })
            i += 1
        
        elif 'goto loop' in line.lower() and 'if' not in line:
            steps.append({'action': '처음으로 (goto loop)', 'value': ''})
            i += 1
        
        elif 'clearscreen' in line:
            steps.append({'action': '화면 청소 (cls)', 'value': ''})
            i += 1
        
        elif 'flushrecv' in line:
            i += 1
        
        elif 'connect' in line:
            match = re.search(r"connect\s+['\"`]([^'\"`]*)['\"`]", original_line, re.IGNORECASE)
            if match:
                conn_info = match.group(1)
                steps.append({'action': '입력 (sendln)', 'value': f"[연결: {conn_info}]"})
            i += 1
        
        elif any(cmd in line for cmd in ['writeln', 'write', 'outputln']):
            match = re.search(r"(?:writeln|write|outputln)\s+['\"`]([^'\"`]*)['\"`]", original_line, re.IGNORECASE)
            if match:
                steps.append({'action': '입력 (sendln)', 'value': match.group(1)})
            i += 1
        else:
            raw_line = original_line.strip()
            if raw_line:
                steps.append({'action': '원문 TTL 1줄', 'value': raw_line})
            i += 1
    
    return steps

# ========== [SessionRow 클래스] ==========
class SessionRow:
    """개별 세션 관리"""
    def __init__(self, master, app_instance, data=None):
        self.app = app_instance
        self.data = data if data else {}
        defaults = {
            'name': '', 'type': 'S', 'ip': '', 'port': '22', 
            'baud_rate': '115200', 'user': '', 'pw': '', 
            'ru_cmd': '', 'ru_pw': '', 'final_cmd': '', 
            'ssh_interval': '2.0', 'moba_interval': '3.0',
            'timestamp_enabled': False,
            'col_positions': {},  
            'macro_steps': [], 'folder': None
        }
        for key, val in defaults.items():
            if key not in self.data: 
                self.data[key] = val
        
        # JSON 키 "null" 방지를 위해 None을 "__ROOT__" 문자열로 맵핑
        if 'col' in self.data and isinstance(self.data['col'], int):
            self.data['col_positions'] = {"__ROOT__": self.data['col']}
            del self.data['col']
        
        self.outer_frame = None
        self.frame = None
        self.status_label = None
        self.is_selected = False
        self._last_status_state = (None, None)

    def _run_on_ui_thread(self, callback, *args, **kwargs):
        """UI 스레드에서 안전하게 콜백 실행"""
        if threading.current_thread() is threading.main_thread():
            return callback(*args, **kwargs)
        return self.app.root.after(0, lambda: callback(*args, **kwargs))

    def _show_message(self, level, title, message):
        """백그라운드 스레드에서도 안전한 메시지박스 표시"""
        def _show():
            if level == "info":
                messagebox.showinfo(title, message)
            elif level == "warning":
                messagebox.showwarning(title, message)
            else:
                messagebox.showerror(title, message)
        self._run_on_ui_thread(_show)

    def get_col(self, folder=None):
        """현재 폴더의 컬럼 위치 반환"""
        f_key = folder if folder else "__ROOT__"
        positions = self.data.get('col_positions', {})
        return positions.get(f_key, 0)
    
    def set_col(self, col, folder=None):
        """현재 폴더의 컬럼 위치 저장"""
        f_key = folder if folder else "__ROOT__"
        if 'col_positions' not in self.data:
            self.data['col_positions'] = {}
        self.data['col_positions'][f_key] = col

    def create_widget(self, master):
        """세션 카드 생성"""
        if self.outer_frame and self.outer_frame.winfo_exists():
            # self.update_ui_elements() is called from refresh_grid
            return self.outer_frame

        self.outer_frame = tk.Frame(master, bg="#f5f7f9", padx=6, pady=6)
        self.frame = tk.Frame(self.outer_frame, bd=0, bg="#ffffff", highlightthickness=1, highlightbackground="#cfd8dc")
        self.frame.pack(fill='both', expand=True)
        
        # 좌측 색상 바
        self._color_bar = tk.Frame(self.frame, width=5)
        self._color_bar.pack(side='left', fill='y')
        
        content = tk.Frame(self.frame, bg="#ffffff", padx=10, pady=8)
        content.pack(side='left', fill='both', expand=True)
        
        header = tk.Frame(content, bg="#ffffff")
        header.pack(fill='x', side='top')
        self._selected_badge = tk.Label(
            header,
            text="SELECTED",
            font=('Segoe UI', 7, 'bold'),
            bg="#0056d6",
            fg="white",
            padx=4,
            pady=1
        )
        btn_box = tk.Frame(header, bg="#ffffff")
        btn_box.pack(side='right')
        btn_s = {"font": ('Arial', 8), "bd": 0, "bg": "#ffffff", "cursor": "hand2"}
        
        tk.Button(btn_box, text="🚀", command=self.open_step_automation, fg="#673ab7", **btn_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="⚡", command=self.run_ping, fg="#ffa000", **btn_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="⚙", command=self.open_config, fg="#90a4ae", **btn_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="⧉", command=self.copy_session, fg="#4caf50", **btn_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="×", command=self.delete_self, fg="#f44336", **btn_s).pack(side='left', padx=2)

        self.lbl_name = tk.Label(header, font=('Segoe UI', 10, 'bold'), bg="#ffffff", fg="#263238", anchor='w', cursor="fleur")
        self.lbl_name.pack(side='left', fill='x', expand=True)
        self.lbl_name.bind("<Button-1>", self.on_drag_start)
        self.lbl_name.bind("<B1-Motion>", self.on_drag_motion)
        self.lbl_name.bind("<ButtonRelease-1>", self.on_drag_release)
        
        self._lbl_info = tk.Label(content, font=('Consolas', 9), bg="#ffffff", fg="#78909c", anchor='w')
        self._lbl_info.pack(fill='x', pady=(2, 5))
        
        self.status_label = tk.Label(content, text="● Ready", font=('Consolas', 8), bg="#ffffff", fg="#4caf50", anchor='w')
        self.status_label.pack(fill='x', pady=(2, 5))
        
        self._btn_conn = tk.Button(content, command=self.run_connection, font=('Segoe UI', 7, 'bold'), bg="#f8f9fa", relief="flat", highlightthickness=1, pady=2)
        self._btn_conn.pack(fill='x')
        
        # --- 일괄 선택을 위한 이벤트 바인딩 ---
        def on_select_proxy(event):
            # 선택은 Ctrl 키를 누른 상태의 좌클릭에서만 처리한다.
            is_ctrl = self.app.is_ctrl_click(event)
            if is_ctrl:
                self.app.handle_selection(self, event, ctrl_override=True)
            return "break"

        def bind_selection_recursive(widget):
            # 드래그 핸들 라벨과 액션 버튼은 전용 동작을 유지한다.
            if widget is self.lbl_name or isinstance(widget, tk.Button):
                return
            widget.bind("<Button-1>", on_select_proxy, add="+")
            for child in widget.winfo_children():
                bind_selection_recursive(child)

        # 카드 전체에 선택 바인딩을 일관되게 적용
        bind_selection_recursive(self.outer_frame)
        
        # 최초 생성 시 데이터 주입
        self.update_ui_elements()
        return self.outer_frame

    def update_ui_elements(self):
        """저장된 데이터를 바탕으로 현재 위젯의 텍스트와 색상을 갱신"""
        if not self.outer_frame: return
        t = self.data.get('type', 'S')
        color = {"S": "#1976d2", "R": "#9c27b0", "M": "#2e7d32", "P": "#fbc02d", "C": "#e64a19"}.get(t, "#455a64")
        
        self._color_bar.config(bg=color)
        self.lbl_name.config(text=self.data.get('name') or 'New')
        
        info = self.data.get('ip', '0.0.0.0')
        if t == "S": 
            info += f":{self.data.get('port', '22')}"
        elif t == "C": 
            info = f"COM{info}"
        self._lbl_info.config(text=info)
        
        conn_text = CONNECT_TEXT_MAP.get(t, "CONNECT")
        self._btn_conn.config(text=conn_text, fg=color, highlightbackground=color)

    def update_selection_visual(self):
        """선택 상태에 따라 카드 외곽선 스타일 변경"""
        if not self.frame or not self.frame.winfo_exists():
            return
        if self.is_selected:
            # 선택 상태를 더 눈에 띄게 강조
            self.frame.config(
                highlightbackground="#0056d6",
                highlightcolor="#0056d6",
                highlightthickness=4,
                bg="#eef5ff"
            )
            if self.outer_frame and self.outer_frame.winfo_exists():
                self.outer_frame.config(bg="#d6e7ff")
            if hasattr(self, "_selected_badge") and self._selected_badge.winfo_exists():
                self._selected_badge.pack(side='left', padx=(0, 6))
        else:
            self.frame.config(
                highlightbackground="#cfd8dc",
                highlightcolor="#cfd8dc",
                highlightthickness=1,
                bg="#ffffff"
            )
            if self.outer_frame and self.outer_frame.winfo_exists():
                self.outer_frame.config(bg="#f5f7f9")
            if hasattr(self, "_selected_badge") and self._selected_badge.winfo_exists():
                self._selected_badge.pack_forget()

    def update_status(self, status, color="#4caf50"):
        """안전한 상태 업데이트"""
        if (status, color) == self._last_status_state:
            return
        self._last_status_state = (status, color)

        def _apply():
            if self.status_label and self.status_label.winfo_exists():
                try:
                    self.status_label.config(text=f"● {status}", fg=color)
                except Exception:
                    pass

        self._run_on_ui_thread(_apply)

    def run_ping(self):
        """Ping 테스트"""
        t = self.data.get('ip', '')
        if not t or self.data['type'] == 'C': 
            return
        
        def ping_thread():
            try:
                self.update_status("Ping...", "#ffa000")
                res = subprocess.run(['ping', '-n', '1', '-w', '1000', t], 
                                   stdout=subprocess.PIPE, 
                                   stderr=subprocess.PIPE,
                                   shell=False, 
                                   text=True, 
                                   timeout=5)
                result = "SUCCESS ✓" if res.returncode == 0 else "FAIL ✗"
                self.update_status("Ready", "#4caf50")
                self._show_message("info", "Ping Test", f"Target: {t}\nResult: {result}")
            except subprocess.TimeoutExpired:
                self.update_status("Ready", "#4caf50")
                self._show_message("warning", "Ping Test", "Timeout")
            except Exception as e: 
                self.update_status("Ready", "#4caf50")
                self._show_message("error", "Error", str(e))
        
        thread = threading.Thread(target=ping_thread, daemon=True)
        thread.start()

    def open_step_automation(self):
        """매크로 자동화 창"""
        win = tk.Toplevel(self.app.root)
        win.title(f"🚀 {self.data['name']} Macro Studio")
        win.geometry("750x800")
        
        lib_f = tk.Frame(win, bg="#eceff1", pady=10)
        lib_f.pack(fill='x')
        
        def save_standalone_ttl():
            name = simpledialog.askstring("저장", "TTL 파일명:")
            if not name: 
                return
            cur_steps = [{"action": r[1].get(), "value": r[2].get()} for r in step_rows]
            
            try:
                with open(os.path.join(MACRO_DIR, f"{name}.json"), 'w', encoding='utf-8') as f: 
                    json.dump(cur_steps, f, indent=4, ensure_ascii=False)
                
                with open(os.path.join(MACRO_DIR, f"{name}.ttl"), 'w', encoding='cp949', errors='replace') as f:
                    f.write("; Standalone TeraTerm Macro\n:loop_start\n")
                    for s in cur_steps:
                        act, val = s['action'], s['value']
                        parsed = parse_macro_value(act, val)
                        
                        if "원문 TTL" in act:
                            raw_line = val.strip()
                            if raw_line:
                                f.write(f"{raw_line}\n")
                        elif "입력" in act:
                            escaped_val = self._escape_teraterm_string(val)
                            f.write(f"sendln '{escaped_val}'\n")
                        elif "대기 (pause)" in act:
                            delay = normalize_pause_seconds(parsed.get('delay', 1), default=1)
                            f.write(f"pause {delay}\n")
                        elif "화면 청소" in act:
                            f.write("clearscreen 0\nflushrecv\n")
                        elif "문구 대기" in act:
                            target = self._escape_teraterm_string(parsed['target'])
                            timeout = parsed['timeout']
                            f.write(f"flushrecv\ntimeout = {timeout}\nwait '{target}'\n")
                        elif "확인 후 진행" in act:
                            target = self._escape_teraterm_string(parsed['target'])
                            timeout = parsed['timeout']
                            f.write(f"flushrecv\ntimeout = {timeout}\nwait '{target}'\nif result=0 goto loop_start\n")
                        elif "확인 후 종료" in act:
                            target = self._escape_teraterm_string(parsed['target'])
                            timeout = parsed['timeout']
                            error_msg = self._escape_teraterm_string(parsed['error_msg'])
                            f.write(f"flushrecv\ntimeout = {timeout}\nwait '{target}'\nif result=1 then\n messagebox '{error_msg}' 'Error'\nendif\n")
                        elif "처음으로" in act:
                            f.write("goto loop_start\n")
                    f.write("end\n")
                messagebox.showinfo("성공", f"{name}.ttl 저장 완료\n\nJSON과 TTL 모두 저장되었습니다")
            except Exception as e:
                messagebox.showerror("Error", f"저장 실패: {str(e)}")

        def load_standalone_ttl():
            files = [f for f in os.listdir(MACRO_DIR) if f.endswith(('.json', '.ttl'))]
            if not files: 
                messagebox.showinfo("알림", "저장된 매크로가 없습니다")
                return
            
            l_win = tk.Toplevel(win)
            l_win.title("매크로 로드")
            l_win.geometry("500x500")
            
            list_frame = tk.Frame(l_win)
            list_frame.pack(padx=10, pady=10, fill='both', expand=True)
            
            tk.Label(list_frame, text="📁 매크로 파일 목록", font=('', 9, 'bold')).pack(anchor='w')
            
            lb = tk.Listbox(list_frame, width=50, height=10, font=('Consolas', 9))
            sb = tk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
            lb.config(yscrollcommand=sb.set)
            lb.pack(side='left', fill='both', expand=True)
            sb.pack(side='right', fill='y')
            
            for f in sorted(files):
                file_type = "JSON" if f.endswith('.json') else "TTL"
                lb.insert('end', f"{f:<35} [{file_type}]")
            
            preview_frame = tk.Frame(l_win)
            preview_frame.pack(padx=10, pady=(0, 10), fill='both', expand=True)
            
            tk.Label(preview_frame, text="📋 미리보기", font=('', 9, 'bold')).pack(anchor='w')
            
            preview_text = tk.Text(preview_frame, height=5, width=50, font=('Consolas', 8), bg="#f9f9f9")
            preview_text.pack(fill='both', expand=True)
            preview_text.config(state='disabled')
            
            def show_preview(event=None):
                if not lb.curselection():
                    return
                selected = lb.get(lb.curselection()).split('[')[0].strip()
                file_path = os.path.join(MACRO_DIR, selected)
                try:
                    preview_text.config(state='normal')
                    preview_text.delete('1.0', 'end')
                    if selected.endswith('.json'):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            for i, step in enumerate(data[:5], 1):
                                preview_text.insert('end', f"{i}. [{step.get('action', 'N/A')}]\n   {step.get('value', '')}\n\n")
                    else:
                        lines = read_text_with_fallback(file_path).splitlines()
                        for line in lines[:10]:
                            preview_text.insert('end', f"{line}\n")
                    preview_text.config(state='disabled')
                except Exception as e:
                    preview_text.config(state='normal')
                    preview_text.insert('end', f"미리보기 실패: {str(e)}")
                    preview_text.config(state='disabled')
            
            lb.bind('<<ListboxSelect>>', show_preview)
            
            btn_frame = tk.Frame(l_win)
            btn_frame.pack(fill='x', padx=10, pady=10)
            
            def do_load():
                if not lb.curselection(): 
                    messagebox.showwarning("경고", "파일을 선택해주세요")
                    return
                
                selected_file = lb.get(lb.curselection()).split('[')[0].strip()
                file_path = os.path.join(MACRO_DIR, selected_file)
                
                try:
                    steps = []
                    if selected_file.endswith('.json'):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            steps = json.load(f)
                    elif selected_file.endswith('.ttl'):
                        steps = parse_ttl_file(file_path)
                        if not steps:
                            messagebox.showwarning("경고", "TTL 파일을 파싱할 수 없습니다")
                            return
                    
                    for r in step_rows[:]: 
                        r[0].destroy()
                        step_rows.remove(r)
                    
                    for s in steps: 
                        add_step_ui(s.get('action', ''), s.get('value', ''))
                    
                    scroll_f.update_idletasks()
                    canvas.config(scrollregion=canvas.bbox("all"))
                    
                    messagebox.showinfo("성공", f"{selected_file} 로드 완료\n({len(steps)} 단계)")
                    l_win.destroy()
                
                except Exception as e:
                    messagebox.showerror("Error", f"로드 실패: {str(e)}")
            
            tk.Button(btn_frame, text="✓ 적용", command=do_load, bg="#1976d2", fg="white", width=20, pady=5).pack(side='left', padx=5)
            tk.Button(btn_frame, text="✕ 취소", command=l_win.destroy, bg="#999", fg="white", width=10, pady=5).pack(side='left')
            
            l_win.update_idletasks()

        tk.Button(lib_f, text="💾 TTL 파일로 저장", command=save_standalone_ttl, bg="#2e7d32", fg="white", padx=15, pady=5).pack(side='left', padx=20)
        tk.Button(lib_f, text="📂 불러오기", command=load_standalone_ttl, bg="#455a64", fg="white", padx=15, pady=5).pack(side='left')

        main_f = tk.Frame(win, padx=20, pady=10)
        main_f.pack(fill='both', expand=True)
        canvas = tk.Canvas(main_f, highlightthickness=0)
        sb = tk.Scrollbar(main_f, orient="vertical", command=canvas.yview)
        scroll_f = tk.Frame(canvas)
        cw = canvas.create_window((0, 0), window=scroll_f, anchor="nw", tags="f")
        canvas.config(yscrollcommand=sb.set)
        def on_macro_mousewheel(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")

        # bind_all 대신 로컬 바인딩으로 윈도우 간 스크롤 이벤트 충돌을 방지
        canvas.bind("<MouseWheel>", on_macro_mousewheel)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfig(cw, width=e.width), canvas.config(scrollregion=canvas.bbox("all"))))
        canvas.bind("<Enter>", lambda e: canvas.focus_set())
        canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        
        step_rows = []
        action_options = ["입력 (sendln)", "문구 대기 (wait)", "문구 확인 후 진행", "문구 확인 후 종료", "대기 (pause)", "화면 청소 (cls)", "처음으로 (goto loop)", "원문 TTL 1줄"]
        
        def add_step_ui(action="", value=""):
            row = tk.Frame(scroll_f, pady=5, relief="solid", bd=1, bg="#fafafa")
            row.pack(fill='x')
            lbl = tk.Label(row, text=f"Step {len(step_rows)+1}:", width=7, anchor='w', bg="#f5f5f5")
            lbl.pack(side='left', padx=5)
            cb = ttk.Combobox(row, values=action_options, width=18, state="readonly")
            cb.set(action if action else action_options[0])
            cb.pack(side='left', padx=2)
            ent = tk.Entry(row, width=32)
            ent.insert(0, value)
            ent.pack(side='left', padx=5, fill='x', expand=True)
            
            def move(d):
                idx = next(i for i, r in enumerate(step_rows) if r[0] == row)
                if 0 <= idx + d < len(step_rows):
                    step_rows[idx], step_rows[idx+d] = step_rows[idx+d], step_rows[idx]
                    for r in step_rows: 
                        r[0].pack_forget()
                    for r in step_rows: 
                        r[0].pack(fill='x')
                    reorder()
            
            tk.Button(row, text="▲", command=lambda: move(-1), font=('', 7), width=3).pack(side='left', padx=1)
            tk.Button(row, text="▼", command=lambda: move(1), font=('', 7), width=3).pack(side='left', padx=1)
            
            def rm():
                row.destroy()
                step_rows[:] = [r for r in step_rows if r[0] != row]
                reorder()
            
            tk.Button(row, text="X", command=rm, fg="red", bd=0, font=('', 8), width=3).pack(side='right', padx=2)
            step_rows.append([row, cb, ent])

        def reorder():
            for i, r in enumerate(step_rows):
                for c in r[0].winfo_children():
                    if isinstance(c, tk.Label) and "Step" in c.cget("text"): 
                        c.config(text=f"Step {i+1}:")

        for s in self.data.get('macro_steps', []): 
            add_step_ui(s['action'], s['value'])
        
        tk.Button(win, text="+ 단계 추가", command=lambda: add_step_ui(), bg="#1976d2", fg="white", pady=8).pack(fill='x', padx=20, pady=5)

        def collect_steps_from_ui():
            return [{"action": r[1].get(), "value": r[2].get()} for r in step_rows]

        def save_macro_steps(show_message=True):
            self.data.update({'macro_steps': collect_steps_from_ui()})
            self.app.request_save()
            if show_message:
                messagebox.showinfo("저장 완료", f"매크로 단계 {len(self.data.get('macro_steps', []))}개 저장됨")

        def run_macro_steps():
            # 실행 전 현재 편집본을 저장해 실행/저장 상태 불일치를 막는다.
            save_macro_steps(show_message=False)
            self.run_connection(use_steps=True)
            win.destroy()

        action_btn_frame = tk.Frame(win)
        action_btn_frame.pack(fill='x', padx=20, pady=10)

        tk.Button(
            action_btn_frame,
            text="저장",
            command=save_macro_steps,
            bg="#2e7d32",
            fg="white",
            pady=10
        ).pack(side='left', fill='x', expand=True, padx=(0, 6))

        tk.Button(
            action_btn_frame,
            text="실행",
            command=run_macro_steps,
            bg="#673ab7",
            fg="white",
            pady=10
        ).pack(side='left', fill='x', expand=True, padx=(6, 0))
        
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.config(yscrollcommand=sb.set)
        scroll_f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    def _escape_teraterm_string(self, s):
        """TeraTerm 문자열 이스케이프"""
        return s.replace("'", "''").replace('\\', '\\\\')

    def _sanitize_teraterm_log(self, src_path):
        """TeraTerm/minicom 로그에서 ANSI 제어문자를 제거한 *_clean.log 파일 생성."""
        if not src_path or not os.path.exists(src_path):
            return

        with open(src_path, "rb") as f:
            raw = f.read()

        decoded = None
        for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
            try:
                decoded = raw.decode(enc)
                break
            except Exception:
                continue
        if decoded is None:
            decoded = raw.decode("utf-8", errors="replace")

        # OSC sequence: ESC ] ... BEL/ESC\
        decoded = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", decoded)
        # CSI sequence: ESC [ ... command
        decoded = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", decoded)
        # ESC ( ... / ESC ) ... charset shift sequence
        decoded = re.sub(r"\x1b[\(\)][0-9A-Za-z]", "", decoded)
        # Single ESC leftovers
        decoded = decoded.replace("\x1b", "")
        # VT100 제어문자(SO/SI 포함) 제거. 개행/탭/CR은 유지.
        decoded = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", decoded)
        # Common mojibake cleanup
        decoded = decoded.replace("ÂÂ°C", "°C").replace("Â°C", "°C")

        base, ext = os.path.splitext(src_path)
        clean_path = f"{base}_clean{ext or '.log'}"
        
        if bool(self.data.get("timestamp_enabled", False)):
            lines = decoded.splitlines()
            old_lines = []
            if os.path.exists(clean_path):
                with open(clean_path, "r", encoding="utf-8", errors="replace") as old_f:
                    old_lines = old_f.read().splitlines()

            ts_pattern = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s?(.*)$")
            rebuilt = []
            preserved_count = min(len(lines), len(old_lines))

            # 기존 라인의 타임스탬프는 유지하고, 새 라인에만 현재 시각을 부여한다.
            for idx in range(preserved_count):
                line = lines[idx]
                old_line = old_lines[idx]
                m = ts_pattern.match(old_line)
                if m and line.strip():
                    rebuilt.append(f"[{m.group(1)}] {line}")
                else:
                    rebuilt.append(line)

            for line in lines[preserved_count:]:
                if line.strip():
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    rebuilt.append(f"[{now_str}] {line}")
                else:
                    rebuilt.append(line)

            decoded = "\n".join(rebuilt)

        with open(clean_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(decoded)

    def open_config(self):
        """세션 설정 창"""
        win = tk.Toplevel(self.app.root)
        win.title("세션 설정")
        win.geometry("480x950")
        
        tk.Label(win, text="폴더 선택", font=('', 8, 'bold'), fg="#546e7a").pack(anchor='w', padx=20, pady=(10, 3))
        
        folder_var = tk.StringVar(value=self.data.get('folder') or "폴더 없음")
        folder_options = ["폴더 없음"] + sorted(self.app.folders.keys())
        
        folder_menu = ttk.Combobox(win, textvariable=folder_var, values=folder_options, state="readonly", width=50)
        folder_menu.pack(padx=20, pady=2)
        
        flds = [("이름", "name"), ("Moba 간격", "moba_interval"), ("IP / COM / 경로", "ip"), ("SSH 포트", "port"), 
                ("Baud Rate", "baud_rate"), ("ID", "user"), ("PW", "pw"), ("RU 명령", "ru_cmd"), 
                ("RU PW", "ru_pw"), ("SSH 간격", "ssh_interval"), ("추가 명령", "final_cmd")]
        ents = {}
        v_type = tk.StringVar(value=TYPE_MAP.get(self.data['type']))
        tk.OptionMenu(win, v_type, *TYPE_MAP.values(), command=lambda _: upd()).pack(pady=10)
        f = tk.Frame(win)
        f.pack(padx=20, pady=5)
        
        # '찾기' 버튼을 참조하기 위한 변수
        ip_browse_btn = None

        def upd():
            nonlocal ip_browse_btn # 외부 변수 참조
            ct = INV_TYPE_MAP.get(v_type.get())
            for k, e in ents.items():
                dis = (ct == 'C' and k in ['port','user','pw','ru_cmd','ru_pw','ssh_interval','moba_interval']) or \
                      (ct == 'M' and k not in ['name','moba_interval']) or \
                      (ct == 'S' and k in ['baud_rate','moba_interval']) or \
                      (ct in ['R','P'] and k in ['port','baud_rate','ru_cmd','ru_pw','ssh_interval','moba_interval'])
                e.config(state='disabled', bg='#eeeeee') if dis else e.config(state='normal', bg='white')
            
            # '찾기' 버튼 활성화/비활성화 로직
            if ip_browse_btn:
                if ct == 'P':
                    ip_browse_btn.config(state='normal')
                else:
                    ip_browse_btn.config(state='disabled')
        
        for l, k in flds:
            tk.Label(f, text=l, font=('', 8, 'bold'), fg="#546e7a").pack(anchor='w', pady=(3,0))
            val = self.data.get(k, '')
            
            if "pw" in k:
                pw_row = tk.Frame(f)
                pw_row.pack(pady=2, fill='x')
                
                ent = tk.Entry(pw_row, width=46, bd=1, relief="solid", show="*")
                ent.insert(0, str(val))
                ent.pack(side='left', padx=0, fill='x', expand=True)
                
                def toggle_password_visibility(entry_widget, toggle_btn):
                    if entry_widget.cget('show') == '*':
                        entry_widget.config(show='')
                        toggle_btn.config(text='👁️‍🗨️')
                    else:
                        entry_widget.config(show='*')
                        toggle_btn.config(text='👁️')
                
                toggle_btn = tk.Button(
                    pw_row, text='👁️', width=4, bd=1, relief="solid",
                    command=lambda e=ent: toggle_password_visibility(e, toggle_btn)
                )
                toggle_btn.pack(side='left', padx=2)
                ents[k] = ent
            elif k == 'ip':
                ip_row = tk.Frame(f)
                ip_row.pack(pady=2, fill='x')
                
                ent = tk.Entry(ip_row, width=46, bd=1, relief="solid")
                ent.insert(0, str(val))
                ent.pack(side='left', padx=0, fill='x', expand=True)
                
                def browse_file(entry_widget):
                    p = filedialog.askopenfilename(
                        title="실행 파일 선택",
                        filetypes=(("Executable files", "*.exe"), ("Python files", "*.py"), ("All files", "*.*"))
                    )
                    if p:
                        entry_widget.delete(0, 'end')
                        entry_widget.insert(0, p)

                ip_browse_btn = tk.Button(ip_row, text="찾기", width=4, bd=1, relief="solid", command=lambda e=ent: browse_file(e))
                ip_browse_btn.pack(side='left', padx=2)
                
                ents[k] = ent
            else:
                ent = tk.Entry(f, width=52, bd=1, relief="solid")
                ent.insert(0, str(val))
                ent.pack(pady=2)
                ents[k] = ent

        var_timestamp_enabled = tk.BooleanVar(value=bool(self.data.get("timestamp_enabled", False)))
        opt_row = tk.Frame(win)
        opt_row.pack(fill='x', padx=20, pady=(6, 2))
        tk.Checkbutton(
            opt_row,
            text="타임스탬프 기능 (clean 로그 라인에 시간표시)",
            variable=var_timestamp_enabled,
            anchor='w'
        ).pack(side='left')
        
        upd()
        
        def save_config():
            for k, e in ents.items():
                self.data[k] = e.get()
            self.data['type'] = INV_TYPE_MAP.get(v_type.get())
            self.data['timestamp_enabled'] = bool(var_timestamp_enabled.get())
            
            folder_val = folder_var.get()
            old_folder = self.data.get('folder')
            new_folder = None if folder_val == "폴더 없음" else folder_val
            
            if old_folder != new_folder:
                self.data['folder'] = new_folder
            
            self.app.request_save()
            self.app._cache_valid = False
            self.app.refresh_grid()
            win.destroy()
        
        tk.Button(win, text="저장", command=save_config, bg="#1976d2", fg="white", font=('', 9, 'bold'), width=20, pady=10).pack(pady=15)

    def run_connection(self, use_steps=False):
        """연결 실행"""
        d = self.data
        tt_path = self.app.global_settings.get("teraterm_path")
        moba_path = self.app.global_settings.get("moba_path")
        log_dir = self.app.resolve_log_dir(create=True)
        if not log_dir:
            self._show_message("error", "Error", "로그 디렉토리 생성 실패")
            return
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"Log_{d['name']}_{ts}.log")
        user = d.get('user', '')
        pw = d.get('pw', '')
        ru_cmd = d.get('ru_cmd', '')
        ru_pw = d.get('ru_pw', '')

        try:
            self.update_status("Connecting...", "#ffa000")
            
            if d['type'] in ["S", "C"]:
                macro_path = os.path.join(tempfile.gettempdir(), f"at_macro_{uuid.uuid4().hex}.ttl")
                with open(macro_path, 'w', encoding='cp949', errors='replace') as f:
                    f.write(f"logopen '{log_file}' 1 0\n")
                    
                    if d['type'] == "S":
                        ip_clean = d['ip'].strip()
                        conn_str = f"{ip_clean}:{d.get('port','22')} /ssh"
                        if user:
                            conn_str += f" /user={user}"
                        if pw:
                            conn_str += f' /passwd="{pw}" /auth=password'
                            
                        f.write(f"connect '{conn_str}'\n")
                        f.write("pause 2\n")
                    else:
                        com_port = re.sub(r'[^0-9]', '', d['ip'])
                        f.write(f"connect '/C={com_port} /BAUD={d.get('baud_rate','115200')}'\n")
                        f.write("pause 1\n")
                        f.write("sendln ''\n")
                        f.write("timeout = 3\n")
                        f.write("wait 'login:' 'user:' 'Username:' 'name:'\n")
                        if user:
                            f.write(f"if result>0 sendln '{user}'\n")
                        f.write("wait 'password:' 'Password:' 'pw:'\n")
                        if pw:
                            f.write(f"if result>0 sendln '{pw}'\n")
                    
                    if ru_cmd:
                        f.write("flushrecv\n")
                        f.write(f"sendln '{self._escape_teraterm_string(ru_cmd)}'\n")
                        if ru_pw:
                            f.write("timeout = 5\n")
                            f.write("wait 'password:' 'Password:' 'pw:' 'yes/no'\n")
                            f.write("if result=4 then\n")
                            f.write("  sendln 'yes'\n")
                            f.write("  wait 'password:' 'Password:' 'pw:'\n")
                            f.write("endif\n")
                            f.write(f"if result>0 sendln '{self._escape_teraterm_string(ru_pw)}'\n")
                        f.write("pause 1\n")
                    
                    f.write("pause 1\n")
                    
                    steps = d.get('macro_steps', []) if use_steps else []

                    # CONNECT/RU 단계 이후 추가 명령은 1회만 실행한다.
                    if d.get('final_cmd'):
                        for c in d.get('final_cmd', '').split(","):
                            cmd = c.strip()
                            if cmd:
                                f.write(f"sendln '{self._escape_teraterm_string(cmd)}'\n")
                                f.write("pause 2\n")
                    
                    # 반복은 사용자 매크로 단계만 대상으로 한다.
                    f.write(":macro_loop_start\n")
                    
                    for s in steps:
                        act, val = s['action'], s['value']
                        parsed = parse_macro_value(act, val)
                        
                        if "원문 TTL" in act:
                            raw_line = val.strip()
                            if raw_line:
                                f.write(f"{raw_line}\n")
                        elif "입력" in act:
                            escaped_val = self._escape_teraterm_string(val)
                            f.write(f"sendln '{escaped_val}'\n")
                        elif "대기 (pause)" in act:
                            delay = normalize_pause_seconds(parsed.get('delay', 1), default=1)
                            f.write(f"pause {delay}\n")
                        elif "화면 청소" in act:
                            f.write("clearscreen 0\nflushrecv\n")
                        elif "문구 대기" in act:
                            target = self._escape_teraterm_string(parsed['target'])
                            timeout = parsed['timeout']
                            f.write(f"flushrecv\ntimeout = {timeout}\nwait '{target}'\n")
                        elif "확인 후 진행" in act:
                            target = self._escape_teraterm_string(parsed['target'])
                            timeout = parsed['timeout']
                            f.write(f"flushrecv\ntimeout = {timeout}\nwait '{target}'\nif result=0 goto macro_loop_start\n")
                        elif "확인 후 종료" in act:
                            target = self._escape_teraterm_string(parsed['target'])
                            timeout = parsed['timeout']
                            error_msg = self._escape_teraterm_string(parsed['error_msg'])
                            f.write(f"flushrecv\ntimeout = {timeout}\nwait '{target}'\nif result=1 then\n messagebox '{error_msg}' 'Error'\nendif\n")
                        elif "처음으로" in act:
                            f.write("goto macro_loop_start\n")
                    
                    f.write("end\n")
                
                try:
                    proc = subprocess.Popen([tt_path, f"/M={macro_path}"], shell=False)

                    # 실행 중에도 clean 로그를 갱신하고, 종료 시 최종 정리한다.
                    def _post_clean():
                        while True:
                            try:
                                self._sanitize_teraterm_log(log_file)
                            except Exception:
                                pass
                            if proc.poll() is not None:
                                break
                            time.sleep(2.0)
                        try:
                            self._sanitize_teraterm_log(log_file)
                        except Exception:
                            pass

                    threading.Thread(target=_post_clean, daemon=True).start()
                    self.update_status("Connected", "#4caf50")
                except FileNotFoundError:
                    self.update_status("Error", "#f44336")
                    self._show_message("error", "Error", "TeraTerm을 찾을 수 없습니다")
            
            elif d['type'] == "M":
                interval = float(d.get('moba_interval', 3.0))
                bookmarks = [b.strip() for b in d['name'].split(",") if b.strip()]
                
                if not bookmarks:
                    self._show_message("warning", "Warning", "북마크가 없습니다")
                    return
                
                def moba_thread():
                    for i, bookmark in enumerate(bookmarks):
                        try:
                            subprocess.Popen([moba_path, "-bookmark", bookmark], shell=False)
                            self.update_status(f"Connected ({i+1}/{len(bookmarks)})", "#4caf50")
                            
                            if i < len(bookmarks) - 1:
                                time.sleep(interval)
                        except FileNotFoundError:
                            self.update_status("Error", "#f44336")
                            self._show_message("error", "Error", "MobaXterm을 찾을 수 없습니다")
                            break
                        except Exception as e:
                            self.update_status("Error", "#f44336")
                            self._show_message("error", "Error", f"북마크 실행 실패: {bookmark}\n{str(e)}")
                            break
                
                thread = threading.Thread(target=moba_thread, daemon=True)
                thread.start()
            
            elif d['type'] == "R":
                def rdp_thread():
                    try:
                        target = d.get("ip", "").strip().strip('"').strip("'")
                        rdp_user = d.get("user", "").strip()
                        rdp_pw = (pw or "").strip()

                        if not target:
                            self.update_status("Error", "#f44336")
                            self._show_message("warning", "RDP", "RDP 대상(IP/호스트)이 비어 있습니다")
                            return

                        # 세션별로 저장된 기존 TERMSRV 자격증명이 충돌할 수 있어 먼저 정리
                        try:
                            subprocess.run(
                                ['cmdkey', f'/delete:TERMSRV/{target}'],
                                shell=False,
                                capture_output=True,
                                timeout=10
                            )
                        except Exception:
                            pass

                        # ID/PW가 있으면 cmdkey에 등록하고, 없으면 자격증명 창을 그대로 사용
                        if rdp_user and rdp_pw:
                            # 사용자가 입력한 계정 문자열을 그대로 사용한다.
                            # (예: domain\user, user@domain, SOLID#thelastsun 등)
                            cred_user = rdp_user

                            cred_res = subprocess.run(
                                ['cmdkey', f'/generic:TERMSRV/{target}', f'/user:{cred_user}', f'/pass:{rdp_pw}'],
                                shell=False,
                                capture_output=True,
                                text=True,
                                timeout=10
                            )
                            if cred_res.returncode != 0:
                                err_msg = (cred_res.stderr or cred_res.stdout or "").strip()
                                self.update_status("Error", "#f44336")
                                self._show_message("error", "RDP", f"자격증명 등록 실패\n대상: {target}\n{err_msg}")
                                return

                        # 저장된 자격증명이 유효하면 즉시 로그인되도록 /prompt는 사용하지 않는다.
                        subprocess.Popen(['mstsc', f'/v:{target}'], shell=False)
                        self.update_status("Connected", "#4caf50")
                    except FileNotFoundError:
                        self.update_status("Error", "#f44336")
                        self._show_message("error", "Error", "cmdkey/mstsc를 찾을 수 없습니다")
                    except subprocess.TimeoutExpired:
                        self.update_status("Error", "#f44336")
                        self._show_message("error", "Error", "명령 실행 시간 초과")
                    except Exception as e:
                        self.update_status("Error", "#f44336")
                        self._show_message("error", "Error", str(e))
                
                thread = threading.Thread(target=rdp_thread, daemon=True)
                thread.start()
            
            elif d['type'] == "P":
                p = d['ip'].strip()
                if os.path.exists(p):
                    try:
                        if p.lower().endswith('.py'):
                            subprocess.Popen(['python', p], shell=False)
                        else:
                            subprocess.Popen([p], shell=False)
                        self.update_status("Running", "#4caf50")
                    except Exception as e:
                        self.update_status("Error", "#f44336")
                        self._show_message("error", "Error", str(e))
                else:
                    self.update_status("Error", "#f44336")
                    self._show_message("error", "Error", f"파일을 찾을 수 없습니다: {p}")
        
        except Exception as e:
            self.update_status("Error", "#f44336")
            self._show_message("error", "Error", f"연결 실패: {str(e)}")

    def on_drag_start(self, e): 
        # Ctrl+좌클릭은 선택 토글, 일반 좌클릭은 드래그 시작
        is_ctrl = self.app.is_ctrl_click(e)
        if is_ctrl:
            self.app.handle_selection(self, e, ctrl_override=True)
            return "break"

        self.outer_frame.config(bg="#90a4ae")
        self.app.dragging_item = self
        return "break"
    
    def on_drag_motion(self, e):
        mx = e.x_root - self.app.grid_frame.winfo_rootx()
        gw = self.app.grid_frame.winfo_width()
        if self.app.dragging_item and gw > 0:
            tc = max(0, min(int(mx // (gw / self.app.col_count)), self.app.col_count - 1))
            if self.get_col(self.app.current_folder) != tc: 
                self.set_col(tc, self.app.current_folder)
                self.app.refresh_grid_light()
    
    def on_drag_release(self, e):
        my = e.y_root - self.app.grid_frame.winfo_rooty()
        tc = self.get_col(self.app.current_folder)
        
        others = [s for s in self.app.sessions 
                 if s.data.get('folder') == self.app.current_folder 
                 and s.get_col(self.app.current_folder) == tc 
                 and s != self]
        
        idx = sum(1 for s in others if s.outer_frame and my > s.outer_frame.winfo_y() + (s.outer_frame.winfo_height()/2))
        
        if self in self.app.sessions: 
            self.app.sessions.remove(self)
        
        p, f = 0, 0
        for i, s in enumerate(self.app.sessions):
            if s.data.get('folder') == self.app.current_folder and s.get_col(self.app.current_folder) == tc:
                if f == idx: 
                    break
                f += 1
            p = i + 1
        
        self.app.sessions.insert(p, self)
        self.app.dragging_item = None
        self.outer_frame.config(bg="#f5f7f9")
        self.app.request_save()
        self.app.refresh_grid()
    
    def copy_session(self):
        """세션 복사"""
        n = copy.deepcopy(self.data)
        n['name'] += "_copy"
        self.app.sessions.append(SessionRow(None, self.app, n))
        self.app.request_save()
        self.app._cache_valid = False
        self.app.refresh_grid()
    
    def delete_self(self):
        """세션 삭제 (버그 픽스: 잔여 UI 프레임을 완전히 파괴하여 숨김 현상 방지)"""
        if messagebox.askyesno("삭제", "삭제?"):
            # 만약 선택된 세션 목록에 있었다면 제거
            if self in self.app.selected_sessions:
                self.app.selected_sessions.remove(self)
                self.app.update_bulk_action_ui()
            if self.outer_frame and self.outer_frame.winfo_exists():
                self.outer_frame.destroy() 
            self.app.sessions.remove(self)
            self.app.save_all()
            self.app._cache_valid = False
            self.app.refresh_grid()

# ========== [MainApp 클래스] ==========
class MainApp:
    """메인 애플리케이션"""
    def __init__(self, root):
        self.root = root
        self.root.title("DashStation v1.0")
        self.root.geometry("1200x900")
        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)
        
        self.sessions = []
        self.col_count = 4
        self.zone_names = {}
        self.global_settings = {}
        self.dragging_item = None
        self.selected_sessions = set()
        self.last_selected_session = None
        self.ctrl_down = False
        
        self.folders = {}
        self.folder_order = [] # ✅ 폴더 순서 저장
        self.current_folder = None
        self.search_query = ""
        
        self._filtered_sessions_cache = None
        self._cache_valid = False
        self._save_after_id = None
        self._save_delay_ms = 250
        self._search_after_id = None
        self._search_delay_ms = 120
        
        self.folder_buttons = {}
        self.headers = []
        self.no_result_msg = None
        
        if os.path.exists(ICON_FILE): 
            self.root.iconbitmap(ICON_FILE)
        
        self.main_container = tk.Frame(self.root, bg="#f5f7f9")
        self.main_container.pack(fill='both', expand=True)
        
        self.grid_frame = None
        self.search_var = None
        
        self.load_all()
        
        # ✅ 시작 폴더 설정 적용
        startup_folder = self.global_settings.get("startup_folder")
        if startup_folder is None or startup_folder == "__ALL__":
            self.current_folder = None
        elif startup_folder in self.folders:
            self.current_folder = startup_folder
        
        # 저장된 창 크기(없으면 자동 계산값)를 시작 시 즉시 반영
        self.adjust_window_size()

        self.cleanup_old_logs(days=30)
        self.init_ui()
        self.refresh_grid()

    def on_app_close(self):
        """종료 직전에 보류 중인 저장 요청까지 반영"""
        try:
            self.save_all()
        finally:
            self.root.destroy()
    
    def adjust_window_size(self):
        """윈도우 크기 조정"""
        width = self.global_settings.get("window_width")
        height = self.global_settings.get("window_height")

        try:
            width = int(width)
            height = int(height)
            if width >= 900 and height >= 600:
                self.root.geometry(f"{width}x{height}")
                return
        except (TypeError, ValueError):
            pass

        self.root.geometry(f"{max(1200, (self.col_count * CARD_WIDTH) + 100)}x900")
    
    def load_all(self):
        """모든 데이터 로드"""
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                    self.global_settings = d.get("settings", {})
                    
                    self.zone_names = {int(k): v for k, v in d.get("zones", {}).items()}
                    
                    folders_data = d.get("folders", {})
                    if isinstance(folders_data, dict):
                        for name, folder_data in folders_data.items():
                            folder = FolderGroup.from_dict(folder_data)
                            self.folders[folder.name] = folder
                    elif isinstance(folders_data, list):
                        for folder_data in folders_data:
                            folder = FolderGroup.from_dict(folder_data)
                            self.folders[folder.name] = folder
                    
                    # ✅ 폴더 순서 로드 및 동기화
                    self.folder_order = d.get("folder_order", [])
                    
                    # 저장된 순서와 실제 폴더 목록 간의 불일치 해결
                    saved_order_set = set(self.folder_order)
                    actual_folders_set = set(self.folders.keys())
                    
                    # 삭제된 폴더를 순서 목록에서 제거
                    self.folder_order = [f for f in self.folder_order if f in actual_folders_set]
                    
                    # 새로 추가된 폴더를 순서 목록 끝에 추가 (알파벳 순)
                    new_folders = sorted(list(actual_folders_set - saved_order_set))
                    self.folder_order.extend(new_folders)
                    
                    for s in d.get("sessions", []):
                        if 'pw' in s:
                            s['pw'] = SecureData.decrypt_password(s['pw'])
                        if 'ru_pw' in s:
                            s['ru_pw'] = SecureData.decrypt_password(s['ru_pw'])
                        
                        if 'col' in s and 'col_positions' not in s:
                            s['col_positions'] = {"__ROOT__": s['col']}
                        
                        self.sessions.append(SessionRow(None, self, s))
            except Exception as e:
                messagebox.showwarning("Load Error", f"설정 로드 실패: {str(e)}")
        
        if not self.global_settings.get("teraterm_path"):
            self.global_settings["teraterm_path"] = r"C:\Program Files (x86)\teraterm\ttermpro.exe"
        if not self.global_settings.get("moba_path"):
            self.global_settings["moba_path"] = r"C:\Program Files (x86)\Mobatek\MobaXterm\MobaXterm.exe"
    
    def resolve_log_dir(self, create=True):
        """설정된 로그 경로를 해석하고, 실패 시 사용자 로컬 경로로 폴백"""
        configured = (self.global_settings.get("log_path") or "").strip()
        candidates = []
        
        if configured:
            expanded = os.path.expandvars(os.path.expanduser(configured))
            if not os.path.isabs(expanded):
                expanded = os.path.join(BASE_DIR, expanded)
            candidates.append(os.path.abspath(expanded))
        
        candidates.append(os.path.abspath(DEFAULT_LOG_DIR))
        candidates.append(os.path.abspath(os.path.join(BASE_DIR, "Logs")))
        
        for log_dir in candidates:
            try:
                if create:
                    os.makedirs(log_dir, exist_ok=True)
                if os.path.isdir(log_dir):
                    if self.global_settings.get("log_path") != log_dir:
                        self.global_settings["log_path"] = log_dir
                    return log_dir
            except Exception:
                continue
        return None
    
    def save_all(self):
        """모든 데이터 저장"""
        if self._save_after_id:
            try:
                self.root.after_cancel(self._save_after_id)
            except Exception:
                pass
            self._save_after_id = None

        data_to_save = {
            "settings": self.global_settings,
            "zones": {str(k): v for k, v in self.zone_names.items()}, 
            "folders": {name: f.to_dict() for name, f in self.folders.items()}, 
            "folder_order": self.folder_order, # ✅ 폴더 순서 저장
            "sessions": []
        }
        
        for s in self.sessions:
            session_data = s.data.copy()
            session_data['pw'] = SecureData.encrypt_password(session_data.get('pw', ''))
            session_data['ru_pw'] = SecureData.encrypt_password(session_data.get('ru_pw', ''))
            data_to_save['sessions'].append(session_data)
        
        try:
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data_to_save, f, indent=4, ensure_ascii=False)
            self._cache_valid = False
        except IOError as e:
            messagebox.showerror("Save Error", f"저장 실패: {str(e)}")

    def request_save(self, immediate=False):
        """짧은 디바운스로 저장 요청을 배치 처리"""
        if immediate:
            self.save_all()
            return

        if self._save_after_id:
            try:
                self.root.after_cancel(self._save_after_id)
            except Exception:
                pass

        self._save_after_id = self.root.after(self._save_delay_ms, self.save_all)

    def _schedule_search_refresh(self):
        """검색 입력 시 리프레시 폭주를 줄이기 위한 내부 디바운스"""
        if self._search_after_id:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.root.after(self._search_delay_ms, self.refresh_grid_search_only)
    
    def cleanup_old_logs(self, days=30):
        """오래된 로그 정리"""
        log_dir = self.resolve_log_dir(create=False)
        if not log_dir or not os.path.exists(log_dir):
            return
        
        cutoff_time = datetime.now() - timedelta(days=days)
        
        try:
            for filename in os.listdir(log_dir):
                file_path = os.path.join(log_dir, filename)
                try:
                    if os.path.isfile(file_path):
                        file_time = datetime.fromtimestamp(os.path.getctime(file_path))
                        if file_time < cutoff_time:
                            os.remove(file_path)
                except:
                    pass
        except:
            pass
    
    def add_folder(self):
        """새 폴더 추가"""
        name = simpledialog.askstring("폴더 추가", "폴더 이름:", parent=self.root)
        if name and name not in self.folders:
            self.folders[name] = FolderGroup(name)
            self.folder_order.append(name) # ✅ 순서 목록에 추가
            self.request_save()
            self.refresh_grid()
        elif name in self.folders:
            messagebox.showwarning("중복", "이미 존재하는 폴더 이름입니다")
    
    def remove_folder(self, folder_name):
        """폴더 삭제"""
        if folder_name in self.folders:
            for s in self.sessions:
                if s.data.get('folder') == folder_name:
                    s.data['folder'] = None
            
            del self.folders[folder_name]
            if folder_name in self.folder_order: # ✅ 순서 목록에서 제거
                self.folder_order.remove(folder_name)
            if self.current_folder == folder_name:
                self.current_folder = None
            
            self.request_save()
            self._cache_valid = False
            self.refresh_grid()
            
    def rename_folder(self, old_name):
        """폴더 이름 변경 (새로 추가된 핵심 로직)"""
        new_name = simpledialog.askstring("폴더 이름 변경", f"새 폴더 이름:", initialvalue=old_name, parent=self.root)
        
        # 입력이 없거나 이름이 그대로인 경우 취소
        if not new_name or new_name == old_name:
            return
            
        # 중복 검사
        if new_name in self.folders:
            messagebox.showwarning("중복", "이미 존재하는 폴더 이름입니다")
            return

        # 1. 내부 폴더 딕셔너리 정보 교체
        folder_obj = self.folders.pop(old_name)
        folder_obj.name = new_name
        self.folders[new_name] = folder_obj
        
        # ✅ 1.5. 폴더 순서 리스트 정보 교체
        try:
            idx = self.folder_order.index(old_name)
            self.folder_order[idx] = new_name
        except ValueError:
            # 만약 순서 목록에 없다면(오류 상황), 그냥 뒤에 추가
            self.folder_order.append(new_name)

        # 2. 현재 선택된 폴더 화면 갱신
        if self.current_folder == old_name:
            self.current_folder = new_name

        # 3. 속해있는 세션들의 소속 정보 및 컬럼 위치 키(Key) 동기화
        for s in self.sessions:
            # 소속 변경
            if s.data.get('folder') == old_name:
                s.data['folder'] = new_name
            
            # col_positions에 기록된 위치 데이터의 키(폴더명) 변경
            if 'col_positions' in s.data and old_name in s.data['col_positions']:
                s.data['col_positions'][new_name] = s.data['col_positions'].pop(old_name)
        
        self.request_save()
        self._cache_valid = False
        self.refresh_grid()
    
    def select_folder(self, folder_name):
        """폴더 선택"""
        self.deselect_all()
        self.current_folder = folder_name
        self.search_query = ""
        if self.search_var:
            self.search_var.set("")
        self._cache_valid = False
        self.refresh_grid()

    def on_folder_button_click(self, event, folder_name):
        """폴더 버튼 좌클릭 시 폴더 전환을 명시적으로 처리"""
        self.select_folder(folder_name)
        return "break"
    
    def show_folder_menu(self, event, folder_name):
        """폴더 우클릭 메뉴 (이름 변경 항목 추가됨)"""
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="✏ 이름 변경", command=lambda: self.rename_folder(folder_name))
        menu.add_command(label="🗑 삭제", command=lambda: self.remove_folder(folder_name))
        menu.post(event.x_root, event.y_root)
    
    def get_filtered_sessions(self):
        """필터링된 세션 반환 (캐싱)"""
        if self._cache_valid and self._filtered_sessions_cache is not None:
            return self._filtered_sessions_cache
        
        result = []
        
        for s in self.sessions:
            if self.current_folder is not None:
                if s.data.get('folder') != self.current_folder:
                    continue
            
            if self.search_query:
                if not (self.search_query in s.data.get('name', '').lower() or
                        self.search_query in s.data.get('ip', '').lower() or
                        self.search_query in s.data.get('user', '').lower() or
                        self.search_query in TYPE_MAP.get(s.data.get('type', 'S'), '').lower()):
                    continue
            
            result.append(s)
        
        self._filtered_sessions_cache = result
        self._cache_valid = True
        
        return result
    
    def init_ui(self):
        """고정 UI 요소들을 단 한 번만 생성"""
        # (선택 로직은 마우스 이벤트의 state 비트를 사용한다)

        self.nav_frame = tk.Frame(self.main_container, bg="#212121", pady=10)
        self.nav_frame.pack(fill='x')

        # 상단 네비게이션을 2줄로 분리해 버튼 밀집도를 낮춘다.
        nav_top = tk.Frame(self.nav_frame, bg="#212121")
        nav_top.pack(fill='x', padx=10)
        nav_bottom = tk.Frame(self.nav_frame, bg="#212121")
        nav_bottom.pack(fill='x', padx=10, pady=(6, 0))
        
        self.nav_left = tk.Frame(nav_top, bg="#212121")
        self.nav_left.pack(side='left', padx=20)
        
        self.btn_all = tk.Button(
            self.nav_left, 
            text="📋 전체", 
            command=lambda: self.select_folder(None),
            font=('Segoe UI', 9, 'bold'),
            fg="white" if self.current_folder is None else "#90a4ae",
            bg="#455a64" if self.current_folder is None else "#212121",
            bd=0, 
            cursor="hand2",
            padx=10
        )
        self.btn_all.pack(side='left', padx=2)
        
        self.frame_folders = tk.Frame(self.nav_left, bg="#212121")
        self.frame_folders.pack(side='left')
        
        tk.Button(
            self.nav_left,
            text="➕ 폴더",
            command=self.add_folder,
            font=('Segoe UI', 9, 'bold'),
            fg="white",
            bg="#2e7d32",
            bd=0,
            cursor="hand2",
            padx=10
        ).pack(side='left', padx=2)

        # ✅ 폴더 순서 변경 버튼 추가
        tk.Button(
            self.nav_left,
            text="🔄 순서 변경",
            command=self.open_folder_order_editor,
            font=('Segoe UI', 9, 'bold'),
            fg="white",
            bg="#607d8b",
            bd=0,
            cursor="hand2",
            padx=10
        ).pack(side='left', padx=2)
        
        # ✅ 일괄 작업 프레임 추가 (초기에는 숨겨짐)
        self.bulk_action_frame = tk.Frame(self.main_container, bg="#37474f", pady=5)

        self.bulk_action_label = tk.Label(self.bulk_action_frame, text="", bg="#37474f", fg="white", font=('Segoe UI', 9, 'bold'))
        self.bulk_action_label.pack(side='left', padx=20)

        btn_style = {"font": ('Segoe UI', 8, 'bold'), "bd": 0, "cursor": "hand2", "padx": 10, "pady": 3}
        tk.Button(self.bulk_action_frame, text="➡️ 이동", command=self.bulk_move, bg="#ffc107", fg="black", **btn_style).pack(side='left', padx=5)
        tk.Button(self.bulk_action_frame, text="🚀 접속", command=self.bulk_connect, bg="#8bc34a", fg="black", **btn_style).pack(side='left', padx=5)
        tk.Button(self.bulk_action_frame, text="🗑️ 삭제", command=self.bulk_delete, bg="#f44336", fg="white", **btn_style).pack(side='left', padx=5)

        tk.Button(self.bulk_action_frame, text="모두 선택 해제", command=self.deselect_all, bg="#90a4ae", fg="white", **btn_style).pack(side='right', padx=20)


        top_right = tk.Frame(nav_top, bg="#212121")
        top_right.pack(side='right', padx=10)
        top_right_btn_style = {"font": ('Segoe UI', 9, 'bold'), "fg": "white", "width": 8, "bd": 0, "cursor": "hand2"}

        tk.Button(top_right, text="⚙ SET", command=self.open_app_settings, bg="#ffa000", **top_right_btn_style).pack(side='right', padx=5)
        tk.Label(top_right, text="DASHSTATION v1.0", fg="#00e5ff", bg="#212121", font=('', 10, 'bold')).pack(side='right', padx=10)

        center = tk.Frame(nav_bottom, bg="#212121")
        center.pack(side='left', padx=20)
        s = {"font": ('Segoe UI', 9, 'bold'), "fg": "white", "width": 8, "bd": 0, "cursor": "hand2"}
        
        tk.Button(center, text="+ ADD", command=self.add_session_ui, bg="#2e7d32", **s).pack(side='left', padx=5)
        tk.Button(center, text="+ COL", command=self.add_column, bg="#455a64", **s).pack(side='left', padx=5)
        tk.Button(center, text="- COL", command=self.remove_column, bg="#c62828", **s).pack(side='left', padx=5)
        tk.Button(center, text="LOG DIR", command=self._open_log_dir, bg="#607d8b", **s).pack(side='left', padx=5)
        
        search_frame = tk.Frame(nav_bottom, bg="#212121")
        search_frame.pack(side='right', padx=10)
        
        tk.Label(search_frame, text="🔍 검색:", bg="#212121", fg="white", font=('', 9)).pack(side='left', padx=(0, 5))
        
        self.search_var = tk.StringVar(value=self.search_query)
        search_entry = tk.Entry(search_frame, width=25, bd=1, relief="solid", textvariable=self.search_var)
        search_entry.pack(side='left', fill='x')
        
        def on_search_change(var_name, index, mode):
            new_query = self.search_var.get().lower()
            if new_query != self.search_query:
                self.deselect_all()
                self.search_query = new_query
                self._cache_valid = False
                self._schedule_search_refresh()
        
        self.search_var.trace('w', on_search_change)
        
        self.canvas = tk.Canvas(self.main_container, bg="#f5f7f9", highlightthickness=0)
        sb = tk.Scrollbar(self.main_container, command=self.canvas.yview)
        self.grid_frame = tk.Frame(self.canvas, bg="#f5f7f9", padx=10, pady=10)
        cw = self.canvas.create_window((0,0), window=self.grid_frame, anchor='nw', tags="f")
        self.canvas.config(yscrollcommand=sb.set)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfig(cw, width=e.width), self.canvas.config(scrollregion=self.canvas.bbox("all"))))
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())
        self.canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

    def refresh_grid(self):
        """기존 UI 객체를 파괴하지 않고 숨기기(forget)와 재배치만 수행"""
        self.btn_all.config(
            fg="white" if self.current_folder is None else "#90a4ae",
            bg="#455a64" if self.current_folder is None else "#212121"
        )
        
        for btn in self.folder_buttons.values():
            btn.pack_forget()
            
        for folder_name in self.folder_order: # ✅ sorted(self.folders.keys()) 대신 self.folder_order 사용
            if folder_name not in self.folders: continue # 안전장치
            count = len([s for s in self.sessions if s.data.get('folder') == folder_name])
            btn_text = f"📁 {folder_name} ({count})"
            is_selected = self.current_folder == folder_name
            
            if folder_name not in self.folder_buttons:
                btn = tk.Button(
                    self.frame_folders,
                    font=('Segoe UI', 9, 'bold'),
                    bd=0,
                    cursor="hand2",
                    padx=10
                )
                btn.bind("<Button-1>", lambda e, fn=folder_name: self.on_folder_button_click(e, fn))
                btn.bind("<Button-3>", lambda e, fn=folder_name: self.show_folder_menu(e, fn))
                self.folder_buttons[folder_name] = btn
                
            btn = self.folder_buttons[folder_name]
            btn.config(
                text=btn_text,
                command=lambda fn=folder_name: self.select_folder(fn),
                fg="white" if is_selected else "#90a4ae",
                bg="#1976d2" if is_selected else "#212121"
            )
            btn.pack(side='left', padx=2)

        for i in range(self.col_count):
            if i >= len(self.headers):
                self.grid_frame.columnconfigure(i, weight=1, uniform="g")
                h = tk.Frame(self.grid_frame, bg="#eceff1", pady=8)
                lbl = tk.Label(h, fg="#546e7a", bg="#eceff1", font=('', 9, 'bold'))
                lbl.pack()
                h.bind("<Button-1>", lambda e, idx=i: self.rename_zone(idx))
                lbl.bind("<Button-1>", lambda e, idx=i: self.rename_zone(idx))
                self.headers.append((h, lbl))
            
            h, lbl = self.headers[i]
            lbl.config(text=self.zone_names.get(i, f"ZONE {i+1}"))
            h.grid(row=0, column=i, sticky='nsew', padx=4, pady=(0,15))
            
        for i in range(self.col_count, len(self.headers)):
            self.headers[i][0].grid_forget()

        # [성능 개선] UI 갱신 로직 최적화
        filtered_sessions = self.get_filtered_sessions()
        filtered_set = set(filtered_sessions)

        # 1. 필터에 포함되지 않는 세션의 UI는 화면에서 숨긴다.
        for session in self.sessions:
            if session not in filtered_set:
                if session.outer_frame and session.outer_frame.winfo_exists():
                    session.outer_frame.grid_forget()

        # 2. 필터링된 세션만 순서대로 화면에 배치한다.
        rc = [1] * self.col_count
        for session in filtered_sessions:
            session.update_ui_elements() # 데이터 변경사항을 UI에 반영
            cl = min(session.get_col(self.current_folder), self.col_count-1)
            widget = session.create_widget(self.grid_frame)
            widget.grid(row=rc[cl], column=cl, padx=2, pady=2, sticky='nsew')
            session.update_selection_visual() # 선택 상태 시각화 적용
            rc[cl]+=1
        
        if not filtered_sessions:
            msg_text = f"검색 결과 없음: '{self.search_query}'"
            # 검색어가 없을 때만 폴더가 비었다는 메시지 표시
            if self.current_folder and not self.search_query:
                msg_text = f"'{self.current_folder}' 폴더가 비어있습니다"
                
            if not self.no_result_msg:
                self.no_result_msg = tk.Label(self.grid_frame, font=('', 11), fg="#999", bg="#f5f7f9")
                
            self.no_result_msg.config(text=msg_text)
            self.no_result_msg.grid(row=1, column=0, columnspan=self.col_count, pady=50)
        else:
            if self.no_result_msg and self.no_result_msg.winfo_exists():
                self.no_result_msg.grid_forget()
                
        self.canvas.update_idletasks()
        self.canvas.config(scrollregion=self.canvas.bbox("all"))
    
    def refresh_grid_search_only(self):
        self._search_after_id = None
        self.refresh_grid()
    
    def refresh_grid_light(self):
        """드래그 중 가벼운 갱신"""
        rc = [1] * self.col_count
        filtered = self.get_filtered_sessions()
        for s in filtered:
            cl = min(s.get_col(self.current_folder), self.col_count-1)
            if s.outer_frame: 
                s.outer_frame.grid(row=rc[cl], column=cl, padx=2, pady=2, sticky='nsew')
            rc[cl]+=1
    
    def add_column(self):
        """컬럼 추가"""
        self.col_count += 1
        self.adjust_window_size()
        self.refresh_grid()
    
    def remove_column(self):
        """컬럼 제거"""
        if self.col_count > 1:
            self.col_count -= 1
            for s in self.sessions:
                for folder in [None] + list(self.folders.keys()):
                    current_col = s.get_col(folder)
                    if current_col >= self.col_count:
                        s.set_col(self.col_count - 1, folder)
            
            self.adjust_window_size()
            self.request_save()
            self.refresh_grid()
    
    def add_session_ui(self):
        """새 세션 추가"""
        new_session = SessionRow(None, self)
        new_session.data['folder'] = self.current_folder
        self.sessions.append(new_session)
        self._cache_valid = False
        self.refresh_grid()
    
    def rename_zone(self, idx):
        """존 이름 변경"""
        n = simpledialog.askstring("Rename", "Zone Name:", initialvalue=self.zone_names.get(idx, f"ZONE {idx+1}"))
        if n:
            self.zone_names[idx] = n
            self.request_save()
            self.refresh_grid()
    
    def _open_log_dir(self):
        """로그 디렉토리 열기"""
        log_dir = self.resolve_log_dir(create=True)
        if not log_dir:
            messagebox.showerror("Error", "로그 디렉토리 생성 실패")
            return
        try:
            os.startfile(log_dir)
        except Exception as e:
            messagebox.showerror("Error", f"디렉토리 열기 실패: {str(e)}")
    
    def open_folder_order_editor(self):
        """폴더 순서 편집 창"""
        win = tk.Toplevel(self.root)
        win.title("폴더 순서 편집")
        win.geometry("400x500")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="버튼을 이용해 폴더 순서를 변경하세요.", pady=10).pack()

        list_frame = tk.Frame(win)
        list_frame.pack(pady=5, padx=20, fill='both', expand=True)

        lb = tk.Listbox(list_frame, height=15, font=('Segoe UI', 10))
        sb = tk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
        lb.config(yscrollcommand=sb.set)
        
        for folder_name in self.folder_order:
            lb.insert('end', folder_name)

        lb.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        btn_frame = tk.Frame(win)
        btn_frame.pack(pady=10)

        def move(direction):
            sel = lb.curselection()
            if not sel: return
            idx = sel[0]
            new_idx = idx + direction
            if 0 <= new_idx < lb.size():
                item = lb.get(idx)
                lb.delete(idx)
                lb.insert(new_idx, item)
                lb.selection_clear(0, 'end')
                lb.selection_set(new_idx)
                lb.activate(new_idx)

        tk.Button(btn_frame, text="▲ 위로", command=lambda: move(-1), width=10).pack(side='left', padx=10)
        tk.Button(btn_frame, text="▼ 아래로", command=lambda: move(1), width=10).pack(side='left', padx=10)

        def save_order():
            self.folder_order = list(lb.get(0, 'end'))
            self.request_save()
            self.refresh_grid()
            win.destroy()

        save_btn_frame = tk.Frame(win)
        save_btn_frame.pack(pady=(10, 20))
        tk.Button(save_btn_frame, text="저장", command=save_order, bg="#1976d2", fg="white", width=15, pady=5).pack(side='left', padx=5)
        tk.Button(save_btn_frame, text="취소", command=win.destroy, width=10, pady=5).pack(side='left', padx=5)

    def open_app_settings(self):
        """전역 설정 창"""
        win = tk.Toplevel(self.root)
        win.title("Global Settings")
        win.geometry("620x560")
        ents = {}
        
        tk.Label(win, text="전역 설정", font=('Segoe UI', 10, 'bold'), bg="#f5f5f5").pack(fill='x', padx=20, pady=10)
        
        for l, k in [("TeraTerm 경로", "teraterm_path"), ("MobaXterm 경로", "moba_path"), ("Log 저장 폴더", "log_path")]:
            f = tk.Frame(win)
            f.pack(fill='x', padx=30, pady=5)
            tk.Label(f, text=l, width=15, anchor='w').pack(side='left')
            e = tk.Entry(f, width=40)
            e.insert(0, self.global_settings.get(k, ""))
            e.pack(side='left', padx=5, fill='x', expand=True)
            
            def browse_dir(entry, is_file=True):
                p = filedialog.askopenfilename() if is_file else filedialog.askdirectory()
                if p:
                    entry.delete(0, 'end')
                    entry.insert(0, p)
            
            tk.Button(f, text="찾기", command=lambda e=e, is_file=(k != "log_path"): browse_dir(e, is_file)).pack(side='left')
            ents[k] = e

        # --- 구분선 ---
        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=20, pady=15)

        # ✅ 시작 폴더 설정 추가
        f_startup = tk.Frame(win)
        f_startup.pack(fill='x', padx=30, pady=5)
        
        tk.Label(f_startup, text="시작 시 열 폴더", width=15, anchor='w').pack(side='left')
        
        folder_options = ["전체"] + self.folder_order
        startup_menu = ttk.Combobox(f_startup, values=folder_options, state="readonly", width=40)
        
        current_startup_key = self.global_settings.get("startup_folder")
        if current_startup_key is None or current_startup_key == "__ALL__":
            startup_menu.set("전체")
        elif current_startup_key in self.folders:
            startup_menu.set(current_startup_key)
        else:
            startup_menu.set("전체")
        startup_menu.pack(side='left', padx=5, fill='x', expand=True)

        # --- 구분선 ---
        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=20, pady=15)

        # ✅ 창 크기 수동 설정
        f_window_size = tk.Frame(win)
        f_window_size.pack(fill='x', padx=30, pady=5)

        tk.Label(f_window_size, text="창 크기 (W x H)", width=15, anchor='w').pack(side='left')

        current_w = str(self.global_settings.get("window_width", "1200"))
        current_h = str(self.global_settings.get("window_height", "900"))

        window_width_entry = tk.Entry(f_window_size, width=8)
        window_width_entry.insert(0, current_w)
        window_width_entry.pack(side='left', padx=(5, 4))

        tk.Label(f_window_size, text="x").pack(side='left')

        window_height_entry = tk.Entry(f_window_size, width=8)
        window_height_entry.insert(0, current_h)
        window_height_entry.pack(side='left', padx=(4, 0))

        tk.Label(
            win,
            text="최소 권장 크기: 900 x 600 (미만 입력 시 자동 크기 사용)",
            fg="#78909c"
        ).pack(anchor='w', padx=32, pady=(2, 0))
        
        def save_settings():
            for k, e in ents.items():
                self.global_settings[k] = e.get()
            
            selected_folder = startup_menu.get()
            self.global_settings["startup_folder"] = "__ALL__" if selected_folder == "전체" else selected_folder

            w_raw = window_width_entry.get().strip()
            h_raw = window_height_entry.get().strip()
            try:
                w = int(w_raw)
                h = int(h_raw)
                if w < 900 or h < 600:
                    raise ValueError
                self.global_settings["window_width"] = str(w)
                self.global_settings["window_height"] = str(h)
            except ValueError:
                # 잘못된 값이면 자동 크기 모드로 폴백
                self.global_settings["window_width"] = ""
                self.global_settings["window_height"] = ""

            self.save_all()
            self.adjust_window_size()
            win.destroy()
            messagebox.showinfo("Success", "설정이 저장되었습니다")
        
        tk.Button(win, text="저장", command=save_settings, bg="#212121", fg="white", width=20, pady=10).pack(pady=20)

    # ========== [일괄 작업 기능] ==========
    def is_ctrl_click(self, event):
        """마우스 이벤트 + OS 키 상태로 Ctrl 입력을 안정적으로 판별"""
        state = int(getattr(event, "state", 0) or 0)
        event_ctrl = bool(state & 0x0004)
        os_ctrl = False
        if IS_WINDOWS:
            try:
                # Windows: VK_CONTROL(0x11) high bit set == currently pressed
                os_ctrl = bool(ctypes.windll.user32.GetKeyState(0x11) & 0x8000)
            except Exception:
                os_ctrl = False
        return event_ctrl or os_ctrl

    def apply_selection_visuals(self):
        """selected_sessions 집합을 기준으로 UI 선택 상태를 동기화"""
        # 선택 수 표시 바는 예외와 무관하게 항상 갱신
        self.update_bulk_action_ui()

        for s in self.sessions:
            try:
                s.is_selected = (s in self.selected_sessions)
                s.update_selection_visual()
            except Exception:
                pass

        # 최종 상태를 한 번 더 반영
        self.update_bulk_action_ui()

    def handle_selection(self, session, event=None, ctrl_override=None):
        """세션 카드 선택 로직 (Ctrl + 클릭 다중 선택 전용)"""
        ctrl_pressed = bool(ctrl_override)

        # 선택은 Ctrl+클릭으로만 처리
        if not ctrl_pressed:
            return

        # Ctrl + 클릭: 항상 "추가 선택"만 수행 (해제는 모두 선택 해제로 처리)
        self.selected_sessions.add(session)

        self.last_selected_session = session
        self.apply_selection_visuals()

    def update_bulk_action_ui(self):
        """선택된 세션 수에 따라 일괄 작업 바 표시/숨김 및 텍스트 업데이트"""
        count = len(self.selected_sessions)
        if count > 0:
            self.bulk_action_label.config(text=f"{count}개 세션 선택됨")
            self.bulk_action_frame.pack(fill='x', before=self.canvas)
        else:
            self.bulk_action_frame.pack_forget()

    def deselect_all(self, refresh_ui=True):
        """모든 세션 선택 해제"""
        self.selected_sessions.clear()
        self.last_selected_session = None
        if refresh_ui:
            self.apply_selection_visuals()
        else:
            self.update_bulk_action_ui()

    def bulk_move(self):
        """선택된 세션들을 다른 폴더로 일괄 이동"""
        if not self.selected_sessions: return

        win = tk.Toplevel(self.root)
        win.title("폴더로 이동"); win.geometry("300x200"); win.transient(self.root); win.grab_set()
        tk.Label(win, text=f"{len(self.selected_sessions)}개 세션을 이동할 폴더를 선택하세요:", pady=10).pack()
        folder_options = ["(폴더 없음)"] + self.folder_order
        folder_var = tk.StringVar(value=folder_options[0])
        menu = ttk.Combobox(win, textvariable=folder_var, values=folder_options, state="readonly", width=30)
        menu.pack(pady=10, padx=20)

        def do_move():
            target_folder = folder_var.get()
            target_folder = None if target_folder == "(폴더 없음)" else target_folder
            for s in self.selected_sessions:
                s.data['folder'] = target_folder
            self.save_all(); self.deselect_all(); self.refresh_grid(); win.destroy()

        btn_frame = tk.Frame(win); btn_frame.pack(pady=20)
        tk.Button(btn_frame, text="이동", command=do_move, bg="#1976d2", fg="white", width=10).pack(side='left', padx=10)
        tk.Button(btn_frame, text="취소", command=win.destroy, width=10).pack(side='left')

    def bulk_delete(self):
        """선택된 세션들을 일괄 삭제"""
        count = len(self.selected_sessions)
        if not count or not messagebox.askyesno("일괄 삭제", f"{count}개의 세션을 정말로 삭제하시겠습니까?"): return
        for s in list(self.selected_sessions):
            if s.outer_frame and s.outer_frame.winfo_exists(): s.outer_frame.destroy()
            self.sessions.remove(s)
        self.selected_sessions.clear(); self.save_all(); self.refresh_grid(); self.update_bulk_action_ui()

    def bulk_connect(self):
        """선택된 세션들에 일괄 접속"""
        count = len(self.selected_sessions)
        if not count: return
        
        def connect_thread():
            sorted_sessions = sorted(list(self.selected_sessions), key=lambda s: s.data.get('name', ''))
            for s in sorted_sessions:
                s.run_connection()
                # 동시 실행 폭주를 막기 위해 세션 사이에 짧은 간격을 둔다.
                if s.data.get('type') == 'M':
                    try:
                        delay = max(0.2, float(s.data.get('moba_interval', 3.0)))
                    except (TypeError, ValueError):
                        delay = 0.5
                else:
                    delay = 0.3
                time.sleep(delay)
        
        threading.Thread(target=connect_thread, daemon=True).start()
        messagebox.showinfo("일괄 접속", f"{count}개 세션에 대한 접속을 시작합니다.")
        self.deselect_all()

# ========== [메인] ==========
if __name__ == "__main__":
    root = tk.Tk()
    app = MainApp(root)
    root.mainloop()