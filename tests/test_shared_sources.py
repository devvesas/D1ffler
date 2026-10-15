"""Disk workflows coordinate editors, reviews, and writes across comparison tabs."""

import difflib
import os
import subprocess
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox, QTreeWidgetItem

from d1ffler.comparison_tabs import ComparisonWindow
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.folder_sync import build_items, execute_plan, operation_for
from d1ffler.folder_sync_dialogs import RestorePlanDialog
from d1ffler.git_dialog import CommitDialog
from d1ffler.git_worktrees import GitError, commit_selected
from d1ffler.patch_dialog import PatchDialog
from d1ffler.shared_sources import SourceScope
from d1ffler.sync_plan_dialog import SyncPlanDialog
from d1ffler.unified_patch import PatchError, parse_patch


class SharedSourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.left, self.right = self.root / "left", self.root / "right"
        for folder, text in ((self.left, "source\n"), (self.right, "original\n")):
            (folder / "nested").mkdir(parents=True)
            (folder / "nested/a.txt").write_text(text, encoding="utf-8")
            (folder / "other.txt").write_text("other\n", encoding="utf-8")
        self.source, self.target = self.left / "nested/a.txt", self.right / "nested/a.txt"
        self.host = ComparisonWindow(deepcopy(DEFAULT_SETTINGS), self.root / "settings.json", DiffApp)
        self.view = self.host.open_paths([self.left, self.right], new_tab=False)
        self.addCleanup(self.close_host)
        self.wait_until(lambda: not self.view._folder_jobs)

    def close_host(self):
        for reservation in list(self.host.shared_sources.pending):
            reservation.finish(notify=False)
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.host.close()
        self.host.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, condition):
        deadline = time.monotonic() + 8
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition())

    def file_tab(self, path=None, *, dirty=False):
        view = self.host.open_paths([path or self.target])
        if dirty:
            view.panels[0]["editor"].setPlainText("unsaved in another tab\n")
        return view

    def sync_dialog(self):
        dialog = SyncPlanDialog(self.view, 0, 1, "nested/a.txt")
        self.addCleanup(dialog.reject)
        self.wait_until(lambda: dialog.apply_plan_button.isEnabled())
        return dialog

    def patch_dialog(self):
        raw = "".join(difflib.unified_diff(
            ["original\n"], ["patched\n"], fromfile="a/nested/a.txt", tofile="b/nested/a.txt"))
        dialog = PatchDialog(self.view, raw, parse_patch(raw), self.root / "change.patch")
        dialog.target.setCurrentIndex(next(index for index in range(dialog.target.count())
                                          if dialog.target.itemData(index) == ("folder", 1)))
        self.addCleanup(dialog.reject)
        self.assertTrue(dialog.apply_button.isEnabled(), dialog.summary.text())
        return dialog

    def restore_dialog(self):
        item = next(item for item in build_items(
            self.left, self.right, {"nested/a.txt": self.source}, {"nested/a.txt": self.target}))
        operation = operation_for(item, "left_to_right", self.left, self.right)
        report = execute_plan([operation], self.left, self.right,
                              backup_directory=self.root / "backups").to_dict()
        dialog = RestorePlanDialog(report, self.view)
        self.addCleanup(dialog.reject)
        self.wait_until(lambda: dialog._worker is None)
        dialog._select_ready()
        return dialog

    def git_dialog(self):
        for args in (("init", "-q"), ("config", "user.name", "D1ffler Test"),
                     ("config", "user.email", "test@example.invalid"), ("add", "."),
                     ("commit", "-qm", "initial")):
            subprocess.run(["git", "-C", str(self.left), *args], check=True, capture_output=True)
        self.source.write_text("reviewed commit\n", encoding="utf-8")
        dialog = CommitDialog(self.left, self.view)
        self.addCleanup(dialog.reject)
        self.wait_until(lambda: not dialog._busy)
        dialog.rows["nested/a.txt"].setCheckState(0, Qt.CheckState.Checked)
        dialog._review()
        self.wait_until(lambda: not dialog._busy)
        dialog.message.setPlainText("Reviewed changes")
        self.assertTrue(dialog.create.isEnabled())
        return dialog

    def test_folder_queries_include_nested_and_standalone_editors_only(self):
        standalone = self.file_tab(dirty=True)
        nested = self.host.open_paths([self.right / "nested"])
        self.wait_until(lambda: not nested._folder_jobs)
        row = QTreeWidgetItem(["a.txt"])
        row.setData(0, Qt.ItemDataRole.UserRole, "a.txt")
        row.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        nested._open_folder_item(nested.panels[0]["widget"], row)
        nested.panels[0]["editor"].setPlainText("nested edits\n")
        unrelated = self.file_tab(self.source, dirty=True)
        found = self.host.shared_sources.editors([SourceScope(self.right / "nested/..", folder=True)],
                                                 dirty_only=True)
        self.assertEqual({item.view for item in found}, {standalone, nested})
        self.assertNotIn(unrelated, {item.view for item in found})
        self.assertFalse(SourceScope(self.right, folder=True).overlaps(SourceScope(self.root / "right-other/a.txt")))

    def test_sync_open_cancel_names_all_tabs_and_preserves_every_edit(self):
        first, second = self.file_tab(self.source, dirty=True), self.file_tab(dirty=True)
        before = (self.source.read_bytes(), self.target.read_bytes())
        with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as warning, \
                patch("d1ffler.diff_tool.SyncPlanDialog.exec") as opened:
            self.view._open_sync_plan(0, 1)
        opened.assert_not_called()
        message = warning.call_args.args[2]
        for number, view, path in ((2, first, self.source), (3, second, self.target)):
            self.assertIn(f"Tab {number}", message)
            self.assertIn(str(path), message)
            self.assertTrue(view.panels[0]["dirty"])
            self.assertEqual(view._panel_text(view.panels[0]), "unsaved in another tab\n")
        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)

    def test_commit_open_does_not_bypass_other_tabs(self):
        self.view._set_git_source(self.view.panels[0], {
            "repo": str(self.left), "kind": "branch", "name": "main", "commit": "0" * 40,
            "readonly": False, "managed": False})
        other = self.file_tab(self.source, dirty=True)
        with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as warning, \
                patch("d1ffler.git_source_mixin.CommitDialog.exec") as opened:
            self.view._create_commit_from_panel(0)
        opened.assert_not_called()
        self.assertIn("Tab 2", warning.call_args.args[2])
        self.assertTrue(other.panels[0]["dirty"])

    def test_same_file_buffers_discard_together_and_cancel_changes_nothing(self):
        first, second = self.file_tab(dirty=True), self.file_tab(dirty=True)
        second.panels[0]["editor"].setPlainText("second version\n")
        scopes = [SourceScope(self.target)]
        with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.assertIsNone(self.host.shared_sources.prepare(scopes, self.view, "Test"))
        self.assertEqual(second._panel_text(second.panels[0]), "second version\n")
        with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.assertFalse(self.host.shared_sources.prepare(scopes, self.view, "Test"))
        for view in (first, second):
            self.assertFalse(view.panels[0]["dirty"])
            self.assertEqual(view._panel_text(view.panels[0]), "original\n")
        self.assertEqual(self.target.read_text(), "original\n")

    def test_save_all_refuses_competing_versions_of_one_file(self):
        first, second = self.file_tab(dirty=True), self.file_tab(dirty=True)
        with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Save), \
                patch("d1ffler.shared_sources.QMessageBox.information") as information:
            self.assertIsNone(self.host.shared_sources.prepare([SourceScope(self.target)], self.view, "Test"))
        self.assertIn("same file", information.call_args.args[2])
        self.assertTrue(first.panels[0]["dirty"])
        self.assertTrue(second.panels[0]["dirty"])
        self.assertEqual(self.target.read_text(), "original\n")

    def test_new_edit_during_resolution_prompt_requires_new_decision(self):
        other = self.file_tab(dirty=True)
        def edit_again(*args):
            other.panels[0]["editor"].setPlainText("newer edits\n")
            return QMessageBox.StandardButton.Discard
        with patch("d1ffler.shared_sources.QMessageBox.warning", side_effect=edit_again), \
                patch("d1ffler.shared_sources.QMessageBox.information"):
            self.assertIsNone(self.host.shared_sources.prepare([SourceScope(self.target)], self.view, "Test"))
        self.assertTrue(other.panels[0]["dirty"])
        self.assertEqual(other._panel_text(other.panels[0]), "newer edits\n")

    def test_sync_rechecks_edits_created_in_confirmation_and_cancel_does_not_write(self):
        dialog = self.sync_dialog()
        other = self.file_tab()
        def edit_while_confirming(*args):
            other.panels[0]["editor"].setPlainText("late edits\n")
            return QMessageBox.StandardButton.Yes
        with patch("d1ffler.sync_plan_dialog.QMessageBox.question", side_effect=edit_while_confirming), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel), \
                patch("d1ffler.sync_plan_dialog.execute_plan") as execute:
            dialog._run(False)
        execute.assert_not_called()
        self.assertEqual(self.target.read_text(), "original\n")
        self.assertEqual(other._panel_text(other.panels[0]), "late edits\n")
        self.assertEqual(self.view.sync_reports, [])

    def test_sync_discard_refreshes_all_clean_tabs_and_preserves_unselected_edits(self):
        dialog = self.sync_dialog()
        dirty, clean = self.file_tab(dirty=True), self.file_tab()
        unrelated = self.file_tab(self.right / "other.txt", dirty=True)
        with patch("d1ffler.sync_plan_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard), \
                patch("d1ffler.sync_plan_dialog.SyncReportDialog.exec", return_value=0):
            dialog._run(False)
        for view in (dirty, clean):
            self.assertEqual(view._panel_text(view.panels[0]), "source\n")
            self.assertFalse(view.panels[0]["dirty"])
            self.assertFalse(view.panels[0]["disk_changed"])
        self.assertTrue(unrelated.panels[0]["dirty"])
        self.assertEqual(unrelated._panel_text(unrelated.panels[0]), "unsaved in another tab\n")
        self.assertEqual(self.target.read_text(), "source\n")
        self.assertEqual(self.host.shared_sources.pending, [])

    def test_sync_save_invalidates_review_even_with_overwrite_policy(self):
        dialog = self.sync_dialog()
        dialog.conflict_combo.setCurrentIndex(dialog.conflict_combo.findData("overwrite"))
        other = self.file_tab(dirty=True)
        with patch("d1ffler.sync_plan_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Save), \
                patch("d1ffler.shared_sources.QMessageBox.information"), \
                patch("d1ffler.sync_plan_dialog.execute_plan") as execute:
            dialog._run(False)
        execute.assert_not_called()
        self.assertEqual(self.target.read_text(), "unsaved in another tab\n")
        self.assertFalse(other.panels[0]["dirty"])
        self.assertEqual(self.view.sync_reports, [])

    def test_patch_rechecks_standalone_dirty_target_after_review(self):
        dialog = self.patch_dialog()
        other = self.file_tab(dirty=True)
        with patch("d1ffler.patch_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel), \
                patch("d1ffler.patch_dialog.apply_folder_plan") as execute:
            dialog._apply()
        execute.assert_not_called()
        self.assertEqual(self.target.read_text(), "original\n")
        self.assertTrue(other.panels[0]["dirty"])

    def test_patch_discard_writes_and_refreshes_clean_file_and_folder_previews(self):
        dialog = self.patch_dialog()
        other = self.file_tab(dirty=True)
        nested = self.host.open_paths([self.right / "nested"])
        self.wait_until(lambda: not nested._folder_jobs)
        row = nested._folder_tree_items[0]["a.txt"]
        nested._open_folder_item(nested.panels[0]["widget"], row)
        with patch("d1ffler.patch_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            dialog._apply()
        for view in (other, nested):
            self.assertEqual(view._panel_text(view.panels[0]), "patched\n")
            self.assertFalse(view.panels[0]["dirty"])
        self.assertTrue(nested._folder_stale)
        self.assertEqual(nested.panels[0]["path"], self.right / "nested")

    def test_restore_rechecks_dirty_target_after_review(self):
        dialog = self.restore_dialog()
        other = self.file_tab(dirty=True)
        with patch("d1ffler.folder_sync_dialogs.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel), \
                patch("d1ffler.folder_sync_dialogs.execute_restore") as execute:
            dialog._run(False)
        execute.assert_not_called()
        self.assertEqual(self.target.read_text(), "source\n")
        self.assertTrue(other.panels[0]["dirty"])
        self.assertEqual(self.view.restore_reports, [])

    def test_restore_discard_refreshes_clean_tabs_without_changing_backup(self):
        dialog = self.restore_dialog()
        other, clean = self.file_tab(dirty=True), self.file_tab()
        with patch("d1ffler.folder_sync_dialogs.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard), \
                patch("d1ffler.folder_sync_dialogs.RestoreReportDialog.exec", return_value=0):
            dialog._run(False)
        for view in (other, clean):
            self.assertEqual(view._panel_text(view.panels[0]), "original\n")
            self.assertFalse(view.panels[0]["dirty"])
        self.assertEqual((self.root / "backups/right/nested/a.txt").read_text(), "original\n")

    def test_reservations_block_overlap_and_saves_in_existing_and_new_tabs(self):
        other = self.file_tab()
        coordinator = self.host.shared_sources
        reservation = coordinator.begin([SourceScope(self.right, folder=True)], self.view, "Test write")
        self.addCleanup(lambda: reservation.finish(notify=False))
        self.assertTrue(other.panels[0]["editor"].isReadOnly())
        new = self.file_tab()
        self.assertTrue(new.panels[0]["editor"].isReadOnly())
        with patch("d1ffler.shared_sources.QMessageBox.information") as information:
            self.assertIsNone(coordinator.begin([SourceScope(self.target)], new, "Overlapping write"))
            self.assertFalse(new._save_panel_file(0))
            self.assertFalse(self.view._prepare_close(tab_close=True))
        self.assertIn("still running", information.call_args_list[0].args[2])
        independent = coordinator.begin([SourceScope(self.source)], other, "Other write")
        self.assertIsNotNone(independent)
        independent.finish(notify=False)
        reservation.finish(notify=False)
        self.assertFalse(new.panels[0]["editor"].isReadOnly())
        self.assertFalse(other.panels[0]["editor"].isReadOnly())

    def test_partial_patch_failure_notifies_clean_tabs_and_preserves_new_dirty_buffer(self):
        dialog = self.patch_dialog()
        dirty, clean = self.file_tab(), self.file_tab()
        def partially_write(*args):
            dirty.panels[0]["editor"].setPlainText("edits retained during write\n")
            self.target.write_text("partial write\n", encoding="utf-8")
            raise PatchError("injected failure after a write")
        with patch("d1ffler.patch_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.patch_dialog.QMessageBox.warning"), \
                patch("d1ffler.patch_dialog.apply_folder_plan", side_effect=partially_write):
            dialog._apply()
        self.assertEqual(clean._panel_text(clean.panels[0]), "partial write\n")
        self.assertEqual(dirty._panel_text(dirty.panels[0]), "edits retained during write\n")
        self.assertTrue(dirty.panels[0]["dirty"])
        self.assertTrue(dirty.panels[0]["disk_changed"])
        self.assertEqual(self.host.shared_sources.pending, [])

    def test_normal_save_updates_clean_tabs_but_retains_competing_dirty_buffer(self):
        writer, dirty, clean = self.file_tab(dirty=True), self.file_tab(dirty=True), self.file_tab()
        dirty.panels[0]["editor"].setPlainText("competing edit\n")
        self.assertTrue(writer._save_panel_file(0))
        self.assertEqual(clean._panel_text(clean.panels[0]), "unsaved in another tab\n")
        self.assertFalse(clean.panels[0]["disk_changed"])
        self.assertEqual(dirty._panel_text(dirty.panels[0]), "competing edit\n")
        self.assertTrue(dirty.panels[0]["dirty"])
        self.assertTrue(dirty.panels[0]["disk_changed"])

    def test_deleted_file_notification_keeps_source_reference_and_readonly_placeholder(self):
        clean = self.file_tab()
        self.target.unlink()
        self.host.shared_sources.notify([SourceScope(self.target)])
        panel = clean.panels[0]
        self.assertEqual(panel["path"], self.target)
        self.assertEqual(panel["editing_path"], self.target)
        self.assertIn("File missing on disk", clean._panel_text(panel))
        self.assertTrue(panel["editor"].isReadOnly())
        self.assertFalse(panel["dirty"])

    def test_sync_creation_refreshes_a_clean_missing_folder_preview(self):
        self.target.unlink()
        dialog = self.sync_dialog()
        other = self.host.open_paths([self.left / "nested", self.right / "nested"])
        self.wait_until(lambda: not other._folder_jobs)
        other._open_folder_item(other.panels[0]["widget"], other._folder_tree_items[0]["a.txt"])
        panel = other.panels[1]
        self.assertIsNone(panel["editing_path"])
        self.assertIn("does not exist", other._panel_text(panel))
        with patch("d1ffler.sync_plan_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.sync_plan_dialog.SyncReportDialog.exec", return_value=0):
            dialog._run(False)
        self.assertEqual(other._panel_text(panel), "source\n")
        self.assertEqual(panel["editing_path"], self.target)
        self.assertEqual(panel["path"], self.right / "nested")
        self.assertFalse(panel["editor"].isReadOnly())
        self.assertFalse(panel["dirty"])
        self.assertTrue(other._folder_stale)

    def test_commit_rechecks_late_edits_and_save_requires_new_patch_review(self):
        dialog = self.git_dialog()
        other = self.file_tab(self.source, dirty=True)
        with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel), \
                patch("d1ffler.git_dialog.commit_selected") as commit:
            dialog._commit()
        commit.assert_not_called()
        self.assertIsNone(dialog.commit_hash)
        with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Save), \
                patch("d1ffler.shared_sources.QMessageBox.information"), patch("d1ffler.git_dialog.commit_selected") as commit:
            dialog._commit()
        commit.assert_not_called()
        self.assertIsNone(dialog.preview)
        self.assertFalse(other.panels[0]["dirty"])
        self.assertEqual(self.source.read_text(), "unsaved in another tab\n")
        self.wait_until(lambda: not dialog._busy)

    def test_commit_holds_worktree_reservation_until_background_write_finishes(self):
        dialog = self.git_dialog()
        other = self.file_tab(self.source, dirty=True)
        entered, release = threading.Event(), threading.Event()
        def delayed_commit(*args):
            entered.set()
            release.wait(5)
            return commit_selected(*args)
        try:
            with patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard), \
                    patch("d1ffler.git_dialog.commit_selected", side_effect=delayed_commit):
                dialog._commit()
                self.assertTrue(entered.wait(2))
                self.assertTrue(other.panels[0]["editor"].isReadOnly())
                with patch("d1ffler.shared_sources.QMessageBox.information"):
                    self.assertFalse(other._save_panel_file(0))
                    self.assertIsNone(self.host.shared_sources.begin(
                        [SourceScope(self.source)], other, "Patch write"))
                release.set()
                self.wait_until(lambda: not dialog._busy)
        finally:
            release.set()
        self.assertIsNotNone(dialog.commit_hash)
        self.assertEqual(self.host.shared_sources.pending, [])
        self.assertFalse(other.panels[0]["editor"].isReadOnly())
        self.assertEqual(other._panel_text(other.panels[0]), "reviewed commit\n")

    def test_hardlink_file_scopes_share_identity(self):
        alias = self.root / "alias.txt"
        try:
            os.link(self.target, alias)
        except OSError as exc:
            self.skipTest(str(exc))
        other = self.file_tab(alias, dirty=True)
        self.assertEqual([item.view for item in self.host.shared_sources.editors(
            [SourceScope(self.target)], dirty_only=True)], [other])

    def test_case_aliases_share_file_and_nested_folder_scopes_on_windows(self):
        if os.name == "nt":
            other = self.file_tab(dirty=True)
            alias = Path(str(self.target).upper())
            scopes = [SourceScope(Path(str(self.right).upper()), folder=True)]
            self.assertTrue(SourceScope(alias).overlaps(SourceScope(self.target)))
            self.assertEqual([item.view for item in self.host.shared_sources.editors(
                scopes, dirty_only=True)], [other])

    def test_sync_reservation_blocks_reentrant_write_while_processing_events(self):
        dialog = self.sync_dialog()
        other = self.file_tab()
        attempts = []
        def run_with_attempt(*args, **kwargs):
            self.assertTrue(other.panels[0]["editor"].isReadOnly())
            progress = kwargs["progress"]
            def try_write(*event):
                with patch("d1ffler.shared_sources.QMessageBox.information"):
                    attempts.append(other._save_panel_file(0))
                progress(*event)
            kwargs["progress"] = try_write
            return execute_plan(*args, **kwargs)
        with patch("d1ffler.sync_plan_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.sync_plan_dialog.execute_plan", side_effect=run_with_attempt), \
                patch("d1ffler.sync_plan_dialog.SyncReportDialog.exec", return_value=0):
            dialog._run(False)
        self.assertEqual(attempts, [False])
        self.assertEqual(self.target.read_text(), "source\n")
        self.assertFalse(other.panels[0]["editor"].isReadOnly())

    def test_patch_save_invalidates_review_and_keeps_the_saved_version(self):
        dialog = self.patch_dialog()
        self.file_tab(dirty=True)
        with patch("d1ffler.patch_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Save), \
                patch("d1ffler.shared_sources.QMessageBox.information"), \
                patch("d1ffler.patch_dialog.apply_folder_plan") as execute:
            dialog._apply()
        execute.assert_not_called()
        self.assertEqual(self.target.read_text(), "unsaved in another tab\n")
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_restore_save_invalidates_review_and_keeps_the_saved_version(self):
        dialog = self.restore_dialog()
        self.file_tab(dirty=True)
        with patch("d1ffler.folder_sync_dialogs.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                patch("d1ffler.shared_sources.QMessageBox.warning", return_value=QMessageBox.StandardButton.Save), \
                patch("d1ffler.shared_sources.QMessageBox.information"), \
                patch("d1ffler.folder_sync_dialogs.execute_restore") as execute:
            dialog._run(False)
        execute.assert_not_called()
        self.assertEqual(self.target.read_text(), "unsaved in another tab\n")
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertEqual(dialog.items, [])

    def test_commit_error_releases_shared_sources(self):
        dialog = self.git_dialog()
        other = self.file_tab(self.source)
        for error in (GitError("hook failed"), RuntimeError("unexpected worker failure")):
            with self.subTest(error=type(error).__name__), \
                    patch("d1ffler.git_dialog.commit_selected", side_effect=error), \
                    patch("d1ffler.git_dialog.QMessageBox.warning"):
                dialog._commit()
                self.wait_until(lambda: not dialog._busy)
        self.assertIsNone(dialog.commit_hash)
        self.assertIsNone(dialog.preview)
        self.assertFalse(dialog.create.isEnabled())
        self.assertEqual(self.host.diagnostics.read_events()[-1]["operation"], "Git commit")
        self.assertEqual(self.host.shared_sources.pending, [])
        self.assertFalse(other.panels[0]["editor"].isReadOnly())

    def test_notification_preserves_manually_edited_merge_result_and_marks_it_stale(self):
        merge = self.host.open_paths([self.target, self.target, self.target])
        merge.merge_mode_button.setChecked(True)
        merge.panels[3]["editor"].setPlainText("manual merge result\n")
        self.assertTrue(merge.merge_touched)
        self.target.write_text("new disk source\n", encoding="utf-8")
        self.host.shared_sources.notify([SourceScope(self.target)])
        self.assertTrue(merge.merge_stale)
        self.assertEqual(merge._panel_text(merge.panels[3]), "manual merge result\n")
        self.assertTrue(merge.panels[3]["dirty"])
        self.assertEqual(merge._panel_text(merge.panels[0]), "new disk source\n")

    def test_unchanged_clean_buffers_keep_their_selection_and_undo_history(self):
        other = self.file_tab()
        editor = other.panels[0]["editor"]
        editor.insertPlainText("temporary edit\n")
        editor.undo()
        self.assertFalse(other.panels[0]["dirty"])
        cursor = editor.textCursor()
        cursor.setPosition(2)
        cursor.setPosition(5, cursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        self.host.shared_sources.notify([SourceScope(self.target)])
        self.assertEqual(editor.textCursor().selectedText(), "igi")
        self.assertTrue(editor.document().isRedoAvailable())


if __name__ == "__main__":
    unittest.main()
