"""Restart recovery must preserve context without trusting old review results."""
import json
import os
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from d1ffler.comparison_context import RetainedReports, cursor_location, target_record
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.folder_sync_dialogs import SyncReportDialog
from d1ffler.recovery import RecoveryStore
from d1ffler.review_notes import content_fingerprint, hunk_anchor, source_key


class ComparisonContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings_path = self.root / "settings.json"
        self.windows = []
        self.addCleanup(self.cleanup_windows)

    def window(self):
        window = DiffApp(deepcopy(DEFAULT_SETTINGS), self.settings_path)
        self.windows.append(window)
        return window

    def cleanup_windows(self):
        for window in self.windows:
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def files(self, left="same\nleft\nsame\nlast left\n", right="same\nright\nsame\nlast right\n"):
        window = self.window()
        for index, text in enumerate((left, right)):
            path = self.root / f"{index}.txt"
            path.write_text(text, encoding="utf-8")
            window._load_panel(index, "file", path)
        self.wait_until(lambda: window._text_worker is None)
        return window

    def folders(self, *, archive=False):
        window = self.window()
        for index in range(2):
            root = self.root / str(index)
            (root / "nested").mkdir(parents=True)
            for name in ("chosen.txt", "other.txt"):
                (root / "nested" / name).write_text(f"source {index}\n", encoding="utf-8")
            if archive and index == 0:
                path = self.root / "left.zip"
                with ZipFile(path, "w") as stream:
                    for file in root.rglob("*.txt"):
                        stream.write(file, file.relative_to(root).as_posix())
                root = path
            window._load_panel(index, "folder", root)
        self.wait_until(lambda: window._folder_worker is None and not window._folder_display_pending)
        return window

    def note(self, window, relative=None, *, hunk=False):
        sources = window._review_sources()
        note = {"id": str(len(window.review_notes)), "source_key": source_key(sources),
                "kind": "hunk" if hunk else "path", "path": relative, "state": "Accepted", "text": "review",
                "fingerprint": content_fingerprint(sources, relative)}
        if hunk:
            note["anchor"] = hunk_anchor(window._aligned_rows,
                                         [window._panel_text(p).splitlines() for p in window.panels], window._hunks[0])
        window.review_notes.append(note)
        return note

    def test_recovery_restores_rules_name_search_notes_and_historical_reports(self):
        first = self.files()
        self.note(first)
        first._active_workspace_name = "Release review"
        first.settings.update(excluded_paths="cache/**", ignore_whitespace=True, ignore_line_endings=True,
                              ignore_gitignore=True, follow_internal_links=True)
        first.workspace_text_rules = [{"name": "timestamps", "scope": "*.txt", "pattern": r"\d+", "mode": "match", "enabled": True}]
        first.metadata_compare_rule = True
        first.search_input.setText("left")
        first.search_case.setChecked(True)
        first.search_whole_word.setChecked(True)
        first._open_find()
        first.replacement_input.setText("replacement")
        first.sync_reports = [{"started_at": "old", "outcomes": [{"status": "completed", "path": "a.txt"}]}]
        first.restore_reports = [{"started_at": "restore", "outcomes": []}]
        first.panels[0]["editor"].setPlainText("unsaved edits\n")
        self.assertTrue(first._write_recovery_snapshot())
        first._recovery_timer.stop()
        first._recovery_store.release()
        snapshot = RecoveryStore(self.settings_path).snapshots()[0][1]
        self.assertEqual(snapshot["version"], 2)
        self.assertNotIn("sync_reports", snapshot["context"])
        second = self.window()
        self.assertTrue(second._restore_recovery(snapshot))
        self.wait_until(lambda: not second._context_jobs)
        self.assertEqual(second._active_workspace_name, "Release review")
        self.assertEqual(second.settings["excluded_paths"], "cache/**")
        self.assertTrue(second.settings["ignore_whitespace"])
        self.assertTrue(second.settings["ignore_line_endings"])
        self.assertTrue(second.settings["ignore_gitignore"])
        self.assertTrue(second.settings["follow_internal_links"])
        self.assertEqual(second.workspace_text_rules, first.workspace_text_rules)
        self.assertTrue(second.metadata_compare_rule)
        self.assertEqual(second.search_input.text(), "left")
        self.assertTrue(second.search_case.isChecked())
        self.assertTrue(second.search_whole_word.isChecked())
        self.assertTrue(second._search_bar_requested)
        self.assertEqual(second.replacement_input.text(), "replacement")
        self.assertEqual(second._panel_text(second.panels[0]), "unsaved edits\n")
        self.assertTrue(second.review_notes[0]["stale"])
        self.assertTrue(second._notes_with_buffer_status()[0]["buffer_stale"])
        self.assertEqual(second.sync_reports[0]["context_status"], "historical")
        self.assertEqual(second.restore_reports[0]["context_status"], "historical")
        dialog = SyncReportDialog(second.sync_reports[0])
        self.assertTrue(any("Historical report" in label.text() for label in dialog.findChildren(QLabel)))
        dialog.deleteLater()

    def test_named_workspace_resumes_unicode_cursor_hunk_and_scroll(self):
        left = "\n".join(f"row {index} 😀 " + "a" * 150 for index in range(90))
        first = self.files(left, left.replace("row 70", "changed 70"))
        first.show()
        self.app.processEvents()
        first._navigate_change(1)
        editor = first.panels[1]["editor"]
        block = editor.document().findBlockByNumber(70)
        cursor = QTextCursor(editor.document())
        cursor.setPosition(block.position() + 10)
        cursor.setPosition(block.position() + 14, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        editor.verticalScrollBar().setValue(60)
        editor.horizontalScrollBar().setValue(30)
        snapshot = first._workspace_snapshot("Review")
        second = self.window()
        second.show()
        self.app.processEvents()
        second._load_workspace(snapshot)
        self.assertEqual(second._change_index, first._change_index)
        self.assertEqual(second._active_panel_index, 1)
        restored = second.panels[1]["editor"]
        self.assertEqual(cursor_location(restored, restored.textCursor().position()), snapshot["view"]["editors"][1]["cursor"])
        self.assertEqual(restored.textCursor().selectedText(), editor.textCursor().selectedText())
        self.assertEqual(restored.verticalScrollBar().value(), snapshot["view"]["editors"][1]["vertical"])
        self.assertEqual(restored.horizontalScrollBar().value(), snapshot["view"]["editors"][1]["horizontal"])

    def test_background_text_layout_restores_position_after_completion(self):
        text = "\n".join(f"{index}: " + "x" * 80 for index in range(950))
        first = self.files(text, text.replace("800:", "different:"))
        editor = first.panels[1]["editor"]
        cursor = QTextCursor(editor.document().findBlockByNumber(800))
        cursor.movePosition(QTextCursor.MoveOperation.Right, n=5)
        editor.setTextCursor(cursor)
        snapshot = first._workspace_snapshot("Large")
        second = self.window()
        second._load_workspace(snapshot)
        resnapshot = second._workspace_snapshot("Still loading")
        self.assertEqual(resnapshot["view"]["editors"], snapshot["view"]["editors"])
        self.wait_until(lambda: second._text_worker is None and second._pending_context_view is None)
        restored = second.panels[1]["editor"]
        self.assertEqual(cursor_location(restored, restored.textCursor().position()), snapshot["view"]["editors"][1]["cursor"])

    def test_folder_selection_expansion_filters_and_preview_resume(self):
        first = self.folders()
        first.folder_path_search.setText("nested")
        first.folder_status_combo.setCurrentIndex(first.folder_status_combo.findData("modified"))
        first._select_folder_target("file", "nested/chosen.txt")
        first._folder_tree_folders[0]["nested"].setExpanded(True)
        snapshot = first._workspace_snapshot("Folders")
        second = self.window()
        second._load_workspace(snapshot)
        self.wait_until(lambda: second._folder_worker is None and not second._folder_display_pending)
        self.assertEqual(second.folder_path_search.text(), "nested")
        self.assertEqual(second._folder_status_filter, "modified")
        self.assertEqual(second.panels[0]["tree"].currentItem().data(0, Qt.ItemDataRole.UserRole), "nested/chosen.txt")
        self.assertTrue(second._folder_tree_folders[0]["nested"].isExpanded())
        first._open_folder_item(first.panels[0]["widget"], first._folder_tree_items[0]["nested/chosen.txt"])
        preview = first._workspace_snapshot("Preview")
        third = self.window()
        third._load_workspace(preview)
        self.wait_until(lambda: third._folder_worker is None and third._pending_context_preview is None)
        self.assertFalse(third.folder_back_button.isHidden())
        self.assertEqual(third.panels[0]["editing_path"], self.root / "0/nested/chosen.txt")
        self.assertEqual(third._panel_text(third.panels[1]), "source 1\n")

    def test_zip_preview_resumes_from_current_inventory(self):
        first = self.folders(archive=True)
        first._open_folder_item(first.panels[0]["widget"], first._folder_tree_items[0]["nested/chosen.txt"])
        snapshot = first._workspace_snapshot("ZIP")
        second = self.window()
        second._load_workspace(snapshot)
        self.wait_until(lambda: second._folder_worker is None and second._pending_context_preview is None)
        self.assertFalse(second.folder_back_button.isHidden())
        self.assertEqual(second._panel_text(second.panels[0]), "source 0\n")
        self.assertTrue(second.panels[0]["editor"].isReadOnly())

    def test_removed_preview_and_unsafe_paths_fall_back_to_trees(self):
        first = self.folders()
        first._open_folder_item(first.panels[0]["widget"], first._folder_tree_items[0]["nested/chosen.txt"])
        snapshot = first._workspace_snapshot("Missing")
        for index in range(2):
            (self.root / str(index) / "nested/chosen.txt").unlink()
        for preview in (snapshot["view"]["preview"], {"kind": "file", "key": "../../outside.txt"}):
            with self.subTest(preview=preview):
                data = deepcopy(snapshot)
                data["view"]["preview"] = preview
                second = self.window()
                second._load_workspace(data)
                self.wait_until(lambda: second._folder_worker is None and not second._folder_display_pending)
                self.assertTrue(second.folder_back_button.isHidden())
                self.assertIsNone(second.panels[0]["editing_path"])

    def test_changed_sources_invalidate_position_notes_and_hunks(self):
        first = self.files()
        self.note(first, hunk=True)
        first._navigate_change(1)
        snapshot = first._workspace_snapshot("Changed")
        first.panels[0]["path"].write_text("completely changed\n", encoding="utf-8")
        second = self.window()
        second._load_workspace(snapshot)
        self.wait_until(lambda: not second._context_jobs)
        self.assertEqual(second._change_index, -1)
        self.assertTrue(second.review_notes[0]["stale"])
        self.assertTrue(second.review_notes[0]["hunk_stale"])
        self.assertIn("stale", second.context_notice.text())

    def test_unchanged_notes_revalidate_but_new_rules_invalidate_hidden_hunk(self):
        first = self.files()
        self.note(first, hunk=True)
        snapshot = first._workspace_snapshot("Notes")
        second = self.window()
        second._load_workspace(snapshot)
        self.wait_until(lambda: not second._context_jobs)
        self.assertFalse(second.review_notes[0]["stale"])
        snapshot["text_rules"] = [{"name": "ignore", "scope": "*", "pattern": "left|right", "mode": "match", "enabled": True}]
        third = self.window()
        third._load_workspace(snapshot)
        self.wait_until(lambda: not third._context_jobs)
        self.assertTrue(third.review_notes[0]["stale"])

    def test_reports_stored_once_and_missing_or_corrupt_references_retained(self):
        first = self.files()
        first.sync_reports = [{"started_at": "old", "outcomes": [{"status": "completed", "path": "a"}]}]
        snapshot = first._workspace_snapshot("Reports")
        first.panels[0]["editor"].setPlainText("unsaved")
        recovery = first._recovery_snapshot()
        store = RetainedReports(self.settings_path)
        self.assertEqual(len(list(store.directory.glob("*.json"))), 1)
        self.assertEqual(snapshot["report_refs"], recovery["context"]["report_refs"])
        self.assertNotIn("outcomes", json.dumps(snapshot))
        reference = snapshot["report_refs"]["sync"][0]
        for corrupt in (True, False):
            path = store.directory / f"{reference['sha256']}.json"
            if corrupt:
                path.write_text('{"outcomes": []}', encoding="utf-8")
            else:
                path.unlink()
            second = self.window()
            second._load_workspace(snapshot)
            self.assertEqual(second.sync_reports[0]["context_status"], "unavailable")
            self.assertEqual(second._workspace_snapshot("Retained")["report_refs"]["sync"][0], reference)

    def test_named_save_preserves_dirty_buffers_and_never_embeds_them(self):
        first = self.files()
        first.panels[0]["editor"].setPlainText("PRIVATE UNSAVED BUFFER")
        with patch("d1ffler.workspace_mixin.QInputDialog.getText", return_value=("Named", True)):
            first._save_named_workspace()
        snapshot = first.settings["pinned_workspaces"][0]
        self.assertNotIn("PRIVATE UNSAVED BUFFER", json.dumps(snapshot))
        self.assertTrue(first.panels[0]["dirty"])
        second = self.window()
        second._load_workspace(snapshot)
        self.assertEqual(second._panel_text(second.panels[0]), "same\nleft\nsame\nlast left\n")
        self.assertFalse(second.panels[0]["dirty"])

    def test_legacy_recovery_and_unknown_optional_context_are_safe(self):
        first = self.window()
        first.panels[0]["editor"].setPlainText("legacy edits")
        snapshot = first._recovery_snapshot()
        snapshot.pop("context")
        snapshot["version"] = 1
        store = RecoveryStore(self.settings_path)
        store.write(snapshot)
        store.release()
        second = self.window()
        self.assertTrue(second._restore_recovery(store.snapshots()[0][1]))
        self.assertEqual(second._panel_text(second.panels[0]), "legacy edits")
        snapshot["context"] = {"context_version": 999, "comparison_options": {"ignore_whitespace": True}}
        third = self.window()
        self.assertTrue(third._restore_recovery(snapshot))
        self.assertFalse(third.settings["ignore_whitespace"])
        self.assertIn("unsupported", third.context_notice.text())

    def test_report_write_failure_preserves_previous_recovery(self):
        first = self.files()
        first.panels[0]["editor"].setPlainText("unsaved")
        self.assertTrue(first._write_recovery_snapshot())
        original = first._recovery_store.session_path.read_bytes()
        first.sync_reports = [{"outcomes": []}]
        with patch("d1ffler.comparison_context.atomic_write", side_effect=PermissionError("read-only reports")):
            self.assertFalse(first._write_recovery_snapshot())
        self.assertEqual(first._recovery_store.session_path.read_bytes(), original)
        self.assertIn("could not be backed up", first.status.text())

    def test_repeated_hunk_anchor_resumes_the_selected_occurrence(self):
        first = self.files("same\nleft\nsame\nleft\n", "same\nright\nsame\nright\n")
        self.assertEqual(len(first._hunks), 2)
        first._navigate_change(1)
        first._navigate_change(1)
        second = self.window()
        second._load_workspace(first._workspace_snapshot("Repeated"))
        self.assertEqual(second._change_index, 1)

    def test_recovered_folder_preview_keeps_clean_side_encoding(self):
        first = self.folders()
        (self.root / "1/nested/chosen.txt").write_bytes("café\n".encode("cp1252"))
        first._open_folder_item(first.panels[0]["widget"], first._folder_tree_items[0]["nested/chosen.txt"])
        first.panels[0]["editor"].setPlainText("unsaved")
        second = self.window()
        self.assertTrue(second._restore_recovery(first._recovery_snapshot()))
        self.assertEqual(second.panels[1]["encoding"], "cp1252")
        self.assertEqual(second._panel_text(second.panels[1]), "café\n")
        self.assertFalse(second.panels[1]["dirty"])

    def test_context_changes_update_pending_recovery_without_another_text_edit(self):
        first = self.files()
        first.panels[0]["editor"].setPlainText("unsaved")
        first._write_recovery_snapshot()
        first._recovery_timer.stop()
        first.search_input.setText("new search")
        self.assertTrue(first._recovery_timer.isActive())
        first._write_recovery_snapshot()
        saved = json.loads(first._recovery_store.session_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["context"]["view"]["search_query"], "new search")

    def test_legacy_embedded_reports_load_as_history_and_upgrade_to_references(self):
        first = self.files()
        legacy = {"name": "Legacy", "panels": first._workspace_sources(),
                  "sync_reports": [{"started_at": "previous", "outcomes": [{"status": "completed"}]}]}
        second = self.window()
        second._load_workspace(legacy)
        self.assertEqual(second.sync_reports[0]["outcomes"][0]["status"], "completed")
        self.assertEqual(second.sync_reports[0]["context_status"], "historical")
        self.assertIn("report_refs", second._workspace_snapshot("Upgraded"))

    def test_recovered_merge_result_is_stale_when_a_clean_disk_input_changes(self):
        first = self.window()
        first.merge_mode_button.setChecked(True)
        for index, text in enumerate(("base\n", "local\n", "incoming\n")):
            path = self.root / f"merge-{index}.txt"
            path.write_text(text, encoding="utf-8")
            first._load_panel(index, "file", path)
        first.panels[3]["editor"].setPlainText("reviewed result\n")
        snapshot = first._recovery_snapshot()
        (self.root / "merge-0.txt").write_text("new base\n", encoding="utf-8")
        second = self.window()
        self.assertTrue(second._restore_recovery(snapshot))
        self.assertEqual(second._panel_text(second.panels[3]), "reviewed result\n")
        self.assertTrue(second.merge_stale)

    def test_note_validation_failure_is_visible_and_keeps_notes_stale(self):
        first = self.files()
        self.note(first)
        snapshot = first._workspace_snapshot("Failed check")
        second = self.window()
        with patch("d1ffler.comparison_context.content_fingerprint", side_effect=RuntimeError("check failed")):
            second._load_workspace(snapshot)
            self.wait_until(lambda: not second._context_jobs)
        self.assertTrue(second.review_notes[0]["stale"])
        self.assertIn("could not be checked", second.context_notice.text())


if __name__ == "__main__":
    unittest.main()
