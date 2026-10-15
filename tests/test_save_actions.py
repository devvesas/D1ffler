"""Exercise user-facing Save actions for file previews and read-only sources."""

import codecs
import os
import tempfile
import time
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.comparison_tabs import ComparisonWindow
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp


class SaveActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = ComparisonWindow(deepcopy(DEFAULT_SETTINGS), self.root / "settings.json", DiffApp)
        self.window = self.host.active_comparison
        self.addCleanup(self.cleanup_window)
        self.host.show()
        self.app.processEvents()

    def cleanup_window(self):
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.host.close()
        self.host.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_for_scan(self):
        deadline = time.monotonic() + 5
        while self.window._folder_jobs and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(self.window._folder_jobs)

    def load_folders(self, left=b"original left\n", right=b"original right\n"):
        folders = [self.root / side for side in ("left", "right")]
        for folder, content in zip(folders, (left, right)):
            folder.mkdir()
            (folder / "sample.txt").write_bytes(content)
        self.window.open_paths(folders)
        self.wait_for_scan()
        return [folder / "sample.txt" for folder in folders]

    def open_preview(self):
        deadline = time.monotonic() + 5
        while "sample.txt" not in self.window._folder_tree_items[0] and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        item = self.window._folder_tree_items[0]["sample.txt"]
        self.window._open_folder_item(self.window.panels[0]["widget"], item)

    def activate(self, index):
        editor = self.window.panels[index]["editor"]
        editor.setFocus()
        self.app.processEvents()
        QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton)
        self.assertEqual(self.window._active_panel_index, index)
        return editor

    def click_save(self):
        QTest.mouseClick(self.window.save_button, Qt.MouseButton.LeftButton)

    def press_save(self, editor):
        QTest.keyClick(editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)

    def test_button_saves_active_folder_preview_with_its_encoding(self):
        left, right = self.load_folders(left=codecs.BOM_UTF16_LE + "original left\n".encode("utf-16-le"))
        self.open_preview()
        self.window.panels[0]["editor"].setPlainText("edited left\n")
        self.window.panels[1]["editor"].setPlainText("edited right\n")
        self.activate(0)
        self.assertTrue(self.window.save_button.isEnabled())
        self.click_save()
        self.assertEqual(left.read_bytes(), codecs.BOM_UTF16_LE + "edited left\n".encode("utf-16-le"))
        self.assertEqual(right.read_bytes(), b"original right\n")
        self.assertFalse(self.window.panels[0]["dirty"])
        self.assertTrue(self.window.panels[1]["dirty"])
        self.assertEqual(self.window._panel_text(self.window.panels[1]), "edited right\n")
        self.assertEqual(self.window.panels[0]["kind"], "folder")
        self.assertEqual(self.window.panels[0]["view_stack"].currentIndex(), 0)

    def test_shortcut_saves_the_other_active_folder_preview(self):
        left, right = self.load_folders()
        self.open_preview()
        self.window.panels[0]["editor"].setPlainText("edited left\n")
        editor = self.activate(1)
        editor.setPlainText("edited right\n")
        self.press_save(editor)
        self.assertEqual(right.read_bytes(), b"edited right\n")
        self.assertEqual(left.read_bytes(), b"original left\n")
        self.assertFalse(self.window.panels[1]["dirty"])
        self.assertTrue(self.window.panels[0]["dirty"])

    def test_main_actions_keep_external_change_confirmation(self):
        left, _right = self.load_folders()
        self.open_preview()
        editor = self.activate(0)
        editor.setPlainText("local edits\n")
        left.write_bytes(b"external edits\n")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as warning:
            self.click_save()
        self.assertIn(str(left), warning.call_args.args[2])
        self.assertEqual(left.read_bytes(), b"external edits\n")
        self.assertTrue(self.window.panels[0]["dirty"])
        self.assertTrue(self.window.panels[0]["disk_changed"])
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            self.press_save(editor)
        self.assertEqual(left.read_bytes(), b"local edits\n")
        self.assertFalse(self.window.panels[0]["dirty"])

    def test_failed_atomic_save_retains_disk_and_buffer(self):
        left, _right = self.load_folders()
        self.open_preview()
        editor = self.activate(0)
        editor.setPlainText("local edits\n")
        with patch("d1ffler.panel_mixin.QSaveFile.commit", return_value=False), patch("d1ffler.diff_tool.QMessageBox.critical"):
            self.click_save()
        self.assertEqual(left.read_bytes(), b"original left\n")
        self.assertEqual(self.window._panel_text(self.window.panels[0]), "local edits\n")
        self.assertTrue(self.window.panels[0]["dirty"])

    def test_back_to_folder_tree_disables_save_even_with_dirty_preview(self):
        left, _right = self.load_folders()
        self.open_preview()
        editor = self.activate(0)
        editor.setPlainText("local edits\n")
        self.window._back_to_folders()
        self.assertFalse(self.window.save_button.isEnabled())
        self.assertIn("Open a folder file", self.window.save_button.toolTip())
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName") as choose_path:
            self.click_save()
            self.press_save(self.window.panels[0]["tree"])
        choose_path.assert_not_called()
        self.assertIn("Open a folder file", self.window.status.text())
        self.assertEqual(left.read_bytes(), b"original left\n")
        self.assertTrue(self.window.panels[0]["dirty"])
        self.open_preview_after_discard()
        self.assertTrue(self.window.save_button.isEnabled())

    def open_preview_after_discard(self):
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.open_preview()

    def test_zip_preview_disables_save_and_never_opens_save_as(self):
        archive = self.root / "sample.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("sample.txt", "archived text\n")
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "sample.txt").write_text("editable text\n", encoding="utf-8")
        self.window.open_paths([archive, folder])
        self.wait_for_scan()
        self.open_preview()
        before = archive.read_bytes()
        editor = self.activate(0)
        self.assertFalse(self.window.save_button.isEnabled())
        self.assertIn("ZIP", self.window.save_button.toolTip())
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName") as choose_path:
            self.click_save()
            self.press_save(editor)
        choose_path.assert_not_called()
        self.assertEqual(archive.read_bytes(), before)
        self.assertIn("ZIP", self.window.status.text())
        self.activate(1)
        self.assertTrue(self.window.save_button.isEnabled())

    def test_read_only_revision_cannot_use_main_save_actions(self):
        left, _right = self.load_folders()
        panel = self.window.panels[0]
        source = {"readonly": True, "kind": "commit", "commit": "a" * 40, "name": "snapshot"}
        with patch("d1ffler.git_source_mixin.is_dirty", return_value=False):
            self.window._set_git_source(panel, source)
            self.open_preview()
        editor = self.activate(0)
        self.assertTrue(editor.isReadOnly())
        self.assertFalse(self.window.save_button.isEnabled())
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName") as choose_path:
            self.click_save()
            self.press_save(editor)
        choose_path.assert_not_called()
        self.assertEqual(left.read_bytes(), b"original left\n")
        self.assertIn("Read-only revision", self.window.status.text())

    def test_binary_preview_cannot_be_saved(self):
        left, _right = self.load_folders(left=b"\x00" * 5, right=b"\x00" * 7)
        self.open_preview()
        editor = self.activate(0)
        self.assertFalse(self.window.save_button.isEnabled())
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName") as choose_path:
            self.click_save()
            self.press_save(editor)
        choose_path.assert_not_called()
        self.assertEqual(left.read_bytes(), b"\x00" * 5)
        self.assertIn("read-only", self.window.status.text())

    def test_missing_folder_file_does_not_offer_to_save_placeholder_text(self):
        _left, right = self.load_folders()
        right.unlink()
        self.window._refresh_folder_comparison()
        self.wait_for_scan()
        self.open_preview()
        editor = self.activate(1)
        self.assertFalse(self.window.save_button.isEnabled())
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName") as choose_path:
            self.press_save(editor)
        choose_path.assert_not_called()
        self.assertFalse(right.exists())
        self.assertIn("no editable file", self.window.status.text())

    def test_file_and_pasted_text_save_still_work(self):
        path = self.root / "file.txt"
        path.write_text("original\n", encoding="utf-8")
        self.window._load_panel(0, "file", path)
        editor = self.activate(0)
        editor.setPlainText("edited\n")
        self.click_save()
        self.assertEqual(path.read_bytes(), b"edited\n")
        editor = self.activate(1)
        editor.setPlainText("pasted\n")
        destination = self.root / "pasted.txt"
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(destination), "")):
            self.press_save(editor)
        self.assertEqual(destination.read_bytes(), b"pasted\n")

    def test_merge_save_actions_only_write_result(self):
        inputs = []
        for index, text in enumerate(("base\n", "local\n", "base\n")):
            path = self.root / f"input-{index}.txt"
            path.write_bytes(text.encode("utf-8"))
            inputs.append(path)
        self.window.open_paths(inputs)
        self.window.merge_mode_button.setChecked(True)
        result = self.root / "result.txt"
        editor = self.activate(0)
        self.assertTrue(self.window.save_button.isEnabled())
        self.assertIn("Result", self.window.save_button.toolTip())
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(result), "")):
            self.press_save(editor)
        self.assertEqual(result.read_bytes(), b"local\n")
        self.window.panels[3]["editor"].setPlainText("manual result\n")
        self.activate(1)
        self.click_save()
        self.assertEqual(result.read_bytes(), b"manual result\n")
        self.assertEqual([path.read_bytes() for path in inputs], [b"base\n", b"local\n", b"base\n"])


if __name__ == "__main__":
    unittest.main()
