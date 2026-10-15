"""Focused GUI checks for reviewed folder synchronization."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton, QTextEdit

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp, RestorePlanDialog, SyncPlanDialog, SyncReportDialog
from d1ffler.folder_sync import build_items, execute_plan, operation_for


class SyncPlanDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.left, self.right = self.root / "left", self.root / "right"
        self.left.mkdir()
        self.right.mkdir()
        (self.left / "a.txt").write_text("source", encoding="utf-8")
        (self.left / "nested").mkdir()
        (self.left / "nested/b.txt").write_text("nested", encoding="utf-8")
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window)
        self.window._load_panel(0, "folder", self.left)
        self.window._load_panel(1, "folder", self.right)

    def cleanup_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def make_dialog(self, *args):
        dialog = SyncPlanDialog(self.window, *args)
        self.addCleanup(dialog.reject)
        deadline = time.monotonic() + 5
        while not dialog.apply_plan_button.isEnabled() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(dialog.apply_plan_button.isEnabled(), dialog.summary.text())
        return dialog

    def test_selected_file_is_reviewed_before_copy_and_report_is_saved(self):
        dialog = self.make_dialog(0, 1, "a.txt")
        self.assertEqual(dialog.row_combos["a.txt"].currentData(), "left_to_right")
        self.assertEqual(dialog.row_combos["nested/b.txt"].currentData(), "skip")
        self.assertFalse((self.right / "a.txt").exists())
        with patch("d1ffler.diff_tool.SyncReportDialog.exec", return_value=0):
            dialog._run(True)
        self.assertFalse((self.right / "a.txt").exists())
        with patch("d1ffler.diff_tool.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch(
            "d1ffler.diff_tool.SyncReportDialog.exec", return_value=0
        ):
            dialog._run(False)
        self.assertEqual((self.right / "a.txt").read_text(encoding="utf-8"), "source")
        self.assertFalse((self.right / "nested/b.txt").exists())
        snapshot = self.window._workspace_snapshot("Saved")
        from d1ffler.comparison_context import RetainedReports
        retained = RetainedReports(self.window.settings_path).load(snapshot["report_refs"]["sync"][-1])
        self.assertEqual(retained["outcomes"][0]["status"], "completed")
        self.window._load_workspace(snapshot)
        self.assertEqual(self.window.sync_reports[-1]["plan"][0]["action"], "left_to_right")

    def test_cancelling_confirmation_does_not_write(self):
        dialog = self.make_dialog(0, 1, "a.txt")
        with patch("d1ffler.diff_tool.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel):
            dialog._run(False)
        self.assertFalse((self.right / "a.txt").exists())
        self.assertEqual(self.window.sync_reports, [])

    def test_subtree_action_and_read_only_target(self):
        dialog = self.make_dialog(0, 1)
        scope = dialog.scope_combo.findData("nested")
        dialog.scope_combo.setCurrentIndex(scope)
        dialog.scope_action.setCurrentIndex(dialog.scope_action.findData("left_to_right"))
        dialog._apply_scope()
        self.assertEqual(dialog.row_combos["nested/b.txt"].currentData(), "left_to_right")
        self.assertEqual(dialog.row_combos["a.txt"].currentData(), "skip")

        self.window._set_git_source(self.window.panels[1], {
            "repo": str(self.right), "kind": "commit", "name": "test", "commit": "0" * 40,
            "readonly": True, "managed": False,
        })
        read_only_dialog = self.make_dialog(0, 1, "a.txt")
        self.assertEqual(read_only_dialog.row_combos["a.txt"].findData("left_to_right"), -1)

    def test_three_panel_comparison_can_choose_any_pair(self):
        third = self.root / "third"
        third.mkdir()
        (third / "third.txt").write_text("third", encoding="utf-8")
        self.window._add_panel()
        self.window._load_panel(2, "folder", third)
        dialog = self.make_dialog(2, 0, "third.txt")
        self.assertEqual(dialog.left_combo.currentData(), 2)
        self.assertEqual(dialog.right_combo.currentData(), 0)
        self.assertEqual(dialog.row_combos["third.txt"].currentData(), "left_to_right")
        self.assertFalse((self.left / "third.txt").exists())

    def test_metadata_only_requires_explicit_choice_and_reports_preview(self):
        source, target = self.left / "a.txt", self.right / "a.txt"
        target.write_bytes(source.read_bytes())
        os.utime(source, ns=(1_600_000_000_000_000_000,) * 2)
        os.utime(target, ns=(1_700_000_000_000_000_000,) * 2)
        self.window.metadata_compare_rule = True
        dialog = self.make_dialog(0, 1, "a.txt")
        combo = dialog.row_combos["a.txt"]
        self.assertEqual(combo.currentData(), "skip")
        self.assertEqual(combo.findData("left_to_right"), -1)
        row = dialog._plan_tree_rows["a.txt"]
        self.assertEqual(row.text(1), "Metadata only")
        combo.setCurrentIndex(combo.findData("mtime_left_to_right"))
        self.assertEqual(row.text(2), "Metadata")
        self.assertIn(str(target.stat().st_mtime_ns), row.text(4))
        self.assertIn(str(source.stat().st_mtime_ns), row.text(4))
        self.assertIn("1 metadata changes", dialog.summary.text())
        with patch("d1ffler.diff_tool.SyncReportDialog.exec", return_value=0):
            dialog._run(True)
        report = self.window.sync_reports[-1]
        self.assertEqual(report["plan"][0]["metadata_changes"][0]["field"], "modified_ns")
        self.assertEqual(report["outcomes"][0]["status"], "dry run")
        report_dialog = SyncReportDialog(report, self.window)
        self.addCleanup(report_dialog.reject)
        self.assertIn("Modified time:", report_dialog.findChild(QTextEdit).toPlainText())
        self.assertEqual(target.stat().st_mtime_ns, 1_700_000_000_000_000_000)
        self.window._load_workspace(self.window._workspace_snapshot("Saved"))
        self.assertEqual(self.window.sync_reports[-1]["outcomes"][0]["metadata_changes"],
                         report["outcomes"][0]["metadata_changes"])

    def test_metadata_scope_and_unavailable_read_only_and_missing_targets(self):
        target = self.right / "nested/b.txt"
        target.parent.mkdir()
        source = self.left / "nested/b.txt"
        target.write_bytes(source.read_bytes())
        os.utime(source, ns=(1_600_000_000_000_000_000,) * 2)
        os.utime(target, ns=(1_700_000_000_000_000_000,) * 2)
        dialog = self.make_dialog(0, 1)
        dialog.scope_combo.setCurrentIndex(dialog.scope_combo.findData("nested"))
        dialog.scope_action.setCurrentIndex(dialog.scope_action.findData("mtime_left_to_right"))
        dialog._apply_scope()
        self.assertEqual(dialog.row_combos["nested/b.txt"].currentData(), "mtime_left_to_right")
        self.assertEqual(dialog.row_combos["a.txt"].currentData(), "skip")
        missing_combo = dialog.row_combos["a.txt"]
        index = missing_combo.findData("mtime_left_to_right")
        self.assertFalse(missing_combo.model().item(index).isEnabled())
        self.assertIn("existing file", missing_combo.itemData(index, Qt.ItemDataRole.ToolTipRole))
        self.window._set_git_source(self.window.panels[1], {
            "repo": str(self.right), "kind": "commit", "name": "test", "commit": "0" * 40,
            "readonly": True, "managed": False,
        })
        read_only = self.make_dialog(0, 1)
        combo = read_only.row_combos["nested/b.txt"]
        index = combo.findData("mtime_left_to_right")
        self.assertFalse(combo.model().item(index).isEnabled())
        self.assertIn("Read-only", combo.itemData(index, Qt.ItemDataRole.ToolTipRole))
        read_only.scope_action.setCurrentIndex(read_only.scope_action.findData("mtime_left_to_right"))
        read_only._apply_scope()
        self.assertEqual(combo.currentData(), "skip")

    def test_review_restore_from_sync_report_and_persist_restore_report(self):
        (self.right / "a.txt").write_text("original", encoding="utf-8")
        item = build_items(self.left, self.right,
                           {"a.txt": self.left / "a.txt"},
                           {"a.txt": self.right / "a.txt"})[0]
        operation = operation_for(item, "left_to_right", self.left, self.right)
        backups = self.root / "backups"
        report = execute_plan([operation], self.left, self.right, backup_directory=backups).to_dict()
        self.window.sync_reports.append(report)
        report_dialog = SyncReportDialog(report, self.window, app=self.window)
        self.addCleanup(report_dialog.reject)
        self.assertTrue(any(button.text() == "Review restore…"
                            for button in report_dialog.findChildren(QPushButton)))
        dialog = RestorePlanDialog(report, self.window)
        self.addCleanup(dialog.reject)
        deadline = time.monotonic() + 5
        while dialog._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(dialog._worker)
        self.assertEqual(dialog.items[0].status, "ready")
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        self.assertTrue(dialog.inspect_button.isEnabled())
        previews = []

        def inspect(preview):
            previews.extend(editor.toPlainText() for editor in preview.findChildren(QTextEdit))
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, "exec", inspect):
            dialog._inspect_selected()
        self.assertIn("source", previews)
        self.assertIn("original", previews)
        dialog._select_ready()
        self.assertTrue(dialog.apply_button.isEnabled())
        with patch("d1ffler.diff_tool.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch(
            "d1ffler.diff_tool.RestoreReportDialog.exec", return_value=0
        ):
            dialog._run(False)
        self.assertEqual((self.right / "a.txt").read_text(encoding="utf-8"), "original")
        self.assertEqual((backups / "right/a.txt").read_text(encoding="utf-8"), "original")
        self.assertEqual(self.window.sync_reports[0]["outcomes"][0]["status"], "completed")
        self.assertEqual(self.window.restore_reports[-1]["outcomes"][0]["status"], "completed")
        from d1ffler.comparison_context import RetainedReports
        snapshot = self.window._workspace_snapshot("Saved")
        retained = RetainedReports(self.window.settings_path).load(snapshot["report_refs"]["restore"][-1])
        self.assertEqual(retained["outcomes"][0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
