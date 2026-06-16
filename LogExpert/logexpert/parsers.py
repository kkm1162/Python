from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from .models import LogEvent

SYSLOG_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<facility>[a-z0-9]+\.[a-z]+)\s+"
    r"(?P<proc>[^:\[]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<body>.*)$"
)
NETCONF_HEADER_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:(?:.*\bsession\s+(?P<session>\S+):?\s+)?"
    r"(?P<dir>received|sending)(?:\s+\S+)*\s+message)"
    r".*$",
    re.IGNORECASE,
)
NETCONF_MESSAGE_MARKER_RE = re.compile(
    r"\bsession\s+(?P<session>[^\s:]+)\s*:\s*(?P<dir>received|sending)\s+message\b",
    re.IGNORECASE,
)
MESSAGE_ID_RE = re.compile(r'message-id="([^"]+)"')
LOOSE_TS_RE = re.compile(
    r"(?:<\d+>)?(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})"
)
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
TEXT_ENCODINGS = (
    "utf-8",
    "utf-8-sig",
)


def parse_log_files(kind: str, file_paths: list[str]) -> list[LogEvent]:
    events: list[LogEvent] = []
    sequence = 0
    for file_path in file_paths:
        for event in iter_events_for_file(kind, Path(file_path)):
            event.sequence = sequence
            sequence += 1
            events.append(event)
    return sort_events(events)


def iter_events_for_file(kind: str, path: Path) -> Iterable[LogEvent]:
    if kind == "netconf":
        yield from iter_netconf_events(path)
    else:
        yield from iter_syslog_like_events(path)


def sort_events(events: Iterable[LogEvent]) -> list[LogEvent]:
    return sorted(
        events,
        key=lambda e: (
            e.timestamp or datetime.min,
            e.sequence,
        ),
    )


def parse_syslog_like_file(path: Path, kind: str) -> list[LogEvent]:
    return list(iter_syslog_like_events(path))


def iter_syslog_like_events(path: Path) -> Iterable[LogEvent]:
    pending_no_ts: list[tuple[int, str]] = []
    last_ts: datetime | None = None
    current_year = datetime.now().year

    for line_no, text in enumerate(iter_text_lines(path), start=1):
        parsed = parse_syslog_line(text, current_year)
        if not parsed:
            pending_no_ts.append((line_no, text))
            continue

        ts, severity, summary = parsed
        ts = normalize_log_timestamp(ts, last_ts)
        if pending_no_ts:
            fill_ts = ts or last_ts
            for p_line_no, p_text in pending_no_ts:
                yield LogEvent(
                    timestamp=fill_ts,
                    timestamp_text=format_ts(fill_ts),
                    source_file=path.name,
                    line_no=p_line_no,
                    severity="INFO",
                    summary=p_text,
                    details=[p_text],
                    sequence=0,
                )
            pending_no_ts.clear()

        last_ts = ts or last_ts
        yield LogEvent(
            timestamp=ts,
            timestamp_text=format_ts(ts),
            source_file=path.name,
            line_no=line_no,
            severity=severity,
            summary=summary,
            details=[text],
            sequence=0,
        )

    if pending_no_ts:
        for p_line_no, p_text in pending_no_ts:
            yield LogEvent(
                timestamp=last_ts,
                timestamp_text=format_ts(last_ts),
                source_file=path.name,
                line_no=p_line_no,
                severity="INFO",
                summary=p_text,
                details=[p_text],
                sequence=0,
            )


def parse_syslog_line(line: str, year: int) -> tuple[datetime, str, str] | None:
    match = SYSLOG_RE.match(line)
    if not match:
        return None
    ts = datetime.strptime(
        f"{year} {match.group('mon')} {match.group('day')} {match.group('time')}",
        "%Y %b %d %H:%M:%S",
    )
    severity = match.group("facility").split(".", 1)[-1].upper()
    proc = match.group("proc").strip()
    body = match.group("body").strip()
    return ts, severity, f"{proc}: {body}"


def parse_netconf_file(path: Path, _: str) -> list[LogEvent]:
    return list(iter_netconf_events(path))


