"""User-state migration, durable writes, and cross-process ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QMessageBox
from d1ffler.app_settings import (SettingsState, SettingsConflictError, load_app_settings,
                          read_app_settings, save_app_settings, settings_candidates)
from d1ffler.comparison_tabs import ComparisonWindow
from d1ffler.diff_tool import DEFAULT_SETTINGS, DiffApp
from d1ffler.recovery import RecoveryStore, RecoveryError, MAX_SNAPSHOTS, migrate_recovery
from d1ffler.user_state import state_lock


SNAPSHOT = {"version": 1, "panels": [{"recover": True, "text": "unfinished"}]}


class UserStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "user" / "appsettings.json"

    def store(self, path=None):
        store = RecoveryStore(path or self.path)
        self.addCleanup(store.release)
        return store

    def abandoned_snapshots(self, count=MAX_SNAPSHOTS):
        stores = []
        for index in range(count):
            store = self.store()
            store.write({"version": 1, "panels": [{"recover": True, "text": f"abandoned {index}"}]})
            os.utime(store.session_path, (1000 + index, 1000 + index))
            store.release()
            stores.append(store)
        return stores

    def child(self, script, *arguments):
        child = subprocess.Popen([sys.executable, "-u", "-c", script, *map(str, arguments)],
                                 cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.addCleanup(cleanup)
        return child

    def test_frozen_settings_location_is_independent_of_executable_and_extraction(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "user")}), \
             patch("d1ffler.app_settings.sys.platform", "win32"), patch("d1ffler.app_settings.sys.frozen", True, create=True):
            with patch("d1ffler.app_settings.sys.executable", str(self.root / "protected" / "D1ffler.exe")):
                first = settings_candidates()
            with patch("d1ffler.app_settings.sys.executable", str(self.root / "upgrade" / "D1ffler.exe")):
                second = settings_candidates()
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[0], self.root / "user" / "D1ffler" / "appsettings.json")
        self.assertEqual(first[1].parent, self.root / "protected")
        self.assertEqual(len(first), 2)

    def test_legacy_import_retains_originals_and_does_not_repeat_discarded_recovery(self):
        legacy = self.root / "installation" / "appsettings.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"theme": "light", "pinned_workspaces": [{"name": "Legacy"}]}), encoding="utf-8")
        recovery = legacy.parent / "D1ffler-recovery"
        recovery.mkdir()
        original = recovery / "old.json"
        original.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
        before = original.read_bytes()
        with patch("d1ffler.app_settings.settings_candidates", return_value=[self.path, legacy]):
            settings, selected = load_app_settings()
        self.assertEqual(selected, self.path)
        self.assertEqual(settings["theme"], "light")
        self.assertEqual(settings["pinned_workspaces"][0]["name"], "Legacy")
        self.assertFalse(settings.startup_warnings)
        self.assertTrue(legacy.exists())
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(list(recovery.glob("*.lock")))
        store = self.store()
        imported = store.snapshots()[0][0]
        store.discard(imported)
        settings["theme"] = "dark"
        save_app_settings(settings, self.path)
        with patch("d1ffler.app_settings.settings_candidates", return_value=[self.path, legacy]):
            reloaded, _ = load_app_settings()
        self.assertEqual(reloaded["theme"], "dark")
        self.assertEqual(store.snapshots(), [])
        self.assertEqual(original.read_bytes(), before)

    def test_failed_settings_import_retains_loaded_state_and_supports_retry(self):
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
        with patch("d1ffler.app_settings.settings_candidates", return_value=[self.path, legacy]), \
             patch("d1ffler.app_settings.atomic_write", side_effect=PermissionError("read-only target")):
            settings, destination = load_app_settings()
        self.assertEqual(settings["theme"], "light")
        self.assertTrue(settings.startup_warnings)
        self.assertFalse(destination.exists())
        self.assertTrue(legacy.exists())
        save_app_settings(settings, destination)
        self.assertEqual(read_app_settings(destination)["theme"], "light")

    def test_interrupted_settings_save_preserves_previous_file_and_baseline(self):
        settings = SettingsState()
        save_app_settings(settings, self.path)
        original = self.path.read_bytes()
        baseline = deepcopy(settings.baseline)
        settings["theme"] = "light"
        for failing_call in ("d1ffler.user_state.os.fsync", "d1ffler.user_state.os.replace"):
            with self.subTest(failure=failing_call):
                with patch(failing_call, side_effect=OSError("injected interruption")):
                    with self.assertRaises(OSError):
                        save_app_settings(settings, self.path)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(settings.baseline, baseline)
                self.assertEqual(settings["theme"], "light")
                self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        save_app_settings(settings, self.path)
        self.assertEqual(read_app_settings(self.path)["theme"], "light")

    def test_invalid_or_future_settings_are_retained_with_visible_startup_warning(self):
        self.path.parent.mkdir()
        for payload in (b"{broken", b'{"schema_version":99}'):
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                with patch("d1ffler.app_settings.settings_candidates", return_value=[self.path]):
                    settings, _ = load_app_settings()
                self.assertTrue(settings.startup_warnings)
                with self.assertRaises(OSError):
                    save_app_settings(settings, self.path)
                self.assertEqual(self.path.read_bytes(), payload)

    def test_shared_state_merges_independent_updates_and_rejects_same_name_conflict(self):
        initial = SettingsState()
        initial["pinned_workspaces"] = [{"name": "Shared", "note": "original"}]
        save_app_settings(initial, self.path)
        first, second = read_app_settings(self.path), read_app_settings(self.path)
        first["pinned_workspaces"].append({"name": "First"})
        first["theme"] = "light"
        save_app_settings(first, self.path)
        second["pinned_workspaces"].append({"name": "Second"})
        save_app_settings(second, self.path)
        self.assertEqual({item["name"] for item in second["pinned_workspaces"]}, {"Shared", "First", "Second"})
        self.assertEqual(second["theme"], "light")
        first, second = read_app_settings(self.path), read_app_settings(self.path)
        first["pinned_workspaces"][0]["note"] = "first update"
        second["pinned_workspaces"][0]["note"] = "second update"
        save_app_settings(first, self.path)
        original = self.path.read_bytes()
        with self.assertRaises(SettingsConflictError):
            save_app_settings(second, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(second["pinned_workspaces"][0]["note"], "second update")

    def test_two_processes_can_add_workspaces_without_losing_either(self):
        save_app_settings(SettingsState(), self.path)
        script = """
