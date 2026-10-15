"""GUI, file/folder, CLI and exports agree about content, rules and formats."""

import codecs
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.comparison_export import text_html_chunks, text_unified_chunks
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp, _files_match_cancellable
from d1ffler.unified_patch import apply_hunks, parse_patch


class TextEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.windows = []
        self.addCleanup(self.cleanup)
        self.window = self.new_window()

    def new_window(self):
        window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / f"settings-{len(self.windows)}.json")
        self.windows.append(window)
        return window

    def cleanup(self):
        for window in self.windows:
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def load(self, left, right, left_encoding="utf-8", right_encoding="utf-8", left_bom=b"", right_bom=b""):
        paths = [self.root / "left.txt", self.root / "right.txt"]
        for index, (path, text, encoding, bom) in enumerate(zip(paths, (left, right),
                                                              (left_encoding, right_encoding), (left_bom, right_bom))):
            path.write_bytes(bom + text.encode(encoding))
            self.window._load_panel(index, "file", path, confirm=False)
        return paths

    def options(self, whitespace=False, endings=False):
        self.window.settings.update(ignore_whitespace=whitespace, ignore_line_endings=endings)
        self.window._refresh_diff()

    def result(self):
        return self.window._text_comparisons[0]

    def test_original_three_examples_agree_in_gui_file_cli_and_html(self):
        cases = [("one\n", "one", False, False, "modified", "format", "Final newline"),
                 ("one\r\n", "one\n", False, False, "modified", "format", "Line endings"),
                 ("one two\n", "onetwo\n", True, False, "ignored", "equivalent", "Equivalent")]
        for left, right, whitespace, endings, status, kind, explanation in cases:
            with self.subTest(kind=kind, left=left):
                paths = self.load(left, right)
                self.options(whitespace, endings)
                self.assertEqual(self.result()["status"], status)
                self.assertEqual(self.result()["kind"], kind)
                self.assertEqual(self.window._hunks, [])
                self.assertIn(explanation, self.window.text_comparison_notice.text())
                self.assertEqual(_files_match_cancellable(*paths, whitespace, endings, lambda: False), status)
                command = [sys.executable, str(Path(__file__).resolve().parents[1] / "D1ffler_cli.py"),
                           "compare", *map(str, paths)]
                if whitespace:
                    command.append("--ignore-whitespace")
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(completed.returncode, int(status == "modified"), completed.stderr)
                report = json.loads(completed.stdout)
                self.assertEqual(report["status"], status)
                self.assertEqual(report["text_comparison"]["kind"], kind)
                snapshot = self.window._comparison_export_snapshot(False, False, False)
                html = "".join(chunk for chunk, _ in text_html_chunks(snapshot["sources"], lambda: False,
                                   snapshot["text_rules"], snapshot["ignore_whitespace"], snapshot["ignore_line_endings"]))
                self.assertIn(explanation, html)
                self.assertNotIn(str(self.root), html)

    def test_line_ending_rules_keep_final_newline_significant(self):
        for a, b in (("one\r", "one\n"), ("one\r\n", "one\n"),
                     ("a\r\nb\nc\r", "a\nb\rc\r\n")):
            with self.subTest(left=a):
                paths = self.load(a, b)
                self.options(endings=True)
                self.assertEqual(self.result()["status"], "ignored")
                self.assertEqual(_files_match_cancellable(*paths, False, True, lambda: False), "ignored")
        self.load("one\n", "one")
        self.options(endings=True)
        self.assertEqual(self.result()["status"], "modified")
        self.assertIn("Final newline", self.window.text_comparison_notice.text())
        self.options(whitespace=True)
        self.assertEqual(self.result()["status"], "ignored")

    def test_whitespace_includes_paragraph_boundaries_without_false_hunks(self):
        paths = self.load("one\ntwo\n", "onetwo")
        self.options(whitespace=True)
        self.assertEqual(self.result()["status"], "ignored")
        self.assertFalse(self.window._hunks)
        self.assertEqual(_files_match_cancellable(*paths, True, False, lambda: False), "ignored")
        self.options()
        self.assertTrue(self.window._hunks)

    def test_rules_never_change_buffers_bytes_history_or_dirty_state(self):
        paths = self.load("a b\r\n", "ab\n")
        editor = self.window.panels[1]["editor"]
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(" ")
        before = [self.window._panel_text_with_line_endings(p) for p in self.window.panels]
        disk = [p.read_bytes() for p in paths]
        self.options(True, True)
        self.assertEqual(self.result()["status"], "ignored")
        self.options()
        self.assertEqual([self.window._panel_text_with_line_endings(p) for p in self.window.panels], before)
        self.assertEqual([p.read_bytes() for p in paths], disk)
        self.assertTrue(self.window.panels[1]["dirty"])
        editor.undo()
        self.assertEqual(self.window._panel_text_with_line_endings(self.window.panels[1]), "ab\n")

    def test_encodings_and_boms_remain_significant_under_every_rule(self):
        for codec, bom in (("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE),
                           ("utf-16-be", codecs.BOM_UTF16_BE), ("utf-16-le", b"")):
            with self.subTest(codec=codec, bom=bom):
                paths = self.load("one\n", "one\n", right_encoding=codec, right_bom=bom)
                for whitespace, endings in ((False, False), (True, True)):
                    self.options(whitespace, endings)
                    self.assertEqual(self.result()["status"], "modified")
                    self.assertEqual(self.result()["kind"], "format")
                    self.assertFalse(self.window._hunks)
                    self.assertEqual(_files_match_cancellable(*paths, whitespace, endings, lambda: False), "modified")
        self.load("one\n", "one\n", "utf-16-le", "utf-16-le", codecs.BOM_UTF16_LE, codecs.BOM_UTF16_LE)
        self.assertEqual(self.result()["status"], "identical")
        self.assertIn("Byte-identical", self.window.text_comparison_notice.text())

    def test_masks_and_whitespace_share_scope_and_do_not_hide_meaningful_edits(self):
        self.load("time=123 a b\r\n", "time=456 ab\n")
        self.window.workspace_text_rules = [{"name": "Time", "scope": "*.txt", "pattern": "[0-9]+",
                                              "mode": "match", "enabled": True}]
        self.options(True, True)
        self.assertEqual(self.result()["status"], "ignored")
        self.assertFalse(self.window._hunks)
        self.window.workspace_text_rules[0]["scope"] = "left.txt"
        self.window._refresh_diff()
        self.assertEqual(self.result()["status"], "modified")
        self.assertTrue(self.window._hunks)
        self.window.workspace_text_rules[0]["scope"] = "*.txt"
        self.window.panels[1]["editor"].replace_logical_text("time=456 changed\n")
        self.assertEqual(self.result()["kind"], "content")
        self.assertTrue(self.window._hunks)

    def test_multi_panel_ignored_pair_does_not_mask_another_content_change(self):
        self.load("a b\n", "ab\n")
        self.window._add_panel()
        self.window.panels[2]["editor"].setPlainText("changed\n")
        self.options(whitespace=True)
        self.assertEqual([result["status"] for result in self.window._text_comparisons], ["ignored", "modified"])
        self.assertTrue(self.window._hunks)

    def test_folder_results_and_edited_preview_are_explicit_about_disk_vs_buffer(self):
        folders = [self.root / "left", self.root / "right"]
        for folder in folders:
            folder.mkdir()
        for name, a, b in (("final.txt", "one\n", "one"), ("ending.txt", "one\r\n", "one\n"),
                           ("space.txt", "one two\n", "onetwo\n")):
            for folder, text in zip(folders, (a, b)):
                (folder / name).write_bytes(text.encode())
        self.window.settings["ignore_whitespace"] = True
        # Whitespace ignores all three text distinctions; encoding remains compared.
        (folders[0] / "encoding.txt").write_bytes(b"one\n")
        (folders[1] / "encoding.txt").write_bytes(codecs.BOM_UTF16_LE + "one\n".encode("utf-16-le"))
        for index, folder in enumerate(folders):
            self.window._load_panel(index, "folder", folder)
        self.wait(lambda: not self.window._folder_jobs)
        self.assertEqual({name: status[1] for name, status in self.window._folder_rows.items()},
                         {"final.txt": "ignored", "ending.txt": "ignored", "space.txt": "ignored", "encoding.txt": "modified"})
        self.window._open_folder_item(self.window.panels[0]["widget"], self.window._folder_tree_items[0]["ending.txt"])
        self.assertEqual(self.result()["status"], "ignored")
        self.window.panels[1]["editor"].replace_logical_text("changed\n")
        self.assertEqual(self.result()["status"], "modified")
        self.assertIn("unsaved buffers", self.window.text_comparison_notice.text())
        self.assertEqual((folders[1] / "ending.txt").read_bytes(), b"one\n")
        self.window._back_to_folders()
        self.assertTrue(self.window.text_comparison_notice.isHidden())
        self.assertEqual(self.window._text_comparisons, [])

    def test_raw_patch_includes_ignored_content_and_retains_final_newline_contract(self):
        sources = [{"name": "before.txt", "text": "one two\n"}, {"name": "after.txt", "text": "onetwo"}]
        raw = "".join(chunk for chunk, _ in text_unified_chunks(sources, lambda: False, [], True))
        self.assertIn("Equivalent under active rules", raw)
        self.assertIn("including differences ignored", raw)
        self.assertIn("\\ No newline at end of file", raw)
        parsed = parse_patch(raw)[0]
        result, outcomes = apply_hunks(sources[0]["text"], parsed, (0,))
        self.assertEqual(result, sources[1]["text"])
        self.assertEqual(outcomes[0].status, "matching")

    def test_format_only_patch_explains_why_it_has_no_operations(self):
        sources = [{"name": "a.txt", "text": "one\r\n"}, {"name": "b.txt", "text": "one\n", "bom": codecs.BOM_UTF8}]
        raw = "".join(chunk for chunk, _ in text_unified_chunks(sources, lambda: False))
        self.assertIn("Format-only differences", raw)
        self.assertIn("not patch operations", raw)
        self.assertNotIn("@@", raw)

    def test_format_only_buffer_change_is_dirty_recoverable_and_undoable(self):
        paths = self.load("one\r\n", "one\r\n")
        editor = self.window.panels[1]["editor"]
        editor.replace_logical_text("one\n")
        self.assertTrue(self.window.panels[1]["dirty"])
        snapshot = self.window._recovery_snapshot()
        self.assertEqual(snapshot["panels"][1]["text"], "one\n")
        restored = self.new_window()
        self.assertTrue(restored._restore_recovery(snapshot))
        self.assertTrue(restored.panels[1]["dirty"])
        self.assertEqual(restored._text_comparisons[0]["kind"], "format")
        editor.undo()
        self.assertFalse(self.window.panels[1]["dirty"])
        editor.redo()
        with patch("d1ffler.panel_mixin.QMessageBox.critical") as error:
            self.assertTrue(self.window._save_panel_file(1), error.call_args)
        self.assertEqual(paths[1].read_bytes(), b"one\n")

    def test_background_layout_uses_the_same_whitespace_model(self):
        a = "".join(f"{i}: one two " + "x" * 80 + "\r\n" for i in range(900))
        b = "".join(f"{i}: onetwo " + "x" * 80 + "\n" for i in range(900))
        self.window.settings.update(ignore_whitespace=True, ignore_line_endings=True)
        self.load(a, b)
        self.wait(lambda: not self.window._text_jobs)
        self.assertEqual(self.result()["status"], "ignored")
        self.assertFalse(self.window._hunks)

    def test_zip_preview_preserves_encoding_and_member_mask_scope(self):
        folder = self.root / "folder"
        folder.mkdir()
        member = "sample.log"
        archive = self.root / "source.zip"
        (folder / member).write_bytes(codecs.BOM_UTF16_LE + "time=123\r\n".encode("utf-16-le"))
        with ZipFile(archive, "w") as writer:
            writer.writestr(member, codecs.BOM_UTF16_LE + "time=456\n".encode("utf-16-le"))
        self.window.workspace_text_rules = [{"name": "Time", "scope": "*.log", "pattern": "[0-9]+",
                                            "mode": "match", "enabled": True}]
        self.window.settings["ignore_line_endings"] = True
        self.window._load_panel(0, "folder", archive)
        self.window._load_panel(1, "folder", folder)
        self.wait(lambda: not self.window._folder_jobs)
        self.assertEqual(self.window._folder_rows[member][1], "ignored")
        self.window._open_folder_item(self.window.panels[0]["widget"], self.window._folder_tree_items[0][member])
        self.assertEqual(self.result()["status"], "ignored")
        self.assertEqual(self.window.panels[0]["encoding"], "utf-16-le")
        self.assertEqual(self.window.panels[0]["bom"], codecs.BOM_UTF16_LE)
        self.assertTrue(self.window.panels[0]["editor"].isReadOnly())
        snapshot = self.window._comparison_export_snapshot(False, False, False)
        self.assertEqual(snapshot["sources"][0]["scope"], member)

    def test_replacing_fourth_panel_for_merge_uses_a_new_format_baseline(self):
        self.load("one\n", "one\n")
        self.window._add_panel()
        self.window._add_panel()
        self.window.panels[2]["editor"].setPlainText("one\n")
        path = self.root / "result.txt"
        path.write_bytes(b"one\n")
        self.window._load_panel(3, "file", path)
        with patch("d1ffler.diff_tool.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window._toggle_merge_mode(True)
        result = self.window.panels[3]
        self.assertEqual(result["saved_text_with_endings"], "")
        self.assertEqual(self.window._panel_text(result), "one\n")
        self.assertTrue(result["dirty"])

    def test_nonbreaking_spaces_are_preserved_and_compared_without_normalizing_source(self):
        paths = self.load("one\u00a0two\n", "one two\n")
        self.assertEqual(self.result()["status"], "modified")
        self.assertTrue(self.window._hunks)
        self.assertEqual(self.window._panel_text_with_line_endings(self.window.panels[0]), "one\u00a0two\n")
        self.options(whitespace=True)
        self.assertEqual(self.result()["status"], "ignored")
        self.assertFalse(self.window._hunks)
        editor = self.window.panels[0]["editor"]
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("extra")
        editor.undo()
        self.assertEqual(self.window._panel_text_with_line_endings(self.window.panels[0]), "one\u00a0two\n")
        with patch("d1ffler.panel_mixin.QMessageBox.critical") as error:
            self.assertTrue(self.window._save_panel_file(0), error.call_args)
        self.assertEqual(paths[0].read_bytes(), "one\u00a0two\n".encode())

    def test_recovered_hunk_hidden_by_whitespace_rule_is_stale(self):
        from d1ffler.review_notes import content_fingerprint, hunk_anchor, source_key
        self.load("one two\n", "onetwo\n")
        self.window._navigate_change(1)
        sources = self.window._review_sources()
        self.window.review_notes.append({"id": "review", "kind": "hunk", "path": None, "source_key": source_key(sources),
            "fingerprint": content_fingerprint(sources), "text": "Review", "state": "Accepted",
            "anchor": hunk_anchor(self.window._aligned_rows,
                                  [self.window._panel_text(p).splitlines() for p in self.window.panels], self.window._hunks[0])})
        snapshot = self.window._workspace_snapshot("Old rules")
        snapshot["comparison_options"]["ignore_whitespace"] = True
        restored = self.new_window()
        restored._load_workspace(snapshot)
        self.wait(lambda: not restored._context_jobs)
        self.assertFalse(restored._hunks)
        self.assertEqual(restored._change_index, -1)
        self.assertIn("stale", restored.context_notice.text())
        self.assertTrue(restored.review_notes[0]["stale"])
        self.assertTrue(restored.review_notes[0]["hunk_stale"])


if __name__ == "__main__":
    unittest.main()
