"""Encoding and BOM behavior for editor saves."""

import codecs
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox, QTreeWidgetItem

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp


class TextEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_no_op_and_edit_preserve_supported_encodings_and_line_endings(self):
        cases = [
            ("utf8-bom", "utf-8", codecs.BOM_UTF8, "café\r\nlast"),
            ("utf16-le", "utf-16-le", codecs.BOM_UTF16_LE, "café\r\nlast"),
            ("utf16-be", "utf-16-be", codecs.BOM_UTF16_BE, "café\r\nlast"),
            ("utf16-no-bom", "utf-16-le", b"", "café\r\nlast"),
            ("utf32-le", "utf-32-le", codecs.BOM_UTF32_LE, "café\r\nlast"),
            ("utf32-be", "utf-32-be", codecs.BOM_UTF32_BE, "café\r\nlast"),
            ("utf32-no-bom", "utf-32-be", b"", "café\r\nlast"),
            ("cp1252", "cp1252", b"", "café\r\nlast"),
        ]
        for name, encoding, bom, content in cases:
            with self.subTest(name=name):
                path = self.root / f"{name}.txt"
                original = bom + content.encode(encoding)
                path.write_bytes(original)
                self.window._load_panel(0, "file", path)
                panel = self.window.panels[0]
                self.assertEqual((panel["encoding"], panel["bom"]), (encoding, bom))
                self.assertFalse(panel["binary"])
                self.assertTrue(self.window._save_panel_file(0))
                self.assertEqual(path.read_bytes(), original)
                panel["editor"].setPlainText("changed\r\nlast")
                self.assertTrue(self.window._save_panel_file(0))
                self.assertEqual(path.read_bytes(), bom + "changed\r\nlast".encode(encoding))

    def test_unrepresentable_text_keeps_file_until_conversion_confirmed(self):
        path = self.root / "legacy.txt"
        path.write_bytes("café".encode("cp1252"))
        self.window._load_panel(0, "file", path)
        panel = self.window.panels[0]
        panel["editor"].setPlainText("café 🚀")
        with patch.object(self.window, "_encoding_error_choice", return_value="cancel"):
            self.assertFalse(self.window._save_panel_file(0))
        self.assertEqual(path.read_bytes(), "café".encode("cp1252"))
        self.assertTrue(panel["dirty"])
        with patch.object(self.window, "_encoding_error_choice", return_value="convert"), patch(
            "d1ffler.diff_tool.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes
        ) as confirm:
            self.assertTrue(self.window._save_panel_file(0))
        self.assertIn("UTF-8", confirm.call_args.args[2])
        self.assertEqual(path.read_bytes(), "café 🚀".encode("utf-8"))
        self.assertEqual(panel["encoding"], "utf-8")

    def test_explicit_conversion_and_reopen_without_losing_dirty_edits(self):
        path = self.root / "text.txt"
        path.write_bytes(codecs.BOM_UTF8 + b"original")
        self.window._load_panel(0, "file", path)
        panel = self.window.panels[0]
        with patch("d1ffler.diff_tool.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(self.window._save_panel_file(0, encoding_override=("utf-16-le", codecs.BOM_UTF16_LE)))
        self.assertEqual(path.read_bytes(), codecs.BOM_UTF16_LE + "original".encode("utf-16-le"))
        self.assertEqual(panel["encoding_display"].text(), "UTF-16 LE with BOM")
        panel["editor"].setPlainText("local edits")
        before = path.read_bytes()
        with patch.object(self.window, "_choose_encoding", return_value=("utf-16-le", codecs.BOM_UTF16_LE)), patch("d1ffler.diff_tool.QDialog.exec"):
            self.window._reopen_with_encoding(0)
        self.assertEqual(self.window._panel_text(panel), "local edits")
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(panel["dirty"])

    def test_reopen_clean_file_with_selected_legacy_encoding(self):
        path = self.root / "ambiguous.txt"
        original = "café".encode("utf-8")
        path.write_bytes(original)
        self.window._load_panel(0, "file", path)
        panel = self.window.panels[0]
        with patch.object(self.window, "_choose_encoding", return_value=("cp1252", b"")):
            self.window._reopen_with_encoding(0)
        self.assertEqual(self.window._panel_text(panel), "cafÃ©")
        self.assertEqual(panel["encoding_display"].text(), "Windows-1252")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(panel["dirty"])

    def test_folder_file_and_merge_result_use_detected_encoding(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        name = "sample.txt"
        (left / name).write_bytes(codecs.BOM_UTF16_LE + "old\n".encode("utf-16-le"))
        (right / name).write_bytes(codecs.BOM_UTF16_LE + "new\n".encode("utf-16-le"))
        self.window._load_panel(0, "folder", left)
        self.window._load_panel(1, "folder", right)
        item = QTreeWidgetItem([name])
        item.setData(0, Qt.ItemDataRole.UserRole, name)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        self.window._open_folder_item(self.window.panels[0]["widget"], item)
        panel = self.window.panels[1]
        self.assertEqual(panel["encoding"], "utf-16-le")
        panel["editor"].setPlainText("edited\n")
        self.assertTrue(self.window._save_panel_file(1))
        self.assertEqual((right / name).read_bytes(), codecs.BOM_UTF16_LE + "edited\n".encode("utf-16-le"))
        self.window._clear_all()
        self.window._add_panel()
        for index, value in enumerate(("base\n", "local\n", "base\n")):
            path = self.root / f"merge-{index}.txt"
            path.write_bytes(codecs.BOM_UTF16_LE + value.encode("utf-16-le"))
            self.window._load_panel(index, "file", path)
        self.window.merge_mode_button.setChecked(True)
        result = self.window.panels[3]
        self.assertEqual((result["encoding"], result["bom"]), ("utf-16-le", codecs.BOM_UTF16_LE))
        output = self.root / "merged.txt"
        with patch("d1ffler.diff_tool.QFileDialog.getSaveFileName", return_value=(str(output), "")):
            self.assertTrue(self.window._save_panel_file(3))
        self.assertEqual(output.read_bytes(), codecs.BOM_UTF16_LE + "local\n".encode("utf-16-le"))


if __name__ == "__main__":
    unittest.main()