import sys
from pathlib import Path
from PyQt6.QtCore import QCoreApplication
from d1ffler.app_settings import read_app_settings, save_app_settings
app = QCoreApplication([])
path = Path(sys.argv[1])
settings = read_app_settings(path)
print('ready', flush=True)
sys.stdin.readline()
settings['pinned_workspaces'].append({'name': sys.argv[2]})
save_app_settings(settings, path)
"""
        children = [self.child(script, self.path, name) for name in ("First", "Second")]
        for child in children:
            self.assertEqual(child.stdout.readline().strip(), "ready")
        for child in children:
            child.stdin.write("save\n")
            child.stdin.flush()
        for child in children:
            _output, errors = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, errors)
        self.assertEqual({item["name"] for item in read_app_settings(self.path)["pinned_workspaces"]}, {"First", "Second"})

    def test_busy_settings_lock_preserves_disk(self):
        save_app_settings(SettingsState(), self.path)
        original = self.path.read_bytes()
        lock = state_lock(self.path.with_name(self.path.name + ".lock"))
        self.assertTrue(lock.tryLock(0))
        try:
            with self.assertRaises(OSError):
                save_app_settings(SettingsState(), self.path)
            self.assertEqual(self.path.read_bytes(), original)
        finally:
            lock.unlock()

    def test_live_recovery_is_not_offered_adopted_discarded_or_pruned(self):
        owner, reader = self.store(), self.store()
        owner.write(SNAPSHOT)
        self.assertEqual(reader.snapshots(), [])
        self.assertEqual(len(reader.snapshots(include_active=True)), 1)
        with self.assertRaises(RecoveryError):
            reader.adopt(owner.session_path)
        with self.assertRaises(RecoveryError):
            reader.discard(owner.session_path)
        for _index in range(MAX_SNAPSHOTS + 2):
            store = self.store()
            store.write(SNAPSHOT)
            store.release()
        self.assertTrue(owner.session_path.exists())
        owner.release()
        reader.adopt(owner.session_path, expected=SNAPSHOT)
        with self.assertRaises(RecoveryError):
            owner.clear()
        self.assertTrue(reader.session_path.exists())

    def test_pruning_counts_real_live_sessions_toward_total_capacity(self):
        live = [self.store(), self.store()]
        for store in live:
            store.write(SNAPSHOT)
        abandoned = self.abandoned_snapshots(MAX_SNAPSHOTS + 2)
        self.assertEqual({path for path, _ in live[0].snapshots(include_active=True)},
                         {store.session_path for store in live} |
                         {store.session_path for store in abandoned[-(MAX_SNAPSHOTS - len(live)):]})

    def test_protected_existing_snapshot_keeps_a_slot_and_missing_paths_do_not(self):
        abandoned = self.abandoned_snapshots()
        writer = self.store()
        empty = [self.store() for _index in range(MAX_SNAPSHOTS + 2)]
        writer.protected_paths = lambda: {abandoned[0].session_path} | {store.session_path for store in empty}
        writer.write(SNAPSHOT)
        self.assertEqual({path for path, _ in writer.snapshots(include_active=True)},
                         {writer.session_path, abandoned[0].session_path} |
                         {store.session_path for store in abandoned[-(MAX_SNAPSHOTS - 2):]})
        self.assertTrue(all(not store.session_path.exists() for store in empty))

    def test_live_snapshots_above_limit_survive_without_abandoned_history(self):
        abandoned = self.abandoned_snapshots(MAX_SNAPSHOTS - 1)
        live = [self.store() for _index in range(MAX_SNAPSHOTS + 2)]
        for store in live:
            store.write(SNAPSHOT)
        self.assertEqual({path for path, _ in live[0].snapshots(include_active=True)},
                         {store.session_path for store in live})
        self.assertTrue(all(not store.session_path.exists() for store in abandoned))
        # Tab protection also works without a lease while sessions are handed off.
        live[0].protected_paths = lambda: {store.session_path for store in live}
        for store in live:
            store.release()
        live[0].write(SNAPSHOT)
        self.assertEqual({path for path, _ in live[0].snapshots(include_active=True)},
                         {store.session_path for store in live})
        # Once owners/protection are gone, the usual bound applies again.
        live[0].protected_paths = lambda: set()
        live[0].write(SNAPSHOT)
        self.assertEqual(len(live[0].snapshots(include_active=True)), MAX_SNAPSHOTS)

    def test_pruning_retains_a_snapshot_claimed_after_enumeration(self):
        abandoned = self.abandoned_snapshots()
        writer, claimant = self.store(), self.store()
        discard = writer.discard

        def claim_before_discard(path, *, expected):
            claimant.adopt(path, expected=expected)
            discard(path, expected=expected)

        with patch.object(writer, "discard", side_effect=claim_before_discard):
            writer.write(SNAPSHOT)
        self.assertEqual(claimant.session_path, abandoned[0].session_path)
        self.assertIsNotNone(claimant._lease)
        self.assertEqual(json.loads(claimant.session_path.read_text())["panels"][0]["text"], "abandoned 0")
        self.assertEqual(len(writer.snapshots(include_active=True)), MAX_SNAPSHOTS + 1)

    def test_pruning_retains_a_snapshot_changed_after_enumeration(self):
        abandoned = self.abandoned_snapshots()
        writer = self.store()
        discard = writer.discard
        changed = {"version": 1, "panels": [{"recover": True, "text": "newer unfinished edits"}]}

        def change_before_discard(path, *, expected):
            path.write_text(json.dumps(changed), encoding="utf-8")
            discard(path, expected=expected)

        with patch.object(writer, "discard", side_effect=change_before_discard):
            writer.write(SNAPSHOT)
        self.assertEqual(json.loads(abandoned[0].session_path.read_text()), changed)
        self.assertEqual(len(writer.snapshots(include_active=True)), MAX_SNAPSHOTS + 1)

    def test_pruning_failed_deletion_retains_snapshot_without_deleting_a_substitute(self):
        abandoned = self.abandoned_snapshots()
        writer = self.store()
        unlink = Path.unlink
        original = abandoned[0].session_path.read_bytes()

        def fail_oldest_unlink(path, *args, **kwargs):
            if path == abandoned[0].session_path:
                raise PermissionError("Recovery removal denied")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_oldest_unlink):
            writer.write(SNAPSHOT)
        self.assertEqual(abandoned[0].session_path.read_bytes(), original)
        self.assertEqual(len(writer.snapshots(include_active=True)), MAX_SNAPSHOTS + 1)
        self.assertEqual(json.loads(writer.session_path.read_text()), SNAPSHOT)
        writer.write(SNAPSHOT)
        self.assertFalse(abandoned[0].session_path.exists())
        self.assertEqual(len(writer.snapshots(include_active=True)), MAX_SNAPSHOTS)

    def test_pruning_retains_snapshot_with_uncertain_ownership(self):
        abandoned = self.abandoned_snapshots()
        writer = self.store()
        from d1ffler.recovery import safe_local

        def uncertain_lock(root, path):
            if path == abandoned[0].session_path.with_suffix(".json.lock"):
                raise PermissionError("Ownership could not be checked")
            safe_local(root, path)

        # Enumeration succeeds; an ownership check then becomes inaccessible.
        snapshots = writer.snapshots

        def enumerate_then_fail(**kwargs):
            found = snapshots(**kwargs)
            ownership.start()
            return found

        ownership = patch("d1ffler.recovery.safe_local", side_effect=uncertain_lock)
        try:
            with patch.object(writer, "snapshots", side_effect=enumerate_then_fail):
                writer.write(SNAPSHOT)
        finally:
            ownership.stop()
        self.assertTrue(abandoned[0].session_path.exists())
        self.assertEqual(len(writer.snapshots(include_active=True)), MAX_SNAPSHOTS)

    def test_clear_waits_for_storage_guard_and_retains_snapshot_on_lock_failure(self):
        owner = self.store()
        owner.write(SNAPSHOT)
        original = owner.session_path.read_bytes()
        guard = state_lock(self.path.parent / "local-data.lock")
        self.assertTrue(guard.tryLock(0))
        try:
            with self.assertRaisesRegex(RecoveryError, "Could not clear recovery"):
                owner.clear()
            self.assertEqual(owner.session_path.read_bytes(), original)
            self.assertIsNotNone(owner._lease)
        finally:
            guard.unlock()
        owner.clear()
        self.assertFalse(owner.session_path.exists())
        self.assertIsNone(owner._lease)

    def test_recovery_claim_checks_snapshot_changes_since_review(self):
        owner, reader = self.store(), self.store()
        owner.write(SNAPSHOT)
        owner.release()
        changed = deepcopy(SNAPSHOT)
        changed["panels"][0]["text"] = "newer buffer"
        owner.session_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(RecoveryError):
            reader.adopt(owner.session_path, expected=SNAPSHOT)
        self.assertEqual(reader.snapshots()[0][1], changed)

    def test_temporary_cleanup_protects_live_writes_and_removes_abandoned_writes(self):
        owner, abandoned, reader = self.store(), self.store(), self.store()
        owner.write(SNAPSHOT)
        live = owner.directory / f".{owner.session_path.name}-inflight.tmp"
        orphan = owner.directory / f".{abandoned.session_path.name}-interrupted.tmp"
        live.write_bytes(b"live write")
        orphan.write_bytes(b"abandoned write")
        reader.snapshots()
        self.assertTrue(live.exists())
        self.assertFalse(orphan.exists())
        owner.release()
        reader.snapshots()
        self.assertFalse(live.exists())
        settings = SettingsState()
        save_app_settings(settings, self.path)
        temporary = self.path.parent / f".{self.path.name}-interrupted.tmp"
        temporary.write_bytes(b"partial settings")
        save_app_settings(settings, self.path)
        self.assertFalse(temporary.exists())

    def test_dead_process_recovery_becomes_available(self):
        script = """
