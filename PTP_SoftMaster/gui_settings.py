#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Load/save Soft PTP Master GUI settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(__file__).resolve().parent / "ptp_softmaster_gui.json"

DEFAULTS: dict[str, Any] = {
    "mode": "remote",
    "ssh_host": "192.168.9.249",
    "ssh_port": "22",
    "ssh_user": "slab",
    "ssh_password": "",
    "trex_path": "/home/slab/trex/v3.08",
    "trex_rpc": "127.0.0.1",
    "remote_dir": "/tmp/ptp_softmaster",
    "sudo_agent": False,
    "autostart_trex": True,
    "backend": "trex",
    "trex_cores": "6",
    "trex_port": "",
    "kernel_iface": "",
    "src_mac": "",
    "vlan": "0",
    "domain": "24",
    "sync_type": "2 Step",
    "link_local_mcast": False,
    "dst_mac": "01:1b:19:00:00:00",
    "encapsulation": "None",
    "announce_rate": "8 per second",
    "sync_rate": "8 per second",
    "delay_req_rate": "16 per second",
    "priority1": "128",
    "priority2": "255",
    "clock_class": "Primary (6)",
    "time_source": "Internal Oscillator",
    "clock_accuracy": "Within 100 ns",
    "utc_offset": "37",
    "freq_traceable": "True",
    "time_traceable": "True",
    "t1_offset": "0",
    "t1_jitter": "0",
    "t1_drift": "0",
    "t4_offset": "0",
    "t4_jitter": "0",
    "t4_drift": "0",
    "t1_random": False,
    "t1_random_max": "1000000",
    "t4_random": False,
    "t4_random_max": "1000000",
    # Relay mode
    "relay_mode": False,
    "relay_rx_port": "0",
    "relay_tx_port": "1",
    "relay_correction_ns": "0",
}


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULTS)
    if CONFIG_FILE.is_file():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    return data


def save_settings(data: dict[str, Any]) -> None:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in data.items() if k in DEFAULTS})
    try:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
