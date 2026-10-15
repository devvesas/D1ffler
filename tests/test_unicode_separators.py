"""Literal text boundaries survive Qt paragraphs, editing, saves and recovery."""

import codecs
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QMimeData, Qt
from PyQt6.QtGui import QKeyEvent, QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox, QTreeWidgetItem

from d1ffler.comparison_export import text_html_chunks
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.recovery_dialog import RecoveryDialog
from d1ffler.text_comparison import compare_text_sources
from d1ffler.text_endings import TextRepresentationError


class UnicodeSeparatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.windows = []
        self.addCleanup(self.cleanup_windows)
        self.window = self.new_window()

    def new_window(self):
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.windows.append(window)
        return window

    def cleanup_windows(self):
        for window in self.windows:
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                window.close()
            window.deleteLater()
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

    def test_save_and_save_as_round_trip_unicode_and_mixed_boundaries(self):
        texts = ["alpha\u2029beta", "alpha\u2029beta\ngamma", "\u2029alpha\u2029\u2029",
                 "alpha\u2029beta\r\ngamma\rdelta\nepsilon\u2028zeta\x85last",
                 "😀\u2029\u2063\nnon\u00a0breaking\u2029", "\u2029", "", "alpha\u2028beta\x85gamma"]
        encodings = [("utf-8", b""), ("utf-8", codecs.BOM_UTF8),
                     ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
                     ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE)]
        for number, (encoding, bom) in enumerate(encodings):
            for case, text in enumerate(texts):
                with self.subTest(encoding=encoding, bom=bom, text=text):
                    source = self.root / f"source-{number}-{case}.txt"
                    output = self.root / f"copy-{number}-{case}.txt"
                    original = bom + text.encode(encoding)
                    source.write_bytes(original)
                    self.window._load_panel(0, "file", source)
                    panel = self.window.panels[0]
                    self.assertFalse(panel["binary"])
                    self.assertEqual(self.text(0), text)
                    self.assertEqual(self.window._panel_text(panel), text.replace("\r\n", "\n").replace("\r", "\n"))
                    self.assertTrue(self.window._save_panel_file(0))
                    self.assertEqual(source.read_bytes(), original)
                    with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(output), "")):
                        self.assertTrue(self.window._save_panel_file(0, save_as=True))
                    self.assertEqual(output.read_bytes(), original)
                    self.assertFalse(panel["dirty"])

    def test_edits_alignment_and_undo_retain_all_separator_types(self):
        original = "😀\u2029alpha\u2028beta\x85gamma\vdelta\fepsilon\x1clast\x1dmore\x1etail\r\nend\r"
        self.load(original, original)
        editor = self.window.panels[1]["editor"]
        cursor = QTextCursor(editor.document().findBlockByNumber(1))
        editor.setTextCursor(cursor)
        QTest.keyClicks(editor, "edit ")
        edited = original.replace("alpha", "edit alpha")
        self.assertEqual(self.text(), edited)
        # Changing another panel rebuilds the aligned display with a gap.
        self.window.panels[0]["editor"].replace_logical_text("extra\n" + original)
        self.assertEqual(self.text(), edited)
        editor.undo()
        self.assertEqual(self.text(), original)
        self.assertFalse(self.window.panels[1]["dirty"])
        editor.redo()
        self.assertEqual(self.text(), edited)

    def test_hunk_and_selection_transfer_are_lossless_and_undoable(self):
        left = "a\u2029extra\r\ntail\u2028end\n"
        right = "a\u2029tail\u2028end\n"
        self.load(left, right)
        editor = self.window.panels[1]["editor"]
        self.window._apply_hunk_to_panel(0, 1, self.window._hunks[0])
        self.assertEqual(self.text(), left)
        editor.undo()
        self.assertEqual(self.text(), right)
        editor.redo()
        self.assertEqual(self.text(), left)
        editor.undo()
        source = self.window.panels[0]["editor"]
        cursor = source.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        source.setTextCursor(cursor)
        self.window._apply_selection_to_panel(0, 1)
        self.assertEqual(self.text(), left)
        editor.undo()
        self.assertEqual(self.text(), right)

    def test_paste_copy_and_unicode_key_input_preserve_literal_boundaries(self):
        editor = self.window.panels[1]["editor"]
        text = "alpha\u2029beta\r\ngamma\u2028last\x85"
        mime = QMimeData()
        mime.setText(text)
        editor.insertFromMimeData(mime)
        self.assertEqual(self.text(), text)
        cursor = editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        editor.setTextCursor(cursor)
        editor.copy()
        self.assertEqual(self.app.clipboard().text(), text)
        editor.undo()
        self.assertEqual(self.text(), "")
        editor.redo()
        self.assertEqual(self.text(), text)
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        event = QKeyEvent(QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, "\u2029")
        self.app.sendEvent(editor, event)
        self.assertEqual(self.text(), text + "\u2029")
        editor.undo()
        self.assertEqual(self.text(), text)

    def test_search_replacement_retains_surrounding_and_inserted_separators(self):
        original = "cat\u2029cat\r\ntail"
        self.load(original, original)
        self.window.search_input.setText("cat")
        self.window.replacement_input.setText("dog\u2029new")
        self.window._active_panel_index = 1
        with patch("d1ffler.text_search_mixin.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window._replace_all()
        self.assertEqual(self.text(), original.replace("cat", "dog\u2029new"))

    def test_folder_preview_save_preserves_literal_separators(self):
        text = "alpha\u2029beta\r\ngamma\u2028tail"
        folders = [self.root / "left", self.root / "right"]
        for index, folder in enumerate(folders):
            folder.mkdir()
            (folder / "sample.txt").write_bytes(text.encode("utf-8"))
            self.window._load_panel(index, "folder", folder)
        item = QTreeWidgetItem(["sample.txt"])
        item.setData(0, Qt.ItemDataRole.UserRole, "sample.txt")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        self.window._open_folder_item(self.window.panels[0]["widget"], item)
        self.assertEqual(self.text(), text)
        editor = self.window.panels[1]["editor"]
        editor.insertPlainText("edited ")
        self.assertTrue(self.window._save_panel_file(1))
        self.assertEqual((folders[1] / "sample.txt").read_bytes(), ("edited " + text).encode("utf-8"))

    def test_merge_result_and_recovered_history_keep_separators(self):
        self.window.merge_mode_button.setChecked(True)
        text = "base\u2029tail\n"
        for panel in self.window.panels[:3]:
            panel["editor"].setPlainText(text)
        self.assertEqual(self.text(3), text)
        result = self.window.panels[3]["editor"]
        result.replace_logical_text("edited\u2029result\r\n", {"1": "manual"})
        snapshot = self.window._recovery_snapshot()
        recovered = self.new_window()
        self.assertTrue(recovered._restore_recovery(snapshot))
        editor = recovered.panels[3]["editor"]
        self.assertEqual(editor.logical_text_with_endings(), "edited\u2029result\r\n")
        editor.insertPlainText("extra ")
        editor.undo()
        self.assertEqual(editor.logical_text_with_endings(), "edited\u2029result\r\n")
        self.assertEqual(recovered.merge_resolutions, {1: "manual"})

    def test_restart_recovery_then_save_preserves_complete_buffer(self):
        original = "alpha\u2029beta\r\ngamma\u2028tail\x85"
        source, _other = self.load(original, original)
        self.window.panels[0]["editor"].insertPlainText("unsaved ")
        self.assertTrue(self.window._write_recovery_snapshot())
        self.window._recovery_timer.stop()
        self.window._recovery_store.release()
        recovered = self.new_window()

        def restore(dialog):
            dialog.choice = "restore"
            return 0

        with patch.object(RecoveryDialog, "exec", restore):
            self.assertTrue(recovered._offer_recovery())
        self.assertEqual(recovered._panel_text_with_line_endings(recovered.panels[0]), "unsaved " + original)
        self.assertTrue(recovered._save_panel_file(0))
        self.assertEqual(source.read_bytes(), ("unsaved " + original).encode("utf-8"))

    def test_gui_comparison_and_report_use_complete_underlying_text(self):
        left, right = "alpha\u2029beta\ngamma", "alpha\u2029changed\ngamma"
        self.load(left, right)
        sources = self.window._text_comparison_sources()
        self.assertEqual([s["text"] for s in sources], [left, right])
        self.assertEqual(self.window._text_comparisons[0], compare_text_sources(*sources, **self.window._text_comparison_options()))
        page = "".join(chunk for chunk, _ in text_html_chunks(sources, lambda: False))
        for word in ("alpha", "beta", "gamma", "changed"):
            self.assertIn(word, page)
        self.assertIn("Content differences", page)

    def test_inconsistent_metadata_stops_save_and_retains_previous_recovery(self):
        original = "alpha\u2029beta\ngamma"
        source, _other = self.load(original, original)
        editor = self.window.panels[0]["editor"]
        editor.insertPlainText("unsaved ")
        self.window._write_recovery_snapshot()
        before = self.window._recovery_store.session_path.read_bytes()
        state = editor.history.state()
        editor.history._set_state(state["endings"][:-1], state["metadata"])
        try:
            with self.assertRaises(TextRepresentationError):
                editor.logical_text_with_endings()
            with patch("d1ffler.panel_mixin.QMessageBox.critical") as notice:
                self.assertFalse(self.window._save_panel_file(0))
            self.assertIn("metadata disagree", notice.call_args.args[2])
            self.assertEqual(source.read_bytes(), original.encode("utf-8"))
            self.assertFalse(self.window._write_recovery_snapshot())
            self.assertEqual(self.window._recovery_store.session_path.read_bytes(), before)
            self.assertIn("gamma", editor.history.text())
        finally:
            editor.history._set_state(state["endings"], state["metadata"])


if __name__ == "__main__":
    unittest.main()
