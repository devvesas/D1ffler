"""Inspect and explicitly export redacted diagnostic records."""

import json

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from d1ffler.app_version import APP_VERSION


class DiagnosticsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("D1ffler diagnostics")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        location = QLabel(f"D1ffler {APP_VERSION}\nLocal logs: {store.directory}")
        location.setWordWrap(True)
        location.setTextFormat(Qt.TextFormat.PlainText)
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(location)
        layout.addWidget(QLabel("Logs omit file contents, exception messages, absolute paths, and local variables.\n"
                               "They retain incident IDs, operation types, and traceback locations."))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        try:
            events = store.read_events()
            self.preview.setPlainText(json.dumps(events, indent=2) if events else "No diagnostic incidents recorded.")
        except OSError:
            self.preview.setPlainText("Local diagnostic logs could not be read.")
        layout.addWidget(self.preview, 1)
        self.include_paths = QCheckBox("Include the current comparison's source paths and the local log location in the export")
        self.include_paths.setChecked(False)
        layout.addWidget(self.include_paths)
        export = QPushButton("Export diagnostics…")
        export.clicked.connect(self._export)
        layout.addWidget(export)
        clear = QPushButton("Clear local diagnostics…")
        clear.clicked.connect(self._clear)
        layout.addWidget(clear)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _clear(self):
        if QMessageBox.question(self, "Clear local diagnostics", "Delete the local log and its two rotations? "
                                "Exports and open edits stay unchanged.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.clear()
            self.preview.setPlainText("Local diagnostic logs cleared. New incidents will be recorded normally.")
        except OSError as exc:
            QMessageBox.warning(self, "Clear local diagnostics", f"Clearing stopped; remaining logs were retained:\n{exc}")

    def _export(self):
        from pathlib import Path
        destination, _filter = QFileDialog.getSaveFileName(self, "Export diagnostics", "D1ffler-diagnostics.json",
                                                         "JSON files (*.json)")
        if not destination:
            return
        context = None
        if self.include_paths.isChecked():
            parent = self.parent()
            context = {"log_directory": str(self.store.directory),
                       "source_paths": [str(panel.get("editing_path") or panel.get("path"))
                                        for panel in getattr(parent, "panels", [])
                                        if panel.get("editing_path") or panel.get("path")]}
        try:
            self.store.export(Path(destination), shared_context=context)
        except OSError as exc:
            QMessageBox.warning(self, "Export diagnostics", f"Could not export diagnostics:\n{exc}")
            return
        QMessageBox.information(self, "Export diagnostics", "Diagnostic report exported. Review it before sharing.")
