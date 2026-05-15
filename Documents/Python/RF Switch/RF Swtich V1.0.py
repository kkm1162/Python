import tkinter as tk
from tkinter import ttk, messagebox
import threading
import json
import os
import sys

# 실행 파일(.exe) 또는 스크립트(.py)의 위치를 기준으로 경로를 설정합니다.
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class RFApp:
    def __init__(self, root):
        self.root = root
        self.root.title("RF Switch v1.0")
        self.root.geometry("500x520") 
        self.root.resizable(False, False)
                
        self.CLR_BG, self.CLR_CARD = "#f5f6fa", "#ffffff"
        self.CLR_TEXT, self.CLR_ACCENT1 = "#2f3640", "#0984e3" 
        self.CLR_ACCENT2, self.CLR_SYNC = "#e17055", "#487eb0"   
        self.CLR_SUCCESS, self.CLR_OFF = "#2ecc71", "#dcdde1"
        self.CLR_BRIGHT_SYNC = "#00d2d3" 
        
        self.root.configure(bg=self.CLR_BG)
        self.remote_cfg_file = "/home/debian/rf_switch_shared.json"
        self.local_cfg_file = os.path.join(BASE_DIR, "rf_config_local.json")
        self._paramiko = None
        self.h, self.u, self.p = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.is_busy = False
        self.st = tk.StringVar(value="● Disconnected")
        
        # Description 변수들
        self.d12 = [tk.StringVar(value=f"PATH {i+1}") for i in range(2)]
        self.d14 = [tk.StringVar(value=f"RF {i+1}") for i in range(4)]
        
        self.load_config()

        style = ttk.Style()
        style.theme_use('default')
        style.configure("TNotebook", background=self.CLR_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#ced6e0", foreground=self.CLR_TEXT, padding=[15, 2])
        style.map("TNotebook.Tab", background=[("selected", self.CLR_CARD)])

        self.nb = ttk.Notebook(root)
        self.nb.pack(expand=1, fill="both", padx=10, pady=5)
        self.t1, self.t2 = tk.Frame(self.nb, bg=self.CLR_CARD), tk.Frame(self.nb, bg=self.CLR_CARD)
        self.nb.add(self.t1, text=" CONTROL "), self.nb.add(self.t2, text=" CONFIG ")

        self.ui_main()
        self.ui_cfg()
        self._set_ui_disconnected()

    def ui_cfg(self):
        c = tk.Frame(self.t2, bg=self.CLR_CARD)
        c.place(relx=.5, rely=.5, anchor="center")
        
        
        fields = [("IP Address", self.h), ("ID", self.u), ("Password", self.p)]
        for i, (l, v) in enumerate(fields):
            tk.Label(c, text=l, fg=self.CLR_TEXT, bg=self.CLR_CARD, font=("Arial", 9, "bold")).grid(row=i, column=0, pady=5, sticky="e")
            e = tk.Entry(c, textvariable=v, width=22, bg="#f1f2f6", fg="#2f3640", borderwidth=1, relief="solid")
            if "Password" in l: e.config(show="*")
            e.grid(row=i, column=1, padx=15)

        # 2. Description 입력 (여기에 다시 넣었습니다!)
        tk.Label(c, text="── Port Description ──", fg=self.CLR_TEXT, bg=self.CLR_CARD, font=("Arial", 8, "bold")).grid(row=3, columnspan=2, pady=(15, 5))
        
        # 1:2 설명
        f_d12 = tk.Frame(c, bg=self.CLR_CARD)
        f_d12.grid(row=4, columnspan=2)
        for i in range(2):
            tk.Entry(f_d12, textvariable=self.d12[i], width=12, font=("Arial", 8)).pack(side="left", padx=5)

        # 1:4 설명
        f_d14 = tk.Frame(c, bg=self.CLR_CARD)
        f_d14.grid(row=5, columnspan=2, pady=5)
        for i in range(4):
            tk.Entry(f_d14, textvariable=self.d14[i], width=8, font=("Arial", 8)).pack(side="left", padx=2)
        
        # [복구] SAVE는 회색(#747d8c), CONNECT는 초록색(CLR_SUCCESS)
        btn_f = tk.Frame(c, bg=self.CLR_CARD)
        btn_f.grid(row=6, columnspan=2, pady=15)
        self.btn_save_cfg = tk.Button(btn_f, text="SAVE", bg="#747d8c", fg="white", font=("Arial", 9, "bold"), width=10, command=self.save_config)
        self.btn_connect_cfg = tk.Button(btn_f, text="CONNECT", bg=self.CLR_SUCCESS, fg="white", font=("Arial", 9, "bold"), width=10, command=self.conn)
        self.btn_save_cfg.pack(side="left", padx=5)
        self.btn_connect_cfg.pack(side="left", padx=5)

    def ui_main(self):
        top_f = tk.Frame(self.t1, bg="#dfe4ea")
        top_f.pack(fill="x")
        self.bar = tk.Label(top_f, textvariable=self.st, fg="#ff4757", bg="#dfe4ea", font=("Arial", 9, "bold"), pady=8)
        self.bar.pack(side="left", padx=10)
        
        # Disconnect 버튼
        self.btn_q_disconn = tk.Button(top_f, text="DISCONNECT", bg=self.CLR_ACCENT2, fg="white", font=("Arial", 8, "bold"), relief="flat", command=self.disconnect)
        self.btn_q_disconn.pack(side="right", padx=(0, 10), pady=5)
        # Connect 버튼
        self.btn_q_conn = tk.Button(top_f, text="CONNECT", bg=self.CLR_SUCCESS, fg="white", font=("Arial", 8, "bold"), relief="flat", command=self.conn)
        self.btn_q_conn.pack(side="right", pady=5)
        
        m = tk.Frame(self.t1, bg=self.CLR_CARD); m.pack(pady=10, fill="both", expand=True)

        # 버튼 텍스트를 Description(self.d12, self.d14)과 연동
        tk.Label(m, text="── 1:2 RF SWITCH ──", fg=self.CLR_ACCENT2, bg=self.CLR_CARD, font=("Arial", 10, "bold")).pack(pady=(15, 5))
        f1 = tk.Frame(m, bg=self.CLR_CARD); f1.pack()
        self.b12 = []
        for i in range(2):
            btn = tk.Button(
                f1,
                textvariable=self.d12[i],
                width=18,
                height=3,
                wraplength=130,
                justify="center",
                font=("Arial", 10, "bold"),
                command=lambda idx=i: self.tx("1to2", idx)
            )
            btn.pack(side="left", padx=15); self.b12.append(btn)

        tk.Label(m, text="── 1:4 RF SWITCH ──", fg=self.CLR_ACCENT1, bg=self.CLR_CARD, font=("Arial", 10, "bold")).pack(pady=(30, 5))
        f2 = tk.Frame(m, bg=self.CLR_CARD); f2.pack()
        self.b14 = []
        for i in range(4):
            btn = tk.Button(
                f2,
                textvariable=self.d14[i],
                width=20,
                height=3,
                wraplength=150,
                justify="center",
                font=("Arial", 9, "bold"),
                command=lambda idx=i: self.tx("1to4", idx)
            )
            r, c = divmod(i, 2)
            btn.grid(row=r, column=c, padx=10, pady=8)
            self.b14.append(btn)

        # CONTROL 탭에서도 현재 Description을 한눈에 확인
        self.desc_summary_var = tk.StringVar()
        self.lbl_desc_summary = tk.Label(
            m,
            textvariable=self.desc_summary_var,
            fg=self.CLR_TEXT,
            bg=self.CLR_CARD,
            font=("Arial", 8),
            justify="left"
        )
        self.lbl_desc_summary.pack(pady=(15, 0))

        self.btn_sync = tk.Button(self.t1, text="🔄 RE-SYNC DEVICE STATUS", bg=self.CLR_BRIGHT_SYNC, fg="white", font=("Arial", 9, "bold"), relief="flat", command=self.sync_logic, state="disabled")
        self.btn_sync.pack(side="bottom", pady=20)
        self.reset_c()
        self._bind_description_watchers()
        self._refresh_description_summary()

    def load_config(self):
        self.h.set("10.0.10.77")
        self.u.set("debian")
        self.p.set("temppwd")

        # 접속 정보(IP/ID/PW)는 로컬에 저장/로드합니다.
        if os.path.exists(self.local_cfg_file):
            try:
                with open(self.local_cfg_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.h.set(d.get("h", "10.0.10.77"))
                self.u.set(d.get("u", "debian"))
                self.p.set(d.get("p", "temppwd"))
            except Exception:
                pass

        for i in range(2):
            self.d12[i].set(f"PATH {i+1}")
        for i in range(4):
            self.d14[i].set(f"RF {i+1}")

    def save_config(self):
        # 접속 정보는 로컬에 즉시 저장
        self._save_local_conn_config()
        # PATH 설명은 보드에 공유 저장
        self._execute_task(self._save_config_worker)

    def _save_local_conn_config(self):
        try:
            data = {
                "h": self.h.get().strip(),
                "u": self.u.get().strip(),
                "p": self.p.get().strip()
            }
            with open(self.local_cfg_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _apply_descriptions(self, d12_val, d14_val):
        for i in range(2):
            self.d12[i].set(d12_val[i] if i < len(d12_val) else f"PATH {i+1}")
        for i in range(4):
            self.d14[i].set(d14_val[i] if i < len(d14_val) else f"RF {i+1}")

    def _bind_description_watchers(self):
        for var in self.d12 + self.d14:
            var.trace_add("write", lambda *args: self._refresh_description_summary())

    def _refresh_description_summary(self):
        path_text = " | ".join([f"P{i+1}:{v.get()}" for i, v in enumerate(self.d12)])
        rf_text = " | ".join([f"R{i+1}:{v.get()}" for i, v in enumerate(self.d14)])
        self.desc_summary_var.set(f"Description  1:2 [{path_text}]   1:4 [{rf_text}]")

    def _load_config_from_board(self):
        s = self.get_ssh()
        try:
            with s.open_sftp() as sftp:
                with sftp.file(self.remote_cfg_file, "r") as f:
                    raw = f.read()
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            data = json.loads(text)
            d12_val = data.get("d12", ["PATH 1", "PATH 2"])
            d14_val = data.get("d14", ["RF 1", "RF 2", "RF 3", "RF 4"])
            self.root.after(0, lambda: self._apply_descriptions(d12_val, d14_val))
        except Exception:
            # 파일이 없거나 읽기 실패 시 기본 이름 유지
            pass
        finally:
            s.close()

    def _save_config_worker(self):
        try:
            data = {
                "d12": [v.get() for v in self.d12],
                "d14": [v.get() for v in self.d14]
            }
            payload = json.dumps(data, ensure_ascii=False)

            s = self.get_ssh()
            try:
                with s.open_sftp() as sftp:
                    with sftp.file(self.remote_cfg_file, "w") as f:
                        f.write(payload)
            finally:
                s.close()

            self.root.after(0, lambda: messagebox.showinfo("OK", "Config(local) + Description(board) saved."))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Save Error", f"보드 저장 실패: {str(e)}"))
        finally:
            self.root.after(0, lambda: self._set_ui_busy(False))

    def disconnect(self):
        """UI를 Disconnected 상태로 초기화합니다."""
        if self.is_busy:
            messagebox.showwarning("Busy", "작업이 완료될 때까지 기다려주세요.")
            return
        self._set_ui_disconnected()

    def _set_ui_disconnected(self):
        """UI를 Disconnected 상태로 설정하는 내부 함수"""
        self.st.set("● Disconnected")
        self.bar.config(bg="#dfe4ea", fg="#ff4757")
        
        # 연결 관련 버튼 활성화
        self.btn_q_conn.config(state="normal")
        self.btn_q_disconn.config(state="normal")
        self.btn_connect_cfg.config(state="normal")

        # 제어 관련 버튼 비활성화
        self.btn_sync.config(state="disabled")
        for btn in self.b12 + self.b14: btn.config(state="disabled")
        self.reset_c()

    def reset_c(self):
        for b in self.b12 + self.b14: b.config(bg=self.CLR_OFF, fg="#57606f")

    def _map_1to2_gpio_to_ui(self, gpio_val):
        """1:2 스위치의 실제 GPIO 값(0/1)을 UI 버튼 인덱스로 변환"""
        return 1 if gpio_val == 0 else 0 if gpio_val == 1 else None

    def _map_1to2_ui_to_gpio(self, ui_idx):
        """1:2 UI 버튼 인덱스를 실제 GPIO 값(0/1)으로 변환"""
        return 1 if ui_idx == 0 else 0 if ui_idx == 1 else 0

    def _shell_quote(self, text):
        return "'" + str(text).replace("'", "'\"'\"'") + "'"

    def _run_sudo(self, ssh, inner_cmd):
        stdin, stdout, stderr = ssh.exec_command(
            f"sudo -S bash -lc {self._shell_quote(inner_cmd)}"
        )
        stdin.write(self.p.get().strip() + "\n")
        stdin.flush()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()
        if exit_code != 0:
            raise RuntimeError(err or out or f"sudo command failed (exit {exit_code})")

    def upd(self, t, idx):
        btns, color = (self.b12, self.CLR_ACCENT2) if t == "1to2" else (self.b14, self.CLR_ACCENT1)
        for b in btns: b.config(bg=self.CLR_OFF, fg="#57606f")
        if idx is not None and 0 <= idx < len(btns): btns[idx].config(bg=color, fg="white")

    def get_ssh(self):
        if self._paramiko is None:
            import paramiko
            self._paramiko = paramiko
        s = self._paramiko.SSHClient()
        s.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
        s.connect(self.h.get().strip(), username=self.u.get().strip(), password=self.p.get().strip(), timeout=5)
        return s

    def _set_ui_busy(self, busy):
        """UI 요소들의 활성화/비활성화 상태를 관리"""
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        
        # 메인 탭 버튼들
        self.btn_q_conn.config(state=state)
        self.btn_q_disconn.config(state=state)
        # Sync 버튼은 연결 성공 시에만 활성화되므로 상태를 유지
        if self.st.get().startswith("● ONLINE"):
            self.btn_sync.config(state=state)
        else:
            self.btn_sync.config(state="disabled")
            
        for btn in self.b12 + self.b14:
            btn.config(state=state)
            
        # 설정 탭 버튼들
        self.btn_save_cfg.config(state=state)
        self.btn_connect_cfg.config(state=state)

    def _execute_task(self, worker_func, *args):
        """백그라운드 스레드에서 작업을 실행하고 UI 상태를 관리"""
        if self.is_busy:
            messagebox.showwarning("Busy", "이미 다른 작업이 실행 중입니다.")
            return
        
        self._set_ui_busy(True)
        thread = threading.Thread(target=worker_func, args=args, daemon=True)
        thread.start()

    def conn(self):
        self._execute_task(self._conn_and_sync_worker)

    def _conn_and_sync_worker(self):
        """Worker to handle connection and subsequent status sync."""
        try:
            # --- Part 1: Connection ---
            self.root.after(0, lambda: self.st.set("● Connecting..."))
            s = self.get_ssh()
            s.close()
            
            # --- UI update after successful connection ---
            self.root.after(0, self._on_conn_success_ui)

            # --- Part 1.5: Load shared description from board ---
            self._load_config_from_board()

            # --- Part 2: Sync Status (logic copied from _sync_worker) ---
            s = self.get_ssh()
            cmd = "echo $(cat /sys/class/gpio/gpio44/value),$(cat /sys/class/gpio/gpio30/value),$(cat /sys/class/gpio/gpio31/value)"
            _, out, _ = s.exec_command(cmd)
            res = out.read().decode().strip().split(',')
            s.close()
            
            if len(res) >= 3:
                raw_12 = int(res[0].strip())
                idx12 = self._map_1to2_gpio_to_ui(raw_12)
                v1, v2 = int(res[1].strip()), int(res[2].strip())
                val_map = {(0,0):0, (1,0):1, (0,1):2, (1,1):3}
                idx14 = val_map.get((v1, v2), 0)
                # Schedule UI updates for sync results
                self.root.after(0, lambda: self.upd("1to2", idx12))
                self.root.after(0, lambda: self.upd("1to4", idx14))

        except Exception as e:
            # On any failure (connection or sync), show error
            self.root.after(0, lambda: self._on_conn_error(e))
        finally:
            # ALWAYS unlock the UI when the worker is done.
            self.root.after(0, lambda: self._set_ui_busy(False))

    def _on_conn_success_ui(self):
        """UI updates for a successful connection (without triggering another task)."""
        self.st.set(f"● ONLINE: {self.h.get()}")
        self.bar.config(bg="#dff9fb", fg=self.CLR_SUCCESS)
        self.btn_sync.config(state="normal")
        if self.nb.index("current") == 1: self.nb.select(0)

    def _on_conn_error(self, e):
        self.st.set("● Connection Error")
        self.bar.config(bg="#dfe4ea", fg="#ff4757")
        # The UI is unlocked by the worker's finally block.
        messagebox.showerror("SSH Error", f"접속 실패: {str(e)}")

    def sync_logic(self):
        self._execute_task(self._sync_worker)

    def _sync_worker(self):
        try:
            s = self.get_ssh()
            cmd = "echo $(cat /sys/class/gpio/gpio44/value),$(cat /sys/class/gpio/gpio30/value),$(cat /sys/class/gpio/gpio31/value)"
            _, out, _ = s.exec_command(cmd)
            res = out.read().decode().strip().split(',')
            s.close()
            
            if len(res) >= 3:
                raw_12 = int(res[0].strip())
                idx12 = self._map_1to2_gpio_to_ui(raw_12)
                v1, v2 = int(res[1].strip()), int(res[2].strip())
                val_map = {(0,0):0, (1,0):1, (0,1):2, (1,1):3}
                idx14 = val_map.get((v1, v2), 0)
                self.root.after(0, lambda: self.upd("1to2", idx12))
                self.root.after(0, lambda: self.upd("1to4", idx14))
        except Exception as e:
            print(f"Sync failed: {e}") # 콘솔에 에러 로그 출력
        finally:
            self.root.after(0, lambda: self._set_ui_busy(False))

    def tx(self, t, v):
        self._execute_task(self._tx_worker, t, v)

    def _tx_worker(self, t, v):
        try:
            s = self.get_ssh()
            if t == "1to2":
                gpio_val = self._map_1to2_ui_to_gpio(v)
                cmd = (
                    "echo out > /sys/class/gpio/gpio44/direction; "
                    f"echo {gpio_val} > /sys/class/gpio/gpio44/value"
                )
            else:
                tx_map = {0:(0,0), 1:(1,0), 2:(0,1), 3:(1,1)}
                v1, v2 = tx_map.get(v, (0,0))
                cmd = (
                    "echo out > /sys/class/gpio/gpio30/direction; "
                    "echo out > /sys/class/gpio/gpio31/direction; "
                    f"echo {v1} > /sys/class/gpio/gpio30/value; "
                    f"echo {v2} > /sys/class/gpio/gpio31/value"
                )
            self._run_sudo(s, cmd)
            s.close()
            self.root.after(0, lambda: self.upd(t, v))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Command Error", str(e)))
        finally:
            self.root.after(0, lambda: self._set_ui_busy(False))

if __name__ == "__main__":
    root = tk.Tk(); app = RFApp(root); root.mainloop()

# Build command (single-file GUI exe with icon):
# python -m PyInstaller --noconsole --onefile --icon="switch.ico" --name "RF_Switch" "RF Swtich V1.0.py"
    