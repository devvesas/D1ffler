"""Folder sync and restore report and plan dialogs."""

from __future__ import annotations

from d1ffler.background_tasks import job_messages

import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QProgressDialog, QPushButton, QSplitter, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from d1ffler.file_content import inspect_file_content, unloaded_file_message
from d1ffler.folder_restore import RestoreItem, build_restore_plan, execute_restore
from d1ffler.folder_sync import SyncError, safe_path, metadata_description, metadata_value
from d1ffler.shared_sources import SourceScope


def retained_report_notice(report, layout):
    state = report.get("context_status")
    if state:
        text = ("Retained report unavailable: its stored file is missing or changed." if state == "unavailable" else
                "Historical report: records a previous operation, not the current comparison or disk state.")
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)

class SyncReportDialog(QDialog):
    def __init__(self, report: dict, parent=None, app=None) -> None:
        super().__init__(parent)
        self.report = report
        self.app = app
        self.setWindowTitle("Folder sync report")
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        retained_report_notice(report, layout)
        outcomes = report.get("outcomes", [])
        planned = report.get("plan", [])
        counts = {status: sum(item.get("status") == status for item in outcomes)
                  for status in ("completed", "dry run", "conflict", "error", "unavailable")}
        skipped = sum(item.get("action") == "skip" or item.get("effect") == "skip" for item in planned)
        not_run = max(0, len(planned) - skipped - len(outcomes))
        heading = QLabel(
            f"{counts['completed']} completed · {counts['dry run']} dry run · "
            f"{counts['conflict']} conflicts · {counts['error']} errors · "
            f"{counts['unavailable']} unavailable · "
            f"{skipped} skipped · {not_run} not run"
            + (" · cancelled" if report.get("cancelled") else "")
            + (" · stopped on conflict" if report.get("stopped_on_conflict") else "")
        )
        layout.addWidget(heading)
        detail = QTextEdit()
        detail.setReadOnly(True)
        lines = [f"Left: {report.get('left', '')}", f"Right: {report.get('right', '')}",
                 f"Started: {report.get('started_at', '')}",
                 f"{'Backup location (dry run)' if report.get('dry_run') else 'Backup'}: "
                 f"{report.get('backup_directory') or 'none'}", "",
                 "Completed operations are not rolled back after cancellation or an error.", ""]
        for item in outcomes:
            lines.append(f"{item.get('status', '').upper()} · {item.get('effect', '')} · {item.get('path', '')}")
            if item.get("detail"):
                lines.append(f"  {item['detail']}")
            for change in item.get("metadata_changes", []):
                lines.append(f"  {metadata_description([change])} · {change.get('status', 'planned')}")
        if planned:
            lines.extend(["", "Reviewed plan:"])
            for item in planned:
                lines.append(
                    f"{item.get('action', 'skip')} · {item.get('effect', 'skip')} · "
                    f"{item.get('status', '')} · {item.get('path', '')}"
                )
                if item.get("metadata_changes"):
                    lines.append("  " + metadata_description(item["metadata_changes"]))
        detail.setPlainText("\n".join(lines))
        layout.addWidget(detail, 1)
        row = QHBoxLayout()
        export = QPushButton("Export report…")
        export.clicked.connect(self._export)
        row.addWidget(export)
        if (app is not None and report.get("backup_directory") and not report.get("dry_run") and
                any(item.get("status") == "completed" for item in outcomes)):
            restore = QPushButton("Review restore…")
            restore.clicked.connect(self._review_restore)
            row.addWidget(restore)
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

    def _review_restore(self) -> None:
        RestorePlanDialog(self.report, self.app, self).exec()

    def _export(self) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self, "Export sync report", "D1ffler-sync-report.json", "JSON files (*.json)"
        )
        if not filename:
            return
        try:
            Path(filename).write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export report", str(exc))


