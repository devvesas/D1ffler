"""Review note and fingerprint dialogs."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from d1ffler.background_tasks import BackgroundTask, job_messages

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from d1ffler.review_notes import STATES, content_fingerprint, note_is_stale

class FingerprintDialog(QDialog):
    """Hash only reviewed paths while keeping the Qt event loop responsive."""

    def __init__(self, sources: list[dict], relatives: list[str | None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Checking review content")
        self.setModal(True)
        self.resize(420, 120)
        self.results: dict[str | None, str | None] = {}
        self._cancel_event = threading.Event()
        self._messages = queue.Queue()
        self._relatives = list(dict.fromkeys(relatives))
        self._sources = sources
        layout = QVBoxLayout(self)
        self.label = QLabel("Checking reviewed files…")
        layout.addWidget(self.label)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)
        worker = SimpleNamespace(run=self._run, cancel_event=self._cancel_event)
        self._thread = (parent._comparison_thread(worker, "Review fingerprints")
                        if hasattr(parent, "_comparison_thread") else
                        BackgroundTask(None, worker, operation="Review fingerprints"))
        self._thread.start()
        self._timer.start()

    def _run(self) -> None:
        results = {}
        for index, relative in enumerate(self._relatives):
            if self._cancel_event.is_set():
                break
            try:
                results[relative] = content_fingerprint(
                    self._sources, relative, self._cancel_event.is_set)
            except (OSError, ValueError, InterruptedError):
                results[relative] = None
            self._messages.put(("progress", index + 1, len(self._relatives)))
        self._messages.put(("done", results))

    def _poll(self) -> None:
        for message in job_messages(self._thread, self._messages, limit=64,
                                    terminal=lambda message: message[0] == "done"):
            if message[0] == "progress":
                self.label.setText(f"Checking reviewed files · {message[1]}/{message[2]}")
            elif message[0] == "done":
                self.results = message[1]
                self.accept()
                return
            elif message[0] == "failed":
                self._timer.stop()
                self.label.setText(f"Review content check failed. Close and retry. {message[1].summary}")
                return
            elif message[0] == "cancelled":
                self.reject()
                return

    def done(self, result: int) -> None:
        self._timer.stop()
        self._cancel_event.set()
        self._thread.join()
        super().done(result)


class ReviewNoteDialog(QDialog):
    def __init__(self, note: dict | None = None, *, stale: bool = False,
                 can_reattach: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review note")
        self.resize(520, 320)
        layout = QVBoxLayout(self)
        if stale:
            layout.addWidget(QLabel("This note is stale: its compared sources or file content changed."))
        layout.addWidget(QLabel("Review state (records a decision; it does not apply or save files):"))
        self.state = QComboBox()
        for value in STATES:
            self.state.addItem(value)
        if note:
            self.state.setCurrentText(str(note.get("state", "Open")))
        layout.addWidget(self.state)
        layout.addWidget(QLabel("Note:"))
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("Reason, decision, or follow-up…")
        self.body.setPlainText(str(note.get("text", "")) if note else "")
        layout.addWidget(self.body, 1)
        self.reattach = QCheckBox("Attach this note to the current file content")
        self.reattach.setVisible(stale and can_reattach)
        layout.addWidget(self.reattach)
        buttons = QHBoxLayout()
        buttons.addStretch()
        save = QPushButton("Save note")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _save(self) -> None:
        if not self.body.toPlainText().strip() and self.state.currentText() == "Open":
            QMessageBox.information(self, "Review note", "Enter a note or choose a review decision.")
            return
        if len(self.body.toPlainText()) > 4000:
            QMessageBox.information(self, "Review note", "Keep notes under 4,000 characters.")
            return
        self.accept()


class ReviewNotesDialog(QDialog):
    def __init__(self, notes: list[dict], current_key: str,
                 fingerprints: dict[str | None, str | None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review notes")
        self.resize(840, 500)
        self.notes = [dict(note) for note in notes]
        self.current_key = current_key
        self.fingerprints = fingerprints
        self.changed = False
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Path or hunk", "State", "Status", "Note"])
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(3, 300)
        layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        edit = QPushButton("Edit selected…")
        edit.clicked.connect(self._edit)
        buttons.addWidget(edit)
        delete = QPushButton("Delete selected")
        delete.clicked.connect(self._delete)
        buttons.addWidget(delete)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._fill()

    def _stale(self, note: dict) -> bool:
        return note_is_stale(note, self.current_key, self.fingerprints.get(note.get("path")))

    def _fill(self) -> None:
        self.tree.clear()
        for note in self.notes:
            path = str(note.get("path") or "File comparison")
            if note.get("kind") == "hunk":
                path += f" · hunk near line {int(note.get('base_start', 0)) + 1}"
            row = QTreeWidgetItem([
                path, str(note.get("state", "Open")),
                "STALE" if self._stale(note) else "Current",
                str(note.get("text", "")).replace("\n", " ")[:120],
            ])
            row.setData(0, Qt.ItemDataRole.UserRole, note.get("id"))
            self.tree.addTopLevelItem(row)

    def _selected(self) -> dict | None:
        rows = self.tree.selectedItems()
        if not rows:
            return None
        identifier = rows[0].data(0, Qt.ItemDataRole.UserRole)
        return next((note for note in self.notes if note.get("id") == identifier), None)

    def _edit(self) -> None:
        note = self._selected()
        if note is None:
            return
        current = self.fingerprints.get(note.get("path"))
        editor = ReviewNoteDialog(note, stale=self._stale(note), can_reattach=current is not None, parent=self)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        note["state"] = editor.state.currentText()
        note["text"] = editor.body.toPlainText().strip()
        if editor.reattach.isChecked() and current is not None:
            note["source_key"] = self.current_key
            note["fingerprint"] = current
            note["hunk_stale"] = False
            note["stale"] = False
        self.changed = True
        self._fill()

    def _delete(self) -> None:
        note = self._selected()
        if note is None:
            return
        choice = QMessageBox.question(self, "Delete review note", "Delete the selected note?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if choice != QMessageBox.StandardButton.Yes:
            return
        self.notes = [item for item in self.notes if item.get("id") != note.get("id")]
        self.changed = True
        self._fill()
