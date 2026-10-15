"""Coordinate disk workflows and live editors within a comparison window."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtWidgets import QMessageBox

from d1ffler.file_content import disk_fingerprint
from d1ffler.folder_links import inspect_link


@dataclass(frozen=True)
class SourceScope:
    path: Path
    folder: bool = False

    @property
    def identity(self):
        return Path(os.path.normcase(str(self.path.resolve())))

    def overlaps(self, other):
        left, right = self.identity, other.identity
        if left == right or (self.folder and right.is_relative_to(left)) or (
                other.folder and left.is_relative_to(right)):
            return True
        # Different spellings, symlinks, and hard links can name the same file.
        if not self.folder and not other.folder:
            try:
                return self.path.samefile(other.path)
            except OSError:
                pass
        return False


@dataclass
class SharedEditor:
    view: object
    index: int
    path: Path
    label: str

    @property
    def panel(self):
        return self.view.panels[self.index]


class SourceReservation:
    def __init__(self, coordinator, scopes, view, title, saved):
        self.coordinator, self.scopes = coordinator, scopes
        self.view, self.title, self.saved = view, title, saved
        self.finished = False

    def finish(self, *, notify=True):
        if self.finished:
            return
        self.finished = True
        self.coordinator.pending.remove(self)
        self.coordinator.update_locking()
        if notify:
            self.coordinator.notify(self.scopes)

    def has_unsaved_edits(self):
        return bool(self.coordinator.editors(self.scopes, dirty_only=True))


class SharedSources:
    def __init__(self, owner):
        self.owner = owner
        self.pending = []

    @property
    def comparisons(self):
        return [view for view in getattr(self.owner, "comparisons", [self.owner])
                if not getattr(view, "_closed", False)]

    def label(self, view):
        tabs = getattr(self.owner, "comparison_tabs", None)
        if tabs is None:
            return "Current comparison"
        index = tabs.indexOf(view)
        return f"Tab {index + 1} · {tabs.tabText(index).rstrip(' *')}"

    def editors(self, scopes, *, dirty_only=False):
        found = []
        for view in self.comparisons:
            for index, panel in enumerate(view.panels):
                path = panel.get("editing_path") or panel.get("preview_path")
                if path is None or (dirty_only and not (
                        panel.get("dirty") or panel.get("recovery_pending"))):
                    continue
                if any(scope.overlaps(SourceScope(path)) for scope in scopes):
                    found.append(SharedEditor(view, index, path,
                                              f"{self.label(view)} · {panel['name'].text()}"))
        return found

    def busy(self, scopes):
        return next((job for job in self.pending if any(
            scope.overlaps(target) for scope in scopes for target in job.scopes)), None)

    def available(self, scopes, view, title, parent):
        job = self.busy(scopes)
        if job is None:
            return True
        QMessageBox.information(parent, title,
                                f"{job.title} is still running in {self.label(job.view)}.\n"
                                f"Wait for it to finish before writing these shared sources:\n" +
                                "\n".join(str(scope.path) for scope in scopes))
        return False

    def prepare(self, scopes, view, title, parent=None):
        """Return None on cancellation, otherwise whether resolving edits saved files."""
        parent = parent or view
        if not self.available(scopes, view, title, parent):
            return None
        editors = self.editors(scopes, dirty_only=True)
        if not editors:
            return False

        def snapshot(items):
            return [(id(item.view), id(item.panel), item.index, item.path,
                     item.view._panel_text_with_line_endings(item.panel)) for item in items]

        reviewed = snapshot(editors)
        choice = QMessageBox.warning(
            parent, title,
            "These open files have unsaved edits. Save all listed edits, discard them and "
            "reload disk contents, or cancel this operation.\n\n" +
            "\n\n".join(f"{item.label}\n{item.path}" for item in editors),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if choice not in (QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Discard):
            return None
        # A modal prompt can run other events. Do not apply its answer to newly edited buffers.
        current = self.editors(scopes, dirty_only=True)
        if snapshot(current) != reviewed:
            QMessageBox.information(parent, title, "The affected editors changed. Try again to review the current list.")
            return None
        if not self.available(scopes, view, title, parent):
            return None
        if choice == QMessageBox.StandardButton.Save:
            if any(SourceScope(item.path).overlaps(SourceScope(other.path))
                   for number, item in enumerate(editors) for other in editors[number + 1:]):
                QMessageBox.information(parent, title,
                                        "Several edited buffers refer to the same file. Save the version you want "
                                        "from its tab, then resolve the remaining edits before continuing.")
                return None
            for item in editors:
                if not item.view._save_panel_file(item.index):
                    return None
        else:
            # One decision covers the entire list: Cancel has made no changes to any buffer.
            for item in editors:
                item.view._reload_panel(item.panel["widget"], discard_confirmed=True)
        if self.editors(scopes, dirty_only=True):
            return None
        return choice == QMessageBox.StandardButton.Save

    def begin(self, scopes, view, title, parent=None, *, resolve=True):
        """Reserve all read/write paths until completion, including during Qt event pumping."""
        scopes = tuple(scopes)
        parent = parent or view
        saved = self.prepare(scopes, view, title, parent) if resolve else False
        if saved is None or not self.available(scopes, view, title, parent):
            return None
        if resolve and self.editors(scopes, dirty_only=True):
            return None
        reservation = SourceReservation(self, scopes, view, title, saved)
        self.pending.append(reservation)
        self.update_locking()
        return reservation

    def update_locking(self):
        for view in self.comparisons:
            if hasattr(view, "save_button"):
                view._update_panel_locking()

    def notify(self, scopes, *, exclude=None):
        """Refresh clean buffers; dirty buffers keep both their edits and disk warnings."""
        for view in self.comparisons:
            affected = [item for item in self.editors(scopes) if item.view is view]
            folders = any(panel["kind"] == "folder" and panel.get("path") is not None and
                          any(scope.overlaps(SourceScope(panel["path"], folder=True)) for scope in scopes)
                          for panel in view.panels)
            for item in affected:
                if exclude == (view, item.index):
                    continue
                if not item.panel.get("dirty") and not item.panel.get("recovery_pending"):
                    if (item.panel.get("editing_path") is None and item.panel["kind"] == "folder"
                            and inspect_link(item.path, item.panel["path"]) is not None):
                        view._mark_folder_stale()
                        continue
                    try:
                        if disk_fingerprint(item.path) == item.panel.get("disk_fingerprint"):
                            continue
                    except OSError:
                        # Reload surfaces the actual read error and retains the current buffer.
                        pass
                    editor = item.panel["editor"]
                    cursor = editor.textCursor()
                    position, anchor = cursor.position(), cursor.anchor()
                    scroll = editor.verticalScrollBar().value(), editor.horizontalScrollBar().value()
                    item.view._reload_panel(item.panel["widget"])
                    cursor = editor.textCursor()
                    last = editor.document().characterCount() - 1
                    cursor.setPosition(min(anchor, last))
                    cursor.setPosition(min(position, last), cursor.MoveMode.KeepAnchor)
                    editor.setTextCursor(cursor)
                    editor.verticalScrollBar().setValue(scroll[0])
                    editor.horizontalScrollBar().setValue(scroll[1])
            if folders:
                view._mark_folder_stale()
            if affected or folders:
                view._check_external_changes()
                view.comparison_changed.emit()
