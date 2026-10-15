"""Exact move detection and presentation in folder comparisons."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication

from d1ffler.diff_tool import (ComparisonCancelled, DEFAULT_SETTINGS, DiffApp,
                       detect_exact_moves)
from d1ffler.review_notes import report_text


class MoveDetectionTests(unittest.TestCase):
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

    def test_unique_hash_match_and_ambiguity(self):
        old = self.left / "old.txt"
        new = self.right / "new.txt"
        old.write_bytes(b"same")
        new.write_bytes(b"same")
        matches, limited = detect_exact_moves([{"old.txt": old}, {"new.txt": new}], lambda: False)
        self.assertFalse(limited)
        self.assertEqual([(item.from_path, item.to_path) for item in matches], [("old.txt", "new.txt")])
        duplicate = self.left / "duplicate.txt"
        duplicate.write_bytes(b"same")
        matches, _limited = detect_exact_moves(
            [{"old.txt": old, "duplicate.txt": duplicate}, {"new.txt": new}], lambda: False)
        self.assertEqual(matches, [])
        matches, limited = detect_exact_moves(
            [{"old.txt": old}, {"new.txt": new}], lambda: False, max_hashes=1)
        self.assertEqual(matches, [])
        self.assertTrue(limited)

    def test_edited_or_cancelled_match_is_not_reported(self):
        old, new = self.left / "old.txt", self.right / "new.txt"
        old.write_bytes(b"first")
        new.write_bytes(b"later")
        matches, _limited = detect_exact_moves([{"old.txt": old}, {"new.txt": new}], lambda: False)
        self.assertEqual(matches, [])
        event = Event()

        def stop_after_hash(path, cancelled):
            event.set()
            return "same"

        with patch("d1ffler.diff_tool.file_sha256", side_effect=stop_after_hash):
            with self.assertRaises(ComparisonCancelled):
                detect_exact_moves([{"old.txt": old}, {"new.txt": new}], event.is_set)

    def test_two_panel_tree_has_one_move_and_opens_both_paths(self):
        (self.left / "old.txt").write_text("same\n", encoding="utf-8")
        (self.right / "new.txt").write_text("same\n", encoding="utf-8")
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        window._load_panel(0, "folder", self.left)
        window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: window._folder_worker is None and len(window._folder_moves) == 1)
        self.wait_until(lambda: len(window._folder_tree_items[0]) == 2)
        self.assertEqual(len(window._folder_rows), 2)
        self.assertEqual(window._folder_moves[0].from_path, "old.txt")
        self.assertTrue(window._folder_tree_items[0]["old.txt"].isHidden())
        self.assertTrue(window._folder_tree_items[0]["new.txt"].isHidden())
        group = window._folder_move_groups[0]
        self.assertEqual(group.childCount(), 1)
        self.assertIn("old.txt → new.txt", group.child(0).text(0))
        window._open_folder_item(window.panels[0]["widget"], group.child(0))
        self.assertEqual(window.panels[0]["editing_path"], self.left / "old.txt")
        self.assertEqual(window.panels[1]["editing_path"], self.right / "new.txt")
        self.assertIn("Move:", window.status.text())
        context = window._review_report_context(window._review_sources())
        report = report_text(context, [])
        self.assertIn("old\\.txt", report)
        self.assertIn("new\\.txt", report)
        window._back_to_folders()
        (self.right / "new.txt").write_text("edited\n", encoding="utf-8")
        window._refresh_folder_comparison()
        self.wait_until(lambda: window._folder_worker is None and not window._folder_moves)
        self.wait_until(lambda: len(window._folder_tree_items[0]) == 2)
        self.assertFalse(window._folder_tree_items[0]["old.txt"].isHidden())
        self.assertIsNone(window._folder_move_groups[0])

    def test_three_panels_show_each_pair_without_hiding_originals(self):
        third = self.root / "third"
        third.mkdir()
        (self.left / "old.txt").write_bytes(b"same")
        (self.right / "new-a.txt").write_bytes(b"same")
        (third / "new-b.txt").write_bytes(b"same")
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        window._add_panel()
        for index, root in enumerate((self.left, self.right, third)):
            window._load_panel(index, "folder", root)
        self.wait_until(lambda: window._folder_worker is None and len(window._folder_moves) >= 2)
        self.assertFalse(window._folder_tree_items[0]["old.txt"].isHidden())
        self.assertTrue(any(match.from_panel == 0 and match.to_panel == 2 for match in window._folder_moves))
        match_item = next(window._folder_move_groups[0].child(index)
                          for index in range(window._folder_move_groups[0].childCount())
                          if window._folder_move_groups[0].child(index).data(0, Qt.ItemDataRole.UserRole + 2).to_panel == 2)
        window._open_folder_item(window.panels[0]["widget"], match_item)
        self.assertEqual(window.panels[0]["editing_path"], self.left / "old.txt")
        self.assertIsNone(window.panels[1]["editing_path"])
        self.assertEqual(window.panels[2]["editing_path"], third / "new-b.txt")

    def test_binary_move_opens_byte_comparison(self):
        (self.left / "old.bin").write_bytes(b"\0\xffpayload")
        (self.right / "new.bin").write_bytes(b"\0\xffpayload")
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        window._load_panel(0, "folder", self.left)
        window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: window._folder_worker is None and len(window._folder_moves) == 1)
        group = window._folder_move_groups[0]
        window._open_folder_item(window.panels[0]["widget"], group.child(0))
        self.assertTrue(window.panels[0]["binary"])
        self.assertTrue(window.panels[1]["binary"])
        self.assertEqual(window.panels[0]["editing_path"], self.left / "old.bin")
        self.assertEqual(window.panels[1]["editing_path"], self.right / "new.bin")

    def test_move_across_directories_hides_empty_original_branches(self):
        (self.left / "src").mkdir()
        (self.right / "moved").mkdir()
        (self.left / "src" / "old.py").write_bytes(b"print('same')\n")
        (self.right / "moved" / "new.py").write_bytes(b"print('same')\n")
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        window._load_panel(0, "folder", self.left)
        window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: window._folder_worker is None and len(window._folder_moves) == 1)
        self.wait_until(lambda: len(window._folder_tree_items[0]) == 2)
        self.assertTrue(window._folder_tree_folders[0]["src"].isHidden())
        self.assertTrue(window._folder_tree_folders[0]["moved"].isHidden())
        self.assertIn("src/old.py → moved/new.py", window._folder_move_groups[0].child(0).text(0))

    def test_move_detection_can_be_cancelled(self):
        (self.left / "old.txt").write_bytes(b"same")
        (self.right / "new.txt").write_bytes(b"same")
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)

        def wait_for_cancel(_maps, cancelled, _progress=None):
            while not cancelled():
                time.sleep(.005)
            raise ComparisonCancelled()

        with patch("d1ffler.diff_tool.detect_exact_moves", side_effect=wait_for_cancel):
            window._load_panel(0, "folder", self.left)
            window._load_panel(1, "folder", self.right)
            self.wait_until(lambda: "Detecting exact moves" in window.status.text())
            window._cancel_active_comparison()
            self.wait_until(lambda: window._folder_worker is None)
        self.assertTrue(window._folder_partial)
        self.assertEqual(window._folder_moves, [])

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def cleanup_window(self, window):
        window.close()
        window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
