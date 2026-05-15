from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class LogEvent:
    timestamp: datetime | None
    timestamp_text: str
    source_file: str
    line_no: int
    severity: str
    summary: str
    details: list[str]
    sequence: int
