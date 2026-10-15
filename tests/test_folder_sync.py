import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from d1ffler.folder_sync import SyncError, build_items, execute_plan, file_state, operation_for, safe_path


class FolderSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.left = self.root / "left"
        self.right = self.root / "right"
        self.left.mkdir()
        self.right.mkdir()

    @staticmethod
    def put(root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def items(self, left_files, right_files, left_excluded=None):
        return build_items(self.left, self.right, left_files, right_files, left_excluded)

    def test_plan_is_read_only_and_dry_run_does_not_write(self):
        source = self.put(self.left, "nested/new.txt", b"new")
        item = self.items({"nested/new.txt": source}, {})[0]
        self.assertEqual(item.status, "removed")
        operation = operation_for(item, "left_to_right", self.left, self.right)
        self.assertEqual(operation.effect, "create")
        self.assertFalse((self.right / "nested/new.txt").exists())
        report = execute_plan([operation], self.left, self.right, dry_run=True)
        self.assertEqual(report.outcomes[0]["status"], "dry run")
        self.assertFalse((self.right / "nested/new.txt").exists())
        report = execute_plan([operation], self.left, self.right)
        self.assertEqual(report.outcomes[0]["status"], "completed")
        self.assertEqual((self.right / "nested/new.txt").read_bytes(), b"new")

    def test_source_change_during_copy_does_not_replace_target(self):
        source = self.put(self.left, "a.txt", b"reviewed")
        target = self.put(self.right, "a.txt", b"original")
        item = self.items({"a.txt": source}, {"a.txt": target})[0]
        operation = operation_for(item, "left_to_right", self.left, self.right)
        original_copy = shutil.copy2

        def change_before_copy(src, dst):
            source.write_bytes(b"changed!")
            return original_copy(src, dst)

        with patch("d1ffler.folder_sync.shutil.copy2", side_effect=change_before_copy):
            report = execute_plan([operation], self.left, self.right)
        self.assertEqual(report.outcomes[0]["status"], "error")
        self.assertIn("changed while copying", report.outcomes[0]["detail"])
        self.assertEqual(target.read_bytes(), b"original")

    def test_target_change_during_copy_does_not_replace_new_disk_version(self):
        source = self.put(self.left, "a.txt", b"reviewed")
        target = self.put(self.right, "a.txt", b"original")
        item = self.items({"a.txt": source}, {"a.txt": target})[0]
        operation = operation_for(item, "left_to_right", self.left, self.right)
        original_copy = shutil.copy2

        def change_before_copy(src, dst):
            target.write_bytes(b"external")
            return original_copy(src, dst)

        with patch("d1ffler.folder_sync.shutil.copy2", side_effect=change_before_copy):
            report = execute_plan([operation], self.left, self.right)
        self.assertEqual(report.outcomes[0]["status"], "error")
        self.assertIn("changed while copying", report.outcomes[0]["detail"])
        self.assertEqual(target.read_bytes(), b"external")

    def test_target_change_before_delete_keeps_new_disk_version(self):
        target = self.put(self.right, "a.txt", b"original")
        item = self.items({}, {"a.txt": target})[0]
        operation = operation_for(item, "delete_right", self.left, self.right)
        checked = False

        def change_after_check(path):
            nonlocal checked
            result = file_state(path)
            if path == target and not checked:
                checked = True
                target.write_bytes(b"external")
            return result

        with patch("d1ffler.folder_sync.file_state", side_effect=change_after_check):
            report = execute_plan([operation], self.left, self.right)
        self.assertEqual(report.outcomes[0]["status"], "error")
        self.assertEqual(target.read_bytes(), b"external")

    def test_overwrite_and_delete_backups_are_recoverable(self):
        left_file = self.put(self.left, "change.bin", b"\0new")
        right_file = self.put(self.right, "change.bin", b"\0old")
        delete_file = self.put(self.right, "delete.txt", b"preserve")
        items = self.items({"change.bin": left_file}, {
            "change.bin": right_file, "delete.txt": delete_file,
        })
        self.assertTrue(next(item for item in items if item.relative == "change.bin").binary)
        operations = [
            operation_for(next(item for item in items if item.relative == "change.bin"),
                          "left_to_right", self.left, self.right),
            operation_for(next(item for item in items if item.relative == "delete.txt"),
                          "delete_right", self.left, self.right),
        ]
        backup = self.root / "backups"
        report = execute_plan(operations, self.left, self.right, backup_directory=backup)
        self.assertEqual([item["status"] for item in report.outcomes], ["completed", "completed"])
        self.assertEqual(right_file.read_bytes(), b"\0new")
        self.assertFalse(delete_file.exists())
        self.assertEqual((backup / "right/change.bin").read_bytes(), b"\0old")
        self.assertEqual((backup / "right/delete.txt").read_bytes(), b"preserve")

    def test_target_change_is_skipped_and_source_change_is_never_overwritten(self):
        left_file = self.put(self.left, "a.txt", b"source")
        right_file = self.put(self.right, "a.txt", b"old")
        item = self.items({"a.txt": left_file}, {"a.txt": right_file})[0]
        operation = operation_for(item, "left_to_right", self.left, self.right)
        right_file.write_bytes(b"newer target")
        skipped = execute_plan([operation], self.left, self.right)
        self.assertEqual(skipped.outcomes[0]["status"], "conflict")
        self.assertEqual(right_file.read_bytes(), b"newer target")
        overwritten = execute_plan([operation], self.left, self.right, conflict_policy="overwrite")
        self.assertEqual(overwritten.outcomes[0]["status"], "completed")
        left_file.write_bytes(b"changed source")
        stopped = execute_plan([operation], self.left, self.right, conflict_policy="overwrite")
        self.assertEqual(stopped.outcomes[0]["status"], "conflict")

    def test_exclusions_and_cancellation(self):
        first = self.put(self.left, "a.txt", b"a")
        second = self.put(self.left, "b.txt", b"b")
        hidden = self.put(self.left, "cache/hidden.txt", b"secret")
        items = self.items({"a.txt": first, "b.txt": second}, {}, ["cache"])
        self.assertTrue(next(item for item in items if item.relative == "cache").excluded)
        self.assertIsNone(operation_for(next(item for item in items if item.relative == "cache"),
                                         "left_to_right", self.left, self.right))
        operations = [operation_for(item, "left_to_right", self.left, self.right)
                      for item in items if item.relative in {"a.txt", "b.txt"}]
        report = execute_plan(operations, self.left, self.right,
                              cancelled=lambda: (self.right / "a.txt").exists())
        self.assertTrue(report.cancelled)
        self.assertEqual(len(report.outcomes), 1)
        self.assertTrue((self.right / "a.txt").exists())
        self.assertFalse((self.right / "b.txt").exists())
        self.assertTrue(hidden.exists())

    def test_per_file_error_is_reported_and_later_file_completes(self):
        first = self.put(self.left, "a.txt", b"a")
        second = self.put(self.left, "b.txt", b"b")
        items = self.items({"a.txt": first, "b.txt": second}, {})
        operations = [operation_for(item, "left_to_right", self.left, self.right) for item in items]
        (self.right / "a.txt").mkdir()  # A new directory conflicts with the planned file.
        report = execute_plan(operations, self.left, self.right, conflict_policy="overwrite")
        self.assertEqual([item["status"] for item in report.outcomes], ["error", "completed"])
        self.assertEqual((self.right / "b.txt").read_bytes(), b"b")

    def test_stop_on_conflict_reports_remaining_work(self):
        first = self.put(self.left, "a.txt", b"a")
        second = self.put(self.left, "b.txt", b"b")
        items = self.items({"a.txt": first, "b.txt": second}, {})
        operations = [operation_for(item, "left_to_right", self.left, self.right) for item in items]
        self.put(self.right, "a.txt", b"new target")
        report = execute_plan(operations, self.left, self.right, conflict_policy="stop")
        self.assertTrue(report.stopped_on_conflict)
        self.assertEqual(len(report.outcomes), 1)
        self.assertFalse((self.right / "b.txt").exists())

    def test_unsafe_paths_and_backup_inside_target_are_rejected(self):
        with self.assertRaises(SyncError):
            safe_path(self.right, "../outside.txt")
        source = self.put(self.left, "a.txt", b"a")
        item = self.items({"a.txt": source}, {})[0]
        operation = operation_for(item, "left_to_right", self.left, self.right)
        with self.assertRaises(SyncError):
            execute_plan([operation], self.left, self.right, backup_directory=self.right / "backup")
        self.assertFalse((self.right / "a.txt").exists())

    def test_case_collisions_are_not_offered_as_sync_actions(self):
        left = self.put(self.left, "Config.txt", b"left")
        right = self.put(self.right, "config.txt", b"right")
        items = self.items({"Config.txt": left}, {"config.txt": right})
        self.assertEqual([item.status for item in items], ["error", "error"])
        self.assertTrue(all(operation_for(item, "left_to_right", self.left, self.right) is None
                            for item in items))


if __name__ == "__main__":
    unittest.main()
