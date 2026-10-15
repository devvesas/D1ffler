"""Read-only ZIP folder sources and unsafe entry handling."""

import os
import queue
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp, FolderComparisonWorker, compare_folders, folder_inventory
from d1ffler.folder_archives import ArchiveIssue, ArchiveMember


class ZipSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, name, entries):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, content in entries.items():
                archive.writestr(relative, content)
        return path

    def results(self, *roots):
        messages = queue.Queue()
        worker = FolderComparisonWorker(list(roots), [], False, False, False, {},
                                        threading.Event(), messages)
        worker.run()
        all_messages = []
        while not messages.empty():
            message = messages.get_nowait()
            all_messages.append(message)
            if message[0] in {"batch", "moves"}:
                worker.permits.release()
        rows = {relative: statuses for message in all_messages if message[0] == "batch"
                for relative, statuses in message[1] if statuses is not None}
        moves = [match for message in all_messages if message[0] == "moves" for match in message[1]]
        return rows, moves, all_messages

    def test_zip_vs_zip_and_folder_compare_internal_paths(self):
        left = self.archive("left.zip", {"same.txt": "same", "edit.txt": "old", "only-left.txt": "left"})
        right = self.archive("right.zip", {"same.txt": "same", "edit.txt": "new", "only-right.txt": "right"})
        rows, _moves, _messages = self.results(left, right)
        self.assertEqual(rows["same.txt"], ["identical", "identical"])
        self.assertEqual(rows["edit.txt"], ["identical", "modified"])
        self.assertEqual(rows["only-left.txt"], ["identical", "removed"])
        self.assertEqual(rows["only-right.txt"], ["removed", "added"])
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "same.txt").write_text("same", encoding="utf-8")
        (folder / "edit.txt").write_text("new", encoding="utf-8")
        rows, _moves, _messages = self.results(right, folder)
        self.assertEqual(rows["same.txt"], ["identical", "identical"])
        self.assertEqual(rows["edit.txt"], ["identical", "identical"])
        self.assertEqual({item.relative_path: item.status for item in compare_folders(right, folder)}["same.txt"],
                         "identical")

    def test_archive_entries_are_opened_on_demand_and_moves_are_detected(self):
        left = self.archive("left.zip", {"old/name.txt": "content"})
        right = self.archive("right.zip", {"new/name.txt": "content"})
        entries, _ = folder_inventory(left, [])
        member = entries["old/name.txt"]
        self.assertIsInstance(member, ArchiveMember)
        self.assertEqual(member.read_bytes(), b"content")
        rows, moves, _messages = self.results(left, right)
        self.assertEqual(rows["old/name.txt"], ["identical", "removed"])
        self.assertEqual(len(moves), 1)
        self.assertEqual((moves[0].from_path, moves[0].to_path), ("old/name.txt", "new/name.txt"))
        with self.assertRaises(OSError):
            with member.open("wb"):
                pass

    def test_unsafe_duplicate_and_oversize_entries_are_error_rows(self):
        archive = self.archive("unsafe.zip", {
            "../escape.txt": "bad", "/absolute.txt": "bad",
            "docs/./same.txt": "one", "docs/same.txt": "two",
            "compressed.txt": "x" * (1024 * 1024),
        })
        entries, _ = folder_inventory(archive, [])
        self.assertTrue(any(relative.startswith("[ZIP errors]/") for relative in entries))
        self.assertIsInstance(entries["docs/same.txt"], ArchiveIssue)
        self.assertIsInstance(entries["compressed.txt"], ArchiveIssue)
        self.assertFalse((self.root / "escape.txt").exists())
        folder = self.root / "empty"
        folder.mkdir()
        rows, _moves, _messages = self.results(archive, folder)
        self.assertEqual(rows["docs/same.txt"][0], "error")
        self.assertTrue(any(statuses[0] == "error" for relative, statuses in rows.items()
                            if relative.startswith("[ZIP errors]/")))

    def test_corrupt_member_is_reported_as_error_even_when_unmatched(self):
        archive = self.archive("corrupt.zip", {"broken.txt": "payload"})
        raw = bytearray(archive.read_bytes())
        central = raw.rfind(b"PK\x01\x02")
        self.assertGreaterEqual(central, 0)
        raw[central + 16] ^= 1  # Recorded CRC no longer matches the member bytes.
        archive.write_bytes(raw)
        folder = self.root / "empty"
        folder.mkdir()
        rows, _moves, _messages = self.results(archive, folder)
        self.assertEqual(rows["broken.txt"][0], "error")

    def test_encrypted_entry_is_visible_as_error(self):
        archive = self.archive("encrypted.zip", {"secret.txt": "private"})
        raw = bytearray(archive.read_bytes())
        local = raw.find(b"PK\x03\x04")
        central = raw.rfind(b"PK\x01\x02")
        self.assertGreaterEqual(local, 0)
        self.assertGreaterEqual(central, 0)
        raw[local + 6] |= 1
        raw[central + 8] |= 1
        archive.write_bytes(raw)
        entries, _ = folder_inventory(archive, [])
        self.assertIsInstance(entries["secret.txt"], ArchiveIssue)
        self.assertIn("Encrypted", entries["secret.txt"].detail)
        excluded_entries, _ = folder_inventory(archive, ["*.txt"])
        self.assertIsInstance(excluded_entries["secret.txt"], ArchiveIssue)

    def test_zip_symlink_and_declared_oversize_entry_are_errors(self):
        archive = self.root / "special.zip"
        link = zipfile.ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr(link, "../outside.txt")
            writer.writestr("huge.txt", "small")
        raw = bytearray(archive.read_bytes())
        central = raw.rfind(b"PK\x01\x02")
        self.assertGreaterEqual(central, 0)
        raw[central + 24:central + 28] = (65 * 1024 * 1024).to_bytes(4, "little")
        archive.write_bytes(raw)
        entries, _ = folder_inventory(archive, [])
        self.assertIsInstance(entries["link.txt"], ArchiveIssue)
        self.assertIn("symbolic link", entries["link.txt"].detail)
        self.assertIsInstance(entries["huge.txt"], ArchiveIssue)
        self.assertIn("64 MiB", entries["huge.txt"].detail)


class ZipSourceGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "folder"
        self.folder.mkdir()
        (self.folder / "file.txt").write_text("disk", encoding="utf-8")
        self.archive = self.root / "source.zip"
        with zipfile.ZipFile(self.archive, "w") as writer:
            writer.writestr("file.txt", "archive")
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
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

    def test_zip_source_preview_is_read_only_and_sync_is_unavailable(self):
        self.window._load_panel(0, "file", self.archive)
        self.window._load_panel(1, "folder", self.folder)
        self.assertEqual(self.window.panels[0]["kind"], "folder")
        self.wait_until(lambda: self.window._folder_worker is None and
                        "file.txt" in self.window._folder_tree_items[0])
        self.window._open_folder_item(self.window.panels[0]["widget"],
                                      self.window._folder_tree_items[0]["file.txt"])
        self.assertEqual(self.window.panels[0]["editor"].toPlainText(), "archive")
        self.assertTrue(self.window.panels[0]["editor"].isReadOnly())
        self.assertIsNone(self.window.panels[0]["editing_path"])
        self.assertEqual(self.window.panels[1]["editor"].toPlainText(), "disk")
        with patch("d1ffler.diff_tool.QMessageBox.information") as info:
            self.window._open_sync_plan(0)
        info.assert_called_once()
        snapshot = self.window._comparison_export_snapshot(True, False, False)
        self.assertEqual(snapshot["rows"], [("file.txt", ["identical", "modified"])])
        workspace = self.window._workspace_snapshot("ZIP comparison")
        self.window._load_workspace(workspace)
        self.assertEqual(self.window.panels[0]["kind"], "folder")
        self.assertEqual(self.window.panels[0]["path"], self.archive)


if __name__ == "__main__":
    unittest.main()
