"""Saved workspaces can contain stale or hand-edited settings data."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp


class WorkspaceValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.left, self.right = self.root / "left.txt", self.root / "right.txt"
        self.left.write_text("left", encoding="utf-8")
        self.right.write_text("right", encoding="utf-8")
        self.window = DiffApp(DEFAULT_SETTINGS.copy(), self.root / "settings.json")
        self.addCleanup(self.cleanup_window)
        self.window._load_panel(0, "file", self.left)

    def cleanup_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_invalid_panel_record_keeps_current_workspace(self):
        with patch("d1ffler.workspace_mixin.QMessageBox.warning") as warning:
            self.window._load_workspace({"panels": [{"path": str(self.right)}, None]})
        warning.assert_called_once()
        self.assertEqual(self.window.panels[0]["path"], self.left)
        self.assertFalse(self.window._loading_workspace)

    def test_invalid_optional_lists_do_not_interrupt_loading(self):
        self.window.settings["pinned_workspaces"] = [{"name": "Saved", "panels": [None]}]
        self.window._load_workspace({
            "name": "Saved",
            "panels": [{"path": str(self.left)}, {"path": str(self.right)}],
            "sync_reports": None, "restore_reports": "bad", "review_notes": 1,
            "text_rules": {"bad": "shape"},
        })
        self.assertEqual([panel["path"] for panel in self.window.panels], [self.left, self.right])
        self.assertEqual(self.window.sync_reports, [])
        self.assertEqual(self.window.restore_reports, [])
        self.assertEqual(self.window.review_notes, [])
        self.assertEqual(self.window.workspace_text_rules, [])
        self.assertFalse(self.window._loading_workspace)


if __name__ == "__main__":
    unittest.main()
