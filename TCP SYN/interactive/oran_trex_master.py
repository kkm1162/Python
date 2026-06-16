#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
O-RAN O-RU DDoS Validation System v6.2 — entry point.

구현은 `oran_validation` 패키지에 모듈별로 분리되어 있습니다.
"""
from oran_validation.gui_app import main

if __name__ == "__main__":
    main()

# python -m PyInstaller --noconsole --onefile --icon="DDOS.ico" oran_trex_master.py
