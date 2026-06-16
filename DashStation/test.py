import tkinter as tk
from tkinter import messagebox, simpledialog, filedialog
import subprocess
import json
import os
import sys
import time
import platform
import re

# [절대 경로 설정] 프로그램/스크립트의 위치를 기준으로 경로를 고정합니다.
if getattr(sys, 'frozen', False):
    # .exe 파일로 실행 중일 때
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # .py 스크립트로 실행 중일 때
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "session_config.json")
ZONE_NAME_FILE = os.path.join(BASE_DIR, "zone_names.json")
APP_SETTING_FILE = os.path.join(BASE_DIR, "app_settings.json")
MACRO_PATH = os.path.join(os.environ['TEMP'], "at_macro.ttl")
ICON_FILE = os.path.join(BASE_DIR, "app_icon1.ico")

TYPE_MAP = {"S": "SSH (TeraTerm)", "R": "RDP (Remote)", "M": "MobaXterm (BM)", "P": "Python / EXE", "C": "Serial (COM Port)"}
CONNECT_TEXT_MAP = {"S": "SSH CONNECT", "R": "RDP CONNECT", "M": "MOBA CONNECT", "P": "EXE / PY CONNECT", "C": "SERIAL CONNECT"}
INV_TYPE_MAP = {v: k for k, v in TYPE_MAP.items()}

