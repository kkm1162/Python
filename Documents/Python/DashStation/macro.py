import tkinter as tk
from tkinter import messagebox
import subprocess
import json
import os
import time

# 1. 경로 설정
CONFIG_FILE = "session_config.json"
TERATERM_PATH = r"C:\Program Files (x86)\teraterm\ttermpro.exe"
MOBA_PATH = r"C:\Program Files (x86)\Mobatek\MobaXterm\MobaXterm.exe"

class SessionRow:
    def __init__(self, master, data=None):
        self.master = master
        self.data = data if data else {'name': '새 세션', 'type': 'SSH(Tera)', 'ip': '', 'user': '', 'pw': ''}
        
        self.frame = tk.Frame(master)
        self.frame.pack(fill='x', padx=10, pady=5)

        # [수정] command 부분을 lambda로 감싸서 실행 시점을 명확히 강제함
        self.btn_connect = tk.Button(
            self.frame, width=35, height=2, 
            command=lambda: self.run_connection(), 
            font=('Malgun Gothic', 9, 'bold')
        )
        self.update_button_ui()
        self.btn_connect.pack(side='left', padx=5)

        self.btn_config = tk.Button(self.frame, text="⚙ 설정", command=self.open_config, height=2)
        self.btn_config.pack(side='left', padx=5)

    def update_button_ui(self):
        t = self.data.get('type', 'SSH(Tera)')
        n = self.data.get('name', '새 세션')
        i = self.data.get('ip', '')
        display_text = f"[{t}] {n}\n({i})" if t != "Moba(북마크)" else f"[{t}]\n{n}"
        colors = {"SSH(Tera)": "#E3F2FD", "RDP": "#F3E5F5", "Moba(IP)": "#FFF3E0", "Moba(북마크)": "#E8F5E9"}
        self.btn_connect.config(text=display_text, bg=colors.get(t, "#FFFFFF"))

    def open_config(self):
        config_win = tk.Toplevel(self.master)
        config_win.title("설정")
        config_win.geometry("350x400")
        entries = {}
        for idx, (label, key) in enumerate([("이름", "name"), ("IP", "ip"), ("ID", "user"), ("PW", "pw")]):
            tk.Label(config_win, text=label).grid(row=idx, column=0, padx=10, pady=10)
            e = tk.Entry(config_win, width=25)
            e.insert(0, self.data.get(key, ''))
            e.grid(row=idx, column=1)
            entries[key] = e
        tk.Label(config_win, text="타입").grid(row=4, column=0)
        var_type = tk.StringVar(value=self.data.get('type', 'SSH(Tera)'))
        tk.OptionMenu(config_win, var_type, "SSH(Tera)", "RDP", "Moba(IP)", "Moba(북마크)").grid(row=4, column=1)

        def save():
            for k, v in entries.items(): self.data[k] = v.get()
            self.data['type'] = var_type.get()
            self.update_button_ui()
            save_all_configs()
            config_win.destroy()
        tk.Button(config_win, text="저장", command=save, bg="#C8E6C9", width=15).grid(row=5, columnspan=2, pady=20)

    def run_connection(self):
        # 실행 직전 데이터를 변수에 명확히 할당
        c_type = self.data.get('type')
        c_ip = self.data.get('ip')
        c_user = self.data.get('user')
        c_pw = self.data.get('pw')
        c_name = self.data.get('name')

        try:
            if c_type == "SSH(Tera)":
                # [수정] 가장 원시적이고 확실한 커맨드 조합으로 복구 (경로 따옴표 강조)
                # shell=True를 사용하므로 문자열 전체를 따옴표로 묶는 방식 사용
                teraterm_cmd = f'"{TERATERM_PATH}" {c_ip}:22 /ssh /auth=password /user={c_user} /passwd={c_pw}'
                subprocess.Popen(teraterm_cmd, shell=True)
            
            elif c_type == "RDP":
                subprocess.run(f'cmdkey /generic:TERMSRV/{c_ip} /user:{c_user} /pass:{c_pw}', shell=True)
                subprocess.Popen(f'mstsc /v:{c_ip}', shell=True)
                
            elif c_type == "Moba(IP)":
                moba_ip_cmd = f'"{MOBA_PATH}" -newtab "ssh -l {c_user} {c_ip}"'
                subprocess.Popen(moba_ip_cmd, shell=True)

            elif c_type == "Moba(북마크)":
                bookmarks = [b.strip() for b in c_name.split(",") if b.strip()]
                for i, b in enumerate(bookmarks):
                    moba_bm_cmd = f'"{MOBA_PATH}" -bookmark "{b}"'
                    subprocess.Popen(moba_bm_cmd, shell=True)
                    if i < len(bookmarks) - 1: time.sleep(1.5)
        except Exception as e:
            messagebox.showerror("실행 오류", str(e))

# --- 앱 메인 (수정 없음) ---
class MainApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Integrated Launcher Final")
        self.root.geometry("500x700")
        self.sessions = []
        self.canvas = tk.Canvas(root)
        self.scrollbar = tk.Scrollbar(root, orient="vertical", command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.create_window((0,0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        tk.Button(root, text="➕ 세션 추가", command=self.add_session, bg="#FFEB3B", pady=10).pack(fill='x')
        self.load_configs()
    def add_session(self, data=None):
        row = SessionRow(self.list_frame, data)
        self.sessions.append(row)
    def load_configs(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                for item in d: self.add_session(item)

def save_all_configs():
    data = [s.data for s in app.sessions]
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    app_root = tk.Tk()
    app = MainApp(app_root)
    app_root.mainloop()