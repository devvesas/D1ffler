"""Text search and replacement across editable and read-only panels."""

import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp


class TextSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def load_files(self, left="one cat\n", right="cat two\n"):
        a, b = self.root / "left.txt", self.root / "right.txt"
        a.write_text(left, encoding="utf-8")
        b.write_text(right, encoding="utf-8")
        self.window._load_panel(0, "file", a)
        self.window._load_panel(1, "file", b)
        return a, b

    def test_nonmodal_search_counts_case_word_navigation_and_wrap(self):
        self.load_files("cat catalog Cat\n", "cat\n")
        self.window._open_find()
        self.assertFalse(self.window.search_bar.isHidden())
        self.window.search_input.setText("cat")
        self.window.search_whole_word.setChecked(True)
        self.window._update_search_state()
        self.assertIn("3 matches", self.window.search_count.text())
        self.window._active_panel_index = 0
        self.window._find_next(1)
        self.assertEqual(self.window.panels[0]["editor"].textCursor().selectedText(), "cat")
        self.window._find_next(1)
        self.assertEqual(self.window.panels[0]["editor"].textCursor().selectedText(), "Cat")
        self.window._find_next(1)
        self.assertEqual(self.window._active_panel_index, 1)
        self.window._find_next(1)
        self.assertIn("wrapped", self.window.status.text())
        self.assertEqual(self.window._active_panel_index, 0)
        self.window._find_next(-1)
        self.assertIn("wrapped", self.window.status.text())
        self.assertEqual(self.window._active_panel_index, 1)
        self.window.search_case.setChecked(True)
        self.window._update_search_state()
        self.assertIn("2 matches", self.window.search_count.text())
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
        self.app.sendEvent(self.window.panels[1]["editor"], event)
        self.assertTrue(event.isAccepted())

    def test_replace_all_is_scoped_undoable_and_does_not_save(self):
        left, right = self.load_files("cat cat\n", "cat cat\n")
        self.window._open_find()
        self.window.search_input.setText("cat")
        self.window.replacement_input.setText("dog")
        self.window._active_panel_index = 1
        self.window._update_search_state()
        panel_name = self.window.panels[1]["name"].text()
        self.assertIn(panel_name, self.window.replace_target.text())
        with patch("d1ffler.text_search_mixin.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as confirm:
            self.window._replace_all()
        self.assertIn("2 matches", confirm.call_args.args[2])
        self.assertIn(panel_name, confirm.call_args.args[2])
        self.assertEqual(self.window._panel_text(self.window.panels[0]), "cat cat\n")
        self.assertEqual(self.window._panel_text(self.window.panels[1]), "dog dog\n")
        self.assertEqual(right.read_text(encoding="utf-8"), "cat cat\n")
        self.assertTrue(self.window.panels[1]["dirty"])
        self.assertTrue(self.window.panels[1]["editor"].document().isUndoAvailable())
        self.window.panels[1]["editor"].undo()
        self.assertEqual(self.window._panel_text(self.window.panels[1]), "cat cat\n")
        self.assertEqual(left.read_text(encoding="utf-8"), "cat cat\n")
        self.window._update_search_state()
        self.assertIn("4 matches", self.window.search_count.text())

    def test_replace_next_and_alignment_keep_undo(self):
        self.load_files("a\nb\n", "a\nx\nb\n")
        self.window._open_find()
        self.window.search_input.setText("x")
        self.window.replacement_input.setText("b")
        self.window._active_panel_index = 1
        self.window._replace_next()
        self.assertEqual(self.window._panel_text(self.window.panels[1]), "a\nb\nb\n")
        self.window.panels[1]["editor"].undo()
        self.assertEqual(self.window._panel_text(self.window.panels[1]), "a\nx\nb\n")

    def test_folder_path_search_stays_independent_and_zip_is_read_only(self):
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "file.txt").write_text("find me\n", encoding="utf-8")
        archive = self.root / "source.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("file.txt", "find me\n")
        self.window._load_panel(0, "folder", archive)
        self.window._load_panel(1, "folder", folder)
        self.wait_until(lambda: self.window._folder_worker is None and
                        "file.txt" in self.window._folder_tree_items[0])
        self.window.folder_path_search.setText("file.txt")
        self.window._open_find()
        self.assertEqual(self.window.folder_path_search.selectedText(), "file.txt")
        self.assertTrue(self.window.search_bar.isHidden())
        self.window._open_folder_item(self.window.panels[0]["widget"],
                                      self.window._folder_tree_items[0]["file.txt"])
        self.window._open_find()
        self.window.search_input.setText("find")
        self.window.replacement_input.setText("replace")
        self.window._active_panel_index = 0
        self.window._update_search_state()
        self.assertFalse(self.window.replace_next_button.isEnabled())
        self.window._replace_next()
        self.assertEqual(self.window._panel_text(self.window.panels[0]), "find me\n")
        self.window._back_to_folders()
        self.assertTrue(self.window.search_bar.isHidden())
        self.assertEqual(self.window.folder_path_search.text(), "file.txt")
        self.wait_until(lambda: self.window._folder_worker is None and
                        "file.txt" in self.window._folder_tree_items[0])
        self.window._open_folder_item(self.window.panels[0]["widget"],
                                      self.window._folder_tree_items[0]["file.txt"])
        self.assertFalse(self.window.search_bar.isHidden())
        self.assertEqual(self.window.search_input.text(), "find")


if __name__ == "__main__":
    unittest.main()
