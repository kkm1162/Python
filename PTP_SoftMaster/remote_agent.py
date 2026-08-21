#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Soft PTP Master remote agent (Linux / TRex host).

Windows GUI opens SSH and runs this process. Protocol: one JSON object per line.
Default backend: TRex DPDK ports (not kernel eno/enx).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Optional

try:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

from soft_master import MasterConfig, SoftPtpMaster, list_interfaces, read_iface_state
from trex_relay import RelayConfig, RelayStats, TrexPtpRelay
from trex_soft_master import TrexSoftPtpMaster, list_trex_ports


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    _emit({"type": "log", "msg": str(msg)})


class Agent:
    def __init__(self) -> None:
        self.master: Any = None
        self._lock = threading.Lock()
        self._stats_stop = threading.Event()
        self._stats_thread: Optional[threading.Thread] = None
        self.trex_path = "/home/slab/trex/v3.08"
        self.rpc_server = "127.0.0.1"

    def _start_stats_pump(self) -> None:
        self._stats_stop.set()
        if self._stats_thread and self._stats_thread.is_alive():
            self._stats_thread.join(timeout=1.0)
        self._stats_stop = threading.Event()

        def _loop() -> None:
            while not self._stats_stop.wait(0.5):
                m = self.master
                if m is None:
                    continue
                _emit({"type": "stats", "stats": m.stats.to_dict()})

        self._stats_thread = threading.Thread(target=_loop, name="stats-pump", daemon=True)
        self._stats_thread.start()

    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = str(req.get("cmd") or "").strip()
        if cmd == "ping":
            return {
                "ok": True,
                "pong": True,
                "pid": os.getpid(),
                "uid": os.geteuid() if hasattr(os, "geteuid") else None,
                "user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
                "backend_default": "trex",
            }
        if cmd == "set_trex":
            if req.get("trex_path"):
                self.trex_path = str(req["trex_path"]).rstrip("/")
            if req.get("rpc_server"):
                self.rpc_server = str(req["rpc_server"]).strip() or "127.0.0.1"
            return {"ok": True, "trex_path": self.trex_path, "rpc_server": self.rpc_server}

        if cmd == "list_trex_ports":
            path = str(req.get("trex_path") or self.trex_path).rstrip("/")
            rpc = str(req.get("rpc_server") or self.rpc_server).strip() or "127.0.0.1"
            self.trex_path = path
            self.rpc_server = rpc
            try:
                ports = list_trex_ports(path, rpc)
                # Dump raw info for link debug
                _log(f"[DEBUG] list_trex_ports returned {len(ports)} ports")
                for p in ports:
                    _log(f"[DEBUG] port={p}")
            except Exception as exc:
                return {"ok": False, "error": f"list_trex_ports failed: {exc}"}
            return {"ok": True, "ports": ports, "trex_path": path, "rpc_server": rpc}

        if cmd == "list_ifaces":
            # Kernel NICs (legacy / debug only)
            ifaces = list_interfaces()
            details = [read_iface_state(i) for i in ifaces]
            return {"ok": True, "ifaces": ifaces, "details": details}

        if cmd == "wire_check":
            # For TRex: report port link + recent stats if running
            backend = str(req.get("backend") or "trex")
            if backend == "trex":
                port = int(req.get("port") if req.get("port") is not None else 0)
                try:
                    ports = list_trex_ports(self.trex_path, self.rpc_server)
                    info = next((p for p in ports if int(p.get("port", -1)) == port), None)
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}
                running = self.master is not None
                st = self.master.stats.to_dict() if running else {}
                return {
                    "ok": True,
                    "backend": "trex",
                    "port": port,
                    "port_info": info,
                    "running": running,
                    "stats": st,
                    "hit_count": int(st.get("sync_sent") or st.get("sync_relayed") or 0) + int(st.get("announce_sent") or st.get("announce_relayed") or 0),
                    "tx_packets_delta": None,
                    "output": json.dumps(info or {}, ensure_ascii=False),
                    "state": {
                        "operstate": "up" if (info or {}).get("link_up") else "down",
                        "carrier": "1" if (info or {}).get("link_up") else "0",
                    },
                }

            iface = str(req.get("iface") or "").strip()
            seconds = max(1, min(10, int(req.get("seconds") or 2)))
            if not iface:
                return {"ok": False, "error": "iface required"}

            def _tx_pkts() -> Optional[int]:
                path = f"/sys/class/net/{iface}/statistics/tx_packets"
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return int(f.read().strip())
                except Exception:
                    return None

            st = read_iface_state(iface)
            before = _tx_pkts()
            cmd_line = (
                f"timeout {seconds} tcpdump -i {shlex.quote(iface)} -nn -e -c 30 "
                f"'ether proto 0x88f7' 2>&1 || true"
            )
            try:
                proc = subprocess.run(
                    ["bash", "-lc", cmd_line],
                    capture_output=True,
                    text=True,
                    timeout=seconds + 5,
                )
                out = (proc.stdout or "") + (proc.stderr or "")
            except Exception as exc:
                time.sleep(seconds)
                out = f"wire_check failed: {exc}"
            after = _tx_pkts()
            tx_delta = None
            if before is not None and after is not None:
                tx_delta = max(0, after - before)
            lines = [ln for ln in out.splitlines() if ln.strip()]
            hits = [ln for ln in lines if "0x88f7" in ln.lower() or "ptp" in ln.lower()]
            return {
                "ok": True,
                "backend": "kernel",
                "iface": iface,
                "seconds": seconds,
                "hit_count": len(hits),
                "tx_packets_delta": tx_delta,
                "state": st,
                "output": "\n".join(lines[-40:]),
            }

        if cmd == "start":
            cfg = MasterConfig.from_dict(req.get("config") or {})
            backend = str(req.get("backend") or "trex").strip().lower()
            with self._lock:
                if self.master is not None:
                    return {"ok": False, "error": "already running; stop first"}
                if backend == "relay":
                    relay_cfg = RelayConfig.from_dict(req.get("relay_config") or req.get("config") or {})
                    trex_path = str(req.get("trex_path") or self.trex_path).rstrip("/")
                    rpc = str(req.get("rpc_server") or self.rpc_server).strip() or "127.0.0.1"
                    rx_port = int(req.get("rx_port") if req.get("rx_port") is not None else 0)
                    tx_port = int(req.get("tx_port") if req.get("tx_port") is not None else 1)
                    self.trex_path = trex_path
                    self.rpc_server = rpc
                    m = TrexPtpRelay(
                        relay_cfg,
                        trex_path=trex_path,
                        rx_port=rx_port,
                        tx_port=tx_port,
                        rpc_server=rpc,
                        log=_log,
                    )
                    m.start()
                    self.master = m
                elif backend == "trex":
                    trex_path = str(req.get("trex_path") or self.trex_path).rstrip("/")
                    rpc = str(req.get("rpc_server") or self.rpc_server).strip() or "127.0.0.1"
                    port = int(req.get("trex_port") if req.get("trex_port") is not None else 0)
                    self.trex_path = trex_path
                    self.rpc_server = rpc
                    m = TrexSoftPtpMaster(
                        cfg,
                        trex_path=trex_path,
                        trex_port=port,
                        rpc_server=rpc,
                        log=_log,
                    )
                    m.start()
                    self.master = m
                else:
                    if not cfg.iface:
                        return {"ok": False, "error": "iface required for kernel backend"}
                    m = SoftPtpMaster(cfg, log=_log)
                    m.start()
                    self.master = m
                self._start_stats_pump()
            return {"ok": True, "started": True, "backend": backend}

        if cmd == "update":
            with self._lock:
                if self.master is None:
                    return {"ok": False, "error": "not running"}
                if isinstance(self.master, TrexPtpRelay):
                    relay_cfg = RelayConfig.from_dict(req.get("relay_config") or req.get("config") or {})
                    self.master.update_config(relay_cfg)
                else:
                    cfg = MasterConfig.from_dict(req.get("config") or {})
                    self.master.update_config(cfg)
            return {"ok": True, "updated": True}

        if cmd == "stop":
            with self._lock:
                if self.master is not None:
                    try:
                        self.master.stop()
                    except Exception as exc:
                        _log(f"[ERR] stop: {exc}")
                    self.master = None
                self._stats_stop.set()
            return {"ok": True, "stopped": True}

        if cmd == "stats":
            with self._lock:
                if self.master is None:
                    return {"ok": True, "running": False, "stats": {}}
                return {"ok": True, "running": True, "stats": self.master.stats.to_dict()}

        if cmd == "quit":
            with self._lock:
                if self.master is not None:
                    try:
                        self.master.stop()
                    except Exception:
                        pass
                    self.master = None
                self._stats_stop.set()
            return {"ok": True, "bye": True}

        return {"ok": False, "error": f"unknown cmd: {cmd}"}


def main() -> int:
    agent = Agent()
    _emit(
        {
            "type": "hello",
            "msg": f"PTP Soft Master agent ready (TRex) pid={os.getpid()} "
            f"uid={os.geteuid() if hasattr(os, 'geteuid') else '?'}",
        }
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                _emit({"ok": False, "error": "request must be object"})
                continue
            req_id = req.get("id")
            resp = agent.handle(req)
            if req_id is not None:
                resp["id"] = req_id
            _emit(resp)
            if resp.get("bye"):
                return 0
        except Exception as exc:
            _emit(
                {
                    "id": req_id,
                    "ok": False,
                    "error": str(exc),
                    "trace": traceback.format_exc()[-800:],
                }
            )
            time.sleep(0.01)
    try:
        agent.handle({"cmd": "stop"})
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
