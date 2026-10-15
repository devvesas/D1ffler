"""Restoring only completed sync work from reviewed backups."""

import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from d1ffler.folder_restore import build_restore_plan, execute_restore
from d1ffler.folder_sync import build_items, execute_plan, operation_for


class FolderRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.left, self.right, self.backups = (self.root / name for name in ("left", "right", "backups"))
        self.left.mkdir()
        self.right.mkdir()

    def put(self, root, relative, data):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def sync(self, actions, **kwargs):
        left_files = {name: self.left / name for name in actions if (self.left / name).is_file()}
        right_files = {name: self.right / name for name in actions if (self.right / name).is_file()}
        items = {item.relative: item for item in build_items(self.left, self.right, left_files, right_files)}
        operations = [operation_for(items[name], action, self.left, self.right)
                      for name, action in actions.items()]
        return execute_plan(operations, self.left, self.right, backup_directory=self.backups, **kwargs).to_dict()

    def test_restore_overwrite_delete_and_reverse_create(self):
        self.put(self.left, "change.txt", b"new")
        self.put(self.right, "change.txt", b"old")
        self.put(self.right, "delete.txt", b"preserve")
        self.put(self.left, "create.txt", b"created")
        report = self.sync({"change.txt": "left_to_right", "delete.txt": "delete_right",
                            "create.txt": "left_to_right"})
        self.assertEqual([row["status"] for row in report["outcomes"]], ["completed"] * 3)
        items = build_restore_plan(report)
        self.assertEqual([item.status for item in items], ["ready"] * 3)
        self.assertEqual({item.action for item in items}, {"restore_backup", "delete_created"})
        dry = execute_restore(items, report, dry_run=True)
        self.assertEqual([row["status"] for row in dry.outcomes], ["dry run"] * 3)
        self.assertEqual((self.right / "change.txt").read_bytes(), b"new")
        restored = execute_restore(items, report)
        self.assertEqual([row["status"] for row in restored.outcomes], ["completed"] * 3)
        self.assertEqual((self.right / "change.txt").read_bytes(), b"old")
        self.assertEqual((self.right / "delete.txt").read_bytes(), b"preserve")
        self.assertFalse((self.right / "create.txt").exists())
        self.assertEqual((self.backups / "right/change.txt").read_bytes(), b"old")
        self.assertEqual(report["outcomes"][0]["status"], "completed")

    def test_backup_change_during_restore_copy_keeps_target(self):
        self.put(self.left, "a.txt", b"new")
        target = self.put(self.right, "a.txt", b"old")
        report = self.sync({"a.txt": "left_to_right"})
        item = build_restore_plan(report)[0]
        backup = self.backups / "right/a.txt"
        original_copy = shutil.copy2

        def change_before_copy(src, dst):
            backup.write_bytes(b"bad")
            return original_copy(src, dst)

        with patch("d1ffler.folder_sync.shutil.copy2", side_effect=change_before_copy):
            result = execute_restore([item], report)
        self.assertEqual(result.outcomes[0]["status"], "error")
        self.assertEqual(target.read_bytes(), b"new")

    def test_changed_target_requires_explicit_overwrite(self):
        self.put(self.left, "change.txt", b"new")
        self.put(self.right, "change.txt", b"old")
        report = self.sync({"change.txt": "left_to_right"})
        target = self.right / "change.txt"
        target.write_bytes(b"later")
        items = build_restore_plan(report)
        self.assertEqual(items[0].status, "changed")
        skipped = execute_restore(items, report)
        self.assertEqual(skipped.outcomes[0]["status"], "conflict")
        self.assertEqual(target.read_bytes(), b"later")
        restored = execute_restore(items, report, conflict_policy="overwrite")
        self.assertEqual(restored.outcomes[0]["status"], "completed")
        self.assertEqual(target.read_bytes(), b"old")

    def test_revalidates_target_and_backup_after_review(self):
        self.put(self.left, "change.txt", b"new")
        self.put(self.right, "change.txt", b"old")
        report = self.sync({"change.txt": "left_to_right"})
        items = build_restore_plan(report)
        (self.right / "change.txt").write_bytes(b"later")
        self.assertEqual(execute_restore(items, report).outcomes[0]["status"], "conflict")
        (self.backups / "right/change.txt").write_bytes(b"tampered")
        self.assertEqual(execute_restore(items, report, conflict_policy="overwrite").outcomes[0]["status"], "error")
        self.assertEqual((self.right / "change.txt").read_bytes(), b"later")

    def test_partial_report_and_invalid_backup_path(self):
        self.put(self.left, "a.txt", b"a")
        self.put(self.left, "b.txt", b"b")
        report = self.sync({"a.txt": "left_to_right", "b.txt": "left_to_right"},
                           cancelled=lambda: (self.right / "a.txt").exists())
        self.assertTrue(report["cancelled"])
        initial = build_restore_plan(report)
        self.assertEqual([item.relative for item in initial], ["a.txt"])
        self.assertEqual(initial[0].status, "ready")
        report["outcomes"][0]["backup_path"] = str(self.root / "outside.txt")
        self.assertEqual(build_restore_plan(report)[0].status, "unavailable")

    def test_tampered_backup_location_is_unavailable(self):
        self.put(self.left, "change.txt", b"new")
        self.put(self.right, "change.txt", b"old")
        report = self.sync({"change.txt": "left_to_right"})
        report["outcomes"][0]["backup_path"] = str(self.root / "outside.txt")
        items = build_restore_plan(report)
        self.assertEqual(items[0].status, "unavailable")
        self.assertEqual(execute_restore(items, report, conflict_policy="overwrite").outcomes[0]["status"], "error")

    def test_tampered_effect_cannot_turn_copy_into_reverse_delete(self):
        self.put(self.left, "change.txt", b"new")
        self.put(self.right, "change.txt", b"old")
        report = self.sync({"change.txt": "left_to_right"})
        report["outcomes"][0]["effect"] = "create"
        items = build_restore_plan(report)
        self.assertEqual(items[0].status, "unavailable")
        self.assertEqual(execute_restore(items, report, conflict_policy="overwrite").outcomes[0]["status"], "error")
        self.assertEqual((self.right / "change.txt").read_bytes(), b"new")

    def test_legacy_report_needs_explicit_overwrite(self):
        self.put(self.left, "change.txt", b"new")
        self.put(self.right, "change.txt", b"old")
        report = self.sync({"change.txt": "left_to_right"})
        outcome = report["outcomes"][0]
        for key in ("backup_path", "backup_size", "backup_sha256", "after_size", "after_sha256"):
            outcome.pop(key, None)
        items = build_restore_plan(report)
        self.assertEqual(items[0].status, "unknown")
        self.assertEqual(execute_restore(items, report).outcomes[0]["status"], "conflict")
        self.assertEqual(execute_restore(items, report, conflict_policy="overwrite").outcomes[0]["status"], "completed")

    def test_stop_policy_and_cancellation_leave_remaining_files_untouched(self):
        for name in ("a.txt", "b.txt"):
            self.put(self.left, name, b"new")
            self.put(self.right, name, b"old")
        report = self.sync({"a.txt": "left_to_right", "b.txt": "left_to_right"})
        (self.right / "a.txt").write_bytes(b"later")
        items = build_restore_plan(report)
        stopped = execute_restore(items, report, conflict_policy="stop")
        self.assertTrue(stopped.stopped_on_conflict)
        self.assertEqual(len(stopped.outcomes), 1)
        self.assertEqual((self.right / "b.txt").read_bytes(), b"new")
        cancelled = execute_restore(items, report, conflict_policy="overwrite",
                                    cancelled=lambda: (self.right / "a.txt").read_bytes() == b"old")
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(len(cancelled.outcomes), 1)
        self.assertEqual((self.right / "b.txt").read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