class RestoreReportDialog(QDialog):
    def __init__(self, report: dict, parent=None) -> None:
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("Folder restore report")
        self.resize(700, 430)
        layout = QVBoxLayout(self)
        retained_report_notice(report, layout)
        outcomes = report.get("outcomes", [])
        planned = report.get("plan", [])
        counts = {status: sum(item.get("status") == status for item in outcomes)
                  for status in ("completed", "dry run", "conflict", "error", "unavailable")}
        layout.addWidget(QLabel(
            f"{counts['completed']} completed · {counts['dry run']} dry run · "
            f"{counts['conflict']} conflicts · {counts['error']} errors · "
            f"{counts['unavailable']} unavailable · "
            f"{sum(item.get('action') == 'skip' for item in planned)} skipped"
            + (" · cancelled" if report.get("cancelled") else "")
            + (" · stopped on conflict" if report.get("stopped_on_conflict") else "")
        ))
        detail = QTextEdit()
        detail.setReadOnly(True)
        lines = [f"Source sync: {report.get('source_started_at', '')}",
                 f"Backup directory: {report.get('source_backup_directory', '')}",
                 f"Started: {report.get('started_at', '')}",
                 "Original sync report and backup files are retained.", ""]
        for item in outcomes:
            lines.append(f"{item.get('status', '').upper()} · {item.get('action', '')} · "
                         f"{item.get('side', '')}: {item.get('path', '')}")
            if item.get("detail"):
                lines.append(f"  {item['detail']}")
            for change in item.get("metadata_changes", []):
                lines.append(f"  {metadata_description([change])} · {change.get('status', 'planned')}")
        if planned:
            lines.extend(["", "Reviewed plan:"])
            for item in planned:
                lines.append(f"{item.get('action', 'skip')} · {item.get('status', '')} · "
                             f"{item.get('side', '')}: {item.get('path', '')}")
        detail.setPlainText("\n".join(lines))
        layout.addWidget(detail, 1)
        row = QHBoxLayout()
        export = QPushButton("Export report…")
        export.clicked.connect(self._export)
        row.addWidget(export)
        row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

    def _export(self) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self, "Export restore report", "D1ffler-restore-report.json", "JSON files (*.json)"
        )
        if not filename:
            return
        try:
            Path(filename).write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export restore report", str(exc))


