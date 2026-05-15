import tkinter as tk
from tkinter import messagebox, simpledialog
import subprocess
import json
import os
import time
import platform
import re

# 1. 환경 설정
CONFIG_FILE = "session_config.json"
ZONE_NAME_FILE = "zone_names.json"
TERATERM_PATH = r"C:\Program Files (x86)\teraterm\ttermpro.exe"
MOBA_PATH = r"C:\Program Files (x86)\Mobatek\MobaXterm\MobaXterm.exe"
MACRO_PATH = os.path.join(os.environ['TEMP'], "at_macro.ttl")
ICON_FILE = "app_icon1.ico"

TYPE_MAP = {
    "S": "SSH (TeraTerm)", "R": "RDP (Remote)", "M": "MobaXterm (BM)", 
    "P": "Python / EXE", "C": "Serial (COM Port)"
}
CONNECT_TEXT_MAP = {
    "S": "SSH CONNECT", "R": "RDP CONNECT", "M": "MOBA CONNECT", 
    "P": "EXE / PY CONNECT", "C": "SERIAL CONNECT"
}
INV_TYPE_MAP = {v: k for k, v in TYPE_MAP.items()}
COLUMN_BASE_WIDTH = 280 

class SessionRow:
    def __init__(self, master, app_instance, data=None):
        self.master = master
        self.app = app_instance
        self.data = data if data else {
            'name': '', 'type': 'S', 'ip': '', 'port': '22', 'baud_rate': '115200',
            'user': '', 'pw': '', 'ru_cmd': '', 'ru_pw': '', 'final_cmd': '', 'col': 0
        }
        if 'port' not in self.data: self.data['port'] = '22'
        if 'baud_rate' not in self.data: self.data['baud_rate'] = '115200'
        self.outer_frame = None

    def create_widget(self, master):
        self.outer_frame = tk.Frame(master, bg="#f5f7f9", padx=6, pady=6)
        self.frame = tk.Frame(self.outer_frame, bd=0, bg="#ffffff", highlightthickness=1, highlightbackground="#cfd8dc")
        self.frame.pack(fill='both', expand=True)
        
        t = self.data.get('type', 'S')
        indicator_color = {"S": "#1976d2", "R": "#9c27b0", "M": "#2e7d32", "P": "#fbc02d", "C": "#e64a19"}.get(t, "#455a64")
        tk.Frame(self.frame, bg=indicator_color, width=5).pack(side='left', fill='y')

        content = tk.Frame(self.frame, bg="#ffffff", padx=10, pady=8)
        content.pack(side='left', fill='both', expand=True)

        # [변경] 헤더 구성: 버튼 박스를 최상단 우측에 고정
        header = tk.Frame(content, bg="#ffffff")
        header.pack(fill='x', side='top')
        
        btn_box = tk.Frame(header, bg="#ffffff")
        btn_box.pack(side='right')
        icon_s = {"font": ('Arial', 8), "bd": 0, "bg": "#ffffff", "activebackground": "#f5f5f5", "cursor": "hand2"}
        tk.Button(btn_box, text="⚡", command=self.run_ping, fg="#ffa000", **icon_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="⚙", command=self.open_config, fg="#90a4ae", **icon_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="⧉", command=self.copy_session, fg="#4caf50", **icon_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="×", command=self.delete_self, fg="#f44336", **icon_s).pack(side='left', padx=2)

        self.lbl_name = tk.Label(header, text=self.data.get('name', 'New'), font=('Segoe UI', 10, 'bold'), bg="#ffffff", fg="#263238", anchor='w', cursor="fleur")
        self.lbl_name.pack(side='left', fill='x', expand=True)
        self.lbl_name.bind("<Button-1>", self.on_drag_start)
        self.lbl_name.bind("<B1-Motion>", self.on_drag_motion)
        self.lbl_name.bind("<ButtonRelease-1>", self.on_drag_release)

        info_text = self.data.get('ip', '0.0.0.0')
        if t == "S": info_text += f":{self.data.get('port', '22')}"
        elif t == "C": info_text = f"COM{info_text} ({self.data.get('baud_rate', '115200')})"

        self.lbl_ip = tk.Label(content, text=info_text, font=('Consolas', 9), bg="#ffffff", fg="#78909c", anchor='w')
        self.lbl_ip.pack(fill='x', pady=(2, 5))

        self.btn_connect = tk.Button(content, text=CONNECT_TEXT_MAP.get(t, "CONNECT"), command=self.run_connection, font=('Segoe UI', 7, 'bold'), bg="#f8f9fa", fg=indicator_color, relief="flat", highlightthickness=1, highlightbackground=indicator_color, cursor="hand2", overrelief="groove")
        self.btn_connect.pack(fill='x')
        return self.outer_frame

    def open_config(self):
        config_win = tk.Toplevel(self.app.root); config_win.title("Configuration"); config_win.geometry("460x720")
        
        # 가이드와 함께 필드 정의
        fields = [
            ("세션 이름 (카드 제목)", "name"), ("IP 주소 / COM 번호 (숫자만)", "ip"),
            ("SSH 포트 (기본: 22)", "port"), ("Baud Rate (시리얼 속도)", "baud_rate"),
            ("ID (Username)", "user"), ("Password", "pw"),
            ("RU 자동 명령 (su - 등)", "ru_cmd"), ("RU PW", "ru_pw"),
            ("추가 명령 (쉼표 구분)", "final_cmd")
        ]
        
        entries = {}
        # 타입 선택 메뉴 상단 배치
        tk.Label(config_win, text="[ 접속 타입 선택 ]", font=('Arial', 9, 'bold')).pack(pady=(10,0))
        var_type = tk.StringVar(value=TYPE_MAP.get(self.data['type']))
        type_menu = tk.OptionMenu(config_win, var_type, *TYPE_MAP.values(), command=lambda _: update_status())
        type_menu.pack(pady=5)

        f_frame = tk.Frame(config_win); f_frame.pack(fill='both', expand=True, padx=20)

        def update_status():
            ctype = INV_TYPE_MAP.get(var_type.get())
            for k, e in entries.items():
                # [변경] 미관련 설정 음영 처리 로직
                disable = (ctype == 'C' and k in ['port', 'user', 'pw', 'ru_cmd', 'ru_pw']) or \
                          (ctype == 'S' and k == 'baud_rate') or \
                          (ctype in ['R', 'M', 'P'] and k in ['port', 'baud_rate', 'ru_cmd', 'ru_pw'])
                if disable: e.config(state='disabled', bg='#e0e0e0')
                else: e.config(state='normal', bg='white')

        for i, (label_text, key) in enumerate(fields):
            tk.Label(f_frame, text=label_text, font=('Arial', 8, 'bold'), fg="#546e7a").pack(anchor='w', pady=(5,0))
            entry = tk.Entry(f_frame, show="*" if "pw" in key or "Password" in label_text else "", width=50, bd=1, relief="solid")
            entry.insert(0, str(self.data.get(key, '')))
            entry.pack(pady=2)
            entries[key] = entry
        
        update_status()

        def save():
            ctype = INV_TYPE_MAP.get(var_type.get())
            if ctype == 'C' and not re.match(r'^\d+$', entries['ip'].get()):
                messagebox.showerror("Error", "COM 포트는 숫자만 입력하세요."); return
            for k, v in entries.items(): self.data[k] = v.get()
            self.data['type'] = ctype
            self.app.save_all_configs(); config_win.destroy(); self.app.refresh_grid()
        
        tk.Button(config_win, text="Save Settings", command=save, bg="#1976d2", fg="white", font=('Arial', 9, 'bold'), width=20, pady=10).pack(pady=20)

    def run_ping(self):
        target = self.data.get('ip', '')
        if not target or self.data['type'] == "C": return
        ping_exe = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'ping.exe')
        try:
            res = subprocess.run([ping_exe if os.path.exists(ping_exe) else 'ping', '-n', '1', target], stdout=subprocess.PIPE, shell=True, text=True)
            messagebox.showinfo("Ping", "OK" if res.returncode == 0 else "No Reply")
        except: pass

    def run_connection(self):
        d = self.data
        try:
            if d['type'] == "S":
                with open(MACRO_PATH, 'w', encoding='ascii') as f:
                    f.write(f"connect '{d['ip']}:{d.get('port','22')} /ssh /auth=password /user={d['user']} /passwd={d['pw']}'\n")
                    f.write("pause 1\n")
                    if d.get('ru_cmd'):
                        f.write(f"sendln '{d['ru_cmd']}'\n"); f.write("wait 'password:' 'Password:'\n")
                        f.write(f"sendln '{d.get('ru_pw', d['pw'])}'\n"); f.write("pause 1\n")
                    if d.get('final_cmd'):
                        for c in d['final_cmd'].split(","):
                            if c.strip(): f.write(f"sendln '{c.strip()}'\n"); f.write("pause 1\n")
                    f.write("end\n")
                subprocess.Popen(f'"{TERATERM_PATH}" /M="{MACRO_PATH}"', shell=True)
            elif d['type'] == "C":
                com = re.sub(r'[^0-9]', '', d['ip'])
                subprocess.Popen(f'"{TERATERM_PATH}" /C={com} /BAUD={d.get("baud_rate","115200")}', shell=True)
            elif d['type'] in ["R", "M", "P"]:
                if d['type'] == "R":
                    subprocess.run(f'cmdkey /generic:TERMSRV/{d["ip"]} /user:{d["user"]} /pass:{d["pw"]}', shell=True)
                    subprocess.Popen(f'mstsc /v:{d["ip"]}', shell=True)
                elif d['type'] == "M":
                    for b in d['name'].split(","):
                        if b.strip(): subprocess.Popen([MOBA_PATH, "-bookmark", b.strip()]); time.sleep(1.2)
                elif d['type'] == "P":
                    p = d['ip'].strip()
                    if os.path.exists(p):
                        if p.lower().endswith('.py'): subprocess.Popen(['python', p], shell=True)
                        else: subprocess.Popen([p], shell=True)
        except Exception as e: messagebox.showerror("Error", str(e))

    def on_drag_start(self, event): self.outer_frame.config(bg="#90a4ae"); self.app.dragging_item = self
    def on_drag_motion(self, event):
        mx = event.x_root - self.app.grid_frame.winfo_rootx(); gw = self.app.grid_frame.winfo_width()
        if gw > 1:
            tc = int(mx // (gw / self.app.col_count))
            if 0 <= tc < self.app.col_count and self.data['col'] != tc:
                self.data['col'] = tc; self.app.refresh_grid_light()
    def on_drag_release(self, event):
        my = event.y_root - self.app.grid_frame.winfo_rooty(); tc = self.data['col']
        others = [s for s in self.app.sessions if s.data['col'] == tc and s != self]
        idx = 0
        for s in others:
            if s.outer_frame and my > s.outer_frame.winfo_y() + (s.outer_frame.winfo_height()/2): idx += 1
        self.app.sessions.remove(self); pos = 0; fnd = 0
        for i, s in enumerate(self.app.sessions):
            if s.data['col'] == tc:
                if fnd == idx: break
                fnd += 1
            pos = i + 1
        self.app.sessions.insert(pos, self); self.app.save_all_configs(); self.app.refresh_grid()

    def copy_session(self):
        new_data = self.data.copy(); new_data['name'] += "_copy"
        self.app.sessions.append(SessionRow(None, self.app, new_data)); self.app.save_all_configs(); self.app.refresh_grid()
    def delete_self(self):
        if messagebox.askyesno("Delete", "Delete session?"):
            self.app.sessions.remove(self); self.app.save_all_configs(); self.app.refresh_grid()

class MainApp:
    def __init__(self, root):
        self.root = root; self.root.title("DashStation v7.0"); self.root.geometry("1200x900")
        if os.path.exists(ICON_FILE): self.root.iconbitmap(ICON_FILE)
        self.sessions, self.col_count, self.zone_names = [], 4, {}
        self.main_container = tk.Frame(self.root); self.main_container.pack(fill='both', expand=True)
        self.load_zone_names(); self.load_configs(); self.refresh_grid()

    def load_zone_names(self):
        if os.path.exists(ZONE_NAME_FILE):
            try:
                with open(ZONE_NAME_FILE, 'r', encoding='utf-8') as f:
                    self.zone_names = {int(k): v for k, v in json.load(f).items()}
            except: pass
    def save_zone_names(self):
        with open(ZONE_NAME_FILE, 'w', encoding='utf-8') as f: json.dump(self.zone_names, f, ensure_ascii=False, indent=4)
    def rename_zone(self, col_idx):
        curr = self.zone_names.get(col_idx, f"ZONE {col_idx+1}"); new = simpledialog.askstring("Rename Zone", f"New name for '{curr}':", initialvalue=curr)
        if new: self.zone_names[col_idx] = new; self.save_zone_names(); self.refresh_grid()
    def load_configs(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data_list = json.load(f)
                    for d in data_list: self.sessions.append(SessionRow(None, self, d))
                    if self.sessions: self.col_count = max([s.data.get('col', 0) for s in self.sessions]) + 1
            except: pass

    def refresh_grid_light(self):
        rc = [1] * self.col_count
        for s in self.sessions:
            c = min(s.data.get('col', 0), self.col_count-1)
            if s.outer_frame: s.outer_frame.grid(row=rc[c], column=c, padx=2, pady=2, sticky='nsew'); rc[c]+=1
            
    def refresh_grid(self):
        for widget in self.main_container.winfo_children(): widget.destroy()
        top_bar = tk.Frame(self.main_container, bg="#212121", pady=10); top_bar.pack(fill='x', side='top')
        l_box = tk.Frame(top_bar, bg="#212121"); l_box.pack(side='left', padx=20)
        btn_s = {"font": ('Segoe UI', 9, 'bold'), "fg": "white", "width": 8, "bd": 0, "cursor": "hand2"}
        tk.Button(l_box, text="+ ADD", command=self.add_session_ui, bg="#2e7d32", **btn_s).pack(side='left', padx=5)
        tk.Button(l_box, text="+ COL", command=self.add_column, bg="#455a64", **btn_s).pack(side='left', padx=5)
        tk.Button(l_box, text="- COL", command=self.remove_column, bg="#455a64", **btn_s).pack(side='left', padx=5)
        tk.Label(top_bar, text="DASHSTATION v7.0", fg="#00e5ff", bg="#212121", font=('Segoe UI', 10, 'bold')).pack(side='right', padx=20)
        
        main_canvas = tk.Canvas(self.main_container, highlightthickness=0, bg="#f5f7f9")
        self.grid_frame = tk.Frame(main_canvas, bg="#f5f7f9", padx=10, pady=10)
        sb = tk.Scrollbar(self.main_container, command=main_canvas.yview); main_canvas.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y'); main_canvas.pack(side='left', fill='both', expand=True)
        main_canvas.create_window((0,0), window=self.grid_frame, anchor='nw', tags="f"); self.grid_frame.bind("<Configure>", lambda e: main_canvas.config(scrollregion=main_canvas.bbox("all")))
        main_canvas.bind("<Configure>", lambda e: main_canvas.itemconfig("f", width=e.width))
        
        for i in range(self.col_count):
            self.grid_frame.columnconfigure(i, weight=1, uniform="group")
            h = tk.Frame(self.grid_frame, bg="#eceff1", pady=8); h.grid(row=0, column=i, sticky='nsew', padx=4, pady=(0, 15))
            tk.Label(h, text=self.zone_names.get(i, f"ZONE {i+1}"), fg="#546e7a", bg="#eceff1", font=('Segoe UI', 9, 'bold')).pack()
            h.bind("<Button-1>", lambda e, idx=i: self.rename_zone(idx))
            
        rc = [1] * self.col_count
        for s in self.sessions:
            c = min(s.data.get('col', 0), self.col_count-1)
            w = s.create_widget(self.grid_frame); w.grid(row=rc[c], column=c, padx=2, pady=2, sticky='nsew'); rc[c]+=1

    def add_session_ui(self): self.sessions.append(SessionRow(None, self)); self.refresh_grid()
    def add_column(self): self.col_count += 1; self.refresh_grid()
    def remove_column(self):
        if self.col_count > 1:
            self.col_count -= 1; [s.data.update({'col': self.col_count-1}) for s in self.sessions if s.data['col'] >= self.col_count]
            self.refresh_grid(); self.save_all_configs()
    def save_all_configs(self):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump([s.data for s in self.sessions], f, indent=4)

if __name__ == "__main__":
    app_root = tk.Tk(); app = MainApp(app_root); app_root.mainloop()