def iter_netconf_events(path: Path) -> Iterable[LogEvent]:
    current_year = datetime.now().year
    block_lines: list[str] = []
    block_ts: datetime | None = None
    block_line_no = 1
    block_session = ""
    block_dir = ""
    last_known_ts: datetime | None = None
    blocks: list[dict[str, object]] = []

    def flush_block() -> None:
        if not block_lines:
            return
        blocks.append(
            {
                "timestamp": block_ts,
                "line_no": block_line_no,
                "session": block_session or "?",
                "direction": block_dir,
                "lines": block_lines.copy(),
            }
        )

    for line_no, text in enumerate(iter_text_lines(path), start=1):
        header = NETCONF_HEADER_RE.match(text)
        marker = NETCONF_MESSAGE_MARKER_RE.search(text)
        line_ts: datetime | None = None

        parsed = parse_syslog_line(text, current_year)
        if parsed:
            line_ts = parsed[0]
            marker = marker or NETCONF_MESSAGE_MARKER_RE.search(parsed[2])
        if line_ts is None:
            line_ts = parse_loose_timestamp(text, current_year)
        line_ts = normalize_log_timestamp(line_ts, last_known_ts)

        if header or marker:
            flush_block()
            block_lines = [text]
            if line_ts is not None:
                block_ts = line_ts
            elif header is not None:
                block_ts = datetime.strptime(
                    f"{current_year} {header.group('mon')} {header.group('day')} {header.group('time')}",
                    "%Y %b %d %H:%M:%S",
                )
            else:
                # Accept NETCONF markers even when timestamp token is damaged.
                block_ts = last_known_ts
            if block_ts is not None:
                last_known_ts = block_ts
            block_line_no = line_no
            session = header.group("session") if header else None
            if session is None and marker is not None:
                session = marker.group("session")
            block_session = (session or "?").strip().rstrip(":")
            direction = header.group("dir") if header else None
            if direction is None and marker is not None:
                direction = marker.group("dir")
            block_dir = (direction or "").strip().lower()
        elif block_lines:
            block_lines.append(text)
    flush_block()

    pending_requests: dict[tuple[str, str], list[int]] = {}
    compare_states: list[str] = []
    levels: list[str] = []
    message_ids: list[str | None] = []
    operations: list[str] = []

    for idx, block in enumerate(blocks):
        lines = block["lines"]
        if not isinstance(lines, list):
            continue
        whole = "\n".join(lines)
        session = str(block.get("session", "?"))
        direction = str(block.get("direction", "")).lower()
        msg_id = extract_netconf_message_id(whole)
        is_reply = bool(re.search(r"<rpc-reply\b", whole, re.IGNORECASE))
        is_request = bool(re.search(r"<rpc(?:\s|>)", whole, re.IGNORECASE)) and not is_reply

        if is_reply:
            operation = "rpc-reply"
        elif is_request:
            operation = detect_netconf_operation(whole)
        else:
            operation = "unknown-op"

        compare_state = "n/a"
        level = "NETCONF"
        if is_request and direction == "received":
            if not msg_id:
                compare_state = "msg-id-missing"
                level = "WARN"
            else:
                compare_state = "reply-missing"
                level = "CRIT"
                pending_requests.setdefault((session, msg_id), []).append(idx)
        elif is_reply and direction == "sending":
            compare_state = "reply"
            level = "NETCONF"
            if msg_id:
                key = (session, msg_id)
                queue = pending_requests.get(key)
                if queue:
                    req_idx = queue.pop(0)
                    if 0 <= req_idx < len(compare_states):
                        compare_states[req_idx] = "ok"
                        levels[req_idx] = "NETCONF"
                    if not queue:
                        del pending_requests[key]

        compare_states.append(compare_state)
        levels.append(level)
        message_ids.append(msg_id)
        operations.append(operation)

    for idx, block in enumerate(blocks):
        lines = block["lines"]
        if not isinstance(lines, list):
            continue
        ts = block.get("timestamp")
        line_no = block.get("line_no")
        session = str(block.get("session", "?"))
        direction = str(block.get("direction", "")).lower()
        msg_id = message_ids[idx]
        operation = operations[idx]
        compare_state = compare_states[idx]
        level = levels[idx]
        summary = summarize_netconf_block(
            session_id=session,
            message_id=msg_id,
            operation=operation,
            compare_state=compare_state,
            line_count=len(lines),
            direction=direction,
        )
        yield LogEvent(
            timestamp=ts if isinstance(ts, datetime) else None,
            timestamp_text=format_ts(ts if isinstance(ts, datetime) else None),
            source_file=path.name,
            line_no=int(line_no) if isinstance(line_no, int) else 1,
            severity=level,
            summary=summary,
            details=lines,
            sequence=0,
        )


