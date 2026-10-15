"""Injected failures stay with their comparison and preserve recoverable buffers."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import MethodType
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.comparison_tabs import ComparisonWindow
from d1ffler.diagnostics import ExceptionHooks
from d1ffler.diagnostics_dialog import DiagnosticsDialog
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.folder_content_search_dialog import FolderContentSearchDialog
from d1ffler.folder_sync_dialogs import RestorePlanDialog
from d1ffler.sync_plan_dialog import SyncPlanDialog
from d1ffler.review_dialogs import FingerprintDialog


class BackgroundFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.window = ComparisonWindow(deepcopy(DEFAULT_SETTINGS), self.root / "settings.json", DiffApp)
        self.view = self.window.active_comparison
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_until(self, condition, timeout=6):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(condition())

    def idle(self, view=None):
        view = view or self.view
        self.wait_until(lambda: not (view._folder_jobs or view._binary_jobs or view._image_jobs or view._text_jobs))

    def folders(self):
        paths = [self.root / side for side in ("left", "right")]
        for path in paths:
            path.mkdir()
            (path / "chosen.txt").write_text("same\n", encoding="utf-8")
        self.window.open_paths(paths, new_tab=False)
        self.idle()
        return paths

    def assert_failed(self, kind):
        self.assertIsNone(getattr(self.view, f"_{kind}_worker"))
        self.assertIn(f"{kind.title()} comparison failed", self.view.status.text())
        self.assertFalse(self.view.failure_notice.isHidden())
        self.assertTrue(self.view.cancel_comparison_button.isHidden())
        self.assertFalse(self.view._background_timer.isActive())
        records = self.view.diagnostics.read_events()
        self.assertTrue(any(record["operation"] == f"{kind.title()} comparison" for record in records))

    def test_folder_failure_retains_partial_rows_and_refresh_retries(self):
        self.folders()
        worker_type = self.view.folder_comparison_worker_type
        class FailAfterBatch(worker_type):
            def run(worker):
                worker.messages.put(("batch", [("chosen.txt", ["identical", "identical"])]))
                raise RuntimeError("private contents")
        self.view.folder_comparison_worker_type = FailAfterBatch
        self.view._refresh_folder_comparison()
        self.idle()
        self.assert_failed("folder")
        self.assertTrue(self.view._folder_partial)
        self.assertIn("chosen.txt", self.view._folder_rows)
        self.assertTrue(self.view._comparison_export_snapshot(True, False, False, False)["partial"])
        self.view.folder_comparison_worker_type = worker_type
        self.view._refresh_folder_comparison()
        self.idle()
        self.assertFalse(self.view._folder_partial)
        self.assertTrue(self.view.failure_notice.isHidden())
        self.assertEqual(self.view._folder_rows["chosen.txt"], ["identical", "identical"])

    def test_text_failure_preserves_dirty_buffer_and_disables_stale_hunks_then_retries(self):
        left, right = self.root / "left.txt", self.root / "right.txt"
        left.write_text("before\n", encoding="utf-8")
        right.write_text("after\n", encoding="utf-8")
        self.window.open_paths([left, right], new_tab=False)
        self.assertTrue(self.view._hunks)
        edited = "UNSAVED_PRIVATE_CONTENTS " * 4000 + "\n"
        with patch("d1ffler.diff_tool._TextDiffWorker.run", side_effect=RuntimeError("matching failed")):
            self.view.panels[1]["editor"].setPlainText(edited)
            self.idle()
        self.assert_failed("text")
        self.assertEqual(self.view._hunks, [])
        self.assertEqual(self.view._panel_text(self.view.panels[1]), edited)
        self.assertTrue(self.view.panels[1]["dirty"])
        self.assertEqual(right.read_text(encoding="utf-8"), "after\n")
        self.view._refresh_folder_comparison()
        self.idle()
        self.assertTrue(self.view.failure_notice.isHidden())
        self.assertTrue(self.view._hunks)
        self.assertEqual(self.view._panel_text(self.view.panels[1]), edited)

    def binary_files(self):
        paths = [self.root / "left.bin", self.root / "right.bin"]
        for path in paths:
            path.write_bytes(b"\x00" * 1024)
        self.window.open_paths(paths, new_tab=False)
        self.idle()
        return paths

    def test_binary_failure_has_visible_error_and_can_retry_unchanged_sources(self):
        self.binary_files()
        with patch("d1ffler.diff_tool._BinarySummaryWorker.run", side_effect=RuntimeError("binary failed")):
            self.view._refresh_folder_comparison()
            self.idle()
        self.assert_failed("binary")
        self.assertIn("failed", self.view.panels[0]["editor"].toPlainText())
        self.view._refresh_folder_comparison()
        self.idle()
        self.assertIsNotNone(self.view._binary_results)
        self.assertTrue(self.view.failure_notice.isHidden())

    def test_binary_failure_and_retry_preserve_other_panels_dirty_text(self):
        source, binary = self.root / "edited.txt", self.root / "binary.bin"
        source.write_text("original\n", encoding="utf-8")
        binary.write_bytes(b"\x00" * 1024)
        self.window.open_paths([source], new_tab=False)
        text = "unsaved text remains available\n"
        self.view.panels[0]["editor"].setPlainText(text)
        with patch("d1ffler.diff_tool._BinarySummaryWorker.run", side_effect=RuntimeError("binary failed")):
            self.view._load_panel(1, "file", binary)
            self.idle()
        self.assert_failed("binary")
        self.assertEqual(self.view._panel_text(self.view.panels[0]), text)
        self.assertTrue(self.view.panels[0]["dirty"])
        self.assertFalse(self.view.panels[0]["editor"].isReadOnly())
        self.view._refresh_folder_comparison()
        self.idle()
        self.assertEqual(self.view._panel_text(self.view.panels[0]), text)
        self.assertTrue(self.view.panels[0]["dirty"])
        self.assertEqual(source.read_text(encoding="utf-8"), "original\n")

    def images(self):
        paths = [self.root / "left.png", self.root / "right.png"]
        for path in paths:
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0xFFFFFFFF)
            self.assertTrue(image.save(str(path)))
        self.window.open_paths(paths, new_tab=False)
        self.idle()
        return paths

    def test_image_failure_remains_visible_after_binary_finishes_and_can_retry(self):
        self.images()
        original = self.view.binary_summary_worker_type.run
        def delayed_binary(worker):
            time.sleep(0.08)
            original(worker)
        with patch("d1ffler.image_compare.ImageDecodeWorker.run", side_effect=RuntimeError("decode failed")), \
                patch("d1ffler.diff_tool._BinarySummaryWorker.run", delayed_binary):
            self.view._refresh_folder_comparison()
            self.idle()
        self.assert_failed("image")
        self.assertIsNotNone(self.view._binary_results)
        self.assertTrue(self.view.image_controls.isHidden())
        self.view._refresh_folder_comparison()
        self.idle()
        self.assertIsNotNone(self.view._image_results)
        self.assertFalse(self.view.image_controls.isHidden())
        self.assertTrue(self.view.failure_notice.isHidden())

    def test_inactive_tab_failure_does_not_change_other_tab(self):
        self.folders()
        other = self.window.new_comparison()
        before = other.status.text()
        with patch("d1ffler.diff_tool._FolderComparisonWorker.run", side_effect=RuntimeError("folder failed")):
            self.view._refresh_folder_comparison()
            self.idle()
        self.assert_failed("folder")
        self.assertIs(self.window.active_comparison, other)
        self.assertEqual(other.status.text(), before)
        self.assertTrue(other.failure_notice.isHidden())

    def test_failure_during_closed_tab_cancellation_is_logged_without_cross_tab_update(self):
        self.folders()
        started = threading.Event()
        worker_type = self.view.folder_comparison_worker_type
        class FailOnClose(worker_type):
            def run(worker):
                started.set()
                worker.cancel_event.wait(3)
                raise RuntimeError("failure during cancellation")
        self.view.folder_comparison_worker_type = FailOnClose
        self.view._refresh_folder_comparison()
        self.wait_until(started.is_set)
        other = self.window.new_comparison()
        before = other.status.text()
        self.assertTrue(self.window.close_comparison(0))
        self.app.processEvents()
        self.assertEqual(other.status.text(), before)
        self.assertTrue(other.failure_notice.isHidden())
        self.assertTrue(self.window.diagnostics.read_events())

    def test_cancelled_running_failure_stays_partial_without_showing_success(self):
        self.folders()
        started = threading.Event()
        worker_type = self.view.folder_comparison_worker_type
        class FailOnCancel(worker_type):
            def run(worker):
                started.set()
                worker.cancel_event.wait(3)
                raise RuntimeError("cancelled failure")
        self.view.folder_comparison_worker_type = FailOnCancel
        self.view._refresh_folder_comparison()
        self.wait_until(started.is_set)
        self.view._cancel_active_comparison()
        self.idle()
        self.assertTrue(self.view._folder_partial)
        self.assertIn("partial", self.view.status.text())
        self.assertTrue(self.view.failure_notice.isHidden())
        self.assertEqual(len(self.view.diagnostics.read_events()), 1)

    def test_queued_failure_is_reported_when_worker_starts(self):
        self.folders()
        release = threading.Event()
        self.addCleanup(release.set)
        blockers = [self.window.worker_pool.submit(release.wait, 3) for _index in range(2)]
        with patch("d1ffler.diff_tool._FolderComparisonWorker.run", side_effect=RuntimeError("queued failure")):
            self.view._refresh_folder_comparison()
            self.assertFalse(self.view._folder_jobs[0][0].future.running())
            self.assertIn("Queued work", self.view.status.text())
            release.set()
            self.idle()
        for blocker in blockers:
            blocker.result(timeout=1)
        self.assert_failed("folder")

    def test_superseded_text_failure_launches_pending_text_without_replacing_latest_buffer(self):
        source = self.root / "source.txt"
        source.write_text("on disk\n", encoding="utf-8")
        self.window.open_paths([source], new_tab=False)
        started = threading.Event()
        worker_type = self.view.text_diff_worker_type
        class FailSuperseded(worker_type):
            def run(worker):
                started.set()
                worker.cancel_event.wait(3)
                raise RuntimeError("superseded failure")
        self.view.text_diff_worker_type = FailSuperseded
        self.view.panels[0]["editor"].setPlainText("first unsaved " * 6000)
        self.wait_until(started.is_set)
        self.view.text_diff_worker_type = worker_type
        latest = "latest unsaved " * 6000
        self.view.panels[0]["editor"].setPlainText(latest)
        self.idle()
        self.assertEqual(self.view._panel_text(self.view.panels[0]), latest)
        self.assertTrue(self.view.panels[0]["dirty"])
        self.assertTrue(self.view.failure_notice.isHidden())
        self.assertIn("ready", self.view.status.text())
        self.assertEqual(len(self.view.diagnostics.read_events()), 1)

    def test_unwritable_diagnostics_still_clear_failed_worker_and_report_log_failure(self):
        self.folders()
        with patch("d1ffler.diff_tool._FolderComparisonWorker.run", side_effect=RuntimeError("failure")), \
                patch("d1ffler.diagnostics.locked_state", side_effect=PermissionError("private path")):
            self.view._refresh_folder_comparison()
            self.idle()
        self.assertIsNone(self.view._folder_worker)
        self.assertIn("could not be written", self.view.failure_notice.text())
        self.assertTrue(self.view._folder_partial)
        self.assertFalse(self.view._background_timer.isActive())

    def test_sync_plan_failure_stops_scan_and_keeps_apply_disabled(self):
        self.folders()
        with patch("d1ffler.diff_tool.SyncPlanWorker.run", side_effect=RuntimeError("sync failed")):
            dialog = SyncPlanDialog(self.view, 0, 1)
            self.addCleanup(dialog.deleteLater)
            self.addCleanup(dialog.reject)
            self.wait_until(lambda: not dialog._plan_jobs)
        self.assertIsNone(dialog._plan_worker)
        self.assertFalse(dialog._plan_poll_timer.isActive())
        self.assertFalse(dialog.apply_plan_button.isEnabled())
        self.assertIn("diagnostic", dialog.summary.text())

    def test_restore_plan_failure_stops_scan_and_keeps_apply_disabled(self):
        with patch("d1ffler.folder_sync_dialogs.build_restore_plan", side_effect=RuntimeError("restore failed")):
            dialog = RestorePlanDialog({"left": str(self.root), "right": str(self.root), "outcomes": []}, self.view)
            self.addCleanup(dialog.deleteLater)
            self.addCleanup(dialog.reject)
            self.wait_until(lambda: dialog._worker is None)
        self.assertFalse(dialog._timer.isActive())
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertIn("diagnostic", dialog.summary.text())

    def test_content_search_failure_labels_partial_and_reenables_search(self):
        self.folders()
        dialog = FolderContentSearchDialog(self.view)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        dialog.query.setText("same")
        with patch("d1ffler.folder_content_search.FolderContentSearchWorker.run", side_effect=RuntimeError("search failed")):
            dialog._start()
            self.wait_until(lambda: not dialog.timer.isActive())
        self.assertIn("failed", dialog.summary.text())
        self.assertTrue(dialog.stale)
        self.assertTrue(dialog.search_button.isEnabled())
        self.assertFalse(dialog.cancel_button.isEnabled())

    def test_unexpected_export_failure_finishes_without_success_or_replacing_report(self):
        destination = self.root / "report.json"
        destination.write_text("existing report", encoding="utf-8")
        def chunks(_cancelled):
            yield "partial report", 1
            raise RuntimeError("private export data")
        with patch("d1ffler.review_export_mixin.QMessageBox.warning") as warning:
            self.assertFalse(self.view._run_comparison_export(destination, chunks, 2))
        self.assertIn("diagnostic", warning.call_args.args[2])
        self.assertEqual(destination.read_text(encoding="utf-8"), "existing report")
        self.assertNotIn("Exported comparison", self.view.status.text())
        self.assertEqual(self.view.diagnostics.read_events()[0]["operation"], "Comparison export")

    def test_review_fingerprint_failure_stops_progress_without_accepting_review(self):
        path = self.root / "source.txt"
        path.write_text("source text", encoding="utf-8")
        with patch("d1ffler.review_dialogs.content_fingerprint", side_effect=RuntimeError("fingerprint failed")):
            dialog = FingerprintDialog([{"path": str(path), "kind": "file"}], [None], self.view)
            self.addCleanup(dialog.deleteLater)
            self.addCleanup(dialog.reject)
            self.wait_until(lambda: not dialog._timer.isActive())
        self.assertIn("failed", dialog.label.text())
        self.assertIn("diagnostic", dialog.label.text())
        self.assertEqual(dialog.results, {})
        self.assertEqual(dialog.result(), dialog.DialogCode.Rejected)
        self.assertEqual(self.view.diagnostics.read_events()[0]["operation"], "Review fingerprints")

    def test_gui_exception_hook_routes_to_owner_and_backs_up_dirty_edits(self):
        path = self.root / "source.txt"
        path.write_text("on disk\n", encoding="utf-8")
        self.window.open_paths([path], new_tab=False)
        self.view.panels[0]["editor"].setPlainText("UNSAVED_PRIVATE_CONTENTS\n")
        other = self.window.new_comparison()
        before = other.status.text()
        hooks = ExceptionHooks(self.app, self.window.diagnostics, self.window)
        self.addCleanup(hooks.restore)
        def fail_action(self):
            raise RuntimeError("UNSAVED_PRIVATE_CONTENTS")
        self.view.fail_action = MethodType(fail_action, self.view)
        QTimer.singleShot(0, self.view.fail_action)
        self.wait_until(lambda: not self.view.failure_notice.isHidden())
        self.assertIn("An action failed", self.view.failure_notice.text())
        self.assertTrue(self.view.panels[0]["dirty"])
        self.assertEqual(self.view._panel_text(self.view.panels[0]), "UNSAVED_PRIVATE_CONTENTS\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "on disk\n")
        snapshot = json.loads(self.view._recovery_store.session_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["panels"][0]["text"], "UNSAVED_PRIVATE_CONTENTS\n")
        self.assertEqual(other.status.text(), before)
        self.assertNotIn("UNSAVED_PRIVATE_CONTENTS", self.window.diagnostics.path.read_text(encoding="utf-8"))

    def test_diagnostic_export_paths_are_an_explicit_unchecked_choice(self):
        path = self.root / "sensitive source.txt"
        path.write_text("PRIVATE_CONTENTS", encoding="utf-8")
        self.window.open_paths([path], new_tab=False)
        self.window.diagnostics.record_exception("GUI action", RuntimeError("PRIVATE_CONTENTS"))
        dialog = DiagnosticsDialog(self.window.diagnostics, self.view)
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.include_paths.isChecked())
        destination = self.root / "diagnostic report.json"
        with patch("d1ffler.diagnostics_dialog.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
                patch("d1ffler.diagnostics_dialog.QMessageBox.information"):
            dialog._export()
            self.assertNotIn(str(path), destination.read_text(encoding="utf-8"))
            dialog.include_paths.setChecked(True)
            dialog._export()
        report = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(report["shared_context"]["source_paths"], [str(path)])
        self.assertNotIn("PRIVATE_CONTENTS", destination.read_text(encoding="utf-8"))
