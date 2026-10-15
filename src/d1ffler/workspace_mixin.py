"""Workspace snapshots, recent sessions, and recovery actions."""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMenu, QMessageBox

from d1ffler.app_settings import save_app_settings, settings_for_save
from d1ffler.file_content import (
    disk_fingerprint, inspect_file_content, unloaded_file_message,
)
from d1ffler.folder_archives import is_zip_source
from d1ffler.git_worktrees import GitError, repository_root, worktrees
from d1ffler.recovery import RecoveryError
from d1ffler.text_endings import TextRepresentationError
from d1ffler.recovery_dialog import RecoveryDialog
from d1ffler.comparison_context import ComparisonContextMixin
from d1ffler.source_paths import has_relative_sources, resolve_source_records


class WorkspaceMixin(ComparisonContextMixin):
    def _workspace_sources(self):
        return [
            {"name": panel["name"].text(), "custom_name": panel["custom_name"], "kind": panel["kind"],
             "path": str(panel["path"]), "git": panel.get("git")}
            for panel in self.panels if panel["kind"] in {"file", "folder"} and panel["path"] is not None
        ]

    def _workspace_snapshot(self, name: str) -> dict[str, object] | None:
        sources = self._workspace_sources()
        if len(sources) < 2:
            return None
        return {**self._comparison_context_snapshot(name), "panels": sources}

    def _recovery_snapshot(self) -> dict | None:
        records = []
        has_recoverable = False
        for index, panel in enumerate(self.panels):
            text = self._panel_text_with_line_endings(panel)
            result = self.merge_mode and index == 3
            recover = bool(panel.get("recovery_pending") or
                           (result and self.merge_touched and panel.get("dirty")) or
                           (not result and (panel.get("dirty") or
                                            (panel["kind"] == "text" and bool(text)))))
            has_recoverable |= recover
            records.append({
                "kind": panel["kind"], "path": str(panel["path"]) if panel["path"] else None,
                "editing_path": str(panel["editing_path"]) if panel["editing_path"] else None,
                "name": panel["name"].text(), "custom_name": panel["custom_name"],
                "encoding": panel["encoding"], "bom": panel["bom"].hex(),
                "disk_fingerprint": panel.get("disk_fingerprint"),
                "saved_text": panel.get("saved_text", "") if recover else "",
                "saved_text_with_endings": panel.get("saved_text_with_endings") if recover else None,
                "text": text if recover else None, "recover": recover,
                "git": panel.get("git"),
                "unresolved_source": deepcopy(panel.get("unresolved_source")),
            })
        if not has_recoverable:
            return None
        return {"version": 2, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "context": self._comparison_context_snapshot(self._active_workspace_name),
                "merge_mode": self.merge_mode, "merge_touched": getattr(self, "merge_touched", False),
                "merge_stale": self.merge_stale,
                "merge_resolutions": self.merge_resolutions, "panels": records}

    def _schedule_recovery(self) -> None:
        if (not self._recovery_restoring and not self._loading_panel and not self._loading_workspace
                and not self._rendering_diff and not self._rebuilding_folder_trees):
            self._recovery_timer.start(900)

    def _write_recovery_snapshot(self) -> bool:
        if self._recovery_restoring:
            return False
        try:
            snapshot = self._recovery_snapshot()
            if snapshot is None:
                self._recovery_store.clear()
            else:
                self._recovery_store.write(snapshot)
            return True
        except (RecoveryError, OSError, TextRepresentationError) as exc:
            self.status.setText(f"Unsaved edits could not be backed up: {exc}")
            return False

    def _offer_recovery(self) -> bool:
        for path, snapshot in self._recovery_store.snapshots():
            dialog = RecoveryDialog(snapshot, self)
            dialog.exec()
            if dialog.choice == "discard":
                try:
                    self._recovery_store.discard(path)
                except (RecoveryError, OSError) as exc:
                    QMessageBox.warning(self, "Discard recovery", str(exc))
                continue
            if dialog.choice == "restore":
                try:
                    self._recovery_store.adopt(path, expected=snapshot)
                except RecoveryError as exc:
                    QMessageBox.warning(self, "Restore recovery", str(exc))
                    continue
                if self._restore_recovery(snapshot):
                    self._write_recovery_snapshot()
                    return True
                self._recovery_store.release(new_session=True)
            break
        return False

    def _restore_recovery(self, snapshot: dict) -> bool:
        records = snapshot.get("panels", [])
        if not isinstance(records, list) or not records or len(records) > 4:
            return False
        if any(panel.get("dirty") or panel.get("recovery_pending") for panel in self.panels):
            return False
        snapshot = deepcopy(snapshot)
        if has_relative_sources(records):
            choice = QMessageBox.warning(
                self, "Locate recovery sources",
                "This recovery uses relative source paths and has no recorded original folder.\n\n"
                "Locate the original folder to reconnect those sources? Choose No to restore "
                "their edits as text and select a file explicitly when saving.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No)
            if choice == QMessageBox.StandardButton.Cancel:
                return False
            base = None
            if choice == QMessageBox.StandardButton.Yes:
                base = QFileDialog.getExistingDirectory(self, "Locate the original working folder for recovery")
                if not base:
                    return False
            try:
                records = resolve_source_records(records, base)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Locate recovery sources", str(exc))
                return False
            snapshot["panels"] = records
        self._recovery_restoring = True
        self._loading_workspace = True
        try:
            if not self._clear_all():
                return False
            self._restore_comparison_context(snapshot.get("context", {}))
            if snapshot.get("merge_mode"):
                self.merge_mode_button.setChecked(True)
            while len(self.panels) < len(records):
                self._add_panel()
            folder_preview = False
            merge_inputs_changed = False
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    continue
                panel = self.panels[index]
                kind = record.get("kind")
                source = Path(record["path"]) if record.get("path") else None
                if kind in {"file", "folder"} and source is not None and source.exists():
                    self._load_panel(index, kind, source, confirm=False)
                elif kind == "file" and source is not None:
                    panel["kind"], panel["path"] = "file", source
                elif kind == "folder" and source is not None:
                    panel["kind"], panel["path"] = "folder", source
                elif kind == "text" and not (self.merge_mode and index == 3):
                    panel["kind"] = "text"
                editing = Path(record["editing_path"]) if record.get("editing_path") else None
                if self.merge_mode and index < 3 and editing is not None:
                    try:
                        merge_inputs_changed |= disk_fingerprint(editing) != record.get("disk_fingerprint")
                    except OSError:
                        merge_inputs_changed = True
                if kind == "folder" and editing is not None and not record.get("recover"):
                    try:
                        content = inspect_file_content(editing, preview_limit=True)
                        current_text = content.text if content.text is not None else unloaded_file_message(editing, content)
                        panel["binary"] = content.text is None
                        panel["too_large"] = content.too_large
                        panel["disk_fingerprint"] = content.fingerprint
                        self._set_panel_encoding(panel, content.encoding or "utf-8", content.bom)
                    except OSError as exc:
                        current_text = f"Could not read {editing}: {exc}"
                        panel["disk_fingerprint"] = None
                    self._loading_panel = True
                    try:
                        panel["editor"].setPlainText(current_text)
                    finally:
                        self._loading_panel = False
                    panel["saved_text"] = self._panel_text(panel)
                    panel["saved_text_with_endings"] = self._panel_text_with_line_endings(panel)
                if record.get("recover"):
                    self._loading_panel = True
                    try:
                        panel["editor"].setPlainText(str(record.get("text") or ""))
                    finally:
                        self._loading_panel = False
                    panel["saved_text"] = str(record.get("saved_text") or "")
                    saved_raw = record.get("saved_text_with_endings")
                    if isinstance(saved_raw, str):
                        panel["saved_text_with_endings"] = saved_raw
                    else:
                        panel.pop("saved_text_with_endings", None)
                    panel["recovery_pending"] = True
                if editing is not None:
                    panel["editing_path"] = editing
                    if record.get("recover"):
                        panel["disk_fingerprint"] = record.get("disk_fingerprint")
                        try:
                            panel["disk_changed"] = disk_fingerprint(editing) != panel["disk_fingerprint"]
                        except OSError:
                            panel["disk_changed"] = True
                if kind == "folder" and editing is not None:
                    panel["view_stack"].setCurrentIndex(0)
                    folder_preview = True
                self._restore_workspace_git(panel, record.get("git"))
                if record.get("recover") or kind == "text":
                    try:
                        self._set_panel_encoding(panel, str(record.get("encoding") or "utf-8"),
                                                 bytes.fromhex(record.get("bom") or ""))
                    except ValueError:
                        self._set_panel_encoding(panel, "utf-8")
                panel["name"].setText(str(record.get("name") or self._panel_name(index)))
                panel["custom_name"] = bool(record.get("custom_name"))
                if record.get("unresolved_source"):
                    panel["unresolved_source"] = deepcopy(record["unresolved_source"])
                self._update_panel_modified_state(panel)
            if folder_preview:
                self.folder_back_button.show()
                self.folder_triage_bar.hide()
            if self.merge_mode and len(self.panels) == 4:
                self.merge_touched = bool(snapshot.get("merge_touched"))
                self.merge_stale = bool(snapshot.get("merge_stale")) or merge_inputs_changed
                self.merge_resolutions = {int(key): str(value) for key, value in
                                          snapshot.get("merge_resolutions", {}).items()}
                editor = self.panels[3]["editor"]
                editor.history.reset(editor.history.text(), editor.history.state()["endings"],
                                     {str(key): value for key, value in self.merge_resolutions.items()})
                self._merge_summary_text()
            self._update_panel_locking()
            self._sync_disk_watches()
            self._prepare_context_view(recovery=True)
            self._loading_workspace = False
            self._refresh_diff()
            self._restore_context_text_view()
            self._start_context_note_validation()
            self.status.setText("Recovered edits as text; choose a file when saving unresolved sources"
                                if any(panel.get("unresolved_source") for panel in self.panels) else
                                "Recovered unsaved comparison edits; review disk changes before saving")
            return True
        finally:
            self._loading_panel = False
            self._loading_workspace = False
            self._recovery_restoring = False

    def _persist_workspaces(self) -> bool:
        try:
            if self.comparison_host is not None:
                self.comparison_host.persist_settings(self, workspace_baseline=self._workspace_save_baseline)
            else:
                candidate = settings_for_save(self.settings, workspace_baseline=self._workspace_save_baseline)
                save_app_settings(candidate, self.settings_path)
                self.settings = candidate
        except OSError as exc:
            self._settings_save_error = str(exc)
            self.status.setText(f"Could not save user state: {exc}")
            return False
        self._settings_save_error = ""
        return True

    def _record_recent_workspace(self, *, persist: bool = True) -> bool:
        if self._loading_workspace:
            return True
        try:
            snapshot = self._workspace_snapshot("Recent comparison")
        except OSError as exc:
            self.status.setText(f"Could not retain comparison context: {exc}")
            return False
        if snapshot is None:
            return True
        recent = [item for item in self.settings.get("recent_workspaces", []) if isinstance(item, dict)]
        panels = snapshot["panels"]
        recent = [item for item in recent if item.get("panels") != panels]
        self.settings["recent_workspaces"] = [snapshot] + recent[:9]
        return self._persist_workspaces() if persist else True

    def _save_named_workspace(self) -> None:
        while True:
            pending = self._pending_named_workspace
            name, accepted = QInputDialog.getText(self, "Save workspace", "Workspace name:",
                                                text=pending["name"] if pending else self._active_workspace_name or "")
            if not accepted or not name.strip():
                return
            try:
                snapshot = self._workspace_snapshot(name.strip())
            except OSError as exc:
                QMessageBox.warning(self, "Save workspace", f"Could not retain comparison context: {exc}")
                return
            if snapshot is None:
                QMessageBox.information(self, "Save workspace", "Add at least two files or folders first.")
                return
            pinned = [item for item in self.settings.get("pinned_workspaces", []) if isinstance(item, dict)]
            if pending and pending["name"] != snapshot["name"]:
                # A retry with a new name must not also re-submit the conflicting old name.
                pinned = [item for item in pinned if item.get("name") != pending["name"]]
                pinned += [deepcopy(item) for item in self.settings.baseline.get("pinned_workspaces", [])
                           if item.get("name") == pending["name"]]
            self.settings["pinned_workspaces"] = [item for item in pinned if item.get("name") != snapshot["name"]] + [snapshot]
            self._pending_named_workspace = deepcopy(snapshot)
            self._record_recent_workspace(persist=False)
            if snapshot["name"] not in self._named_workspace_baselines:
                self._named_workspace_baselines[snapshot["name"]] = next(
                    (deepcopy(item) for item in self.settings.baseline["pinned_workspaces"]
                     if isinstance(item, dict) and item.get("name") == snapshot["name"]), None)
            self._workspace_save_baseline = (snapshot["name"], self._named_workspace_baselines[snapshot["name"]])
            try:
                saved = self._persist_workspaces()
            finally:
                self._workspace_save_baseline = None
            if saved:
                self._active_workspace_name = str(snapshot["name"])
                self._named_workspace_baselines[snapshot["name"]] = deepcopy(snapshot)
                self._pending_named_workspace = None
                self.status.setText(f"Saved workspace: {snapshot['name']}")
                self.comparison_changed.emit()
                return
            # Keep a failed named save separate from automatic recent-history saves.
            self.settings["pinned_workspaces"] = deepcopy(pinned)
            self.status.setText(f"Workspace '{snapshot['name']}' was not saved; use Save workspace to retry")
            choice = QMessageBox.warning(
                self, "Save workspace", f"Workspace '{snapshot['name']}' was not saved to {self.settings_path}:\n"
                f"{self._settings_save_error}\n\nYour comparison and pending workspace are retained. "
                "Retry to choose a name and save again.",
                QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Retry:
                return

    def _show_workspace_menu(self) -> None:
        menu = QMenu(self)
        for heading, key in (("Saved", "pinned_workspaces"), ("Recent", "recent_workspaces")):
            workspaces = [item for item in self.settings.get(key, []) if isinstance(item, dict)]
            if workspaces:
                menu.addSection(heading)
                for workspace in workspaces:
                    action = menu.addAction(str(workspace.get("name", "Unnamed workspace")))
                    opener = self.comparison_host.open_workspace if self.comparison_host is not None else self._load_workspace
                    action.triggered.connect(lambda _checked=False, item=workspace: opener(item))
        if self._pending_named_workspace:
            menu.addSection("Not saved")
            retry = menu.addAction(f"Retry saving: {self._pending_named_workspace['name']}")
            retry.triggered.connect(self._save_named_workspace)
        if menu.isEmpty():
            menu.addAction("No saved or recent workspaces").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Local history and storage…", self._open_local_data)
        menu.exec(self.workspaces_button.mapToGlobal(self.workspaces_button.rect().bottomLeft()))

    def _resolve_workspace_sources(self, workspace):
        if not isinstance(workspace, dict) or not isinstance(workspace.get("panels"), list):
            return workspace
        sources = workspace["panels"][:4]
        if len(sources) < 2 or any(not isinstance(item, dict) or
                not isinstance(item.get("path"), str) or not item["path"] for item in sources):
            return workspace
        if not has_relative_sources(sources):
            return workspace
        base = QFileDialog.getExistingDirectory(
            self, "Locate the original working folder for this legacy workspace")
        if not base:
            return None
        try:
            resolved = deepcopy(workspace)
            resolved["panels"] = resolve_source_records(workspace["panels"], base)
            return resolved
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Open workspace", str(exc))
            return None

    def _validate_workspace(self, workspace: dict[str, object]) -> bool:
        if not isinstance(workspace, dict):
            return False
        sources = workspace.get("panels")
        if not isinstance(sources, list) or len(sources) < 2:
            return False
        sources = sources[:4]
        if any(not isinstance(item, dict) or not isinstance(item.get("path"), str) or
               not item["path"] for item in sources):
            QMessageBox.warning(self, "Open workspace", "This workspace has invalid panel data.")
            return False
        if has_relative_sources(sources):
            QMessageBox.warning(self, "Open workspace", "Locate the original folder before opening relative sources.")
            return False
        try:
            missing = [item["path"] for item in sources if not Path(item["path"]).exists()]
        except (OSError, ValueError):
            QMessageBox.warning(self, "Open workspace", "This workspace has an invalid source path.")
            return False
        if missing:
            QMessageBox.warning(self, "Open workspace", "These paths no longer exist:\n\n" + "\n".join(missing))
            return False
        return True

    def _load_workspace(self, workspace: dict[str, object]) -> None:
        workspace = self._resolve_workspace_sources(workspace)
        if workspace is None or not self._validate_workspace(workspace):
            return
        workspace = deepcopy(workspace)
        sources = workspace["panels"][:4]
        if self.merge_mode:
            self.merge_mode_button.setChecked(False)
        self._loading_workspace = True
        if not self._clear_all():
            self._loading_workspace = False
            return
        self._restore_comparison_context(workspace)
        while len(self.panels) < min(len(sources), 4):
            self._add_panel()
        for index, source in enumerate(sources[:4]):
            path = Path(str(source["path"]))
            self._load_panel(index, "folder" if path.is_dir() or is_zip_source(path) else "file", path)
            self._restore_workspace_git(self.panels[index], source.get("git"))
            self.panels[index]["name"].setText(str(source.get("name", self._panel_name(index))))
            self.panels[index]["custom_name"] = bool(source.get("custom_name", False))
        if self._active_workspace_name:
            saved = next((item for item in self.settings.get("pinned_workspaces", [])
                          if isinstance(item, dict) and item.get("name") == self._active_workspace_name), None)
            if saved is not None:
                self._named_workspace_baselines[self._active_workspace_name] = deepcopy(saved)
        self._prepare_context_view()
        self._loading_workspace = False
        self._update_panel_locking()
        self._refresh_diff()
        self._restore_context_text_view()
        self._start_context_note_validation()
        self._record_recent_workspace()
        self.status.setText(f"Opened workspace: {workspace.get('name', 'Unnamed workspace')}")
        self.comparison_changed.emit()

    def _restore_workspace_git(self, panel, source_git):
        path = panel.get("path")
        if path is None:
            return
        if isinstance(source_git, dict) and path.is_dir() and {
            "repo", "kind", "name", "commit", "readonly"
        } <= source_git.keys():
            try:
                if repository_root(path) == path.resolve():
                    current = next((item for item in worktrees(path) if item.path == path.resolve()), None)
                    same_source = current is not None and (
                        (source_git.get("readonly") and current.branch is None and current.commit == source_git.get("commit"))
                        or (not source_git.get("readonly") and current.branch == source_git.get("name"))
                    )
                    if same_source:
                        source_git = dict(source_git)
                        source_git["commit"] = current.commit
                        source_git["managed"] = str(path) in self.managed_worktrees
                        self._set_git_source(panel, source_git)
            except GitError:
                pass
