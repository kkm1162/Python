#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tkinter GUI for Soft PTP Master (local or remote Linux NIC)."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from typing import Any, Optional

from gui_settings import CONFIG_FILE, load_settings, save_settings

from ptp_codec import CLOCK_ACCURACY, CLOCK_CLASS, TIME_SOURCE
from remote_client import RemotePtpClient
from soft_master import (
    PTP_MCAST_DEFAULT,
    PTP_MCAST_LINK_LOCAL,
    MasterConfig,
    SoftPtpMaster,
    list_interfaces,
)

RATE_CHOICES = (
    "1 per second",
    "2 per second",
    "4 per second",
    "8 per second",
    "16 per second",
    "32 per second",
    "64 per second",
)

RATE_MAP = {
    "1 per second": 1.0,
    "2 per second": 2.0,
    "4 per second": 4.0,
    "8 per second": 8.0,
    "16 per second": 16.0,
    "32 per second": 32.0,
    "64 per second": 64.0,
}


def _rate_label(rate: float) -> str:
    for label, val in RATE_MAP.items():
        if abs(val - rate) < 1e-9:
            return label
    return f"{rate:g} per second"


def _parse_rate(label: str) -> float:
    label = (label or "").strip()
    if label in RATE_MAP:
        return RATE_MAP[label]
    try:
        return max(0.001, float(label.split()[0]))
    except (ValueError, IndexError) as exc:
        raise ValueError(f"invalid rate: {label}") from exc


class SoftPtpMasterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PTP Master / Relay — TRex / T1·T4 Shake")
        self.geometry("1000x900")
        self.minsize(880, 720)

        self._master: Optional[SoftPtpMaster] = None
        self._remote: Optional[RemotePtpClient] = None
        self._busy = False
        self._log_q: queue.Queue[str] = queue.Queue()
        self._build()
        self._load_gui_settings()
        self.after(200, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _is_remote(self) -> bool:
        return self.var_mode.get() == "remote"

    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _on_mousewheel(event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # --- Target mode / SSH ---
        tgt = ttk.LabelFrame(body, text="Target (TRex DPDK)", padding=8)
        tgt.pack(fill="x", padx=10, pady=(10, 4))

        self.var_mode = tk.StringVar(value="remote")
        ttk.Radiobutton(
            tgt, text="Remote TRex host (SSH)", variable=self.var_mode, value="remote",
            command=self._on_mode_changed,
        ).grid(row=0, column=0, sticky="w", **pad)
        ttk.Radiobutton(
            tgt, text="Local kernel NIC (debug)", variable=self.var_mode, value="local",
            command=self._on_mode_changed,
        ).grid(row=0, column=1, sticky="w", **pad)

        self.frm_ssh = ttk.Frame(tgt)
        self.frm_ssh.grid(row=1, column=0, columnspan=4, sticky="we", pady=(4, 0))

        ttk.Label(self.frm_ssh, text="Host").grid(row=0, column=0, sticky="w")
        self.var_ssh_host = tk.StringVar(value="")
        ttk.Entry(self.frm_ssh, textvariable=self.var_ssh_host, width=16).grid(
            row=0, column=1, sticky="w", **pad
        )
        ttk.Label(self.frm_ssh, text="SSH Port").grid(row=0, column=2, sticky="e")
        self.var_ssh_port = tk.StringVar(value="22")
        ttk.Entry(self.frm_ssh, textvariable=self.var_ssh_port, width=6).grid(
            row=0, column=3, sticky="w", **pad
        )
        ttk.Label(self.frm_ssh, text="User").grid(row=0, column=4, sticky="e")
        self.var_ssh_user = tk.StringVar(value="slab")
        ttk.Entry(self.frm_ssh, textvariable=self.var_ssh_user, width=10).grid(
            row=0, column=5, sticky="w", **pad
        )
        ttk.Label(self.frm_ssh, text="Password").grid(row=0, column=6, sticky="e")
        self.var_ssh_pw = tk.StringVar(value="")
        ttk.Entry(self.frm_ssh, textvariable=self.var_ssh_pw, width=12, show="*").grid(
            row=0, column=7, sticky="w", **pad
        )

        ttk.Label(self.frm_ssh, text="TRex Path").grid(row=1, column=0, sticky="w")
        self.var_trex_path = tk.StringVar(value="/home/slab/trex/v3.08")
        ttk.Entry(self.frm_ssh, textvariable=self.var_trex_path, width=28).grid(
            row=1, column=1, columnspan=2, sticky="w", **pad
        )
        ttk.Label(self.frm_ssh, text="RPC").grid(row=1, column=3, sticky="e")
        self.var_trex_rpc = tk.StringVar(value="127.0.0.1")
        ttk.Entry(self.frm_ssh, textvariable=self.var_trex_rpc, width=12).grid(
            row=1, column=4, sticky="w", **pad
        )

        self.btn_ssh_connect = ttk.Button(
            self.frm_ssh, text="Connect & Deploy", command=self.connect_remote
        )
        self.btn_ssh_connect.grid(row=1, column=5, sticky="w", **pad)
        self.btn_ssh_disconnect = ttk.Button(
            self.frm_ssh, text="Disconnect", command=self.disconnect_remote, state="disabled"
        )
        self.btn_ssh_disconnect.grid(row=1, column=6, sticky="w", **pad)
        self.btn_start_trex = ttk.Button(
            self.frm_ssh, text="Start TRex", command=self.start_trex_engine, state="disabled"
        )
        self.btn_start_trex.grid(row=1, column=7, sticky="w", **pad)

        ttk.Label(self.frm_ssh, text="Remote dir").grid(row=2, column=0, sticky="w")
        self.var_remote_dir = tk.StringVar(value="/tmp/ptp_softmaster")
        ttk.Entry(self.frm_ssh, textvariable=self.var_remote_dir, width=28).grid(
            row=2, column=1, columnspan=2, sticky="w", **pad
        )
        self.var_sudo = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.frm_ssh,
            text="sudo agent",
            variable=self.var_sudo,
        ).grid(row=2, column=3, sticky="w", **pad)
        self.var_autostart_trex = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self.frm_ssh,
            text="TRex 꺼져있으면 자동 기동",
            variable=self.var_autostart_trex,
        ).grid(row=2, column=4, columnspan=2, sticky="w", **pad)

        self.var_backend = tk.StringVar(value="trex")
        ttk.Radiobutton(
            self.frm_ssh, text="Soft Master", variable=self.var_backend, value="trex",
            command=self._on_backend_changed,
        ).grid(row=3, column=1, sticky="w", **pad)
        ttk.Radiobutton(
            self.frm_ssh, text="Relay (GM→RU)", variable=self.var_backend, value="relay",
            command=self._on_backend_changed,
        ).grid(row=3, column=2, sticky="w", **pad)
        ttk.Radiobutton(
            self.frm_ssh, text="Kernel NIC", variable=self.var_backend, value="kernel",
            command=self._on_backend_changed,
        ).grid(row=3, column=3, sticky="w", **pad)
        ttk.Label(self.frm_ssh, text="Cores").grid(row=3, column=4, sticky="e")
        self.var_trex_cores = tk.StringVar(value="6")
        ttk.Entry(self.frm_ssh, textvariable=self.var_trex_cores, width=4).grid(
            row=3, column=5, sticky="w", **pad
        )

        self.var_ssh_status = tk.StringVar(value="SSH: disconnected")
        ttk.Label(tgt, textvariable=self.var_ssh_status, foreground="#0f766e").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )

        # --- TRex / NIC ---
        nic = ttk.LabelFrame(body, text="TRex Port / Interface", padding=8)
        nic.pack(fill="x", padx=10, pady=4)

        ttk.Label(nic, text="TRex Port").grid(row=0, column=0, sticky="w")
        self.var_trex_port = tk.StringVar(value="0")
        self.cmb_trex_port = ttk.Combobox(nic, textvariable=self.var_trex_port, width=48, values=())
        self.cmb_trex_port.grid(row=0, column=1, sticky="we", **pad)
        ttk.Button(nic, text="Refresh Ports", command=self._refresh_ifaces).grid(row=0, column=2, **pad)

        ttk.Label(nic, text="Kernel NIC").grid(row=1, column=0, sticky="w")
        self.var_iface = tk.StringVar()
        self.cmb_iface = ttk.Combobox(nic, textvariable=self.var_iface, width=42, values=())
        self.cmb_iface.grid(row=1, column=1, sticky="we", **pad)
        ttk.Label(nic, text="(Kernel backend 전용)", foreground="#64748b").grid(row=1, column=2, sticky="w")

        ttk.Label(nic, text="Src MAC").grid(row=2, column=0, sticky="w")
        self.var_src_mac = tk.StringVar(value="")
        ttk.Entry(nic, textvariable=self.var_src_mac, width=22).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(nic, text="(비우면 TRex port MAC 자동)").grid(row=2, column=2, sticky="w")

        ttk.Label(nic, text="VLAN").grid(row=3, column=0, sticky="w")
        self.var_vlan = tk.StringVar(value="0")
        ttk.Entry(nic, textvariable=self.var_vlan, width=8).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(nic, text="0 = untagged").grid(row=3, column=2, sticky="w")
        nic.columnconfigure(1, weight=1)
        self._trex_port_map: dict[str, dict] = {}

        # --- Relay Port Selection ---
        self.frm_relay = ttk.LabelFrame(body, text="Relay Mode: GM Port → RU Port", padding=8)
        self.frm_relay.pack(fill="x", padx=10, pady=4)

        ttk.Label(self.frm_relay, text="GM RX Port (from GM)").grid(row=0, column=0, sticky="w")
        self.var_relay_rx = tk.StringVar(value="0")
        self.cmb_relay_rx = ttk.Combobox(self.frm_relay, textvariable=self.var_relay_rx, width=48, values=())
        self.cmb_relay_rx.grid(row=0, column=1, sticky="we", **pad)

        ttk.Label(self.frm_relay, text="RU TX Port (to RU)").grid(row=1, column=0, sticky="w")
        self.var_relay_tx = tk.StringVar(value="1")
        self.cmb_relay_tx = ttk.Combobox(self.frm_relay, textvariable=self.var_relay_tx, width=48, values=())
        self.cmb_relay_tx.grid(row=1, column=1, sticky="we", **pad)

        ttk.Label(self.frm_relay, text="Correction offset (ns)").grid(row=2, column=0, sticky="w")
        self.var_relay_corr = tk.StringVar(value="0")
        ttk.Entry(self.frm_relay, textvariable=self.var_relay_corr, width=12).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(
            self.frm_relay,
            text="상용 GM → Port 0 (RX) → T1/T4 수정 → Port 1 (TX) → RU.  Shake 설정은 아래 T1/T4 그대로 사용.",
            foreground="#0369a1",
            wraplength=700,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.frm_relay.columnconfigure(1, weight=1)

        # --- General PTP ---
        gen = ttk.LabelFrame(body, text="General PTP Settings", padding=8)
        gen.pack(fill="x", padx=10, pady=4)

        ttk.Label(gen, text="Domain").grid(row=0, column=0, sticky="w")
        self.var_domain = tk.StringVar(value="24")
        ttk.Entry(gen, textvariable=self.var_domain, width=8).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(gen, text="Sync Type").grid(row=0, column=2, sticky="e", padx=(16, 4))
        self.var_sync_type = tk.StringVar(value="2 Step")
        ttk.Combobox(
            gen,
            textvariable=self.var_sync_type,
            width=12,
            state="readonly",
            values=("1 Step", "2 Step"),
        ).grid(row=0, column=3, sticky="w", **pad)

        self.var_link_local = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            gen,
            text="Use 01-80-C2-00-00-0E non-forwardable Destination MAC",
            variable=self.var_link_local,
        ).grid(row=1, column=0, columnspan=4, sticky="w", **pad)

        ttk.Label(gen, text="Dst MAC (if unchecked)").grid(row=2, column=0, sticky="w")
        self.var_dst_mac = tk.StringVar(value=PTP_MCAST_DEFAULT)
        ttk.Combobox(
            gen,
            textvariable=self.var_dst_mac,
            width=20,
            values=(PTP_MCAST_DEFAULT, PTP_MCAST_LINK_LOCAL),
        ).grid(row=2, column=1, sticky="w", **pad)

        # --- Encapsulation ---
        enc = ttk.LabelFrame(body, text="Encapsulation", padding=8)
        enc.pack(fill="x", padx=10, pady=4)
        ttk.Label(enc, text="Encapsulation").grid(row=0, column=0, sticky="w")
        self.var_encap = tk.StringVar(value="None")
        ttk.Combobox(
            enc,
            textvariable=self.var_encap,
            width=14,
            state="readonly",
            values=("None",),
        ).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(enc, text="L2 Ethernet EtherType 0x88F7", foreground="#64748b").grid(
            row=0, column=2, sticky="w", padx=8
        )

        # --- Message Interval ---
        msg = ttk.LabelFrame(body, text="Message Interval", padding=8)
        msg.pack(fill="x", padx=10, pady=4)

        ttk.Label(msg, text="Announce").grid(row=0, column=0, sticky="w")
        self.var_ann_rate = tk.StringVar(value="8 per second")
        ttk.Combobox(msg, textvariable=self.var_ann_rate, width=16, values=RATE_CHOICES).grid(
            row=0, column=1, sticky="w", **pad
        )

        ttk.Label(msg, text="Sync").grid(row=0, column=2, sticky="e", padx=(16, 4))
        self.var_sync_rate = tk.StringVar(value="8 per second")
        ttk.Combobox(msg, textvariable=self.var_sync_rate, width=16, values=RATE_CHOICES).grid(
            row=0, column=3, sticky="w", **pad
        )

        ttk.Label(msg, text="Delay Request").grid(row=1, column=0, sticky="w")
        self.var_dreq_rate = tk.StringVar(value="16 per second")
        ttk.Combobox(msg, textvariable=self.var_dreq_rate, width=16, values=RATE_CHOICES).grid(
            row=1, column=1, sticky="w", **pad
        )
        ttk.Label(
            msg,
            text="(Delay_Resp logInterval 광고; Delay_Req는 slave가 송신)",
            foreground="#64748b",
        ).grid(row=1, column=2, columnspan=2, sticky="w")

        # --- Clock Attributes ---
        clk = ttk.LabelFrame(body, text="Clock Attributes", padding=8)
        clk.pack(fill="x", padx=10, pady=4)

        self.var_manual_clk = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            clk,
            text="Always configure clock attributes manually",
            variable=self.var_manual_clk,
        ).grid(row=0, column=0, columnspan=4, sticky="w", **pad)

        ttk.Label(clk, text="Priority 1").grid(row=1, column=0, sticky="w")
        self.var_p1 = tk.StringVar(value="128")
        ttk.Entry(clk, textvariable=self.var_p1, width=8).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(clk, text="Priority 2").grid(row=1, column=2, sticky="e", padx=(12, 4))
        self.var_p2 = tk.StringVar(value="255")
        ttk.Entry(clk, textvariable=self.var_p2, width=8).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(clk, text="Class").grid(row=2, column=0, sticky="w")
        self.var_class = tk.StringVar(value="Primary (6)")
        ttk.Combobox(
            clk,
            textvariable=self.var_class,
            width=22,
            state="readonly",
            values=tuple(CLOCK_CLASS.keys()),
        ).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(clk, text="Time Source").grid(row=2, column=2, sticky="e", padx=(12, 4))
        self.var_time_src = tk.StringVar(value="Internal Oscillator")
        ttk.Combobox(
            clk,
            textvariable=self.var_time_src,
            width=20,
            state="readonly",
            values=tuple(TIME_SOURCE.keys()),
        ).grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(clk, text="Clock Accuracy").grid(row=3, column=0, sticky="w")
        self.var_accuracy = tk.StringVar(value="Within 100 ns")
        ttk.Combobox(
            clk,
            textvariable=self.var_accuracy,
            width=22,
            state="readonly",
            values=tuple(CLOCK_ACCURACY.keys()),
        ).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(clk, text="UTC Offset (s)").grid(row=3, column=2, sticky="e", padx=(12, 4))
        self.var_utc = tk.StringVar(value="37")
        ttk.Entry(clk, textvariable=self.var_utc, width=8).grid(row=3, column=3, sticky="w", **pad)

        ttk.Label(clk, text="Freq. Traceable").grid(row=4, column=0, sticky="w")
        self.var_freq_tr = tk.StringVar(value="True")
        ttk.Combobox(
            clk, textvariable=self.var_freq_tr, width=10, state="readonly", values=("True", "False")
        ).grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(clk, text="Time Traceable").grid(row=4, column=2, sticky="e", padx=(12, 4))
        self.var_time_tr = tk.StringVar(value="True")
        ttk.Combobox(
            clk, textvariable=self.var_time_tr, width=10, state="readonly", values=("True", "False")
        ).grid(row=4, column=3, sticky="w", **pad)

        ttk.Label(
            body,
            text="Soft Master: TRex 포트 1개로 PTP 패킷 직접 생성 (SW timestamp, LOCK 어려움)\n"
            "Relay (GM→RU): 상용 GM → Port 0 수신 → T1/T4 수정 → Port 1 → RU (GM 품질 유지, LOCK 가능)",
            foreground="#475569",
            wraplength=920,
            justify="left",
        ).pack(fill="x", padx=14, pady=(2, 4))

        # --- T1/T4 Shake ---
        shake = ttk.LabelFrame(body, text="T1 / T4 Shake (nanoseconds)", padding=8)
        shake.pack(fill="x", padx=10, pady=4)

        ttk.Label(shake, text="T1 offset").grid(row=0, column=0, sticky="w")
        self.var_t1_off = tk.StringVar(value="0")
        ttk.Entry(shake, textvariable=self.var_t1_off, width=12).grid(row=0, column=1, **pad)
        ttk.Label(shake, text="T1 jitter ±").grid(row=0, column=2, sticky="w")
        self.var_t1_jit = tk.StringVar(value="0")
        ttk.Entry(shake, textvariable=self.var_t1_jit, width=12).grid(row=0, column=3, **pad)
        ttk.Label(shake, text="T1 drift/step").grid(row=0, column=4, sticky="w")
        self.var_t1_drift = tk.StringVar(value="0")
        ttk.Entry(shake, textvariable=self.var_t1_drift, width=12).grid(row=0, column=5, **pad)

        ttk.Label(shake, text="T4 offset").grid(row=1, column=0, sticky="w")
        self.var_t4_off = tk.StringVar(value="0")
        ttk.Entry(shake, textvariable=self.var_t4_off, width=12).grid(row=1, column=1, **pad)
        ttk.Label(shake, text="T4 jitter ±").grid(row=1, column=2, sticky="w")
        self.var_t4_jit = tk.StringVar(value="0")
        ttk.Entry(shake, textvariable=self.var_t4_jit, width=12).grid(row=1, column=3, **pad)
        ttk.Label(shake, text="T4 drift/step").grid(row=1, column=4, sticky="w")
        self.var_t4_drift = tk.StringVar(value="0")
        ttk.Entry(shake, textvariable=self.var_t4_drift, width=12).grid(row=1, column=5, **pad)

        ttk.Label(shake, text="T1 Random max").grid(row=2, column=0, sticky="w")
        self.var_t1_rand_max = tk.StringVar(value="1000000")
        ttk.Entry(shake, textvariable=self.var_t1_rand_max, width=12).grid(row=2, column=1, **pad)
        self.var_t1_rand = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            shake,
            text="T1 Random (매 Sync마다 0~max)",
            variable=self.var_t1_rand,
            command=self._on_random_toggled,
        ).grid(row=2, column=2, columnspan=2, sticky="w", **pad)

        ttk.Label(shake, text="T4 Random max").grid(row=3, column=0, sticky="w")
        self.var_t4_rand_max = tk.StringVar(value="1000000")
        ttk.Entry(shake, textvariable=self.var_t4_rand_max, width=12).grid(row=3, column=1, **pad)
        self.var_t4_rand = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            shake,
            text="T4 Random (매 Delay_Resp마다 0~max)",
            variable=self.var_t4_rand,
            command=self._on_random_toggled,
        ).grid(row=3, column=2, columnspan=2, sticky="w", **pad)

        # --- Buttons ---
        btn = ttk.Frame(self)
        btn.pack(fill="x", padx=10, pady=6)
        self.btn_start = ttk.Button(btn, text="Start Master", command=self.start_master)
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_stop = ttk.Button(btn, text="Stop", command=self.stop_master, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.btn_apply = ttk.Button(btn, text="Apply Config", command=self.apply_config)
        self.btn_apply.pack(side="left", padx=(0, 8))
        self.btn_wire = ttk.Button(btn, text="Wire Check", command=self.wire_check)
        self.btn_wire.pack(side="left", padx=(0, 8))
        ttk.Button(btn, text="Save Settings", command=self._save_gui_settings).pack(side="left")
        self.var_status = tk.StringVar(value="Idle")
        ttk.Label(btn, textvariable=self.var_status, foreground="#0f766e").pack(side="left", padx=16)

        preset = ttk.Frame(btn)
        preset.pack(side="right")
        ttk.Button(preset, text="Preset +1ms T1", command=lambda: self._preset(1_000_000, 0)).pack(
            side="left", padx=2
        )
        ttk.Button(preset, text="Preset ±100µs both", command=lambda: self._preset_jitter(100_000)).pack(
            side="left", padx=2
        )
        ttk.Button(preset, text="Enable T1 Random 0~1ms", command=self._preset_t1_random).pack(
            side="left", padx=2
        )

        logf = ttk.LabelFrame(self, text="Log", padding=6)
        logf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.txt = scrolledtext.ScrolledText(logf, height=12, font=("Consolas", 10))
        self.txt.pack(fill="both", expand=True)

    def _set_var(self, var: tk.Variable, value: Any) -> None:
        try:
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("" if value is None else str(value))
        except Exception:
            pass

    def _load_gui_settings(self) -> None:
        s = load_settings()
        self._set_var(self.var_mode, s.get("mode", "remote"))
        self._set_var(self.var_ssh_host, s.get("ssh_host"))
        self._set_var(self.var_ssh_port, s.get("ssh_port"))
        self._set_var(self.var_ssh_user, s.get("ssh_user"))
        self._set_var(self.var_ssh_pw, s.get("ssh_password"))
        self._set_var(self.var_trex_path, s.get("trex_path"))
        self._set_var(self.var_trex_rpc, s.get("trex_rpc"))
        self._set_var(self.var_remote_dir, s.get("remote_dir"))
        self._set_var(self.var_sudo, s.get("sudo_agent"))
        self._set_var(self.var_autostart_trex, s.get("autostart_trex"))
        self._set_var(self.var_backend, s.get("backend"))
        self._set_var(self.var_trex_cores, s.get("trex_cores"))
        self._set_var(self.var_trex_port, s.get("trex_port"))
        self._set_var(self.var_iface, s.get("kernel_iface"))
        self._set_var(self.var_src_mac, s.get("src_mac"))
        self._set_var(self.var_vlan, s.get("vlan"))
        self._set_var(self.var_domain, s.get("domain"))
        self._set_var(self.var_sync_type, s.get("sync_type"))
        self._set_var(self.var_link_local, s.get("link_local_mcast"))
        self._set_var(self.var_dst_mac, s.get("dst_mac"))
        self._set_var(self.var_encap, s.get("encapsulation"))
        self._set_var(self.var_ann_rate, s.get("announce_rate"))
        self._set_var(self.var_sync_rate, s.get("sync_rate"))
        self._set_var(self.var_dreq_rate, s.get("delay_req_rate"))
        self._set_var(self.var_p1, s.get("priority1"))
        self._set_var(self.var_p2, s.get("priority2"))
        self._set_var(self.var_class, s.get("clock_class"))
        self._set_var(self.var_time_src, s.get("time_source"))
        self._set_var(self.var_accuracy, s.get("clock_accuracy"))
        self._set_var(self.var_utc, s.get("utc_offset"))
        self._set_var(self.var_freq_tr, s.get("freq_traceable"))
        self._set_var(self.var_time_tr, s.get("time_traceable"))
        self._set_var(self.var_t1_off, s.get("t1_offset"))
        self._set_var(self.var_t1_jit, s.get("t1_jitter"))
        self._set_var(self.var_t1_drift, s.get("t1_drift"))
        self._set_var(self.var_t4_off, s.get("t4_offset"))
        self._set_var(self.var_t4_jit, s.get("t4_jitter"))
        self._set_var(self.var_t4_drift, s.get("t4_drift"))
        self._set_var(self.var_t1_rand, s.get("t1_random"))
        self._set_var(self.var_t1_rand_max, s.get("t1_random_max"))
        self._set_var(self.var_t4_rand, s.get("t4_random"))
        self._set_var(self.var_t4_rand_max, s.get("t4_random_max"))
        self._set_var(self.var_relay_rx, s.get("relay_rx_port"))
        self._set_var(self.var_relay_tx, s.get("relay_tx_port"))
        self._set_var(self.var_relay_corr, s.get("relay_correction_ns"))
        self._on_mode_changed()
        self._log(f"[GUI] settings loaded from {CONFIG_FILE}")

    def _collect_gui_settings(self) -> dict[str, Any]:
        return {
            "mode": self.var_mode.get(),
            "ssh_host": self.var_ssh_host.get().strip(),
            "ssh_port": self.var_ssh_port.get().strip(),
            "ssh_user": self.var_ssh_user.get().strip(),
            "ssh_password": self.var_ssh_pw.get(),
            "trex_path": self.var_trex_path.get().strip(),
            "trex_rpc": self.var_trex_rpc.get().strip(),
            "remote_dir": self.var_remote_dir.get().strip(),
            "sudo_agent": bool(self.var_sudo.get()),
            "autostart_trex": bool(self.var_autostart_trex.get()),
            "backend": self.var_backend.get(),
            "trex_cores": self.var_trex_cores.get().strip(),
            "trex_port": self.var_trex_port.get().strip(),
            "kernel_iface": self.var_iface.get().strip(),
            "src_mac": self.var_src_mac.get().strip(),
            "vlan": self.var_vlan.get().strip(),
            "domain": self.var_domain.get().strip(),
            "sync_type": self.var_sync_type.get(),
            "link_local_mcast": bool(self.var_link_local.get()),
            "dst_mac": self.var_dst_mac.get().strip(),
            "encapsulation": self.var_encap.get(),
            "announce_rate": self.var_ann_rate.get(),
            "sync_rate": self.var_sync_rate.get(),
            "delay_req_rate": self.var_dreq_rate.get(),
            "priority1": self.var_p1.get().strip(),
            "priority2": self.var_p2.get().strip(),
            "clock_class": self.var_class.get(),
            "time_source": self.var_time_src.get(),
            "clock_accuracy": self.var_accuracy.get(),
            "utc_offset": self.var_utc.get().strip(),
            "freq_traceable": self.var_freq_tr.get(),
            "time_traceable": self.var_time_tr.get(),
            "t1_offset": self.var_t1_off.get().strip(),
            "t1_jitter": self.var_t1_jit.get().strip(),
            "t1_drift": self.var_t1_drift.get().strip(),
            "t4_offset": self.var_t4_off.get().strip(),
            "t4_jitter": self.var_t4_jit.get().strip(),
            "t4_drift": self.var_t4_drift.get().strip(),
            "t1_random": bool(self.var_t1_rand.get()),
            "t1_random_max": self.var_t1_rand_max.get().strip(),
            "t4_random": bool(self.var_t4_rand.get()),
            "t4_random_max": self.var_t4_rand_max.get().strip(),
            "relay_rx_port": self.var_relay_rx.get().strip(),
            "relay_tx_port": self.var_relay_tx.get().strip(),
            "relay_correction_ns": self.var_relay_corr.get().strip(),
        }

    def _save_gui_settings(self, quiet: bool = False) -> None:
        try:
            save_settings(self._collect_gui_settings())
            if not quiet:
                self._log(f"[GUI] settings saved -> {CONFIG_FILE}")
        except Exception as exc:
            if not quiet:
                self._log(f"[GUI] settings save failed: {exc}")

    def _on_close(self) -> None:
        self._save_gui_settings(quiet=True)
        self.destroy()

    def _on_mode_changed(self) -> None:
        remote = self._is_remote()
        state = "normal" if remote else "disabled"
        for child in self.frm_ssh.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass
        if not remote:
            self.disconnect_remote(silent=True)
            self._set_ifaces(list_interfaces())
            self.cmb_trex_port["values"] = ()
            self.var_trex_port.set("")
        else:
            self.cmb_iface["values"] = ()
            self.var_iface.set("")
            if self._remote and self._remote.connected:
                self.var_ssh_status.set(f"SSH: connected {self._remote.host}")
            else:
                self.var_ssh_status.set("SSH: disconnected")
        self._on_backend_changed()

    def _on_backend_changed(self) -> None:
        backend = self.var_backend.get()
        is_remote = self._is_remote()
        is_trex = backend == "trex" and is_remote
        is_relay = backend == "relay" and is_remote
        try:
            self.cmb_trex_port.configure(state="readonly" if is_trex else "disabled")
            self.cmb_iface.configure(state="disabled" if (is_trex or is_relay) else "normal")
        except tk.TclError:
            pass
        # Show/hide relay frame
        if is_relay:
            self.frm_relay.pack(fill="x", padx=10, pady=4)
        else:
            try:
                self.frm_relay.pack_forget()
            except Exception:
                pass

    def _set_ifaces(self, vals: list[str]) -> None:
        self.cmb_iface["values"] = vals
        if vals and self.var_iface.get() not in vals:
            self.var_iface.set(vals[0])
        if not vals:
            self.var_iface.set("")

    def _set_trex_ports(self, ports: list[dict]) -> None:
        labels = []
        self._trex_port_map = {}
        for p in ports:
            idx = int(p.get("port", 0))
            link = "UP" if p.get("link_up") else "DOWN"
            mac = p.get("src_mac") or "?"
            spd = p.get("speed") or "?"
            label = f"{idx}  link={link}  speed={spd}  mac={mac}"
            labels.append(label)
            self._trex_port_map[label] = p
            self._trex_port_map[str(idx)] = p
        self.cmb_trex_port["values"] = labels
        self.cmb_relay_rx["values"] = labels
        self.cmb_relay_tx["values"] = labels
        if labels:
            self.var_trex_port.set(labels[0])
            mac = ports[0].get("src_mac") or ""
            if mac and not self.var_src_mac.get().strip():
                self.var_src_mac.set(str(mac).lower())
            if len(labels) >= 2:
                if not self.var_relay_rx.get().strip() or self.var_relay_rx.get() not in labels:
                    self.var_relay_rx.set(labels[0])
                if not self.var_relay_tx.get().strip() or self.var_relay_tx.get() not in labels:
                    self.var_relay_tx.set(labels[1])
        else:
            self.var_trex_port.set("")

    def _selected_trex_port(self) -> int:
        raw = self.var_trex_port.get().strip()
        if raw in self._trex_port_map:
            return int(self._trex_port_map[raw].get("port", 0))
        try:
            return int(raw.split()[0])
        except Exception as exc:
            raise ValueError("TRex Port를 선택하세요") from exc

    def _selected_relay_rx_port(self) -> int:
        raw = self.var_relay_rx.get().strip()
        if raw in self._trex_port_map:
            return int(self._trex_port_map[raw].get("port", 0))
        try:
            return int(raw.split()[0])
        except Exception as exc:
            raise ValueError("Relay RX Port(GM)를 선택하세요") from exc

    def _selected_relay_tx_port(self) -> int:
        raw = self.var_relay_tx.get().strip()
        if raw in self._trex_port_map:
            return int(self._trex_port_map[raw].get("port", 0))
        try:
            return int(raw.split()[0])
        except Exception as exc:
            raise ValueError("Relay TX Port(RU)를 선택하세요") from exc

    def _refresh_ifaces(self) -> None:
        if self._is_remote():
            if not self._remote or not self._remote.connected:
                messagebox.showinfo("Refresh", "먼저 Connect & Deploy 하세요.")
                return
            try:
                if self.var_backend.get() == "trex":
                    ports = self._remote.list_trex_ports(
                        self.var_trex_path.get().strip() or "/home/slab/trex/v3.08",
                        self.var_trex_rpc.get().strip() or "127.0.0.1",
                    )
                    self._set_trex_ports(ports)
                    self._log(f"[GUI] TRex ports: {len(ports)}")
                else:
                    vals = self._remote.list_ifaces()
                    self._set_ifaces(vals)
                    self._log(f"[GUI] remote ifaces: {vals}")
            except Exception as exc:
                messagebox.showerror("Refresh failed", str(exc))
            return
        self._set_ifaces(list_interfaces())

    def connect_remote(self) -> None:
        if self._busy:
            return
        if not self._is_remote():
            self.var_mode.set("remote")
            self._on_mode_changed()
        host = self.var_ssh_host.get().strip()
        user = self.var_ssh_user.get().strip()
        pw = self.var_ssh_pw.get()
        if not host or not user:
            messagebox.showerror("SSH", "Host / User를 입력하세요.")
            return
        try:
            port = int(self.var_ssh_port.get().strip() or "22")
        except ValueError:
            messagebox.showerror("SSH", "Port must be integer")
            return

        self._busy = True
        self.btn_ssh_connect.configure(state="disabled")
        self.var_ssh_status.set("SSH: connecting...")
        trex_path = self.var_trex_path.get().strip() or "/home/slab/trex/v3.08"
        trex_rpc = self.var_trex_rpc.get().strip() or "127.0.0.1"
        autostart = bool(self.var_autostart_trex.get())
        try:
            cores = int(self.var_trex_cores.get().strip() or "6")
        except ValueError:
            cores = 6

        def _job() -> None:
            err: Optional[str] = None
            warn: Optional[str] = None
            client: Optional[RemotePtpClient] = None
            ports: list = []
            ifaces: list = []
            try:
                if self._remote is not None:
                    try:
                        self._remote.close()
                    except Exception:
                        pass
                    self._remote = None
                client = RemotePtpClient(
                    host,
                    user,
                    pw,
                    port=port,
                    remote_dir=self.var_remote_dir.get().strip() or "/tmp/ptp_softmaster",
                    use_sudo=bool(self.var_sudo.get()),
                    log=self._log,
                )
                client.connect_and_start_agent()
                client.set_trex(trex_path, trex_rpc)

                # Ensure TRex RPC before listing ports
                if not client.trex_port_open(trex_rpc, 4501):
                    self._log("[GUI] TRex RPC :4501 down")
                    if autostart:
                        self._log("[GUI] auto-starting TRex engine ...")
                        ok = client.start_trex_daemon(
                            trex_path, cores=cores, rpc_server=trex_rpc, wait_sec=90
                        )
                        if not ok:
                            warn = (
                                "SSH OK but TRex RPC :4501 not ready.\n"
                                "Start TRex 버튼을 누르거나 DDoS GUI로 엔진을 먼저 기동하세요.\n"
                                "로그의 [TREX-RUN] 메시지를 확인하세요."
                            )
                    else:
                        warn = (
                            "TRex 데몬이 꺼져 있습니다 (RPC :4501).\n"
                            "'TRex 꺼져있으면 자동 기동'을 켜거나 Start TRex / DDoS GUI로 기동하세요."
                        )

                try:
                    ports = client.list_trex_ports(trex_path, trex_rpc)
                except Exception as port_exc:
                    warn = (warn + "\n" if warn else "") + f"list_trex_ports: {port_exc}"
                    ports = []
                try:
                    ifaces = client.list_ifaces()
                except Exception:
                    ifaces = []
            except Exception as exc:
                err = str(exc)
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = None
                ports, ifaces = [], []

            def _done() -> None:
                self._busy = False
                self.btn_ssh_connect.configure(state="normal")
                if err:
                    self._remote = None
                    self.var_ssh_status.set("SSH: failed")
                    self.btn_ssh_disconnect.configure(state="disabled")
                    self.btn_start_trex.configure(state="disabled")
                    messagebox.showerror("SSH connect failed", err)
                    return
                self._remote = client
                self._set_trex_ports(ports)
                self._set_ifaces(ifaces)
                self.btn_ssh_disconnect.configure(state="normal")
                self.btn_start_trex.configure(state="normal")
                self._on_backend_changed()
                if ports:
                    self.var_ssh_status.set(
                        f"SSH: connected {host}  TRex ports={len(ports)}"
                    )
                else:
                    self.var_ssh_status.set(
                        f"SSH: connected {host}  TRex ports=0 (engine down?)"
                    )
                self._log(f"[GUI] ready trex_ports={len(ports)} ifaces={ifaces}")
                if warn:
                    messagebox.showwarning("TRex not ready", warn)
                self._save_gui_settings(quiet=True)

            self.after(0, _done)

        threading.Thread(target=_job, name="ssh-connect", daemon=True).start()

    def start_trex_engine(self) -> None:
        if not self._remote or not self._remote.connected:
            messagebox.showinfo("Start TRex", "먼저 Connect & Deploy 하세요.")
            return
        if self._busy:
            return
        trex_path = self.var_trex_path.get().strip() or "/home/slab/trex/v3.08"
        trex_rpc = self.var_trex_rpc.get().strip() or "127.0.0.1"
        try:
            cores = int(self.var_trex_cores.get().strip() or "6")
        except ValueError:
            cores = 6

        self._busy = True
        self.btn_start_trex.configure(state="disabled")
        self.var_ssh_status.set("TRex: starting...")

        def _job() -> None:
            err = None
            ports: list = []
            try:
                assert self._remote is not None
                ok = self._remote.start_trex_daemon(
                    trex_path,
                    cores=cores,
                    rpc_server=trex_rpc,
                    wait_sec=90,
                    restart=True,
                )
                if not ok:
                    err = "TRex RPC :4501 not ready. /tmp/ptp_trex.log 를 확인하세요."
                else:
                    ports = self._remote.list_trex_ports(trex_path, trex_rpc)
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                self._busy = False
                self.btn_start_trex.configure(state="normal")
                if err:
                    self.var_ssh_status.set("TRex: start failed")
                    messagebox.showerror("Start TRex failed", err)
                    return
                self._set_trex_ports(ports)
                self.var_ssh_status.set(f"TRex: ready  ports={len(ports)}")
                messagebox.showinfo("Start TRex", f"TRex RPC ready. ports={len(ports)}")

            self.after(0, _done)

        threading.Thread(target=_job, name="trex-start", daemon=True).start()

    def disconnect_remote(self, silent: bool = False) -> None:
        remote = self._remote
        self._remote = None
        if remote is not None:
            try:
                if remote.is_master_running:
                    remote.stop_master()
            except Exception:
                pass
            try:
                remote.close()
            except Exception:
                pass
            if not silent:
                self._log("[GUI] SSH disconnected")
        self.var_ssh_status.set("SSH: disconnected")
        self.btn_ssh_disconnect.configure(state="disabled")
        try:
            self.btn_start_trex.configure(state="disabled")
        except Exception:
            pass
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if self._is_remote():
            self._set_ifaces([])
            self._set_trex_ports([])

    def _preset_t1_random(self) -> None:
        self.var_t1_rand_max.set("1000000")
        self.var_t1_rand.set(True)
        self.var_t4_rand.set(False)
        self._on_random_toggled()

    def _on_random_toggled(self) -> None:
        if self.var_t1_rand.get() or self.var_t4_rand.get():
            self._log(
                f"[GUI] Random mode  T1={self.var_t1_rand.get()} max={self.var_t1_rand_max.get()}  "
                f"T4={self.var_t4_rand.get()} max={self.var_t4_rand_max.get()}"
            )
        if self._master is not None or (self._remote and self._remote.is_master_running):
            try:
                self.apply_config()
            except Exception:
                pass

    def _preset(self, t1_off: int, t4_off: int) -> None:
        self.var_t1_off.set(str(t1_off))
        self.var_t4_off.set(str(t4_off))
        self.var_t1_jit.set("0")
        self.var_t4_jit.set("0")
        self.var_t1_drift.set("0")
        self.var_t4_drift.set("0")
        self.var_t1_rand.set(False)
        self.var_t4_rand.set(False)

    def _preset_jitter(self, j: int) -> None:
        self.var_t1_off.set("0")
        self.var_t4_off.set("0")
        self.var_t1_jit.set(str(j))
        self.var_t4_jit.set(str(j))
        self.var_t1_drift.set("0")
        self.var_t4_drift.set("0")
        self.var_t1_rand.set(False)
        self.var_t4_rand.set(False)

    def _log(self, msg: str) -> None:
        self._log_q.put(msg)

    def _engine_active(self) -> bool:
        if self._is_remote():
            return bool(self._remote and self._remote.is_master_running)
        return self._master is not None

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self._log_q.get_nowait()
                self.txt.insert("end", msg + "\n")
                self.txt.see("end")
        except queue.Empty:
            pass
        if self._is_remote() and self._remote and self._remote.is_master_running:
            s = self._remote.stats
            sd = getattr(self._remote, '_last_stats_raw', {}) or {}
            if sd.get("sync_relayed") is not None and self.var_backend.get() == "relay":
                self.var_status.set(
                    f"RELAY Sync={sd.get('sync_relayed',0)} FU={sd.get('follow_up_relayed',0)} "
                    f"Ann={sd.get('announce_relayed',0)} DReq={sd.get('delay_req_rx',0)} "
                    f"DFwd={sd.get('delay_req_fwd',0)} DResp={sd.get('delay_resp_relayed', sd.get('delay_resp_tx',0))} "
                    f"GM_RX={sd.get('gm_rx_total',0)}"
                )
            else:
                self.var_status.set(
                    f"RUN(remote) Ann={s.announce_sent} Sync={s.sync_sent} FU={s.follow_up_sent} "
                    f"DReq={s.delay_req_rx} DResp={s.delay_resp_tx}  "
                    f"lastT1adj={s.last_t1_ns_adj} lastT4adj={s.last_t4_ns_adj}"
                )
        elif self._master is not None:
            s = self._master.stats
            self.var_status.set(
                f"RUN(local) Ann={s.announce_sent} Sync={s.sync_sent} FU={s.follow_up_sent} "
                f"DReq={s.delay_req_rx} DResp={s.delay_resp_tx}  "
                f"lastT1adj={s.last_t1_ns_adj} lastT4adj={s.last_t4_ns_adj}"
            )
        self.after(200, self._drain_log)

    def _parse_cfg(self) -> MasterConfig:
        def i(v: tk.StringVar, name: str) -> int:
            try:
                return int(str(v.get()).strip() or "0")
            except ValueError as exc:
                raise ValueError(f"{name} must be integer") from exc

        backend = self.var_backend.get() if self._is_remote() else "kernel"
        if backend == "trex":
            # iface unused; placeholder for serialization
            iface = f"trex:{self._selected_trex_port()}"
        else:
            iface = self.var_iface.get().strip()
            if not iface:
                raise ValueError("NIC를 선택하세요")

        two_step = self.var_sync_type.get().strip().startswith("2")
        cls_label = self.var_class.get()
        acc_label = self.var_accuracy.get()
        src_label = self.var_time_src.get()
        if cls_label not in CLOCK_CLASS:
            raise ValueError(f"unknown Class: {cls_label}")
        if acc_label not in CLOCK_ACCURACY:
            raise ValueError(f"unknown Clock Accuracy: {acc_label}")
        if src_label not in TIME_SOURCE:
            raise ValueError(f"unknown Time Source: {src_label}")

        return MasterConfig(
            iface=iface,
            src_mac=self.var_src_mac.get().strip(),
            dst_mac=self.var_dst_mac.get().strip() or PTP_MCAST_DEFAULT,
            use_link_local_mcast=bool(self.var_link_local.get()),
            vlan=i(self.var_vlan, "VLAN"),
            domain=i(self.var_domain, "Domain"),
            encapsulation=self.var_encap.get().strip() or "None",
            two_step=two_step,
            announce_per_sec=_parse_rate(self.var_ann_rate.get()),
            sync_per_sec=_parse_rate(self.var_sync_rate.get()),
            delay_req_per_sec=_parse_rate(self.var_dreq_rate.get()),
            priority1=i(self.var_p1, "Priority 1") & 0xFF,
            priority2=i(self.var_p2, "Priority 2") & 0xFF,
            clock_class=CLOCK_CLASS[cls_label],
            clock_accuracy=CLOCK_ACCURACY[acc_label],
            time_source=TIME_SOURCE[src_label],
            utc_offset_s=i(self.var_utc, "UTC Offset"),
            freq_traceable=self.var_freq_tr.get().strip().lower() == "true",
            time_traceable=self.var_time_tr.get().strip().lower() == "true",
            t1_offset_ns=i(self.var_t1_off, "T1 offset"),
            t1_jitter_ns=abs(i(self.var_t1_jit, "T1 jitter")),
            t4_offset_ns=i(self.var_t4_off, "T4 offset"),
            t4_jitter_ns=abs(i(self.var_t4_jit, "T4 jitter")),
            t1_drift_step_ns=i(self.var_t1_drift, "T1 drift"),
            t4_drift_step_ns=i(self.var_t4_drift, "T4 drift"),
            t1_random_enable=bool(self.var_t1_rand.get()),
            t1_random_max_ns=max(0, i(self.var_t1_rand_max, "T1 Random max")),
            t4_random_enable=bool(self.var_t4_rand.get()),
            t4_random_max_ns=max(0, i(self.var_t4_rand_max, "T4 Random max")),
        )

    def _build_relay_config(self) -> dict:
        def i(v: tk.StringVar, name: str) -> int:
            try:
                return int(str(v.get()).strip() or "0")
            except ValueError as exc:
                raise ValueError(f"{name} must be integer") from exc

        return {
            "domain": i(self.var_domain, "Domain"),
            "vlan": i(self.var_vlan, "VLAN"),
            "t1_offset_ns": i(self.var_t1_off, "T1 offset"),
            "t1_jitter_ns": abs(i(self.var_t1_jit, "T1 jitter")),
            "t1_drift_step_ns": i(self.var_t1_drift, "T1 drift"),
            "t1_random_enable": bool(self.var_t1_rand.get()),
            "t1_random_max_ns": max(0, i(self.var_t1_rand_max, "T1 Random max")),
            "t4_offset_ns": i(self.var_t4_off, "T4 offset"),
            "t4_jitter_ns": abs(i(self.var_t4_jit, "T4 jitter")),
            "t4_drift_step_ns": i(self.var_t4_drift, "T4 drift"),
            "t4_random_enable": bool(self.var_t4_rand.get()),
            "t4_random_max_ns": max(0, i(self.var_t4_rand_max, "T4 Random max")),
            "correction_offset_ns": i(self.var_relay_corr, "Correction offset"),
        }

    def apply_config(self) -> None:
        if not self._engine_active():
            messagebox.showinfo("Apply Config", "먼저 Start Master 하세요.")
            return
        try:
            if self.var_backend.get() == "relay" and self._is_remote():
                relay_cfg = self._build_relay_config()
                assert self._remote is not None
                self._remote.update_relay(relay_cfg)
                self._log(f"[GUI] relay config applied  t1_off={relay_cfg['t1_offset_ns']} t4_off={relay_cfg['t4_offset_ns']}")
            else:
                cfg = self._parse_cfg()
                if self._is_remote():
                    assert self._remote is not None
                    self._remote.update_config(cfg)
                else:
                    assert self._master is not None
                    self._master.update_config(cfg)
                self._log(
                    f"[GUI] applied  domain={cfg.domain} sync={_rate_label(cfg.sync_per_sec)} "
                    f"ann={_rate_label(cfg.announce_per_sec)} two_step={cfg.two_step}"
                )
        except Exception as exc:
            messagebox.showerror("Apply failed", str(exc))

    def wire_check(self) -> None:
        if not self._is_remote() or not self._remote or not self._remote.connected:
            messagebox.showinfo("Wire Check", "Remote SSH 연결 후 사용하세요.")
            return
        backend = self.var_backend.get()
        self.btn_wire.configure(state="disabled")
        self._log(f"[GUI] Wire Check backend={backend}...")

        def _job() -> None:
            err = None
            resp = None
            try:
                assert self._remote is not None
                if backend == "relay":
                    resp = self._remote.wire_check(
                        backend="trex",
                        port=self._selected_relay_tx_port(),
                    )
                elif backend == "trex":
                    resp = self._remote.wire_check(
                        backend="trex",
                        port=self._selected_trex_port(),
                    )
                else:
                    iface = self.var_iface.get().strip()
                    resp = self._remote.wire_check(iface=iface, seconds=2, backend="kernel")
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                self.btn_wire.configure(state="normal")
                if err:
                    messagebox.showerror("Wire Check failed", err)
                    return
                assert resp is not None
                if backend == "trex":
                    info = resp.get("port_info") or {}
                    st = resp.get("stats") or {}
                    self._log(
                        f"[WIRE] trex port={resp.get('port')} link_up={info.get('link_up')} "
                        f"running={resp.get('running')} sync={st.get('sync_sent')} "
                        f"ann={st.get('announce_sent')}"
                    )
                    if not info.get("link_up", True):
                        messagebox.showwarning("Wire Check", "TRex port link DOWN")
                    elif not resp.get("running"):
                        messagebox.showinfo("Wire Check", "포트 정보는 OK. Start Master 후 다시 확인하세요.")
                    else:
                        messagebox.showinfo(
                            "Wire Check",
                            f"TRex port link UP, Sync={st.get('sync_sent')} Announce={st.get('announce_sent')}\n"
                            "장치에 안 보이면 Domain/VLAN/DST MAC을 확인하세요.",
                        )
                    return
                hits = int(resp.get("hit_count") or 0)
                tx_delta = resp.get("tx_packets_delta")
                self._log(f"[WIRE] hit_count={hits} tx_delta={tx_delta}")
                messagebox.showinfo("Wire Check", f"hits={hits} tx_delta={tx_delta}")

            self.after(0, _done)

        threading.Thread(target=_job, name="wire-check", daemon=True).start()

    def start_master(self) -> None:
        if self._engine_active():
            return
        try:
            cfg = self._parse_cfg()
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))
            return

        if self._is_remote():
            if not self._remote or not self._remote.connected:
                messagebox.showerror("Start failed", "먼저 Connect & Deploy 하세요.")
                return
            try:
                backend = self.var_backend.get()
                if backend == "relay":
                    relay_cfg = self._build_relay_config()
                    self._remote.start_relay(
                        relay_cfg,
                        trex_path=self.var_trex_path.get().strip() or "/home/slab/trex/v3.08",
                        rx_port=self._selected_relay_rx_port(),
                        tx_port=self._selected_relay_tx_port(),
                        rpc_server=self.var_trex_rpc.get().strip() or "127.0.0.1",
                    )
                elif backend == "trex":
                    self._remote.start_master(
                        cfg,
                        backend="trex",
                        trex_path=self.var_trex_path.get().strip() or "/home/slab/trex/v3.08",
                        trex_port=self._selected_trex_port(),
                        rpc_server=self.var_trex_rpc.get().strip() or "127.0.0.1",
                    )
                else:
                    self._remote.start_master(cfg, backend="kernel")
                self.btn_start.configure(state="disabled")
                self.btn_stop.configure(state="normal")
                self.var_status.set(f"RUN({backend})")
                self._save_gui_settings(quiet=True)
            except Exception as exc:
                messagebox.showerror("Start failed", str(exc))
            return

        try:
            m = SoftPtpMaster(cfg, log=self._log)
            m.start()
            self._master = m
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.var_status.set("RUN(local)")
            self._save_gui_settings(quiet=True)
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))

    def stop_master(self) -> None:
        if self._is_remote():
            if self._remote is not None:
                try:
                    self._remote.stop_master()
                except Exception as exc:
                    self._log(f"[GUI] stop error: {exc}")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.var_status.set("Idle")
            return
        if self._master is None:
            return
        try:
            self._master.stop()
        except Exception:
            pass
        self._master = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.var_status.set("Idle")

    def destroy(self) -> None:
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:
            pass
        try:
            self.stop_master()
        except Exception:
            pass
        try:
            self.disconnect_remote(silent=True)
        except Exception:
            pass
        super().destroy()


def main() -> None:
    app = SoftPtpMasterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
