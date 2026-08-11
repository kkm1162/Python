#!/usr/bin/env python3
"""Minimal jq stand-in for O-RAN Conformance scripts when real jq is missing.

Supports the patterns used in this repo:
  jq -r '.["management-configurations"]["NETCONF-ID"] // empty' file.json
  jq -r '.["some"]["key"] // empty' file.json
"""
from __future__ import annotations

import json
import re
import sys


def _extract_keys(expr: str) -> list[str]:
    return re.findall(r'\["([^"]+)"\]', expr)


def _lookup(data: object, keys: list[str]) -> object:
    cur: object = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return ""
        cur = cur[k]
    if cur is None:
        return ""
    return cur


def main() -> int:
    args = sys.argv[1:]
    raw = False
    if args and args[0] == "-r":
        raw = True
        args = args[1:]
    if not args:
        print("jq_shim: usage: jq -r '<expr>' <file>", file=sys.stderr)
        return 2
    expr = args[0]
    path = args[1] if len(args) > 1 else None
    try:
        if path:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)
    except Exception as exc:
        print(f"jq_shim: cannot read json: {exc}", file=sys.stderr)
        return 1

    keys = _extract_keys(expr)
    if not keys and expr.strip() not in (".",):
        # Unsupported filter — fail visibly so scripts do not silently get empty values.
        print(f"jq_shim: unsupported filter: {expr}", file=sys.stderr)
        return 1

    val = data if not keys else _lookup(data, keys)
    if isinstance(val, (dict, list)):
        print(json.dumps(val, ensure_ascii=True))
    else:
        if raw:
            print("" if val is None else str(val))
        else:
            print(json.dumps(val, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
