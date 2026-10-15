"""Privacy, bounded storage, and terminal-result behavior at the task boundary."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from d1ffler.app_version import APP_VERSION
from d1ffler.background_tasks import BackgroundTask
from d1ffler.diagnostics import DiagnosticStore


class DiagnosticStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = DiagnosticStore(self.root / "logs")

    def incident(self):
        private_contents = "PRIVATE_CONTENTS café 日本語"
        try:
            try:
                raise ValueError(f"{self.root / 'private-file.txt'}: {private_contents}")
            except ValueError as exc:
                raise RuntimeError(private_contents) from exc
        except RuntimeError as exc:
            return self.store.record_exception("Text comparison", exc)

    def test_local_log_records_version_operation_chain_and_frames_without_sensitive_details(self):
        failure = self.incident()
        self.assertTrue(failure.logged)
        event, = self.store.read_events()
        self.assertEqual(event["app_version"], APP_VERSION)
        self.assertEqual(event["operation"], "Text comparison")
        self.assertEqual(event["incident_id"], failure.incident_id)
        self.assertEqual([part["exception_type"] for part in event["exception_chain"]], ["RuntimeError", "ValueError"])
        self.assertTrue(event["exception_chain"][0]["frames"])
        payload = self.store.path.read_text(encoding="utf-8")
        for private in ("PRIVATE_CONTENTS", "private-file.txt", str(self.root), str(Path(__file__).parent),
                        "raise RuntimeError", "private_contents"):
            self.assertNotIn(private, payload)

    def test_export_omits_paths_unless_explicitly_supplied(self):
        self.incident()
        destination = self.root / "report.json"
        self.store.export(destination)
        self.assertNotIn("shared_context", json.loads(destination.read_text(encoding="utf-8")))
        self.assertNotIn(str(self.root), destination.read_text(encoding="utf-8"))
        paths = {"source_paths": [str(self.root / "source.txt")]}
        self.store.export(destination, shared_context=paths)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["shared_context"], paths)

    def test_rotated_storage_is_bounded(self):
        self.store.MAX_BYTES = 4096
        failures = [self.incident() for _index in range(60)]
        paths = list(self.store.directory.glob("diagnostics.jsonl*"))
        self.assertEqual(len(paths), self.store.BACKUPS + 1)
        self.assertTrue(all(path.stat().st_size <= self.store.MAX_BYTES for path in paths))
        retained = self.store.read_events()
        self.assertLess(len(retained), len(failures))
        self.assertEqual(retained[-1]["incident_id"], failures[-1].incident_id)

    def test_concurrent_store_instances_share_lock_and_keep_complete_records(self):
        stores = [DiagnosticStore(self.store.directory) for _index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            failures = list(executor.map(lambda index: stores[index % 4].record_exception(
                "Concurrent task", RuntimeError("private")), range(24)))
        self.assertTrue(all(failure.logged for failure in failures))
        events = self.store.read_events()
        self.assertEqual(len(events), 24)
        self.assertEqual(len({event["incident_id"] for event in events}), 24)

    def test_logging_failure_does_not_escape_and_is_explicit(self):
        with patch("d1ffler.diagnostics.locked_state", side_effect=PermissionError("private path")):
            failure = self.incident()
        self.assertFalse(failure.logged)
        self.assertIn("could not be written", failure.summary)
        self.assertNotIn("private path", failure.summary)


class BackgroundTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = DiagnosticStore(Path(self.temporary.name) / "logs")

    def task(self, run, executor=None):
        messages = queue.Queue()
        worker = SimpleNamespace(run=lambda: run(messages), messages=messages, cancel_event=threading.Event())
        task = BackgroundTask(executor, worker, diagnostics=self.store, operation="Test operation")
        return task, messages

    def test_success_followed_by_exception_delivers_only_failure(self):
        def run(messages):
            messages.put(("finished", "claimed success"))
            raise RuntimeError("private file contents")
        task, messages = self.task(run)
        task.start()
        task.join()
        result = task.read_messages(messages)
        self.assertEqual([message[0] for message in result], ["failed"])
        self.assertTrue(result[0][1].logged)
        self.assertEqual(len(self.store.read_events()), 1)
        self.assertEqual(task.read_messages(messages), [])

    def test_terminal_result_waits_for_worker_return(self):
        terminal_sent, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def run(messages):
            messages.put(("finished", "result"))
            terminal_sent.set()
            release.wait(2)
        task, messages = self.task(run)
        task.start()
        self.assertTrue(terminal_sent.wait(2))
        self.assertEqual(task.read_messages(messages), [])
        release.set()
        task.join()
        self.assertEqual(task.read_messages(messages), [("finished", "result")])

    def test_queued_cancel_returns_terminal_without_running_or_waiting_for_executor(self):
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                executor.submit(release.wait, 3)
                task, messages = self.task(lambda _messages: self.fail("Cancelled queued worker ran"), executor)
                task.start()
                task.worker.cancel_event.set()
                self.assertEqual(task.read_messages(messages), [("cancelled", None)])
                self.assertTrue(task.future.cancelled())
            finally:
                release.set()
        self.assertEqual(self.store.read_events(), [])

    def test_missing_terminal_result_becomes_failure(self):
        task, messages = self.task(lambda messages: messages.put(("progress", 1)))
        task.start()
        task.join()
        self.assertEqual([message[0] for message in task.read_messages(messages)], ["progress", "failed"])

    def test_executor_submission_failure_has_a_terminal_result(self):
        executor = ThreadPoolExecutor(max_workers=1)
        executor.shutdown()
        task, messages = self.task(lambda messages: messages.put(("finished", None)), executor)
        task.start()
        self.assertEqual(task.read_messages(messages)[0][0], "failed")
        self.assertEqual(len(self.store.read_events()), 1)

    def test_failed_future_is_consumed_and_logged_even_without_polling(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            task, _messages = self.task(lambda _messages: None, executor)
            def failed_runner():
                raise RuntimeError("Future failed outside worker boundary")
            task._run = failed_runner
            task.start()
            task.join()
            self.assertIsInstance(task.future.exception(), RuntimeError)
        self.assertIsNotNone(task.failure)
        self.assertEqual(len(self.store.read_events()), 1)