import sys
from pathlib import Path
from PyQt6.QtCore import QCoreApplication
from d1ffler.recovery import RecoveryStore
app = QCoreApplication([])
store = RecoveryStore(Path(sys.argv[1]))
store.write({'version': 1, 'panels': [{'recover': True, 'text': 'child edits'}]})
print('ready', flush=True)
sys.stdin.readline()
"""
        child = self.child(script, self.path)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        reader = self.store()
        self.assertEqual(reader.snapshots(), [])
        live_path, live_data = reader.snapshots(include_active=True)[0]
        abandoned = self.abandoned_snapshots(MAX_SNAPSHOTS + 2)
        self.assertEqual({path for path, _ in reader.snapshots(include_active=True)},
                         {live_path} | {store.session_path for store in abandoned[-(MAX_SNAPSHOTS - 1):]})
        self.assertEqual(json.loads(live_path.read_text()), live_data)
        child.kill()
        child.communicate(timeout=10)
        snapshot = dict(reader.snapshots())[live_path]
        self.assertEqual(snapshot["panels"][0]["text"], "child edits")
        reader.adopt(live_path, expected=snapshot)
        reader.write(snapshot)

    def test_migration_skips_live_recovery_and_retries_after_owner_exits(self):
        legacy = self.store(self.root / "legacy" / "settings.json")
        legacy.write(SNAPSHOT)
        destination = self.store()
        migrate_recovery(legacy.directory, destination.directory)
        self.assertEqual(destination.snapshots(), [])
        legacy.release()
        migrate_recovery(legacy.directory, destination.directory)
        self.assertEqual(destination.snapshots()[0][1], SNAPSHOT)
        self.assertTrue(legacy.session_path.exists())

    def test_failed_recovery_write_preserves_previous_snapshot(self):
        store = self.store()
        store.write(SNAPSHOT)
        original = store.session_path.read_bytes()
        with patch("d1ffler.user_state.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(RecoveryError):
                store.write({"version": 1, "panels": [{"recover": True, "text": "next"}]})
        self.assertEqual(store.session_path.read_bytes(), original)
        self.assertEqual(list(store.directory.glob("*.tmp")), [])

    def test_partial_recovery_migration_can_retry_without_losing_or_reimporting_data(self):
        legacy = self.root / "legacy-recovery"
        legacy.mkdir()
        original = legacy / "old.json"
        original.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
        destination = self.store()
        from d1ffler.recovery import atomic_write
        def fail_index(path, payload):
            if path.name == "migration-index.json":
                raise OSError("migration index failed")
            atomic_write(path, payload)
        with patch("d1ffler.recovery.atomic_write", side_effect=fail_index):
            with self.assertRaises(OSError):
                migrate_recovery(legacy, destination.directory)
        self.assertTrue(original.exists())
        self.assertEqual(len(destination.snapshots()), 1)
        migrate_recovery(legacy, destination.directory)
        self.assertEqual(len(destination.snapshots()), 1)
        destination.discard(destination.snapshots()[0][0])
        migrate_recovery(legacy, destination.directory)
        self.assertEqual(destination.snapshots(), [])
        moved = self.root / "moved-legacy-recovery"
        moved.mkdir()
        (moved / original.name).write_bytes(original.read_bytes())
        migrate_recovery(moved, destination.directory)
        self.assertEqual(destination.snapshots(), [])


class UserStateGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "state" / "settings.json"
        self.hosts = []
        self.addCleanup(self.cleanup_windows)
        self.files = [self.root / "left.txt", self.root / "right.txt"]
        for path in self.files:
            path.write_text("source\n", encoding="utf-8")

    def host(self):
        host = ComparisonWindow(read_app_settings(self.path), self.path, DiffApp)
        self.hosts.append(host)
        return host

    def cleanup_windows(self):
        for host in self.hosts:
            with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
                host.close()
            host.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def save_named(self, view, name):
        with patch("d1ffler.workspace_mixin.QInputDialog.getText", return_value=(name, True)):
            view._save_named_workspace()

    def test_failed_workspace_save_is_not_claimed_successful_and_can_retry(self):
        host = self.host()
        view = host.open_paths(self.files, new_tab=False)
        view.panels[0]["editor"].setPlainText("retained local edits\n")
        original = self.path.read_bytes()
        with patch("d1ffler.comparison_tabs.save_app_settings", side_effect=PermissionError("read-only state")), \
             patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.save_named(view, "Pending")
        self.assertIn("not saved", view.status.text())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNone(view._active_workspace_name)
        self.assertEqual(view._pending_named_workspace["name"], "Pending")
        self.assertEqual(view._panel_text(view.panels[0]), "retained local edits\n")
        self.assertTrue(view.panels[0]["dirty"])
        self.save_named(view, "Pending")
        self.assertEqual(read_app_settings(self.path)["pinned_workspaces"][0]["name"], "Pending")
        self.assertIsNone(view._pending_named_workspace)
        self.assertEqual(view.status.text(), "Saved workspace: Pending")

    def test_same_workspace_conflict_survives_recent_refresh_and_can_save_copy(self):
        seed = self.host().open_paths(self.files, new_tab=False)
        self.save_named(seed, "Shared")
        snapshot = deepcopy(read_app_settings(self.path)["pinned_workspaces"][0])
        first = self.host().open_workspace(snapshot)
        second = self.host().open_workspace(snapshot)
        first.review_notes = [{"text": "first reviewer"}]
        self.save_named(first, "Shared")
        second.review_notes = [{"text": "second reviewer"}]
        second._record_recent_workspace()
        with patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.save_named(second, "Shared")
        self.assertIn("not saved", second.status.text())
        self.assertEqual(read_app_settings(self.path)["pinned_workspaces"][0]["review_notes"][0]["text"], "first reviewer")
        second._record_recent_workspace()
        self.assertEqual(read_app_settings(self.path)["pinned_workspaces"][0]["review_notes"][0]["text"], "first reviewer")
        self.save_named(second, "My copy")
        saved = {item["name"]: item for item in read_app_settings(self.path)["pinned_workspaces"]}
        self.assertEqual(saved["Shared"]["review_notes"][0]["text"], "first reviewer")
        self.assertEqual(saved["My copy"]["review_notes"][0]["text"], "second reviewer")

    def test_another_window_does_not_offer_live_recovery(self):
        first = self.host()
        first.active_comparison.panels[0]["editor"].setPlainText("live comparison")
        first.active_comparison._write_recovery_snapshot()
        second = self.host()
        with patch("d1ffler.comparison_tabs.RecoveryDialog") as dialog:
            self.assertFalse(second.offer_recovery())
        dialog.assert_not_called()
        self.assertTrue(first.close())
        self.assertEqual(second.active_comparison._recovery_store.snapshots()[0][1]["panels"][0]["text"], "live comparison")

    def test_failed_restore_preserves_abandoned_snapshot_when_window_closes(self):
        host = self.host()
        store = RecoveryStore(self.path)
        store.write(SNAPSHOT)
        store.release()
        path = store.session_path
        def restore(dialog):
            dialog.choice = "restore"
            return 0
        with patch("d1ffler.comparison_tabs.RecoveryDialog.exec", restore), \
             patch.object(host.active_comparison, "_restore_recovery", return_value=False):
            self.assertFalse(host.offer_recovery())
        self.assertTrue(host.close())
        self.assertTrue(path.exists())

    def test_pending_named_save_requires_resolution_before_close(self):
        host = self.host()
        view = host.open_paths(self.files, new_tab=False)
        view.review_notes = [{"id": "note", "source_key": "key", "text": "pending decision"}]
        with patch("d1ffler.comparison_tabs.save_app_settings", side_effect=PermissionError("blocked")), \
             patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.save_named(view, "Pending")
        self.assertFalse(any(panel["dirty"] for panel in view.panels))
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(host.close())
        self.assertFalse(view._closed)
        self.assertEqual(view.review_notes[0]["text"], "pending decision")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Save), \
             patch("d1ffler.workspace_mixin.QInputDialog.getText", return_value=("Pending", True)):
            self.assertTrue(host.close())
        self.assertEqual(read_app_settings(self.path)["pinned_workspaces"][0]["review_notes"][0]["text"], "pending decision")
        host = self.host()
        view = host.open_paths(self.files, new_tab=False)
        with patch("d1ffler.comparison_tabs.save_app_settings", side_effect=PermissionError("blocked")), \
             patch("d1ffler.workspace_mixin.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.save_named(view, "Discarded")
        with patch("d1ffler.diff_tool.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(host.close())
        self.assertNotIn("Discarded", {item["name"] for item in read_app_settings(self.path)["pinned_workspaces"]})


if __name__ == "__main__":
    unittest.main()
