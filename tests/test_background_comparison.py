"""GUI checks for background folder and binary comparisons."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from d1ffler.diff_tool import ComparisonCancelled, DEFAULT_SETTINGS, DiffApp, inspect_file_content


class BackgroundComparisonTests(unittest.TestCase):
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
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_folder_scan_is_incremental_and_refresh_detects_change(self):
        for index in range(640):
            name = f"file-{index:03}.txt"
            (self.left / name).write_text("same", encoding="utf-8")
            (self.right / name).write_text("same", encoding="utf-8")
        started = time.monotonic()
        self.window._load_panel(0, "folder", self.left)
        self.window._load_panel(1, "folder", self.right)
        self.assertLess(time.monotonic() - started, 2)
        self.wait_until(lambda: self.window._folder_worker is None and
                        len(self.window._folder_rows) == 640)
        self.assertEqual(self.window._folder_rows["file-000.txt"], ["identical", "identical"])
        self.wait_until(lambda: len(self.window._folder_tree_items[0]) == 640)
        (self.right / "file-000.txt").write_text("edit", encoding="utf-8")
        self.window._refresh_folder_comparison()
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("file-000.txt") == ["identical", "modified"])

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_junction_is_visible_with_link_details_and_exported(self):
        target = self.left / "target"
        target.mkdir()
        (target / "data.txt").write_text("inside", encoding="utf-8")
        result = subprocess.run(["cmd", "/c", "mklink", "/J",
                                 str(self.left / "alias"), str(target)],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest(f"Junctions unavailable: {result.stderr or result.stdout}")
        vanished = self.left / "vanished"
        vanished.mkdir()
        result = subprocess.run(["cmd", "/c", "mklink", "/J",
                                 str(self.left / "broken"), str(vanished)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        vanished.rmdir()
        self.window._load_panel(0, "folder", self.left)
        self.window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: self.window._folder_worker is None and
                        "alias" in self.window._folder_tree_items[0])
        item = self.window._folder_tree_items[0]["alias"]
        self.assertIn("junction", item.text(1))
        self.assertIn("internal", item.toolTip(0))
        self.assertEqual(self.window._folder_links["alias"][0].kind, "junction")
        self.assertEqual(self.window._folder_rows["broken"], ["error", "removed"])
        self.wait_until(lambda: "broken" in self.window._folder_tree_items[0])
        self.assertIn("broken", self.window._folder_tree_items[0]["broken"].text(1))
        self.assertIn("alias", self.window._comparison_export_snapshot(True, False, False)["links"])
        self.window._folder_status_filter = "linked"
        self.window._apply_folder_filters()
        self.assertEqual(self.window._folder_visible_count, 2)
        self.wait_until(lambda: "alias" in self.window._folder_tree_items[0])
        self.window._open_folder_item(self.window.panels[0]["widget"],
                                      self.window._folder_tree_items[0]["alias"])
        self.assertIn("Linked path: alias", self.window.panels[0]["editor"].toPlainText())
        self.assertTrue(self.window.panels[0]["editor"].isReadOnly())

    def test_cancel_marks_unchecked_rows_partial(self):
        (self.left / "large.bin").write_bytes(b"x" * 1024)
        (self.right / "large.bin").write_bytes(b"x" * 1024)

        def slow_match(_a, _b, _w, _e, cancelled):
            while not cancelled():
                time.sleep(0.005)
            return "cancelled"

        with patch("d1ffler.diff_tool._files_match_cancellable", side_effect=slow_match):
            self.window._load_panel(0, "folder", self.left)
            self.window._load_panel(1, "folder", self.right)
            self.wait_until(lambda: "large.bin" in self.window._folder_rows)
            self.window._cancel_folder_comparison()
            self.wait_until(lambda: self.window._folder_worker is None)
        self.assertTrue(self.window._folder_partial)
        self.assertIsNone(self.window._folder_rows["large.bin"])
        self.assertIn("partial", self.window.status.text())

    def test_binary_summary_can_be_cancelled_without_blocking_window(self):
        left, right = self.root / "a.bin", self.root / "b.bin"
        left.write_bytes(b"\0" * 1024)
        right.write_bytes(b"\1" * 1024)

        def slow_summary(_left, _right, cancelled):
            while not cancelled():
                time.sleep(0.005)
            raise ComparisonCancelled()

        with patch("d1ffler.diff_tool.binary_file_summary", side_effect=slow_summary):
            self.window._load_panel(0, "file", left)
            started = time.monotonic()
            self.window._load_panel(1, "file", right)
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNotNone(self.window._binary_worker)
            self.window._cancel_active_comparison()
            self.assertEqual(self.window.status.text(), "Binary comparison cancelled")

    def test_large_text_file_uses_bounded_editor_preview(self):
        path = self.root / "large.txt"
        with path.open("wb") as stream:
            stream.write(b"a" * (16 * 1024 * 1024 + 1))
        content = inspect_file_content(path, preview_limit=True)
        self.assertTrue(content.too_large)
        self.assertIsNone(content.text)

    def test_large_text_alignment_runs_in_worker(self):
        left, right = self.root / "left.txt", self.root / "right.txt"
        lines = [f"unique line {index:05} value\n" for index in range(4000)]
        left.write_text("".join(lines), encoding="utf-8")
        lines[2000] = "changed line 02000 value\n"
        right.write_text("".join(lines), encoding="utf-8")
        self.window._load_panel(0, "file", left)
        self.window._load_panel(1, "file", right)
        self.wait_until(lambda: self.window._text_worker is None and
                        bool(self.window._hunks), timeout=10)
        self.assertEqual(len(self.window._hunks), 1)

    def test_large_text_rule_error_replaces_stale_alignment(self):
        left, right = self.root / "left.txt", self.root / "right.txt"
        left.write_text("before\n", encoding="utf-8")
        right.write_text("after\n", encoding="utf-8")
        self.window._load_panel(0, "file", left)
        self.window._load_panel(1, "file", right)
        self.assertEqual(len(self.window._hunks), 1)
        self.window.workspace_text_rules = [{"name": "Mask", "scope": "*", "pattern": "x",
                                             "mode": "match", "enabled": True}]
        text = "x" * 17000 + "\n" + ("a" * 13000 + "\n") * 4
        self.window._loading_panel = True
        try:
            for panel in self.window.panels:
                panel["editor"].setPlainText(text)
        finally:
            self.window._loading_panel = False
        self.window._refresh_diff()
        self.wait_until(lambda: self.window._text_worker is None and not self.window._text_jobs)
        self.assertEqual(self.window._hunks, [])
        self.assertGreater(len(self.window._aligned_rows), 1)
        self.assertIn("Text rule error", self.window.status.text())


if __name__ == "__main__":
    unittest.main()
