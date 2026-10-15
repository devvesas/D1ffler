"""Cleanup must preserve references, live edits and all non-owned disk content."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from d1ffler.app_settings import read_app_settings, save_app_settings
from d1ffler.comparison_context import RetainedReports
from d1ffler.comparison_tabs import ComparisonWindow
from d1ffler.diagnostics import DiagnosticStore
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.local_data import LocalDataStore, protect_local_sources, release_report_pins
from d1ffler.local_data_dialog import LocalDataDialog
from d1ffler.recovery import RecoveryError, RecoveryStore
from d1ffler.user_state import state_lock


class LocalDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings_path = self.root / "settings.json"
        self.store = LocalDataStore(self.settings_path)
        self.windows = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for window in self.windows:
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        release_report_pins(self.root)

    def report(self, name, *, days=40):
        payload = (json.dumps({"started_at": name, "outcomes": [{"path": "private/source.txt"}]}) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        self.store.reports.mkdir(exist_ok=True)
        path = self.store.reports / f"{digest}.json"
        path.write_bytes(payload)
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))
        return path, {"sha256": digest}

    def write_settings(self, **values):
        self.settings_path.write_text(json.dumps({"schema_version": 1, **values}), encoding="utf-8")

    def workspace(self, reference, name="Review"):
        return {"name": name, "panels": [{"path": str(self.root / "left")}, {"path": str(self.root / "right")}],
                "report_refs": {"sync": [reference], "restore": []}}

    def snapshot(self, reference=None):
        data = {"version": 2, "panels": [{"kind": "text", "recover": True, "text": "unfinished"}]}
        if reference:
            data["context"] = {"context_version": 1, "report_refs": {"sync": [reference]}}
        return data

    def window(self):
        window = ComparisonWindow(read_app_settings(self.settings_path), self.settings_path, DiffApp)
        self.windows.append(window)
        return window

    def select(self, dialog, kind):
        for index in range(dialog.tree.topLevelItemCount()):
            item = dialog.tree.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole).kind == kind:
                item.setSelected(True)
                return item
        self.fail(f"No {kind} entry")

    def test_saved_recent_and_recovery_references_survive(self):
        saved, saved_ref = self.report("saved")
        recent, recent_ref = self.report("recent")
        recover, recover_ref = self.report("recover")
        orphan, _ = self.report("orphan")
        self.write_settings(pinned_workspaces=[self.workspace(saved_ref)], recent_workspaces=[self.workspace(recent_ref)])
        recovery = RecoveryStore(self.settings_path)
        recovery.write(self.snapshot(recover_ref))
        self.addCleanup(recovery.release)
        preview = self.store.preview_reports(30)
        self.assertEqual([entry.path for entry in preview], [orphan])
        self.assertEqual(self.store.cleanup_reports(preview), [orphan])
        self.assertTrue(all(path.exists() for path in (saved, recent, recover)))

    def test_reference_added_after_preview_is_rechecked(self):
        path, reference = self.report("later referenced")
        preview = self.store.preview_reports()
        settings = read_app_settings(self.settings_path)
        settings["pinned_workspaces"] = [self.workspace(reference)]
        save_app_settings(settings, self.settings_path)
        self.assertEqual(self.store.cleanup_reports(preview), [])
        self.assertTrue(path.exists())

    def test_corrupt_or_unknown_state_blocks_all_cleanup(self):
        path, _ = self.report("uncertain")
        preview = self.store.preview_reports()
        for content in ("broken JSON", '{"schema_version": 99}', '{"recent_workspaces": [null]}',
                        '{"pinned_workspaces": [{"report_refs": {"sync": ["bad"]}}]}',
                        '{"pinned_workspaces": [{"context_version": 99}]}'):
            with self.subTest(content=content):
                self.settings_path.write_text(content)
                with self.assertRaises(OSError):
                    self.store.cleanup_reports(preview)
                self.assertTrue(path.exists())
        self.settings_path.unlink()
        self.store.recovery.mkdir()
        (self.store.recovery / "unknown.json").write_text("{}")
        with self.assertRaises(OSError):
            self.store.cleanup_reports(preview)
        self.assertTrue(path.exists())

    def test_changed_report_stops_cleanup(self):
        path, _ = self.report("modified since review")
        preview = self.store.preview_reports()
        path.write_text("changed")
        with self.assertRaisesRegex(OSError, "changed since preview"):
            self.store.cleanup_reports(preview)
        self.assertEqual(path.read_text(), "changed")

    def test_failed_removal_retains_remaining_files(self):
        for index in range(3):
            self.report(str(index))
        preview = self.store.preview_reports()
        original = Path.unlink
        def fail_second(path, *args, **kwargs):
            if path == preview[1].path:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_second), self.assertRaisesRegex(OSError, "Removed 1 report"):
            self.store.cleanup_reports(preview)
        self.assertFalse(preview[0].path.exists())
        self.assertTrue(preview[1].path.exists())
        self.assertTrue(preview[2].path.exists())

    def test_cleanup_never_visits_sources_worktrees_backups_or_exports(self):
        for name in ("source.txt", "worktree/.git/config", "backup/original.txt", "export.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep me")
        orphan, _ = self.report("owned report")
        self.write_settings(pinned_workspaces=[{"name": "Old", "panels": [{"path": str(self.root / "source.txt")}]}],
                            managed_worktrees=[{"path": str(self.root / "worktree"), "repo": "repo"}])
        record = json.loads(self.settings_path.read_text())["pinned_workspaces"][0]
        self.store.remove_history([("pinned_workspaces", record)])
        self.store.cleanup_reports(self.store.preview_reports())
        self.assertFalse(orphan.exists())
        for name in ("source.txt", "worktree/.git/config", "backup/original.txt", "export.json"):
            self.assertEqual((self.root / name).read_text(), "keep me")
        self.assertEqual(len(read_app_settings(self.settings_path)["managed_worktrees"]), 1)

    def test_report_used_as_saved_comparison_source_survives(self):
        path, _ = self.report("comparison source")
        self.write_settings(pinned_workspaces=[{"name": "Report review", "panels": [{"path": str(path)}]}])
        self.assertEqual(self.store.preview_reports(), [])
        self.assertTrue(path.exists())
        self.write_settings(pinned_workspaces=[{"name": "Folder review", "panels": [{"path": str(self.root)}]}])
        self.assertEqual(self.store.preview_reports(), [])

    def test_report_opened_as_live_source_after_preview_survives(self):
        path, _ = self.report("live comparison source")
        preview = self.store.preview_reports()
        window = self.window()
        window.active_comparison._load_panel(0, "file", path)
        self.assertEqual(self.store.cleanup_reports(preview), [])
        self.assertTrue(path.exists())

    def source_alias(self, path):
        nested = path.parent / "nested"
        nested.mkdir(exist_ok=True)
        return nested / ".." / path.name

    def test_live_dotdot_source_survives_stale_preview_and_automatic_retention(self):
        path, _ = self.report("aliased live source")
        orphan, _ = self.report("unreferenced")
        preview = self.store.preview_reports()
        alias = self.source_alias(path)
        original = path.read_bytes()
        window = self.window()
        view = window.active_comparison
        view._load_panel(0, "file", alias)
        self.assertEqual(view.panels[0]["path"], alias)
        self.assertEqual(view._panel_text(view.panels[0]), original.decode())
        self.assertEqual(self.store.cleanup_reports(preview), [orphan])
        another, _ = self.report("automatic cleanup orphan")
        self.store.save_policy(True, 30)
        self.assertEqual(self.store.apply_retention(), [another])
        self.assertEqual(path.read_bytes(), original)

    def test_saved_and_recent_sources_protect_dotdot_relative_and_case_aliases(self):
        path, _ = self.report("saved source aliases")
        orphan, _ = self.report("unreferenced")
        alias = self.source_alias(path)
        spellings = [str(alias), os.path.relpath(alias, Path.cwd())]
        if sys.platform == "win32":
            spellings.append(str(alias).upper())
        for key in ("pinned_workspaces", "recent_workspaces"):
            for spelling in spellings:
                with self.subTest(key=key, spelling=spelling):
                    self.write_settings(**{key: [{"name": "Alias", "panels": [{"path": spelling}]}]})
                    if Path(spelling).is_absolute():
                        self.assertEqual([entry.path for entry in self.store.preview_reports()], [orphan])
                    else:
                        with self.assertRaisesRegex(OSError, "unresolved relative sources"):
                            self.store.preview_reports()
        self.assertTrue(path.exists())

    def test_abandoned_recovery_alias_added_after_preview_is_rechecked(self):
        path, _ = self.report("recovery source alias")
        orphan, _ = self.report("unreferenced")
        preview = self.store.preview_reports()
        alias = self.source_alias(path)
        snapshot = self.snapshot()
        snapshot["panels"][0]["editing_path"] = os.path.relpath(alias, Path.cwd())
        recovery = RecoveryStore(self.settings_path)
        recovery.write(snapshot)
        recovery.release()
        with self.assertRaisesRegex(OSError, "unresolved relative sources"):
            self.store.cleanup_reports(preview)
        self.assertTrue(orphan.exists())
        snapshot["panels"][0]["editing_path"] = str(alias)
        recovery.write(snapshot)
        recovery.release()
        self.assertEqual(self.store.cleanup_reports(preview), [orphan])
        self.assertTrue(path.exists())
        self.assertTrue(recovery.session_path.exists())

    def test_live_report_directory_and_ancestor_alias_protect_all_reports(self):
        paths = [self.report("first")[0], self.report("second")[0]]
        for directory in (self.store.reports, self.root):
            nested = directory / "nested"
            nested.mkdir(exist_ok=True)
            alias = nested / ".."
            with self.subTest(directory=directory):
                preview = self.store.preview_reports()
                self.assertEqual(len(preview), 2)
                window = self.window()
                window.active_comparison._load_panel(0, "folder", alias)
                self.assertEqual(window.active_comparison.panels[0]["path"], alias)
                self.assertEqual(self.store.cleanup_reports(preview), [])
                self.store.save_policy(True, 0)
                self.assertEqual(self.store.apply_retention(), [])
                self.assertTrue(all(path.exists() for path in paths))
                window.close()

    def directory_alias(self, link, target):
        if sys.platform == "win32":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(link.rmdir)  # Exact junction only; never traverse its target.
        else:
            link.symlink_to(target, target_is_directory=True)
            self.addCleanup(link.unlink)
        return link

    def test_directory_junction_or_symlink_source_protects_files_and_ancestors(self):
        path, _ = self.report("directory alias source")
        orphan, _ = self.report("unreferenced")
        report_alias = self.directory_alias(self.root / "report-alias", self.store.reports)
        state_alias = self.directory_alias(self.root / "state-alias", self.root)
        for spelling, expected in ((report_alias / path.name, [orphan]),
                                   (report_alias, []), (state_alias, [])):
            with self.subTest(spelling=spelling):
                self.write_settings(pinned_workspaces=[{"name": "Alias", "panels": [{"path": str(spelling)}]}])
                self.assertEqual([entry.path for entry in self.store.preview_reports()], expected)
        self.write_settings()
        preview = self.store.preview_reports()
        window = self.window()
        window.active_comparison._load_panel(0, "file", report_alias / path.name)
        self.assertEqual(self.store.cleanup_reports(preview), [orphan])
        self.assertTrue(path.exists())

    def test_directory_file_identity_protects_an_alias_with_a_distinct_canonical_spelling(self):
        path, _ = self.report("directory identity alias")
        alias = self.directory_alias(self.root / "report-alias", self.store.reports)
        self.write_settings(pinned_workspaces=[{"name": "Alias", "panels": [{"path": str(alias)}]}])
        resolve = Path.resolve

        def distinct_spelling(candidate, *args, **kwargs):
            # Model a filesystem directory alias (such as a mount) that resolves
            # to a distinct spelling but has the target's real stat identity.
            return alias if candidate == alias else resolve(candidate, *args, **kwargs)

        with patch.object(Path, "resolve", distinct_spelling):
            self.assertEqual(self.store.preview_reports(), [])
        self.assertTrue(path.exists())

    def test_hard_link_with_unrelated_name_protects_live_saved_and_recovery_sources(self):
        path, _ = self.report("hard linked source")
        orphan, _ = self.report("unreferenced")
        alias = self.root / "ordinary-source.txt"
        os.link(path, alias)
        self.write_settings(pinned_workspaces=[{"name": "Alias", "panels": [{"path": str(alias)}]}])
        self.assertEqual([entry.path for entry in self.store.preview_reports()], [orphan])
        self.write_settings()
        recovery = RecoveryStore(self.settings_path)
        snapshot = self.snapshot()
        snapshot["panels"][0]["editing_path"] = str(alias)
        recovery.write(snapshot)
        recovery.release()
        self.assertEqual([entry.path for entry in self.store.preview_reports()], [orphan])
        recovery.discard(recovery.session_path)
        preview = self.store.preview_reports()
        window = self.window()
        window.active_comparison._load_panel(0, "file", alias)
        self.assertEqual(self.store.cleanup_reports(preview), [orphan])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), alias.read_bytes())

    def test_source_alias_in_other_process_protects_a_stale_cleanup_preview(self):
        path, _ = self.report("other process source alias")
        alias = self.source_alias(path)
        preview = self.store.preview_reports()
        script = ("import sys; from pathlib import Path; "
                  "from d1ffler.local_data import protect_local_sources, release_report_pins; "
                  "root=Path(sys.argv[1]); protect_local_sources(root,[Path(sys.argv[2])]); "
                  "print('ready',flush=True); sys.stdin.readline(); release_report_pins(root)")
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root), str(alias)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertEqual(self.store.cleanup_reports(preview), [])
            self.assertTrue(path.exists())
            _out, errors = process.communicate("release\n", timeout=15)
            self.assertEqual(process.returncode, 0, errors)
            self.assertEqual([entry.path for entry in self.store.preview_reports()], [path])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_uncertain_saved_source_resolution_blocks_all_cleanup(self):
        path, _ = self.report("uncertain source")
        orphan, _ = self.report("unreferenced")
        preview = self.store.preview_reports()
        alias = self.source_alias(path)
        self.write_settings(pinned_workspaces=[{"name": "Alias", "panels": [{"path": str(alias)}]}])
        self.store.save_policy(True, 0)
        resolve = Path.resolve
        for error in (PermissionError("denied"), RuntimeError("alias loop"), ValueError("unsupported path")):
            def fail_source(candidate, *args, **kwargs):
                if candidate == alias:
                    raise error
                return resolve(candidate, *args, **kwargs)
            with self.subTest(error=error), patch.object(Path, "resolve", fail_source):
                with self.assertRaisesRegex(OSError, "source identity"):
                    self.store.cleanup_reports(preview)
                with self.assertRaisesRegex(OSError, "source identity"):
                    self.store.apply_retention()
            self.assertTrue(path.exists())
            self.assertTrue(orphan.exists())

    def test_uncertain_live_source_publishes_conservative_cross_process_pin(self):
        path, _ = self.report("uncertain live source")
        preview = self.store.preview_reports()
        alias = self.source_alias(path)
        resolve = Path.resolve

        def fail_source(candidate, *args, **kwargs):
            if candidate == alias:
                raise PermissionError("denied")
            return resolve(candidate, *args, **kwargs)

        with patch.object(Path, "resolve", fail_source), self.assertRaises(OSError):
            protect_local_sources(self.root, [alias])
        self.assertEqual(self.store.cleanup_reports(preview), [])
        markers = list((self.root / "D1ffler-report-users").glob("*.json"))
        self.assertEqual(json.loads(markers[0].read_text())["reports"], ["*"])
        self.assertTrue(path.exists())

    def test_unreadable_or_unsupported_file_identities_prevent_speculative_deletion(self):
        path, _ = self.report("uncertain identity")
        orphan, _ = self.report("unreferenced")
        alias = self.root / "unrelated-name.txt"
        os.link(path, alias)
        preview = self.store.preview_reports()
        self.write_settings(pinned_workspaces=[{"name": "Alias", "panels": [{"path": str(alias)}]}])
        source_info = alias.stat()
        stat_path = Path.stat
        for kind in ("source access", "source identity", "report access", "unsupported reparse"):
            def uncertain_stat(candidate, *args, **kwargs):
                if kind == "report access" and candidate == path:
                    raise PermissionError("denied")
                if candidate == alias:
                    if kind == "source access":
                        raise PermissionError("denied")
                    if kind in {"source identity", "unsupported reparse"}:
                        return SimpleNamespace(st_mode=source_info.st_mode, st_dev=source_info.st_dev,
                                               st_ino=0 if kind == "source identity" else source_info.st_ino,
                                               st_file_attributes=0x400 if kind == "unsupported reparse" else 0)
                return stat_path(candidate, *args, **kwargs)

            with self.subTest(kind=kind), patch.object(Path, "stat", uncertain_stat), self.assertRaises(OSError):
                self.store.cleanup_reports(preview)
            self.assertTrue(path.exists())
            self.assertTrue(orphan.exists())

    def test_unreadable_report_directory_blocks_alias_identity_check(self):
        path, _ = self.report("hard linked source")
        alias = self.root / "ordinary-source.txt"
        os.link(path, alias)
        preview = self.store.preview_reports()
        self.write_settings(pinned_workspaces=[{"name": "Alias", "panels": [{"path": str(alias)}]}])
        iterdir = Path.iterdir

        def deny_reports(candidate):
            if candidate == self.store.reports:
                raise PermissionError("denied")
            return iterdir(candidate)

        with patch.object(Path, "iterdir", deny_reports), self.assertRaises(OSError):
            self.store.cleanup_reports(preview)
        self.assertTrue(path.exists())

    def test_missing_unrelated_sources_do_not_block_unreferenced_reports(self):
        path, _ = self.report("unreferenced")
        self.write_settings(pinned_workspaces=[{"name": "Missing", "panels": [{
            "path": str(self.root / "missing" / "nested" / ".." / "source.txt")
        }]}])
        self.assertEqual(self.store.cleanup_reports(self.store.preview_reports()), [path])

    def test_stale_history_selection_preserves_concurrent_update(self):
        _, reference = self.report("record")
        original = self.workspace(reference)
        self.write_settings(pinned_workspaces=[{**original, "name": "Changed"}])
        before = self.settings_path.read_bytes()
        with self.assertRaisesRegex(OSError, "History changed"):
            self.store.remove_history([("pinned_workspaces", original)])
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_stale_recent_save_does_not_resurrect_removed_history(self):
        _, reference = self.report("recent")
        removed = self.workspace(reference)
        self.write_settings(recent_workspaces=[removed])
        stale = read_app_settings(self.settings_path)
        self.store.remove_history([("recent_workspaces", removed)])
        newer = self.workspace(reference, "New")
        newer["panels"] = [{"path": "another"}, {"path": "right"}]
        stale["recent_workspaces"].insert(0, newer)
        save_app_settings(stale, self.settings_path)
        self.assertEqual(read_app_settings(self.settings_path)["recent_workspaces"], [newer])

    def test_live_recovery_cannot_be_discarded(self):
        recovery = RecoveryStore(self.settings_path)
        recovery.write(self.snapshot())
        self.addCleanup(recovery.release)
        entries, _errors = self.store.inventory()
        live = next(item for item in entries if item.kind == "recovery")
        self.assertEqual(live.status, "Live — protected")
        self.assertIsNone(live.data)
        with self.assertRaises(RecoveryError):
            RecoveryStore(self.settings_path).discard(recovery.session_path)
        self.assertTrue(recovery.session_path.exists())

    def test_recovery_changed_after_inspection_cannot_be_discarded(self):
        recovery = RecoveryStore(self.settings_path)
        original = self.snapshot()
        recovery.write(original)
        recovery.release()
        recovery.session_path.write_text(json.dumps(self.snapshot({"sha256": "a" * 64})))
        with self.assertRaisesRegex(RecoveryError, "changed since inspection"):
            recovery.discard(recovery.session_path, expected=original)
        self.assertTrue(recovery.session_path.exists())

    def test_live_report_loaded_in_other_process_survives(self):
        path, reference = self.report("other process")
        script = ("import sys; from pathlib import Path; from d1ffler.comparison_context import RetainedReports; "
                  "from d1ffler.local_data import release_report_pins; root=Path(sys.argv[1]); "
                  "RetainedReports(root/'settings.json').load({'sha256':sys.argv[2]}); "
                  "print('ready',flush=True); sys.stdin.readline(); release_report_pins(root)")
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root), reference["sha256"]],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertEqual(self.store.preview_reports(), [])
            self.assertTrue(path.exists())
            _out, errors = process.communicate("release\n", timeout=15)
            self.assertEqual(process.returncode, 0, errors)
            self.assertEqual([entry.path for entry in self.store.preview_reports()], [path])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_unreadable_live_marker_blocks_cleanup(self):
        path, _ = self.report("protected by uncertain owner")
        directory = self.root / "D1ffler-report-users"
        directory.mkdir()
        marker = directory / "owner.json"
        marker.write_text("broken")
        lease = state_lock(marker.with_suffix(".json.lock"))
        self.assertTrue(lease.tryLock(0))
        try:
            with self.assertRaises(OSError):
                self.store.preview_reports()
            self.assertTrue(path.exists())
        finally:
            lease.unlock()
        self.assertEqual(len(self.store.preview_reports()), 1)

    def test_other_process_state_transaction_blocks_cleanup(self):
        path, _ = self.report("transaction in progress")
        script = ("import sys; from pathlib import Path; from d1ffler.local_data import storage_guard; "
                  "root=Path(sys.argv[1]); guard=storage_guard(root); guard.__enter__(); "
                  "print('locked',flush=True); sys.stdin.readline(); guard.__exit__(None,None,None)")
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(OSError, "Could not lock"):
                self.store.preview_reports()
            self.assertTrue(path.exists())
            _out, errors = process.communicate("release\n", timeout=15)
            self.assertEqual(process.returncode, 0, errors)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_failed_history_write_preserves_original_settings(self):
        record = {"name": "Old", "panels": []}
        self.write_settings(recent_workspaces=[record])
        before = self.settings_path.read_bytes()
        with patch("d1ffler.local_data.atomic_write", side_effect=PermissionError("denied")), self.assertRaises(OSError):
            self.store.remove_history([("recent_workspaces", record)])
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_report_pins_release_only_after_last_comparison_closes(self):
        path, reference = self.report("open comparison")
        first = self.window()
        second = self.window()
        report = RetainedReports(self.settings_path).load(reference)
        first.active_comparison.sync_reports = [report]
        self.assertEqual(self.store.preview_reports(), [])
        first.close()
        self.assertEqual(self.store.preview_reports(), [])
        second.close()
        self.assertEqual([entry.path for entry in self.store.preview_reports()], [path])

    def test_retention_disabled_by_default_and_respects_age_and_refs(self):
        old, _ = self.report("old")
        young, _ = self.report("young", days=2)
        referenced, reference = self.report("referenced")
        self.write_settings(recent_workspaces=[self.workspace(reference)])
        self.assertEqual(self.store.apply_retention(), [])
        self.store.save_policy(True, 30)
        self.assertEqual(self.store.apply_retention(), [old])
        self.assertTrue(young.exists())
        self.assertTrue(referenced.exists())
        self.store.save_policy(True, 0)
        self.assertEqual(self.store.apply_retention(), [young])
        self.assertTrue(referenced.exists())

    def test_diagnostics_clear_keeps_exports_and_continues_recording(self):
        diagnostics = DiagnosticStore(self.root / "diagnostics")
        diagnostics.MAX_BYTES = 1500
        for _ in range(8):
            diagnostics.record_exception("incident", RuntimeError("sensitive content"))
        exported = self.root / "exported-diagnostics.json"
        diagnostics.export(exported)
        diagnostics.clear()
        self.assertEqual(diagnostics.read_events(), [])
        self.assertTrue(exported.exists())
        self.assertTrue(diagnostics.record_exception("new", ValueError()).logged)
        self.assertEqual(len(diagnostics.read_events()), 1)

    def test_manager_removal_keeps_unsaved_buffers_and_updates_all_tabs(self):
        _, reference = self.report("shared")
        record = self.workspace(reference)
        self.write_settings(recent_workspaces=[record])
        window = self.window()
        first = window.active_comparison
        source = self.root / "open-source.txt"
        source.write_text("saved text")
        first._load_panel(0, "file", source)
        first.panels[0]["editor"].setPlainText("UNSAVED TEXT")
        second = window.new_comparison()
        dialog = LocalDataDialog(first)
        self.select(dialog, "recent_workspaces")
        with patch.object(dialog, "_confirm", return_value=True):
            dialog._remove_history()
            dialog._clear_diagnostics()
        self.assertEqual(first._panel_text(first.panels[0]), "UNSAVED TEXT")
        self.assertTrue(first.panels[0]["dirty"])
        self.assertEqual(first.settings["recent_workspaces"], [])
        self.assertEqual(second.settings["recent_workspaces"], [])
        self.assertEqual(window.settings["recent_workspaces"], [])
        dialog.deleteLater()

    def test_manager_restores_abandoned_recovery_in_new_tab(self):
        seed = DiffApp(DEFAULT_SETTINGS.copy(), self.settings_path)
        self.windows.append(seed)
        seed.panels[0]["editor"].setPlainText("recover me")
        seed._write_recovery_snapshot()
        seed._recovery_timer.stop()
        seed._recovery_store.release(new_session=True)
        window = self.window()
        original = window.active_comparison
        source = self.root / "open-source.txt"
        source.write_text("saved text")
        original._load_panel(0, "file", source)
        original.panels[0]["editor"].setPlainText("keep current edits")
        dialog = LocalDataDialog(original)
        item = self.select(dialog, "recovery")
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        self.assertTrue(dialog.restore_recovery(entry))
        self.assertEqual(len(window.comparisons), 2)
        self.assertEqual(original._panel_text(original.panels[0]), "keep current edits")
        self.assertTrue(original.panels[0]["dirty"])
        self.assertEqual(window.active_comparison._panel_text(window.active_comparison.panels[0]), "recover me")
        self.assertTrue(window.active_comparison.panels[0]["recovery_pending"])
        dialog.deleteLater()

    def test_manager_cancel_leaves_history_and_recovery_untouched(self):
        record = {"name": "Old", "panels": [{"path": "source"}]}
        self.write_settings(recent_workspaces=[record])
        window = self.window()
        dialog = LocalDataDialog(window.active_comparison)
        self.select(dialog, "recent_workspaces")
        before = self.settings_path.read_bytes()
        with patch.object(dialog, "_confirm", return_value=False):
            dialog._remove_history()
        self.assertEqual(self.settings_path.read_bytes(), before)
        dialog.deleteLater()

    def test_workspaces_menu_opens_local_data_management(self):
        window = self.window()
        view = window.active_comparison
        def choose_management(menu, *_args):
            action = next(action for action in menu.actions() if action.text() == "Local history and storage…")
            action.trigger()
        with patch("d1ffler.workspace_mixin.QMenu.exec", new=choose_management), patch("d1ffler.local_data_dialog.LocalDataDialog.exec") as opened:
            view._show_workspace_menu()
        opened.assert_called_once()

    def test_directory_junction_is_retained(self):
        external = self.root / "external-source"
        external.mkdir()
        payload = b"source content"
        digest = hashlib.sha256(payload).hexdigest()
        source = external / f"{digest}.json"
        source.write_bytes(payload)
        link = self.store.reports
        if sys.platform == "win32":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(external, target_is_directory=True)
        try:
            with self.assertRaisesRegex(OSError, "Linked local data"):
                self.store.preview_reports()
            self.assertEqual(source.read_bytes(), payload)
        finally:
            # Remove the exact link only, never its target or contents.
            link.rmdir() if sys.platform == "win32" else link.unlink()
