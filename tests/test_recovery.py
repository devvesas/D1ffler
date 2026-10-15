"""Recovery snapshots and startup restoration for unsaved comparison buffers."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QTreeWidgetItem

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp, RecoveryDialog
from d1ffler.recovery import MAX_SNAPSHOT_BYTES, RecoveryError, RecoveryStore


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings_path = self.root / "settings.json"
        self.windows = []
        self.addCleanup(self.cleanup_windows)

    def window(self):
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.settings_path)
        self.windows.append(window)
        return window

    def abandon(self, window):
        """Simulate process exit without saving or discarding its buffers."""
        window._recovery_timer.stop()
        window._recovery_store.release()

    def cleanup_windows(self):
        for window in self.windows:
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    @staticmethod
    def restore_dialog(dialog):
        dialog.choice = "restore"
        return QDialog.DialogCode.Accepted

    def test_dirty_file_restores_without_overwriting_changed_disk(self):
        source = self.root / "source.txt"
        source.write_text("original\n", encoding="utf-8")
        first = self.window()
        first._load_panel(0, "file", source)
        first.panels[0]["editor"].setPlainText("unsaved\n")
        self.assertTrue(first._write_recovery_snapshot())
        source.write_text("external\n", encoding="utf-8")
        self.abandon(first)

        store = RecoveryStore(self.settings_path)
        _path, snapshot = store.snapshots()[0]
        inspector = RecoveryDialog(snapshot)
        self.assertEqual(inspector.tree.topLevelItem(0).text(2), "Changed")
        self.assertIn("unsaved", inspector.recovered.toPlainText())
        self.assertIn("external", inspector.current.toPlainText())
        inspector.close()
        inspector.deleteLater()

        second = self.window()
        with patch.object(RecoveryDialog, "exec", self.restore_dialog):
            self.assertTrue(second._offer_recovery())
        panel = second.panels[0]
        self.assertEqual(second._panel_text(panel), "unsaved\n")
        self.assertTrue(panel["dirty"])
        self.assertTrue(panel["disk_changed"])
        self.assertEqual(source.read_text(encoding="utf-8"), "external\n")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(second._save_panel_file(0))
        self.assertEqual(source.read_text(encoding="utf-8"), "external\n")

    def test_pasted_text_and_edited_merge_result_restore(self):
        first = self.window()
        first.merge_mode_button.setChecked(True)
        first.panels[0]["editor"].setPlainText("base\n")
        first.panels[1]["editor"].setPlainText("local\n")
        first.panels[2]["editor"].setPlainText("incoming\n")
        first.panels[3]["editor"].setPlainText("my merged result\n")
        self.assertTrue(first._write_recovery_snapshot())
        self.abandon(first)
        second = self.window()
        with patch.object(RecoveryDialog, "exec", self.restore_dialog):
            self.assertTrue(second._offer_recovery())
        self.assertTrue(second.merge_mode)
        self.assertEqual([second._panel_text(panel) for panel in second.panels],
                         ["base\n", "local\n", "incoming\n", "my merged result\n"])
        self.assertTrue(second.merge_touched)
        self.assertTrue(second._recovery_store.snapshots(include_active=True))

    def test_folder_file_preview_restores_both_panels_and_dirty_side(self):
        left = self.root / "left"
        right = self.root / "right"
        left.mkdir()
        right.mkdir()
        (left / "sample.txt").write_text("left disk\n", encoding="utf-8")
        (right / "sample.txt").write_text("right disk\n", encoding="utf-8")
        first = self.window()
        first._load_panel(0, "folder", left)
        first._load_panel(1, "folder", right)
        item = QTreeWidgetItem(["sample.txt"])
        item.setData(0, Qt.ItemDataRole.UserRole, "sample.txt")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        first._open_folder_item(first.panels[0]["widget"], item)
        first.panels[0]["editor"].setPlainText("left unsaved\n")
        first._write_recovery_snapshot()
        self.abandon(first)

        second = self.window()
        with patch.object(RecoveryDialog, "exec", self.restore_dialog):
            self.assertTrue(second._offer_recovery())
        self.assertEqual(second._panel_text(second.panels[0]), "left unsaved\n")
        self.assertEqual(second._panel_text(second.panels[1]), "right disk\n")
        self.assertEqual(second.panels[0]["editing_path"], left / "sample.txt")
        self.assertTrue(second.panels[0]["dirty"])
        self.assertTrue(second.folder_back_button.isVisibleTo(second))

    def test_save_and_explicit_discard_remove_recovery_entry(self):
        source = self.root / "source.txt"
        source.write_text("original\n", encoding="utf-8")
        window = self.window()
        window._load_panel(0, "file", source)
        window.panels[0]["editor"].setPlainText("edit\n")
        window._write_recovery_snapshot()
        self.assertEqual(len(window._recovery_store.snapshots(include_active=True)), 1)
        self.assertTrue(window._save_panel_file(0))
        self.assertEqual(window._recovery_store.snapshots(), [])
        window.panels[0]["editor"].setPlainText("next edit\n")
        window._write_recovery_snapshot()
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            window._clear_panel(0)
        window._write_recovery_snapshot()
        self.assertEqual(window._recovery_store.snapshots(), [])

    def test_orderly_close_keeps_pasted_text_for_next_launch(self):
        window = self.window()
        window.panels[0]["editor"].setPlainText("pasted comparison\n")
        self.assertTrue(window.close())
        snapshots = window._recovery_store.snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0][1]["panels"][0]["text"], "pasted comparison\n")

    def test_standalone_discard_then_cancel_recovery_preserves_edits_and_workspace(self):
        source = self.root / "source.txt"
        source.write_text("original\n", encoding="utf-8")
        window = self.window()
        window._load_panel(0, "file", source)
        window.panels[0]["editor"].setPlainText("unsaved\n")
        window._pending_named_workspace = {"name": "Pending"}
        window._write_recovery_snapshot()
        with patch.object(window._recovery_store, "write", side_effect=RecoveryError("Backup failed")), \
                patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                                   QMessageBox.StandardButton.Discard,
                                                                   QMessageBox.StandardButton.Cancel]):
            self.assertFalse(window.close())
        self.assertFalse(window._closed)
        self.assertTrue(window.panels[0]["dirty"])
        self.assertEqual(window.panels[0]["saved_text"], "original\n")
        self.assertEqual(window._panel_text(window.panels[0]), "unsaved\n")
        self.assertEqual(window._pending_named_workspace, {"name": "Pending"})
        self.assertTrue(window._recovery_timer.isActive())
        self.assertEqual(window._recovery_store.snapshots(include_active=True)[0][1]["panels"][0]["text"],
                         "unsaved\n")
        self.assertEqual(source.read_text(), "original\n")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(window.close())
        self.assertIsNone(window._pending_named_workspace)
        self.assertEqual(window._recovery_store.snapshots(), [])
        self.assertIsNone(window._recovery_store._lease)

    def test_missing_source_remains_recoverable(self):
        source = self.root / "vanished.txt"
        source.write_text("original\n", encoding="utf-8")
        first = self.window()
        first._load_panel(0, "file", source)
        first.panels[0]["editor"].setPlainText("unsaved\n")
        first._write_recovery_snapshot()
        source.unlink()
        self.abandon(first)
        second = self.window()
        with patch.object(RecoveryDialog, "exec", self.restore_dialog):
            self.assertTrue(second._offer_recovery())
        self.assertEqual(second._panel_text(second.panels[0]), "unsaved\n")
        self.assertTrue(second.panels[0]["disk_changed"])
        self.assertEqual(second.panels[0]["editing_path"], source)

    def test_recovery_store_bounds_and_replaces_atomically(self):
        store = RecoveryStore(self.settings_path)
        data = {"version": 1, "panels": [{"recover": True, "text": "first"}]}
        store.write(data)
        data["panels"][0]["text"] = "second"
        store.write(data)
        self.assertEqual(store.snapshots(include_active=True)[0][1]["panels"][0]["text"], "second")
        self.assertEqual(list(store.directory.glob("*.tmp")), [])
        data["panels"][0]["text"] = "x" * MAX_SNAPSHOT_BYTES
        with self.assertRaises(RecoveryError):
            store.write(data)
        self.assertEqual(store.snapshots(include_active=True)[0][1]["panels"][0]["text"], "second")
        store.release()


if __name__ == "__main__":
    unittest.main()
