"""Nonmodal search and scoped replacement for comparison editors."""

from __future__ import annotations

from PyQt6.QtGui import QTextCursor, QTextDocument
from PyQt6.QtWidgets import QMessageBox


MAX_COUNTED_MATCHES = 50_000
MAX_REPLACE_MATCHES = 100_000


class TextSearchMixin:
    def _text_search_available(self) -> bool:
        return not (self._comparison_mode() == "folder" and self.folder_back_button.isHidden()) and not any(
            panel.get("binary") for panel in self.panels if panel["kind"])

    def _sync_search_bar_visibility(self) -> None:
        self.search_bar.setVisible(self._search_bar_requested and self._text_search_available())
        if not self.search_bar.isHidden():
            self._schedule_search_update()

    def _close_find(self) -> None:
        self._search_bar_requested = False
        self._schedule_recovery()
        self.search_bar.hide()
        index = min(self._active_panel_index, len(self.panels) - 1)
        if index >= 0:
            self.panels[index]["editor"].setFocus()

    def _open_find(self) -> None:
        if self._comparison_mode() == "folder" and self.folder_back_button.isHidden():
            self.folder_path_search.setFocus()
            self.folder_path_search.selectAll()
            return
        if not self._text_search_available():
            self.status.setText("Text search is unavailable for binary previews")
            return
        self._search_bar_requested = True
        self._schedule_recovery()
        self._sync_search_bar_visibility()
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _open_replace(self) -> None:
        self._open_find()
        if not self.search_bar.isHidden():
            self.replacement_input.setFocus()
            self.replacement_input.selectAll()

    def _search_query_changed(self, value: str) -> None:
        self._search_query = value
        self._schedule_search_update()

    def _schedule_search_update(self) -> None:
        self._schedule_recovery()
        if hasattr(self, "_search_update_timer"):
            self._search_update_timer.start(90)

    def _find_flags(self, backward: bool = False):
        flags = QTextDocument.FindFlag(0)
        if self.search_case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.search_whole_word.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _search_panels(self) -> list[int]:
        if not self._text_search_available():
            return []
        return [index for index, panel in enumerate(self.panels)
                if not panel.get("binary") and panel["view_stack"].currentIndex() == 0]

    def _update_search_state(self) -> None:
        query = self.search_input.text()
        counts = {}
        total = 0
        limited = False
        if query:
            flags = self._find_flags()
            for index in self._search_panels():
                document = self.panels[index]["editor"].document()
                cursor = document.find(query, 0, flags)
                count = 0
                while not cursor.isNull():
                    count += 1
                    if total + count >= MAX_COUNTED_MATCHES:
                        limited = True
                        break
                    cursor = document.find(query, cursor.selectionEnd(), flags)
                counts[index] = count
                total += count
                if limited:
                    break
        active = min(self._active_panel_index, len(self.panels) - 1)
        name = self.panels[active]["name"].text() if active >= 0 else "No panel"
        count_label = f"{total:,}+ matches" if limited else f"{total:,} matches"
        self.search_count.setText(f"{count_label} · {name}: {counts.get(active, 0):,}")
        self._update_replace_target()

    def _update_replace_target(self) -> None:
        index = min(self._active_panel_index, len(self.panels) - 1)
        panel = self.panels[index] if index >= 0 else None
        editable = (panel is not None and panel["view_stack"].currentIndex() == 0 and
                    not panel.get("binary") and not panel["editor"].isReadOnly() and
                    self._text_search_available())
        name = panel["name"].text() if panel is not None else "No panel"
        self.replace_target.setText(f"Replace in {name}" + (" · read-only" if not editable else ""))
        enabled = bool(editable and self.search_input.text())
        self.replace_next_button.setEnabled(enabled)
        self.replace_all_button.setEnabled(enabled)

    def _find_next(self, direction: int = 1) -> None:
        query = self.search_input.text()
        if not query:
            self._open_find()
            return
        indices = self._search_panels()
        if not indices:
            self.status.setText("No text panels to search")
            return
        start = self._active_panel_index if self._active_panel_index in indices else indices[0]
        position = indices.index(start)
        ordered = [indices[(position + offset * direction) % len(indices)]
                   for offset in range(len(indices))]
        flags = self._find_flags(direction < 0)
        for offset, index in enumerate(ordered):
            editor = self.panels[index]["editor"]
            cursor = editor.textCursor()
            if offset == 0:
                anchor = cursor.selectionStart() if direction < 0 else cursor.selectionEnd()
            else:
                anchor = editor.document().characterCount() - 1 if direction < 0 else 0
            found = editor.document().find(query, anchor, flags)
            if not found.isNull():
                self._select_search_match(index, found, wrapped=offset > 0 and
                                          ((direction > 0 and index <= start) or
                                           (direction < 0 and index >= start)))
                return
        editor = self.panels[start]["editor"]
        anchor = editor.document().characterCount() - 1 if direction < 0 else 0
        found = editor.document().find(query, anchor, flags)
        if not found.isNull():
            self._select_search_match(start, found, wrapped=True)
        else:
            self.status.setText(f"Not found: {query}")

    def _select_search_match(self, index: int, cursor: QTextCursor, *, wrapped: bool = False) -> None:
        editor = self.panels[index]["editor"]
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()
        editor.setFocus()
        self._active_panel_index = index
        self._update_search_state()
        self.status.setText(f"Found in {self.panels[index]['name'].text()}" +
                            (" (wrapped)" if wrapped else ""))

    def _replacement_panel(self):
        index = min(self._active_panel_index, len(self.panels) - 1)
        if index < 0:
            return None
        panel = self.panels[index]
        if (panel["view_stack"].currentIndex() != 0 or panel.get("binary") or
                panel["editor"].isReadOnly() or not self._text_search_available()):
            self.status.setText(f"Cannot replace in read-only panel {panel['name'].text()}")
            return None
        return index, panel

    def _replace_next(self) -> None:
        target = self._replacement_panel()
        query = self.search_input.text()
        if target is None or not query:
            return
        index, panel = target
        editor = panel["editor"]
        document = editor.document()
        current = editor.textCursor()
        flags = self._find_flags()
        found = document.find(query, current.selectionStart() if current.hasSelection()
                              else current.position(), flags)
        if found.isNull():
            found = document.find(query, 0, flags)
        if found.isNull():
            self.status.setText(f"No matches in {panel['name'].text()}")
            return
        self._apply_replacements(index, [(found.selectionStart(), found.selectionEnd())])
        next_match = document.find(query, found.selectionStart() + len(self.replacement_input.text()), flags)
        if next_match.isNull():
            next_match = document.find(query, 0, flags)
        if not next_match.isNull():
            editor.setTextCursor(next_match)
        editor.setFocus()
        self._active_panel_index = index
        self.status.setText(f"Replaced one match in {panel['name'].text()}")

    def _replace_all(self) -> None:
        target = self._replacement_panel()
        query = self.search_input.text()
        if target is None or not query:
            return
        index, panel = target
        document = panel["editor"].document()
        flags = self._find_flags()
        matches = []
        found = document.find(query, 0, flags)
        while not found.isNull():
            matches.append((found.selectionStart(), found.selectionEnd()))
            if len(matches) > MAX_REPLACE_MATCHES:
                self.status.setText(f"Replace All is limited to {MAX_REPLACE_MATCHES:,} matches per panel")
                return
            found = document.find(query, found.selectionEnd(), flags)
        if not matches:
            self.status.setText(f"No matches in {panel['name'].text()}")
            return
        choice = QMessageBox.question(
            self, "Replace all", f"Replace {len(matches):,} matches in {panel['name'].text()}?\n\n"
            "The open buffer will change; you can undo it before saving.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if choice != QMessageBox.StandardButton.Yes:
            return
        self._apply_replacements(index, matches)
        panel["editor"].setFocus()
        self._active_panel_index = index
        self.status.setText(f"Replaced {len(matches):,} matches in {panel['name'].text()}")

    def _apply_replacements(self, index: int, matches: list[tuple[int, int]]) -> None:
        panel = self.panels[index]
        editor = panel["editor"]
        replacement = self.replacement_input.text()
        parts = []
        position = 0
        for start, end in matches:
            logical_start = editor._logical_position(start)
            logical_end = editor._logical_position(end)
            parts.extend((editor.logical_text_slice(position, logical_start), replacement))
            position = logical_end
        parts.append(editor.logical_text_slice(position, editor.history.document.characterCount() - 1))
        previous_loading = self._loading_panel
        self._loading_panel = True
        try:
            editor.replace_logical_text("".join(parts))
        finally:
            self._loading_panel = previous_loading
        self._panel_text_changed(index)
        self._update_search_state()
