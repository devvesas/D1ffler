"""Accepted sources survive cwd changes; older ambiguous sources require a choice."""
import os
import hashlib
import tempfile
import time
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox, QTreeWidgetItem

from d1ffler.app_settings import SettingsState, read_app_settings, save_app_settings
from d1ffler.comparison_tabs import ComparisonWindow, command_line_source_sets
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.folder_restore import build_restore_plan, execute_restore
from d1ffler.folder_sync import build_items, execute_plan, operation_for
from d1ffler.local_data import LocalDataStore
from d1ffler.recovery_dialog import RecoveryDialog
from d1ffler.source_paths import absolute_source


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class SourcePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).absolute()
        self.project_a, self.project_b = self.root / "A", self.root / "B"
        for project in (self.project_a, self.project_b):
            project.mkdir()
            for name in ("left.txt", "right.txt", "third.txt", "fourth.txt"):
                (project / name).write_bytes(b"original\n")
            for name in ("left", "right"):
                (project / name / "nested").mkdir(parents=True)
                (project / name / "nested" / "item.txt").write_bytes(b"original\n")
        self.settings_path = self.root / "state" / "settings.json"
        self.windows = []
        self.addCleanup(self.cleanup_windows)

    def window(self, settings=None, *, tabs=False):
        settings = deepcopy(DEFAULT_SETTINGS) if settings is None else settings
        window = (ComparisonWindow(settings, self.settings_path, DiffApp) if tabs else
                  DiffApp(settings, self.settings_path))
        self.windows.append(window)
        return window

    def cleanup_windows(self):
        for window in self.windows:
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def recovery(self, *, folders=False):
        first = self.window()
        with working_directory(self.project_a):
            first.open_paths([Path("left" if folders else "left.txt"),
                              Path("right" if folders else "right.txt")])
            if folders:
                deadline = time.monotonic() + 5
                while first._folder_worker is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.005)
                self.assertIsNone(first._folder_worker)
                item = QTreeWidgetItem(["nested/item.txt"])
                item.setData(0, Qt.ItemDataRole.UserRole, "nested/item.txt")
                item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
                first._open_folder_item(first.panels[0]["widget"], item)
            first.panels[0]["editor"].setPlainText("unfinished A edits\n")
            snapshot = first._recovery_snapshot()
            self.assertTrue(first._write_recovery_snapshot())
            first._recovery_timer.stop()
            first._recovery_store.release()
        return first, snapshot

    def legacy_recovery(self):
        _first, snapshot = self.recovery()
        for record in snapshot["panels"]:
            for key in ("path", "editing_path"):
                if record.get(key):
                    record[key] = os.path.relpath(record[key], self.project_a)
        return snapshot

    def test_relative_cli_groups_are_anchored_before_independent_tabs_open(self):
        with working_directory(self.project_a):
            groups = command_line_source_sets(["left.txt", "right.txt", "third.txt", "fourth.txt",
                                               "--new-tab", "left.txt", "right.txt"])
        with working_directory(self.project_b):
            host = self.window(tabs=True)
            for index, sources in enumerate(groups):
                host.open_paths(sources, new_tab=index > 0)
            self.assertEqual(len(host.comparisons), 2)
            for view, sources in zip(host.comparisons, groups):
                self.assertEqual([panel["path"] for panel in view.panels], sources)
                self.assertTrue(all(path.is_absolute() and path.parent == self.project_a for path in sources))
                view.panels[0]["editor"].setPlainText("tab edits")
                self.assertTrue(view._write_recovery_snapshot())
                for record in view._recovery_snapshot()["panels"]:
                    self.assertTrue(Path(record["path"]).is_absolute())
                    self.assertTrue(Path(record["editing_path"]).is_absolute())
            recent = read_app_settings(self.settings_path)["recent_workspaces"]
            self.assertTrue({2, 4} <= {len(item["panels"]) for item in recent})
            self.assertTrue(all(Path(record["path"]).parent == self.project_a
                                for item in recent for record in item["panels"]))

    def test_recent_named_workspace_and_recovery_keep_original_save_target(self):
        first, snapshot = self.recovery()
        first.panels[0]["name"].setText("My original left")
        first.panels[0]["custom_name"] = True
        with patch("d1ffler.workspace_mixin.QInputDialog.getText", return_value=("Original project", True)):
            first._save_named_workspace()
        saved = read_app_settings(self.settings_path)
        with working_directory(self.project_b):
            reopened = self.window()
            reopened._load_workspace(saved["pinned_workspaces"][0])
            self.assertEqual(reopened.panels[0]["path"], self.project_a / "left.txt")
            self.assertEqual(reopened.panels[0]["name"].text(), "My original left")
            recent = self.window()
            recent._load_workspace(saved["recent_workspaces"][0])
            self.assertEqual(recent.panels[1]["path"], self.project_a / "right.txt")
            recovered = self.window()
            self.assertTrue(recovered._restore_recovery(snapshot))
            self.assertEqual(recovered.panels[0]["editing_path"], self.project_a / "left.txt")
            with patch("d1ffler.panel_mixin.QMessageBox.warning") as warning:
                self.assertTrue(recovered._save_panel_file(0))
            warning.assert_not_called()
        self.assertEqual((self.project_a / "left.txt").read_bytes(), b"unfinished A edits\n")
        self.assertEqual((self.project_b / "left.txt").read_bytes(), b"original\n")

    def test_missing_original_is_not_replaced_by_same_named_file_and_save_as_is_anchored(self):
        _first, snapshot = self.recovery()
        (self.project_a / "left.txt").unlink()
        with working_directory(self.project_b):
            recovered = self.window()
            self.assertTrue(recovered._restore_recovery(snapshot))
            self.assertEqual(recovered.panels[0]["path"], self.project_a / "left.txt")
            self.assertTrue(recovered.panels[0]["disk_changed"])
            with patch("d1ffler.panel_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
                self.assertFalse(recovered._save_panel_file(0))
            with patch("d1ffler.panel_mixin.QFileDialog.getSaveFileName", return_value=("recovered-copy.txt", "")):
                self.assertTrue(recovered._save_panel_file(0, save_as=True))
            self.assertEqual(recovered.panels[0]["editing_path"], self.project_b / "recovered-copy.txt")
        self.assertFalse((self.project_a / "left.txt").exists())
        self.assertEqual((self.project_b / "left.txt").read_bytes(), b"original\n")
        self.assertEqual((self.project_b / "recovered-copy.txt").read_bytes(), b"unfinished A edits\n")

    def test_folder_preview_recovery_saves_original_absolute_child(self):
        first, snapshot = self.recovery(folders=True)
        for record in snapshot["panels"]:
            self.assertTrue(Path(record["path"]).is_absolute())
            self.assertTrue(Path(record["editing_path"]).is_absolute())
        workspace = first._workspace_snapshot("Folder preview")
        with working_directory(self.project_b):
            reopened = self.window()
            reopened._load_workspace(workspace)
            self.assertEqual(reopened.panels[0]["path"], self.project_a / "left")
            recovered = self.window()
            self.assertTrue(recovered._restore_recovery(snapshot))
            self.assertEqual(recovered.panels[0]["editing_path"], self.project_a / "left/nested/item.txt")
            self.assertTrue(recovered._save_panel_file(0))
        self.assertEqual((self.project_a / "left/nested/item.txt").read_bytes(), b"unfinished A edits\n")
        self.assertEqual((self.project_b / "left/nested/item.txt").read_bytes(), b"original\n")

    def test_legacy_workspace_requires_original_folder_before_creating_a_tab(self):
        legacy = {"name": "Legacy", "panels": [{"path": "left.txt"}, {"path": "right.txt"}]}
        with working_directory(self.project_b):
            host = self.window(tabs=True)
            with patch("d1ffler.workspace_mixin.QFileDialog.getExistingDirectory", return_value=""):
                self.assertIsNone(host.open_workspace(legacy))
            self.assertEqual(len(host.comparisons), 1)
            self.assertIsNone(host.active_comparison.panels[0]["path"])
            with patch("d1ffler.workspace_mixin.QFileDialog.getExistingDirectory", return_value=str(self.project_a)) as locate:
                view = host.open_workspace(legacy)
            locate.assert_called_once()
            self.assertEqual(view.panels[0]["path"], self.project_a / "left.txt")
            self.assertEqual(view._workspace_snapshot("Located")["panels"][1]["path"], str(self.project_a / "right.txt"))
        self.assertEqual(legacy["panels"][0]["path"], "left.txt")

    def test_legacy_recovery_inspector_does_not_read_new_working_directory(self):
        snapshot = self.legacy_recovery()
        with working_directory(self.project_b), patch("d1ffler.recovery_dialog.disk_fingerprint") as fingerprint, \
                patch("d1ffler.recovery_dialog.inspect_file_content") as inspect:
            dialog = RecoveryDialog(snapshot)
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.tree.topLevelItem(0).text(2), "Original folder unknown")
            self.assertIn("original working folder", dialog.current.toPlainText())
            fingerprint.assert_not_called()
            inspect.assert_not_called()

    def test_legacy_recovery_as_text_survives_restart_and_requires_explicit_save_path(self):
        snapshot = self.legacy_recovery()
        with working_directory(self.project_b):
            recovered = self.window()
            with patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.No):
                self.assertTrue(recovered._restore_recovery(snapshot))
            panel = recovered.panels[0]
            self.assertEqual(panel["kind"], "text")
            self.assertIsNone(panel["path"])
            self.assertIsNone(panel["editing_path"])
            self.assertTrue(panel["recovery_pending"])
            self.assertEqual(panel["unresolved_source"]["editing_path"], "left.txt")
            self.assertTrue(recovered._write_recovery_snapshot())
            unbound = recovered._recovery_snapshot()
            again = self.window()
            with patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.No):
                self.assertTrue(again._restore_recovery(unbound))
            with patch("d1ffler.panel_mixin.QFileDialog.getSaveFileName", return_value=("", "")):
                self.assertFalse(again._save_panel_file(0))
            self.assertEqual(again._panel_text(again.panels[0]), "unfinished A edits\n")
            with patch("d1ffler.panel_mixin.QFileDialog.getSaveFileName", return_value=("chosen-copy.txt", "")):
                self.assertTrue(again._save_panel_file(0))
            self.assertEqual(again.panels[0]["editing_path"], self.project_b / "chosen-copy.txt")
            self.assertNotIn("unresolved_source", again.panels[0])
        self.assertEqual((self.project_a / "left.txt").read_bytes(), b"original\n")
        self.assertEqual((self.project_b / "left.txt").read_bytes(), b"original\n")

    def test_legacy_recovery_locate_uses_selected_base_and_preserves_edits_on_cancel(self):
        snapshot = self.legacy_recovery()
        with working_directory(self.project_b):
            recovered = self.window()
            recovered._load_panel(0, "file", self.project_b / "right.txt")
            with patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
                self.assertFalse(recovered._restore_recovery(snapshot))
            with patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes), \
                    patch("d1ffler.workspace_mixin.QFileDialog.getExistingDirectory", return_value=""):
                self.assertFalse(recovered._restore_recovery(snapshot))
            self.assertEqual(recovered.panels[0]["path"], self.project_b / "right.txt")
            with patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes), \
                    patch("d1ffler.workspace_mixin.QFileDialog.getExistingDirectory", return_value=str(self.project_a)):
                self.assertTrue(recovered._restore_recovery(snapshot))
            self.assertTrue(recovered._save_panel_file(0))
        self.assertEqual((self.project_a / "left.txt").read_bytes(), b"unfinished A edits\n")
        self.assertEqual((self.project_b / "left.txt").read_bytes(), b"original\n")
        self.assertEqual(snapshot["panels"][0]["editing_path"], "left.txt")

    def test_relative_git_identity_is_anchored_and_legacy_worktrees_cannot_be_removed(self):
        with working_directory(self.project_a):
            view = self.window()
            view._load_panel(0, "folder", Path("left"))
            source = {"repo": "left", "kind": "branch", "name": "main", "commit": "a" * 40,
                      "readonly": False, "managed": False}
            with patch("d1ffler.git_source_mixin.is_dirty", return_value=False):
                view._set_git_source(view.panels[0], source)
            self.assertEqual(view._workspace_sources()[0]["git"]["repo"], str(self.project_a / "left"))
            self.assertEqual(source["repo"], "left")
        legacy = {**deepcopy(DEFAULT_SETTINGS), "managed_worktrees": [{"path": "left", "repo": "right"}]}
        save_app_settings(SettingsState(legacy, baseline=DEFAULT_SETTINGS), self.settings_path)
        with working_directory(self.project_b):
            old = self.window(read_app_settings(self.settings_path))
            self.assertEqual(old.managed_worktrees, {})
            with patch("d1ffler.git_source_mixin.remove_worktree") as remove, patch("d1ffler.git_source_mixin.QInputDialog.getItem") as choose:
                old._manage_worktrees()
            remove.assert_not_called()
            choose.assert_not_called()
            old._persist_managed_worktrees()
            self.assertEqual(old.settings["managed_worktrees"], legacy["managed_worktrees"])

    def test_relative_sync_and_backup_report_restore_original_project_after_restart(self):
        with working_directory(self.project_a):
            (self.project_a / "left/nested/item.txt").write_bytes(b"new\n")
            item = build_items(Path("left"), Path("right"),
                               {"nested/item.txt": Path("left/nested/item.txt")},
                               {"nested/item.txt": Path("right/nested/item.txt")})[0]
            operation = operation_for(item, "left_to_right", Path("left"), Path("right"))
            report = execute_plan([operation], Path("left"), Path("right"), backup_directory=Path("backups")).to_dict()
            self.assertEqual(report["outcomes"][0]["status"], "completed")
            self.assertEqual(report["left"], str(self.project_a / "left"))
            self.assertEqual(report["backup_directory"], str(self.project_a / "backups"))
            self.assertTrue(Path(report["outcomes"][0]["backup_path"]).is_absolute())
        with working_directory(self.project_b):
            self.assertEqual(operation.target_root, self.project_a / "right")
            restored = execute_restore(build_restore_plan(report), report)
            self.assertEqual(restored.outcomes[0]["status"], "completed")
        self.assertEqual((self.project_a / "right/nested/item.txt").read_bytes(), b"original\n")
        self.assertEqual((self.project_b / "right/nested/item.txt").read_bytes(), b"original\n")

    def test_unresolved_recovery_sources_block_report_cleanup(self):
        _first, snapshot = self.recovery()
        store = LocalDataStore(self.settings_path)
        payload = b'{"outcomes": []}\n'
        reference = {"sha256": hashlib.sha256(payload).hexdigest()}
        store.reports.mkdir(exist_ok=True)
        (store.reports / (reference["sha256"] + ".json")).write_bytes(payload)
        preview = store.preview_reports()
        self.assertEqual(len(preview), 1)
        for key in ("path", "editing_path"):
            snapshot["panels"][0][key] = "left.txt"
        recovered = self.window()
        with patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.No):
            self.assertTrue(recovered._restore_recovery(snapshot))
        self.assertTrue(recovered._write_recovery_snapshot())
        with working_directory(self.project_b), self.assertRaisesRegex(OSError, "unresolved relative sources"):
            store.cleanup_reports(preview)
        self.assertTrue((self.settings_path.parent / "D1ffler-reports" / (reference["sha256"] + ".json")).exists())

    def test_absolute_source_keeps_alias_spelling_for_existing_protection(self):
        with working_directory(self.project_a):
            self.assertEqual(absolute_source("left/nested/../nested/item.txt"),
                             self.project_a / "left/nested/../nested/item.txt")
