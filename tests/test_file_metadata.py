"""Folder metadata status, display, exports, and sync separation."""

import csv
import os
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from d1ffler.comparison_export import folder_csv_chunks, folder_html_chunks
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp, folder_inventory
from d1ffler.file_metadata import metadata_differences, metadata_snapshot
from d1ffler.folder_archives import ArchiveMember
from d1ffler.folder_sync import build_items


class MetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.left = self.root / "left"
        self.right = self.root / "right"
        self.left.mkdir()
        self.right.mkdir()
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

    def test_metadata_only_rule_filter_details_export_and_workspace(self):
        a, b = self.left / "same.txt", self.right / "same.txt"
        a.write_text("same", encoding="utf-8")
        b.write_text("same", encoding="utf-8")
        os.utime(a, ns=(1_600_000_000_000_000_000,) * 2)
        os.utime(b, ns=(1_700_000_000_000_000_000,) * 2)
        self.window._load_panel(0, "folder", self.left)
        self.window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("same.txt") == ["identical", "identical"])
        self.window.metadata_compare_rule = True
        self.window._refresh_diff()
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("same.txt") == ["identical", "metadata"])
        self.assertEqual(self.window._folder_status_counts["metadata"], 1)
        self.window.folder_status_combo.setCurrentIndex(
            self.window.folder_status_combo.findData("metadata"))
        self.assertEqual(self.window._folder_visible_count, 1)
        self.window.folder_status_combo.setCurrentIndex(0)
        self.window.changed_files_button.setChecked(True)
        self.assertEqual(self.window._folder_visible_count, 1)
        self.wait_until(lambda: "same.txt" in self.window._folder_tree_items[1])
        item = self.window._folder_tree_items[1]["same.txt"]
        self.assertIn("Metadata only", item.text(1))
        self.window.panels[1]["tree"].setCurrentItem(item)
        self.assertIn("Metadata differs: modified time", self.window.panels[1]["metadata_display"].text())
        self.assertIn("UTC", self.window.panels[1]["metadata_display"].text())
        snapshot = self.window._comparison_export_snapshot(True, False, False, True)
        csv_text = "".join(chunk for chunk, _ in folder_csv_chunks(snapshot, lambda: False))
        rows = list(csv.reader(csv_text.splitlines()))
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))
        self.assertIn("Modified in", csv_text)
        self.assertIn("metadata", csv_text)
        self.assertIn("Compare file metadata", csv_text)
        html_text = "".join(chunk for chunk, _ in folder_html_chunks(snapshot, lambda: False))
        self.assertIn("Metadata paths", html_text)
        self.assertIn("UTC", html_text)
        workspace = self.window._workspace_snapshot("Metadata comparison")
        self.assertTrue(workspace["compare_metadata"])
        self.assertEqual(self.window._review_report_context([])["counts"]["Metadata-only paths"], 1)
        self.assertEqual(build_items(self.left, self.right, {"same.txt": a}, {"same.txt": b})[0].status,
                         "identical")
        self.window.metadata_compare_rule = False
        self.window._refresh_diff()
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("same.txt") == ["identical", "identical"])
        self.window._load_workspace(workspace)
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("same.txt") == ["identical", "metadata"])

    def test_zip_time_and_permissions_are_unavailable(self):
        file = self.right / "same.txt"
        file.write_text("same", encoding="utf-8")
        archive = self.root / "source.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("same.txt", "same")
        entries, _issues = folder_inventory(archive, [])
        member = entries["same.txt"]
        self.assertIsInstance(member, ArchiveMember)
        metadata = metadata_snapshot(member)
        self.assertEqual(metadata["size"], 4)
        self.assertIsNone(metadata["modified_ns"])
        self.assertIsNone(metadata["permission_key"])
        self.assertEqual(metadata_differences(metadata, metadata_snapshot(file)), [])
        self.window.metadata_compare_rule = True
        self.window._load_panel(0, "folder", archive)
        self.window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("same.txt") == ["identical", "identical"])
        self.assertIn("unavailable for ZIP", self.window._folder_metadata["same.txt"]["panels"][0]["reason"])

    def test_partial_export_separates_pending_and_metadata_only_rows(self):
        snapshot = {"sources": [{"name": "Base"}, {"name": "Other"}],
                    "rows": [("ready.txt", ["identical", "metadata"]), ("waiting.txt", None)],
                    "moves": [], "partial": True, "include_metadata": True,
                    "rules": {"Compare file metadata": True},
                    "metadata": {"ready.txt": {"panels": [
                        {"size": 4, "modified": "2020-01-01 00:00 UTC+00:00", "permissions": "Writable"},
                        {"size": 4, "modified": "2021-01-01 00:00 UTC+00:00", "permissions": "Writable"}],
                        "content_statuses": ["identical", "identical"]}}}
        content = "".join(chunk for chunk, _ in folder_csv_chunks(snapshot, lambda: False))
        rows = list(csv.reader(content.splitlines()))
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))
        self.assertTrue(any(row[:3] == ["meta", "Comparison state", "PARTIAL"] for row in rows))
        self.assertTrue(any(row[:3] == ["meta", "Metadata paths", "1"] for row in rows))
        self.assertTrue(any(row[:3] == ["meta", "Pending paths", "1"] for row in rows))
        self.assertTrue(any(row[:3] == ["file", "waiting.txt", "pending"] for row in rows))

    def test_content_and_metadata_changes_remain_distinct(self):
        a, b = self.left / "changed.txt", self.right / "changed.txt"
        a.write_text("left", encoding="utf-8")
        b.write_text("right", encoding="utf-8")
        os.utime(a, ns=(1_600_000_000_000_000_000,) * 2)
        os.utime(b, ns=(1_700_000_000_000_000_000,) * 2)
        self.window.metadata_compare_rule = True
        self.window._load_panel(0, "folder", self.left)
        self.window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("changed.txt") == ["identical", "modified"])
        self.wait_until(lambda: "changed.txt" in self.window._folder_tree_items[1])
        self.assertIn("metadata differs", self.window._folder_tree_items[1]["changed.txt"].text(1))
        self.assertEqual(self.window._folder_metadata["changed.txt"]["content_statuses"][1], "modified")

    @unittest.skipUnless(os.name != "nt", "POSIX permission bits")
    def test_permission_difference_is_distinct_from_content(self):
        a, b = self.left / "script.sh", self.right / "script.sh"
        a.write_text("echo same\n", encoding="utf-8")
        b.write_text("echo same\n", encoding="utf-8")
        stamp = 1_600_000_000_000_000_000
        os.utime(a, ns=(stamp, stamp))
        os.utime(b, ns=(stamp, stamp))
        a.chmod(0o644)
        b.chmod(0o755)
        self.window.metadata_compare_rule = True
        self.window._load_panel(0, "folder", self.left)
        self.window._load_panel(1, "folder", self.right)
        self.wait_until(lambda: self.window._folder_worker is None and
                        self.window._folder_rows.get("script.sh") == ["identical", "metadata"])
        self.assertEqual(self.window._folder_metadata["script.sh"]["changed_fields"][1], ["permissions"])

    @unittest.skipUnless(os.name == "nt", "Windows read-only attribute")
    def test_windows_permission_rule_uses_read_only_state(self):
        a, b = self.left / "same.txt", self.right / "same.txt"
        a.write_text("same", encoding="utf-8")
        b.write_text("same", encoding="utf-8")
        self.addCleanup(lambda: a.chmod(stat.S_IREAD | stat.S_IWRITE))
        a.chmod(stat.S_IREAD)
        b.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.assertIn("permissions", metadata_differences(metadata_snapshot(a), metadata_snapshot(b)))


if __name__ == "__main__":
    unittest.main()
