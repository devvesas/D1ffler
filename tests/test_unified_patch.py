"""Patch parsing, exact hunk preview, editor edits, and folder writes."""

import difflib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.comparison_export import text_unified_chunks
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.patch_dialog import PatchDialog
from d1ffler.unified_patch import PatchApplyError, PatchError, apply_folder_plan, apply_hunks, parse_patch, plan_folder_patch


def patch(old_path, new_path, before, after):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{old_path}" if old_path else "/dev/null",
        tofile=f"b/{new_path}" if new_path else "/dev/null"))


class UnifiedPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_parser_and_hunk_preview_apply_offset_and_reject_ambiguity(self):
        files = parse_patch(patch("sample.txt", "sample.txt", "one\ntwo\n", "one\nchanged\n"))
        result, statuses = apply_hunks("prefix\none\ntwo\n", files[0], (0,))
        self.assertEqual(result, "prefix\none\nchanged\n")
        self.assertEqual(statuses[0].status, "offset")
        _result, statuses = apply_hunks("prefix\none\ntwo\nspacer\none\ntwo\n", files[0], (0,))
        self.assertEqual(statuses[0].status, "ambiguous")
        with self.assertRaises(PatchError):
            parse_patch("--- a/../escape.txt\n+++ b/../escape.txt\n@@ -1 +1 @@\n-a\n+b\n")
        with self.assertRaises(PatchError):
            parse_patch("GIT binary patch\n")

    def test_hunks_preserve_existing_line_endings_and_allow_partial_selection(self):
        before = "a\nb\nc\nd\ne\n"
        after = "a\nB\nc\nd\nE\n"
        raw = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="a/item.txt", tofile="b/item.txt", n=0))
        file = parse_patch(raw)[0]
        self.assertEqual(len(file.hunks), 2)
        result, statuses = apply_hunks(before.replace("\n", "\r\n"), file, (0,))
        self.assertEqual(result, "a\r\nB\r\nc\r\nd\r\ne\r\n")
        self.assertEqual([item.status for item in statuses], ["matching", "skipped"])
        no_newline = ("--- a/item.txt\n+++ b/item.txt\n@@ -1 +1 @@\n"
                      "-old\n\\ No newline at end of file\n"
                      "+new\n\\ No newline at end of file\n")
        result, statuses = apply_hunks("old", parse_patch(no_newline)[0], (0,))
        self.assertEqual(result, "new")
        self.assertEqual(statuses[0].status, "matching")

    def test_D1ffler_export_can_be_reopened_for_an_editor(self):
        raw = "".join(chunk for chunk, _units in text_unified_chunks(
            [{"name": "Left", "text": "before\n"},
             {"name": "Right", "text": "after\n"}], lambda: False))
        file = parse_patch(raw)[0]
        result, statuses = apply_hunks("before\n", file, (0,))
        self.assertEqual(result, "after\n")
        self.assertEqual(statuses[0].status, "matching")

    def test_folder_create_delete_and_rename_use_reviewed_plan(self):
        source = self.root / "source.txt"
        gone = self.root / "gone.txt"
        source.write_text("old\n", encoding="utf-8")
        gone.write_text("remove\n", encoding="utf-8")
        raw = (patch(None, "new.txt", "", "created\n") +
               patch("gone.txt", None, "remove\n", "") +
               "diff --git a/source.txt b/renamed.txt\n"
               "similarity index 100%\nrename from source.txt\nrename to renamed.txt\n")
        files = parse_patch(raw)
        self.assertEqual(len(files), 3)
        selected = {index: tuple(range(len(item.hunks))) for index, item in enumerate(files)}
        plan = plan_folder_patch(self.root, files, selected)
        self.assertEqual(plan.errors, [])
        self.assertEqual([item.effect for item in plan.operations], ["create", "delete", "rename"])
        backup = self.root.parent / (self.root.name + "-backup")
        outcomes = apply_folder_plan(self.root, plan, backup)
        self.assertEqual(len(outcomes), 3)
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "created\n")
        self.assertFalse(gone.exists())
        self.assertFalse(source.exists())
        self.assertEqual((self.root / "renamed.txt").read_text(encoding="utf-8"), "old\n")
        self.assertEqual((backup / "gone.txt").read_text(encoding="utf-8"), "remove\n")
        self.addCleanup(lambda: __import__("shutil").rmtree(backup, ignore_errors=True))

    def test_folder_plan_detects_changed_target_before_writing(self):
        target = self.root / "item.txt"
        target.write_text("before\n", encoding="utf-8")
        files = parse_patch(patch("item.txt", "item.txt", "before\n", "after\n"))
        plan = plan_folder_patch(self.root, files, {0: (0,)})
        self.assertEqual(plan.errors, [])
        target.write_text("changed externally\n", encoding="utf-8")
        with self.assertRaises(PatchError):
            apply_folder_plan(self.root, plan)
        self.assertEqual(target.read_text(encoding="utf-8"), "changed externally\n")

    def folder_plan(self, raw, root=None):
        files = parse_patch(raw)
        plan = plan_folder_patch(root or self.root, files,
                                 {i: tuple(range(len(item.hunks))) for i, item in enumerate(files)})
        self.assertEqual(plan.errors, [])
        return plan

    def inject_at_flush(self, action, number=1):
        fsync = os.fsync
        count = 0

        def flush(descriptor):
            nonlocal count
            fsync(descriptor)
            count += 1
            if count == number:
                action()

        return mock_patch("d1ffler.unified_patch.os.fsync", side_effect=flush)

    def backups(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def assert_no_temporaries(self, *directories):
        for directory in (self.root, *directories):
            self.assertEqual(list(directory.rglob(".D1ffler-patch-*")), [])

    def test_overwrite_rechecks_modification_and_deletion_after_temporary_flush(self):
        target = self.root / "item.txt"
        for action in (lambda: target.write_bytes(b"new external edits\n"), target.unlink):
            with self.subTest(action=action):
                target.write_bytes(b"before\n")
                plan = self.folder_plan(patch("item.txt", "item.txt", "before\n", "patched\n"))
                with self.inject_at_flush(action), self.assertRaisesRegex(PatchApplyError, "changed during application"):
                    apply_folder_plan(self.root, plan)
                if target.exists():
                    self.assertEqual(target.read_bytes(), b"new external edits\n")
                self.assert_no_temporaries()

    def test_create_rechecks_external_creation_after_temporary_flush(self):
        target = self.root / "nested" / "created.txt"
        plan = self.folder_plan(patch(None, "nested/created.txt", "", "patched\n"))
        with self.inject_at_flush(lambda: target.write_bytes(b"external creation\n")), \
                self.assertRaisesRegex(PatchApplyError, "Target changed"):
            apply_folder_plan(self.root, plan)
        self.assertEqual(target.read_bytes(), b"external creation\n")
        self.assert_no_temporaries()

    def test_overwrite_rechecks_after_metadata_copy(self):
        target = self.root / "item.txt"
        target.write_bytes(b"before\n")
        plan = self.folder_plan(patch("item.txt", "item.txt", "before\n", "patched\n"))
        copymode = shutil.copymode

        def change_after_metadata(source, temporary):
            copymode(source, temporary)
            target.write_bytes(b"external edits\n")

        with mock_patch("d1ffler.unified_patch.shutil.copymode", side_effect=change_after_metadata), \
                self.assertRaisesRegex(PatchApplyError, "Source changed"):
            apply_folder_plan(self.root, plan)
        self.assertEqual(target.read_bytes(), b"external edits\n")
        self.assert_no_temporaries()

    def test_rename_rechecks_source_and_destination_during_temporary_writing(self):
        source, target = self.root / "old.txt", self.root / "new.txt"
        for changed in (source, target):
            with self.subTest(changed=changed):
                source.write_bytes(b"before\n")
                target.unlink(missing_ok=True)
                plan = self.folder_plan(patch("old.txt", "new.txt", "before\n", "patched\n"))
                with self.inject_at_flush(lambda: changed.write_bytes(b"external edits\n")), \
                        self.assertRaises(PatchApplyError) as failure:
                    apply_folder_plan(self.root, plan)
                self.assertEqual(failure.exception.outcomes, ())
                self.assertIsNone(failure.exception.partial_operation)
                self.assertEqual(changed.read_bytes(), b"external edits\n")
                self.assertTrue(source.exists())
                if changed == source:
                    self.assertFalse(target.exists())
                self.assert_no_temporaries()

    def test_delete_rechecks_after_backup_and_retains_reviewed_original(self):
        target = self.root / "gone.txt"
        for action in (lambda: target.write_bytes(b"external edits\n"), target.unlink):
            with self.subTest(action=action):
                target.write_bytes(b"before\n")
                backup = self.backups()
                plan = self.folder_plan(patch("gone.txt", None, "before\n", ""))
                # Change the source after the verified original backup commits.
                replace = os.replace

                def change_after_backup(old, new):
                    replace(old, new)
                    if Path(new) == backup / "gone.txt":
                        action()

                with mock_patch("d1ffler.unified_patch.os.replace", side_effect=change_after_backup), \
                        self.assertRaisesRegex(PatchApplyError, "Source changed"):
                    apply_folder_plan(self.root, plan, backup)
                self.assertEqual((backup / "gone.txt").read_bytes(), b"before\n")
                if target.exists():
                    self.assertEqual(target.read_bytes(), b"external edits\n")
                self.assert_no_temporaries(backup)

    def test_backup_is_reviewed_original_when_source_changes_during_backup_flush(self):
        source = self.root / "item.txt"
        source.write_bytes(b"before\n")
        backup = self.backups()
        plan = self.folder_plan(patch("item.txt", "item.txt", "before\n", "patched\n"))
        with self.inject_at_flush(lambda: source.write_bytes(b"external edits\n")), \
                self.assertRaisesRegex(PatchApplyError, "Source changed"):
            apply_folder_plan(self.root, plan, backup)
        self.assertEqual(source.read_bytes(), b"external edits\n")
        self.assertEqual((backup / "item.txt").read_bytes(), b"before\n")
        self.assert_no_temporaries(backup)

    def test_source_changed_before_backup_read_is_not_backed_up_as_reviewed(self):
        source = self.root / "item.txt"
        source.write_bytes(b"before\n")
        backup = self.backups()
        plan = self.folder_plan(patch("item.txt", "item.txt", "before\n", "patched\n"))
        read_bytes = Path.read_bytes

        def change_before_read(path):
            if path == source:
                source.write_bytes(b"external edits\n")
            return read_bytes(path)

        with mock_patch.object(Path, "read_bytes", change_before_read), \
                self.assertRaisesRegex(PatchApplyError, "reading backup"):
            apply_folder_plan(self.root, plan, backup)
        self.assertEqual(source.read_bytes(), b"external edits\n")
        self.assertFalse((backup / "item.txt").exists())
        self.assert_no_temporaries(backup)

    def test_external_backup_creation_is_not_overwritten(self):
        source = self.root / "item.txt"
        source.write_bytes(b"before\n")
        backup = self.backups()
        plan = self.folder_plan(patch("item.txt", "item.txt", "before\n", "patched\n"))
        with self.inject_at_flush(lambda: (backup / "item.txt").write_bytes(b"external backup\n")), \
                self.assertRaisesRegex(PatchApplyError, "Backup location changed or already exists"):
            apply_folder_plan(self.root, plan, backup)
        self.assertEqual(source.read_bytes(), b"before\n")
        self.assertEqual((backup / "item.txt").read_bytes(), b"external backup\n")
        self.assert_no_temporaries(backup)

    def test_backup_changed_during_patch_writing_stops_replacement(self):
        source = self.root / "item.txt"
        source.write_bytes(b"before\n")
        backup = self.backups()
        plan = self.folder_plan(patch("item.txt", "item.txt", "before\n", "patched\n"))
        for change in (lambda: (backup / "item.txt").write_bytes(b"altered backup\n"),
                       (backup / "item.txt").unlink):
            with self.subTest(change=change):
                (backup / "item.txt").unlink(missing_ok=True)
                with self.inject_at_flush(change, number=2), self.assertRaisesRegex(PatchApplyError, "Backup does not match"):
                    apply_folder_plan(self.root, plan, backup)
                self.assertEqual(source.read_bytes(), b"before\n")
                self.assert_no_temporaries(backup)

    def test_rename_source_removal_checks_both_files_and_reports_partial_destination(self):
        source, target = self.root / "old.txt", self.root / "new.txt"
        for changed in (source, target):
            with self.subTest(changed=changed):
                source.write_bytes(b"before\n")
                target.unlink(missing_ok=True)
                plan = self.folder_plan(patch("old.txt", "new.txt", "before\n", "patched\n"))
                replace = os.replace

                def change_after_destination(old, new):
                    replace(old, new)
                    if Path(new) == target:
                        changed.write_bytes(b"external edits\n")

                with mock_patch("d1ffler.unified_patch.os.replace", side_effect=change_after_destination), \
                        self.assertRaises(PatchApplyError) as failure:
                    apply_folder_plan(self.root, plan)
                self.assertEqual(failure.exception.outcomes, ())
                self.assertIn("new.txt was written", failure.exception.partial_operation)
                self.assertIn("source removal did not complete", str(failure.exception))
                self.assertEqual(changed.read_bytes(), b"external edits\n")
                self.assertTrue(source.exists())
                self.assertEqual(target.read_bytes(), b"patched\n" if changed == source else b"external edits\n")
                self.assert_no_temporaries()

    def test_multi_file_failure_reports_completed_operations_without_rollback(self):
        second = self.root / "second.txt"
        third = self.root / "third.txt"
        second.write_bytes(b"before\n")
        third.write_bytes(b"before\n")
        plan = self.folder_plan(patch(None, "first.txt", "", "created\n") +
                                patch("second.txt", "second.txt", "before\n", "patched\n") +
                                patch("third.txt", "third.txt", "before\n", "patched\n"))
        with self.inject_at_flush(lambda: second.write_bytes(b"external edits\n"), number=2), \
                self.assertRaises(PatchApplyError) as failure:
            apply_folder_plan(self.root, plan)
        self.assertEqual(len(failure.exception.outcomes), 1)
        self.assertIn("first.txt", failure.exception.outcomes[0])
        self.assertIn("Stopped after 1 completed", str(failure.exception))
        self.assertEqual((self.root / "first.txt").read_bytes(), b"created\n")
        self.assertEqual(second.read_bytes(), b"external edits\n")
        self.assertEqual(third.read_bytes(), b"before\n")
        self.assert_no_temporaries()

    def test_rename_unlink_failure_reports_written_destination_and_retained_source(self):
        source, target = self.root / "old.txt", self.root / "new.txt"
        source.write_bytes(b"before\n")
        plan = self.folder_plan(patch("old.txt", "new.txt", "before\n", "patched\n"))
        unlink = Path.unlink

        def fail_source_unlink(path, *args, **kwargs):
            if path == source:
                raise PermissionError("Source removal denied")
            return unlink(path, *args, **kwargs)

        with mock_patch.object(Path, "unlink", fail_source_unlink), self.assertRaises(PatchApplyError) as failure:
            apply_folder_plan(self.root, plan)
        self.assertEqual(failure.exception.outcomes, ())
        self.assertIn("new.txt was written", failure.exception.partial_operation)
        self.assertIn("Source removal denied", str(failure.exception))
        self.assertEqual(source.read_bytes(), b"before\n")
        self.assertEqual(target.read_bytes(), b"patched\n")
        self.assert_no_temporaries()

    def test_replaced_root_with_identical_content_invalidates_review(self):
        root = self.root / "target"
        root.mkdir()
        (root / "item.txt").write_bytes(b"before\n")
        plan = self.folder_plan(patch("item.txt", "item.txt", "before\n", "patched\n"), root)
        moved = self.root / "reviewed-folder"
        root.rename(moved)
        root.mkdir()
        (root / "item.txt").write_bytes(b"before\n")
        with self.assertRaisesRegex(PatchError, "Directory changed"):
            apply_folder_plan(root, plan)
        self.assertEqual((root / "item.txt").read_bytes(), b"before\n")
        self.assertEqual((moved / "item.txt").read_bytes(), b"before\n")
        self.assert_no_temporaries()

    def test_redirected_ancestor_cannot_replace_target_or_delete_a_spoofed_temporary(self):
        folder = self.root / "nested"
        folder.mkdir()
        (folder / "item.txt").write_bytes(b"before\n")
        external = self.backups()
        (external / "item.txt").write_bytes(b"external file\n")
        plan = self.folder_plan(patch("nested/item.txt", "nested/item.txt", "before\n", "patched\n"))
        moved = self.root / "original-folder"
        spoof = None
        redirected = False
        copymode = shutil.copymode

        def redirect(source, temporary):
            nonlocal spoof, redirected
            copymode(source, temporary)
            folder.rename(moved)
            spoof = external / Path(temporary).name
            spoof.write_bytes(b"external temporary\n")
            if sys.platform == "win32":
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(folder), str(external)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                folder.symlink_to(external, target_is_directory=True)
            redirected = True

        try:
            with mock_patch("d1ffler.unified_patch.shutil.copymode", side_effect=redirect), \
                    self.assertRaisesRegex(PatchApplyError, "temporary cleanup failed"):
                apply_folder_plan(self.root, plan)
            self.assertEqual((external / "item.txt").read_bytes(), b"external file\n")
            self.assertEqual(spoof.read_bytes(), b"external temporary\n")
            self.assertEqual((moved / "item.txt").read_bytes(), b"before\n")
            self.assertEqual(len(list(moved.glob(".D1ffler-patch-*"))), 1)
        finally:
            if redirected:
                folder.rmdir() if sys.platform == "win32" else folder.unlink()


class PatchDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qapp.processEvents()

    def test_preview_and_editor_apply_are_undoable_and_unsaved(self):
        left = self.root / "left.txt"
        right = self.root / "right.txt"
        left.write_text("one\ntwo\n", encoding="utf-8")
        right.write_text("one\ntwo\n", encoding="utf-8")
        self.window._load_panel(0, "file", left)
        self.window._load_panel(1, "file", right)
        raw = patch("right.txt", "right.txt", "one\ntwo\n", "one\nchanged\n")
        dialog = PatchDialog(self.window, raw, parse_patch(raw), self.root / "change.patch")
        self.addCleanup(dialog.deleteLater)
        self.assertGreater(dialog.target.count(), 0,
                           [(panel["kind"], panel["editor"].isReadOnly(), panel.get("binary"))
                            for panel in self.window.panels])
        target_index = next(i for i in range(dialog.target.count())
                            if dialog.target.itemData(i) == ("editor", 1))
        dialog.target.setCurrentIndex(target_index)
        self.assertTrue(dialog.apply_button.isEnabled(), dialog.summary.text())
        self.assertEqual(dialog._preview_result.replace("\r\n", "\n"), "one\nchanged\n")
        with mock_patch("d1ffler.patch_dialog.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes):
            dialog._apply()
        edited_text = self.window._panel_text(self.window.panels[1])
        edited_with_endings = self.window._panel_text_with_line_endings(self.window.panels[1])
        disk_text = right.read_text(encoding="utf-8")
        was_dirty = self.window.panels[1]["dirty"]
        self.window.panels[1]["editor"].undo()
        self.assertEqual(edited_text, "one\nchanged\n")
        self.assertEqual(edited_with_endings, dialog._preview_result)
        self.assertEqual(disk_text, "one\ntwo\n")
        self.assertTrue(was_dirty)
        self.assertEqual(self.window._panel_text(self.window.panels[1]), "one\ntwo\n")
        self.assertEqual(self.window._panel_text_with_line_endings(self.window.panels[1]),
                         right.read_bytes().decode("utf-8"))
        self.window.panels[1]["editor"].redo()
        self.assertEqual(self.window._panel_text_with_line_endings(self.window.panels[1]),
                         dialog._preview_result)
        self.window.panels[1]["editor"].undo()


if __name__ == "__main__":
    unittest.main()
