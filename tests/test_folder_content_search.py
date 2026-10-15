"""Bounded folder content search and hit navigation."""

import os
import queue
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.folder_archives import zip_inventory
from d1ffler.folder_content_search import FolderContentSearchWorker, SearchTask
from d1ffler.folder_content_search_dialog import FolderContentSearchDialog


class FolderContentSearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _run(self, tasks, query="needle", *, pending=0, cancelled=None):
        messages = queue.Queue()
        worker = FolderContentSearchWorker(
            tasks, query, False, cancelled or threading.Event(), messages,
            pending=pending, snapshot_partial=pending > 0)
        worker.run()
        output = []
        while not messages.empty():
            output.append(messages.get_nowait())
        return output

    def test_searches_text_and_zip_and_reports_skips(self):
        text = self.root / "text.txt"
        text.write_bytes(b"first\nNeedle here\n")
        binary = self.root / "binary.bin"
        binary.write_bytes(bytes(range(256)))
        oversized = self.root / "oversized.txt"
        with oversized.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024 + 1)
        archive = self.root / "source.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("nested/entry.txt", "zip needle\n")
        members, _excluded = zip_inventory(archive, [], lambda _path, _patterns: False)
        tasks = [SearchTask("text.txt", 0, "Left", text),
                 SearchTask("binary.bin", 0, "Left", binary),
                 SearchTask("oversized.txt", 0, "Left", oversized),
                 SearchTask("nested/entry.txt", 1, "ZIP", members["nested/entry.txt"]),
                 SearchTask("link.txt", 0, "Left", text, linked=True)]
        messages = self._run(tasks, pending=1)
        hits = [hit for message in messages if message[0] == "hits" for hit in message[1]]
        self.assertEqual([(hit.relative, hit.panel_name, hit.line, hit.column) for hit in hits],
                         [("text.txt", "Left", 2, 1), ("nested/entry.txt", "ZIP", 1, 5)])
        finished = next(message for message in messages if message[0] == "finished")
        counts = finished[1]
        self.assertEqual(counts["searched"], 2)
        self.assertEqual(counts["binary"], 1)
        self.assertEqual(counts["oversized"], 1)
        self.assertEqual(counts["linked"], 1)
        self.assertEqual(counts["pending"], 1)
        self.assertTrue(finished[-1])

    def test_cancel_marks_results_partial(self):
        file = self.root / "file.txt"
        file.write_text("needle\n", encoding="utf-8")
        event = threading.Event()
        event.set()
        messages = self._run([SearchTask("file.txt", 0, "Left", file)], cancelled=event)
        finished = next(message for message in messages if message[0] == "finished")
        self.assertEqual(finished[4], 0)
        self.assertTrue(finished[-1])


class FolderContentSearchDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qapp.processEvents()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.qapp.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_result_opens_correct_panel_and_line_without_changing_path_search(self):
        left, right = self.root / "left", self.root / "right"
        (left / "nested").mkdir(parents=True)
        (right / "nested").mkdir(parents=True)
        (left / "nested" / "file.txt").write_bytes(b"before\nneedle here\nafter\n")
        (right / "nested" / "file.txt").write_bytes(b"before\nother\nafter\n")
        (left / "other.txt").write_bytes(b"needle elsewhere\n")
        self.window._load_panel(0, "folder", left)
        self.window._load_panel(1, "folder", right)
        self.wait_until(lambda: self.window._folder_worker is None and
                        "nested/file.txt" in self.window._folder_rows)
        self.window.folder_path_search.setText("nested")
        dialog = FolderContentSearchDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.query.setText("needle")
        dialog.scope.setCurrentIndex(1)
        dialog._start()
        self.wait_until(lambda: dialog.search_button.isEnabled())
        self.assertEqual(dialog.results.topLevelItemCount(), 1)
        result = dialog.results.topLevelItem(0)
        self.assertEqual(result.text(0), "nested/file.txt")
        self.assertEqual(result.text(1), self.window.panels[0]["name"].text())
        dialog._open_hit(result, 0)
        self.assertEqual(self.window.panels[0]["editor"].textCursor().selectedText(), "needle")
        self.assertEqual(self.window.folder_path_search.text(), "nested")

    def test_refresh_marks_results_stale(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        (left / "file.txt").write_bytes(b"needle\n")
        (right / "file.txt").write_bytes(b"other\n")
        self.window._load_panel(0, "folder", left)
        self.window._load_panel(1, "folder", right)
        self.wait_until(lambda: self.window._folder_worker is None and "file.txt" in self.window._folder_rows)
        dialog = FolderContentSearchDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.query.setText("needle")
        dialog._start()
        self.wait_until(lambda: dialog.search_button.isEnabled())
        self.window._folder_generation += 1
        result = dialog.results.topLevelItem(0)
        dialog._open_hit(result, 0)
        self.assertIn("stale", dialog.summary.text().lower())
        self.assertTrue(self.window.folder_back_button.isHidden())

    def test_hit_position_survives_background_text_alignment(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        long_line = b"a" * 70_000 + b"\n"
        (left / "large.txt").write_bytes(long_line + b"needle\n")
        (right / "large.txt").write_bytes(long_line + b"other\n")
        self.window._load_panel(0, "folder", left)
        self.window._load_panel(1, "folder", right)
        self.wait_until(lambda: self.window._folder_worker is None and "large.txt" in self.window._folder_rows)
        dialog = FolderContentSearchDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.query.setText("needle")
        dialog._start()
        self.wait_until(lambda: dialog.search_button.isEnabled())
        dialog._open_hit(dialog.results.topLevelItem(0), 0)
        self.wait_until(lambda: self.window._text_worker is None and
                        self.window._pending_folder_content_hit is None)
        self.assertEqual(self.window.panels[0]["editor"].textCursor().selectedText(), "needle")


if __name__ == "__main__":
    unittest.main()
