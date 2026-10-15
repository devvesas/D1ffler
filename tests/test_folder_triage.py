"""Path search, status triage, and navigation for folder trees."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp, _files_match_cancellable


class FolderTriageTests(unittest.TestCase):
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
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def open_folders(self):
        self.window._load_panel(0, "folder", self.left)
        self.window._load_panel(1, "folder", self.right)

    def test_path_search_reveals_discovered_pending_file(self):
        (self.left / "src").mkdir()
        (self.right / "src").mkdir()
        for number in range(80):
            name = f"src/file-{number:03}.txt"
            (self.left / name).write_text("same", encoding="utf-8")
            (self.right / name).write_text("same", encoding="utf-8")

        def slow_match(left, right, whitespace, endings, cancelled):
            while not cancelled():
                time.sleep(.005)
            return "cancelled"

        with patch("d1ffler.diff_tool._files_match_cancellable", side_effect=slow_match):
            self.open_folders()
            self.window.folder_path_search.setText("src/file-079.txt")
            with patch("d1ffler.diff_tool.QInputDialog.getText") as text_dialog:
                self.window._open_find()
            text_dialog.assert_not_called()
            self.assertEqual(self.window.folder_path_search.selectedText(), "src/file-079.txt")
            self.window._apply_folder_filters()
            self.wait_until(lambda: "src/file-079.txt" in self.window._folder_rows)
            self.wait_until(lambda: "src/file-079.txt" in self.window._folder_tree_items[0])
            item = self.window._folder_tree_items[0]["src/file-079.txt"]
            self.assertEqual(item.text(1), "Checking…")
            self.assertTrue(item.parent().isExpanded())
            self.assertEqual(self.window.panels[0]["tree"].currentItem(), item)
            self.assertEqual(self.window.panels[1]["tree"].currentItem().data(0, Qt.ItemDataRole.UserRole), "src/file-079.txt")
            self.assertIn("1 shown", self.window.folder_counts_label.text())
            self.window.folder_status_combo.setCurrentIndex(6)  # Pending
            self.assertGreater(self.window._folder_status_counts["pending"], 0)
            self.assertEqual(self.window._folder_status_counts["identical"], 0)
            self.window._cancel_folder_comparison()
            self.wait_until(lambda: self.window._folder_worker is None)
        self.assertIn("partial", self.window.folder_counts_label.text())

    def test_status_counts_and_filters(self):
        for name, left, right in (
            ("same.txt", "same", "same"),
            ("modified.txt", "left", "right"),
            ("removed.txt", "left", None),
            ("added.txt", None, "right"),
        ):
            if left is not None:
                (self.left / name).write_text(left, encoding="utf-8")
            if right is not None:
                (self.right / name).write_text(right, encoding="utf-8")
        self.open_folders()
        self.wait_until(lambda: self.window._folder_worker is None and len(self.window._folder_rows) == 4)
        self.assertEqual({key: self.window._folder_status_counts[key]
                          for key in ("added", "removed", "modified", "identical", "error", "pending")},
                         {"added": 1, "removed": 1, "modified": 1, "identical": 1, "error": 0, "pending": 0})
        self.assertEqual(self.window.folder_status_combo.itemText(2), "Removed (1)")
        self.window.folder_status_combo.setCurrentIndex(2)  # Removed
        self.wait_until(lambda: len(self.window._folder_tree_items[0]) == 1)
        self.assertIn("removed.txt", self.window._folder_tree_items[0])
        self.assertIn("1 shown", self.window.folder_counts_label.text())
        self.window.folder_status_combo.setCurrentIndex(4)  # Identical
        self.wait_until(lambda: len(self.window._folder_tree_items[0]) == 1)
        self.assertIn("same.txt", self.window._folder_tree_items[0])
        self.assertFalse(self.window.next_change_button.isEnabled())
        self.window.folder_status_combo.setCurrentIndex(5)  # Error
        self.assertIn("0 shown", self.window.folder_counts_label.text())
        self.assertEqual(len(self.window._folder_display_pending), 0)
        self.window.folder_status_combo.setCurrentIndex(0)
        self.wait_until(lambda: len(self.window._folder_tree_items[0]) == 4)
        selected = self.window._folder_tree_items[0]["removed.txt"]
        self.window.panels[0]["tree"].setCurrentItem(selected)
        self.assertEqual(self.window.panels[1]["tree"].currentItem().data(0, Qt.ItemDataRole.UserRole),
                         "removed.txt")

    def test_f7_navigates_known_change_during_partial_scan_and_after_cancel(self):
        for number in range(64):
            (self.left / f"a-{number:03}.txt").write_text("only here", encoding="utf-8")
        (self.left / "b-hold.txt").write_text("left", encoding="utf-8")
        (self.right / "b-hold.txt").write_text("right", encoding="utf-8")

        def slow_match(left, right, whitespace, endings, cancelled):
            if left.name == "b-hold.txt":
                while not cancelled():
                    time.sleep(.005)
                return "cancelled"
            return _files_match_cancellable(left, right, whitespace, endings, cancelled)

        with patch("d1ffler.diff_tool._files_match_cancellable", side_effect=slow_match):
            self.open_folders()
            self.wait_until(lambda: self.window._folder_rows.get("a-000.txt") is not None and
                            self.window._folder_worker is not None)
            self.window._navigate_change(1)
            for panel in self.window.panels[:2]:
                self.assertEqual(panel["tree"].currentItem().data(0, Qt.ItemDataRole.UserRole), "a-000.txt")
            self.assertIn("Missing", self.window.panels[1]["tree"].currentItem().text(1))
            self.window._cancel_folder_comparison()
            self.wait_until(lambda: self.window._folder_worker is None)
        self.window._navigate_change(1)
        self.assertIn("a-001.txt", self.window.status.text())
        self.window._navigate_change(-1)
        self.assertIn("a-000.txt", self.window.status.text())
        self.assertIn("partial", self.window.folder_counts_label.text())

    def test_error_filter_does_not_count_error_as_identical(self):
        (self.left / "broken.txt").write_text("left", encoding="utf-8")
        (self.right / "broken.txt").write_text("right", encoding="utf-8")
        with patch("d1ffler.diff_tool._files_match_cancellable", return_value="error"):
            self.open_folders()
            self.wait_until(lambda: self.window._folder_worker is None and
                            self.window._folder_rows.get("broken.txt") is not None)
        self.assertEqual(self.window._folder_status_counts["error"], 1)
        self.assertEqual(self.window._folder_status_counts["identical"], 0)
        self.window.folder_status_combo.setCurrentIndex(5)
        self.wait_until(lambda: "broken.txt" in self.window._folder_tree_items[0])
        self.assertIn("1 shown", self.window.folder_counts_label.text())

    def test_refresh_restores_scroll_position(self):
        for number in range(120):
            name = f"file-{number:03}.txt"
            (self.left / name).write_text("same", encoding="utf-8")
            (self.right / name).write_text("same", encoding="utf-8")
        self.window.show()
        self.open_folders()
        self.wait_until(lambda: self.window._folder_worker is None and
                        len(self.window._folder_tree_items[0]) == 120)
        tree = self.window.panels[0]["tree"]
        bar = tree.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        position = bar.maximum() // 2
        bar.setValue(position)
        self.window._refresh_folder_comparison()
        self.wait_until(lambda: self.window._folder_worker is None and
                        len(self.window._folder_tree_items[0]) == 120)
        self.wait_until(lambda: bar.value() == position)

    def test_refresh_preserves_explicit_folder_expansion(self):
        (self.left / "nested").mkdir()
        (self.right / "nested").mkdir()
        (self.left / "nested" / "file.txt").write_text("same", encoding="utf-8")
        (self.right / "nested" / "file.txt").write_text("same", encoding="utf-8")
        self.open_folders()
        self.wait_until(lambda: self.window._folder_worker is None and
                        "nested" in self.window._folder_tree_folders[0])
        folder = self.window._folder_tree_folders[0]["nested"]
        folder.setExpanded(True)
        self.assertIn("nested", self.window._folder_expanded_paths[0])
        self.window._refresh_folder_comparison()
        self.wait_until(lambda: self.window._folder_worker is None and
                        "nested" in self.window._folder_tree_folders[0])
        folder = self.window._folder_tree_folders[0]["nested"]
        self.assertTrue(folder.isExpanded())
        folder.setExpanded(False)
        self.window._refresh_folder_comparison()
        self.wait_until(lambda: self.window._folder_worker is None and
                        "nested" in self.window._folder_tree_folders[0])
        self.assertFalse(self.window._folder_tree_folders[0]["nested"].isExpanded())

    def test_refresh_keeps_search_filter_selection_and_expansion(self):
        (self.left / "nested").mkdir()
        (self.right / "nested").mkdir()
        for number in range(20):
            name = f"nested/file-{number:02}.txt"
            (self.left / name).write_text("before", encoding="utf-8")
            (self.right / name).write_text("after", encoding="utf-8")
        self.open_folders()
        self.wait_until(lambda: self.window._folder_worker is None and len(self.window._folder_rows) == 20)
        self.window.folder_path_search.setText("file-17")
        self.window._apply_folder_filters()
        self.wait_until(lambda: "nested/file-17.txt" in self.window._folder_tree_items[0])
        self.window.folder_status_combo.setCurrentIndex(3)  # Modified
        self.wait_until(lambda: "nested/file-17.txt" in self.window._folder_tree_items[0])
        selected = self.window._folder_tree_items[0]["nested/file-17.txt"]
        self.window._select_folder_target("file", "nested/file-17.txt")
        self.assertTrue(selected.parent().isExpanded())
        self.window._refresh_folder_comparison()
        self.wait_until(lambda: self.window._folder_worker is None and
                        "nested/file-17.txt" in self.window._folder_tree_items[0])
        self.assertEqual(self.window.folder_path_search.text(), "file-17")
        self.assertEqual(self.window.folder_status_combo.currentData(), "modified")
        for panel in self.window.panels[:2]:
            self.assertEqual(panel["tree"].currentItem().data(0, Qt.ItemDataRole.UserRole), "nested/file-17.txt")
        self.assertTrue(self.window._folder_tree_folders[0]["nested"].isExpanded())


if __name__ == "__main__":
    unittest.main()
