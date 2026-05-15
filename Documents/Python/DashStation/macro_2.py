import tkinter as tk
from tkinter import messagebox
import subprocess
import json
import os

CONFIG_FILE = "session_config.json"
TERATERM_PATH = r"C:\Program Files (x86)\teraterm\ttermpro.exe"

class SessionRow:
    def __init__(self, master, index, data=None):
        self.master = master
        self.index = index
        # 기본 데이터 구조
        self.data = data if data else {'name': f'세션 {index+1}', 'type': 'SSH', 'ip': '', 'user': '', 'pw': ''}
        
        self.frame = tk.Frame(master)
        self.frame.pack(fill='x', padx=10, pady=5)

        # 접속 버튼 생성 및 초기화
        self.btn_connect = tk.Button(self.frame, width=30, height=2, command=self.run_connection, font=('Arial', 9, 'bold'))
        self.update_button_ui() # 버튼 텍스트와 색상을 설정하는 함수
        self.btn_connect.pack(side='left', padx=5)

        # 설정 버튼
        self.btn_config = tk.Button(self.frame, text="⚙ 설정", command=self.open_config, height=2)
        self.btn_config.pack(side='left', padx=5)

    def update_button_ui(self):
        """버튼의 텍스트와 색상을 타입에 맞게 업데이트"""
        display_text = f"[{self.data['type']}] {self.data['name']}\n({self.data['ip']})"
        
        # 타입에 따른 색상 구분 (SSH: 연파랑, RDP: 연보라)
        bg_color = "#e3f2fd" if self.data['type'] == "SSH" else "#f3e5f5"
        
        self.btn_connect.config(text=display_text, bg=bg_color)

    def open_config(self):
        config_win = tk.Toplevel(self.master)
        config_win.title("접속 정보 설정")
        config_win.geometry("320x350")

        fields = [("이름", "name"), ("IP 주소", "ip"), ("ID", "user"), ("PW", "pw")]
        entries = {}

        for i, (label, key) in enumerate(fields):
            tk.Label(config_win, text=label).grid(row=i, column=0, pady=10, padx=10)
            entry = tk.Entry(config_win, show="*" if key == "pw" else "")
            entry.insert(0, self.data[key])
            entry.grid(row=i, column=1)
            entries[key] = entry

        tk.Label(config_win, text="연결 타입:").grid(row=4, column=0, pady=10)
        var_type = tk.StringVar(value=self.data['type'])
        type_menu = tk.OptionMenu(config_win, var_type, "SSH", "RDP")
        type_menu.grid(row=4, column=1, sticky='w')

        def save():
            for key, entry in entries.items():
                self.data[key] = entry.get()
            self.data['type'] = var_type.get()
            
            self.update_button_ui() # UI 즉시 갱신
            save_all_configs()
            config_win.destroy()

        tk.Button(config_win, text="저장 및 적용", command=save, bg="#c8e6c9", width=15).grid(row=5, columnspan=2, pady=20)

    def run_connection(self):
        ip, user, pw = self.data['ip'], self.data['user'], self.data['pw']
        if not ip or not user or not pw:
            messagebox.showwarning("정보 부족", "설정에서 모든 정보를 입력해주세요.")
            return

        if self.data['type'] == "SSH":
            # SSH 연결 시 /ssh 옵션 사용 (이전 이슈 해결)
            cmd = f'"{TERATERM_PATH}" {ip}:22 /ssh /auth=password /user={user} /passwd={pw}'
            subprocess.Popen(cmd, shell=True)
        
        elif self.data['type'] == "RDP":
            # RDP 자동 로그인 처리
            subprocess.run(f'cmdkey /generic:TERMSRV/{ip} /user:{user} /pass:{pw}', shell=True)
            subprocess.Popen(f'mstsc /v:{ip}', shell=True)

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("One-Key Multi Connect (Improved UI)")
        self.root.geometry("450x600")
        
        self.sessions = []
        
        header = tk.Frame(root, bg="#263238")
        header.pack(fill='x')
        tk.Label(header, text="통합 원클릭 접속기", fg="white", bg="#263238", font=('Arial', 14, 'bold'), pady=15).pack()

        # 스크롤 가능한 영역을 만들고 싶다면 여기에 추가 가능 (일단 기본 프레임)
        self.list_frame = tk.Frame(root)
        self.list_frame.pack(fill='both', expand=True, pady=10)

        footer = tk.Frame(root)
        footer.pack(fill='x', side='bottom', pady=20)
        self.btn_add = tk.Button(footer, text="➕ 새로운 접속 세션 추가", command=self.add_session, 
                                 bg="#fff9c4", font=('Arial', 10, 'bold'), padx=20, pady=10)
        self.btn_add.pack()

        self.load_configs()

    def add_session(self, data=None):
        new_session = SessionRow(self.list_frame, len(self.sessions), data)
        self.sessions.append(new_session)

    def load_configs(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
                for data in saved_data:
                    self.add_session(data)

def save_all_configs():
    data_to_save = [s.data for s in app.sessions]
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    app_root = tk.Tk()
    app = App(app_root)
    app_root.mainloop()