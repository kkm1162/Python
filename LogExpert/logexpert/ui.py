from __future__ import annotations

import bisect
from collections import Counter
from datetime import datetime
from pathlib import Path
import re
from collections import defaultdict, deque

from PySide6.QtCore import QObject, QPoint, Qt, QThread, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QLineEdit,
    QMainWindow,
    QDialog,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .models import LogEvent
from .parsers import iter_events_for_file, sort_events


class LogLoadWorker(QObject):
    batch_ready = Signal(int, object)
    status = Signal(int, str)
    finished = Signal(int, object, bool, int, int)
    failed = Signal(int, str)
    warning = Signal(int, str)

    def __init__(
        self,
        load_id: int,
        kind: str,
        files: list[str],
        batch_size: int = 300,
        sequence_start: int = 0,
    ) -> None:
        super().__init__()
        self.load_id = load_id
        self.kind = kind
        self.files = files
        self.batch_size = batch_size
        self.sequence_start = sequence_start
        self._stop_requested = False

    @Slot()
    def request_stop(self) -> None:
        self._stop_requested = True

    @Slot()
    def run(self) -> None:
        try:
            all_events: list[LogEvent] = []
            batch: list[LogEvent] = []
            sequence = self.sequence_start
            file_count = len(self.files)
            skipped_files = 0
            processed_files = 0

            for file_idx, file_path in enumerate(self.files, start=1):
                if self._stop_requested:
                    break
                self.status.emit(self.load_id, f"Parsing {file_idx}/{file_count}: {file_path}")
                try:
                    for event in iter_events_for_file(self.kind, Path(file_path)):
                        if self._stop_requested:
                            break
                        event.sequence = sequence
                        sequence += 1
                        all_events.append(event)
                        batch.append(event)
                        if len(batch) >= self.batch_size:
                            self.batch_ready.emit(self.load_id, batch.copy())
                            batch.clear()
                except Exception as file_exc:  # noqa: BLE001
                    skipped_files += 1
                    processed_files += 1
                    self.warning.emit(self.load_id, f"Skipped file {file_path}: {file_exc}")
                    continue
                processed_files += 1
                self.status.emit(
                    self.load_id,
                    f"Parsed {file_idx}/{file_count} files, {len(all_events)} events",
                )

            if batch:
                self.batch_ready.emit(self.load_id, batch.copy())
            self.finished.emit(
                self.load_id,
                sort_events(all_events),
                self._stop_requested,
                skipped_files,
                processed_files,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.load_id, str(exc))


class SyncedTreeView(QTreeView):
    sync_requested = Signal(object)

    def __init__(self, pane: "LogPane") -> None:
        super().__init__()
        self.pane = pane

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            and event.button() == Qt.MouseButton.LeftButton
        ):
            idx = self.indexAt(event.pos())
            if idx.isValid() and idx.column() == 1:
                ts = self.pane.timestamp_for_index(idx)
                if ts is not None:
                    self.sync_requested.emit(ts)
        super().mousePressEvent(event)