class SessionRow:
    def __init__(self, master, app_instance, data=None):
        self.master = master
        self.app = app_instance
        self.data = data if data else {}
        # 기존 데이터 로드 시 누락된 필드 자동 보정
        defaults = {
            'name': '', 'type': 'S', 'ip': '', 'port': '22', 
            'baud_rate': '115200', 'user': '', 'pw': '', 
            'ru_cmd': '', 'ru_pw': '', 'final_cmd': '', 'col': 0
        }
        for key, val in defaults.items():
            if key not in self.data:
                self.data[key] = val
        self.outer_frame = None

    def create_widget(self, master):
        self.outer_frame = tk.Frame(master, bg="#f5f7f9", padx=6, pady=6)
        self.frame = tk.Frame(self.outer_frame, bd=0, bg="#ffffff", highlightthickness=1, highlightbackground="#cfd8dc")
        self.frame.pack(fill='both', expand=True)
        
        t = self.data.get('type', 'S')
        color = {"S": "#1976d2", "R": "#9c27b0", "M": "#2e7d32", "P": "#fbc02d", "C": "#e64a19"}.get(t, "#455a64")
        tk.Frame(self.frame, bg=color, width=5).pack(side='left', fill='y')

        content = tk.Frame(self.frame, bg="#ffffff", padx=10, pady=8)
        content.pack(side='left', fill='both', expand=True)

        header = tk.Frame(content, bg="#ffffff")
        header.pack(fill='x', side='top')
        
        btn_box = tk.Frame(header, bg="#ffffff")
        btn_box.pack(side='right')
        btn_s = {"font": ('Arial', 8), "bd": 0, "bg": "#ffffff", "cursor": "hand2"}
        tk.Button(btn_box, text="⚡", command=self.run_ping, fg="#ffa000", **btn_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="⚙", command=self.open_config, fg="#90a4ae", **btn_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="⧉", command=self.copy_session, fg="#4caf50", **btn_s).pack(side='left', padx=2)
        tk.Button(btn_box, text="×", command=self.delete_self, fg="#f44336", **btn_s).pack(side='left', padx=2)

        self.lbl_name = tk.Label(header, text=self.data.get('name', 'New'), font=('Segoe UI', 10, 'bold'), bg="#ffffff", fg="#263238", anchor='w', cursor="fleur")
        self.lbl_name.pack(side='left', fill='x', expand=True)
        self.lbl_name.bind("<Button-1>", self.on_drag_start)
        self.lbl_name.bind("<B1-Motion>", self.on_drag_motion)
        self.lbl_name.bind("<ButtonRelease-1>", self.on_drag_release)

        info = self.data.get('ip', '0.0.0.0')
        if t == "S": info += f":{self.data.get('port', '22')}"
        elif t == "C": info = f"COM{info} ({self.data.get('baud_rate', '115200')})"

        tk.Label(content, text=info, font=('Consolas', 9), bg="#ffffff", fg="#78909c", anchor='w').pack(fill='x', pady=(2, 5))
        tk.Button(content, text=CONNECT_TEXT_MAP.get(t, "CONNECT"), command=self.run_connection, font=('Segoe UI', 7, 'bold'), bg="#f8f9fa", fg=color, relief="flat", highlightthickness=1, highlightbackground=color, pady=2).pack(fill='x')
        return self.outer_frame

    def open_config(self):
        win = tk.Toplevel(self.app.root); win.title("세션 설정"); win.geometry("460x720")
        fields = [("세션 이름", "name"), ("IP / COM 번호", "ip"), ("SSH 포트", "port"), ("Baud Rate", "baud_rate"), ("ID", "user"), ("PW", "pw"), ("RU 자동 명령", "ru_cmd"), ("RU PW", "ru_pw"), ("추가 명령", "final_cmd")]
        entries = {}
        tk.Label(win, text="[ 접속 프로토콜 설정 ]", font=('Arial', 9, 'bold')).pack(pady=10)
        var_type = tk.StringVar(value=TYPE_MAP.get(self.data['type']))
        tk.OptionMenu(win, var_type, *TYPE_MAP.values(), command=lambda _: update()).pack()
        f = tk.Frame(win); f.pack(padx=20, pady=10)

        def update():
            ctype = INV_TYPE_MAP.get(var_type.get())
            for k, e in entries.items():
                dis = (ctype == 'C' and k in ['port', 'user', 'pw', 'ru_cmd', 'ru_pw']) or (ctype == 'S' and k == 'baud_rate') or (ctype in ['R', 'M', 'P'] and k in ['port', 'baud_rate', 'ru_cmd', 'ru_pw'])
                e.config(state='disabled', bg='#e0e0e0') if dis else e.config(state='normal', bg='white')

        for label, key in fields:
            tk.Label(f, text=label, font=('Arial', 8, 'bold'), fg="#546e7a").pack(anchor='w', pady=(3,0))
            ent = tk.Entry(f, width=50, bd=1, relief="solid", show="*" if "pw" in key else "")
            ent.insert(0, str(self.data.get(key, ''))); ent.pack(pady=2); entries[key] = ent
        update()

        def save():
            ctype = INV_TYPE_MAP.get(var_type.get())
            if ctype == 'C' and not re.match(r'^\d+$', entries['ip'].get()):
                messagebox.showerror("Error", "COM 번호는 숫자만!"); return
            for k, v in entries.items(): self.data[k] = v.get()
            self.data['type'] = ctype; self.app.save_all_configs(); win.destroy(); self.app.refresh_grid()
        tk.Button(win, text="Save Settings", command=save, bg="#1976d2", fg="white", font=('Arial', 9, 'bold'), width=20, pady=10).pack(pady=15)

    def run_connection(self):
        d = self.data; tt_path = self.app.settings.get("teraterm_path"); moba_path = self.app.settings.get("moba_path")
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
                subprocess.Popen(f'"{tt_path}" /M="{MACRO_PATH}"', shell=True)
            elif d['type'] == "C":
                com = re.sub(r'[^0-9]', '', d['ip'])
                subprocess.Popen(f'"{tt_path}" /C={com} /BAUD={d.get("baud_rate","115200")}', shell=True)
            elif d['type'] == "M":
                for b in d['name'].split(","):
                    if b.strip(): subprocess.Popen([moba_path, "-bookmark", b.strip()]); time.sleep(1.2)
            elif d['type'] == "R":
                subprocess.run(f'cmdkey /generic:TERMSRV/{d["ip"]} /user:{d["user"]} /pass:{d["pw"]}', shell=True)
                subprocess.Popen(f'mstsc /v:{d["ip"]}', shell=True)
            elif d['type'] == "P":
                p = d['ip'].strip()
                if os.path.exists(p):
                    if p.lower().endswith('.py'): subprocess.Popen(['python', p], shell=True)
                    else: subprocess.Popen([p], shell=True)
        except Exception as e: messagebox.showerror("Error", str(e))

    def run_ping(self):
        t = self.data.get('ip', '')
        if not t or self.data['type'] == 'C': return
        try:
            res = subprocess.run(['ping', '-n', '1', t], stdout=subprocess.PIPE, shell=True, text=True)
            messagebox.showinfo("Ping", "OK" if res.returncode == 0 else "No Reply")
        except: pass
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
        new = self.data.copy(); new['name'] += "_copy"; self.app.sessions.append(SessionRow(None, self.app, new)); self.app.save_all_configs(); self.app.refresh_grid()
    def delete_self(self):
        if messagebox.askyesno("Delete", "Delete session?"): self.app.sessions.remove(self); self.app.save_all_configs(); self.app.refresh_grid()

