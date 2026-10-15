"""Search controls and results for a folder comparison."""

from __future__ import annotations

from d1ffler.background_tasks import job_messages

import queue
import threading

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from d1ffler.file_content import inspect_file_content
from d1ffler.folder_content_search import (
    MAX_SEARCH_FILE_BYTES, MAX_SEARCH_HITS, MAX_SEARCH_TOTAL_BYTES,
    FolderContentSearchWorker, SearchTask,
)


class FolderContentSearchDialog(QDialog):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.worker = None
        self.thread = None
        self.messages = None
        self.cancel_event = None
        self.generation = None
        self.stale = False
        self.setWindowTitle("Search folder contents")
        self.resize(980, 640)
        layout = QVBoxLayout(self)
        query_row = QHBoxLayout()
        query_row.addWidget(QLabel("Find text:"))
        self.query = QLineEdit(app._content_search_query)
        self.query.setPlaceholderText("Literal text in folder files…")
        self.query.returnPressed.connect(self._start)
        query_row.addWidget(self.query, 1)
        self.case = QCheckBox("Case sensitive")
        query_row.addWidget(self.case)
        layout.addLayout(query_row)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Scope:"))
        self.scope = QComboBox()
        self.scope.addItem("All discovered paths", "all")
        self.scope.addItem("Current path and status filter", "visible")
        scope_row.addWidget(self.scope)
        scope_row.addWidget(QLabel("Panels:"))
        self.panel_checks = {}
        for index, panel in enumerate(app.panels):
            if panel["kind"] != "folder":
                continue
            check = QCheckBox(panel["name"].text())
            check.setChecked(True)
            self.panel_checks[index] = check
            scope_row.addWidget(check)
        scope_row.addStretch()
        layout.addLayout(scope_row)
        limit = (f"Up to {MAX_SEARCH_FILE_BYTES // (1024 * 1024)} MiB per file, "
                 f"{MAX_SEARCH_TOTAL_BYTES // (1024 * 1024)} MiB per search, "
                 f"{MAX_SEARCH_HITS:,} displayed hits. Supported ZIP entries are searched read-only.")
        layout.addWidget(QLabel(limit))
        controls = QHBoxLayout()
        self.search_button = QPushButton("Search contents")
        self.search_button.clicked.connect(self._start)
        controls.addWidget(self.search_button)
        self.cancel_button = QPushButton("Cancel search")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        controls.addWidget(self.cancel_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Relative path", "Panel", "Line", "Column", "Excerpt"])
        self.results.setColumnWidth(0, 300)
        self.results.setColumnWidth(1, 110)
        self.results.setColumnWidth(2, 65)
        self.results.setColumnWidth(3, 70)
        self.results.itemDoubleClicked.connect(self._open_hit)
        layout.addWidget(self.results, 1)
        self.summary = QLabel("Enter a query and choose Search contents.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Double-click a result to open its file at the match."))
        bottom.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)
        self.query.setFocus()

    def _snapshot(self, selected_panels: list[int]) -> tuple[list[SearchTask], int, bool]:
        rows = self.app._folder_rows
        if self.scope.currentData() == "visible":
            relatives = {relative for relative, statuses in rows.items()
                         if self.app._folder_row_visible(relative, statuses)}
            for move in self.app._folder_moves:
                if self.app._folder_move_visible(move):
                    relatives.update((move.from_path, move.to_path))
        else:
            relatives = set(rows)
        folder_indices = [index for index, panel in enumerate(self.app.panels)
                          if panel["kind"] == "folder"]
        maps = self.app._folder_entry_maps
        tasks = []
        pending = 0
        for relative in sorted(relatives, key=str.casefold):
            for index in selected_panels:
                position = folder_indices.index(index)
                if position >= len(maps):
                    pending += 1
                    continue
                entry = maps[position].get(relative)
                if entry is None and rows.get(relative) is None:
                    pending += 1
                    continue
                link_entries = self.app._folder_links.get(relative, [])
                link = link_entries[position] if position < len(link_entries) else None
                followed = bool(self.app.settings.get("follow_internal_links") and
                                link is not None and link.state == "internal" and not link.directory)
                tasks.append(SearchTask(relative, index, self.app.panels[index]["name"].text(),
                                        entry, linked=link is not None and not followed))
        partial = self.app._folder_worker is not None or self.app._folder_partial or pending > 0
        return tasks, pending, partial

    def _start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        query = self.query.text()
        panels = [index for index, check in self.panel_checks.items() if check.isChecked()]
        if not query:
            self.summary.setText("Enter text to search.")
            return
        if not panels:
            self.summary.setText("Select at least one folder panel.")
            return
        self.app._content_search_query = query
        tasks, pending, partial = self._snapshot(panels)
        self.results.clear()
        self.generation = self.app._folder_generation
        self.stale = bool(self.app._folder_stale)
        self.messages = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = FolderContentSearchWorker(
            tasks, query, self.case.isChecked(), self.cancel_event, self.messages,
            pending=pending, snapshot_partial=partial)
        self.thread = self.app._comparison_thread(self.worker, "Folder content search")
        self.search_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.summary.setText(f"Searching {len(tasks):,} file entries…" +
                             (" Snapshot is partial." if partial else ""))
        self.timer.start()
        self.thread.start()

    def _cancel(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.summary.setText("Cancelling content search…")

    def _poll(self):
        if self.generation is not None and (
                self.app._folder_generation != self.generation or self.app._folder_stale):
            self.stale = True
        for message in job_messages(self.thread, self.messages, limit=64) if self.messages is not None else []:
            kind = message[0]
            if kind == "hits":
                for hit in message[1]:
                    item = QTreeWidgetItem([
                        hit.relative, hit.panel_name, str(hit.line), str(hit.column), hit.excerpt])
                    item.setData(0, Qt.ItemDataRole.UserRole, hit)
                    self.results.addTopLevelItem(item)
            elif kind == "progress":
                self.summary.setText(f"Searched {message[1]:,}/{message[2]:,} entries · "
                                     f"{message[3]:,} hits" + (" · stale" if self.stale else ""))
            elif kind == "finished":
                counts, scanned, total_bytes, hits, partial = message[1:]
                skipped = ", ".join(f"{count:,} {name}" for name, count in counts.items()
                                    if name not in {"searched"} and count)
                self.summary.setText(
                    f"{hits:,} hits · {counts.get('searched', 0):,} text entries searched · "
                    f"{total_bytes / (1024 * 1024):.1f} MiB read"
                    + (f" · skipped: {skipped}" if skipped else "")
                    + (" · partial" if partial else "")
                    + (" · stale; search again" if self.stale else ""))
                self.search_button.setEnabled(True)
                self.cancel_button.setEnabled(False)
                self.timer.stop()
            elif kind == "error":
                self.summary.setText(f"Content search failed: {message[1]}")
                self.search_button.setEnabled(True)
                self.cancel_button.setEnabled(False)
                self.timer.stop()
            elif kind in {"failed", "cancelled"}:
                self.stale = True
                self.summary.setText("Content search failed · partial; Search again to retry. " + message[1].summary
                                     if kind == "failed" else "Content search cancelled · partial")
                self.search_button.setEnabled(True)
                self.cancel_button.setEnabled(False)
                self.timer.stop()
        if self.thread is not None and not self.thread.is_alive() and self.timer.isActive() and self.messages.empty():
            self.timer.stop()
            self.search_button.setEnabled(True)
            self.cancel_button.setEnabled(False)

    def _open_hit(self, item: QTreeWidgetItem, _column: int):
        hit = item.data(0, Qt.ItemDataRole.UserRole)
        if self.stale or self.app._folder_generation != self.generation or self.app._folder_stale:
            self.summary.setText("Results are stale. Search again after refreshing the folder comparison.")
            return
        try:
            current = inspect_file_content(hit.entry, preview_limit=True)
            if current.fingerprint != hit.fingerprint or current.text is None:
                self.stale = True
                self.summary.setText("This file changed since the search. Search again.")
                return
        except (OSError, UnicodeError) as exc:
            self.stale = True
            self.summary.setText(f"This file is unavailable: {exc}")
            return
        if not self.app._open_folder_content_hit(hit):
            self.summary.setText("Could not open the match. Save or discard open edits if prompted.")
            return
        self.accept()

    def done(self, result: int):
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.timer.stop()
        if self.thread is not None:
            self.thread.join()
        super().done(result)
