"""Unsaved session recovery inspector."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from d1ffler.file_content import disk_fingerprint, inspect_file_content
from d1ffler.source_paths import has_relative_sources

class RecoveryDialog(QDialog):
    """Inspect an interrupted session before restoring or discarding its buffers."""

    def __init__(self, snapshot: dict, parent=None) -> None:
        super().__init__(parent)
        self.snapshot = snapshot
        self.choice = "later"
        self.setWindowTitle("Recover unsaved comparison edits")
        self.resize(950, 630)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("An earlier session has unsaved text. Select a buffer to inspect it "
                                "beside the current disk version. Restoring never writes a source file."))
        context = snapshot.get("context")
        if isinstance(context, dict) and context.get("context_version") == 1:
            label = QLabel(f"Workspace: {context.get('name') or 'Unnamed comparison'}. "
                           "Recovery also restores comparison rules, search, filters, and review position. "
                           "Notes are checked again; retained reports are historical.")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Buffer", "Source", "Disk state"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 520)
        layout.addWidget(self.tree, 1)
        self.records = []
        for index, record in enumerate(snapshot.get("panels", [])):
            if not isinstance(record, dict) or not record.get("recover"):
                continue
            path = record.get("editing_path") or record.get("path")
            disk_state = "Pasted text" if not record.get("editing_path") else "Current"
            if has_relative_sources([record]):
                original = record.get("unresolved_source")
                if not isinstance(original, dict):
                    original = record
                path = original.get("editing_path") or original.get("path")
                disk_state = "Original folder unknown"
            elif record.get("editing_path"):
                try:
                    current = disk_fingerprint(Path(record["editing_path"]))
                    if current != record.get("disk_fingerprint"):
                        disk_state = "Changed" if current is not None else "Missing"
                except OSError:
                    disk_state = "Unreadable"
            item = QTreeWidgetItem([record.get("name") or f"Panel {index + 1}", str(path or "Pasted text"), disk_state])
            item.setData(0, Qt.ItemDataRole.UserRole, len(self.records))
            self.records.append(record)
            self.tree.addTopLevelItem(item)
        self.tree.currentItemChanged.connect(self._inspect)
        preview_row = QHBoxLayout()
        self.recovered = QPlainTextEdit()
        self.recovered.setReadOnly(True)
        self.current = QPlainTextEdit()
        self.current.setReadOnly(True)
        for title, editor in (("Recovered text", self.recovered), ("Current disk file", self.current)):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            column.addWidget(editor)
            preview_row.addLayout(column, 1)
        layout.addLayout(preview_row, 2)
        buttons = QHBoxLayout()
        buttons.addStretch()
        restore = QPushButton("Restore buffers")
        restore.setObjectName("primaryButton")
        restore.clicked.connect(lambda: self._finish("restore"))
        buttons.addWidget(restore)
        discard = QPushButton("Discard recovery")
        discard.clicked.connect(lambda: self._finish("discard"))
        buttons.addWidget(discard)
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        buttons.addWidget(later)
        layout.addLayout(buttons)
        if self.records:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _inspect(self, item, _previous) -> None:
        if item is None:
            return
        record = self.records[item.data(0, Qt.ItemDataRole.UserRole)]
        self.recovered.setPlainText(record.get("text", ""))
        if has_relative_sources([record]):
            self.current.setPlainText("The original working folder was not recorded. Locate it during "
                                      "restore, or restore edits as text and save to a chosen file.")
            return
        path = record.get("editing_path")
        if not path:
            self.current.setPlainText("No disk source for this buffer.")
            return
        try:
            content = inspect_file_content(Path(path), preview_limit=True)
            self.current.setPlainText(content.text if content.text is not None else
                                      "Disk file is binary or exceeds the editor preview limit.")
        except OSError as exc:
            self.current.setPlainText(f"Could not read current disk file: {exc}")

    def _finish(self, choice: str) -> None:
        self.choice = choice
        self.accept()