class MainApp:
    def __init__(self, root):
        self.root = root; self.root.title("DashStation v7.1.2"); self.root.geometry("1200x900")
        if os.path.exists(ICON_FILE): self.root.iconbitmap(ICON_FILE)
        self.sessions, self.col_count, self.zone_names, self.settings = [], 4, {}, {}
        self.load_settings(); self.main_container = tk.Frame(self.root); self.main_container.pack(fill='both', expand=True)
        self.load_zone_names(); self.load_configs(); self.refresh_grid()

    def load_settings(self):
        if os.path.exists(APP_SETTING_FILE):
            try:
                with open(APP_SETTING_FILE, 'r', encoding='utf-8') as f: self.settings = json.load(f)
            except: pass
        if "teraterm_path" not in self.settings: self.settings["teraterm_path"] = r"C:\Program Files (x86)\teraterm\ttermpro.exe"
        if "moba_path" not in self.settings: self.settings["moba_path"] = r"C:\Program Files (x86)\Mobatek\MobaXterm\MobaXterm.exe"

    def open_app_settings(self):
        win = tk.Toplevel(self.root); win.title("Global Settings"); win.geometry("600x300")
        tk.Label(win, text="[ 전역 프로그램 경로 설정 ]", font=('Arial', 10, 'bold')).pack(pady=15)
        f1 = tk.Frame(win); f1.pack(fill='x', padx=30, pady=5)
        tk.Label(f1, text="TeraTerm:").pack(side='left'); e1 = tk.Entry(f1, width=50); e1.insert(0, self.settings["teraterm_path"]); e1.pack(side='left', padx=5)
        tk.Button(f1, text="찾기", command=lambda: [p := filedialog.askopenfilename(), e1.delete(0, 'end'), e1.insert(0, p) if p else None]).pack(side='left')
        f2 = tk.Frame(win); f2.pack(fill='x', padx=30, pady=5)
        tk.Label(f2, text="MobaXterm:").pack(side='left'); e2 = tk.Entry(f2, width=50); e2.insert(0, self.settings["moba_path"]); e2.pack(side='left', padx=5)
        tk.Button(f2, text="찾기", command=lambda: [p := filedialog.askopenfilename(), e2.delete(0, 'end'), e2.insert(0, p) if p else None]).pack(side='left')
        def save():
            self.settings["teraterm_path"] = e1.get(); self.settings["moba_path"] = e2.get()
            with open(APP_SETTING_FILE, 'w', encoding='utf-8') as f: json.dump(self.settings, f, indent=4)
            win.destroy(); messagebox.showinfo("Saved", "설정이 저장되었습니다.")
        tk.Button(win, text="Save Settings", command=save, bg="#212121", fg="white", width=20, pady=10).pack(pady=20)

    def refresh_grid(self):
        for w in self.main_container.winfo_children(): w.destroy()
        top = tk.Frame(self.main_container, bg="#212121", pady=10); top.pack(fill='x')
        l_box = tk.Frame(top, bg="#212121"); l_box.pack(side='left', padx=20)
        btn_s = {"font": ('Segoe UI', 9, 'bold'), "fg": "white", "width": 8, "bd": 0, "cursor": "hand2"}
        tk.Button(l_box, text="+ ADD", command=self.add_session_ui, bg="#2e7d32", **btn_s).pack(side='left', padx=5)
        tk.Button(l_box, text="+ COL", command=self.add_column, bg="#455a64", **btn_s).pack(side='left', padx=5)
        tk.Button(l_box, text="- COL", command=self.remove_column, bg="#455a64", **btn_s).pack(side='left', padx=5)
        tk.Button(top, text="⚙ APP SET", command=self.open_app_settings, bg="#ffa000", **btn_s).pack(side='right', padx=10)
        tk.Label(top, text="DASHSTATION v7.1.2", fg="#00e5ff", bg="#212121", font=('Segoe UI', 10, 'bold')).pack(side='right', padx=10)
        c = tk.Canvas(self.main_container, bg="#f5f7f9"); sb = tk.Scrollbar(self.main_container, command=c.yview)
        self.grid_frame = tk.Frame(c, bg="#f5f7f9", padx=10, pady=10); cw = c.create_window((0,0), window=self.grid_frame, anchor='nw', tags="f")
        c.config(yscrollcommand=sb.set); c.bind("<Configure>", lambda e: (c.itemconfig(cw, width=e.width), c.config(scrollregion=c.bbox("all"))))
        c.pack(side='left', fill='both', expand=True); sb.pack(side='right', fill='y')
        for i in range(self.col_count):
            self.grid_frame.columnconfigure(i, weight=1, uniform="g")
            h = tk.Frame(self.grid_frame, bg="#eceff1", pady=8); h.grid(row=0, column=i, sticky='nsew', padx=4, pady=(0,15))
            tk.Label(h, text=self.zone_names.get(i, f"ZONE {i+1}"), fg="#546e7a", bg="#eceff1", font=('Segoe UI', 9, 'bold')).pack()
            h.bind("<Button-1>", lambda e, idx=i: self.rename_zone(idx))
        rc = [1] * self.col_count
        for s in self.sessions:
            col = min(s.data.get('col', 0), self.col_count-1)
            s.create_widget(self.grid_frame).grid(row=rc[col], column=col, padx=2, pady=2, sticky='nsew'); rc[col]+=1

    def load_zone_names(self):
        if os.path.exists(ZONE_NAME_FILE):
            try:
                with open(ZONE_NAME_FILE, 'r', encoding='utf-8') as f: self.zone_names = {int(k): v for k, v in json.load(f).items()}
            except: pass
    def save_zone_names(self):
        with open(ZONE_NAME_FILE, 'w', encoding='utf-8') as f: json.dump(self.zone_names, f, indent=4)
    def rename_zone(self, idx):
        curr = self.zone_names.get(idx, f"ZONE {idx+1}"); new = simpledialog.askstring("Rename", "Name:", initialvalue=curr)
        if new: self.zone_names[idx] = new; self.save_zone_names(); self.refresh_grid()
    def load_configs(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        for d in data: self.sessions.append(SessionRow(None, self, d))
                        if self.sessions: self.col_count = max(4, max([s.data.get('col', 0) for s in self.sessions]) + 1)
            except Exception as e: messagebox.showerror("Load Error", str(e))
    def refresh_grid_light(self):
        rc = [1] * self.col_count
        for s in self.sessions:
            c = min(s.data.get('col', 0), self.col_count-1)
            if s.outer_frame: s.outer_frame.grid(row=rc[c], column=c, padx=2, pady=2, sticky='nsew'); rc[c]+=1
    def add_session_ui(self): self.sessions.append(SessionRow(None, self)); self.refresh_grid()
    def add_column(self): self.col_count += 1; self.refresh_grid()
    def remove_column(self):
        if self.col_count > 1:
            self.col_count -= 1; [s.data.update({'col': self.col_count-1}) for s in self.sessions if s.data['col'] >= self.col_count]
            self.refresh_grid(); self.save_all_configs()
    def save_all_configs(self):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump([s.data for s in self.sessions], f, indent=4)

if __name__ == "__main__":
    r = tk.Tk(); app = MainApp(r); r.mainloop()