class LogPane(QWidget):
    DEFAULT_CHUNK_SIZE = 10
    MAX_RENDER_ROWS = 50000
    MAX_LIVE_PREVIEW_ROWS = 12000
    DEFAULT_ALARM_KEYWORD_RULES = {
        "CRIT": [
            "kernel panic",
            "panic",
            "failed",
            "backtrace",
            "call trace",
            "stack trace",
            "segfault",
            "oops",
            "fatal exception",
            "general protection fault",
        ],
        "ERR": [],
        "WARN": [],
    }
    SUMMARY_SEVERITIES = {"CRIT", "ERR", "ERROR", "WARN"}
    LBM_RECV_RE = re.compile(r"\brecv\s+LBM\b.*\brx-cnt=(\d+)\b", re.IGNORECASE)
    LBR_SEND_RE = re.compile(r"\bsend\s+LBR\b.*\btx-cnt=(\d+)\b", re.IGNORECASE)
    NOISE_INFO_PATTERNS = (
        re.compile(r"\brecv\s+LBM\b.*\brx-cnt=\d+\b", re.IGNORECASE),
        re.compile(r"\bsend\s+LBR\b.*\btx-cnt=\d+\b", re.IGNORECASE),
        re.compile(r"supervision-notification", re.IGNORECASE),
        re.compile(r"o-ru controller .* from dhcp", re.IGNORECASE),
        re.compile(r"compare=ok", re.IGNORECASE),
    )
    BURST_WINDOW_SECONDS = 10
    BURST_THRESHOLD = 20
    NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z])[-+]?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][-+]?\d+)?")
    SYSLOG_ROTATION_RE = re.compile(
        r"^(?:syslog(?:\.log)?)(?:\.(\d+))?$",
        re.IGNORECASE,
    )
    EVENT_ROTATION_RE = re.compile(
        r"^(?P<base>(?:event|event[_-]?log|ims[-_]?msg)(?:\.log)?)(?:\.(?P<idx>\d+))?$",
        re.IGNORECASE,
    )
    NETCONF_ROTATION_RE = re.compile(
        r"^(?:netconf[-_.]?rpc(?:\.log)?|netopeer2?-server(?:\.log)?)(?:\.(\d+))?$",
        re.IGNORECASE,
    )

    @staticmethod
    def natural_file_sort_key(path_text: str) -> tuple[tuple[int, int | str], ...]:
        name = Path(path_text).name.lower()
        parts = re.split(r"(\d+)", name)
        key: list[tuple[int, int | str]] = []
        for part in parts:
            if not part:
                continue
            if part.isdigit():
                key.append((0, int(part)))
            else:
                key.append((1, part))
        return tuple(key)

    @classmethod
    def syslog_file_sort_key(cls, path_text: str) -> tuple[int, int, str]:
        name = Path(path_text).name.lower()
        match = cls.SYSLOG_ROTATION_RE.match(name)
        if match:
            rotation = -1 if match.group(1) is None else int(match.group(1))
            return (0, rotation, name)
        return (1, 10**9, name)

    @classmethod
    def event_file_sort_key(cls, path_text: str) -> tuple[int, int, str]:
        name = Path(path_text).name.lower()
        match = cls.EVENT_ROTATION_RE.match(name)
        if match:
            base = match.group("base").lower()
            rotation_text = match.group("idx")
            rotation = -1 if rotation_text is None else int(rotation_text)
            base_priority = {
                "event": 0,
                "event.log": 0,
                "event_log": 1,
                "event_log.log": 1,
                "event-log": 1,
                "event-log.log": 1,
                "ims-msg": 2,
                "ims-msg.log": 2,
                "ims_msg": 2,
                "ims_msg.log": 2,
            }.get(base, 9)
            return (0, rotation, f"{base_priority:02d}:{base}:{name}")
        return (1, 10**9, name)

    @classmethod
    def netconf_file_sort_key(cls, path_text: str) -> tuple[int, int, str]:
        name = Path(path_text).name.lower()
        match = cls.NETCONF_ROTATION_RE.match(name)
        if match:
            rotation = -1 if match.group(1) is None else int(match.group(1))
            return (0, rotation, name)
        return (1, 10**9, name)

    def sort_file_list(self, files: list[str]) -> list[str]:
        if self.kind == "syslog":
            return sorted(files, key=self.syslog_file_sort_key)
        if self.kind == "event":
            return sorted(files, key=self.event_file_sort_key)
        if self.kind == "netconf":
            return sorted(files, key=self.netconf_file_sort_key)
        return sorted(files, key=self.natural_file_sort_key)

    def __init__(self, kind: str, title: str) -> None:
        super().__init__()
        self.kind = kind
        self.events: list[LogEvent] = []
        self.display_events: list[LogEvent] = []
        self._timestamp_values: list[datetime] = []
        self._first_row_by_timestamp: dict[datetime, int] = {}
        self.sort_descending = True
        self._loaded_files_count = 0
        self._total_matched_files = 0
        self._current_chunk_start = 0
        self._current_chunk_end = 0
        self.include_terms: list[str] = []
        self.exclude_terms: list[str] = []
        self.matched_files: list[str] = []
        self.alarm_keyword_rules: dict[str, list[str]] = {
            sev: list(values)
            for sev, values in self.DEFAULT_ALARM_KEYWORD_RULES.items()
        }
        self._lbm_unmatched_ids: set[int] = set()
        self._burst_promoted_ids: set[int] = set()
        self._classification_cache: dict[int, tuple[str, str]] = {}
        self._filter_text_cache: dict[int, str] = {}

        self.loader_thread: QThread | None = None
        self.loader_worker: LogLoadWorker | None = None
        self.active_load_id = 0
        self._active_skipped_files = 0

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: bold;")
        self.load_button = QPushButton("Load Files")
        self.chunk_size_spin = QSpinBox()
        self.chunk_size_spin.setRange(1, 200)
        self.chunk_size_spin.setValue(self.DEFAULT_CHUNK_SIZE)
        self.chunk_size_spin.setPrefix("Files: ")
        self.prev_button = QPushButton("Previous")
        self.prev_button.setEnabled(False)
        self.next_button = QPushButton("Next")
        self.next_button.setEnabled(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.summary_button = QPushButton("Summary")
        self.settings_button = QPushButton("Alarm")
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Text contains...")
        self.include_button = QPushButton("Only")
        self.exclude_button = QPushButton("Exclude")
        self.clear_filter_button = QPushButton("Clear Filters")
        self.info_label = QLabel("No files loaded")
        self.info_label.setStyleSheet("color: #777;")
        self.filter_label = QLabel("Filters: none")
        self.filter_label.setStyleSheet("color: #777;")
        self.severity_label = QLabel("Severity: -")
        self.severity_label.setStyleSheet("color: #777;")

        self.tree = SyncedTreeView(self)
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Time", "Summary"])
        self.tree.setModel(self.model)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(False)
        self.tree.setMouseTracking(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.header().setStretchLastSection(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        header_row = QHBoxLayout()
        header_row.addWidget(self.title_label)
        header_row.addStretch(1)
        header_row.addWidget(self.settings_button)
        header_row.addWidget(self.summary_button)

        control_row = QHBoxLayout()
        control_row.addWidget(self.chunk_size_spin)
        control_row.addWidget(self.prev_button)
        control_row.addWidget(self.next_button)
        control_row.addWidget(self.cancel_button)
        control_row.addStretch(1)
        control_row.addWidget(self.load_button)

        filter_row = QHBoxLayout()
        filter_row.addWidget(self.filter_input, 1)
        filter_row.addWidget(self.include_button)
        filter_row.addWidget(self.exclude_button)
        filter_row.addWidget(self.clear_filter_button)

        layout = QVBoxLayout()
        layout.addLayout(header_row)
        layout.addLayout(control_row)
        layout.addLayout(filter_row)
        layout.addWidget(self.filter_label)
        layout.addWidget(self.severity_label)
        layout.addWidget(self.info_label)
        layout.addWidget(self.tree, 1)
        self.setLayout(layout)

        self.load_button.clicked.connect(self.load_files)
        self.prev_button.clicked.connect(self.load_previous_chunk)
        self.next_button.clicked.connect(self.load_next_chunk)
        self.cancel_button.clicked.connect(self.cancel_loading)
        self.settings_button.clicked.connect(self.show_alarm_settings_dialog)
        self.summary_button.clicked.connect(self.show_severity_summary)
        self.include_button.clicked.connect(self.add_include_filter)
        self.exclude_button.clicked.connect(self.add_exclude_filter)
        self.clear_filter_button.clicked.connect(self.clear_filters)
        self.filter_input.returnPressed.connect(self.add_include_filter)
        self.tree.customContextMenuRequested.connect(self.open_context_menu)
        self.tree.expanded.connect(self.on_tree_expanded)

    def load_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            f"Select {self.kind} files",
            "",
            "Log files (*.log* *.txt *syslog*);;All files (*.*)",
        )
        if not files:
            return
        self.matched_files = self.sort_file_list(files)
        self._total_matched_files = len(self.matched_files)
        self._current_chunk_start = 0
        self._current_chunk_end = 0
        self.load_current_chunk()

    def start_loading(self, files: list[str], sequence_start: int = 0) -> None:
        if self.loader_thread is not None and self.loader_thread.isRunning():
            self.info_label.setText("Already loading...")
            return
        self.active_load_id += 1
        self._active_skipped_files = 0
        self._loaded_files_count = 0
        self.events = []
        self.display_events = []
        self._filter_text_cache = {}
        self.model.removeRows(0, self.model.rowCount())
        self.info_label.setText(
            f"Loading... chunk {self._current_chunk_start + 1}-{self._current_chunk_end}/{self._total_matched_files}"
        )
        self.load_button.setEnabled(False)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.cancel_button.setEnabled(True)

        self.loader_thread = QThread(self)
        self.loader_worker = LogLoadWorker(
            self.active_load_id,
            self.kind,
            files,
            batch_size=320,
            sequence_start=sequence_start,
        )
        self.loader_worker.moveToThread(self.loader_thread)

        self.loader_thread.started.connect(self.loader_worker.run)
        self.loader_worker.batch_ready.connect(self.on_batch_ready)
        self.loader_worker.status.connect(self.on_status)
        self.loader_worker.warning.connect(self.on_warning)
        self.loader_worker.finished.connect(self.on_load_finished)
        self.loader_worker.failed.connect(self.on_load_failed)
        self.loader_worker.finished.connect(self.loader_thread.quit)
        self.loader_worker.failed.connect(self.loader_thread.quit)
        self.loader_thread.finished.connect(self.cleanup_loader)
        self.loader_thread.start()

    def load_from_folder(self, folder_path: str) -> None:
        folder = Path(folder_path)
        if not folder.exists():
            self.info_label.setText("Folder not found")
            return
        matched: list[str] = []
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if self.matches_kind(path):
                matched.append(str(path))
        matched = self.sort_file_list(matched)
        if not matched:
            self.events = []
            self.display_events = []
            self.model.removeRows(0, self.model.rowCount())
            self._loaded_files_count = 0
            self._total_matched_files = 0
            self.matched_files = []
            self._current_chunk_start = 0
            self._current_chunk_end = 0
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.info_label.setText("No matching files in folder")
            return
        self.matched_files = matched
        self._total_matched_files = len(self.matched_files)
        self._current_chunk_start = 0
        self._current_chunk_end = 0
        self.load_current_chunk()

    def load_current_chunk(self) -> None:
        if not self.matched_files:
            self.info_label.setText("No files to load")
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        start = self._current_chunk_start
        chunk_size = max(1, int(self.chunk_size_spin.value()))
        end = min(start + chunk_size, self._total_matched_files)
        if start >= end:
            self.info_label.setText("No more chunks")
            self.prev_button.setEnabled(start > 0)
            self.next_button.setEnabled(False)
            return
        self._current_chunk_end = end
        chunk_files = self.matched_files[start:end]
        self.prev_button.setEnabled(start > 0)
        self.next_button.setEnabled(end < self._total_matched_files)
        self.start_loading(chunk_files, sequence_start=0)

    def load_next_chunk(self) -> None:
        if self.loader_thread is not None and self.loader_thread.isRunning():
            return
        chunk_size = max(1, int(self.chunk_size_spin.value()))
        next_start = self._current_chunk_start + chunk_size
        if next_start >= self._total_matched_files:
            self.prev_button.setEnabled(self._current_chunk_start > 0)
            self.next_button.setEnabled(False)
            self.info_label.setText(
                f"Chunk {self._current_chunk_start + 1}-{self._current_chunk_end}/{self._total_matched_files} loaded (last chunk)"
            )
            return
        self._current_chunk_start = next_start
        self.load_current_chunk()

    def load_previous_chunk(self) -> None:
        if self.loader_thread is not None and self.loader_thread.isRunning():
            return
        if self._current_chunk_start <= 0:
            self.prev_button.setEnabled(False)
            return
        chunk_size = max(1, int(self.chunk_size_spin.value()))
        prev_start = max(0, self._current_chunk_start - chunk_size)
        self._current_chunk_start = prev_start
        self.load_current_chunk()

    def matches_kind(self, path: Path) -> bool:
        name = path.name.lower()
        if self.kind == "syslog":
            return bool(self.SYSLOG_ROTATION_RE.match(name))
        if self.kind == "event":
            return bool(self.EVENT_ROTATION_RE.match(name))
        if self.kind == "netconf":
            return bool(self.NETCONF_ROTATION_RE.match(name))
        return False

    @Slot()
    def cleanup_loader(self) -> None:
        if self.loader_worker is not None:
            self.loader_worker.deleteLater()
        if self.loader_thread is not None:
            self.loader_thread.deleteLater()
        self.loader_worker = None
        self.loader_thread = None
        self.load_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.prev_button.setEnabled(self._current_chunk_start > 0)
        self.next_button.setEnabled(self._current_chunk_end < self._total_matched_files)

    def cancel_loading(self) -> None:
        if self.loader_worker is None:
            return
        self.loader_worker.request_stop()
        self.info_label.setText("Cancelling load...")

    @Slot(int, object)
    def on_batch_ready(self, load_id: int, batch: list[LogEvent]) -> None:
        if load_id != self.active_load_id:
            return
        self.events.extend(batch)
        self.append_live_batch(batch)
        self.info_label.setText(
            f"Loading... chunk {self._current_chunk_start + 1}-{self._current_chunk_end}/{self._total_matched_files} | {len(self.events)} events (skipped {self._active_skipped_files})"
        )

    @Slot(int, str)
    def on_status(self, load_id: int, text: str) -> None:
        if load_id != self.active_load_id:
            return
        self.info_label.setText(text)

    @Slot(int, str)
    def on_warning(self, load_id: int, text: str) -> None:
        if load_id != self.active_load_id:
            return
        self._active_skipped_files += 1
        self.info_label.setText(text)

    @Slot(int, object, bool, int, int)
    def on_load_finished(
        self,
        load_id: int,
        ordered_events: list[LogEvent],
        was_cancelled: bool,
        skipped_files: int,
        processed_files: int,
    ) -> None:
        if load_id != self.active_load_id:
            return
        self.events = ordered_events
        self._filter_text_cache = {}

        self._loaded_files_count = max(0, processed_files - skipped_files)
        self.render_events()
        if was_cancelled:
            self.info_label.setText(
                f"Load cancelled. chunk {self._current_chunk_start + 1}-{self._current_chunk_end}/{self._total_matched_files}, {len(self.events)} events kept (skipped {skipped_files})"
            )
            return
        # Keep render_events() summary text (includes visible/rendered row count).

    @Slot(int, str)
    def on_load_failed(self, load_id: int, error: str) -> None:
        if load_id != self.active_load_id:
            return
        QMessageBox.critical(self, "Load failed", error)
        self.info_label.setText("Load failed")

    def add_exclude_filter(self) -> None:
        term = self.filter_input.text().strip()
        if not term:
            return
        term_lower = term.lower()
        if term_lower not in self.exclude_terms:
            self.exclude_terms.append(term_lower)
        self.filter_input.clear()
        self.update_filter_label()
        self.render_events()

    def add_include_filter(self) -> None:
        term = self.filter_input.text().strip()
        if not term:
            return
        term_lower = term.lower()
        if term_lower not in self.include_terms:
            self.include_terms.append(term_lower)
        self.filter_input.clear()
        self.update_filter_label()
        self.render_events()

    def clear_filters(self) -> None:
        self.include_terms = []
        self.exclude_terms = []
        self.update_filter_label()
        self.render_events()

    def update_filter_label(self) -> None:
        if not self.include_terms and not self.exclude_terms:
            self.filter_label.setText("Filters: none")
            return
        include_text = (
            f"Only[{', '.join(self.include_terms)}]" if self.include_terms else "Only[-]"
        )
        exclude_text = (
            f"Exclude[{', '.join(self.exclude_terms)}]" if self.exclude_terms else "Exclude[-]"
        )
        self.filter_label.setText(f"Filters: {include_text} | {exclude_text}")

    def event_matches_filters(self, event: LogEvent) -> bool:
        evt_id = id(event)
        text = self._filter_text_cache.get(evt_id)
        if text is None:
            detail_preview = " ".join(event.details[:8]).lower()
            text = f"{event.summary} {detail_preview}".lower()
            self._filter_text_cache[evt_id] = text

        if self.include_terms and not any(term in text for term in self.include_terms):
            return False
        if self.exclude_terms and any(term in text for term in self.exclude_terms):
            return False
        return True

    def create_row_items(
        self,
        event: LogEvent,
        sev: str,
        reason: str,
        summary_text: str | None = None,
    ) -> list[QStandardItem]:
        source_text = f"{event.source_file}:{event.line_no}"
        ts_value = event.timestamp.isoformat() if event.timestamp else ""
        color_brush = self.brush_for_severity(sev)
        row_items = [
            QStandardItem(event.timestamp_text),
            QStandardItem(summary_text if summary_text is not None else event.summary),
        ]
        for item in row_items:
            item.setEditable(False)
            item.setToolTip(f"{source_text}\nseverity={sev}\nreason={reason}")
            item.setData(ts_value, Qt.ItemDataRole.UserRole)
            if color_brush is not None:
                item.setForeground(color_brush)
        return row_items

    def append_live_batch(self, batch: list[LogEvent]) -> None:
        # Keep the UI responsive while parsing by showing a live preview.
        # Final time-sorted rendering happens when loading completes.
        if self.model.rowCount() >= self.MAX_LIVE_PREVIEW_ROWS:
            return
        visible_batch = [event for event in batch if self.event_matches_filters(event)]
        if not visible_batch:
            return
        self.tree.setUpdatesEnabled(False)
        try:
            for event in visible_batch:
                if self.model.rowCount() >= self.MAX_LIVE_PREVIEW_ROWS:
                    break
                sev, reason = self.classify_event(event)
                self.model.appendRow(self.create_row_items(event, sev, reason))
        finally:
            self.tree.setUpdatesEnabled(True)

    def render_events(self) -> None:
        self.tree.setUpdatesEnabled(False)
        try:
            self.model.removeRows(0, self.model.rowCount())
            self._timestamp_values = []
            self._first_row_by_timestamp = {}
            self.rebuild_correlation_flags()
            self.rebuild_classification_cache()
            ordered_events = (
                list(reversed(self.events)) if self.sort_descending else list(self.events)
            )
            filtered_events = [
                event for event in ordered_events if self.event_matches_filters(event)
            ]
            total_filtered = len(filtered_events)
            if total_filtered > self.MAX_RENDER_ROWS:
                if self.sort_descending:
                    rendered_events = filtered_events[: self.MAX_RENDER_ROWS]
                else:
                    rendered_events = filtered_events[-self.MAX_RENDER_ROWS :]
            else:
                rendered_events = filtered_events
            self.display_events = rendered_events

            for row, event in enumerate(self.display_events):
                sev, reason = self.classify_event(event)
                self.model.appendRow(self.create_row_items(event, sev, reason))

                if event.timestamp is not None:
                    self._first_row_by_timestamp.setdefault(event.timestamp, row)

                if self.kind == "netconf":
                    parent = self.model.item(row, 0)
                    if parent is not None:
                        placeholder_items = self.create_row_items(
                            event,
                            sev,
                            reason,
                            summary_text="[expand to load details]",
                        )
                        placeholder_items[0].setText("")
                        parent.appendRow(placeholder_items)
                        parent.setData(False, Qt.ItemDataRole.UserRole + 1)
            if self.kind == "netconf":
                self.tree.collapseAll()
            self._timestamp_values = sorted(self._first_row_by_timestamp.keys())
        finally:
            self.tree.setUpdatesEnabled(True)

        rendered = len(self.display_events)
        total = len(self.events)
        if rendered < total:
            self.info_label.setText(
                f"Chunk {self._current_chunk_start + 1}-{self._current_chunk_end}/{self._total_matched_files}, showing {rendered} rows (of {total} total)"
            )
        else:
            self.info_label.setText(
                f"Chunk {self._current_chunk_start + 1}-{self._current_chunk_end}/{self._total_matched_files}, {rendered} visible / {total} total"
            )
        self.update_severity_label()

    def anchor_timestamp(self) -> datetime | None:
        top_idx = self.tree.indexAt(QPoint(2, 2))
        if not top_idx.isValid():
            top_idx = self.tree.currentIndex()
        if not top_idx.isValid():
            return None
        row = top_idx.row()
        if row < 0 or row >= len(self.display_events):
            return None
        ts = self.display_events[row].timestamp
        if ts is not None:
            return ts
        # When current row has no timestamp, walk to nearest row with timestamp.
        for offset in range(1, len(self.display_events)):
            left = row - offset
            if left >= 0 and self.display_events[left].timestamp is not None:
                return self.display_events[left].timestamp
            right = row + offset
            if right < len(self.display_events) and self.display_events[right].timestamp is not None:
                return self.display_events[right].timestamp
        return None

    def timestamp_for_index(self, idx) -> datetime | None:
        ts_text = idx.data(Qt.ItemDataRole.UserRole)
        if ts_text:
            try:
                return datetime.fromisoformat(ts_text)
            except ValueError:
                return None
        return None

    def open_context_menu(self, pos: QPoint) -> None:
        idx = self.tree.indexAt(pos)
        if not idx.isValid():
            return
        summary_idx = idx.sibling(idx.row(), 1)
        summary_text = summary_idx.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(summary_text, str) or not summary_text.strip():
            return

        menu = QMenu(self)
        action_include = menu.addAction("Show only this message")
        action_exclude = menu.addAction("Exclude this message")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen not in {action_include, action_exclude}:
            return
        term = summary_text.strip()
        if term:
            term_lower = term.lower()
            if chosen == action_include:
                if term_lower not in self.include_terms:
                    self.include_terms.append(term_lower)
            else:
                if term_lower not in self.exclude_terms:
                    self.exclude_terms.append(term_lower)
            self.update_filter_label()
            self.render_events()

    @Slot(object)
    def on_tree_expanded(self, idx) -> None:
        if self.kind != "netconf":
            return
        if idx.parent().isValid():
            return
        row = idx.row()
        if row < 0 or row >= len(self.display_events):
            return
        parent = self.model.item(row, 0)
        if parent is None:
            return
        if parent.data(Qt.ItemDataRole.UserRole + 1) is True:
            return
        event = self.display_events[row]
        source_text = f"{event.source_file}:{event.line_no}"
        ts_value = event.timestamp.isoformat() if event.timestamp else ""
        sev, reason = self.classify_event(event)
        color_brush = self.brush_for_severity(sev)
        parent.removeRows(0, parent.rowCount())
        for detail_line in event.details:
            detail_items = [
                QStandardItem(""),
                QStandardItem(detail_line),
            ]
            for item in detail_items:
                item.setEditable(False)
                item.setToolTip(f"{source_text}\nseverity={sev}\nreason={reason}")
                item.setData(ts_value, Qt.ItemDataRole.UserRole)
                if color_brush is not None:
                    item.setForeground(color_brush)
            parent.appendRow(detail_items)
        parent.setData(True, Qt.ItemDataRole.UserRole + 1)

    def brush_for_severity(self, sev: str) -> QBrush | None:
        if sev in {"ERR", "ERROR", "CRIT", "ALERT", "EMERG", "FATAL"}:
            return QBrush(QColor("#c0392b"))
        if sev in {"WARN", "WARNING", "WRN"}:
            return QBrush(QColor("#d68910"))
        if sev in {"NOTICE", "NETCONF"}:
            return QBrush(QColor("#1f618d"))
        return None

    def classify_event(self, event: LogEvent) -> tuple[str, str]:
        cached = self._classification_cache.get(id(event))
        if cached is not None:
            return cached
        result = self.compute_base_classification(event)
        self._classification_cache[id(event)] = result
        return result

    def compute_base_classification(self, event: LogEvent) -> tuple[str, str]:
        sev = event.severity.upper()
        text = f"{event.summary} {' '.join(event.details[:20])}".lower()
        if id(event) in self._lbm_unmatched_ids:
            return "CRIT", "lbm-lbr-cnt-mismatch"
        if "compare=reply-missing" in event.summary.lower():
            return "CRIT", "netconf-reply-missing"
        custom_rule = self.match_custom_alarm_rule(text)
        if custom_rule is not None:
            return custom_rule
        for pattern in self.NOISE_INFO_PATTERNS:
            if pattern.search(event.summary):
                return "INFO", "whitelist-periodic-noise"
        if "error" in text and ("kernel" in text or "fatal" in text):
            return "CRIT", "kernel-or-fatal-error-context"
        if "compare=msg-id-missing" in event.summary.lower():
            return "WARN", "netconf-msg-id-missing"
        if sev in {"CRIT", "ALERT", "EMERG", "FATAL"}:
            return "CRIT", "source-severity"
        if sev in {"ERR", "ERROR"}:
            return "ERR", "source-severity"
        if sev in {"WRN", "WARNING"}:
            return "WARN", "source-severity"
        if "error" in text:
            return "ERR", "error-keyword"
        return sev, "default"

    def rebuild_classification_cache(self) -> None:
        self._classification_cache = {}
        self._burst_promoted_ids = set()

        base_by_id: dict[int, tuple[str, str]] = {}
        for event in self.events:
            sev, reason = self.compute_base_classification(event)
            base_by_id[id(event)] = (sev, reason)

        # Burst escalation: repeated WARN/ERR patterns in a short window -> CRIT.
        windows: dict[tuple[str, str], deque[tuple[datetime, int]]] = defaultdict(deque)
        for event in self.events:
            base = base_by_id[id(event)]
            base_sev = base[0]
            if base_sev not in {"WARN", "ERR"}:
                continue
            if event.timestamp is None:
                continue
            key = (base_sev, self.normalize_summary_for_burst(event.summary))
            dq = windows[key]
            while dq and (event.timestamp - dq[0][0]).total_seconds() > self.BURST_WINDOW_SECONDS:
                dq.popleft()
            dq.append((event.timestamp, id(event)))
            if len(dq) >= self.BURST_THRESHOLD:
                for _, evt_id in dq:
                    self._burst_promoted_ids.add(evt_id)

        for event in self.events:
            evt_id = id(event)
            sev, reason = base_by_id[evt_id]
            if evt_id in self._burst_promoted_ids and sev in {"WARN", "ERR"}:
                self._classification_cache[evt_id] = (
                    "CRIT",
                    f"burst-escalation({sev}->CRIT)",
                )
            else:
                self._classification_cache[evt_id] = (sev, reason)

    def normalize_summary_for_burst(self, summary: str) -> str:
        normalized = summary.lower()
        normalized = re.sub(r"\b\d+\b", "#", normalized)
        normalized = re.sub(r"0x[0-9a-f]+", "0x#", normalized)
        return normalized

    def match_custom_alarm_rule(self, text: str) -> tuple[str, str] | None:
        for severity in ("CRIT", "ERR", "WARN"):
            for keyword in self.alarm_keyword_rules.get(severity, []):
                if keyword and keyword in text:
                    return severity, f"custom-keyword:{keyword}"
        return None

    def rebuild_correlation_flags(self) -> None:
        self._lbm_unmatched_ids = set()
        if self.kind != "syslog":
            return

        recv_by_cnt: dict[int, list[LogEvent]] = {}
        send_by_cnt: dict[int, list[LogEvent]] = {}
        for event in self.events:
            recv_match = self.LBM_RECV_RE.search(event.summary)
            if recv_match:
                cnt = int(recv_match.group(1))
                recv_by_cnt.setdefault(cnt, []).append(event)
                continue
            send_match = self.LBR_SEND_RE.search(event.summary)
            if send_match:
                cnt = int(send_match.group(1))
                send_by_cnt.setdefault(cnt, []).append(event)

        all_cnts = set(recv_by_cnt) | set(send_by_cnt)
        for cnt in all_cnts:
            recv_events = recv_by_cnt.get(cnt, [])
            send_events = send_by_cnt.get(cnt, [])
            pair_count = min(len(recv_events), len(send_events))
            if len(recv_events) > pair_count:
                for event in recv_events[pair_count:]:
                    self._lbm_unmatched_ids.add(id(event))
            if len(send_events) > pair_count:
                for event in send_events[pair_count:]:
                    self._lbm_unmatched_ids.add(id(event))

    def update_severity_label(self) -> None:
        if not self.display_events:
            self.severity_label.setText("Severity: -")
            return
        counts: dict[str, int] = {}
        for event in self.display_events:
            sev, _ = self.classify_event(event)
            if sev not in self.SUMMARY_SEVERITIES:
                continue
            if sev == "ERROR":
                sev = "ERR"
            counts[sev] = counts.get(sev, 0) + 1
        if not counts:
            self.severity_label.setText("Severity(visible): CRIT:0 | ERR:0 | WARN:0")
            return
        priority = ["CRIT", "ERR", "WARN"]
        ordered_keys = [k for k in priority if k in counts]
        ordered_keys.extend(sorted(k for k in counts if k not in priority))
        parts = [f"{k}:{counts[k]}" for k in ordered_keys]
        self.severity_label.setText(f"Severity(visible): {' | '.join(parts)}")

    def show_severity_summary(self) -> None:
        counts: Counter[tuple[str, str]] = Counter()
        reasons_by_group: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        display_by_group: dict[tuple[str, str], str] = {}
        first_seen_by_group: dict[tuple[str, str], datetime | None] = {}
        last_seen_by_group: dict[tuple[str, str], datetime | None] = {}
        for event in self.display_events:
            sev, reason = self.classify_event(event)
            if sev not in self.SUMMARY_SEVERITIES:
                continue
            if sev == "ERROR":
                sev = "ERR"
            normalized_summary = self.normalize_summary_for_display_grouping(event.summary, sev)
            # Group duplicates by normalized severity + normalized summary text.
            group = (sev, normalized_summary)
            counts[group] += 1
            reasons_by_group[group][reason] += 1
            display_by_group.setdefault(group, normalized_summary)
            event_ts = event.timestamp
            first_ts = first_seen_by_group.get(group)
            last_ts = last_seen_by_group.get(group)
            if event_ts is not None:
                if first_ts is None or event_ts < first_ts:
                    first_seen_by_group[group] = event_ts
                if last_ts is None or event_ts > last_ts:
                    last_seen_by_group[group] = event_ts
            else:
                first_seen_by_group.setdefault(group, None)
                last_seen_by_group.setdefault(group, None)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"{self.kind.upper()} Severity Summary")
        dialog.resize(1160, 700)

        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(
            ["Severity", "Count", "Reason", "First Seen", "Last Seen", "Summary"]
        )
        tree = QTreeView(dialog)
        tree.setModel(model)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(True)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.header().setStretchLastSection(True)
        tree.header().resizeSection(0, 120)
        tree.header().resizeSection(1, 90)
        tree.header().resizeSection(2, 240)
        tree.header().resizeSection(3, 170)
        tree.header().resizeSection(4, 170)

        sort_combo = QComboBox(dialog)
        sort_combo.addItems(
            [
                "Count (desc)",
                "First seen (newest)",
                "First seen (oldest)",
                "Last seen (newest)",
                "Last seen (oldest)",
            ]
        )
        sort_combo.setCurrentText("Last seen (newest)")
        unit_combo = QComboBox(dialog)
        unit_combo.addItems(["second", "minute", "hour", "day"])
        unit_combo.setCurrentText("minute")

        def format_dt(ts: datetime | None) -> str:
            if ts is None:
                return "-"
            return ts.strftime("%Y-%m-%d %H:%M:%S")

        def build_sorted_groups() -> list[tuple[tuple[str, str], int]]:
            groups = list(counts.items())
            sort_mode = sort_combo.currentText()
            time_unit = unit_combo.currentText()

            def bucket_or_min(ts: datetime | None) -> datetime:
                if ts is None:
                    return datetime.min
                return self.bucket_timestamp(ts, time_unit)

            if sort_mode == "Count (desc)":
                groups.sort(
                    key=lambda item: (
                        -item[1],
                        bucket_or_min(first_seen_by_group.get(item[0])),
                        item[0][0],
                        item[0][1],
                    )
                )
            elif sort_mode == "First seen (newest)":
                groups.sort(
                    key=lambda item: (
                        bucket_or_min(first_seen_by_group.get(item[0])),
                        item[1],
                        item[0][0],
                        item[0][1],
                    ),
                    reverse=True,
                )
            elif sort_mode == "First seen (oldest)":
                groups.sort(
                    key=lambda item: (
                        bucket_or_min(first_seen_by_group.get(item[0])),
                        -item[1],
                        item[0][0],
                        item[0][1],
                    )
                )
            elif sort_mode == "Last seen (newest)":
                groups.sort(
                    key=lambda item: (
                        bucket_or_min(last_seen_by_group.get(item[0])),
                        item[1],
                        item[0][0],
                        item[0][1],
                    ),
                    reverse=True,
                )
            else:  # Last seen (oldest)
                groups.sort(
                    key=lambda item: (
                        bucket_or_min(last_seen_by_group.get(item[0])),
                        -item[1],
                        item[0][0],
                        item[0][1],
                    )
                )
            return groups

        def refresh_summary_rows() -> None:
            model.removeRows(0, model.rowCount())
            time_unit = unit_combo.currentText()
            for (sev, group_key), count in build_sorted_groups():
                summary = display_by_group.get((sev, group_key), group_key)
                reason_counter = reasons_by_group[(sev, group_key)]
                reason = reason_counter.most_common(1)[0][0] if reason_counter else "default"
                first_seen = self.bucket_timestamp(first_seen_by_group.get((sev, group_key)), time_unit)
                last_seen = self.bucket_timestamp(last_seen_by_group.get((sev, group_key)), time_unit)
                row = [
                    QStandardItem(sev),
                    QStandardItem(str(count)),
                    QStandardItem(reason),
                    QStandardItem(format_dt(first_seen)),
                    QStandardItem(format_dt(last_seen)),
                    QStandardItem(summary),
                ]
                color = self.brush_for_severity(sev)
                if color is not None:
                    for cell in row:
                        cell.setForeground(color)
                for cell in row:
                    cell.setEditable(False)
                row[0].setData(sev, Qt.ItemDataRole.UserRole)
                row[2].setData(reason, Qt.ItemDataRole.UserRole)
                row[5].setData(group_key, Qt.ItemDataRole.UserRole)
                model.appendRow(row)

        sort_combo.currentTextChanged.connect(lambda _text: refresh_summary_rows())
        unit_combo.currentTextChanged.connect(lambda _text: refresh_summary_rows())
        refresh_summary_rows()

        def open_summary_context_menu(pos: QPoint) -> None:
            idx = tree.indexAt(pos)
            if not idx.isValid():
                return
            row = idx.row()
            sev_idx = model.index(row, 0)
            reason_idx = model.index(row, 2)
            summary_idx = model.index(row, 5)
            sev_value = sev_idx.data(Qt.ItemDataRole.UserRole)
            reason_value = reason_idx.data(Qt.ItemDataRole.UserRole)
            summary_key = summary_idx.data(Qt.ItemDataRole.UserRole)
            if not isinstance(sev_value, str) or not isinstance(summary_key, str):
                return

            menu = QMenu(dialog)
            find_action = menu.addAction("Find in log")
            chosen = menu.exec(tree.viewport().mapToGlobal(pos))
            if chosen != find_action:
                return
            if self.find_and_focus_message(sev_value, summary_key):
                dialog.accept()
            else:
                QMessageBox.information(self, "Not found", "No matching message in current view.")

        tree.customContextMenuRequested.connect(open_summary_context_menu)

        top = QLabel(f"Unique messages: {len(counts)} | Visible events: {len(self.display_events)}")
        top.setStyleSheet("color: #555;")
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Sort"))
        controls.addWidget(sort_combo)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Time Unit"))
        controls.addWidget(unit_combo)
        controls.addStretch(1)
        layout = QVBoxLayout()
        layout.addWidget(top)
        layout.addLayout(controls)
        layout.addWidget(tree, 1)
        dialog.setLayout(layout)
        dialog.exec()

    def bucket_timestamp(self, ts: datetime | None, unit: str) -> datetime | None:
        if ts is None:
            return None
        if unit == "day":
            return ts.replace(hour=0, minute=0, second=0, microsecond=0)
        if unit == "hour":
            return ts.replace(minute=0, second=0, microsecond=0)
        if unit == "minute":
            return ts.replace(second=0, microsecond=0)
        return ts.replace(microsecond=0)

    def show_alarm_settings_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{self.kind.upper()} Alarm Settings")
        dialog.resize(840, 520)

        severity_order = ["CRIT", "ERR", "WARN"]
        working_rules: dict[str, list[str]] = {
            sev: list(self.alarm_keyword_rules.get(sev, []))
            for sev in severity_order
        }
        list_widgets: dict[str, QListWidget] = {}

        list_row = QHBoxLayout()
        for sev in severity_order:
            col = QVBoxLayout()
            col.addWidget(QLabel(sev))
            lst = QListWidget()
            lst.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
            list_widgets[sev] = lst
            col.addWidget(lst, 1)
            list_row.addLayout(col, 1)

        def refresh_lists() -> None:
            for sev in severity_order:
                lst = list_widgets[sev]
                lst.clear()
                for keyword in sorted(set(working_rules[sev])):
                    lst.addItem(keyword)

        controls = QHBoxLayout()
        sev_combo = QComboBox()
        sev_combo.addItems(severity_order)
        keyword_input = QLineEdit()
        keyword_input.setPlaceholderText("Keyword")
        add_button = QPushButton("Add")
        remove_button = QPushButton("Remove Selected")
        controls.addWidget(QLabel("Severity"))
        controls.addWidget(sev_combo)
        controls.addWidget(keyword_input, 1)
        controls.addWidget(add_button)
        controls.addWidget(remove_button)

        def add_keyword() -> None:
            keyword = keyword_input.text().strip().lower()
            if not keyword:
                return
            sev = sev_combo.currentText()
            if keyword not in working_rules[sev]:
                working_rules[sev].append(keyword)
            keyword_input.clear()
            refresh_lists()

        def remove_selected() -> None:
            changed = False
            for sev in severity_order:
                lst = list_widgets[sev]
                selected = [item.text() for item in lst.selectedItems()]
                if not selected:
                    continue
                for keyword in selected:
                    if keyword in working_rules[sev]:
                        working_rules[sev].remove(keyword)
                        changed = True
            if changed:
                refresh_lists()

        add_button.clicked.connect(add_keyword)
        remove_button.clicked.connect(remove_selected)
        keyword_input.returnPressed.connect(add_keyword)
        refresh_lists()

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        apply_btn = QPushButton("Apply")
        footer.addWidget(cancel_btn)
        footer.addWidget(apply_btn)
        cancel_btn.clicked.connect(dialog.reject)
        apply_btn.clicked.connect(dialog.accept)

        root = QVBoxLayout()
        root.addLayout(list_row, 1)
        root.addLayout(controls)
        root.addLayout(footer)
        dialog.setLayout(root)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.alarm_keyword_rules = {
            sev: sorted(set(values))
            for sev, values in working_rules.items()
        }
        if self.events:
            self.render_events()

    def find_and_focus_message(self, severity: str, summary_group_key: str) -> bool:
        for row, event in enumerate(self.display_events):
            sev, _ = self.classify_event(event)
            if sev != severity:
                continue
            event_key = self.normalize_summary_for_display_grouping(event.summary, sev)
            if event_key != summary_group_key:
                continue
            idx = self.model.index(row, 0)
            if not idx.isValid():
                return False
            self.tree.scrollTo(idx, QTreeView.ScrollHint.PositionAtCenter)
            self.tree.setCurrentIndex(idx)
            self.tree.setFocus()
            return True
        return False

    def normalize_summary_for_display_grouping(self, summary: str, severity: str) -> str:
        # For noisy error/warn/crit metrics, collapse numeric-only drift into one group.
        sev = severity.upper()
        lowered = summary.lower()
        if sev in {"CRIT", "ERR", "WARN"} and "error" in lowered:
            normalized = self.NUMBER_TOKEN_RE.sub("<num>", summary)
            normalized = re.sub(r"\s+", " ", normalized).strip()
            return normalized
        return summary

    def merge_sorted_events(
        self,
        left: list[LogEvent],
        right: list[LogEvent],
    ) -> list[LogEvent]:
        if not left:
            return list(right)
        if not right:
            return list(left)
        merged: list[LogEvent] = []
        i = 0
        j = 0
        while i < len(left) and j < len(right):
            l = left[i]
            r = right[j]
            l_key = (l.timestamp or datetime.min, l.sequence)
            r_key = (r.timestamp or datetime.min, r.sequence)
            if l_key <= r_key:
                merged.append(l)
                i += 1
            else:
                merged.append(r)
                j += 1
        if i < len(left):
            merged.extend(left[i:])
        if j < len(right):
            merged.extend(right[j:])
        return merged

    def scroll_to_nearest(self, target: datetime) -> None:
        if not self._timestamp_values:
            return
        pos = bisect.bisect_left(self._timestamp_values, target)
        best_ts = None
        if pos <= 0:
            best_ts = self._timestamp_values[0]
        elif pos >= len(self._timestamp_values):
            best_ts = self._timestamp_values[-1]
        else:
            left = self._timestamp_values[pos - 1]
            right = self._timestamp_values[pos]
            if abs((target - left).total_seconds()) <= abs((right - target).total_seconds()):
                best_ts = left
            else:
                best_ts = right

        if best_ts is None:
            return
        row = self._first_row_by_timestamp.get(best_ts)
        if row is None:
            return
        idx = self.model.index(row, 0)
        self.tree.scrollTo(idx, QTreeView.ScrollHint.PositionAtTop)
        self.tree.setCurrentIndex(idx)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LogExpert PySide6 MVP")
        self.resize(1800, 980)
        self.folder_button = QPushButton("Load Log Folder")
        self.folder_label = QLabel("No folder selected")
        self.folder_label.setStyleSheet("color: #777;")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.panes = [
            LogPane("syslog", "SYSLOG (merged by time)"),
            LogPane("event", "EVENT (merged by time)"),
            LogPane("netconf", "NETCONF (merged by time, expandable)"),
        ]
        for pane in self.panes:
            splitter.addWidget(pane)
            pane.setMinimumWidth(260)
            pane.tree.sync_requested.connect(lambda ts, p=pane: self.sync_from_pane(p, ts))
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)

        top_row = QHBoxLayout()
        top_row.addWidget(self.folder_button)
        top_row.addWidget(self.folder_label, 1)

        container = QWidget()
        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(splitter, 1)
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.folder_button.clicked.connect(self.load_log_folder)

    def sync_from_pane(self, source: LogPane, target_ts: datetime) -> None:
        for pane in self.panes:
            if pane is source:
                continue
            pane.scroll_to_nearest(target_ts)

    def load_log_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select log folder", "")
        if not folder:
            return
        self.folder_label.setText(folder)
        for pane in self.panes:
            pane.load_from_folder(folder)