class RestorePlanDialog(QDialog):
    def __init__(self, report: dict, app, parent=None) -> None:
        super().__init__(parent)
        self.report = report
        self.app = app
        self.items: list[RestoreItem] = []
        self.row_combos: list[QComboBox] = []
        self._cancel_event = threading.Event()
        self._messages = queue.Queue()
        self._worker: threading.Thread | None = None
        self.setWindowTitle("Review folder restore")
        self.resize(1050, 580)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose files to restore from the recorded backup. Created files can be removed. "
                                "No files change until you apply the plan."))
        layout.addWidget(QLabel(f"A: {report.get('left', '')}    B: {report.get('right', '')}\n"
                                f"Backups: {report.get('backup_directory', '')}"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Relative path", "Target", "Current / saved backup", "Review status", "Action"])
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 130)
        self.tree.setColumnWidth(2, 330)
        self.tree.setColumnWidth(3, 190)
        layout.addWidget(self.tree, 1)
        options = QHBoxLayout()
        self.select_ready_button = QPushButton("Select ready files")
        self.select_ready_button.clicked.connect(self._select_ready)
        self.select_ready_button.setEnabled(False)
        options.addWidget(self.select_ready_button)
        self.inspect_button = QPushButton("Inspect selected…")
        self.inspect_button.setEnabled(False)
        self.inspect_button.clicked.connect(self._inspect_selected)
        options.addWidget(self.inspect_button)
        self.tree.itemSelectionChanged.connect(
            lambda: self.inspect_button.setEnabled(self.tree.currentItem() is not None and bool(self.items)))
        options.addWidget(QLabel("If a target changed since sync or review:"))
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItem("Skip that file", "skip")
        self.conflict_combo.addItem("Stop the restore", "stop")
        self.conflict_combo.addItem("Overwrite changed target", "overwrite")
        options.addWidget(self.conflict_combo)
        options.addStretch()
        layout.addLayout(options)
        self.summary = QLabel("Checking backup and target files…")
        layout.addWidget(self.summary)
        buttons = QHBoxLayout()
        self.cancel_scan_button = QPushButton("Cancel scan")
        self.cancel_scan_button.clicked.connect(self._cancel_scan)
        buttons.addWidget(self.cancel_scan_button)
        self.dry_run_button = QPushButton("Dry run")
        self.dry_run_button.setEnabled(False)
        self.dry_run_button.clicked.connect(lambda: self._run(True))
        buttons.addWidget(self.dry_run_button)
        buttons.addStretch()
        self.apply_button = QPushButton("Apply restore…")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(lambda: self._run(False))
        buttons.addWidget(self.apply_button)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)
        self._start_scan()

    def _start_scan(self) -> None:
        self._cancel_event = threading.Event()
        self._messages = queue.Queue()
        worker = SimpleNamespace(run=self._scan, cancel_event=self._cancel_event)
        self._worker = self.app._comparison_thread(worker, "Restore plan")
        self._timer.start()
        self._worker.start()

    def _scan(self) -> None:
        try:
            self._messages.put((build_restore_plan(self.report, self._cancel_event.is_set), ""))
        except (OSError, SyncError) as exc:
            self._messages.put((None, str(exc)))

    def _poll(self) -> None:
        messages = job_messages(self._worker, self._messages, terminal=lambda _message: True)
        if not messages:
            return
        message = messages[-1]
        if message[0] == "failed":
            items, error = None, message[1].summary
        elif message[0] == "cancelled":
            items, error = None, "Cancelled"
        else:
            items, error = message
        self._timer.stop()
        self._worker = None
        self.cancel_scan_button.hide()
        if error:
            self.summary.setText(f"Restore plan unavailable: {error}")
            return
        self.items = items
        self.tree.clear()
        self.row_combos.clear()
        for item in items:
            current = "missing" if item.reviewed_current is None else f"{item.reviewed_current.size:,} B · {item.reviewed_current.sha256[:12]}"
            saved = ("none" if item.backup is None else "missing" if item.reviewed_backup is None
                     else f"{item.reviewed_backup.size:,} B · {item.reviewed_backup.sha256[:12]}")
            if item.metadata_fields and item.reviewed_metadata and item.backup_metadata:
                current += " · " + "; ".join(metadata_value(name, getattr(item.reviewed_metadata, name))
                                             for name in item.metadata_fields)
                saved += " · " + "; ".join(metadata_value(name, getattr(item.backup_metadata, name))
                                           for name in item.metadata_fields)
            expected = ("unknown" if not item.after_known else "missing" if item.expected_after is None
                        else f"{item.expected_after.size:,} B · {item.expected_after.sha256[:12]}")
            row = QTreeWidgetItem([item.relative, item.side.title(),
                                   f"Current: {current} · Backup: {saved}",
                                   item.status.title(), ""])
            row.setToolTip(0, f"Target: {item.target_root / item.relative}\n"
                              f"Backup: {item.backup or 'none'}\nPost-sync: {expected}\n{item.detail}")
            row.setToolTip(3, item.detail)
            if item.status == "unavailable":
                row.setForeground(3, QColor("#ff8585"))
            elif item.status in {"changed", "unknown"}:
                row.setForeground(3, QColor("#f3c969"))
            self.tree.addTopLevelItem(row)
            combo = QComboBox()
            combo.addItem("Skip", "skip")
            if item.status != "unavailable":
                combo.addItem("Restore metadata" if item.action == "restore_metadata" else
                              "Restore backup" if item.action == "restore_backup" else
                              "Delete created file", "restore")
            combo.currentIndexChanged.connect(lambda _index: self._update_summary())
            self.tree.setItemWidget(row, 4, combo)
            self.row_combos.append(combo)
        self.select_ready_button.setEnabled(bool(items))
        self._update_summary()

    def _selected(self) -> list[RestoreItem]:
        return [item for item, combo in zip(self.items, self.row_combos)
                if combo.currentData() == "restore"]

    def _select_ready(self) -> None:
        for item, combo in zip(self.items, self.row_combos):
            if item.status == "ready":
                combo.setCurrentIndex(combo.findData("restore"))
        self._update_summary()

    def _inspect_selected(self) -> None:
        row = self.tree.currentItem()
        index = self.tree.indexOfTopLevelItem(row) if row is not None else -1
        if index < 0 or index >= len(self.items):
            return
        item = self.items[index]
        preview = QDialog(self)
        preview.setWindowTitle(f"Restore preview · {item.relative}")
        preview.resize(900, 550)
        layout = QVBoxLayout(preview)
        layout.addWidget(QLabel(f"Target: {item.target_root / item.relative}\n"
                                f"Action: {'Restore metadata' if item.action == 'restore_metadata' else 'Restore saved backup' if item.action == 'restore_backup' else 'Delete created file'} · "
                                f"Status: {item.status}"))
        if item.metadata_fields and item.reviewed_metadata and item.backup_metadata:
            label = QLabel(metadata_description([{"field": name, "before": getattr(item.reviewed_metadata, name),
                                                  "after": getattr(item.backup_metadata, name)}
                                                 for name in item.metadata_fields]))
            label.setWordWrap(True)
            layout.addWidget(label)
        panes = QSplitter(Qt.Orientation.Horizontal)
        for label, path in (("Current target", item.target_root / item.relative),
                            ("Saved backup", item.backup)):
            frame = QWidget()
            column = QVBoxLayout(frame)
            column.addWidget(QLabel(label + (f" · {path}" if path else "")))
            editor = QTextEdit()
            editor.setReadOnly(True)
            try:
                if path is None:
                    content = ("Backup unavailable." if item.action in {"restore_backup", "restore_metadata"} else
                               "No backup was made for a created file.")
                else:
                    safe = safe_path(item.target_root if label == "Current target" else item.backup_root,
                                     item.relative if label == "Current target" else f"{item.side}/{item.relative}")
                    if label == "Saved backup" and safe.resolve() != path.resolve():
                        raise SyncError("Recorded backup path differs from the expected location.")
                    if not safe.is_file():
                        content = "File is missing."
                    else:
                        inspected = inspect_file_content(safe, preview_limit=True)
                        content = inspected.text if inspected.text is not None else unloaded_file_message(safe, inspected)
            except (OSError, SyncError) as exc:
                content = f"Preview unavailable: {exc}"
            editor.setPlainText(content)
            column.addWidget(editor, 1)
            panes.addWidget(frame)
        layout.addWidget(panes, 1)
        close = QPushButton("Close")
        close.clicked.connect(preview.accept)
        layout.addWidget(close)
        preview.exec()

    def _update_summary(self) -> None:
        chosen = self._selected()
        counts = {status: sum(item.status == status for item in self.items)
                  for status in ("ready", "changed", "unknown", "unavailable")}
        self.summary.setText(f"{len(chosen)} selected · {counts['ready']} ready · "
                             f"{counts['changed']} changed · {counts['unknown']} unknown · "
                             f"{counts['unavailable']} unavailable")
        self.dry_run_button.setEnabled(bool(chosen))
        self.apply_button.setEnabled(bool(chosen))

    def _cancel_scan(self) -> None:
        self._cancel_event.set()
        self.summary.setText("Cancelling restore scan…")

    def _run(self, dry_run: bool) -> None:
        chosen = self._selected()
        if not chosen:
            return
        if not dry_run:
            choice = QMessageBox.question(
                self, "Apply folder restore",
                f"Apply {len(chosen)} reviewed restore actions?\n\n"
                f"A: {self.report.get('left', '')}\nB: {self.report.get('right', '')}\n"
                f"Backups: {self.report.get('backup_directory', '')}\n\n"
                "Completed actions are not rolled back if the restore stops. "
                "Original backups and the sync report are retained.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        reservation = None
        if not dry_run:
            scopes = [SourceScope(item.target_root / item.relative) for item in chosen]
            scopes += [SourceScope(item.backup) for item in chosen if item.backup is not None]
            reservation = self.app.shared_sources.begin(scopes, self.app, "Apply folder restore", self)
            if reservation is None:
                return
            if reservation.saved:
                reservation.finish(notify=False)
                # Reviewed fingerprints are now obsolete. Reopen to scan saved targets again.
                self.apply_button.setEnabled(False)
                self.dry_run_button.setEnabled(False)
                self.tree.clear()
                self.items = []
                self.row_combos = []
                self.summary.setText("Open edits were saved. Close and reopen restore to review current files.")
                QMessageBox.information(self, "Review changed restore plan", self.summary.text())
                return
        progress = QProgressDialog("Checking restore files…", "Cancel", 0, len(chosen), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def cancelled() -> bool:
            QApplication.processEvents()
            return progress.wasCanceled() or bool(reservation and reservation.has_unsaved_edits())

        def updated(number: int, _total: int, outcome: dict) -> None:
            progress.setLabelText(f"{outcome['action']}: {outcome['path']}")
            progress.setValue(number)
            QApplication.processEvents()

        try:
            restore = execute_restore(chosen, self.report,
                                      conflict_policy=self.conflict_combo.currentData(),
                                      dry_run=dry_run, cancelled=cancelled, progress=updated)
        finally:
            progress.close()
            if reservation is not None:
                reservation.finish()
        data = restore.to_dict()
        data["plan"] = [
            {"path": item.relative, "side": item.side, "status": item.status,
             "action": combo.currentData(), "backup_path": str(item.backup) if item.backup else None}
            for item, combo in zip(self.items, self.row_combos)
        ]
        self.app.restore_reports.append(data)
        self.app.restore_reports = self.app.restore_reports[-10:]
        self.app._record_recent_workspace()
        if not dry_run:
            self.app._refresh_folder_comparison()
        RestoreReportDialog(data, self).exec()
        if not dry_run:
            self.tree.clear()
            self.items = []
            self.row_combos = []
            self.summary.setText("Restore finished. Close and review the sync report to build a new plan.")
            self.dry_run_button.setEnabled(False)
            self.apply_button.setEnabled(False)

    def reject(self) -> None:
        self._cancel_event.set()
        self._timer.stop()
        if self._worker is not None:
            self._worker.join()
        super().reject()
