"""Comparisons retain their own buffers, review state, and worker results."""

import os
import queue
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox, QTreeWidgetItem

from d1ffler.comparison_tabs import (BackgroundTask, ComparisonWindow, MAX_COMPARISON_TABS,
                             MAX_COMPARISON_WORKERS, command_line_source_sets)
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.recovery_dialog import RecoveryDialog
from d1ffler.recovery import MAX_SNAPSHOTS, RecoveryError, RecoveryStore


class ComparisonTabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.window = ComparisonWindow(deepcopy(DEFAULT_SETTINGS), self.root / "settings.json", DiffApp)
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition())

    def files(self, prefix, count=2):
        paths = [self.root / f"{prefix}-{index}.txt" for index in range(count)]
        for index, path in enumerate(paths):
            path.write_text(f"source {index}\n", encoding="utf-8")
        return paths

    def folders(self, prefix):
        paths = [self.root / f"{prefix}-{side}" for side in ("left", "right")]
        for side, path in enumerate(paths):
            path.mkdir()
            (path / "chosen.txt").write_text(str(side), encoding="utf-8")
            (path / "other.txt").write_text("same", encoding="utf-8")
        return paths

    def test_switch_retains_sources_edits_search_cursor_and_active_panel(self):
        paths = self.files("first")
        first = self.window.open_paths(paths, new_tab=False)
        first.panels[1]["editor"].setPlainText("unsaved comparison\nsecond line\n")
        editor = first.panels[1]["editor"]
        cursor = editor.textCursor()
        cursor.setPosition(7)
        editor.setTextCursor(cursor)
        first._active_panel_index = 1
        first.search_input.setText("comparison")
        first.search_case.setChecked(True)
        first._search_bar_requested = True
        first._sync_search_bar_visibility()
        first.review_notes = [{"id": "first-note"}]
        first.sync_reports = [{"outcomes": [{"path": "first", "status": "completed"}]}]
        second = self.window.open_paths(self.files("second"))
        second.search_input.setText("source")
        second.review_notes = [{"id": "second-note"}]
        second.restore_reports = [{"outcomes": [{"path": "second"}]}]
        with patch.object(first, "_save_panel_file", side_effect=AssertionError("switch saved")):
            self.window.comparison_tabs.setCurrentWidget(first)
            self.app.processEvents()
        self.assertEqual([panel["path"] for panel in first.panels], paths)
        self.assertEqual(first._panel_text(first.panels[1]), "unsaved comparison\nsecond line\n")
        self.assertEqual(editor.textCursor().position(), 7)
        self.assertEqual(first._active_panel_index, 1)
        self.assertEqual(first.search_input.text(), "comparison")
        self.assertTrue(first.search_case.isChecked())
        self.assertTrue(first.panels[1]["dirty"])
        self.assertTrue(self.window.comparison_tabs.tabText(0).endswith(" *"))
        self.assertEqual(second.search_input.text(), "source")
        self.assertEqual(first.review_notes, [{"id": "first-note"}])
        self.assertEqual(second.review_notes, [{"id": "second-note"}])
        self.assertEqual(first.sync_reports[0]["outcomes"][0]["path"], "first")
        self.assertEqual(second.restore_reports[0]["outcomes"][0]["path"], "second")
        self.assertEqual(paths[1].read_text(), "source 1\n")

    def test_folder_filters_and_selection_survive_switch(self):
        first = self.window.open_paths(self.folders("one"), new_tab=False)
        self.wait_until(lambda: first._folder_worker is None and "chosen.txt" in first._folder_tree_items[0])
        first.folder_path_search.setText("chosen")
        first.folder_status_combo.setCurrentIndex(first.folder_status_combo.findData("modified"))
        self.wait_until(lambda: "chosen.txt" in first._folder_tree_items[0] and not first._folder_display_pending)
        first.panels[0]["tree"].setCurrentItem(first._folder_tree_items[0]["chosen.txt"])
        second = self.window.open_paths(self.folders("two"))
        self.wait_until(lambda: second._folder_worker is None and "other.txt" in second._folder_tree_items[0])
        second.folder_path_search.setText("other")
        second.folder_status_combo.setCurrentIndex(second.folder_status_combo.findData("identical"))
        self.window.comparison_tabs.setCurrentWidget(first)
        self.app.processEvents()
        self.assertEqual(first.folder_path_search.text(), "chosen")
        self.assertEqual(first.folder_status_combo.currentData(), "modified")
        self.assertEqual(first.panels[0]["tree"].currentItem().text(0), "chosen.txt")
        self.assertEqual(second.folder_path_search.text(), "other")
        self.assertEqual(second.folder_status_combo.currentData(), "identical")

    def test_dirty_tab_cancel_and_discard_affect_only_that_tab(self):
        first = self.window.open_paths(self.files("first"), new_tab=False)
        first.panels[0]["editor"].setPlainText("first unsaved")
        first._write_recovery_snapshot()
        second = self.window.open_paths(self.files("second"))
        second.panels[1]["editor"].setPlainText("second unsaved")
        second._write_recovery_snapshot()
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as prompt:
            self.assertFalse(self.window.close_comparison(0))
        self.assertIn("first-0.txt", prompt.call_args.args[2])
        self.assertNotIn("second-1.txt", prompt.call_args.args[2])
        self.assertEqual(self.window.comparison_tabs.count(), 2)
        self.assertTrue(first.panels[0]["dirty"])
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.window.close_comparison(0))
        self.assertIs(self.window.active_comparison, second)
        self.assertEqual(second._panel_text(second.panels[1]), "second unsaved")
        self.assertTrue(second.panels[1]["dirty"])
        snapshots = second._recovery_store.snapshots(include_active=True)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0][1]["panels"][1]["text"], "second unsaved")

    def test_named_workspace_opens_in_new_tab_and_changes_require_explicit_save(self):
        first = self.window.open_paths(self.files("first"), new_tab=False)
        first.review_notes = [{"id": "saved-note", "source_key": "key", "text": "saved"}]
        with patch("d1ffler.workspace_mixin.QInputDialog.getText", return_value=("Named", True)):
            first._save_named_workspace()
        saved = deepcopy(self.window.settings["pinned_workspaces"][0])
        first.panels[0]["editor"].setPlainText("unsaved text")
        first.review_notes[0]["text"] = "new review"
        first.sync_reports.append({"outcomes": []})
        first._record_recent_workspace()
        self.assertEqual(self.window.settings["pinned_workspaces"][0], saved)
        second = self.window.open_workspace(saved)
        self.assertIsNotNone(second)
        self.assertEqual(self.window.comparison_tabs.count(), 2)
        self.assertEqual(first._panel_text(first.panels[0]), "unsaved text")
        self.assertEqual(second.review_notes[0]["text"], "saved")
        self.assertEqual(second.sync_reports, [])
        second.review_notes[0]["text"] = "second saved"
        with patch("d1ffler.workspace_mixin.QInputDialog.getText", return_value=("Second", True)):
            second._save_named_workspace()
        self.assertEqual(self.window.settings["pinned_workspaces"][0], saved)
        self.assertEqual(self.window.settings["pinned_workspaces"][1]["review_notes"][0]["text"], "second saved")
        self.assertNotIn("unsaved text", str(self.window.settings["pinned_workspaces"]))

    def test_invalid_workspace_does_not_create_tab(self):
        with patch("d1ffler.workspace_mixin.QMessageBox.warning"):
            self.assertIsNone(self.window.open_workspace({"panels": [{"path": "missing"}, None]}))
        self.assertEqual(self.window.comparison_tabs.count(), 1)

    def test_late_background_results_remain_with_their_original_tab(self):
        first = self.window.active_comparison
        second = self.window.new_comparison()
        first._folder_generation = second._folder_generation = 7
        worker = SimpleNamespace(permits=threading.Semaphore(0))
        first._folder_worker = worker
        first._folder_jobs = [(SimpleNamespace(is_alive=lambda: False), worker,
                               queue.Queue(), 7)]
        first._folder_jobs[0][2].put(("batch", [("only-first.txt", ["identical", "modified"])]))
        first._poll_background_jobs()
        self.assertIn("only-first.txt", first._folder_rows)
        self.assertNotIn("only-first.txt", second._folder_rows)
        self.assertIs(self.window.active_comparison, second)
        first._folder_jobs.clear()
        first._folder_worker = None

    def test_worker_limit_and_cancelling_queued_job_does_not_wait(self):
        release = threading.Event()
        self.addCleanup(release.set)
        running, peak = 0, 0
        lock = threading.Lock()

        def run():
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            release.wait(5)
            with lock:
                running -= 1

        jobs = [BackgroundTask(self.window.worker_pool, SimpleNamespace(run=run, cancel_event=threading.Event()))
                for _ in range(5)]
        for job in jobs:
            job.start()
        self.wait_until(lambda: running == MAX_COMPARISON_WORKERS)
        jobs[-1].worker.cancel_event.set()
        before = time.monotonic()
        jobs[-1].join()
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertTrue(jobs[-1].future.cancelled())
        self.assertEqual(peak, MAX_COMPARISON_WORKERS)
        release.set()
        for job in jobs:
            job.join()

    def test_tab_limit_and_live_recovery_snapshots_are_protected(self):
        for index in range(MAX_COMPARISON_TABS):
            view = self.window.active_comparison if index == 0 else self.window.new_comparison()
            view.panels[0]["editor"].setPlainText(f"unfinished {index}")
            view._write_recovery_snapshot()
        self.assertEqual(len(view._recovery_store.snapshots(include_active=True)), MAX_COMPARISON_TABS)
        with patch("d1ffler.comparison_tabs.QMessageBox.information") as notice:
            self.assertIsNone(self.window.new_comparison())
        notice.assert_called_once()
        self.assertEqual(self.window.comparison_tabs.count(), MAX_COMPARISON_TABS)
        self.assertFalse(self.window.new_comparison_button.isEnabled())

    def test_empty_and_clean_tabs_do_not_evict_abandoned_recovery(self):
        abandoned = []
        for index in range(MAX_SNAPSHOTS - 1):
            store = RecoveryStore(self.root / "settings.json")
            self.addCleanup(store.release)
            store.write({"version": 1, "panels": [{"recover": True, "text": f"abandoned {index}"}]})
            store.release()
            abandoned.append(store.session_path)
        for _index in range(MAX_SNAPSHOTS - 1):
            self.window.new_comparison()
        dirty = self.window.active_comparison
        # One clean file comparison and three empty comparisons have no snapshot.
        clean = self.window.comparisons[0]
        clean.open_paths(self.files("clean"))
        dirty.panels[0]["editor"].setPlainText("new unfinished edits")
        self.assertTrue(dirty._write_recovery_snapshot())
        expected = set(abandoned) | {dirty._recovery_store.session_path}
        self.assertEqual({path for path, _ in dirty._recovery_store.snapshots(include_active=True)}, expected)
        empty = self.window.new_comparison()
        self.assertFalse(empty._recovery_store.session_path.exists())
        self.assertTrue(dirty._write_recovery_snapshot())
        self.assertEqual({path for path, _ in dirty._recovery_store.snapshots(include_active=True)}, expected)
        self.assertTrue(self.window.close_comparison(self.window.comparison_tabs.indexOf(empty)))
        self.assertTrue(dirty._write_recovery_snapshot())
        self.assertEqual({path for path, _ in dirty._recovery_store.snapshots(include_active=True)}, expected)

    def test_pasted_tab_close_prompts_and_window_close_keeps_recovery(self):
        first = self.window.active_comparison
        first.panels[0]["editor"].setPlainText("pasted buffer")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.window.close_comparison(0))
        self.assertTrue(self.window.close())
        snapshots = first._recovery_store.snapshots()
        self.assertEqual(snapshots[0][1]["panels"][0]["text"], "pasted buffer")

    def test_window_close_cancel_retains_all_tabs(self):
        first = self.window.open_paths(self.files("first"), new_tab=False)
        first.panels[0]["editor"].setPlainText("first edit")
        second = self.window.open_paths(self.files("second"))
        second.panels[0]["editor"].setPlainText("second edit")
        with patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Save,
                                                               QMessageBox.StandardButton.Cancel]):
            self.assertFalse(self.window.close())
        self.assertEqual(self.window.comparison_tabs.count(), 2)
        self.assertFalse(first._closed)
        self.assertFalse(second._closed)
        self.assertEqual(first.panels[0]["editing_path"].read_text(), "first edit")
        self.assertFalse(first.panels[0]["dirty"])
        self.assertEqual(first._panel_text(first.panels[0]), "first edit")
        self.assertTrue(second.panels[0]["dirty"])
        self.assertEqual(second._panel_text(second.panels[0]), "second edit")

    def edited_tabs(self):
        first = self.window.open_paths(self.files("first"), new_tab=False)
        first.panels[0]["editor"].setPlainText("first edit")
        second = self.window.open_paths(self.files("second"))
        second.panels[0]["editor"].setPlainText("second edit")
        for view in (first, second):
            self.assertTrue(view._write_recovery_snapshot())
        return first, second

    def assert_recoverable_edit(self, view, index, text):
        panel = view.panels[index]
        self.assertFalse(view._closed)
        self.assertTrue(panel["dirty"])
        self.assertEqual(view._panel_text(panel), text)
        self.assertTrue(view._recovery_timer.isActive())
        snapshots = dict(view._recovery_store.snapshots(include_active=True))
        record = snapshots[view._recovery_store.session_path]["panels"][index]
        self.assertTrue(record["recover"])
        self.assertEqual(record["text"], text)
        self.assertIsNotNone(view._recovery_store._lease)

    def test_discard_then_cancel_preserves_dirty_recovery_and_next_prompt(self):
        first, second = self.edited_tabs()
        with patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                               QMessageBox.StandardButton.Cancel]):
            self.assertFalse(self.window.close())
        self.assertEqual(self.window.comparison_tabs.count(), 2)
        self.assertFalse(self.window._closing)
        self.assert_recoverable_edit(first, 0, "first edit")
        self.assert_recoverable_edit(second, 0, "second edit")
        self.assertEqual(first.panels[0]["saved_text"], "source 0\n")
        self.assertEqual(first.panels[0]["editing_path"].read_text(), "source 0\n")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as prompt:
            self.assertFalse(self.window.close())
        self.assertIn("first-0.txt", prompt.call_args.args[2])

    def test_discard_then_failed_save_or_reservation_preserves_both_tabs(self):
        first, second = self.edited_tabs()
        failures = [patch("d1ffler.panel_mixin.QSaveFile.commit", return_value=False),
                    patch.object(self.window.shared_sources, "begin", return_value=None)]
        for failure in failures:
            with self.subTest(failure=failure.attribute), failure, \
                    patch("d1ffler.diff_tool.QMessageBox.critical"), \
                    patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                                       QMessageBox.StandardButton.Save]):
                self.assertFalse(self.window.close())
            self.assert_recoverable_edit(first, 0, "first edit")
            self.assert_recoverable_edit(second, 0, "second edit")
            self.assertEqual(second.panels[0]["editing_path"].read_text(), "source 0\n")

    def test_discard_then_cancel_recovery_warning_preserves_both_tabs(self):
        first, second = self.edited_tabs()
        with patch.object(second._recovery_store, "write", side_effect=RecoveryError("Backup failed")), \
                patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                                   QMessageBox.StandardButton.Discard,
                                                                   QMessageBox.StandardButton.Cancel]) as prompt:
            self.assertFalse(self.window.close())
        self.assertEqual(prompt.call_args.args[1], "Recovery unavailable")
        self.assert_recoverable_edit(first, 0, "first edit")
        self.assert_recoverable_edit(second, 0, "second edit")

    def test_partial_save_is_durable_while_discard_and_failed_buffer_are_retained(self):
        first, second = self.edited_tabs()
        second.panels[1]["editor"].setPlainText("failed edit")
        second._write_recovery_snapshot()
        save = second._save_panel_file

        def save_with_later_failure(index):
            if index == 0:
                return save(index)
            with patch("d1ffler.panel_mixin.QSaveFile.commit", return_value=False):
                return save(index)

        with patch.object(second, "_save_panel_file", side_effect=save_with_later_failure), \
                patch("d1ffler.diff_tool.QMessageBox.critical"), \
                patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                                   QMessageBox.StandardButton.Save]):
            self.assertFalse(self.window.close())
        self.assert_recoverable_edit(first, 0, "first edit")
        self.assert_recoverable_edit(second, 1, "failed edit")
        self.assertFalse(second.panels[0]["dirty"])
        self.assertEqual(second.panels[0]["editing_path"].read_text(), "second edit")
        self.assertEqual(second.panels[1]["editing_path"].read_text(), "source 1\n")

    def test_pending_workspace_discard_survives_cancel_in_same_or_later_tab(self):
        first, second = self.edited_tabs()
        pending = first._workspace_snapshot("Pending")
        first._pending_named_workspace = deepcopy(pending)
        for choices in ([QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Cancel],
                        [QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Discard,
                         QMessageBox.StandardButton.Cancel]):
            with self.subTest(choices=choices), patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=choices):
                self.assertFalse(self.window.close())
            self.assertEqual(first._pending_named_workspace, pending)
            self.assert_recoverable_edit(first, 0, "first edit")
            self.assert_recoverable_edit(second, 0, "second edit")

    def test_folder_preview_discard_survives_cancel(self):
        folders = self.folders("preview")
        first = self.window.open_paths(folders, new_tab=False)
        item = QTreeWidgetItem(["chosen.txt"])
        item.setData(0, Qt.ItemDataRole.UserRole, "chosen.txt")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        first._open_folder_item(first.panels[0]["widget"], item)
        first.panels[0]["editor"].setPlainText("preview edit")
        second = self.window.open_paths(self.files("second"))
        second.panels[0]["editor"].setPlainText("second edit")
        with patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                               QMessageBox.StandardButton.Cancel]):
            self.assertFalse(self.window.close())
        self.assert_recoverable_edit(first, 0, "preview edit")
        self.assertEqual(first.panels[0]["kind"], "folder")
        self.assertEqual(first.panels[0]["editing_path"], folders[0] / "chosen.txt")
        self.assertEqual((folders[0] / "chosen.txt").read_text(), "0")

    def test_merge_result_discard_survives_cancel(self):
        first = self.window.active_comparison
        first.merge_mode_button.setChecked(True)
        first.panels[3]["editor"].setPlainText("saved result")
        result = self.root / "result.txt"
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(result), "")):
            self.assertTrue(first._save_panel_file(3))
        first.panels[3]["editor"].setPlainText("unfinished result")
        second = self.window.open_paths(self.files("second"))
        second.panels[0]["editor"].setPlainText("second edit")
        with patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                               QMessageBox.StandardButton.Cancel]):
            self.assertFalse(self.window.close())
        self.assert_recoverable_edit(first, 3, "unfinished result")
        self.assertTrue(first.merge_touched)
        self.assertEqual(result.read_text(), "saved result")

    def test_tab_discard_then_cancel_recovery_preserves_pasted_buffer(self):
        view = self.window.active_comparison
        panel = view.panels[0]
        panel["editor"].setPlainText("pasted buffer")
        panel["recovery_pending"] = True
        view._write_recovery_snapshot()
        with patch.object(view._recovery_store, "write", side_effect=RecoveryError("Backup failed")), \
                patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                                   QMessageBox.StandardButton.Cancel]):
            self.assertFalse(self.window.close_comparison(0))
        self.assertEqual(view._panel_text(panel), "pasted buffer")
        self.assertTrue(panel["recovery_pending"])
        self.assertTrue(view._recovery_timer.isActive())
        self.assertEqual(view._recovery_store.snapshots(include_active=True)[0][1]["panels"][0]["text"],
                         "pasted buffer")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as prompt:
            self.assertFalse(self.window.close_comparison(0))
        self.assertEqual(prompt.call_args.args[1], "Unsaved file changes")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.window.close_comparison(0))
        self.assertEqual(view._recovery_store.snapshots(), [])

    def test_successful_close_commits_save_discard_and_releases_resources(self):
        first, second = self.edited_tabs()
        first._pending_named_workspace = first._workspace_snapshot("Pending")
        with patch("d1ffler.diff_tool.QMessageBox.warning", side_effect=[QMessageBox.StandardButton.Discard,
                                                               QMessageBox.StandardButton.Discard,
                                                               QMessageBox.StandardButton.Save]):
            self.assertTrue(self.window.close())
        self.assertIsNone(first._pending_named_workspace)
        self.assertEqual(first.panels[0]["editing_path"].read_text(), "source 0\n")
        self.assertEqual(second.panels[0]["editing_path"].read_text(), "second edit")
        self.assertEqual(first._recovery_store.snapshots(include_active=True), [])
        for view in (first, second):
            self.assertTrue(view._closed)
            self.assertFalse(view.panels[0]["dirty"])
            self.assertIsNone(view._recovery_store._lease)
            self.assertFalse(view._recovery_timer.isActive())
            self.assertEqual(view._folder_jobs + view._text_jobs + view._context_jobs, [])
        self.assertEqual(self.window.comparison_tabs.count(), 0)

    def test_committed_recovery_cleanup_failure_keeps_backup_and_releases_lease(self):
        view = self.window.open_paths(self.files("first"), new_tab=False)
        view.panels[0]["editor"].setPlainText("first edit")
        view._write_recovery_snapshot()
        with patch.object(view._recovery_store, "clear", side_effect=RecoveryError("Cleanup failed")), \
                patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard) as prompt:
            self.assertTrue(self.window.close())
        self.assertEqual(prompt.call_args.args[1], "Recovery cleanup failed")
        self.assertEqual(prompt.call_args.args[3], QMessageBox.StandardButton.Ok)
        self.assertTrue(view._closed)
        self.assertIsNone(view._recovery_store._lease)
        self.assertEqual(view._recovery_store.snapshots()[0][1]["panels"][0]["text"], "first edit")
        self.assertEqual(view.panels[0]["editing_path"].read_text(), "source 0\n")

    def test_recovery_reopens_each_comparison_in_its_own_tab(self):
        first = self.window.active_comparison
        first.panels[0]["editor"].setPlainText("first pasted")
        second = self.window.new_comparison()
        second.panels[0]["editor"].setPlainText("second pasted")
        first._write_recovery_snapshot()
        second._write_recovery_snapshot()
        self.assertTrue(self.window.close())
        reopened = ComparisonWindow(deepcopy(DEFAULT_SETTINGS), self.root / "settings.json", DiffApp)
        self.addCleanup(reopened.deleteLater)
        self.addCleanup(reopened.close)

        def restore(dialog):
            dialog.choice = "restore"
            return 0

        with patch.object(RecoveryDialog, "exec", restore):
            self.assertTrue(reopened.offer_recovery())
        self.assertEqual(reopened.comparison_tabs.count(), 2)
        self.assertEqual({view._panel_text(view.panels[0]) for view in reopened.comparisons},
                         {"first pasted", "second pasted"})
        self.assertEqual(len({view._recovery_store.session_path for view in reopened.comparisons}), 2)

    def test_close_cancels_one_scan_without_cancelling_another(self):
        started = threading.Event()

        class SlowWorker:
            def __init__(self, *args):
                self.cancel_event = args[6]
                self.messages = args[7]
                self.permits = threading.Semaphore(4)
                self.excluded_count = 0
                self.move_detection_limited = False

            def run(self):
                started.set()
                self.cancel_event.wait(5)
                self.messages.put(("finished", True, ""))

        first = self.window.active_comparison
        first.folder_comparison_worker_type = SlowWorker
        self.window.open_paths(self.folders("slow"), new_tab=False)
        self.wait_until(started.is_set)
        worker = first._folder_worker
        second = self.window.open_paths(self.folders("fast"))
        self.assertTrue(self.window.close_comparison(0))
        self.assertTrue(worker.cancel_event.is_set())
        self.wait_until(lambda: second._folder_worker is None and "chosen.txt" in second._folder_rows)
        self.assertIs(self.window.active_comparison, second)
        self.assertFalse(second._folder_partial)

    def test_command_line_source_sets_include_all_four_panels(self):
        groups = command_line_source_sets([str(path) for path in self.files("first", 4)] +
                                          ["--new-tab"] + [str(path) for path in self.files("second")])
        for index, paths in enumerate(groups):
            self.window.open_paths(paths, new_tab=index > 0)
        self.assertEqual(self.window.comparison_tabs.count(), 2)
        self.assertEqual(len(self.window.comparisons[0].panels), 4)
        self.assertEqual(len(self.window.comparisons[1].panels), 2)
        self.assertEqual(self.window.comparisons[0].panels[3]["path"], groups[0][3])


if __name__ == "__main__":
    unittest.main()
