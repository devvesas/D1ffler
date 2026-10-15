"""Standalone comparison exports and cancellation safety."""

import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QDialog

from d1ffler.comparison_export import (
    ExportCancelled, folder_csv_chunks, folder_html_chunks, text_html_chunks,
    text_unified_chunks, write_export,
)
from d1ffler.diff_tool import ComparisonExportDialog, DEFAULT_SETTINGS, DiffApp


class ComparisonExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def cleanup_window(self, window):
        window.close()
        window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_text_export_uses_buffer_content_and_escapes_html(self):
        sources = [
            {"name": "Base", "text": "first\n<script>\n"},
            {"name": "Local", "text": "first\nchanged\n"},
            {"name": "Incoming", "text": "first\nthird\n"},
        ]
        unified = "".join(chunk for chunk, _units in text_unified_chunks(sources, lambda: False))
        self.assertIn("--- Base", unified)
        self.assertIn("+++ Local", unified)
        self.assertIn("+++ Incoming", unified)
        self.assertIn("-<script>", unified)
        self.assertNotIn(str(self.root), unified)
        page = "".join(chunk for chunk, _units in text_html_chunks(sources, lambda: False))
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("class=\"line\">2", page)
        self.assertIn("Incoming", page)

    def test_folder_manifest_labels_partial_stale_pending_error_and_moves(self):
        snapshot = {
            "sources": [{"name": "A"}, {"name": "B"}],
            "rows": [("pending.txt", None), ("error.txt", ["identical", "error"]),
                     ("added.txt", ["removed", "added"]),
                     ("same.txt", ["identical", "identical"]),
                     ("linked.txt", ["identical", "modified"])],
            "links": {"linked.txt": [{"kind": "symbolic link", "target": "one.txt", "state": "internal"},
                                      {"kind": "symbolic link", "target": "two.txt", "state": "internal"}]},
            "moves": [{"from_panel": 0, "from_path": "old.txt", "to_panel": 1,
                       "to_path": "new.txt", "sha256": "abc123"}],
            "scope": "Current view (status pending)", "discovered": 7,
            "excluded": 2, "partial": True, "stale": True,
            "rules": {"Ignore whitespace": True},
        }
        content = "".join(chunk for chunk, _units in folder_csv_chunks(snapshot, lambda: False))
        records = list(csv.reader(content.splitlines()))
        self.assertTrue(all(len(row) == len(records[0]) for row in records))
        self.assertTrue(any(row[:3] == ["meta", "Comparison state", "PARTIAL · STALE"] for row in records))
        self.assertTrue(any(row[:5] == ["file", "pending.txt", "pending", "pending", "pending"] for row in records))
        self.assertTrue(any(row[:5] == ["file", "error.txt", "error", "present", "error"] for row in records))
        self.assertTrue(any(row[:5] == ["file", "added.txt", "added", "missing", "added"] for row in records))
        self.assertTrue(any(row[0] == "move" and row[-3:-1] == ["old.txt", "new.txt"] for row in records))
        self.assertTrue(any(row[0] == "file" and row[1] == "linked.txt" and
                            "symbolic link → one.txt (internal)" in row for row in records))
        self.assertTrue(any(row[:3] == ["meta", "Linked paths", "1"] for row in records))
        self.assertNotIn(str(self.root), content)
        page = "".join(chunk for chunk, _units in folder_html_chunks(snapshot, lambda: False))
        self.assertIn("PARTIAL · STALE", page)
        self.assertIn("old.txt → new.txt", page)
        self.assertIn("Ignore whitespace", page)
        self.assertIn("symbolic link → one.txt (internal)", page)

    def test_csv_escapes_spreadsheet_formula_paths(self):
        snapshot = {"sources": [{"name": "A"}, {"name": "B"}],
                    "rows": [("=HYPERLINK(test)", ["identical", "modified"])],
                    "moves": [], "partial": False, "stale": False}
        content = "".join(chunk for chunk, _units in folder_csv_chunks(snapshot, lambda: False))
        record = next(row for row in csv.reader(content.splitlines()) if row[0] == "file")
        self.assertEqual(record[1], "'=HYPERLINK(test)")

    def test_absolute_link_target_requires_path_opt_in(self):
        snapshot = {"sources": [{"name": "A"}, {"name": "B"}],
                    "rows": [("alias", ["identical", "removed"])], "moves": [],
                    "links": {"alias": [{"kind": "junction", "target": r"C:\private\folder",
                                         "state": "external"}, None]}}
        hidden = "".join(chunk for chunk, _ in folder_csv_chunks(snapshot, lambda: False))
        self.assertIn("[absolute target hidden]", hidden)
        self.assertNotIn(r"C:\private\folder", hidden)
        snapshot["include_paths"] = True
        shown = "".join(chunk for chunk, _ in folder_csv_chunks(snapshot, lambda: False))
        self.assertIn(r"C:\private\folder", shown)

    def test_folder_export_includes_archive_issue_details(self):
        snapshot = {"sources": [{"name": "package.zip", "type": "ZIP archive"},
                                {"name": "folder", "type": "folder"}],
                    "rows": [("bad.txt", ["error", "removed"])], "moves": [],
                    "archive_issues": {"bad.txt": ["Encrypted ZIP entry: bad.txt", ""]}}
        content = "".join(chunk for chunk, _ in folder_csv_chunks(snapshot, lambda: False))
        rows = list(csv.reader(content.splitlines()))
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))
        self.assertTrue(any(row[0] == "file" and "Encrypted ZIP entry: bad.txt" in row for row in rows))
        self.assertTrue(any(row[:3] == ["meta", "Panel 1 type", "ZIP archive"] for row in rows))

    def test_cancel_keeps_existing_export_and_removes_temporary_file(self):
        target = self.root / "report.csv"
        target.write_text("previous report", encoding="utf-8")
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 20

        with self.assertRaises(ExportCancelled):
            write_export(target, (("row\n", 1) for _ in range(1000)), cancelled)
        self.assertEqual(target.read_text(encoding="utf-8"), "previous report")
        self.assertEqual(list(self.root.glob(".D1ffler-export-*")), [])

    def test_current_folder_filter_is_frozen_in_snapshot(self):
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        for panel, path in zip(window.panels, (left, right)):
            panel["kind"] = "folder"
            panel["path"] = path
        window._folder_rows = {
            "docs/readme.txt": ["identical", "modified"],
            "docs/notes.txt": None,
            "src/code.py": ["identical", "modified"],
        }
        window._folder_path_query = "docs/"
        window._folder_status_filter = "modified"
        window._folder_partial = True
        selected = window._comparison_export_snapshot(True, True, False)
        self.assertEqual([relative for relative, _ in selected["rows"]], ["docs/readme.txt"])
        self.assertTrue(selected["partial"])
        self.assertNotIn("path", selected["sources"][0])
        all_rows = window._comparison_export_snapshot(True, False, True)
        self.assertEqual(len(all_rows["rows"]), 3)
        self.assertEqual(all_rows["sources"][0]["path"], str(left.resolve()))

    def test_dialog_exports_folder_manifest_without_notes(self):
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        for panel, path in zip(window.panels, (left, right)):
            panel["kind"] = "folder"
            panel["path"] = path
        window._folder_rows = {"changed.txt": ["identical", "modified"]}
        target = self.root / "comparison.csv"
        with patch.object(ComparisonExportDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch(
            "d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(target), "")
        ):
            window._export_comparison()
        self.assertTrue(target.exists())
        self.assertIn("changed.txt", target.read_text(encoding="utf-8"))
        self.assertIn("Exported comparison", window.status.text())

    def test_export_refuses_destination_inside_compared_folder(self):
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        for panel, path in zip(window.panels, (left, right)):
            panel["kind"] = "folder"
            panel["path"] = path
        target = left / "report.csv"
        with patch.object(ComparisonExportDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch(
            "d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(target), "")
        ), patch("d1ffler.diff_tool.QMessageBox.warning") as warning:
            window._export_comparison()
        warning.assert_called_once()
        self.assertFalse(target.exists())

    def test_dialog_exports_live_pasted_text_as_unified_diff(self):
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window, window)
        window.panels[0]["editor"].setPlainText("before\n")
        window.panels[1]["editor"].setPlainText("after\n")
        target = self.root / "comparison.diff"
        with patch.object(ComparisonExportDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch(
            "d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(target), "")
        ):
            window._export_comparison()
        content = target.read_text(encoding="utf-8")
        self.assertIn("-before", content)
        self.assertIn("+after", content)


if __name__ == "__main__":
    unittest.main()
