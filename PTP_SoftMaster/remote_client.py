#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSH client that deploys and drives remote_agent.py on a Linux host."""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import paramiko

from soft_master import MasterConfig, MasterStats

LogFn = Callable[[str], None]
AGENT_FILES = ("ptp_codec.py", "soft_master.py", "trex_soft_master.py", "trex_relay.py", "remote_agent.py")


class RemoteAgentError(RuntimeError):
    pass


class RemotePtpClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 22,
        remote_dir: str = "/tmp/ptp_softmaster",
        use_sudo: bool = True,
        log: Optional[LogFn] = None,
    ):
        self.host = host.strip()
        self.username = username.strip()
        self.password = password
        self.port = int(port)
        self.remote_dir = remote_dir.rstrip("/") or "/tmp/ptp_softmaster"
        self.use_sudo = bool(use_sudo)
        self.log = log or (lambda m: None)

        self._ssh: Optional[paramiko.SSHClient] = None
        self._chan: Optional[paramiko.Channel] = None
        self._trex_chan: Optional[paramiko.Channel] = None
        self._trex_reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._cond = threading.Condition(self._lock)
        self._reader: Optional[threading.Thread] = None
        self._alive = False
        self._last_stats = MasterStats()
        self._last_stats_raw: dict[str, Any] = {}
        self._running = False

    @property
    def connected(self) -> bool:
        return bool(self._alive and self._chan is not None and not self._chan.closed)

    @property
    def stats(self) -> MasterStats:
        return self._last_stats

    @property
    def is_master_running(self) -> bool:
        return self._running

    def connect_and_start_agent(self) -> None:
        self.close()
        local_root = Path(__file__).resolve().parent

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.log(f"[SSH] connecting {self.username}@{self.host}:{self.port} ...")
        ssh.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=20,
            allow_agent=False,
            look_for_keys=False,
        )
        try:
            t = ssh.get_transport()
            if t is not None:
                t.set_keepalive(30)
        except Exception:
            pass
        self._ssh = ssh
        self._deploy(local_root)
        self._spawn_agent()
        # Wait for hello or first readable readiness via ping
        hello_deadline = time.time() + 15
        while time.time() < hello_deadline:
            if not self.connected:
                raise RemoteAgentError("agent channel closed during startup")
            try:
                self.request("ping", timeout=3.0)
                self.log("[SSH] agent ready")
                return
            except Exception:
                time.sleep(0.3)
        raise RemoteAgentError("agent did not respond to ping (need root/sudo + scapy?)")

    def _deploy(self, local_root: Path) -> None:
        assert self._ssh is not None
        self.log(f"[SSH] deploy -> {self.remote_dir}")
        sftp = self._ssh.open_sftp()
        try:
            self._mkdir_p(sftp, self.remote_dir)
            for name in AGENT_FILES:
                local = local_root / name
                if not local.is_file():
                    raise RemoteAgentError(f"missing local file: {local}")
                remote = f"{self.remote_dir}/{name}"
                sftp.put(str(local), remote)
                self.log(f"[SSH] uploaded {name}")
        finally:
            sftp.close()
        # Ensure scapy is available (best-effort)
        py = "python3"
        check = (
            f"{py} -c \"import scapy; print('scapy-ok')\" 2>/dev/null "
            f"|| {py} -m pip install --user -q scapy 2>/dev/null; "
            f"{py} -c \"import scapy; print('scapy-ok')\""
        )
        out, err, code = self._exec(check, timeout=120)
        if "scapy-ok" not in (out or ""):
            self.log(f"[SSH] WARN scapy check failed out={out!r} err={err!r} code={code}")
        else:
            self.log("[SSH] scapy available on remote")

    def _mkdir_p(self, sftp: paramiko.SFTPClient, remote: str) -> None:
        parts = remote.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except OSError:
                try:
                    sftp.mkdir(cur)
                except OSError:
                    pass

    def _exec(self, command: str, timeout: float = 30.0) -> tuple[str, str, int]:
        assert self._ssh is not None
        stdin, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code

    def _exec_sudo(self, command: str, timeout: float = 30.0) -> tuple[str, str, int]:
        """Run remote command with sudo -S (password on stdin)."""
        assert self._ssh is not None
        wrapped = f"sudo -S -p '' bash -lc {shlex.quote(command)}"
        stdin, stdout, stderr = self._ssh.exec_command(wrapped, get_pty=True, timeout=timeout)
        try:
            stdin.write(self.password + "\n")
            stdin.flush()
        except Exception:
            pass
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code

    def trex_process_running(self) -> bool:
        out, _, _ = self._exec("ps aux | grep '[t]-rex-64' | grep -v grep || true", timeout=10)
        return bool((out or "").strip())

    def trex_port_open(self, rpc_server: str = "127.0.0.1", rpc_port: int = 4501) -> bool:
        """Fast check: is TRex JSON-RPC TCP port listening?"""
        host = (rpc_server or "127.0.0.1").strip() or "127.0.0.1"
        # Prefer ss/netstat; fallback to bash /dev/tcp
        cmd = (
            f"(ss -ltn 2>/dev/null | grep -q ':{rpc_port} ') && echo OPEN || "
            f"(netstat -ltn 2>/dev/null | grep -q ':{rpc_port} ') && echo OPEN || "
            f"(timeout 1 bash -c 'echo > /dev/tcp/{host}/{rpc_port}' 2>/dev/null && echo OPEN) || true"
        )
        out, _, _ = self._exec(cmd, timeout=8)
        return "OPEN" in (out or "")

    def trex_rpc_alive(self, trex_path: str, rpc_server: str = "127.0.0.1") -> bool:
        """
        Probe STL RPC on the remote host.
        1) TCP :4501 open
        2) STLClient.connect via a remote script file (avoids fragile python -c quoting)
        """
        trex_path = trex_path.rstrip("/")
        rpc_server = (rpc_server or "127.0.0.1").strip() or "127.0.0.1"

        if not self.trex_port_open(rpc_server, 4501):
            self.log("[TREX] RPC port :4501 not listening")
            return False

        ipath = f"{trex_path}/automation/trex_control_plane/interactive"
        script = (
            "import sys, traceback\n"
            f"sys.path.insert(0, {ipath!r})\n"
            "try:\n"
            "    from trex.stl.api import STLClient\n"
            f"    c = STLClient(server={rpc_server!r})\n"
            "    c.connect()\n"
            "    c.disconnect()\n"
            "    print('rpc-ok')\n"
            "except Exception as e:\n"
            "    print('rpc-fail:' + str(e))\n"
            "    traceback.print_exc()\n"
        )
        remote_probe = "/tmp/ptp_trex_rpc_probe.py"
        try:
            assert self._ssh is not None
            sftp = self._ssh.open_sftp()
            try:
                with sftp.file(remote_probe, "w") as f:
                    f.write(script)
            finally:
                sftp.close()
        except Exception as exc:
            self.log(f"[TREX] probe upload failed: {exc}")
            # TCP is open — treat as alive enough to try list_trex_ports
            return True

        out, err, code = self._exec(f"python3 {shlex.quote(remote_probe)}", timeout=25)
        ok = "rpc-ok" in (out or "")
        if not ok:
            msg = ((out or "") + "\n" + (err or "")).strip().replace("\n", " | ")
            self.log(f"[TREX] RPC STL probe fail code={code} {msg[:300]}")
            # Port is open; agent-side list may still work
            return True
        return True

    def start_trex_daemon(
        self,
        trex_path: str,
        *,
        cores: int = 6,
        rpc_server: str = "127.0.0.1",
        wait_sec: float = 90.0,
        restart: bool = False,
    ) -> bool:
        """
        Start TRex interactive server like DDoS GUI:
        keep a dedicated SSH/PTY session running: sudo ./t-rex-64 -i -c N
        """
        trex_path = trex_path.rstrip("/")
        rpc_server = (rpc_server or "127.0.0.1").strip() or "127.0.0.1"

        if self.trex_port_open(rpc_server, 4501):
            self.log("[TREX] already up (RPC :4501 listening)")
            return True

        if restart or self.trex_process_running():
            self.log("[TREX] stopping old t-rex-64 ...")
            self._exec_sudo("pkill -f t-rex-64 || true", timeout=20)
            time.sleep(2.0)
            self._close_trex_session()

        self.log(f"[TREX] starting ./t-rex-64 -i -c {cores} @ {trex_path} (persistent SSH session)")
        assert self._ssh is not None
        transport = self._ssh.get_transport()
        if transport is None:
            raise RemoteAgentError("no SSH transport for TRex")

        chan = transport.open_session()
        chan.get_pty(term="vt100", width=120, height=40)
        # Same command style as DDoS GUI (sudo -S on the binary itself)
        cmd = f"cd {shlex.quote(trex_path)} && sudo -S -p '' ./t-rex-64 -i -c {int(cores)}"
        chan.exec_command(cmd)
        try:
            chan.sendall((self.password + "\n").encode("utf-8"))
        except Exception:
            pass
        self._trex_chan = chan
        self._trex_reader = threading.Thread(
            target=self._trex_session_read_loop, name="trex-session-rx", daemon=True
        )
        self._trex_reader.start()

        deadline = time.time() + float(wait_sec)
        while time.time() < deadline:
            if self.trex_port_open(rpc_server, 4501):
                self.log("[TREX] RPC :4501 listening — ready")
                return True
            if self.trex_process_running():
                self.log("[TREX] process up, waiting for :4501 ...")
            elif self._trex_chan is not None and self._trex_chan.exit_status_ready():
                self.log("[TREX] session exited early — see [TREX-RUN] logs")
                break
            time.sleep(2.0)

        log_out, _, _ = self._exec(
            "tail -n 50 /tmp/ptp_trex.log 2>/dev/null; "
            "dmesg 2>/dev/null | tail -n 5 || true",
            timeout=10,
        )
        if (log_out or "").strip():
            for ln in log_out.splitlines()[-20:]:
                self.log(f"[TREX-LOG] {ln}")
        self.log("[TREX] start timeout — RPC :4501 not ready")
        return False

    def _trex_session_read_loop(self) -> None:
        chan = getattr(self, "_trex_chan", None)
        if chan is None:
            return
        try:
            while True:
                if chan.recv_ready():
                    data = chan.recv(4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    for ln in text.splitlines():
                        ln = ln.strip()
                        if ln:
                            self.log(f"[TREX-RUN] {ln[:200]}")
                elif chan.exit_status_ready():
                    while chan.recv_ready():
                        data = chan.recv(4096)
                        text = data.decode("utf-8", errors="replace")
                        for ln in text.splitlines():
                            ln = ln.strip()
                            if ln:
                                self.log(f"[TREX-RUN] {ln[:200]}")
                    break
                else:
                    time.sleep(0.2)
        except Exception as exc:
            self.log(f"[TREX] session reader: {exc}")

    def _close_trex_session(self) -> None:
        chan = getattr(self, "_trex_chan", None)
        self._trex_chan = None
        if chan is not None:
            try:
                chan.close()
            except Exception:
                pass

    def _spawn_agent(self) -> None:
        assert self._ssh is not None
        transport = self._ssh.get_transport()
        if transport is None:
            raise RemoteAgentError("no SSH transport")
        chan = transport.open_session()
        chan.set_combine_stderr(True)
        # PYTHONPATH so imports resolve from remote_dir
        env_prefix = (
            f"cd {shlex.quote(self.remote_dir)} && "
            f"export PYTHONUNBUFFERED=1 PYTHONPATH={shlex.quote(self.remote_dir)} && "
        )
        if self.use_sudo:
            # sudo -S consumes password line; remaining stdin goes to python
            cmd = env_prefix + "sudo -S -p '' python3 -u remote_agent.py"
        else:
            cmd = env_prefix + "python3 -u remote_agent.py"
        self.log(f"[SSH] exec: {cmd}")
        chan.exec_command(cmd)
        if self.use_sudo:
            chan.sendall((self.password + "\n").encode("utf-8"))
        self._chan = chan
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, name="ptp-agent-rx", daemon=True)
        self._reader.start()
        time.sleep(0.4)
        # Drain early sudo/auth noise by waiting briefly; ping will confirm

    def _read_loop(self) -> None:
        buf = b""
        chan = self._chan
        if chan is None:
            return
        try:
            while self._alive and chan is not None and not chan.closed:
                if chan.recv_ready():
                    chunk = chan.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._handle_line(line.decode("utf-8", errors="replace").strip())
                elif chan.exit_status_ready():
                    # flush remaining
                    while chan.recv_ready():
                        buf += chan.recv(4096)
                    break
                else:
                    time.sleep(0.05)
            # leftover
            if buf.strip():
                for line in buf.decode("utf-8", errors="replace").splitlines():
                    self._handle_line(line.strip())
        except Exception as exc:
            self.log(f"[SSH] reader error: {exc}")
        finally:
            self._alive = False
            with self._cond:
                self._cond.notify_all()
            self.log("[SSH] agent channel closed")

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        # Non-JSON noise (sudo prompts etc.)
        if not line.startswith("{"):
            self.log(f"[REMOTE] {line}")
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            self.log(f"[REMOTE] {line}")
            return
        if not isinstance(obj, dict):
            return

        typ = obj.get("type")
        if typ == "log" or typ == "hello":
            self.log(f"[REMOTE] {obj.get('msg', '')}")
            return
        if typ == "stats":
            try:
                raw = obj.get("stats") or {}
                self._last_stats_raw = dict(raw)
                self._last_stats = MasterStats.from_dict(raw)
                self._running = True
            except Exception:
                pass
            return

        req_id = obj.get("id")
        if req_id is not None:
            with self._cond:
                self._pending[int(req_id)] = obj
                self._cond.notify_all()
            if obj.get("ok") and (obj.get("started") or obj.get("updated")):
                self._running = True
            if obj.get("ok") and obj.get("stopped"):
                self._running = False
            if not obj.get("ok") and obj.get("error"):
                self.log(f"[REMOTE] error: {obj.get('error')}")
            return

        # unknown async
        if obj.get("msg"):
            self.log(f"[REMOTE] {obj.get('msg')}")

    def request(self, cmd: str, timeout: float = 20.0, **payload: Any) -> dict[str, Any]:
        if not self.connected or self._chan is None:
            raise RemoteAgentError("not connected")
        with self._cond:
            self._req_id += 1
            req_id = self._req_id
            msg = {"id": req_id, "cmd": cmd, **payload}
            data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                self._chan.sendall(data)
            except Exception as exc:
                raise RemoteAgentError(f"send failed: {exc}") from exc
            deadline = time.time() + timeout
            while req_id not in self._pending:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RemoteAgentError(f"timeout waiting for cmd={cmd} id={req_id}")
                if not self._alive:
                    raise RemoteAgentError("agent died while waiting for response")
                self._cond.wait(timeout=min(0.5, remaining))
            resp = self._pending.pop(req_id)
        if not resp.get("ok", False):
            raise RemoteAgentError(str(resp.get("error") or resp))
        return resp

    def list_ifaces(self) -> list[str]:
        resp = self.request("list_ifaces", timeout=15.0)
        details = resp.get("details") or []
        for d in details:
            try:
                self.log(
                    f"[IFACE] {d.get('iface')} operstate={d.get('operstate')} "
                    f"carrier={d.get('carrier')} mac={d.get('mac')}"
                )
            except Exception:
                pass
        return list(resp.get("ifaces") or [])

    def set_trex(self, trex_path: str, rpc_server: str = "127.0.0.1") -> dict:
        return self.request(
            "set_trex",
            timeout=10.0,
            trex_path=trex_path,
            rpc_server=rpc_server,
        )

    def list_trex_ports(self, trex_path: str, rpc_server: str = "127.0.0.1") -> list[dict]:
        resp = self.request(
            "list_trex_ports",
            timeout=30.0,
            trex_path=trex_path,
            rpc_server=rpc_server,
        )
        ports = list(resp.get("ports") or [])
        for p in ports:
            self.log(
                f"[TREX] port={p.get('port')} link_up={p.get('link_up')} "
                f"speed={p.get('speed')} mac={p.get('src_mac')}"
            )
        return ports

    def wire_check(self, iface: str = "", seconds: int = 2, **kwargs) -> dict:
        return self.request(
            "wire_check",
            timeout=seconds + 20.0,
            iface=iface,
            seconds=seconds,
            **kwargs,
        )

    def start_master(
        self,
        cfg: MasterConfig,
        *,
        backend: str = "trex",
        trex_path: str = "",
        trex_port: int = 0,
        rpc_server: str = "127.0.0.1",
    ) -> None:
        payload = {
            "config": cfg.to_dict(),
            "backend": backend,
        }
        if backend == "trex":
            payload["trex_path"] = trex_path
            payload["trex_port"] = int(trex_port)
            payload["rpc_server"] = rpc_server
        self.request("start", timeout=40.0, **payload)
        self._running = True

    def update_config(self, cfg: MasterConfig) -> None:
        self.request("update", timeout=15.0, config=cfg.to_dict())

    def start_relay(
        self,
        relay_config: dict[str, Any],
        *,
        trex_path: str = "",
        rx_port: int = 0,
        tx_port: int = 1,
        rpc_server: str = "127.0.0.1",
    ) -> None:
        self.request(
            "start",
            timeout=40.0,
            backend="relay",
            relay_config=relay_config,
            trex_path=trex_path,
            rx_port=int(rx_port),
            tx_port=int(tx_port),
            rpc_server=rpc_server,
        )
        self._running = True

    def update_relay(self, relay_config: dict[str, Any]) -> None:
        self.request("update", timeout=15.0, relay_config=relay_config)

    def stop_master(self) -> None:
        try:
            self.request("stop", timeout=15.0)
        finally:
            self._running = False

    def close(self) -> None:
        self._alive = False
        chan = self._chan
        self._chan = None
        if chan is not None:
            try:
                try:
                    chan.sendall(b'{"id":0,"cmd":"quit"}\n')
                except Exception:
                    pass
                time.sleep(0.2)
                chan.close()
            except Exception:
                pass
        # Keep TRex engine running after GUI disconnect (only close our reader handle).
        # Process may still be tied to this SSH transport — closing SSH will stop it.
        # So: leave a note; prefer not killing via pkill here.
        self._close_trex_session()
        ssh = self._ssh
        self._ssh = None
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass
        with self._cond:
            self._pending.clear()
            self._cond.notify_all()
        self._running = False
