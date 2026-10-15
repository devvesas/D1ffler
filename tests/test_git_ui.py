"""A focused GUI check for revision panels and workspace restoration."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QTreeWidgetItem

from d1ffler.diff_tool import CommitDialog, DEFAULT_SETTINGS, DiffApp, ReviewNotesDialog, SettingsDialog, load_app_settings, save_app_settings
from d1ffler.git_worktrees import changed_paths, create_worktree, revisions
from d1ffler.review_notes import content_fingerprint, source_key


class GitPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "D1ffler Test"),
                     ("config", "user.email", "D1ffler@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        (self.repo / "sample.txt").write_text("base\n", encoding="utf-8")
        for args in (("add", "sample.txt"), ("commit", "-qm", "base"), ("tag", "v1")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.tag_path = self.root / "tag"
        tag = next(item for item in revisions(self.repo) if item.kind == "tag")
        create_worktree(self.repo, self.tag_path, tag)
        self.settings = DEFAULT_SETTINGS.copy()
        self.window = DiffApp(self.settings, self.root / "settings.json")
        self.addCleanup(self.cleanup_window)
        self.window._load_panel(0, "folder", self.repo)
        self.window._load_panel(1, "folder", self.tag_path)
        self.window._set_git_source(self.window.panels[1], {
            "repo": str(self.repo), "kind": "tag", "name": "v1", "commit": tag.commit,
            "readonly": True, "managed": True,
        })

    def cleanup_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_review_note_survives_workspace_reopen_and_becomes_stale(self):
        sources = self.window._review_sources()
        key = source_key(sources)
        fingerprint = content_fingerprint(sources, "sample.txt")
        self.window.review_notes = [{"id": "note-1", "kind": "path", "path": "sample.txt",
                                     "state": "Needs follow-up", "text": "Check this change",
                                     "source_key": key, "fingerprint": fingerprint}]
        snapshot = self.window._workspace_snapshot("Review")
        self.window._load_workspace(snapshot)
        self.assertEqual(self.window.review_notes[0]["text"], "Check this change")
        self.assertEqual(self.window.review_notes[0]["state"], "Needs follow-up")
        (self.repo / "sample.txt").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual(content_fingerprint(self.window._review_sources(), "sample.txt"), fingerprint)

    def test_stale_note_requires_explicit_reattachment(self):
        sources = self.window._review_sources()
        current = content_fingerprint(sources, "sample.txt")
        note = {"id": "note-1", "kind": "path", "path": "sample.txt", "state": "Open",
                "text": "Review", "source_key": source_key(sources), "fingerprint": "old"}
        manager = ReviewNotesDialog([note], source_key(sources), {"sample.txt": current}, self.window)
        manager.tree.topLevelItem(0).setSelected(True)

        def save_without_reattach(editor):
            self.assertFalse(editor.reattach.isHidden())
            return QDialog.DialogCode.Accepted

        with patch("d1ffler.diff_tool.ReviewNoteDialog.exec", save_without_reattach):
            manager._edit()
        self.assertTrue(manager._stale(manager.notes[0]))

        def save_with_reattach(editor):
            editor.reattach.setChecked(True)
            return QDialog.DialogCode.Accepted

        manager.tree.topLevelItem(0).setSelected(True)
        with patch("d1ffler.diff_tool.ReviewNoteDialog.exec", save_with_reattach):
            manager._edit()
        self.assertFalse(manager._stale(manager.notes[0]))
        manager.close()
        manager.deleteLater()

    def test_tag_file_is_read_only_and_restores_as_read_only(self):
        item = QTreeWidgetItem(["sample.txt"])
        item.setData(0, Qt.ItemDataRole.UserRole, "sample.txt")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        self.window._open_folder_item(self.window.panels[0]["widget"], item)
        self.assertTrue(self.window.panels[1]["editor"].isReadOnly())
        snapshot = self.window._workspace_snapshot("Test")
        self.assertEqual(snapshot["panels"][1]["git"]["kind"], "tag")
        self.window._load_workspace(snapshot)
        self.assertTrue(self.window.panels[1]["git"]["readonly"])
        self.window._open_folder_item(self.window.panels[0]["widget"], item)
        self.assertTrue(self.window.panels[1]["editor"].isReadOnly())

    def test_create_branch_makes_open_file_editable_and_savable(self):
        item = QTreeWidgetItem(["sample.txt"])
        item.setData(0, Qt.ItemDataRole.UserRole, "sample.txt")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        self.window._open_folder_item(self.window.panels[0]["widget"], item)
        with patch("d1ffler.diff_tool.QInputDialog.getText", return_value=("from-v1", True)):
            self.window._create_branch_from_panel(1)
        self.assertFalse(self.window.panels[1]["editor"].isReadOnly())
        self.window.panels[1]["editor"].setPlainText("edited\n")
        self.assertEqual(self.window.panels[1]["view_stack"].currentIndex(), 0)
        self.assertTrue(self.window._save_panel_file(1))
        self.assertEqual(self.window.panels[1]["view_stack"].currentIndex(), 0)
        self.assertEqual((self.tag_path / "sample.txt").read_text(encoding="utf-8"), "edited\n")
        self.window._apply_hunk_to_panel(0, 1, self.window._hunks[0])
        self.assertEqual(self.window._panel_text(self.window.panels[1]), "base\n")
        self.assertTrue(self.window._save_panel_file(1))

    def test_commit_is_listed_and_selected_without_entering_hash(self):
        selected_path = self.root / "selected-commit"
        def select_commit(_parent, _title, _label, entries, _current, _editable):
            listed = next(entry for entry in entries if entry.startswith("Commit: "))
            self.assertIn("base", listed)
            self.assertEqual(entries[-1], "Enter commit hash…")
            return listed, True

        with patch("d1ffler.diff_tool.QInputDialog.getItem", side_effect=select_commit), patch(
            "d1ffler.diff_tool.QInputDialog.getText", return_value=(str(selected_path), True)
        ) as get_text:
            self.window._choose_revision(1)
        self.assertEqual(get_text.call_count, 1)  # worktree path only
        self.assertTrue((selected_path / "sample.txt").exists())
        self.assertEqual(self.window.panels[1]["git"]["kind"], "commit")
        self.assertTrue(self.window.panels[1]["git"]["readonly"])

    def test_gitignore_setting_is_saved_and_restored(self):
        dialog = SettingsDialog(self.settings)
        self.assertFalse(dialog.ignore_gitignore_check.isChecked())
        dialog.ignore_gitignore_check.setChecked(True)
        path = self.root / "settings.json"
        save_app_settings(dialog.values(), path)
        with patch("d1ffler.app_settings.settings_candidates", return_value=[path]):
            loaded, _ = load_app_settings()
        self.assertTrue(loaded["ignore_gitignore"])

    def test_commit_dialog_remains_responsive_during_status_and_previews_patch(self):
        (self.repo / "sample.txt").write_text("saved contents\n", encoding="utf-8")
        release = threading.Event()

        def delayed_status(repo):
            release.wait(3)
            return changed_paths(repo)

        with patch("d1ffler.git_dialog.changed_paths", side_effect=delayed_status):
            dialog = CommitDialog(self.repo, self.window)
            self.assertTrue(dialog._busy)
            self.assertEqual(dialog.tree.topLevelItemCount(), 0)
            self.app.processEvents()
            release.set()
            deadline = time.monotonic() + 5
            while dialog._busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
        self.assertFalse(dialog._busy)
        dialog.rows["sample.txt"].setCheckState(0, Qt.CheckState.Checked)
        dialog._review()
        deadline = time.monotonic() + 5
        while dialog._busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertIn("+saved contents", dialog.patch.toPlainText())
        self.assertTrue(dialog.create.isEnabled())
        dialog.close()
        dialog.deleteLater()

    def test_enabling_gitignore_closes_loaded_folder_file(self):
        (self.repo / ".gitignore").write_text("sample.txt\n", encoding="utf-8")
        self.window._clear_panel(1)
        item = QTreeWidgetItem(["sample.txt"])
        item.setData(0, Qt.ItemDataRole.UserRole, "sample.txt")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        self.window._open_folder_item(self.window.panels[0]["widget"], item)
        self.assertIn("base", self.window.panels[0]["editor"].toPlainText())
        changed = self.settings | {"ignore_gitignore": True}
        with patch.object(SettingsDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch.object(
            SettingsDialog, "values", return_value=changed
        ):
            self.window._open_settings()
        self.assertEqual(self.window.panels[0]["editor"].toPlainText(), "")
        self.assertTrue(self.window.folder_back_button.isHidden())
        deadline = time.monotonic() + 3
        while self.window.panels[0]["tree"].topLevelItemCount() != 1 and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertEqual(self.window.panels[0]["tree"].topLevelItemCount(), 1)  # .gitignore only


if __name__ == "__main__":
    unittest.main()
