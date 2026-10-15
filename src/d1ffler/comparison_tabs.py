"""Independent comparison views with bounded shared background work."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from PyQt6.QtCore import QEvent, QTimer, Qt
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton, QTabWidget

from d1ffler.app_assets import logo_asset_path
from d1ffler.background_tasks import BackgroundTask
from d1ffler.diagnostics import DiagnosticStore
from d1ffler.app_settings import SettingsState, save_app_settings, settings_for_save
from d1ffler.recovery import RecoveryError
from d1ffler.recovery_dialog import RecoveryDialog
from d1ffler.shared_sources import SharedSources
from d1ffler.source_paths import absolute_source, absolute_worktrees
from d1ffler.ui_theme import apply_theme


MAX_COMPARISON_TABS = 8
MAX_COMPARISON_WORKERS = 2
SHARED_SETTINGS = ("pinned_workspaces", "recent_workspaces", "managed_worktrees")


class ComparisonWindow(QMainWindow):
    def __init__(self, settings, settings_path, comparison_type):
        super().__init__()
        settings_path = absolute_source(settings_path)
        self.settings = deepcopy(settings) if isinstance(settings, SettingsState) else SettingsState(settings)
        self.settings_path = settings_path
        self.diagnostics = DiagnosticStore(settings_path.parent / "diagnostics")
        self.comparison_type = comparison_type
        self.shared_sources = SharedSources(self)
        self.worker_pool = ThreadPoolExecutor(max_workers=MAX_COMPARISON_WORKERS,
                                             thread_name_prefix="D1ffler-comparison")
        self._closing = False
        self.setWindowTitle("D1ffler — File & Folder Comparison")
        self.setWindowIcon(QIcon(str(logo_asset_path())))
        self.resize(1280, 820)
        self.setMinimumSize(940, 650)
        self.comparison_tabs = QTabWidget()
        self.comparison_tabs.setTabsClosable(True)
        self.comparison_tabs.setMovable(True)
        self.comparison_tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.comparison_tabs.tabCloseRequested.connect(self.close_comparison)
        self.comparison_tabs.currentChanged.connect(self._comparison_changed)
        self.setCentralWidget(self.comparison_tabs)
        self.new_comparison_button = QPushButton("+ New comparison")
        self.new_comparison_button.setToolTip("Open an empty comparison tab (Ctrl+T); up to eight tabs")
        self.new_comparison_button.clicked.connect(lambda: self.new_comparison())
        self.comparison_tabs.setCornerWidget(self.new_comparison_button)
        for sequence, callback in (("Ctrl+T", self.new_comparison),
                                   ("Ctrl+W", lambda: self.close_comparison(self.comparison_tabs.currentIndex()))):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
        self.new_comparison()
        QTimer.singleShot(0, self._apply_local_retention)

    def _apply_local_retention(self):
        if self._closing:
            return
        from d1ffler.local_data import LocalDataStore
        try:
            removed = LocalDataStore(self.settings_path).apply_retention()
            if removed:
                self.active_comparison.status.setText(f"Local retention removed {len(removed)} unreferenced report copy/copies.")
        except OSError as exc:
            self.active_comparison.status.setText(f"Local report cleanup stopped: {exc}")

    def report_gui_failure(self, failure, owner=None):
        if self._closing:
            return
        if owner is not None:
            if owner in self.comparisons and not owner._closed:
                owner._handle_gui_failure(failure)
            return
        # A callback without an identifiable view must not mark another tab failed.
        backed_up = True
        for view in self.comparisons:
            try:
                backed_up = view._write_recovery_snapshot() and backed_up
            except Exception as exc:
                self.diagnostics.record_exception("Recovery after GUI failure", exc)
                backed_up = False
        recovery_notice = ("" if backed_up else
                           "\nSome unsaved edits could not be backed up; save or copy them before closing.")
        notice = QMessageBox(QMessageBox.Icon.Warning, "Unexpected application error",
                             f"An action failed. Open Diagnostics for details.\n{failure.summary}\n"
                             "Open edits remain available; review the action's results before retrying." + recovery_notice,
                             QMessageBox.StandardButton.Ok, self)
        notice.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.show()

    @property
    def comparisons(self):
        return [self.comparison_tabs.widget(index) for index in range(self.comparison_tabs.count())]

    @property
    def active_comparison(self):
        return self.comparison_tabs.currentWidget()

    def new_comparison(self):
        if self.comparison_tabs.count() >= MAX_COMPARISON_TABS:
            QMessageBox.information(self, "Comparison tabs", "Up to eight comparisons can stay open. Close a tab first.")
            return None
        view = self.comparison_type(deepcopy(self.settings), self.settings_path, comparison_host=self)
        view.setWindowFlags(Qt.WindowType.Widget)
        if self.comparisons:
            view.managed_worktrees = self.comparisons[0].managed_worktrees
        view._recovery_store.protected_paths = lambda: {item._recovery_store.session_path for item in self.comparisons}
        view.comparison_changed.connect(lambda: self.update_tab(view))
        self.comparison_tabs.addTab(view, "New comparison")
        self.comparison_tabs.setCurrentWidget(view)
        self.update_tab(view)
        self.new_comparison_button.setEnabled(self.comparison_tabs.count() < MAX_COMPARISON_TABS)
        return view

    def update_tab(self, view):
        index = self.comparison_tabs.indexOf(view)
        if index < 0:
            return
        paths = [str(panel["path"]) for panel in view.panels if panel["path"] is not None]
        name = view._active_workspace_name or " ↔ ".join(Path(path).name or path for path in paths)
        if not name:
            name = "Text comparison" if any(panel["kind"] == "text" for panel in view.panels) else "New comparison"
        dirty = any(panel.get("dirty") or panel.get("recovery_pending") or
                    (panel["kind"] == "text" and bool(view._panel_text(panel))) for panel in view.panels)
        self.comparison_tabs.setTabText(index, name + (" *" if dirty else ""))
        self.comparison_tabs.setTabToolTip(index, name + ("\nUnsaved edits" if dirty else "") +
                                          ("\n" + "\n".join(paths) if paths else ""))
        if view is self.active_comparison:
            self.setWindowTitle(f"{name}{' *' if dirty else ''} — D1ffler")

    def _comparison_changed(self, _index):
        view = self.active_comparison
        if view is not None and not self._closing:
            # Widgets stay intact: switching never reloads or saves their buffers.
            self.update_tab(view)
            apply_theme(QApplication.instance(), view.settings["theme"], view.zoom_percent)
            view._disk_check_timer.start(100)

    def persist_settings(self, view, *, workspace_baseline=None):
        candidate = settings_for_save(view.settings, baseline=self.settings.baseline,
                                      workspace_baseline=workspace_baseline)
        save_app_settings(candidate, self.settings_path)
        self.settings = candidate
        for other in self.comparisons:
            for key in SHARED_SETTINGS:
                other.settings[key] = deepcopy(self.settings.get(key, []))
            other.settings.baseline = deepcopy(self.settings.baseline)
        view.managed_worktrees.clear()
        view.managed_worktrees.update(absolute_worktrees(self.settings["managed_worktrees"]))

    def open_workspace(self, workspace):
        # Validation happens before a new tab is allocated.
        workspace = self.active_comparison._resolve_workspace_sources(workspace)
        if workspace is None or not self.active_comparison._validate_workspace(workspace):
            return None
        view = self.new_comparison()
        if view is not None:
            view._load_workspace(deepcopy(workspace))
            self.update_tab(view)
        return view

    def open_paths(self, paths, *, new_tab=True):
        view = self.new_comparison() if new_tab else self.active_comparison
        if view is not None:
            while len(view.panels) < min(len(paths), 4):
                view._add_panel()
            view.open_paths(paths)
            self.update_tab(view)
        return view

    def _dispose(self, view):
        view._closed = True
        for timer in view.findChildren(QTimer):
            timer.stop()
        view._shutdown_background_jobs()
        view._recovery_store.release()
        from d1ffler.local_data import unregister_report_user
        unregister_report_user(view.settings_path.parent)
        QApplication.instance().removeEventFilter(view)
        watched = view._disk_watcher.files() + view._disk_watcher.directories()
        if watched:
            view._disk_watcher.removePaths(watched)
        self.comparison_tabs.removeTab(self.comparison_tabs.indexOf(view))
        view.deleteLater()

    def close_comparison(self, index):
        view = self.comparison_tabs.widget(index)
        if view is None:
            return False
        plan = view._prepare_close(tab_close=True)
        if plan is None:
            return False
        view._commit_close(plan)
        self._dispose(view)
        if not self._closing and not self.comparison_tabs.count():
            self.new_comparison()
        self.new_comparison_button.setEnabled(self.comparison_tabs.count() < MAX_COMPARISON_TABS)
        return True

    def offer_recovery(self):
        store = self.active_comparison._recovery_store
        restored = False
        for path, snapshot in store.snapshots():
            dialog = RecoveryDialog(snapshot, self)
            dialog.exec()
            if dialog.choice == "discard":
                try:
                    store.discard(path)
                except (RecoveryError, OSError) as exc:
                    QMessageBox.warning(self, "Discard recovery", str(exc))
                continue
            if dialog.choice != "restore":
                break
            view = self.new_comparison() if restored else self.active_comparison
            if view is None:
                break
            try:
                view._recovery_store.adopt(path, expected=snapshot)
            except RecoveryError as exc:
                QMessageBox.warning(self, "Restore recovery", str(exc))
                continue
            if view._restore_recovery(snapshot):
                view._write_recovery_snapshot()
                self.update_tab(view)
                restored = True
            else:
                view._recovery_store.release(new_session=True)
        return restored

    def event(self, event):
        if event.type() == QEvent.Type.WindowActivate and hasattr(self, "comparison_tabs"):
            view = self.active_comparison
            if view is not None:
                view._disk_check_timer.start(100)
        return super().event(event)

    def closeEvent(self, event):
        # Collect decisions without applying discards, so Cancel retains edits
        # and recovery even in tabs that already approved closing.
        plans = []
        for view in self.comparisons:
            self.comparison_tabs.setCurrentWidget(view)
            plan = view._prepare_close()
            if plan is None:
                event.ignore()
                return
            plans.append((view, plan))
        self._closing = True
        for view, plan in plans:
            view._commit_close(plan)
            self._dispose(view)
        self.worker_pool.shutdown(wait=True, cancel_futures=True)
        event.accept()


def command_line_source_sets(arguments):
    """--new-tab separates independent sets of one to four sources."""
    groups = [[]]
    for argument in arguments:
        if argument == "--new-tab":
            if groups[-1]:
                groups.append([])
        else:
            groups[-1].append(absolute_source(argument))
    return [group for group in groups if group]
