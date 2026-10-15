"""Explicit metadata writes preserve content and honor reviewed state."""

import errno
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from d1ffler.folder_restore import build_restore_plan, execute_restore
from d1ffler.folder_sync import (MetadataUnavailable, build_items, execute_plan, metadata_state,
                         operation_for, _write_metadata)


class MetadataSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.left, self.right = self.root / "left", self.root / "right"
        self.left.mkdir()
        self.right.mkdir()
        self.a, self.b = self.left / "same.bin", self.right / "same.bin"
        for path in (self.a, self.b):
            path.write_bytes(b"same\0bytes")
        self.source_time = 1_600_000_000_123_456_700
        self.target_time = 1_700_000_000_000_000_000
        os.utime(self.a, ns=(self.source_time,) * 2)
        os.utime(self.b, ns=(self.target_time,) * 2)
        self.addCleanup(self.make_writable)

    def make_writable(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def operation(self, action="mtime_left_to_right"):
        item = build_items(self.left, self.right, {"same.bin": self.a}, {"same.bin": self.b})[0]
        return operation_for(item, action, self.left, self.right)

    def run_plan(self, operation=None, **kwargs):
        return execute_plan([operation or self.operation()], self.left, self.right, **kwargs)

    def set_source_permissions(self):
        self.a.chmod(stat.S_IREAD if os.name == "nt" else 0o755)

    def test_timestamp_only_keeps_bytes_inode_and_permissions(self):
        before = self.b.stat()
        operation = self.operation()
        with patch("d1ffler.folder_sync._copy_atomic", side_effect=AssertionError("must not copy content")):
            report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "completed")
        self.assertEqual(self.b.read_bytes(), b"same\0bytes")
        after = self.b.stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mode, before.st_mode)
        self.assertEqual(after.st_mtime_ns, self.a.stat().st_mtime_ns)
        change = report.outcomes[0]["metadata_changes"][0]
        self.assertEqual(change["before"], before.st_mtime_ns)
        self.assertEqual(change["after"], self.a.stat().st_mtime_ns)
        self.assertEqual(change["status"], "completed")

    def test_permissions_only_preserves_timestamp_and_bytes(self):
        self.set_source_permissions()
        before = self.b.stat()
        report = self.run_plan(self.operation("permissions_left_to_right"))
        self.assertEqual(report.outcomes[0]["status"], "completed")
        self.assertEqual(metadata_state(self.b).permission_key, metadata_state(self.a).permission_key)
        self.assertEqual(self.b.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.b.stat().st_ino, before.st_ino)
        self.assertEqual(self.b.read_bytes(), b"same\0bytes")

    def test_posix_writer_passes_exact_permission_bits(self):
        from unittest.mock import Mock
        from d1ffler.folder_sync import MetadataState
        target = Mock()
        with patch("d1ffler.folder_sync.os", SimpleNamespace(name="posix")), patch(
                "d1ffler.folder_sync.metadata_state", return_value=MetadataState(0, 0o2751)):
            _write_metadata(target, "permission_key", 0o2751)
        target.chmod.assert_called_once_with(0o2751)

    def test_permission_change_keeps_read_handle_for_byte_verification(self):
        self.set_source_permissions()
        operation = self.operation("permissions_left_to_right")
        original_open = Path.open

        def deny_reopen(path, *args, **kwargs):
            if path == self.b and metadata_state(path).permission_key == operation.source_metadata.permission_key:
                raise PermissionError("Read permission removed")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", deny_reopen):
            report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "completed")
        self.assertEqual(self.b.read_bytes(), b"same\0bytes")

    @unittest.skipUnless(os.name == "nt", "Windows writable state")
    def test_writable_field_precedes_timestamp_for_read_only_target(self):
        self.b.chmod(stat.S_IREAD)
        operation = self.operation("metadata_left_to_right")
        self.assertEqual(operation.metadata_fields, ("permission_key", "modified_ns"))
        writes = []

        def write(target, name, value):
            if name == "modified_ns":
                self.assertTrue(metadata_state(target).permission_key)
            writes.append(name)
            _write_metadata(target, name, value)

        with patch("d1ffler.folder_sync._write_metadata", side_effect=write):
            report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "completed")
        self.assertEqual(writes, ["permission_key", "modified_ns"])

    def test_reverse_direction_and_only_different_requested_fields(self):
        operation = self.operation("metadata_right_to_left")
        self.assertEqual(operation.metadata_fields, ("modified_ns",))
        report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["target_side"], "left")
        self.assertEqual(self.a.stat().st_mtime_ns, self.b.stat().st_mtime_ns)
        self.assertIsNone(self.operation("metadata_left_to_right"))

    def test_dry_run_reports_exact_values_without_backup_or_writes(self):
        self.set_source_permissions()
        before = metadata_state(self.b)
        backup = self.root / "backups"
        with patch("d1ffler.folder_sync._write_metadata", side_effect=AssertionError("dry run wrote")):
            report = self.run_plan(self.operation("metadata_left_to_right"), dry_run=True,
                                   backup_directory=backup)
        self.assertEqual(report.outcomes[0]["status"], "dry run")
        changes = report.to_dict()["outcomes"][0]["metadata_changes"]
        self.assertEqual([change["field"] for change in changes], ["modified_ns", "permission_key"])
        self.assertIn(str(self.a.stat().st_mtime_ns), report.outcomes[0]["detail"])
        self.assertEqual(metadata_state(self.b), before)
        self.assertFalse(backup.exists())

    def test_target_metadata_change_skip_stop_and_explicit_overwrite(self):
        operation = self.operation()
        newer = self.target_time + 10_000_000_000
        os.utime(self.b, ns=(newer,) * 2)
        skipped = self.run_plan(operation)
        self.assertEqual(skipped.outcomes[0]["status"], "conflict")
        self.assertEqual(self.b.stat().st_mtime_ns, newer)
        stopped = self.run_plan(operation, conflict_policy="stop")
        self.assertTrue(stopped.stopped_on_conflict)
        overwritten = self.run_plan(operation, conflict_policy="overwrite")
        change = overwritten.outcomes[0]["metadata_changes"][0]
        self.assertEqual(change["before"], newer)
        self.assertEqual(change["reviewed_before"], operation.target_metadata.modified_ns)
        self.assertEqual(overwritten.outcomes[0]["status"], "completed")

    def test_target_permission_change_conflicts_with_timestamp_action(self):
        operation = self.operation()
        self.b.chmod(stat.S_IREAD if os.name == "nt" else 0o600)
        report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "conflict")

    def test_target_bytes_change_with_original_metadata_is_detected(self):
        operation = self.operation()
        self.b.write_bytes(b"newer disk")
        os.utime(self.b, ns=(self.target_time,) * 2)
        report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "conflict")
        report = self.run_plan(operation, conflict_policy="overwrite")
        self.assertEqual(report.outcomes[0]["status"], "completed")
        self.assertEqual(self.b.read_bytes(), b"newer disk")

    def test_changed_source_metadata_is_never_adopted(self):
        operation = self.operation()
        os.utime(self.a, ns=(self.target_time,) * 2)
        report = self.run_plan(operation, conflict_policy="overwrite")
        self.assertEqual(report.outcomes[0]["status"], "conflict")
        self.assertEqual(self.b.stat().st_mtime_ns, self.target_time)

    def test_changed_target_after_backup_is_checked_before_write(self):
        operation = self.operation()
        from d1ffler.folder_sync import _backup

        def changed(*args):
            backup = _backup(*args)
            self.b.write_bytes(b"external change")
            return backup

        with patch("d1ffler.folder_sync._backup", side_effect=changed):
            report = self.run_plan(operation, backup_directory=self.root / "backups", conflict_policy="stop")
        self.assertEqual(report.outcomes[0]["status"], "conflict")
        self.assertTrue(report.stopped_on_conflict)
        self.assertEqual(self.b.read_bytes(), b"external change")

    def test_cancel_between_fields_reports_partial_completion(self):
        self.set_source_permissions()
        operation = self.operation("metadata_left_to_right")
        writes = []

        def write(*args):
            _write_metadata(*args)
            writes.append(args[1])

        with patch("d1ffler.folder_sync._write_metadata", side_effect=write):
            report = self.run_plan(operation, cancelled=lambda: bool(writes))
        self.assertTrue(report.cancelled)
        self.assertEqual(writes, ["modified_ns"])
        self.assertEqual([change["status"] for change in report.outcomes[0]["metadata_changes"]],
                         ["completed", "planned"])
        self.assertEqual(metadata_state(self.b).permission_key, operation.target_metadata.permission_key)

    def test_external_change_between_fields_does_not_get_adopted(self):
        self.set_source_permissions()
        operation = self.operation("metadata_left_to_right")

        def write(*args):
            _write_metadata(*args)
            self.b.write_bytes(b"external")

        with patch("d1ffler.folder_sync._write_metadata", side_effect=write):
            report = self.run_plan(operation, conflict_policy="overwrite")
        self.assertEqual(report.outcomes[0]["status"], "conflict")
        self.assertEqual(self.b.read_bytes(), b"external")
        self.assertEqual(metadata_state(self.b).permission_key, operation.target_metadata.permission_key)

    def test_unavailable_snapshot_and_unsupported_filesystem_never_complete(self):
        item = build_items(self.left, self.right, {"same.bin": self.a}, {"same.bin": self.b})[0]
        item = replace(item, left_metadata=replace(item.left_metadata, modified_ns=None, reason="Unavailable"))
        with self.assertRaisesRegex(MetadataUnavailable, "unavailable"):
            operation_for(item, "mtime_left_to_right", self.left, self.right)
        operation = self.operation()
        with patch("d1ffler.folder_sync.os.utime", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "unavailable")
        self.assertIn("unsupported", report.outcomes[0]["detail"])
        with patch("d1ffler.folder_sync.os.utime", return_value=None):
            report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "unavailable")
        self.assertIn("exact requested", report.outcomes[0]["detail"])

    def test_unsupported_second_field_reports_first_field_completion(self):
        self.set_source_permissions()
        operation = self.operation("metadata_left_to_right")

        def write(target, name, value):
            if name == "permission_key":
                raise MetadataUnavailable("Permissions unsupported")
            _write_metadata(target, name, value)

        with patch("d1ffler.folder_sync._write_metadata", side_effect=write):
            report = self.run_plan(operation)
        self.assertEqual(report.outcomes[0]["status"], "unavailable")
        self.assertEqual([change["status"] for change in report.outcomes[0]["metadata_changes"]],
                         ["completed", "unavailable"])
        self.assertEqual(self.b.stat().st_mtime_ns, self.a.stat().st_mtime_ns)

    def test_missing_target_cannot_be_created_by_metadata_sync(self):
        operation = self.operation()
        self.b.unlink()
        report = self.run_plan(operation, conflict_policy="overwrite")
        self.assertEqual(report.outcomes[0]["status"], "conflict")
        self.assertFalse(self.b.exists())

    def test_backup_and_restore_preserve_content_and_original_metadata(self):
        self.set_source_permissions()
        original = metadata_state(self.b)
        inode = self.b.stat().st_ino
        report = self.run_plan(self.operation("metadata_left_to_right"),
                               backup_directory=self.root / "backups").to_dict()
        self.assertEqual(report["outcomes"][0]["status"], "completed")
        backup = self.root / "backups/right/same.bin"
        self.assertEqual(metadata_state(backup), original)
        items = build_restore_plan(report)
        self.assertEqual(items[0].status, "ready")
        dry = execute_restore(items, report, dry_run=True)
        self.assertEqual(dry.outcomes[0]["status"], "dry run")
        self.assertNotEqual(metadata_state(self.b), original)
        restored = execute_restore(items, report)
        self.assertEqual(restored.outcomes[0]["status"], "completed")
        self.assertEqual(metadata_state(self.b), original)
        self.assertEqual(self.b.stat().st_ino, inode)
        self.assertEqual(self.b.read_bytes(), b"same\0bytes")
        self.assertEqual(backup.read_bytes(), b"same\0bytes")

    def test_restore_detects_target_and_backup_metadata_changes(self):
        report = self.run_plan(backup_directory=self.root / "backups").to_dict()
        items = build_restore_plan(report)
        os.utime(self.b, ns=(self.target_time,) * 2)
        self.assertEqual(build_restore_plan(report)[0].status, "changed")
        skipped = execute_restore(items, report)
        self.assertEqual(skipped.outcomes[0]["status"], "conflict")
        backup = self.root / "backups/right/same.bin"
        os.utime(backup, ns=(self.source_time,) * 2)
        self.assertEqual(build_restore_plan(report)[0].status, "unavailable")
        failed = execute_restore(items, report, conflict_policy="overwrite")
        self.assertEqual(failed.outcomes[0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
