# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — O-RAN Protocol Analyzer GUI

from pathlib import Path

block_cipher = None
root = Path(SPECPATH)

hidden = [
    "oran_msg",
    "oran_advanced",
    "scapy",
    "scapy.all",
    "scapy.layers.inet",
    "scapy.layers.inet6",
    "scapy.layers.l2",
    "scapy.packet",
    "scapy.utils",
    "scapy.plist",
    "scapy.arch",
    "scapy.arch.windows",
    "scapy.layers.all",
]

a = Analysis(
    [str(root / "PCAP.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["scapy.arch.linux", "scapy.arch.bpf"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="O-RAN-Protocol-Analyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
