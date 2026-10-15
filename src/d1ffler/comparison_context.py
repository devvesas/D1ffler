"""Versioned review context shared by workspaces and dirty-buffer recovery."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import queue
import re
import threading
from types import SimpleNamespace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QTreeWidgetItem

from d1ffler.background_tasks import job_messages
from d1ffler.comparison_models import MoveMatch
from d1ffler.editor_widgets import LineNumberTextEdit
from d1ffler.file_content import inspect_file_content
from d1ffler.folder_archives import is_zip_source
from d1ffler.review_notes import content_fingerprint, hunk_anchor, note_is_stale, source_key
from d1ffler.text_comparison import build_text_layout
from d1ffler.user_state import atomic_write
from d1ffler.local_data import pin_report, safe_local, storage_guard

CONTEXT_VERSION = 1
MAX_REPORT_BYTES = 24 * 1024 * 1024
OPTION_KEYS = ("excluded_paths", "ignore_whitespace", "ignore_line_endings",
               "ignore_gitignore", "follow_internal_links", "text_rules")


def text_digest(text):
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    parts = PurePosixPath(value).parts
    return bool(parts) and not value.startswith("/") and all(part not in {".", ".."} and ":" not in part for part in parts)


def target_record(kind, key):
    if kind == "move" and isinstance(key, MoveMatch):
        return {"kind": kind, "key": asdict(key)}
    if kind in {"file", "folder"} and safe_relative(key):
        return {"kind": kind, "key": key}
    return None


def read_target(record):
    if not isinstance(record, dict):
        return None
    kind, key = record.get("kind"), record.get("key")
    if kind == "move" and isinstance(key, dict):
        try:
            key = MoveMatch(**key)
            if (type(key.from_panel) is not int or type(key.to_panel) is not int or
                    not 0 <= key.from_panel < 4 or not 0 <= key.to_panel < 4 or
                    key.from_panel == key.to_panel or not isinstance(key.sha256, str) or
                    not safe_relative(key.from_path) or not safe_relative(key.to_path)):
                return None
        except TypeError:
            return None
        return kind, key
    return (kind, key) if kind in {"file", "folder"} and safe_relative(key) else None


class RetainedReports:
    """Immutable reports stored once; snapshots contain bounded local references."""
    def __init__(self, settings_path):
        self.directory = settings_path.parent / "D1ffler-reports"

    def retain(self, report):
        with storage_guard(self.directory.parent):
            return self._retain(report)

    def _retain(self, report):
        if report.get("context_status") == "unavailable" and report.get("retained_ref"):
            pin_report(self.directory.parent, report["retained_ref"].get("sha256"))
            return deepcopy(report["retained_ref"])
        data = {key: value for key, value in report.items() if key not in {"context_status", "retained_ref"}}
        payload = (json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > MAX_REPORT_BYTES:
            raise OSError("Retained report exceeds the 24 MiB limit.")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.directory / f"{digest}.json"
        pin_report(self.directory.parent, digest)
        safe_local(self.directory.parent, path)
        if not path.exists() or path.read_bytes() != payload:
            atomic_write(path, payload)
        return {"sha256": digest, "started_at": str(report.get("started_at", ""))}

    def load(self, reference, *, pin=True):
        with storage_guard(self.directory.parent):
            return self._load(reference, pin=pin)

    def _load(self, reference, *, pin=True):
        try:
            digest = reference.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid report reference")
            path = self.directory / f"{digest}.json"
            if pin:
                pin_report(self.directory.parent, digest)
            safe_local(self.directory.parent, path)
            if path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError("Report too large")
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("Report changed")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("Invalid report")
            return {**data, "context_status": "historical", "retained_ref": deepcopy(reference)}
        except (OSError, ValueError, TypeError):
            return {"started_at": str(reference.get("started_at", "")),
                    "context_status": "unavailable", "retained_ref": deepcopy(reference)}


def cursor_location(editor, position):
    block = editor.document().findBlock(position)
    line = sum(item != LineNumberTextEdit.ALIGNMENT_PLACEHOLDER
               for item in editor.toPlainText().split("\n")[:block.blockNumber()])
    return [line, position - block.position()]


def cursor_position(editor, location):
    if not isinstance(location, list) or len(location) != 2 or any(type(n) is not int or n < 0 for n in location):
        return None
    line, column = location
    block = editor.document().firstBlock()
    logical = 0
    while block.isValid():
        if block.text() != LineNumberTextEdit.ALIGNMENT_PLACEHOLDER:
            if logical == line:
                return block.position() + min(column, block.length() - 1)
            logical += 1
        block = block.next()
    return None


class ComparisonContextMixin:
    def _comparison_context_snapshot(self, name):
        folders = [panel for panel in self.panels if panel["kind"] == "folder"]
        self._capture_folder_view_state(folders)
        folder_view = []
        for index, panel in enumerate(folders):
            panel_index = self.panels.index(panel)
            state = self._folder_view_state[index]
            selected = state.get("selected")
            folder_view.append({"panel": panel_index, "selected": target_record(*selected) if selected else None,
                                "expanded": sorted(self._folder_expanded_paths[panel_index]),
                                "move_group_expanded": self._folder_move_group_expanded[panel_index],
                                "scroll": state.get("scroll", 0)})
        editors = []
        for panel in self.panels:
            editor = panel["editor"]
            cursor = editor.textCursor()
            editors.append({"text_digest": text_digest(self._panel_text(panel)),
                            "cursor": cursor_location(editor, cursor.position()),
                            "anchor": cursor_location(editor, cursor.anchor()),
                            "vertical": editor.verticalScrollBar().value(),
                            "horizontal": editor.horizontalScrollBar().value()})
        hunk = None
        if 0 <= self._change_index < len(self._hunks):
            panels = folders if folders else self.panels
            hunk = {"index": self._change_index,
                    "anchor": hunk_anchor(self._aligned_rows, [self._panel_text(p).splitlines() for p in panels],
                                          self._hunks[self._change_index])}
        reports = RetainedReports(self.settings_path)
        context = deepcopy({"context_version": CONTEXT_VERSION, "name": name,
                "changed_only": self.show_changed_files_only, "compare_metadata": self.metadata_compare_rule,
                "comparison_options": {key: self.settings.get(key) for key in OPTION_KEYS},
                "text_rules": self.workspace_text_rules, "review_notes": self.review_notes,
                "report_refs": {"sync": [reports.retain(item) for item in self.sync_reports[-10:]],
                                "restore": [reports.retain(item) for item in self.restore_reports[-10:]]},
                "view": {"active_panel": self._active_panel_index, "folder_query": self.folder_path_search.text(),
                         "folder_status": self._folder_status_filter, "search_query": self.search_input.text(),
                         "search_case": self.search_case.isChecked(), "search_whole_word": self.search_whole_word.isChecked(),
                         "search_visible": self._search_bar_requested, "replacement": self.replacement_input.text(),
                         "content_search_query": self._content_search_query,
                         "folders": folder_view, "editors": editors, "hunk": hunk,
                         "preview": self._folder_preview_target if not self.folder_back_button.isHidden() else None}})
        if isinstance(self._pending_context_view, dict):
            # Resnapshotting during a scan/layout must preserve the requested
            # position rather than the temporary empty view.
            for key in ("editors", "folders", "hunk", "preview", "active_panel"):
                if key in self._pending_context_view:
                    context["view"][key] = deepcopy(self._pending_context_view[key])
        return context

    def _restore_comparison_context(self, context):
        if not isinstance(context, dict) or context.get("context_version", 1) != CONTEXT_VERSION:
            self._context_message("Comparison context uses an unsupported version; sources opened with current settings.")
            return
        options = context.get("comparison_options")
        if isinstance(options, dict):
            for key in OPTION_KEYS:
                expected = list if key == "text_rules" else str if key == "excluded_paths" else bool
                if isinstance(options.get(key), expected):
                    self.settings[key] = deepcopy(options[key])
        def records(key):
            value = context.get(key)
            return [deepcopy(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        self.workspace_text_rules = records("text_rules")
        self.review_notes = [item for item in records("review_notes") if item.get("id") and item.get("source_key")]
        for note in self.review_notes:
            note["stale"] = True
            note["hunk_stale"] = note.get("kind") == "hunk"
        refs = context.get("report_refs")
        reports = RetainedReports(self.settings_path)
        for key, attribute in (("sync", "sync_reports"), ("restore", "restore_reports")):
            values = refs.get(key) if isinstance(refs, dict) else None
            restored = ([reports.load(item) for item in values[-10:] if isinstance(item, dict)]
                        if isinstance(values, list) else [{**item, "context_status": "historical"}
                                                        for item in records(attribute)[-10:]])
            setattr(self, attribute, restored)
        self.show_changed_files_only = bool(context.get("changed_only", False))
        self.metadata_compare_rule = (context["compare_metadata"] if isinstance(context.get("compare_metadata"), bool)
                                      else bool(self.settings.get("compare_metadata", False)))
        self._active_workspace_name = str(context["name"]) if context.get("name") else None
        self.changed_files_button.setChecked(self.show_changed_files_only)
        view = context.get("view")
        if isinstance(view, dict):
            self.folder_path_search.setText(str(view.get("folder_query") or ""))
            self.folder_status_combo.setCurrentIndex(max(0, self.folder_status_combo.findData(view.get("folder_status", "all"))))
            self.search_input.setText(str(view.get("search_query") or ""))
            self.search_case.setChecked(bool(view.get("search_case")))
            self.search_whole_word.setChecked(bool(view.get("search_whole_word")))
            self._search_bar_requested = bool(view.get("search_visible"))
            self.replacement_input.setText(str(view.get("replacement") or ""))
            self._content_search_query = str(view.get("content_search_query") or "")
            self._pending_context_view = deepcopy(view)
        if self.review_notes or self.sync_reports or self.restore_reports:
            self._context_message("Restored notes are checked against current content; retained reports are historical.")
        if any(item.get("context_status") == "unavailable" for item in self.sync_reports + self.restore_reports):
            self._context_message("Some retained reports are missing or changed and are unavailable.")

    def _context_message(self, message):
        lines = self.context_notice.text().splitlines()
        if message not in lines:
            self.context_notice.setText("\n".join(lines + [message]).strip())
        self.context_notice.show()

    def _prepare_context_view(self, *, recovery=False):
        view = self._pending_context_view
        if not isinstance(view, dict):
            return
        active = view.get("active_panel")
        if type(active) is int and 0 <= active < len(self.panels):
            self._active_panel_index = active
        folders = [panel for panel in self.panels if panel["kind"] == "folder"]
        self._context_folder_restoring = bool(folders)
        states = view.get("folders")
        self._folder_view_state = []
        for panel in folders:
            index = self.panels.index(panel)
            state = next((item for item in states if isinstance(item, dict) and item.get("panel") == index), {}) if isinstance(states, list) else {}
            expanded = state.get("expanded")
            self._folder_expanded_paths[index] = {value for value in expanded if safe_relative(value)} if isinstance(expanded, list) else set()
            self._folder_move_group_expanded[index] = bool(state.get("move_group_expanded", True))
            scroll = state.get("scroll", 0)
            self._folder_view_state.append({"selected": read_target(state.get("selected")),
                                            "scroll": scroll if type(scroll) is int and scroll >= 0 else 0})
            panel["tree"].clear()
        self._folder_search_needs_reveal = False
        target = read_target(view.get("preview"))
        if folders and target and not recovery:
            # Folder previews need the freshly scanned inventory (including ZIPs and moves).
            self._pending_context_preview = target
        else:
            self._pending_context_preview = None
            if recovery and target:
                self._folder_preview_target = target_record(*target)

    def _finish_context_folder_view(self):
        if not self._context_folder_restoring or self._folder_worker is not None or self._folder_display_pending:
            return
        self._context_folder_restoring = False
        folders = [panel for panel in self.panels if panel["kind"] == "folder"]
        target = self._pending_context_preview
        if target:
            self._pending_context_preview = None
            kind, key = target
            valid = (key in self._folder_rows if kind == "file" else key in self._folder_moves if kind == "move" else False)
            paths = [key] if kind == "file" else [key.from_path, key.to_path] if kind == "move" else []
            for panel in folders:
                if is_zip_source(panel["path"]):
                    continue
                try:
                    if any(not (panel["path"] / relative).resolve().is_relative_to(panel["path"].resolve()) for relative in paths):
                        valid = False
                except (OSError, ValueError):
                    valid = False
            if valid:
                item = QTreeWidgetItem()
                item.setData(0, Qt.ItemDataRole.UserRole + 1, kind)
                item.setData(0, Qt.ItemDataRole.UserRole + (2 if kind == "move" else 0), key)
                self._open_folder_item(folders[0]["widget"], item)
                self._restore_context_text_view()
            else:
                self._pending_context_view = None
                self._context_message("The saved folder preview is stale or missing; showing current folder results.")
        if isinstance(self._pending_context_view, dict) and self.folder_back_button.isHidden():
            self._pending_context_view = None
        for index, panel in enumerate(folders):
            state = self._folder_view_state[index] if index < len(self._folder_view_state) else {}
            if state.get("selected") and panel["tree"].currentItem() is None:
                state["selected"] = None
                self._context_message("A saved folder selection is stale or filtered out; showing current folder results.")
            scroll = panel["tree"].verticalScrollBar()
            scroll.setValue(min(state.get("scroll", 0), scroll.maximum()))
            panel_index = self.panels.index(panel)
            self._folder_expanded_paths[panel_index].intersection_update(self._folder_tree_folders[index])

    def _restore_context_text_view(self):
        view = self._pending_context_view
        if not isinstance(view, dict) or self._pending_context_preview or self._text_worker is not None:
            return
        if self._comparison_mode() == "folder" and self.folder_back_button.isHidden():
            return
        self._pending_context_view = None
        states = view.get("editors")
        for panel, state in zip(self.panels, states if isinstance(states, list) else []):
            if not isinstance(state, dict) or panel.get("binary"):
                continue
            if state.get("text_digest") != text_digest(self._panel_text(panel)):
                self._context_message("Source content changed; the saved text position is stale and was reset.")
                continue
            editor = panel["editor"]
            position, anchor = cursor_position(editor, state.get("cursor")), cursor_position(editor, state.get("anchor"))
            if position is not None and anchor is not None:
                cursor = QTextCursor(editor.document())
                cursor.setPosition(anchor)
                cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
                editor.setTextCursor(cursor)
            for key, scroll in (("vertical", editor.verticalScrollBar()), ("horizontal", editor.horizontalScrollBar())):
                value = state.get(key)
                if type(value) is int and value >= 0:
                    scroll.setValue(min(value, scroll.maximum()))
        active = view.get("active_panel")
        if type(active) is int and 0 <= active < len(self.panels):
            self._active_panel_index = active
        hunk_record = view.get("hunk")
        if hunk_record:
            anchor = hunk_record.get("anchor") if isinstance(hunk_record, dict) else hunk_record
            preferred = hunk_record.get("index") if isinstance(hunk_record, dict) else None
            panels = [p for p in self.panels if p["kind"] == "folder"] if self._comparison_mode() == "folder" else self.panels
            lines = [self._panel_text(p).splitlines() for p in panels]
            matches = [index for index, hunk in enumerate(self._hunks)
                       if hunk_anchor(self._aligned_rows, lines, hunk) == anchor]
            self._change_index = (preferred if type(preferred) is int and preferred in matches else
                                  matches[0] if len(matches) == 1 else -1)
            if self._change_index == -1:
                self._context_message("The saved hunk is stale; choose a change in the current comparison.")
        self._sync_search_bar_visibility()

    def _start_context_note_validation(self):
        sources = self._review_sources()
        if not sources or not self.review_notes:
            return
        notes = list(self.review_notes)
        copies = deepcopy(notes)
        event, messages = threading.Event(), queue.Queue()
        rules = deepcopy(self._active_text_rules())
        options = self._text_comparison_options()
        key = source_key(sources)
        def run():
            fingerprints, anchors = {}, {}
            for note in copies:
                if event.is_set():
                    break
                relative = note.get("path")
                if relative is not None and not isinstance(relative, str):
                    continue
                try:
                    if relative not in fingerprints:
                        fingerprints[relative] = content_fingerprint(sources, relative, event.is_set)
                    if note.get("kind") == "hunk" and relative not in anchors:
                        # Conservatively leave ZIP/large/unreadable hunks stale; they can be reviewed again.
                        paths = [Path(s["path"]) / relative if s["kind"] == "folder" and safe_relative(relative)
                                 else Path(s["path"]) for s in sources]
                        contents = [inspect_file_content(path, preview_limit=True) for path in paths]
                        if any(content.text is None for content in contents):
                            anchors[relative] = set()
                        else:
                            lines = [content.text.splitlines() for content in contents]
                            text_sources = [{"text": content.text, "scope": path.name,
                                             "encoding": content.encoding, "bom": content.bom.hex()}
                                            for path, content in zip(paths, contents)]
                            (rows, statuses, _pairs), _comparisons = build_text_layout(
                                text_sources, rules, options, self._build_aligned_rows)
                            hunks = []
                            for row_index, row in enumerate(rows):
                                if any(line is not None and statuses[index][line] not in {"equal", "ignored"} for index, line in enumerate(row)):
                                    if hunks and row_index == hunks[-1][1] + 1:
                                        hunks[-1] = (hunks[-1][0], row_index)
                                    else:
                                        hunks.append((row_index, row_index))
                            anchors[relative] = {hunk_anchor(rows, lines, hunk) for hunk in hunks}
                except (OSError, ValueError, TypeError, InterruptedError):
                    fingerprints[relative] = None
                    anchors[relative] = set()
            messages.put(("finished", fingerprints, anchors))
        worker = SimpleNamespace(run=run, cancel_event=event)
        thread = self._comparison_thread(worker, "Restored review validation")
        self._context_jobs.append((thread, worker, messages, notes, sources, rules, key, options))
        self._background_timer.start()
        thread.start()

    def _poll_context_jobs(self):
        for job in list(self._context_jobs):
            thread, worker, messages, notes, sources, rules, key, options = job
            for message in job_messages(thread, messages):
                if message[0] == "failed" and not worker.cancel_event.is_set():
                    self._context_message("Restored notes could not be checked and remain stale; reopen Review notes to retry.")
                if message[0] != "finished" or worker.cancel_event.is_set():
                    continue
                current = self._review_sources()
                if (not current or source_key(current) != key or rules != self._active_text_rules()
                        or options != self._text_comparison_options()):
                    continue
                fingerprints, anchors = message[1:]
                for note in notes:
                    if not any(item is note for item in self.review_notes):
                        continue
                    relative = note.get("path")
                    if relative is not None and not isinstance(relative, str):
                        continue
                    dirty = any(panel.get("dirty") or panel.get("recovery_pending") for panel in self.panels
                                if panel["kind"] == "file" or (panel.get("editing_path") and relative and
                                   safe_relative(relative) and panel["editing_path"] == panel["path"] / relative))
                    note["hunk_stale"] = note.get("kind") == "hunk" and note.get("anchor") not in anchors.get(relative, set())
                    note["stale"] = dirty or note_is_stale(note, key, fingerprints.get(relative))
                self._schedule_recovery()
            if not thread.is_alive() and messages.empty():
                self._context_jobs.remove(job)

    def _reset_comparison_context(self):
        for _thread, worker, *_rest in self._context_jobs:
            worker.cancel_event.set()
        self._pending_context_view = self._pending_context_preview = self._folder_preview_target = None
        self._context_folder_restoring = False
        self._folder_view_state = []
        self._folder_expanded_paths = [set() for _ in self.panels]
        self._folder_move_group_expanded = [True for _ in self.panels]
        self.context_notice.clear()
        self.context_notice.hide()
