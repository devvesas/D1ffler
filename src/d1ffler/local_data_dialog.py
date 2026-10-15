"""Inspect local storage and explicitly remove selected owned history."""
from copy import deepcopy
from datetime import datetime
import json

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from d1ffler.diagnostics_dialog import DiagnosticsDialog
from d1ffler.local_data import LocalDataStore
from d1ffler.recovery import RecoveryError
from d1ffler.recovery_dialog import RecoveryDialog


def size_label(size):
    return f"{size / (1024 * 1024):.2f} MiB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KiB"


class LocalDataDialog(QDialog):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.store = LocalDataStore(view.settings_path)
        self.setWindowTitle("Local history and storage")
        self.resize(1000, 680)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        notice = QLabel("History removal deletes saved setup, rules, notes and report references. Recovery discard deletes "
                        "the selected recovered text. Report cleanup removes only unreferenced local report copies. "
                        "Sources, Git worktrees, sync backups, exports and open edits are preserved.")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Local data", "Size", "Modified / state", "Location"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 210)
        self.tree.itemDoubleClicked.connect(lambda _item, _column: self._inspect())
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        for title, callback in (("Inspect / open…", self._inspect), ("Remove selected history…", self._remove_history),
                                ("Discard selected recovery…", self._discard_recovery),
                                ("Clear diagnostics…", self._clear_diagnostics), ("Refresh", self.refresh)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        retention = QHBoxLayout()
        self.enabled = QCheckBox("On startup, remove unreferenced reports; minimum age:")
        self.days = QSpinBox()
        self.days.setRange(0, 3650)
        self.days.setSpecialValueText("Any age")
        self.days.setSuffix(" days")
        self.days.setValue(30)
        retention.addWidget(self.enabled)
        retention.addWidget(self.days)
        for title, callback in (("Save retention rule", self._save_rule), ("Preview report cleanup…", self._preview_cleanup)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            retention.addWidget(button)
        layout.addLayout(retention)
        layout.addWidget(QLabel("Cleanup previews use this age threshold. Referenced reports always stay; unreadable state blocks cleanup."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        try:
            rule = self.store.policy()
            self.enabled.setChecked(rule["enabled"])
            self.days.setValue(rule["days"])
        except OSError as exc:
            self._error(exc)
        self.refresh()

    def _error(self, exc):
        QMessageBox.warning(self, "Local history and storage", str(exc))

    def refresh(self):
        self.tree.clear()
        try:
            entries, errors = self.store.inventory()
        except OSError as exc:
            self.summary.setText(f"Local state: {self.store.root}\nStorage could not be read: {exc}")
            return
        total = sum(entry.size for entry in entries if entry.kind not in ("pinned_workspaces", "recent_workspaces"))
        self.summary.setText(f"Local state: {self.store.root}\nListed local files: {size_label(total)}. "
                             "Workspace sizes are estimates within the settings file." +
                             ("\n" + "\n".join(dict.fromkeys(errors)) if errors else ""))
        names = {"pinned_workspaces": "Saved workspace", "recent_workspaces": "Recent comparison",
                 "settings": "Settings / policy", "recovery": "Recovery", "report": "Retained report",
                 "diagnostics": "Diagnostics", "metadata": "Other local file — protected"}
        for entry in entries:
            title = names[entry.kind]
            if entry.kind in ("pinned_workspaces", "recent_workspaces"):
                title += f": {entry.data.get('name') or 'Unnamed'}"
            elif entry.kind == "recovery" and isinstance(entry.data, dict):
                context = entry.data.get("context", {})
                if isinstance(context, dict) and context.get("name"):
                    title += f": {context['name']}"
            else:
                title += f": {entry.path.name}"
            modified = datetime.fromtimestamp(entry.modified).isoformat(timespec="seconds") if entry.modified else ""
            item = QTreeWidgetItem([title, size_label(entry.size), entry.status or modified, str(entry.path)])
            item.setToolTip(2, modified)
            item.setToolTip(0, title)
            item.setToolTip(3, str(entry.path))
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            self.tree.addTopLevelItem(item)

    def _selected(self):
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]

    def _confirm(self, title, text):
        return QMessageBox.question(self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes

    def _inspect(self):
        selected = self._selected()
        if len(selected) != 1:
            return
        entry = selected[0]
        if entry.kind in ("pinned_workspaces", "recent_workspaces"):
            host = self.view.comparison_host
            (host.open_workspace if host else self.view._load_workspace)(deepcopy(entry.data))
        elif entry.kind == "recovery" and entry.status == "Abandoned":
            snapshot = entry.data
            if (not isinstance(snapshot, dict) or snapshot.get("version") not in (1, 2) or
                    not isinstance(snapshot.get("panels"), list) or
                    any(not isinstance(panel, dict) for panel in snapshot["panels"])):
                self._error("This recovery cannot be inspected safely; its file was retained.")
                return
            dialog = RecoveryDialog(snapshot, self)
            dialog.exec()
            if dialog.choice == "discard":
                if self._confirm("Discard recovery", "Permanently remove this recovered text? Source files and open edits stay unchanged."):
                    self._discard(entry)
            elif dialog.choice == "restore":
                self.restore_recovery(entry)
            self.refresh()
        elif entry.kind == "diagnostics":
            DiagnosticsDialog(self.view.diagnostics, self.view).exec()
        elif entry.kind == "report":
            from d1ffler.comparison_context import RetainedReports
            try:
                report = RetainedReports(self.view.settings_path).load({"sha256": entry.path.stem}, pin=False)
            except OSError as exc:
                self._error(exc)
                return
            inspector = QDialog(self)
            inspector.setWindowTitle("Historical retained report")
            inspector.resize(800, 550)
            layout = QVBoxLayout(inspector)
            preview = QPlainTextEdit()
            preview.setReadOnly(True)
            preview.setPlainText(json.dumps(report, indent=2, ensure_ascii=False))
            layout.addWidget(preview)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(inspector.reject)
            layout.addWidget(buttons)
            inspector.exec()

    def restore_recovery(self, entry):
        host = self.view.comparison_host
        view = host.new_comparison() if host else self.view
        if view is None:
            return False
        if not host and any(panel.get("dirty") or panel.get("recovery_pending") or
                            (panel["kind"] == "text" and view._panel_text(panel)) for panel in view.panels):
            self._error("Resolve the open edits with Save/Discard/Cancel before restoring recovery.")
            return False
        try:
            view._recovery_store.adopt(entry.path, expected=entry.data)
            if not view._restore_recovery(entry.data):
                view._recovery_store.release(new_session=True)
                raise RecoveryError("Recovery could not be restored. Its snapshot was retained.")
            if not view._write_recovery_snapshot():
                self._error("Restored buffers remain open, but their updated recovery could not be saved. Save or copy the edits.")
            if host:
                host.update_tab(view)
            return True
        except (RecoveryError, OSError) as exc:
            self._error(exc)
            return False

    def _remove_history(self):
        entries = self._selected()
        if not entries or any(entry.kind not in ("pinned_workspaces", "recent_workspaces") for entry in entries):
            self._error("Select only the saved workspaces or recent comparisons to remove.")
            return
        if not self._confirm("Remove selected history", f"Remove {len(entries)} saved/recent comparison(s), including their rules, notes "
                             "and report references? Reports stay until a separate unreferenced-report cleanup. "
                             "Open comparisons, recovery text, sources and backups are preserved."):
            return
        try:
            latest = self.store.remove_history([(entry.kind, entry.data) for entry in entries])
            host = self.view.comparison_host
            views = host.comparisons if host else [self.view]
            states = [host.settings] if host else []
            states.extend(view.settings for view in views)
            for state in states:
                for key in ("pinned_workspaces", "recent_workspaces"):
                    state[key] = deepcopy(latest[key])
                    state.baseline[key] = deepcopy(latest[key])
            for view in views:
                for entry in entries:
                    if entry.kind == "pinned_workspaces":
                        view._named_workspace_baselines.pop(entry.data.get("name"), None)
        except OSError as exc:
            self._error(exc)
        self.refresh()

    def _discard(self, entry):
        try:
            self.view._recovery_store.discard(entry.path, expected=entry.data)
            return True
        except (RecoveryError, OSError) as exc:
            self._error(exc)
            return False

    def _discard_recovery(self):
        entries = self._selected()
        if not entries or any(entry.kind != "recovery" or entry.status != "Abandoned" for entry in entries):
            self._error("Select abandoned recovery snapshots. Live recovery and migration metadata are protected.")
            return
        if self._confirm("Discard selected recovery", f"Permanently delete recovered text in {len(entries)} abandoned snapshot(s)? "
                         "Their report references are removed. Sources, backups and open edits stay unchanged."):
            for entry in entries:
                if not self._discard(entry):
                    break
            self.refresh()

    def _clear_diagnostics(self):
        if self._confirm("Clear diagnostics", "Delete the local diagnostic log and its two rotations? Existing exports and open edits stay unchanged."):
            try:
                self.view.diagnostics.clear()
            except OSError as exc:
                self._error(f"Clearing stopped; remaining logs were retained: {exc}")
            self.refresh()

    def _save_rule(self):
        try:
            self.store.save_policy(self.enabled.isChecked(), self.days.value())
            self.view.status.setText("Local retention rule saved; it runs on startup. Use Preview report cleanup to review files now.")
        except OSError as exc:
            self._error(exc)
        self.refresh()

    def _preview_cleanup(self):
        try:
            preview = self.store.preview_reports(self.days.value())
        except OSError as exc:
            self._error(exc)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Preview unreferenced report cleanup")
        dialog.resize(800, 480)
        layout = QVBoxLayout(dialog)
        label = QLabel(f"{len(preview)} unreferenced report(s), {size_label(sum(item.size for item in preview))}, "
                       f"older than {self.days.value()} days. Uncheck files to keep them. References are checked again before deletion.")
        label.setWordWrap(True)
        layout.addWidget(label)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Local report copy", "Size"])
        for entry in preview:
            item = QTreeWidgetItem([str(entry.path), size_label(entry.size)])
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            tree.addTopLevelItem(item)
        layout.addWidget(tree)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        remove = buttons.addButton("Delete checked report copies", QDialogButtonBox.ButtonRole.AcceptRole)
        remove.setEnabled(bool(preview))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = [tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) for index in range(tree.topLevelItemCount())
                        if tree.topLevelItem(index).checkState(0) == Qt.CheckState.Checked]
            try:
                removed = self.store.cleanup_reports(selected)
                self.view.status.setText(f"Removed {len(removed)} unreferenced report copy/copies; newly referenced files were retained.")
            except OSError as exc:
                self._error(exc)
        self.refresh()