def summarize_netconf_block(
    session_id: str,
    message_id: str | None,
    operation: str,
    compare_state: str,
    line_count: int,
    direction: str,
) -> str:
    msg_id_text = message_id if message_id else "?"
    direction_text = direction if direction else "unknown-dir"
    return (
        f"Session {session_id} | {direction_text} | message-id {msg_id_text} | {operation} | "
        f"compare={compare_state} | {line_count} lines"
    )


def extract_netconf_message_id(text: str) -> str | None:
    match = MESSAGE_ID_RE.search(text)
    if match:
        return match.group(1)
    return None


def detect_netconf_operation(text: str) -> str:
    try:
        rpc_start = text.find("<rpc")
        rpc_end = text.find("</rpc>")
        if rpc_start == -1 or rpc_end == -1:
            return "unknown-op"
        rpc_xml = text[rpc_start : rpc_end + len("</rpc>")]
        root = ET.fromstring(rpc_xml)
        for child in root:
            tag = child.tag
            return tag.rsplit("}", 1)[-1]
    except ET.ParseError:
        return "unknown-op"
    return "unknown-op"


def parse_loose_timestamp(line: str, year: int) -> datetime | None:
    match = LOOSE_TS_RE.search(line)
    if not match:
        return None
    mon = match.group("mon").title()
    day = match.group("day")
    time_text = match.group("time")
    try:
        return datetime.strptime(f"{year} {mon} {day} {time_text}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None


def normalize_text_line(text: str) -> str:
    # Remove problematic control bytes to avoid mojibake artifacts in UI.
    cleaned = text.replace("\ufeff", "").replace("\x00", "")
    cleaned = CONTROL_CHAR_RE.sub("", cleaned)
    cleaned = reduce_mojibake_noise(cleaned)
    return cleaned.rstrip("\r\n")


def iter_text_lines(path: Path) -> Iterable[str]:
    # Fast path: most logs are valid UTF-8 and can be decoded in one pass.
    # Fallback to per-line decoding only for problematic files.
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
        for raw_line in text.splitlines():
            yield normalize_text_line(raw_line)
        return
    except UnicodeDecodeError:
        pass

    # Decode per-line to avoid "whole file mojibake" when a file contains mixed/broken bytes.
    with path.open("rb") as fh:
        for raw_line in fh:
            if is_probably_binary_line(raw_line):
                continue
            decoded: str | None = None
            for encoding in TEXT_ENCODINGS:
                try:
                    decoded = raw_line.decode(encoding, errors="strict")
                    break
                except UnicodeDecodeError:
                    continue
            if decoded is None:
                decoded = raw_line.decode("utf-8", errors="replace")
            if decoded.count("\ufffd") >= 8 and decoded.count("\ufffd") * 4 > len(decoded):
                continue
            yield normalize_text_line(decoded)


def reduce_mojibake_noise(text: str) -> str:
    if not text:
        return text

    ascii_printable = sum(1 for ch in text if 32 <= ord(ch) <= 126)
    non_ascii = sum(1 for ch in text if ord(ch) > 126)
    if non_ascii < 24:
        return text
    if non_ascii <= ascii_printable:
        return text

    # Keep readable ASCII/XML payload and drop heavy mojibake bursts.
    filtered = "".join(ch for ch in text if ch == "\t" or 32 <= ord(ch) <= 126)
    filtered = re.sub(r"\s{2,}", " ", filtered).strip()
    return filtered or text


def is_probably_binary_line(raw_line: bytes) -> bool:
    if not raw_line:
        return False
    if b"\x00" in raw_line:
        return True
    printable = sum(
        1
        for b in raw_line
        if b in {9, 10, 13} or 32 <= b <= 126
    )
    return printable / max(1, len(raw_line)) < 0.55


def normalize_log_timestamp(ts: datetime | None, prev_ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    now = datetime.now()
    adjusted = ts

    # If month/day has no year and appears in the future, map to previous year.
    if adjusted > now + timedelta(days=30):
        adjusted = adjusted.replace(year=adjusted.year - 1)

    # Handle year-rollover boundaries when processing long rotated logs.
    if prev_ts is not None:
        if adjusted - prev_ts > timedelta(days=200):
            adjusted = adjusted.replace(year=adjusted.year - 1)
        elif prev_ts - adjusted > timedelta(days=200):
            adjusted = adjusted.replace(year=adjusted.year + 1)
    return adjusted


def format_ts(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    return ts.strftime("%Y-%m-%d %H:%M:%S")
