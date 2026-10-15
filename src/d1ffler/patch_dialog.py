"""Reviewed application of a unified text patch to an editor or folder."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from d1ffler.folder_sync import SyncError
from d1ffler.shared_sources import SourceScope
from d1ffler.unified_patch import PatchError, apply_folder_plan, apply_hunks, plan_folder_patch


class PatchDialog(QDialog):
    def __init__(self, app, source_text: str, files: tuple, patch_path: Path):
        super().__init__(app)
        self.app = app
        self.files = files
        self.patch_path = patch_path
        self._updating = False
        self._preview_before = ""
        self._preview_result = ""
        self._plan = None
        self.setWindowTitle(f"Review patch · {patch_path.name}")
        self.resize(1040, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Patch: {patch_path}"))
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Apply to:"))
        self.target = QComboBox()
        for index, panel in enumerate(app.panels):
            if panel.get("git") and panel["git"]["readonly"]:
                continue
            if panel["kind"] == "folder":
                if not panel["path"].is_dir():
                    continue
                if (panel.get("editing_path") is not None and
                        panel["view_stack"].currentIndex() == 0 and
                        not panel["editor"].isReadOnly()):
                    self.target.addItem(f"{panel['name'].text()} · open file buffer", ("editor", index))
                self.target.addItem(f"{panel['name'].text()} · folder {panel['path']}", ("folder", index))
            elif not panel["editor"].isReadOnly() and not panel.get("binary"):
                self.target.addItem(f"{panel['name'].text()} · text buffer", ("editor", index))
        target_row.addWidget(self.target, 1)
        layout.addLayout(target_row)
        layout.addWidget(QLabel("Select files and hunks. A failed or ambiguous selected hunk prevents Apply."))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Patch file / hunk", "Preview status", "Target location"])
        self.tree.setColumnWidth(0, 450)
        self.tree.setColumnWidth(1, 200)
        layout.addWidget(self.tree, 1)
        for file_index, patch in enumerate(files):
            label = f"{patch.old_path or '[new]'} → {patch.new_path or '[deleted]'}"
            row = QTreeWidgetItem([label, "", ""])
            row.setData(0, Qt.ItemDataRole.UserRole, (file_index, -1))
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(0, Qt.CheckState.Checked)
            self.tree.addTopLevelItem(row)
            for hunk_index, hunk in enumerate(patch.hunks):
                child = QTreeWidgetItem([hunk.header, "", ""])
                child.setData(0, Qt.ItemDataRole.UserRole, (file_index, hunk_index))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked)
                row.addChild(child)
            row.setExpanded(True)
        self.tree.itemChanged.connect(self._update_preview)
        self.target.currentIndexChanged.connect(self._update_preview)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.patch_text = QPlainTextEdit()
        self.patch_text.setReadOnly(True)
        self.patch_text.setPlainText(source_text)
        self.patch_text.setPlaceholderText("Patch source")
        split.addWidget(self.patch_text)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setPlaceholderText("Result preview / folder plan")
        split.addWidget(self.result_text)
        layout.addWidget(split, 2)
        backup_row = QHBoxLayout()
        self.backup_check = QCheckBox("Back up changed source files")
        backup_row.addWidget(self.backup_check)
        self.backup_path = QLineEdit()
        self.backup_path.setPlaceholderText("Backup directory outside target folder")
        backup_row.addWidget(self.backup_path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_backup)
        backup_row.addWidget(browse)
        layout.addLayout(backup_row)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.apply_button = QPushButton("Apply selected…")
        self.apply_button.clicked.connect(self._apply)
        buttons.addWidget(self.apply_button)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self._update_preview()

    def _browse_backup(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choose patch backup directory")
        if chosen:
            self.backup_path.setText(chosen)
            self.backup_check.setChecked(True)

    def _selection(self) -> dict[int, tuple[int, ...]]:
        selected = {}
        for file_index in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(file_index)
            if row.checkState(0) != Qt.CheckState.Checked:
                continue
            selected[file_index] = tuple(index for index in range(row.childCount())
                                         if row.child(index).checkState(0) == Qt.CheckState.Checked)
        return selected

    def _set_hunk_results(self, results: dict[int, list]) -> None:
        self._updating = True
        try:
            for file_index in range(self.tree.topLevelItemCount()):
                row = self.tree.topLevelItem(file_index)
                row.setText(1, "Selected" if row.checkState(0) == Qt.CheckState.Checked else "Skipped")
                for hunk_index in range(row.childCount()):
                    child = row.child(hunk_index)
                    entry = results.get(file_index, [])
                    result = entry[hunk_index] if hunk_index < len(entry) else None
                    child.setText(1, result.status if result else "")
                    child.setText(2, result.detail if result else "")
        finally:
            self._updating = False

    def _update_preview(self, *_):
        if self._updating:
            return
        choice = self.target.currentData()
        selected = self._selection()
        self._plan = None
        self._preview_before = self._preview_result = ""
        self.apply_button.setEnabled(False)
        if choice is None:
            self.summary.setText("No editable target is available.")
            return
        kind, index = choice
        self.backup_check.setVisible(kind == "folder")
        self.backup_path.setVisible(kind == "folder")
        if not selected:
            self.summary.setText("Select at least one patch file.")
            self.result_text.clear()
            self._set_hunk_results({})
            return
        panel = self.app.panels[index]
        if kind == "editor":
            if len(selected) != 1 or len(self.files) != 1:
                self.summary.setText("A text buffer accepts one patch file. Choose a folder for a multi-file patch.")
                self.result_text.clear()
                return
            if panel["editor"].isReadOnly() or panel.get("binary"):
                self.summary.setText("The selected editor is read-only.")
                return
            self._preview_before = self.app._panel_text_with_line_endings(panel)
            self._preview_result, statuses = apply_hunks(
                self._preview_before, self.files[0], selected[0])
            self.result_text.setPlainText(self._preview_result)
            self._set_hunk_results({0: statuses})
            failures = [item for item in statuses if item.status in {"failed", "ambiguous"}]
            if not selected[0]:
                self.summary.setText("Select a hunk to apply to this buffer.")
                return
            if failures:
                self.summary.setText(f"{len(failures)} selected hunk(s) cannot be applied. Uncheck them or choose another target.")
                return
            self.summary.setText(f"{len(selected[0])} hunk(s) will edit {panel['name'].text()} in memory. Save separately.")
            self.apply_button.setEnabled(True)
            return
        self._plan = plan_folder_patch(panel["path"], self.files, selected)
        self._set_hunk_results(self._plan.hunk_results)
        descriptions = [f"{op.effect}: {op.source_relative or '[new]'} → {op.target_relative or '[deleted]'}"
                        for op in self._plan.operations]
        self.result_text.setPlainText("\n".join(descriptions + self._plan.errors))
        if self._plan.errors:
            self.summary.setText(f"{len(self._plan.errors)} file(s) cannot be applied. Uncheck them or fix the target.")
        elif not self._plan.operations:
            self.summary.setText("No patch operations selected.")
        else:
            self.summary.setText(f"{len(self._plan.operations)} reviewed operation(s) will change files in {panel['path']}.")
            self.apply_button.setEnabled(True)

    def _apply(self):
        prior_before = self._preview_before
        prior_result = self._preview_result
        prior_plan = self._plan
        self._update_preview()
        if not self.apply_button.isEnabled():
            return
        kind, index = self.target.currentData()
        panel = self.app.panels[index]
        if kind == "editor":
            if self._preview_before != prior_before or self._preview_result != prior_result:
                QMessageBox.warning(self, "Apply patch", "The target changed since preview. Review the patch again.")
                return
            choice = QMessageBox.question(
                self, "Apply patch", f"Apply selected hunks to {panel['name'].text()}?\n\n"
                "This changes the editor buffer and can be undone. Save separately.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Yes:
                return
            self._apply_editor(index, self._preview_result)
            self.app.status.setText(f"Applied patch to {panel['name'].text()} · unsaved")
            self.accept()
            return
        if prior_plan is None or prior_plan.operations != self._plan.operations:
            QMessageBox.warning(self, "Apply folder patch", "The target changed since preview. Review the plan again.")
            return
        backup = None
        if self.backup_check.isChecked():
            value = self.backup_path.text().strip()
            if not value:
                QMessageBox.warning(self, "Apply patch", "Choose a backup directory outside the target folder.")
                return
            backup = Path(value)
        choice = QMessageBox.question(
            self, "Apply folder patch", f"Apply {len(self._plan.operations)} reviewed file operation(s) in\n"
            f"{panel['path']}?\n\nCompleted operations are not rolled back if a later operation fails.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice != QMessageBox.StandardButton.Yes:
            return
        scopes = [SourceScope(panel["path"] / relative) for operation in self._plan.operations
                  for relative in (operation.source_relative, operation.target_relative) if relative]
        if backup is not None:
            scopes.append(SourceScope(backup, folder=True))
        reservation = self.app.shared_sources.begin(scopes, self.app, "Apply folder patch", self)
        if reservation is None:
            return
        if reservation.saved:
            reservation.finish(notify=False)
            self._update_preview()
            QMessageBox.information(self, "Review changed patch plan",
                                    "Open edits were saved. Review the patch again before applying it.")
            return
        try:
            outcomes = apply_folder_plan(panel["path"], self._plan, backup)
        except (OSError, PatchError, SyncError) as exc:
            QMessageBox.warning(self, "Apply folder patch", f"Patch stopped:\n{exc}")
            self.app._folder_job_key = None
            self.app._folder_cache = {}
            self.app._folder_stale = True
            self.app._refresh_diff()
            return
        finally:
            reservation.finish()
        self.app._folder_job_key = None
        self.app._folder_cache = {}
        self.app._folder_stale = True
        self.app._refresh_diff()
        self.app.status.setText(f"Applied {len(outcomes)} patch operation(s) · refresh folder results if needed")
        self.accept()

    def _apply_editor(self, index: int, result: str):
        panel = self.app.panels[index]
        editor = panel["editor"]
        editor.replace_logical_text(result)
