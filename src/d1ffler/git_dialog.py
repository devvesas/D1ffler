"""Git commit review dialog."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from d1ffler.background_tasks import BackgroundTask, job_messages

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from d1ffler.git_worktrees import (
    CommitContentMismatchError, PreviewChangedError, GitError,
    changed_paths, commit_selected, preview_selected,
)
from d1ffler.shared_sources import SourceScope

class CommitDialog(QDialog):
    def __init__(self, repo: Path, parent=None) -> None:
        super().__init__(parent)
        self.repo = repo
        self.app = parent if hasattr(parent, "shared_sources") else None
        self._reservation = None
        self.commit_hash: str | None = None
        self.changes = []
        self.preview = None
        self._messages = queue.Queue()
        self._busy = False
        self._closed = False
        self.setWindowTitle("Create Git commit")
        self.resize(900, 700)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Worktree: {repo}"))
        layout.addWidget(QLabel(
            "Choose files to commit from their saved disk contents. Already staged files are shown below; "
            "unselected staged changes stay staged and are excluded from this commit."
        ))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Staged", "Working tree"])
        self.tree.setColumnWidth(0, 470)
        layout.addWidget(self.tree, 1)
        self.rows = {}
        self.tree.itemChanged.connect(self._selection_changed)
        self.progress = QLabel("Loading changed files…")
        layout.addWidget(self.progress)
        self.review = QPushButton("Review selected patch")
        self.review.clicked.connect(self._review)
        review_row = QHBoxLayout()
        review_row.addWidget(self.review)
        refresh = QPushButton("Refresh file list")
        refresh.clicked.connect(self._refresh_status)
        review_row.addWidget(refresh)
        layout.addLayout(review_row)
        self.patch = QPlainTextEdit()
        self.patch.setReadOnly(True)
        self.patch.setPlaceholderText("Select files, then review the patch that will be committed.")
        layout.addWidget(self.patch, 2)
        layout.addWidget(QLabel("The patch uses saved disk files, including complete deletions and binary changes. "
                                "It may differ from what is currently staged."))
        layout.addWidget(QLabel("Commit message:"))
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("Explain why these changes were made…")
        self.message.setMaximumHeight(110)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.create = QPushButton("Create commit")
        self.create.setObjectName("primaryButton")
        self.create.clicked.connect(self._commit)
        buttons.addWidget(self.create)
        close = QPushButton("Cancel")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(40)
        self._start("status", changed_paths, self.repo)

    def _populate(self, changes) -> None:
        previously_selected = set(self._selected())
        self.tree.blockSignals(True)
        self.tree.clear()
        self.rows.clear()
        self.changes = changes
        for change in changes:
            row = QTreeWidgetItem([
                change.path,
                "Yes" if change.staged else "No",
                "Untracked" if change.index_status == "?" else
                "Changed" if change.worktree_status != " " else "—",
            ])
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(0, Qt.CheckState.Checked if change.path in previously_selected
                              else Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(row)
            self.rows[change.path] = row
        self.tree.blockSignals(False)
        self.progress.setText(f"{len(changes)} changed files" if changes else "No changed files in this worktree.")
        self._selection_changed()

    def _selection_changed(self) -> None:
        self.preview = None
        self.create.setEnabled(False)
        self.patch.clear()
        self.review.setEnabled(bool(self._selected()) and not self._busy)

    def _selected(self) -> list[str]:
        return [path for path, row in self.rows.items() if row.checkState(0) == Qt.CheckState.Checked]

    def _refresh_status(self) -> None:
        if self._busy:
            return
        self._selection_changed()
        self._start("status", changed_paths, self.repo)

    def _start(self, operation: str, function, *args) -> None:
        self._busy = True
        self.review.setEnabled(False)
        self.create.setEnabled(False)
        self.tree.setEnabled(False)
        self.progress.setText({"status": "Reading Git status…", "preview": "Preparing selected patch…",
                               "commit": "Creating commit; Git hooks may still be running…"}[operation])

        def run():
            try:
                self._messages.put((operation, function(*args), None))
            except GitError as exc:
                self._messages.put((operation, None, exc))
        worker = SimpleNamespace(run=run, cancel_event=threading.Event())
        self._task = (self.app._comparison_thread(worker, f"Git {operation}") if self.app else
                      BackgroundTask(None, worker, operation=f"Git {operation}"))
        self._task.start()

    def _poll(self) -> None:
        if self._closed:
            return
        messages = job_messages(self._task, self._messages, terminal=lambda _message: True)
        if not messages:
            return
        message = messages[-1]
        if message[0] == "failed":
            operation = "commit" if self._reservation is not None else "status"
            result, error = None, RuntimeError(message[1].summary)
            self.preview = None
            self.patch.clear()
        elif message[0] == "cancelled":
            operation, result, error = "status", None, RuntimeError("Cancelled")
        else:
            operation, result, error = message
        self._busy = False
        self.tree.setEnabled(True)
        if operation == "commit" and self._reservation is not None:
            self._reservation.finish()
            self._reservation = None
        if error is not None:
            self.progress.setText(str(error))
            if isinstance(error, CommitContentMismatchError):
                self.commit_hash = error.commit_hash
                QMessageBox.warning(self, "Commit content changed", str(error))
                self.accept()
                return
            if operation == "commit" and isinstance(error, PreviewChangedError):
                self.preview = None
                self.patch.clear()
                self._refresh_status()
                QMessageBox.information(self, "Review changed commit", str(error))
                return
            QMessageBox.warning(self, "Create Git commit", str(error))
        elif operation == "status":
            self._populate(result)
        elif operation == "preview":
            self.preview = result
            self.patch.setPlainText(result.patch or "No changes to commit from these saved files.")
            self.progress.setText("Review this patch, then create the commit.")
        else:
            self.commit_hash = result
            self.accept()
            return
        self.review.setEnabled(bool(self._selected()))
        self.create.setEnabled(self.preview is not None and bool(self.preview.patch))

    def _review(self) -> None:
        selected = self._selected()
        if not selected:
            return
        self.preview = None
        self.patch.clear()
        self._start("preview", preview_selected, self.repo, selected)

    def _commit(self) -> None:
        if self.preview is None or self._busy:
            return
        message = self.message.toPlainText().strip()
        if not message:
            QMessageBox.information(self, "Create Git commit", "Enter a commit message.")
            return
        if self.app is not None:
            self._reservation = self.app.shared_sources.begin(
                [SourceScope(self.repo, folder=True)], self.app, "Create Git commit", self)
            if self._reservation is None:
                return
            if self._reservation.saved:
                self._reservation.finish(notify=False)
                self._reservation = None
                self._refresh_status()
                QMessageBox.information(self, "Review changed commit", "Open edits were saved. Review the patch again before committing.")
                return
        self._start("commit", commit_selected, self.repo, list(self.preview.paths), message,
                    None, self.preview)

    def reject(self) -> None:
        if not self._busy or self.progress.text() != "Creating commit; Git hooks may still be running…":
            super().reject()

    def done(self, result):
        self._closed = True
        self._timer.stop()
        if hasattr(self, "_task"):
            self._task.worker.cancel_event.set()
        super().done(result)
