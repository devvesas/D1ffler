"""Comparison edits retain logical Qt history across aligned view rebuilds."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.comparison_context import cursor_location
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp


class UndoHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.close_window)

    def close_window(self):
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def load(self, left, right):
        paths = [self.root / "left.txt", self.root / "right.txt"]
        for index, (path, text) in enumerate(zip(paths, (left, right))):
            path.write_bytes(text.encode("utf-8"))
            self.window._load_panel(index, "file", path)
        return paths

    def text(self, index=1):
        return self.window._panel_text_with_line_endings(self.window.panels[index])

    def insert(self, index, text, at_end=False):
        editor = self.window.panels[index]["editor"]
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if at_end else QTextCursor.MoveOperation.Start)
        cursor.insertText(text)

    def wait_for_text(self):
        deadline = time.monotonic() + 10
        while self.window._text_jobs and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(self.window._text_jobs)

    def test_hunk_is_one_transaction_retains_previous_edit_and_other_history(self):
        paths = self.load("same\nleft\ntail\n", "same\nright\ntail\n")
        editor = self.window.panels[1]["editor"]
        self.insert(1, "!")
        before = self.text()
        self.insert(0, "source ", at_end=True)
        hunk = next(h for h in self.window._hunks if h[0] <= 1 <= h[1])
        self.window._apply_hunk_to_panel(0, 1, hunk)
        applied = self.text()
        self.assertIn("left", applied)
        self.assertTrue(editor.document().isUndoAvailable())
        editor.undo()
        self.assertEqual(self.text(), before)
        editor.redo()
        self.assertEqual(self.text(), applied)
        editor.undo()
        editor.undo()
        self.assertEqual(self.text(), "same\nright\ntail\n")
        self.assertFalse(self.window.panels[1]["dirty"])
        self.assertTrue(self.window.panels[0]["editor"].document().isUndoAvailable())
        self.assertEqual(paths[1].read_bytes(), b"same\nright\ntail\n")

    def test_opposite_alignment_preserves_selection_undo_and_redo_branch(self):
        self.load("a\nb\n", "a\nb\n")
        editor = self.window.panels[1]["editor"]
        self.insert(1, "edited ", at_end=True)
        cursor = editor.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(1, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        self.insert(0, "extra\n")
        self.window._refresh_diff()
        self.assertEqual(editor.textCursor().selectedText(), "a")
        self.assertEqual(cursor_location(editor, editor.textCursor().anchor()), [0, 0])
        editor.undo()
        self.assertEqual(self.text(), "a\nb\n")
        self.insert(0, "another\n")
        self.assertTrue(editor.document().isRedoAvailable())
        editor.redo()
        self.assertEqual(self.text(), "a\nb\nedited ")
        editor.undo()
        self.insert(1, "new")
        self.assertFalse(editor.document().isRedoAvailable())

    def test_insertion_deletion_unicode_and_mixed_endings(self):
        for left, right in (("😀\r\nextra\rtail\n", "😀\r\ntail\n"),
                            ("😀\r\ntail\n", "😀\r\nextra\rtail\n")):
            with self.subTest(left=left):
                self.load(left, right)
                editor = self.window.panels[1]["editor"]
                self.window._apply_hunk_to_panel(0, 1, self.window._hunks[0])
                self.assertEqual(self.text(), left)
                editor.undo()
                self.assertEqual(self.text(), right)
                editor.redo()
                self.assertEqual(self.text(), left)
                editor.undo()
                self.insert(1, "💡\n")
                editor.undo()
                self.assertEqual(self.text(), right)

    def test_selection_copy_with_padding_is_one_transaction_and_has_no_markers(self):
        self.load("a\r\nextra\nlast", "a")
        source = self.window.panels[0]["editor"]
        target = self.window.panels[1]["editor"]
        cursor = source.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        source.setTextCursor(cursor)
        # Exercise a target with fewer display rows, as before a delayed layout.
        target.set_alignment([0], ["a"])
        self.window._apply_selection_to_panel(0, 1)
        self.assertEqual(self.text(), "a\r\nextra\nlast")
        target.undo()
        self.assertEqual(self.text(), "a")
        self.assertFalse(target.document().isUndoAvailable())
        target.redo()
        self.assertEqual(self.text(), "a\r\nextra\nlast")

    def merge(self):
        self.window.panels[0]["editor"].setPlainText("base\n")
        self.window.panels[1]["editor"].setPlainText("local\n")
        self.window._toggle_merge_mode(True)
        self.window.panels[2]["editor"].setPlainText("incoming\n")
        self.assertEqual(len(self.window.merge_conflicts), 1)
        return self.window.panels[3]["editor"]

    def test_conflict_choice_restores_resolution_with_text_and_preserves_earlier_edit(self):
        editor = self.merge()
        self.insert(3, "tail", at_end=True)
        unresolved = self.text(3)
        self.window._resolve_merge_conflict("incoming")
        self.assertEqual(self.window.merge_resolutions, {1: "incoming"})
        resolved = self.text(3)
        editor.undo()
        self.assertEqual(self.text(3), unresolved)
        self.assertEqual(self.window.merge_resolutions, {})
        self.assertIn("Unresolved conflicts: 1", self.window.merge_summary.text())
        editor.redo()
        self.assertEqual(self.text(3), resolved)
        self.assertEqual(self.window.merge_resolutions, {1: "incoming"})
        editor.undo()
        editor.undo()
        self.assertNotIn("tail", self.text(3))

    def test_manual_resolution_is_undoable_even_without_a_text_change(self):
        editor = self.merge()
        cursor = editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText("manual result\n")
        self.window._resolve_merge_conflict("manual")
        self.assertEqual(self.window.merge_resolutions, {1: "manual"})
        editor.undo()
        self.assertEqual(self.text(3), "manual result\n")
        self.assertEqual(self.window.merge_resolutions, {})
        editor.redo()
        self.assertEqual(self.window.merge_resolutions, {1: "manual"})
        editor.undo()
        editor.undo()
        self.assertIn("<<<<<<<", self.text(3))

    def test_shortcuts_toolbar_readonly_and_recovery_follow_history(self):
        self.load("a\n", "a\n")
        editor = self.window.panels[1]["editor"]
        self.insert(1, "x")
        self.window._active_panel_index = 1
        self.window._update_undo_actions()
        self.assertTrue(self.window.undo_button.isEnabled())
        QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.text(), "a\n")
        self.assertTrue(self.window.redo_button.isEnabled())
        self.window.redo_button.click()
        self.assertEqual(self.text(), "xa\n")
        self.assertEqual(self.window._recovery_snapshot()["panels"][1]["text"], "xa\n")
        editor.setReadOnly(True)
        self.window._update_undo_actions()
        self.assertFalse(self.window.undo_button.isEnabled())
        editor.undo()
        self.window._apply_hunk_to_panel(0, 1, self.window._hunks[0])
        self.assertEqual(self.text(), "xa\n")

    def test_delayed_background_layout_does_not_erase_history(self):
        original = "".join(f"line {i}: " + "x" * 80 + "\r\n" for i in range(900))
        self.load(original, original)
        self.wait_for_text()
        editor = self.window.panels[1]["editor"]
        self.insert(1, "🦊 edit\n")
        self.insert(0, "extra\n")
        self.wait_for_text()
        editor.undo()
        self.wait_for_text()
        self.assertEqual(self.text(), original)
        self.assertFalse(self.window.panels[1]["dirty"])
        editor.redo()
        self.wait_for_text()
        self.assertEqual(self.text(), "🦊 edit\n" + original)

    def test_typing_groups_and_shortcuts_retain_history_after_realignment(self):
        self.load("a\nb\n", "a\nb\n")
        editor = self.window.panels[1]["editor"]
        QTest.keyClicks(editor, "hello")
        self.assertEqual(self.text(), "helloa\nb\n")
        self.insert(0, "extra\n")
        QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.text(), "a\nb\n")
        QTest.keyClick(editor, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.text(), "helloa\nb\n")
        editor.undo()
        QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.text(), "helloa\nb\n")

    def test_editing_before_trailing_spacer_preserves_absent_final_newline(self):
        self.load("a\nextra", "a")
        editor = self.window.panels[1]["editor"]
        self.insert(1, "💡")
        self.assertEqual(self.text(), "💡a")
        editor.undo()
        self.assertEqual(self.text(), "a")
        editor.redo()
        self.assertEqual(self.text(), "💡a")

    def test_typing_in_alignment_gap_does_not_save_synthetic_marker(self):
        self.load("a\nextra\nb\n", "a\nb\n")
        editor = self.window.panels[1]["editor"]
        cursor = QTextCursor(editor.document().findBlockByNumber(1))
        editor.setTextCursor(cursor)
        QTest.keyClicks(editor, "mine")
        self.assertEqual(self.text(), "a\nmine\nb\n")
        self.assertNotIn(editor.ALIGNMENT_PLACEHOLDER, self.text())
        editor.undo()
        self.assertEqual(self.text(), "a\nb\n")

    def test_folder_preview_hunk_undo_retains_source_and_dirty_state(self):
        folders = [self.root / "left", self.root / "right"]
        for index, folder in enumerate(folders):
            folder.mkdir()
            (folder / "file.txt").write_bytes(("a\nextra\nb\n" if index == 0 else "a\nb\n").encode())
            self.window._load_panel(index, "folder", folder)
        deadline = time.monotonic() + 5
        while self.window._folder_jobs and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.window._open_folder_item(self.window.panels[0]["widget"], self.window._folder_tree_items[0]["file.txt"])
        editor = self.window.panels[1]["editor"]
        self.window._apply_hunk_to_panel(0, 1, self.window._hunks[0])
        self.assertEqual(self.text(), "a\nextra\nb\n")
        self.assertTrue(self.window.panels[1]["dirty"])
        editor.undo()
        self.assertEqual(self.text(), "a\nb\n")
        self.assertFalse(self.window.panels[1]["dirty"])
        editor.redo()
        self.assertEqual((folders[1] / "file.txt").read_bytes(), b"a\nb\n")

    def test_recovered_resolution_remains_part_of_later_history(self):
        self.merge()
        self.window._resolve_merge_conflict("local")
        snapshot = self.window._recovery_snapshot()
        original = self.window
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "recovered-settings.json")
        def close_original():
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                original.close()
            original.deleteLater()
        self.addCleanup(close_original)
        self.assertTrue(self.window._restore_recovery(snapshot))
        editor = self.window.panels[3]["editor"]
        self.assertEqual(self.window.merge_resolutions, {1: "local"})
        self.insert(3, "extra ")
        editor.undo()
        self.assertEqual(self.window.merge_resolutions, {1: "local"})
        self.assertNotIn("extra", self.text(3))

    def test_multiple_conflict_choices_restore_each_resolution_independently(self):
        self.window.panels[0]["editor"].setPlainText("base one\nsame\nbase two\n")
        self.window.panels[1]["editor"].setPlainText("local one\nsame\nlocal two\n")
        self.window._toggle_merge_mode(True)
        self.window.panels[2]["editor"].setPlainText("incoming one\nsame\nincoming two\n")
        self.assertEqual(len(self.window.merge_conflicts), 2)
        editor = self.window.panels[3]["editor"]
        self.window._resolve_merge_conflict("local")
        first = self.text(3)
        self.window.merge_conflict_choice.setCurrentIndex(1)
        self.window._resolve_merge_conflict("base")
        self.assertEqual(self.window.merge_resolutions, {1: "local", 2: "base"})
        editor.undo()
        self.assertEqual(self.text(3), first)
        self.assertEqual(self.window.merge_resolutions, {1: "local"})
        editor.undo()
        self.assertEqual(self.window.merge_resolutions, {})
        editor.redo()
        editor.redo()
        self.assertEqual(self.window.merge_resolutions, {1: "local", 2: "base"})

    def test_save_retains_history_and_new_source_resets_it(self):
        _, path = self.load("a\n", "a\n")
        editor = self.window.panels[1]["editor"]
        self.insert(1, "saved ")
        with patch("d1ffler.panel_mixin.QMessageBox.critical") as error:
            self.assertTrue(self.window._save_panel_file(1), error.call_args)
        self.assertFalse(self.window.panels[1]["dirty"])
        editor.undo()
        self.assertEqual(self.text(), "a\n")
        self.assertTrue(self.window.panels[1]["dirty"])
        self.assertEqual(path.read_bytes(), b"saved a\n")
        editor.redo()
        self.assertFalse(self.window.panels[1]["dirty"])
        self.window._load_panel(1, "file", path)
        self.assertFalse(editor.document().isUndoAvailable())
        self.assertFalse(editor.document().isRedoAvailable())


if __name__ == "__main__":
    unittest.main()
