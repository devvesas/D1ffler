"""Folder sync plan dialog."""

from __future__ import annotations

from d1ffler.background_tasks import job_messages

import queue
import threading
from collections import deque
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from d1ffler.folder_sync import (SyncError, SyncItem, execute_plan, operation_for, METADATA_ACTIONS,
                         metadata_unavailable_reason, metadata_preview, metadata_description)
from d1ffler.folder_sync_dialogs import SyncReportDialog
from d1ffler.shared_sources import SourceScope

class SyncPlanDialog(QDialog):
    ACTIONS = (
        ("Skip", "skip"), ("Copy A → B", "left_to_right"),
        ("Copy B → A", "right_to_left"),
        ("Delete in A", "delete_left"), ("Delete in B", "delete_right"),
        ("Modified time A → B", "mtime_left_to_right"),
        ("Modified time B → A", "mtime_right_to_left"),
        ("Permissions A → B", "permissions_left_to_right"),
        ("Permissions B → A", "permissions_right_to_left"),
        ("Time and permissions A → B", "metadata_left_to_right"),
        ("Time and permissions B → A", "metadata_right_to_left"),
    )

    def __init__(
        self, app: "DiffApp", source_index: int,
        target_index: int | None = None, selected_relative: str | None = None,
    ) -> None:
        super().__init__(app)
        self.app = app
        self.selected_relative = selected_relative
        self.folder_indices = [i for i, panel in enumerate(app.panels) if panel["kind"] == "folder"]
        self.items: dict[str, SyncItem] = {}
        self.row_combos: dict[str, QComboBox] = {}
        self._plan_tree_rows: dict[str, QTreeWidgetItem] = {}
        self._plan_generation = 0
        self._plan_worker = None
        self._plan_jobs = []
        self._plan_pending = deque()
        self._plan_timer_active = False
        self._plan_poll_timer = QTimer(self)
        self._plan_poll_timer.setInterval(20)
        self._plan_poll_timer.timeout.connect(self._poll_plan_jobs)
        self.setWindowTitle("Plan folder sync")
        self.resize(1280, 680)
        layout = QVBoxLayout(self)
        pair = QHBoxLayout()
        pair.addWidget(QLabel("A:"))
        self.left_combo = QComboBox()
        pair.addWidget(self.left_combo, 1)
        pair.addWidget(QLabel("B:"))
        self.right_combo = QComboBox()
        pair.addWidget(self.right_combo, 1)
        for index in self.folder_indices:
            panel = app.panels[index]
            label = f"{panel['name'].text()} · {panel['path']}"
            self.left_combo.addItem(label, index)
            self.right_combo.addItem(label, index)
        left = source_index if source_index in self.folder_indices else self.folder_indices[0]
        right = target_index if target_index in self.folder_indices and target_index != left else next(
            index for index in self.folder_indices if index != left
        )
        self.left_combo.setCurrentIndex(self.left_combo.findData(left))
        self.right_combo.setCurrentIndex(self.right_combo.findData(right))
        layout.addLayout(pair)
        layout.addWidget(QLabel("Status describes B relative to A. Choosing actions does not write files."))
        limits = QLabel("Metadata actions change only the chosen fields. Windows permissions mean writable/read-only; "
                        "POSIX uses permission bits. ZIP metadata and linked targets are unavailable. "
                        "Filesystem support is verified when applying; dry run does not test writes.")
        limits.setWordWrap(True)
        layout.addWidget(limits)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Relative path", "Status", "Effect", "Action", "Metadata changes (target → source)"])
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 140)
        self.tree.setColumnWidth(2, 110)
        self.tree.setColumnWidth(3, 230)
        self.tree.setColumnWidth(4, 500)
        layout.addWidget(self.tree, 1)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Set action for:"))
        self.scope_combo = QComboBox()
        scope_row.addWidget(self.scope_combo, 1)
        self.scope_action = QComboBox()
        for label, value in self.ACTIONS:
            self.scope_action.addItem(label, value)
        scope_row.addWidget(self.scope_action)
        apply_scope = QPushButton("Apply to scope")
        apply_scope.clicked.connect(self._apply_scope)
        scope_row.addWidget(apply_scope)
        layout.addLayout(scope_row)

        options = QHBoxLayout()
        options.addWidget(QLabel("If target changed since review:"))
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItem("Skip that file", "skip")
        self.conflict_combo.addItem("Stop the plan", "stop")
        self.conflict_combo.addItem("Overwrite changed target", "overwrite")
        options.addWidget(self.conflict_combo)
        options.addStretch()
        layout.addLayout(options)
        backup_row = QHBoxLayout()
        self.backup_check = QCheckBox("Back up overwritten, deleted, and metadata targets")
        backup_row.addWidget(self.backup_check)
        self.backup_entry = QLineEdit()
        self.backup_entry.setPlaceholderText("Backup parent directory")
        backup_row.addWidget(self.backup_entry, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_backup)
        backup_row.addWidget(browse)
        layout.addLayout(backup_row)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.cancel_plan_button = QPushButton("Cancel scan")
        self.cancel_plan_button.clicked.connect(self._cancel_plan)
        layout.addWidget(self.cancel_plan_button)
        buttons = QHBoxLayout()
        dry_run = QPushButton("Dry run")
        self.dry_run_button = dry_run
        dry_run.clicked.connect(lambda: self._run(True))
        buttons.addWidget(dry_run)
        buttons.addStretch()
        apply_plan = QPushButton("Apply plan…")
        self.apply_plan_button = apply_plan
        apply_plan.clicked.connect(lambda: self._run(False))
        buttons.addWidget(apply_plan)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.left_combo.currentIndexChanged.connect(self._build_rows)
        self.right_combo.currentIndexChanged.connect(self._build_rows)
        self._build_rows()

    def _roots(self) -> tuple[Path, Path]:
        return (self.app.panels[self.left_combo.currentData()]["path"],
                self.app.panels[self.right_combo.currentData()]["path"])

    def _writable(self, side: str) -> bool:
        index = self.left_combo.currentData() if side == "left" else self.right_combo.currentData()
        source = self.app.panels[index].get("git")
        return not (source and source["readonly"]) and not self.app.panels[index].get("archive")

    def _build_rows(self) -> None:
        self._cancel_plan()
        self._plan_generation += 1
        self.tree.clear()
        self.items.clear()
        self.row_combos.clear()
        self._plan_tree_rows.clear()
        self.scope_combo.clear()
        self._plan_pending.clear()
        self.dry_run_button.setEnabled(False)
        self.apply_plan_button.setEnabled(False)
        if self.left_combo.currentData() == self.right_combo.currentData():
            self.summary.setText("Choose two different folder panels.")
            self.cancel_plan_button.hide()
            return
        left, right = self._roots()
        if left.resolve() == right.resolve():
            self.summary.setText("Choose two different folder paths.")
            self.cancel_plan_button.hide()
            return
        exclusions, ignore_whitespace, ignore_endings, ignore_gitignore = self.app._folder_compare_options()
        event = threading.Event()
        messages = queue.Queue()
        worker = self.app.sync_plan_worker(left, right, exclusions, ignore_whitespace,
                                ignore_endings, ignore_gitignore, event, messages)
        thread = self.app._comparison_thread(worker, "Sync plan")
        generation = self._plan_generation
        self._plan_worker = worker
        self._plan_jobs.append((thread, worker, messages, generation))
        self.summary.setText("Scanning files for sync plan…")
        self.cancel_plan_button.show()
        self._plan_poll_timer.start()
        thread.start()

    def _poll_plan_jobs(self) -> None:
        for job in list(self._plan_jobs):
            thread, worker, messages, generation = job
            for message in job_messages(thread, messages):
                if message[0] == "finished":
                    self._plan_finished(generation, message[1], message[2])
                elif message[0] == "failed":
                    self._plan_finished(generation, None, message[1].summary + " Scan again to retry.")
                elif message[0] == "cancelled":
                    self._plan_finished(generation, None, "Cancelled")
            if not thread.is_alive() and messages.empty():
                self._plan_jobs.remove(job)
        if not self._plan_jobs:
            self._plan_poll_timer.stop()

    def _cancel_plan(self) -> None:
        if self._plan_worker is not None:
            self._plan_worker.cancel_event.set()
            self._plan_worker = None
            self._plan_generation += 1
            self.summary.setText("Sync plan scan cancelled")
        self.cancel_plan_button.hide()

    def done(self, result: int) -> None:
        self._plan_poll_timer.stop()
        self._cancel_plan()
        self._plan_generation += 1
        self._plan_pending.clear()
        for thread, worker, _messages, _generation in list(self._plan_jobs):
            worker.cancel_event.set()
        for thread, _worker, _messages, _generation in list(self._plan_jobs):
            thread.join()
        self._plan_jobs.clear()
        super().done(result)

    def _plan_finished(self, generation: int, rows, error: str) -> None:
        if generation != self._plan_generation:
            return
        self._plan_worker = None
        self.cancel_plan_button.hide()
        if rows is None:
            self.summary.setText(f"Could not build the plan: {error}")
            return
        self.items = {item.relative: item for item in rows}
        self._plan_pending = deque(rows)
        self.scope_combo.addItem("All paths", "")
        scopes = set()
        for item in rows:
            parts = item.relative.split("/")
            for count in range(1, len(parts)):
                scopes.add("/".join(parts[:count]) + "/")
        for scope in sorted(scopes, key=str.casefold):
            self.scope_combo.addItem(scope, scope.rstrip("/"))
        for item in rows:
            self.scope_combo.addItem(item.relative, item.relative)
        self.summary.setText(f"Preparing {len(rows):,} plan rows…")
        self._schedule_plan_rows()

    def _schedule_plan_rows(self) -> None:
        if self._plan_pending and not self._plan_timer_active:
            self._plan_timer_active = True
            QTimer.singleShot(0, self._append_plan_rows)

    def _append_plan_rows(self) -> None:
        self._plan_timer_active = False
        if not self._plan_pending:
            return
        for _ in range(min(32, len(self._plan_pending))):
            item = self._plan_pending.popleft()
            self._append_plan_row(item)
        if self._plan_pending:
            self._schedule_plan_rows()
        else:
            if self.selected_relative:
                scope_index = self.scope_combo.findData(self.selected_relative.rstrip("/"))
                if scope_index >= 0:
                    self.scope_combo.setCurrentIndex(scope_index)
            self.dry_run_button.setEnabled(True)
            self.apply_plan_button.setEnabled(True)
            self._update_summary()

    def _append_plan_row(self, item: SyncItem) -> None:
        detail = item.status.title() + (" · binary" if item.binary else "")
        if (item.status == "identical" and item.left_metadata is not None and item.right_metadata is not None and
                any(getattr(item.left_metadata, name) is not None and getattr(item.right_metadata, name) is not None and
                    getattr(item.left_metadata, name) != getattr(item.right_metadata, name)
                    for name in ("modified_ns", "permission_key"))):
            detail = "Metadata only" + (" · binary" if item.binary else "")
        if item.error:
            detail = f"Unavailable · {item.error}"
        row = QTreeWidgetItem([item.relative, detail, "Skip", "", ""])
        if item.error:
            row.setToolTip(1, item.error)
            row.setToolTip(0, item.error)
        self.tree.addTopLevelItem(row)
        self._plan_tree_rows[item.relative] = row
        combo = QComboBox()
        combo.addItem("Skip", "skip")
        if not item.excluded and not item.error and item.status != "identical":
            if item.left_state and self._writable("right"):
                combo.addItem("Copy A → B", "left_to_right")
            if item.right_state and self._writable("left"):
                combo.addItem("Copy B → A", "right_to_left")
            if item.left_state and self._writable("left"):
                combo.addItem("Delete in A", "delete_left")
            if item.right_state and self._writable("right"):
                combo.addItem("Delete in B", "delete_right")
        if not item.excluded and not item.error:
            for label, action in self.ACTIONS:
                if action not in METADATA_ACTIONS:
                    continue
                reason = metadata_unavailable_reason(item, action)
                side = "right" if action.endswith("left_to_right") else "left"
                if not self._writable(side):
                    reason = "Read-only source cannot be a sync target"
                combo.addItem(label + (" · unavailable" if reason else ""), action)
                index = combo.count() - 1
                if reason:
                    combo.model().item(index).setEnabled(False)
                    combo.setItemData(index, reason, Qt.ItemDataRole.ToolTipRole)
        combo.currentIndexChanged.connect(lambda _value, p=item.relative, r=row: self._update_row(p, r))
        self.tree.setItemWidget(row, 3, combo)
        self.row_combos[item.relative] = combo
        if self.selected_relative:
            if item.relative == self.selected_relative or item.relative.startswith(self.selected_relative.rstrip("/") + "/"):
                index = combo.findData("left_to_right")
                if index >= 0:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(index)
                    combo.blockSignals(False)
                    operation = operation_for(item, "left_to_right", *self._roots())
                    self._show_operation(row, operation)

    def _show_operation(self, row, operation) -> None:
        row.setText(2, operation.effect.title() if operation else "Skip")
        preview = metadata_description(metadata_preview(operation)) if operation and operation.metadata_fields else ""
        row.setText(4, preview)
        row.setToolTip(4, preview)

    def _update_row(self, relative: str, row: QTreeWidgetItem) -> None:
        action = self.row_combos[relative].currentData()
        operation = operation_for(self.items[relative], action, *self._roots())
        self._show_operation(row, operation)
        self._update_summary()

    def _apply_scope(self) -> None:
        scope = self.scope_combo.currentData()
        action = self.scope_action.currentData()
        for relative, combo in self.row_combos.items():
            if not scope or relative == scope or relative.startswith(scope + "/"):
                index = combo.findData(action)
                if index >= 0 and combo.model().item(index).isEnabled():
                    combo.blockSignals(True)
                    combo.setCurrentIndex(index)
                    combo.blockSignals(False)
                    row = self._plan_tree_rows[relative]
                    operation = operation_for(self.items[relative], action, *self._roots())
                    self._show_operation(row, operation)
        self._update_summary()

    def _operations(self) -> list:
        left, right = self._roots()
        result = []
        for relative, item in self.items.items():
            operation = operation_for(item, self.row_combos[relative].currentData(), left, right)
            if operation:
                result.append(operation)
        return result

    def _update_summary(self) -> None:
        operations = self._operations()
        counts = {effect: sum(operation.effect == effect for operation in operations)
                  for effect in ("create", "overwrite", "delete", "metadata")}
        excluded = sum(item.excluded for item in self.items.values())
        binary = sum(item.binary for item in self.items.values())
        self.summary.setText(
            f"Plan: {counts['create']} creates · {counts['overwrite']} overwrites · "
            f"{counts['delete']} deletions · {counts['metadata']} metadata changes · "
            f"{len(self.items) - len(operations)} skips "
            f"({excluded} excluded paths, {binary} binary files)"
        )

    def _browse_backup(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose backup parent directory")
        if path:
            self.backup_entry.setText(path)
            self.backup_check.setChecked(True)

    def _run(self, dry_run: bool) -> None:
        if self._plan_worker is not None or self._plan_pending:
            return
        operations = self._operations()
        if not operations:
            QMessageBox.information(self, "Folder sync", "Choose at least one file action first.")
            return
        left, right = self._roots()
        backup_directory = None
        if self.backup_check.isChecked():
            value = self.backup_entry.text().strip()
            if not value:
                QMessageBox.information(self, "Folder sync", "Choose a backup parent directory.")
                return
            backup_parent = Path(value).expanduser().resolve()
            backup_directory = backup_parent if dry_run else backup_parent / f"D1ffler-sync-{uuid4().hex[:12]}"
            if any(backup_directory == root.resolve() or backup_directory.is_relative_to(root.resolve())
                   for root in (left, right)):
                QMessageBox.warning(self, "Folder sync", "The backup directory must be outside both compared folders.")
                return
        if not dry_run:
            choice = QMessageBox.question(
                self, "Apply folder sync plan",
                f"Apply {len(operations)} reviewed file operations?\n\n"
                f"A: {left}\nB: {right}\n\nCompleted operations will not be rolled back if the plan stops.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        reservation = None
        if not dry_run:
            scopes = [SourceScope(operation.target_root / operation.relative) for operation in operations]
            scopes += [SourceScope(operation.source_root / operation.relative)
                       for operation in operations if operation.source_root is not None]
            if backup_directory is not None:
                scopes.append(SourceScope(backup_directory, folder=True))
            reservation = self.app.shared_sources.begin(scopes, self.app, "Apply folder sync plan", self)
            if reservation is None:
                return
            if reservation.saved:
                reservation.finish(notify=False)
                self._build_rows()
                QMessageBox.information(self, "Review changed sync plan",
                                        "Open edits were saved. Review the new plan before applying it.")
                return
        progress = QProgressDialog("Checking files…", "Cancel", 0, len(operations), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def cancelled() -> bool:
            QApplication.processEvents()
            return progress.wasCanceled() or bool(reservation and reservation.has_unsaved_edits())

        def updated(number: int, _total: int, outcome: dict) -> None:
            progress.setLabelText(f"{outcome['effect'].title()}: {outcome['path']}")
            progress.setValue(number)
            QApplication.processEvents()

        try:
            report = execute_plan(
                operations, left, right, conflict_policy=self.conflict_combo.currentData(),
                backup_directory=backup_directory, dry_run=dry_run,
                cancelled=cancelled, progress=updated,
            )
        except SyncError as exc:
            QMessageBox.warning(self, "Folder sync", str(exc))
            return
        finally:
            progress.close()
            if reservation is not None:
                reservation.finish()
        report_data = report.to_dict()
        report_data["plan"] = []
        for relative, item in self.items.items():
            action = self.row_combos[relative].currentData()
            operation = operation_for(item, action, left, right)
            reviewed = {"path": relative, "status": item.status, "binary": item.binary,
                        "excluded": item.excluded, "action": action,
                        "effect": operation.effect if operation else "skip"}
            if operation and operation.metadata_fields:
                reviewed["metadata_changes"] = metadata_preview(operation)
            report_data["plan"].append(reviewed)
        self.app.sync_reports.append(report_data)
        self.app.sync_reports = self.app.sync_reports[-10:]
        self.app._record_recent_workspace()
        if not dry_run:
            self.app._refresh_folder_comparison()
        SyncReportDialog(report_data, self, app=self.app).exec()
        if not dry_run:
            self._build_rows()
