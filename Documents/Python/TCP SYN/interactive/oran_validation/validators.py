# -*- coding: utf-8 -*-
"""IP/MAC/path validation and safe remote shell quoting."""

import ipaddress
import posixpath
import re
import shlex


def is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
        return True
    except Exception:
        return False


def is_valid_mac(value: str) -> bool:
    return bool(re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value.strip()))


def is_linux_abs_path(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if "\\" in value:
        return False
    return value.startswith("/")


def sanitize_remote_filename(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("PCAP 파일명이 비어 있습니다.")
    if "/" in name or "\\" in name:
        raise ValueError("PCAP 파일명에는 경로 구분자를 포함할 수 없습니다.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("PCAP 파일명은 영문/숫자/._- 만 사용할 수 있습니다.")
    if name.startswith("."):
        raise ValueError("PCAP 파일명은 점(.)으로 시작할 수 없습니다.")
    if not name.lower().endswith(".pcap"):
        name += ".pcap"
    return name


def validate_remote_path(path_value: str, label: str) -> str:
    path_value = path_value.strip()
    if not is_linux_abs_path(path_value):
        raise ValueError(f"{label}: Linux 절대경로만 허용됩니다.")
    dangerous_patterns = ["..", "~", ";", "|", "&", "$", "`", ">", "<"]
    for pat in dangerous_patterns:
        if pat in path_value:
            raise ValueError(f"{label}: 허용되지 않는 경로 문자가 포함되어 있습니다. ({pat})")
    normalized = posixpath.normpath(path_value)
    if not normalized.startswith("/"):
        raise ValueError(f"{label}: 올바른 Linux 절대경로가 아닙니다.")
    return normalized


def quote_remote(value: str) -> str:
    return shlex.quote(value)
