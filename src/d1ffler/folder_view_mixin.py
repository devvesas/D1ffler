"""Folder tree filters, navigation, and row rendering."""

from __future__ import annotations

from d1ffler.image_compare import is_image_source

import bisect
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import QApplication, QMessageBox, QStyle, QTreeWidgetItem, QWidget

from d1ffler.comparison_models import FOLDER_STATUS_FILTERS, MoveMatch
from d1ffler.file_content import inspect_file_content, unloaded_file_message
from d1ffler.file_metadata import metadata_label
from d1ffler.folder_archives import ArchiveIssue, ArchiveMember, is_zip_source
from d1ffler.folder_links import inspect_link
from d1ffler.comparison_context import target_record


class FolderViewMixin:
    def _open_folder_content_hit(self, hit) -> bool:
        if hit.panel_index >= len(self.panels) or self.panels[hit.panel_index]["kind"] != "folder":
            return False
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
        item.setData(0, Qt.ItemDataRole.UserRole, hit.relative)
        panel = self.panels[hit.panel_index]
        self._open_folder_item(panel["widget"], item)
        if (panel["view_stack"].currentIndex() != 0 or panel.get("binary") or
                not (panel.get("editing_path") == hit.entry or panel.get("image_source") == hit.entry)):
            return False
        self._pending_folder_content_hit = hit
        if self._text_worker is None:
            self._position_folder_content_hit()
        return True

    def _position_folder_content_hit(self) -> None:
        hit = self._pending_folder_content_hit
        if hit is None or hit.panel_index >= len(self.panels):
            return
        panel = self.panels[hit.panel_index]
        if not (panel.get("editing_path") == hit.entry or panel.get("image_source") == hit.entry):
            self._pending_folder_content_hit = None
            return
        visual_line = hit.line - 1
        folder_position = [index for index, item in enumerate(self.panels)
                           if item["kind"] == "folder"].index(hit.panel_index)
        for row, mapping in enumerate(self._aligned_rows):
            if folder_position < len(mapping) and mapping[folder_position] == hit.line - 1:
                visual_line = row
                break
        editor = panel["editor"]
        block = editor.document().findBlockByNumber(visual_line)
        if not block.isValid():
            self._pending_folder_content_hit = None
            return
        prefix = block.text()[:hit.column - 1]
        offset = len(prefix.encode("utf-16-le")) // 2
        cursor = QTextCursor(editor.document())
        cursor.setPosition(block.position() + offset)
        cursor.setPosition(block.position() + offset + len(hit.match_text.encode("utf-16-le")) // 2,
                           QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()
        editor.setFocus()
        self._active_panel_index = hit.panel_index
        self._pending_folder_content_hit = None
        self.status.setText(f"Found in {panel['name'].text()}: {hit.relative}:{hit.line}")

    def _toggle_changed_files_only(self, checked: bool) -> None:
        self.show_changed_files_only = checked
        self._schedule_recovery()
        if checked and self._folder_status_filter != "all":
            self.folder_status_combo.blockSignals(True)
            self.folder_status_combo.setCurrentIndex(0)
            self.folder_status_combo.blockSignals(False)
            self._folder_status_filter = "all"
        if self._comparison_mode() == "folder":
            self._apply_folder_filters()

    def _folder_search_changed(self, value: str) -> None:
        self._schedule_recovery()
        self._folder_path_query = value.strip().replace("\\", "/").casefold()
        self._folder_search_needs_reveal = bool(self._folder_path_query)
        if self._comparison_mode() == "folder":
            self._folder_search_timer.start(120)

    def _folder_status_changed(self, _index: int) -> None:
        self._schedule_recovery()
        self._folder_status_filter = str(self.folder_status_combo.currentData() or "all")
        self._folder_search_needs_reveal = True
        if self._folder_status_filter != "all" and self.show_changed_files_only:
            self.changed_files_button.blockSignals(True)
            self.changed_files_button.setChecked(False)
            self.changed_files_button.blockSignals(False)
            self.show_changed_files_only = False
        if self._comparison_mode() == "folder":
            self._apply_folder_filters()

    def _apply_folder_filters(self) -> None:
        if self._comparison_mode() != "folder":
            return
        self._rebuild_folder_display([panel for panel in self.panels if panel["kind"] == "folder"])
        self._update_folder_counts_label()
        self._update_change_navigation()

    @staticmethod
    def _folder_row_categories(statuses: list[str] | None) -> set[str]:
        if statuses is None:
            return {"pending"}
        categories = {status for status in statuses[1:] if status in FOLDER_STATUS_FILTERS}
        if statuses[0] == "error":
            categories.add("error")
        if all(status == "identical" for status in statuses):
            categories.add("identical")
        else:
            categories.discard("identical")
        return categories

    def _folder_row_visible(self, relative: str, statuses: list[str] | None) -> bool:
        if self._folder_path_query and self._folder_path_query not in relative.casefold():
            return False
        if relative in self._folder_move_hidden_paths:
            return False
        if self.show_changed_files_only and (statuses is None or
                not (statuses[0] == "error" or any(value not in {"identical", "ignored"} for value in statuses[1:]))):
            return False
        if self._folder_status_filter == "all":
            return True
        if self._folder_status_filter == "linked":
            return relative in self._folder_links
        return self._folder_status_filter in self._folder_row_categories(statuses)

    def _folder_move_visible(self, match: MoveMatch) -> bool:
        if self._folder_path_query and not any(
            self._folder_path_query in path.casefold() for path in (match.from_path, match.to_path)
        ):
            return False
        return self._folder_status_filter in {"all", "moved"}

    def _folder_expansion_changed(self, widget: QWidget, item: QTreeWidgetItem, expanded: bool) -> None:
        if self._rebuilding_folder_trees:
            return
        index = self._panel_index_for_widget(widget)
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        self._schedule_recovery()
        if kind == "move_group":
            self._folder_move_group_expanded[index] = expanded
            return
        if kind != "folder":
            return
        relative = item.data(0, Qt.ItemDataRole.UserRole)
        if expanded:
            self._folder_expanded_paths[index].add(relative)
        else:
            self._folder_expanded_paths[index].discard(relative)

    def _folder_selection_changed(self, widget: QWidget) -> None:
        if self._syncing_folder_selection or self._rebuilding_folder_trees or self._comparison_mode() != "folder":
            return
        index = self._panel_index_for_widget(widget)
        item = self.panels[index]["tree"].currentItem()
        if item is None:
            for panel in self.panels:
                if panel["kind"] == "folder":
                    panel["metadata_display"].hide()
            return
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind not in {"file", "folder", "move"}:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole + (2 if kind == "move" else 0))
        self._select_folder_target(kind, key)
        self._show_folder_metadata(kind, key)
        self._schedule_recovery()

    def _show_folder_metadata(self, kind: str, key) -> None:
        panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        for index, panel in enumerate(panels):
            relative = ((key.from_path if index == key.from_panel else
                         key.to_path if index == key.to_panel else None)
                        if kind == "move" else key if kind == "file" else None)
            record = self._folder_metadata.get(relative) if relative else None
            label = panel["metadata_display"]
            if record is None or index >= len(record["panels"]):
                label.hide()
                continue
            text = metadata_label(record["panels"][index])
            changes = record["changed_fields"][index]
            if changes:
                text += "\nMetadata differs: " + ", ".join(changes)
            content = record["content_statuses"][index]
            if content == "ignored":
                text += "\nContent difference ignored by active text rules"
            label.setText(text)
            label.show()

    def _refresh_selected_folder_metadata(self) -> None:
        for panel in self.panels:
            if panel["kind"] != "folder":
                continue
            item = panel["tree"].currentItem()
            if item is None:
                continue
            kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if kind in {"file", "move"}:
                key = item.data(0, Qt.ItemDataRole.UserRole + (2 if kind == "move" else 0))
                self._show_folder_metadata(kind, key)
                return
        for panel in self.panels:
            if panel["kind"] == "folder":
                panel["metadata_display"].hide()

    def _stop_folder_scroll_restore(self, widget: QWidget) -> None:
        panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        for index, panel in enumerate(panels):
            if panel["widget"] is widget and index < len(self._folder_scroll_restore_active):
                self._folder_scroll_restore_active[index] = False
                return

    def _capture_folder_view_state(self, panels: list[dict]) -> None:
        previous = self._folder_view_state
        states = []
        for index, panel in enumerate(panels):
            tree = panel["tree"]
            item = tree.currentItem()
            prior = previous[index] if index < len(previous) else {}
            kind = item.data(0, Qt.ItemDataRole.UserRole + 1) if item is not None else None
            if kind in {"file", "folder", "move"}:
                key = item.data(0, Qt.ItemDataRole.UserRole + (2 if kind == "move" else 0))
                selected = (kind, key)
            else:
                selected = prior.get("selected")
            scroll = tree.verticalScrollBar().value() if tree.topLevelItemCount() else prior.get("scroll", 0)
            states.append({"selected": selected, "scroll": scroll})
        self._folder_view_state = states

    def _restore_folder_item(self, index: int, item: QTreeWidgetItem, kind: str, key) -> None:
        if index >= len(self._folder_view_state):
            return
        if self._folder_view_state[index].get("selected") != (kind, key):
            return
        tree = [panel for panel in self.panels if panel["kind"] == "folder"][index]["tree"]
        self._syncing_folder_selection = True
        try:
            tree.setCurrentItem(item)
            parent = item.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            tree.scrollToItem(item)
        finally:
            self._syncing_folder_selection = False

    def _select_folder_target(self, kind: str, key) -> None:
        panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        self._syncing_folder_selection = True
        try:
            for index, panel in enumerate(panels):
                if kind == "move":
                    item = self._folder_move_items[index].get(key)
                elif kind == "folder":
                    item = self._folder_tree_folders[index].get(key)
                else:
                    item = self._folder_tree_items[index].get(key)
                    if item is None and key in self._folder_rows and self._folder_row_visible(key, self._folder_rows[key]):
                        self._upsert_folder_item(index, panel, key, self._folder_rows[key])
                        item = self._folder_tree_items[index].get(key)
                if item is None or item.isHidden():
                    continue
                tree = panel["tree"]
                tree.setCurrentItem(item)
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                tree.scrollToItem(item)
                if index < len(self._folder_view_state):
                    self._folder_view_state[index]["selected"] = (kind, key)
            if kind == "file":
                self._folder_navigation_anchor = str(key).casefold()
            elif kind == "move":
                self._folder_navigation_anchor = self._folder_move_sort_key(key)
        finally:
            self._syncing_folder_selection = False

    def _navigate_folder_change(self, direction: int) -> None:
        targets: list[tuple[str, str, object]] = []
        for relative, statuses in self._folder_rows.items():
            if statuses is not None and (statuses[0] == "error" or any(
                    value not in {"identical", "ignored"} for value in statuses[1:])) and self._folder_row_visible(relative, statuses):
                targets.append((relative.casefold(), "file", relative))
        for match in self._folder_moves:
            if self._folder_move_visible(match):
                targets.append((self._folder_move_sort_key(match), "move", match))
        if not targets:
            self.status.setText("No changed files match the current folder filters")
            return
        targets.sort(key=lambda target: target[0])
        keys = [target[0] for target in targets]
        if self._folder_navigation_anchor is None:
            position = 0 if direction > 0 else len(targets) - 1
        elif direction > 0:
            position = bisect.bisect_right(keys, self._folder_navigation_anchor) % len(targets)
        else:
            position = (bisect.bisect_left(keys, self._folder_navigation_anchor) - 1) % len(targets)
        _sort_key, kind, key = targets[position]
        self._select_folder_target(kind, key)
        self.status.setText(f"Changed file {position + 1:,}/{len(targets):,}: " +
                            (f"{key.from_path} → {key.to_path}" if kind == "move" else key))

    @staticmethod
    def _folder_move_sort_key(match: MoveMatch) -> str:
        return f"{match.from_path} → {match.to_path} [{match.from_panel}:{match.to_panel}]".casefold()

    def _update_folder_counts_label(self) -> None:
        if not hasattr(self, "folder_status_combo"):
            return
        counts = self._folder_status_counts
        total = len(self._folder_rows)
        counts["all"] = total - (len(self._folder_move_hidden_paths) if len(self._folder_move_groups) == 2 else 0) + len(self._folder_moves)
        counts["moved"] = len(self._folder_moves)
        counts["linked"] = sum(relative in self._folder_rows and relative not in self._folder_move_hidden_paths
                               for relative in self._folder_links)
        for index, status in enumerate(FOLDER_STATUS_FILTERS):
            self.folder_status_combo.setItemText(index, f"{status.title()} ({counts[status]:,})")
        state = " · partial" if self._folder_partial else " · scanning" if self._folder_worker is not None else ""
        self.folder_counts_label.setText(f"{self._folder_visible_count:,} shown / {total:,} paths{state}")

    def _update_change_navigation(self) -> None:
        folder_view = self._comparison_mode() == "folder" and self.folder_back_button.isHidden()
        enabled = (self._folder_status_filter not in {"identical", "pending"} and
                   any(self._folder_status_counts[status] for status in
                       ("added", "removed", "modified", "metadata", "error", "moved"))
                   if folder_view else bool(self._change_rows) and self._comparison_mode() != "folder")
        self.previous_change_button.setEnabled(enabled)
        self.next_change_button.setEnabled(enabled)
        self.previous_change_button.setToolTip("Previous changed file (Shift+F7)" if folder_view else "Previous change (Shift+F7)")
        self.next_change_button.setToolTip("Next changed file (F7)" if folder_view else "Next change (F7)")

    def _navigate_change(self, direction: int) -> None:
        if self._comparison_mode() == "folder":
            if self.folder_back_button.isHidden():
                self._navigate_folder_change(direction)
            return
        if not self._change_rows:
            return
        self._change_index = (self._change_index + direction) % len(self._change_rows)
        row = self._change_rows[self._change_index]
        for panel in self.panels:
            editor = panel["editor"]
            block = editor.document().findBlockByNumber(row)
            if not block.isValid():
                continue
            cursor = editor.textCursor()
            cursor.setPosition(block.position())
            cursor.select(cursor.SelectionType.LineUnderCursor)
            editor.setTextCursor(cursor)
            editor.ensureCursorVisible()
        self.status.setText(f"Change {self._change_index + 1} of {len(self._change_rows)}")
        self._schedule_recovery()

    def _rebuild_folder_display(self, panels: list[dict]) -> None:
        self._capture_folder_view_state(panels)
        self._folder_scroll_restore_active = [True for _ in panels]
        if self._folder_search_needs_reveal:
            for state in self._folder_view_state:
                state["scroll"] = 0
        self._folder_tree_items = [{} for _ in panels]
        self._folder_tree_folders = [{} for _ in panels]
        self._folder_move_groups = [None for _ in panels]
        self._folder_move_items = [{} for _ in panels]
        self._folder_move_hidden_paths = (
            {relative for match in self._folder_moves for relative in (match.from_path, match.to_path)}
            if len(panels) == 2 else set())
        self._folder_display_pending = deque(relative for relative, statuses in self._folder_rows.items()
                                             if self._folder_row_visible(relative, statuses))
        self._folder_display_queued = set(self._folder_display_pending)
        self._folder_visible_count = len(self._folder_display_pending) + sum(
            self._folder_move_visible(match) for match in self._folder_moves)
        self._rebuilding_folder_trees = True
        try:
            for panel in panels:
                panel["tree"].clear()
                panel["view_stack"].setCurrentIndex(1)
        finally:
            self._rebuilding_folder_trees = False
        for match in self._folder_moves:
            self._add_folder_move_item(match)
        self._schedule_folder_display()
        self._folder_scroll_timer.start(60)
        self._update_folder_counts_label()

    def _add_folder_move_item(self, match: MoveMatch) -> None:
        panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        if len(self._folder_move_groups) != len(panels):
            return
        left_name = panels[match.from_panel]["name"].text()
        right_name = panels[match.to_panel]["name"].text()
        title = f"{match.from_path} → {match.to_path}"
        if len(panels) > 2:
            title = f"{left_name}: {match.from_path} → {right_name}: {match.to_path}"
        for index, panel in enumerate(panels):
            group = self._folder_move_groups[index]
            if group is None:
                group = QTreeWidgetItem(["Detected moves", "Exact content"])
                group.setData(0, Qt.ItemDataRole.UserRole + 1, "move_group")
                panel["tree"].insertTopLevelItem(0, group)
                self._folder_move_groups[index] = group
            item = QTreeWidgetItem([title, "Moved · exact SHA-256"])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "move")
            item.setData(0, Qt.ItemDataRole.UserRole + 2, match)
            item.setToolTip(0, f"{left_name}: {match.from_path}\n{right_name}: {match.to_path}\nIdentical content")
            item.setForeground(0, QColor("#72d49a"))
            item.setForeground(1, QColor("#72d49a"))
            group.addChild(item)
            item.setHidden(not self._folder_move_visible(match))
            self._folder_move_items[index][match] = item
            self._restore_folder_item(index, item, "move", match)
            if index == 0 and self._folder_search_needs_reveal and not item.isHidden():
                self._folder_search_needs_reveal = False
                self._select_folder_target("move", match)
            group.setHidden(all(group.child(child).isHidden() for child in range(group.childCount())))
            panel_index = self._panel_index_for_widget(panel["widget"])
            group.setExpanded(self._folder_move_group_expanded[panel_index])

    @staticmethod
    def _refresh_folder_ancestor_visibility(item: QTreeWidgetItem) -> None:
        parent = item.parent()
        while parent is not None:
            if not item.isHidden():
                parent.setHidden(False)
            elif parent.childCount() <= 256:
                parent.setHidden(all(parent.child(index).isHidden()
                                     for index in range(parent.childCount())))
            else:
                break
            parent = parent.parent()

    def _schedule_folder_display(self) -> None:
        if self._folder_display_pending and not self._folder_display_scheduled:
            self._folder_display_scheduled = True
            QTimer.singleShot(0, self._drain_folder_display)

    def _drain_folder_display(self) -> None:
        self._folder_display_scheduled = False
        panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        for _ in range(min(64, len(self._folder_display_pending))):
            relative = self._folder_display_pending.popleft()
            self._folder_display_queued.discard(relative)
            statuses = self._folder_rows.get(relative)
            if not self._folder_row_visible(relative, statuses):
                continue
            for index, panel in enumerate(panels):
                self._upsert_folder_item(index, panel, relative, statuses)
        for index, panel in enumerate(panels):
            if (index < len(self._folder_view_state) and self._folder_scroll_restore_active[index]
                    and panel["tree"].currentItem() is None):
                scroll = panel["tree"].verticalScrollBar()
                scroll.setValue(min(self._folder_view_state[index]["scroll"], scroll.maximum()))
        if not self._folder_display_pending:
            self._folder_scroll_timer.start(60)
        self._refresh_selected_folder_metadata()
        self._schedule_folder_display()
        self._finish_context_folder_view()

    def _restore_folder_scroll_positions(self) -> None:
        panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        for index, panel in enumerate(panels):
            if (index >= len(self._folder_view_state) or not self._folder_scroll_restore_active[index]
                    or panel["tree"].currentItem() is not None):
                continue
            scroll = panel["tree"].verticalScrollBar()
            scroll.setValue(min(self._folder_view_state[index]["scroll"], scroll.maximum()))

    def _upsert_folder_item(self, index: int, panel: dict, relative: str, statuses) -> None:
        if index >= len(self._folder_tree_items):
            return
        items = self._folder_tree_items[index]
        status = statuses[index] if statuses is not None else "pending"
        entry = (self._folder_entry_maps[index].get(relative)
                 if index < len(self._folder_entry_maps) else None)
        link = (self._folder_links.get(relative) or [None] * len(self._folder_tree_items))[index]
        exists = status not in {"removed", "pending"}
        label = (("Not checked · partial" if self._folder_partial else "Checking…") if status == "pending" else
                 "Metadata only" if status == "metadata" else
                 status.title() if exists else f"Missing in {panel['name'].text()}")
        record = self._folder_metadata.get(relative)
        if record and index < len(record["changed_fields"]) and record["changed_fields"][index]:
            if status == "modified":
                label += " · metadata differs"
        if link is not None:
            label += f" · {link.label()}"
        elif isinstance(entry, ArchiveIssue):
            label += f" · {entry.detail}"
        elif isinstance(entry, ArchiveMember):
            label += " · ZIP (read-only)"
        color = {"modified": "#f3c969", "metadata": "#c9a6f2", "ignored": "#8fb9e8", "added": "#72d49a", "removed": "#ff8585",
                 "error": "#ff8585", "identical": "#98a2b3", "pending": "#98a2b3"}[status]
        if relative in items:
            item = items[relative]
            item.setText(1, label)
        else:
            parts = relative.split("/")
            parent = None
            folders = self._folder_tree_folders[index]
            for depth, part in enumerate(parts[:-1]):
                folder_path = "/".join(parts[:depth + 1])
                if folder_path not in folders:
                    folder = QTreeWidgetItem([part, ""])
                    folder.setIcon(0, QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
                    folder.setData(0, Qt.ItemDataRole.UserRole, folder_path)
                    folder.setData(0, Qt.ItemDataRole.UserRole + 1, "folder")
                    if parent is None:
                        panel["tree"].addTopLevelItem(folder)
                    else:
                        parent.addChild(folder)
                    folders[folder_path] = folder
                    panel_index = self._panel_index_for_widget(panel["widget"])
                    folder.setExpanded(folder_path in self._folder_expanded_paths[panel_index] or
                                       bool(self._folder_path_query and self._folder_search_needs_reveal
                                            and self._folder_visible_count <= 100))
                    self._restore_folder_item(index, folder, "folder", folder_path)
                parent = folders[folder_path]
            item = QTreeWidgetItem([parts[-1], label])
            item.setIcon(0, QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
                         if link is not None and link.directory else self._file_icon(relative))
            item.setData(0, Qt.ItemDataRole.UserRole, relative)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
            if parent is None:
                panel["tree"].addTopLevelItem(item)
            else:
                parent.addChild(item)
            items[relative] = item
        item.setToolTip(0, relative)
        item.setToolTip(1, relative)
        if link is not None:
            item.setToolTip(0, f"{relative}\n{link.label()}\nFolder sync: unavailable for linked paths")
            item.setToolTip(1, item.toolTip(0))
        elif isinstance(entry, (ArchiveMember, ArchiveIssue)):
            detail = entry.detail if isinstance(entry, ArchiveIssue) else f"{entry.archive}\n{entry.relative}\nRead-only ZIP entry"
            item.setToolTip(0, detail)
            item.setToolTip(1, detail)
        if record and index < len(record["panels"]):
            detail = item.toolTip(0)
            text = metadata_label(record["panels"][index])
            item.setToolTip(0, (detail + "\n" if detail else "") + text)
            item.setToolTip(1, item.toolTip(0))
        item.setForeground(0, QColor(color))
        item.setForeground(1, QColor(color))
        if relative in self._folder_move_hidden_paths:
            item.setHidden(True)
        else:
            item.setHidden(False)
        self._refresh_folder_ancestor_visibility(item)
        if not item.isHidden():
            self._restore_folder_item(index, item, "file", relative)
            if index == 0 and self._folder_search_needs_reveal:
                self._folder_search_needs_reveal = False
                self._select_folder_target("file", relative)

    def _open_folder_item(self, source_panel: QWidget, item: QTreeWidgetItem) -> None:
        self._pending_folder_content_hit = None
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind not in {"file", "move"}:
            return
        folder_panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        if not folder_panels:
            return
        if kind == "move":
            match = item.data(0, Qt.ItemDataRole.UserRole + 2)
            relatives = {match.from_panel: match.from_path, match.to_panel: match.to_path}
            title = f"Move: {match.from_path} → {match.to_path}"
        else:
            relative = item.data(0, Qt.ItemDataRole.UserRole)
            relatives = {index: relative for index in range(len(folder_panels))}
            title = f"File diff: {relative}"
        preview_target = target_record(kind, match if kind == "move" else relative)
        for panel in folder_panels:
            if panel.get("dirty") and not self._confirm_replace_panel(self.panels.index(panel)):
                return
        prepared = []
        try:
            for index, panel in enumerate(folder_panels):
                relative = relatives.get(index)
                if relative is None:
                    prepared.append(("Not part of this detected move.", None, None, False, None))
                    continue
                entry = (self._folder_entry_maps[index].get(relative)
                         if index < len(self._folder_entry_maps) else None)
                if isinstance(entry, ArchiveIssue):
                    prepared.append((f"Archive entry error: {relative}\n{entry.detail}", None, None, True, None))
                    continue
                if isinstance(entry, ArchiveMember):
                    file_content = inspect_file_content(entry, preview_limit=True)
                    content = (file_content.text if file_content.text is not None and not is_image_source(entry) else
                               unloaded_file_message(entry, file_content))
                    prepared.append((content, None, file_content, True, entry))
                    continue
                path = panel["path"] / Path(relative)
                link = inspect_link(path, panel["path"])
                if link is not None:
                    prepared.append((f"Linked path: {relative}\n{link.label()}\n"
                                     "Folder sync is unavailable for linked paths.", None, None, True, None))
                elif path.is_file():
                    file_content = inspect_file_content(path, preview_limit=True)
                    content = (file_content.text if file_content.text is not None and not is_image_source(path)
                               else unloaded_file_message(path, file_content))
                    prepared.append((content, path, file_content, True, path))
                else:
                    prepared.append((f"does not exist in {panel['name'].text()}: {relative}", None, None, False, None))
        except OSError as exc:
            QMessageBox.warning(self, "Open folder file", str(exc))
            return
        self._loading_panel = True
        for index, (panel, (content, editing_path, file_content, present, image_source)) in enumerate(zip(folder_panels, prepared)):
            relative = relatives.get(index)
            panel["preview_path"] = (panel["path"] / relative if not present and relative is not None
                                     and not is_zip_source(panel["path"]) else None)
            panel["image_source"] = image_source
            if file_content is not None:
                panel["binary"] = file_content.text is None or is_image_source(image_source)
                panel["too_large"] = file_content.too_large
            else:
                panel["binary"] = False
                panel["too_large"] = False
            panel["editor"].setPlainText(content)
            if file_content is not None:
                self._set_panel_encoding(panel, file_content.encoding or "utf-8", file_content.bom)
                if file_content.text is None:
                    panel["encoding_display"].setText("Large file" if file_content.too_large else "Binary")
                if is_zip_source(panel["path"]):
                    panel["encoding_display"].setText("ZIP · " + panel["encoding_display"].text() + " · read-only")
            else:
                self._set_panel_encoding(panel, "utf-8")
            panel["editor"].setReadOnly(editing_path is None or panel["binary"] or bool(panel.get("git") and panel["git"]["readonly"]))
            panel["view_stack"].setCurrentIndex(0)
            self._set_panel_clean(panel, editing_path,
                                  file_content.fingerprint if editing_path is not None else None)
        self._loading_panel = False
        self._render_panels_text_diff(folder_panels)
        self._show_folder_metadata(kind, match if kind == "move" else relative)
        for panel in folder_panels:
            if panel.get("git"):
                self._set_git_source(panel, panel["git"])
        self.folder_back_button.setVisible(True)
        self._folder_preview_target = preview_target
        self._sync_search_bar_visibility()
        self.folder_triage_bar.hide()
        self._update_change_navigation()
        missing = [panel["name"].text() for panel, (_text, _editing_path, _file_content, present, _source)
                   in zip(folder_panels, prepared) if not present]
        suffix = f" · missing in {', '.join(missing)}" if missing and kind == "file" else ""
        self.status.setText(title + suffix)
        self._update_panel_locking()
        self._schedule_recovery()

    def _back_to_folders(self) -> None:
        self._pending_folder_content_hit = None
        self._folder_preview_target = None
        self._show_folder_trees()
        self._sync_search_bar_visibility()
        self.folder_triage_bar.show()
        self._render_panels_folder_diff([panel for panel in self.panels if panel["kind"] == "folder"])
        self._update_change_navigation()
        self.status.setText("Folder comparison" + (" · stale; Refresh to rescan" if self._folder_stale else ""))
