"""Panel creation, editing, source loading, and file save actions."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from PyQt6.QtCore import QIODeviceBase, QSaveFile, QSize, Qt
from PyQt6.QtGui import QFont, QTextDocument
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QStackedWidget, QTreeWidget, QVBoxLayout, QWidget,
)

from d1ffler.editor_widgets import LineNumberTextEdit
from d1ffler.editor_history import utf16_length
from d1ffler.text_endings import TextRepresentationError
from d1ffler.image_compare import ImagePane, is_image_source
from d1ffler.file_content import (
    ENCODING_CHOICES, disk_fingerprint, encode_text, encoding_label,
    inspect_file_content, unloaded_file_message,
)
from d1ffler.folder_archives import is_zip_source
from d1ffler.folder_links import inspect_link
from d1ffler.shared_sources import SourceScope
from d1ffler.source_paths import absolute_source
from d1ffler.git_worktrees import GitError, repository_root, worktrees


class PanelMixin:
    def _set_panel_modified(self, panel: dict, modified: bool) -> None:
        panel["dirty"] = modified
        panel["modified"].setVisible(modified)
        self.comparison_changed.emit()

    @staticmethod
    def _set_panel_encoding(panel: dict, encoding: str, bom: bytes = b"") -> None:
        panel["encoding"] = encoding
        panel["bom"] = bom
        panel["encoding_display"].setText(encoding_label(encoding, bom))

    def _choose_encoding(self, title: str, panel: dict) -> tuple[str, bytes] | None:
        labels = [item[0] for item in ENCODING_CHOICES]
        current = encoding_label(panel["encoding"], panel["bom"])
        selection, accepted = QInputDialog.getItem(
            self, title, "Text encoding:", labels,
            labels.index(current) if current in labels else 0, False)
        if not accepted:
            return None
        return next((codec, bom) for label, codec, bom in ENCODING_CHOICES if label == selection)

    def _reopen_with_encoding(self, index: int) -> None:
        panel = self.panels[index]
        path = panel.get("editing_path")
        if path is None:
            return
        selected = self._choose_encoding("Reopen with encoding", panel)
        if selected is None:
            return
        try:
            content = inspect_file_content(path, preview_limit=True, forced_encoding=selected)
            if content.text is None:
                raise ValueError("This file is too large for the editor preview.")
        except (OSError, UnicodeError, ValueError) as exc:
            QMessageBox.warning(self, "Reopen with encoding", f"Could not decode {path} as {encoding_label(*selected)}:\n{exc}")
            return
        if panel.get("dirty"):
            dialog = QDialog(self)
            dialog.setWindowTitle(f"{encoding_label(*selected)} disk preview — {path.name}")
            dialog.resize(850, 600)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("Local edits remain in the panel. Save them as a copy or discard them before reopening."))
            preview = QPlainTextEdit()
            preview.setReadOnly(True)
            preview.setPlainText(content.text)
            layout.addWidget(preview)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            dialog.exec()
            return
        self._loading_panel = True
        panel["editor"].setPlainText(content.text)
        self._set_panel_encoding(panel, content.encoding or selected[0], content.bom)
        panel["binary"] = False
        panel["too_large"] = False
        panel["editor"].setReadOnly(bool(panel.get("git") and panel["git"]["readonly"]))
        self._loading_panel = False
        self._set_panel_clean(panel, path, content.fingerprint)
        self._update_panel_locking()
        self._refresh_diff()
        self.status.setText(f"Reopened {path.name} as {encoding_label(panel['encoding'], panel['bom'])}")

    def _encoding_error_choice(self, path: Path, encoding: str, bom: bytes, exc: UnicodeEncodeError) -> str:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Character cannot be saved")
        dialog.setText(f"{path} contains a character that {encoding_label(encoding, bom)} cannot represent (at position {exc.start + 1}).")
        convert = dialog.addButton("Convert to UTF-8", QMessageBox.ButtonRole.AcceptRole)
        copy = dialog.addButton("Save as UTF-8 copy…", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        return "convert" if dialog.clickedButton() is convert else "copy" if dialog.clickedButton() is copy else "cancel"

    def _set_panel_clean(self, panel: dict, editing_path: Path | None = None,
                         fingerprint: str | None = None) -> None:
        panel["editing_path"] = absolute_source(editing_path) if editing_path is not None else None
        panel.pop("unresolved_source", None)
        panel["disk_fingerprint"] = fingerprint
        panel["disk_changed"] = False
        self._update_disk_notice(panel)
        panel["saved_text"] = self._panel_text(panel)
        panel["saved_text_with_endings"] = self._panel_text_with_line_endings(panel)
        panel["recovery_pending"] = False
        self._set_panel_modified(panel, False)
        self._sync_disk_watches()
        self._schedule_recovery()

    def _update_disk_notice(self, panel: dict) -> None:
        changed = bool(panel.get("disk_changed"))
        panel["disk_notice"].setVisible(changed)
        if changed:
            path = panel.get("editing_path")
            panel["disk_message"].setText(f"Disk version changed: {path}")
            panel["disk_compare"].setVisible(bool(panel.get("dirty")))
            panel["disk_copy"].setVisible(bool(panel.get("dirty")))
            panel["disk_reload"].setText("Discard local edits and reload" if panel.get("dirty") else "Reload")

    def _update_panel_modified_state(self, panel: dict) -> None:
        editing_path = panel.get("editing_path")
        is_merge_result = self.merge_mode and len(self.panels) == 4 and panel is self.panels[3]
        self._set_panel_modified(
            panel,
            (editing_path is not None or is_merge_result)
            and (self._panel_text_with_line_endings(panel) != panel["saved_text_with_endings"]
                 if "saved_text_with_endings" in panel else self._panel_text(panel) != panel.get("saved_text", "")),
        )
        self._update_disk_notice(panel)

    def _sync_disk_watches(self) -> None:
        paths = set()
        for panel in self.panels:
            path = panel.get("editing_path")
            if path is not None:
                if path.is_file():
                    paths.add(str(path))
                if path.parent.is_dir():
                    paths.add(str(path.parent))
            if panel.get("kind") == "folder" and panel.get("path") is not None:
                paths.add(str(panel["path"]))
        current = set(self._disk_watcher.files() + self._disk_watcher.directories())
        if current - paths:
            self._disk_watcher.removePaths(list(current - paths))
        if paths - current:
            self._disk_watcher.addPaths(list(paths - current))

    def _watched_path_changed(self, changed_path: str) -> None:
        if any(panel.get("kind") == "folder" and
               str(panel.get("path")) == changed_path for panel in self.panels):
            self._mark_folder_stale()
        self._disk_check_timer.start(150)

    def _mark_folder_stale(self) -> None:
        if self._comparison_mode() != "folder" or self._folder_job_key is None:
            return
        self._folder_stale = True
        if "stale" not in self.status.text().lower():
            self.status.setText(self.status.text() + " · stale; Refresh to rescan")

    def _check_external_changes(self) -> None:
        self._sync_disk_watches()  # QFileSystemWatcher drops a file watch after atomic replacement.
        for panel in self.panels:
            path = panel.get("editing_path")
            if path is None:
                continue
            try:
                changed = disk_fingerprint(path) != panel.get("disk_fingerprint")
            except OSError:
                changed = True
            panel["disk_changed"] = changed
            self._update_disk_notice(panel)

    def _compare_disk_version(self, widget: QWidget) -> None:
        panel = self.panels[self._panel_index_for_widget(widget)]
        path = panel.get("editing_path")
        if path is None:
            return
        try:
            disk = inspect_file_content(path, preview_limit=True)
            disk_text = disk.text if disk.text is not None else unloaded_file_message(path, disk)
        except OSError as exc:
            disk_text = f"Disk version unavailable: {exc}"
        diff = "".join(difflib.unified_diff(
            disk_text.splitlines(keepends=True), self._panel_text(panel).splitlines(keepends=True),
            fromfile=f"Disk: {path}", tofile="Local edits", n=3))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Compare disk version — {path.name}")
        dialog.resize(900, 620)
        layout = QVBoxLayout(dialog)
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText(diff or "No text differences. The disk file may have changed in encoding or bytes.")
        layout.addWidget(preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _reload_panel(self, widget: QWidget, *, discard_confirmed: bool = False) -> None:
        panel = self.panels[self._panel_index_for_widget(widget)]
        path = panel.get("editing_path") or panel.get("preview_path")
        if path is None:
            return
        if panel.get("editing_path") is None and panel["kind"] == "folder" and inspect_link(path, panel["path"]) is not None:
            self._mark_folder_stale()
            return
        if panel.get("dirty") and not discard_confirmed:
            choice = QMessageBox.warning(
                self, "Discard local edits", f"Discard local edits to {path} and reload its disk version?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Discard:
                return
        try:
            content = inspect_file_content(path, preview_limit=True)
            text = (content.text if content.text is not None and not is_image_source(path)
                    else unloaded_file_message(path, content))
            fingerprint = content.fingerprint
            panel["binary"] = content.text is None or is_image_source(path)
            panel["too_large"] = content.too_large
        except FileNotFoundError:
            text, fingerprint = f"File missing on disk: {path}\nReload after it reappears.", None
            panel["binary"] = True
            panel["too_large"] = False
        except OSError as exc:
            QMessageBox.warning(self, "Reload file", f"Could not reload {path}:\n{exc}")
            return
        self._loading_panel = True
        panel["editor"].setPlainText(text)
        if fingerprint is not None:
            self._set_panel_encoding(panel, content.encoding or "utf-8", content.bom)
            if content.text is None:
                panel["encoding_display"].setText("Large file" if content.too_large else "Binary")
        else:
            self._set_panel_encoding(panel, "utf-8")
        panel["editor"].setReadOnly(panel["binary"] or bool(panel.get("git") and panel["git"]["readonly"]))
        self._loading_panel = False
        self._set_panel_clean(panel, path, fingerprint)
        panel["preview_path"] = None
        panel["image_source"] = path
        self._update_panel_locking()
        self._refresh_diff()
        if self.merge_mode and self._panel_index_for_widget(widget) != 3:
            self._source_merge_changed()
        if panel["kind"] == "folder":
            self._mark_folder_stale()
        message = f"Reloaded: {path.name}" if fingerprint is not None else f"Missing on disk: {path}"
        self.status.setText(message + (" · folder results stale; Refresh to rescan" if self._folder_stale else ""))

    def _add_panel(self) -> None:
        if len(self.panels) >= 4:
            return
        index = len(self.panels)
        panel = QFrame()
        panel.setObjectName("comparisonPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 8, 10, 10)
        header = QHBoxLayout()
        name = QLabel(self._panel_name(index))
        name.setObjectName("panelName")
        modified = QLabel("Modified")
        modified.setObjectName("modifiedIndicator")
        modified.setVisible(False)
        rename = QPushButton("Rename Panel")
        rename.setObjectName("smallButton")
        rename.clicked.connect(lambda _checked=False, p=panel: self._rename_panel_widget(p))
        rename.setEnabled(not self.merge_mode)
        header.addWidget(name)
        header.addWidget(modified)
        header.addStretch()
        remove = None
        if index >= 2:
            remove = QPushButton("×")
            remove.setObjectName("removePanelButton")
            remove.setToolTip("Remove panel")
            remove.setFixedWidth(28)
            remove.clicked.connect(lambda _checked=False, p=panel: self._remove_panel(p))
            remove.setVisible(not self.merge_mode)
            header.addWidget(remove)
        header.addWidget(rename)
        panel_layout.addLayout(header)
        editor = LineNumberTextEdit()
        editor.setFont(QFont("Cascadia Mono", 10))
        # Wrapped visual lines cannot be kept in lockstep across panels. A long
        # line is therefore scrolled horizontally, like most code diff tools.
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor.set_line_number_theme(self.settings["theme"])
        self._set_editor_special_characters(editor)
        editor.setPlaceholderText("Paste or type text here…\nRight-click to add a file or folder.")
        editor.drop_paths_callback = lambda paths, p=panel: self._drop_paths_to_panel(self._panel_index_for_widget(p), paths)
        editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        editor.customContextMenuRequested.connect(lambda pos, p=panel: self._panel_menu_widget(p, pos))
        editor.textChanged.connect(lambda p=panel: self._panel_text_changed_widget(p))
        editor.textChanged.connect(self._schedule_search_update)
        editor.historyChanged.connect(self._update_undo_actions)
        editor.cursorPositionChanged.connect(lambda p=panel: self._set_active_panel_widget(p))
        editor.cursorPositionChanged.connect(self._schedule_recovery)
        editor.horizontalScrollBar().valueChanged.connect(self._schedule_recovery)
        editor.verticalScrollBar().valueChanged.connect(self._schedule_recovery)
        editor.verticalScrollBar().valueChanged.connect(
            lambda value, p=panel: self._sync_panel_scroll(p, value)
        )
        view_stack = QStackedWidget()
        view_stack.addWidget(editor)
        folder_tree = QTreeWidget()
        folder_tree.setHeaderLabels(["Name", "Status"])
        folder_tree.setColumnWidth(0, 260)
        folder_tree.setIconSize(QSize(27, 27))
        folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        folder_tree.customContextMenuRequested.connect(lambda pos, p=panel: self._panel_menu_tree(p, pos))
        folder_tree.itemDoubleClicked.connect(lambda item, _column, p=panel: self._open_folder_item(p, item))
        folder_tree.itemExpanded.connect(lambda item, p=panel: self._folder_expansion_changed(p, item, True))
        folder_tree.itemCollapsed.connect(lambda item, p=panel: self._folder_expansion_changed(p, item, False))
        folder_tree.itemSelectionChanged.connect(lambda p=panel: self._folder_selection_changed(p))
        folder_tree.verticalScrollBar().valueChanged.connect(self._schedule_recovery)
        folder_tree.verticalScrollBar().sliderPressed.connect(
            lambda p=panel: self._stop_folder_scroll_restore(p))
        view_stack.addWidget(folder_tree)
        image_pane = ImagePane(lambda step: self._change_image_zoom(step),
                               lambda _value, p=panel: self._sync_image_scroll(p))
        image_pane.view.drop_paths_callback = lambda paths, p=panel: self._drop_paths_to_panel(self._panel_index_for_widget(p), paths)
        image_pane.view.activate_callback = lambda p=panel: self._set_active_panel_widget(p)
        image_pane.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        image_pane.view.customContextMenuRequested.connect(
            lambda pos, p=panel, view=image_pane.view: self._panel_menu_at(self._panel_index_for_widget(p), view, pos))
        view_stack.addWidget(image_pane)
        panel_layout.addWidget(view_stack, 1)
        source = QLabel("")
        source.setWordWrap(True)
        source.setVisible(False)
        panel_layout.insertWidget(1, source)
        encoding_display = QLabel("UTF-8")
        encoding_display.setToolTip("Encoding used when saving this panel")
        header.insertWidget(2, encoding_display)
        disk_notice = QWidget()
        disk_row = QHBoxLayout(disk_notice)
        disk_row.setContentsMargins(0, 0, 0, 0)
        disk_message = QLabel()
        disk_message.setWordWrap(True)
        disk_row.addWidget(disk_message, 1)
        disk_compare = QPushButton("Compare disk version")
        disk_copy = QPushButton("Save as copy")
        disk_reload = QPushButton("Reload")
        for button in (disk_compare, disk_copy, disk_reload):
            button.setObjectName("smallButton")
            disk_row.addWidget(button)
        disk_compare.clicked.connect(lambda _checked=False, p=panel: self._compare_disk_version(p))
        disk_copy.clicked.connect(lambda _checked=False, p=panel: self._save_panel_file(self._panel_index_for_widget(p), save_as=True, copy_only=True))
        disk_reload.clicked.connect(lambda _checked=False, p=panel: self._reload_panel(p))
        panel_layout.insertWidget(2, disk_notice)
        disk_notice.hide()
        metadata_display = QLabel()
        metadata_display.setWordWrap(True)
        metadata_display.setToolTip("Selected file metadata; comparison-only differences never change folder sync actions")
        panel_layout.insertWidget(3, metadata_display)
        metadata_display.hide()
        self.panels.append({"widget": panel, "editor": editor, "image_pane": image_pane, "image_source": None, "metadata_display": metadata_display, "name": name, "source": source, "git": None, "modified": modified, "remove_button": remove, "rename_button": rename, "kind": None, "path": None, "editing_path": None, "encoding": "utf-8", "bom": b"", "encoding_display": encoding_display, "disk_fingerprint": None, "disk_changed": False, "disk_notice": disk_notice, "disk_message": disk_message, "disk_compare": disk_compare, "disk_copy": disk_copy, "disk_reload": disk_reload, "saved_text": "", "dirty": False, "loading": False, "custom_name": False, "binary": False, "too_large": False, "view_stack": view_stack, "tree": folder_tree})
        self._folder_expanded_paths.append(set())
        self._folder_move_group_expanded.append(True)
        self.panel_area.insertWidget(index, panel, 1)
        self.add_panel_button.setEnabled(not self.merge_mode and len(self.panels) < 4)
        self._refresh_diff()

    def _drop_paths_to_panel(self, start_index: int, paths: list[Path]) -> None:
        valid = [path for path in paths if path.is_file() or path.is_dir()]
        if not valid:
            return
        kind = "folder" if valid[0].is_dir() or is_zip_source(valid[0]) else "file"
        if any(("folder" if path.is_dir() or is_zip_source(path) else "file") != kind for path in valid):
            QMessageBox.information(self, "Drop comparison sources", "Drop folders and ZIP archives together, or text files together.")
            return
        targets = [start_index] + [index for index, panel in enumerate(self.panels) if index != start_index and panel["kind"] is None]
        for target, path in zip(targets, valid):
            self._load_panel(target, kind, path)
        self._record_recent_workspace()

    def open_paths(self, paths: list[Path]) -> None:
        """Open one to four command-line paths using the same validation as a drop."""
        if not paths or any(not (path.is_file() or path.is_dir()) for path in paths):
            self.status.setText("Command-line paths must exist")
            return
        while len(self.panels) < min(len(paths), 4):
            self._add_panel()
        self._drop_paths_to_panel(0, paths[:4])

    def _rename_panel(self, index: int) -> None:
        if self.merge_mode:
            return
        panel = self.panels[index]
        value, accepted = QInputDialog.getText(self, "Rename panel", "Panel name:", text=panel["name"].text())
        if accepted and value.strip():
            panel["name"].setText(value.strip())
            panel["custom_name"] = True
            self._refresh_diff()

    def _panel_index_for_widget(self, widget: QWidget) -> int:
        return next(index for index, panel in enumerate(self.panels) if panel["widget"] is widget)

    def _rename_panel_widget(self, widget: QWidget) -> None:
        self._rename_panel(self._panel_index_for_widget(widget))

    def _remove_panel(self, widget: QWidget) -> None:
        index = self._panel_index_for_widget(widget)
        if index < 2 or self.merge_mode:
            return
        if not self._confirm_replace_panel(index):
            return
        self._show_folder_trees()
        panel = self.panels.pop(index)
        self._folder_expanded_paths.pop(index)
        self._folder_move_group_expanded.pop(index)
        panel["widget"].deleteLater()
        for new_index, remaining in enumerate(self.panels):
            if not remaining["custom_name"]:
                remaining["name"].setText(self._panel_name(new_index))
        self.add_panel_button.setEnabled(len(self.panels) < 4)
        self._sync_disk_watches()
        self._refresh_diff()
        self._schedule_recovery()

    def _panel_menu_widget(self, widget: QWidget, position) -> None:
        self._panel_menu(self._panel_index_for_widget(widget), position)

    def _panel_menu_tree(self, widget: QWidget, position) -> None:
        index = self._panel_index_for_widget(widget)
        self._panel_menu_at(index, self.panels[index]["tree"], position)

    def _panel_text_changed_widget(self, widget: QWidget) -> None:
        self._panel_text_changed(self._panel_index_for_widget(widget))

    def _set_active_panel_widget(self, widget: QWidget) -> None:
        """Remember the editor touched by the user, even if its caret is later hidden."""
        if self._rendering_diff:
            return
        self._active_panel_index = self._panel_index_for_widget(widget)
        self._update_save_action()
        self._schedule_search_update()

    def _sync_panel_scroll(self, source: QWidget, value: int) -> None:
        """Keep baseline-aligned panes on the same visual hunk while scrolling."""
        if self._syncing_panel_scroll:
            return
        self._syncing_panel_scroll = True
        try:
            for panel in self.panels:
                if panel["widget"] is not source:
                    panel["editor"].verticalScrollBar().setValue(value)
        finally:
            self._syncing_panel_scroll = False

    def _panel_menu(self, index: int, position) -> None:
        self._panel_menu_at(index, self.panels[index]["editor"], position)

    def _panel_menu_at(self, index: int, source_widget: QWidget, position) -> None:
        panel = self.panels[index]
        menu = QMenu(self)
        if source_widget is panel["editor"]:
            editor = panel["editor"]
            undo = menu.addAction("Undo\tCtrl+Z")
            redo = menu.addAction("Redo\tCtrl+Y")
            undo.setEnabled(not editor.isReadOnly() and editor.history.document.isUndoAvailable())
            redo.setEnabled(not editor.isReadOnly() and editor.history.document.isRedoAvailable())
            menu.addSeparator()
        else:
            undo = redo = None
        add_file = menu.addAction("Add file…")
        add_folder = menu.addAction("Add folder…")
        add_archive = menu.addAction("Add ZIP as folder…")
        add_archive.setToolTip("Compare ZIP contents as a read-only folder with another ZIP or a folder.")
        choose_revision = None
        create_branch_action = None
        create_commit_action = None
        manage_worktrees = None
        sync_plan = None
        sync_reports = None
        restore_reports = None
        review_path_action = None
        review_hunk_action = None
        review_relative = None
        sync_copy_actions = {}
        if self.managed_worktrees:
            manage_worktrees = menu.addAction("Manage D1ffler worktrees…")
        if self.sync_reports:
            sync_reports = menu.addAction("View sync reports…")
        if self.restore_reports:
            restore_reports = menu.addAction("View restore reports…")
        if not self.merge_mode and self._comparison_mode() == "folder":
            if index > 0 and self._baseline_repository() is not None:
                choose_revision = menu.addAction("Choose revision…")
            if panel.get("git") and panel["git"]["readonly"]:
                create_branch_action = menu.addAction("Create branch from this revision…")
            if panel.get("git") and not panel["git"]["readonly"]:
                create_commit_action = menu.addAction("Create commit…")
            if source_widget is panel["tree"] and sum(item["kind"] == "folder" for item in self.panels) >= 2:
                sync_plan = menu.addAction("Plan folder sync…")
                if any(is_zip_source(item["path"]) for item in self.panels if item["kind"] == "folder"):
                    sync_plan.setEnabled(False)
                    sync_plan.setToolTip("ZIP archives are read-only; folder sync with archives is unavailable.")
                selected = panel["tree"].itemAt(position)
                if selected is not None and selected.data(0, Qt.ItemDataRole.UserRole + 1) in {"file", "folder"}:
                    relative = selected.data(0, Qt.ItemDataRole.UserRole)
                    kind = selected.data(0, Qt.ItemDataRole.UserRole + 1)
                    if kind == "file" and not any(
                            is_zip_source(item["path"]) for item in self.panels if item["kind"] == "folder"):
                        review_path_action = menu.addAction("Review this file…")
                        review_relative = relative
                    source_path = panel["path"] / Path(relative)
                    available = (source_path.is_file() if kind == "file" else source_path.is_dir()) and not any(
                        is_zip_source(item["path"]) for item in self.panels if item["kind"] == "folder")
                    for target_index, target in enumerate(self.panels):
                        if target_index == index or target["kind"] != "folder":
                            continue
                        action = menu.addAction(f"Copy {kind} to {target['name'].text()} via Sync…")
                        target_git = target.get("git")
                        action.setEnabled(available and not (target_git and target_git["readonly"]))
                        sync_copy_actions[action] = (target_index, relative)
        menu.addSeparator()
        rename = menu.addAction("Rename panel…")
        rename.setEnabled(not self.merge_mode)
        save_file = menu.addAction("Save file")
        save_as = menu.addAction("Save file as…")
        save_encoding = menu.addAction("Save with encoding…")
        reopen_encoding = menu.addAction("Reopen with encoding…")
        can_write = (not self.merge_mode or index == 3) and not (
            panel.get("git") and panel["git"]["readonly"]
        )
        save_as.setEnabled(can_write and (
            panel["kind"] != "folder" or panel.get("editing_path") is not None
        ))
        save_encoding.setEnabled(can_write and not panel["binary"] and
                                 (panel["kind"] != "folder" or panel.get("editing_path") is not None))
        reopen_encoding.setEnabled(panel.get("editing_path") is not None and not panel.get("too_large"))
        can_save_new_file = not panel["binary"] and panel["kind"] != "folder" and bool(self._panel_text(panel))
        save_file.setEnabled(can_write and (
            (panel.get("editing_path") is not None and panel.get("dirty", False)) or can_save_new_file
        ))
        clear = menu.addAction("Clear panel")
        apply_actions = {}
        hunk_actions = {}
        current_hunk = self._hunk_at_editor(index) if source_widget is panel["editor"] else None
        if current_hunk is not None and not self.merge_mode:
            menu.addSeparator()
            review_hunk_action = menu.addAction("Review this hunk…")
            for target_index, target_panel in enumerate(self.panels):
                if target_index == index:
                    continue
                action = menu.addAction(f"Apply current hunk to {target_panel['name'].text()}")
                action.setEnabled(not target_panel["editor"].isReadOnly())
                hunk_actions[action] = target_index
        if not self.merge_mode and source_widget is panel["editor"] and panel["editor"].textCursor().hasSelection():
            menu.addSeparator()
            for target_index, target_panel in enumerate(self.panels):
                if target_index == index:
                    continue
                action = menu.addAction(f"Apply to {target_panel['name'].text()}")
                action.setEnabled(not target_panel["editor"].isReadOnly())
                apply_actions[action] = target_index
        if self._comparison_mode() == "folder":
            add_file.setVisible(False)
        elif self._comparison_mode() == "file":
            add_folder.setVisible(False)
            add_archive.setVisible(False)
        chosen = menu.exec(source_widget.mapToGlobal(position))
        if chosen == undo and undo is not None:
            panel["editor"].undo()
        elif chosen == redo and redo is not None:
            panel["editor"].redo()
        elif chosen == add_file:
            path = QFileDialog.getOpenFileName(self, "Add file")[0]
            if path:
                self._load_panel(index, "file", Path(path))
        elif chosen == add_folder:
            path = QFileDialog.getExistingDirectory(self, "Add folder")
            if path:
                self._load_panel(index, "folder", Path(path))
        elif chosen == add_archive:
            path = QFileDialog.getOpenFileName(self, "Add ZIP as folder", "", "ZIP archives (*.zip)")[0]
            if path:
                self._load_panel(index, "folder", Path(path))
        elif chosen == choose_revision and choose_revision is not None:
            self._choose_revision(index)
        elif chosen == create_branch_action and create_branch_action is not None:
            self._create_branch_from_panel(index)
        elif chosen == create_commit_action and create_commit_action is not None:
            self._create_commit_from_panel(index)
        elif chosen == manage_worktrees and manage_worktrees is not None:
            self._manage_worktrees()
        elif chosen == sync_plan and sync_plan is not None:
            self._open_sync_plan(index)
        elif chosen == sync_reports and sync_reports is not None:
            self._show_sync_reports()
        elif chosen == restore_reports and restore_reports is not None:
            self._show_restore_reports()
        elif chosen == review_path_action and review_path_action is not None:
            self._review_file_path(review_relative)
        elif chosen == review_hunk_action and review_hunk_action is not None:
            self._review_hunk(current_hunk)
        elif chosen in sync_copy_actions:
            target_index, relative = sync_copy_actions[chosen]
            self._open_sync_plan(index, target_index, relative)
        elif chosen == rename:
            self._rename_panel(index)
        elif chosen == save_file:
            self._save_panel_file(index)
        elif chosen == save_as:
            self._save_panel_file(index, save_as=True)
        elif chosen == save_encoding:
            selected = self._choose_encoding("Save with encoding", panel)
            if selected is not None:
                self._save_panel_file(index, encoding_override=selected)
        elif chosen == reopen_encoding:
            self._reopen_with_encoding(index)
        elif chosen in hunk_actions:
            self._apply_hunk_to_panel(index, hunk_actions[chosen], current_hunk)
        elif chosen in apply_actions:
            self._apply_selection_to_panel(index, apply_actions[chosen])
        elif chosen == clear:
            self._clear_panel(index)

    def _apply_selection_to_panel(self, source_index: int, target_index: int) -> None:
        source_editor = self.panels[source_index]["editor"]
        target_editor = self.panels[target_index]["editor"]
        source_cursor = source_editor.textCursor()
        if not source_cursor.hasSelection() or target_editor.isReadOnly():
            return

        selection_start = source_cursor.selectionStart()
        selection_end = source_cursor.selectionEnd()
        source_document = source_editor.document()
        start_block = source_document.findBlock(selection_start)
        end_block = source_document.findBlock(selection_end)
        start_column = selection_start - start_block.position()
        end_column = selection_end - end_block.position()

        # Prepare the selection and any required padding without editing the view.
        # The final logical replacement enters history as a single transaction.
        target_document = QTextDocument()
        target_document.setPlainText(target_editor.toPlainText())
        missing_blocks = end_block.blockNumber() - target_document.blockCount() + 1
        if missing_blocks > 0:
            target_document.setPlainText(target_document.toPlainText() + "\n" * missing_blocks)

        target_start_block = target_document.findBlockByNumber(start_block.blockNumber())
        target_end_block = target_document.findBlockByNumber(end_block.blockNumber())
        target_start = target_start_block.position() + min(start_column, target_start_block.length() - 1)
        target_end = target_end_block.position() + min(end_column, target_end_block.length() - 1)
        def logical_offset(document, position):
            block = document.findBlock(position)
            prefix = "".join(line + "\n" for line in document.toPlainText().split("\n")[:block.blockNumber()]
                             if line != LineNumberTextEdit.ALIGNMENT_PLACEHOLDER)
            return utf16_length(prefix) + (0 if block.text() == LineNumberTextEdit.ALIGNMENT_PLACEHOLDER
                                           else position - block.position())

        def raw_slice(editor, start, end):
            return editor.logical_text_slice(start, end)

        source_start = source_editor._logical_position(selection_start)
        source_end = source_editor._logical_position(selection_end)
        selected_text = raw_slice(source_editor, source_start, source_end)
        target_a = logical_offset(target_document, target_start)
        target_b = logical_offset(target_document, target_end)
        target_length = target_editor.history.document.characterCount() - 1
        prefix = raw_slice(target_editor, 0, min(target_a, target_length)) + "\n" * max(0, target_a - target_length)
        suffix = raw_slice(target_editor, min(target_b, target_length), target_length)
        target_editor.replace_logical_text(prefix + selected_text + suffix)
        target_cursor = target_editor.textCursor()
        target_cursor.setPosition(target_editor._display_position(target_a + utf16_length(selected_text.replace("\r\n", "\n").replace("\r", "\n"))))
        target_editor.setTextCursor(target_cursor)
        target_editor.setFocus()
        self.status.setText(f"Applied selection to {self.panels[target_index]['name'].text()}")

    def _hunk_at_editor(self, panel_index: int) -> tuple[int, int] | None:
        row = self.panels[panel_index]["editor"].textCursor().blockNumber()
        return next((hunk for hunk in self._hunks if hunk[0] <= row <= hunk[1]), None)

    def _hunk_line_span(self, panel_index: int, hunk: tuple[int, int]) -> tuple[int, int]:
        """Return the raw line range represented by an aligned visual hunk."""
        start_row, end_row = hunk
        values = [
            self._aligned_rows[row][panel_index]
            for row in range(start_row, end_row + 1)
            if self._aligned_rows[row][panel_index] is not None
        ]
        if values:
            return min(values), max(values) + 1
        preceding = [
            self._aligned_rows[row][panel_index]
            for row in range(start_row - 1, -1, -1)
            if self._aligned_rows[row][panel_index] is not None
        ]
        insertion_point = preceding[0] + 1 if preceding else 0
        return insertion_point, insertion_point

    def _apply_hunk_to_panel(
        self, source_index: int, target_index: int, hunk: tuple[int, int] | None
    ) -> None:
        if hunk is None or (self._comparison_mode() == "folder" and self.folder_back_button.isHidden()):
            return
        source, target = self.panels[source_index], self.panels[target_index]
        if target["editor"].isReadOnly():
            return
        source_start, source_end = self._hunk_line_span(source_index, hunk)
        target_start, target_end = self._hunk_line_span(target_index, hunk)
        source_lines = self._panel_text_with_line_endings(source).splitlines(keepends=True)
        target_lines = self._panel_text_with_line_endings(target).splitlines(keepends=True)
        replacement = source_lines[source_start:source_end]
        target["editor"].replace_logical_text("".join(target_lines[:target_start] + replacement + target_lines[target_end:]))
        self.status.setText(f"Applied hunk to {target['name'].text()} · unsaved")

    def _comparison_mode(self) -> str | None:
        kinds = {panel["kind"] for panel in self.panels if panel["kind"]}
        if "folder" in kinds:
            return "folder"
        if kinds:
            return "file"
        return None

    def _show_folder_trees(self) -> None:
        self.folder_back_button.setVisible(False)
        self.folder_triage_bar.setVisible(self._comparison_mode() == "folder")
        for panel in self.panels:
            if panel["kind"] == "folder":
                panel["view_stack"].setCurrentIndex(1)
                panel["editor"].setReadOnly(True)
        self._update_save_action()

    def _load_panel(self, index: int, kind: str, path: Path, confirm: bool = True) -> None:
        path = absolute_source(path)
        if kind == "file" and is_zip_source(path):
            kind = "folder"
        if kind == "folder" and not (path.is_dir() or is_zip_source(path)):
            QMessageBox.warning(self, "Add panel source", f"Choose a folder or ZIP archive: {path}")
            return
        if self.merge_mode and (kind == "folder" or index == 3):
            QMessageBox.information(self, "Three-way merge", "Load text files into Base, Local, or Incoming.")
            return
        if self._comparison_mode() == "folder" and kind != "folder":
            QMessageBox.information(self, "Folder comparison", "All panels are locked to folder comparison.")
            return
        if self._comparison_mode() == "file" and kind == "folder":
            QMessageBox.information(self, "File comparison", "All panels are locked to file/text comparison.")
            return
        if confirm and not self._confirm_replace_panel(index):
            return
        if kind == "folder":
            self._show_folder_trees()
        panel = self.panels[index]
        try:
            from d1ffler.local_data import protect_local_sources
            protect_local_sources(self.settings_path.parent, [path])
            if kind == "file":
                file_content = inspect_file_content(path, preview_limit=True)
                content = (file_content.text if file_content.text is not None and not is_image_source(path)
                           else unloaded_file_message(path, file_content))
            else:
                source_type = "ZIP archive" if is_zip_source(path) else "Folder"
                content = f"{source_type}: {path}\n\nRight-click another panel to add a folder or ZIP archive."
        except OSError as exc:
            QMessageBox.warning(self, "Add panel source", str(exc))
            return
        self._loading_panel = True
        panel["kind"], panel["path"] = kind, path
        panel["preview_path"] = None
        self._set_git_source(panel, None)
        if kind == "file":
            self._set_panel_encoding(panel, file_content.encoding or "utf-8", file_content.bom)
            if file_content.text is None:
                panel["encoding_display"].setText("Large file" if file_content.too_large else "Binary")
        else:
            self._set_panel_encoding(panel, "utf-8")
        panel["binary"] = kind == "file" and (file_content.text is None or is_image_source(path))
        panel["image_source"] = path if kind == "file" else None
        panel["metadata_display"].hide()
        panel["too_large"] = kind == "file" and file_content.too_large
        panel["editor"].setPlainText(content)
        panel["editor"].setReadOnly(kind != "file" or panel["binary"])
        panel["editor"].setPlaceholderText("")
        panel["view_stack"].setCurrentIndex(0 if kind == "file" else 1)
        self._loading_panel = False
        self._set_panel_clean(panel, path if kind == "file" else None,
                              file_content.fingerprint if kind == "file" else None)
        self._sync_disk_watches()
        if kind == "folder" and path.is_dir():
            try:
                root = repository_root(path)
                if root == path.resolve():
                    current = next((item for item in worktrees(root) if item.path == root), None)
                    if current:
                        self._set_git_source(panel, {
                            "repo": str(root), "kind": "branch" if current.branch else "commit",
                            "name": current.branch or current.commit, "commit": current.commit,
                            "readonly": False, "managed": str(root) in self.managed_worktrees,
                        })
            except GitError:
                pass
        self._update_panel_locking()
        self._refresh_diff()
        self._record_recent_workspace()
        if self.merge_mode:
            self._source_merge_changed()

    def _clear_panel(self, index: int) -> None:
        if self.merge_mode and index == 3:
            self.status.setText("Use Rebuild merge to reset Result")
            return
        if not self._confirm_replace_panel(index):
            return
        if self._comparison_mode() == "folder":
            self._show_folder_trees()
        panel = self.panels[index]
        self._loading_panel = True
        panel["kind"], panel["path"], panel["binary"] = None, None, False
        panel["preview_path"] = None
        panel["image_source"] = None
        panel["metadata_display"].hide()
        panel["too_large"] = False
        self._set_panel_encoding(panel, "utf-8")
        self._set_git_source(panel, None)
        panel["editor"].clear()
        self._set_panel_clean(panel)
        panel["editor"].setReadOnly(False)
        panel["editor"].setPlaceholderText("Paste or type text here…\nRight-click to add a file or folder.")
        panel["view_stack"].setCurrentIndex(0)
        self._loading_panel = False
        self._update_panel_locking()
        self._refresh_diff()
        if self.merge_mode:
            self._source_merge_changed()
        self._schedule_recovery()

    def _active_save_target(self) -> tuple[int, str]:
        """Select the main Save target and explain why its current view cannot be saved."""
        index = min(3 if self.merge_mode else self._active_panel_index, len(self.panels) - 1)
        if self.merge_mode and len(self.panels) != 4:
            return index, "Preparing merge Result"
        panel = self.panels[index]
        if self._comparison_mode() == "folder" and (
                panel["kind"] != "folder" or panel["view_stack"].currentIndex() == 1):
            return index, "Open a folder file in the active panel before saving it"
        if panel["kind"] == "folder" and is_zip_source(panel["path"]):
            return index, "ZIP archive entries are read-only"
        if panel.get("git") and panel["git"]["readonly"]:
            return index, "Read-only revision: create a branch before saving"
        if panel.get("too_large"):
            return index, "Large file previews are read-only"
        if panel.get("binary"):
            return index, "Binary and image inputs are read-only"
        if panel["kind"] == "folder" and panel.get("editing_path") is None:
            return index, "This folder-file preview is read-only; no editable file is open"
        if panel["editor"].isReadOnly() or panel["view_stack"].currentIndex() != 0:
            return index, "This preview is read-only"
        return index, ""

    def _update_save_action(self) -> None:
        _index, reason = self._active_save_target()
        self.save_button.setEnabled(not reason)
        description = "Save Result (Ctrl+S)" if self.merge_mode else "Save active panel (Ctrl+S)"
        self.save_button.setToolTip(reason or description)
        self._update_undo_actions()

    def _update_undo_actions(self):
        index = self._active_panel_index
        editor = self.panels[index]["editor"] if 0 <= index < len(self.panels) else None
        editable = editor is not None and not editor.isReadOnly() and self.panels[index]["view_stack"].currentIndex() == 0
        self.undo_button.setEnabled(editable and editor.history.document.isUndoAvailable())
        self.redo_button.setEnabled(editable and editor.history.document.isRedoAvailable())

    def _undo_active_panel(self, redo=False):
        index = self._active_panel_index
        if 0 <= index < len(self.panels):
            editor = self.panels[index]["editor"]
            if self.panels[index]["view_stack"].currentIndex() == 0 and not editor.isReadOnly():
                (editor.redo if redo else editor.undo)()
                editor.setFocus()

    def _save_active_panel(self) -> None:
        index, reason = self._active_save_target()
        if reason:
            self.status.setText(reason)
            return
        self._save_panel_file(index)

    def _save_all_dirty(self) -> None:
        dirty = [
            (index, panel) for index, panel in enumerate(self.panels)
            if panel.get("dirty") and panel.get("editing_path") is not None
            and (not self.merge_mode or index == 3)
        ]
        if not dirty:
            self.status.setText("No modified files to save")
            return
        names = "\n".join(f"• {panel['editing_path']}" for _index, panel in dirty)
        choice = QMessageBox.question(
            self, "Save all modified files", f"Save changes to these files?\n\n{names}",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice != QMessageBox.StandardButton.Save:
            return
        for index, _panel in dirty:
            if not self._save_panel_file(index):
                return
        self.status.setText(f"Saved {len(dirty)} files")

    def _save_panel_file(self, index: int, show_error: bool = True, save_as: bool = False,
                         copy_only: bool = False,
                         encoding_override: tuple[str, bytes] | None = None) -> bool:
        if self.panels[index].get("git") and self.panels[index]["git"]["readonly"]:
            if show_error:
                QMessageBox.information(self, "Read-only revision", "Create a branch from this revision before editing or saving it.")
            return False
        if self.merge_mode and index != 3:
            if show_error:
                QMessageBox.information(self, "Three-way merge", "Only Result can be saved in merge mode.")
            return False
        if self.merge_mode and self.merge_stale:
            QMessageBox.information(self, "Three-way merge", "Rebuild the merge after source changes before saving Result.")
            return False
        if self.merge_mode and any(
            conflict.number not in self.merge_resolutions for conflict in self.merge_conflicts
        ):
            choice = QMessageBox.question(
                self, "Save unresolved merge",
                "Result still has unresolved conflicts. Save it with conflict markers?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Save:
                return False
        panel = self.panels[index]
        if panel.get("binary"):
            if show_error:
                QMessageBox.information(self, "Save file", "Binary inputs are read-only.")
            return False
        path = None if save_as else panel.get("editing_path")
        if path is None:
            suggested_name = (panel.get("editing_path") or Path(f"{panel['name'].text()}.txt")).name
            filename, _selected_filter = QFileDialog.getSaveFileName(
                self, "Save file as" if save_as else "Save file", suggested_name, "Text files (*.txt);;All files (*)"
            )
            if not filename:
                return False
            path = absolute_source(filename)
        if not self.shared_sources.available([SourceScope(path)], self, "Save file", self):
            return False
        original = panel.get("editing_path")
        same_target = original is not None and path.resolve() == original.resolve()
        if copy_only and same_target:
            if show_error:
                QMessageBox.warning(self, "Save as copy", "Choose a different path for the copy.")
            return False
        encoding, bom = encoding_override or (panel["encoding"], panel["bom"])
        try:
            data = encode_text(self._panel_text_with_line_endings(panel), encoding, bom)
        except TextRepresentationError as exc:
            if show_error:
                QMessageBox.critical(self, "Save file", f"Could not save {path}:\n{exc}")
            return False
        except UnicodeEncodeError as exc:
            action = self._encoding_error_choice(path, encoding, bom, exc)
            if action == "convert":
                return self._save_panel_file(index, show_error, save_as, copy_only,
                                             encoding_override=("utf-8", b""))
            if action == "copy":
                return self._save_panel_file(index, show_error, save_as=True, copy_only=True,
                                             encoding_override=("utf-8", b""))
            return False
        if (encoding, bom) != (panel["encoding"], panel["bom"]):
            choice = QMessageBox.question(
                self, "Change text encoding",
                f"Save {path} as {encoding_label(encoding, bom)} instead of "
                f"{encoding_label(panel['encoding'], panel['bom'])}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Yes:
                return False
        try:
            current_fingerprint = disk_fingerprint(path)
        except OSError as exc:
            if show_error:
                QMessageBox.critical(self, "Save file", f"Could not check {path}:\n{exc}")
            return False
        if same_target and current_fingerprint != panel.get("disk_fingerprint"):
            panel["disk_changed"] = True
            self._update_disk_notice(panel)
            choice = QMessageBox.warning(
                self, "Disk version changed",
                f"{path} changed on disk since it was opened. Overwrite that disk version with your local edits?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Yes:
                return False
        elif not same_target and current_fingerprint is not None:
            choice = QMessageBox.warning(
                self, "Replace existing file", f"Overwrite existing file {path}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Yes:
                return False
        reservation = self.shared_sources.begin([SourceScope(path)], self, "Save file", resolve=False)
        if reservation is None:
            return False
        try:
            output = QSaveFile(str(path))
            if not output.open(QIODeviceBase.OpenModeFlag.WriteOnly):
                raise OSError(output.errorString())
            try:
                if output.write(data) != len(data):
                    raise OSError(output.errorString())
                if disk_fingerprint(path) != current_fingerprint:
                    raise OSError("Disk version changed again during save; local edits were retained")
                if not output.commit():
                    raise OSError(output.errorString())
            except (OSError, RuntimeError):
                output.cancelWriting()
                raise
        except (OSError, RuntimeError) as exc:
            if show_error:
                QMessageBox.critical(self, "Save file", f"Could not save {path}:\n{exc}")
            return False
        finally:
            reservation.finish(notify=False)
        self.shared_sources.notify([SourceScope(path)], exclude=(self, index))
        is_folder_file = panel["kind"] == "folder"
        if copy_only or (is_folder_file and save_as and panel.get("git")):
            self.status.setText(f"Saved copy: {path.name}")
            return True
        if not is_folder_file:
            panel["kind"] = "file"
            panel["path"] = path
            panel["editor"].setReadOnly(False)
        self._set_panel_encoding(panel, encoding, bom)
        self._set_panel_clean(panel, path, hashlib.sha256(data).hexdigest())
        if panel.get("git"):
            self._set_git_source(panel, panel["git"])
        if not is_folder_file:
            self._update_panel_locking()
        if is_folder_file:
            self._folder_job_key = None
            self._folder_cache = {}
        self._refresh_diff()
        self._write_recovery_snapshot()
        self.status.setText(f"Saved: {path.name}")
        return True

    def _panel_text_changed(self, index: int) -> None:
        if self._loading_panel:
            return
        panel = self.panels[index]
        if self.merge_mode and index == 3:
            self.merge_resolutions = {int(key): value for key, value in panel["editor"].history.state()["metadata"].items()}
            self._merge_summary_text()
        if panel["kind"] is None and self._panel_text(panel):
            panel["kind"] = "text"
            self._update_panel_locking()
        self._update_panel_modified_state(panel)
        self._refresh_diff()
        if self.merge_mode and not self._rendering_diff:
            if index == 3:
                self.merge_touched = True
            else:
                self._source_merge_changed()
        self._schedule_recovery()

    def _source_merge_changed(self) -> None:
        if not self.merge_mode:
            return
        if getattr(self, "merge_touched", False):
            self.merge_stale = True
            self._merge_summary_text()
        else:
            self._build_merge_result()

    @staticmethod
    def _panel_text(panel: dict) -> str:
        """Return file text without visual-only rows used for hunk alignment."""
        if isinstance(panel["editor"], LineNumberTextEdit):
            return panel["editor"].logical_text()
        marker = LineNumberTextEdit.ALIGNMENT_PLACEHOLDER
        return "\n".join(line for line in panel["editor"].toPlainText().split("\n") if line != marker)

    def _panel_text_with_line_endings(self, panel: dict) -> str:
        editor = panel["editor"]
        if not isinstance(editor, LineNumberTextEdit):
            return self._panel_text(panel)
        return editor.logical_text_with_endings()

    def _update_panel_locking(self) -> None:
        mode = self._comparison_mode()
        for index, panel in enumerate(self.panels):
            if panel.get("editing_path") is not None and self.shared_sources.busy(
                    [SourceScope(panel["editing_path"])]):
                panel["editor"].setReadOnly(True)
                continue
            if self.merge_mode and index == 3:
                panel["editor"].setReadOnly(False)
                continue
            if panel["kind"] == "folder":
                panel["editor"].setReadOnly(
                    panel["view_stack"].currentIndex() != 0
                    or panel.get("editing_path") is None or panel.get("binary", False)
                )
            elif panel["kind"] == "file":
                panel["editor"].setReadOnly(panel.get("binary", False))
            else:
                panel["editor"].setReadOnly(False if mode != "folder" else True)
                if mode == "folder":
                    panel["editor"].setPlaceholderText("Add a folder here…\nRight-click to choose a folder.")
                else:
                    panel["editor"].setPlaceholderText("Paste or type text here…\nRight-click to add a file or folder.")
            if panel.get("git") and panel["git"]["readonly"]:
                panel["editor"].setReadOnly(True)
        self._update_save_action()
