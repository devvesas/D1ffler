"""External edit protection around open panels and saves."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp, disk_fingerprint


class ExternalChangesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "sample.txt"
        self.path.write_text("original\n", encoding="utf-8")
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.window._load_panel(0, "file", self.path)
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_dirty_save_requires_named_overwrite_confirmation(self):
        panel = self.window.panels[0]
        panel["editor"].setPlainText("local\n")
        self.path.write_text("external\n", encoding="utf-8")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as warning:
            self.assertFalse(self.window._save_panel_file(0))
        self.assertIn(str(self.path), warning.call_args.args[2])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "external\n")
        self.assertTrue(panel["dirty"])
        self.assertTrue(panel["disk_changed"])
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(self.window._save_panel_file(0))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "local\n")
        self.assertFalse(panel["dirty"])
        self.assertFalse(panel["disk_changed"])

    def test_clean_reloads_replacement_and_deletion(self):
        panel = self.window.panels[0]
        replacement = self.root / "replacement.txt"
        replacement.write_text("replacement\n", encoding="utf-8")
        os.replace(replacement, self.path)
        self.window._check_external_changes()
        self.assertTrue(panel["disk_changed"])
        self.assertIn(str(self.path), self.window._disk_watcher.files())
        self.window._reload_panel(panel["widget"])
        self.assertEqual(self.window._panel_text(panel), "replacement\n")
        self.assertFalse(panel["disk_changed"])
        self.path.unlink()
        self.window._check_external_changes()
        self.assertTrue(panel["disk_changed"])
        self.window._reload_panel(panel["widget"])
        self.assertIn("File missing on disk", self.window._panel_text(panel))
        self.assertFalse(panel["dirty"])

    def test_failed_commit_keeps_old_file_and_dirty_buffer(self):
        panel = self.window.panels[0]
        panel["editor"].setPlainText("local\n")
        with patch("d1ffler.panel_mixin.QSaveFile.commit", return_value=False), patch("d1ffler.diff_tool.QMessageBox.critical"):
            self.assertFalse(self.window._save_panel_file(0))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original\n")
        self.assertTrue(panel["dirty"])

    def test_change_during_temp_write_cancels_save(self):
        panel = self.window.panels[0]
        panel["editor"].setPlainText("local\n")
        checks = 0

        def change_after_open(path):
            nonlocal checks
            checks += 1
            if checks == 2:
                self.path.write_text("late external\n", encoding="utf-8")
            return disk_fingerprint(path)

        with patch("d1ffler.panel_mixin.disk_fingerprint", side_effect=change_after_open), patch("d1ffler.diff_tool.QMessageBox.critical"):
            self.assertFalse(self.window._save_panel_file(0))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "late external\n")
        self.assertTrue(panel["dirty"])

    def test_save_as_copy_keeps_original_and_dirty_buffer(self):
        panel = self.window.panels[0]
        panel["editor"].setPlainText("local\n")
        self.path.write_text("external\n", encoding="utf-8")
        copy = self.root / "copy.txt"
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(copy), "")):
            self.assertTrue(self.window._save_panel_file(0, save_as=True, copy_only=True))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "external\n")
        self.assertEqual(copy.read_text(encoding="utf-8"), "local\n")
        self.assertTrue(panel["dirty"])
        self.assertEqual(panel["editing_path"], self.path)

    def test_folder_root_event_marks_results_stale(self):
        other = self.root / "other"
        other.mkdir()
        self.window._clear_panel(0)
        self.window._load_panel(0, "folder", self.root)
        self.window._load_panel(1, "folder", other)
        self.window._watched_path_changed(str(self.root))
        self.assertTrue(self.window._folder_stale)
        self.assertIn("stale", self.window.status.text())
        self.window._refresh_folder_comparison()
        self.assertFalse(self.window._folder_stale)


if __name__ == "__main__":
    unittest.main()
