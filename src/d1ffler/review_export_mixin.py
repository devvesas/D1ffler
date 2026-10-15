"""Review notes and comparison report actions for the main window."""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

from d1ffler.background_tasks import job_messages

from PyQt6.QtCore import QEventLoop, QTimer, Qt
from PyQt6.QtWidgets import QDialog, QFileDialog, QMessageBox, QProgressDialog

from d1ffler.comparison_export import (
    ExportCancelled, folder_csv_chunks, folder_html_chunks,
    text_html_chunks, text_unified_chunks, write_export,
)
from d1ffler.folder_archives import ArchiveIssue, is_zip_source
from d1ffler.review_dialogs import FingerprintDialog, ReviewNoteDialog, ReviewNotesDialog
from d1ffler.review_notes import (
    hunk_anchor, note_is_stale, report_text, source_key, unified_excerpt,
)
from d1ffler.ui_dialogs import ComparisonExportDialog, ReviewReportDialog


class ReviewExportMixin:
    def _notes_with_buffer_status(self):
        notes = [dict(note) for note in self.review_notes]
        for note in notes:
            relative = note.get("path")
            note["buffer_stale"] = any(
                (panel.get("dirty") or panel.get("recovery_pending")) and
                (panel["kind"] == "file" or (panel["kind"] == "folder" and isinstance(relative, str) and relative and
                 panel.get("editing_path") == panel["path"] / relative)) for panel in self.panels)
        return notes

    def _review_sources(self) -> list[dict] | None:
        populated = [panel for panel in self.panels if panel["kind"]]
        if len(populated) < 2 or any(panel["kind"] not in {"file", "folder"} for panel in populated):
            return None
        return self._workspace_sources()

    def _review_fingerprints(self, sources: list[dict],
                             relatives: list[str | None]) -> dict[str | None, str | None] | None:
        dialog = FingerprintDialog(sources, relatives, self)
        return dialog.results if dialog.exec() == QDialog.DialogCode.Accepted else None

    def _review_file_path(self, relative: str | None) -> None:
        if relative is None:
            return
        self._edit_review_target("path", relative)

    def _review_hunk(self, hunk: tuple[int, int] | None) -> None:
        if hunk is None or not self._aligned_rows:
            return
        relative = None
        if self._comparison_mode() == "folder":
            for panel in self.panels:
                editing_path = panel.get("editing_path")
                if panel["kind"] == "folder" and editing_path is not None:
                    try:
                        relative = editing_path.relative_to(panel["path"]).as_posix()
                    except ValueError:
                        return
                    break
            if relative is None:
                return
        panels = ([panel for panel in self.panels if panel["kind"] == "folder"]
                  if self._comparison_mode() == "folder" else self.panels)
        lines = [self._panel_text(panel).splitlines() for panel in panels]
        anchor = hunk_anchor(self._aligned_rows, lines, hunk)
        base_start, base_end = self._hunk_line_span(0, hunk)
        self._edit_review_target("hunk", relative, anchor=anchor,
                                 base_start=base_start, base_end=base_end)

    def _edit_review_target(self, kind: str, relative: str | None, **hunk_data) -> None:
        sources = self._review_sources()
        if sources is None:
            QMessageBox.information(self, "Review note",
                                    "Add at least two file or folder sources before saving review notes.")
            return
        if any(panel.get("dirty") for panel in self.panels if panel["kind"] in {"file", "folder"}):
            QMessageBox.information(self, "Review note",
                                    "Save or discard open edits before attaching a note to file content.")
            return
        fingerprints = self._review_fingerprints(sources, [relative])
        if fingerprints is None:
            return
        fingerprint = fingerprints.get(relative)
        if fingerprint is None:
            QMessageBox.warning(self, "Review note", "Could not read the selected file for review.")
            return
        key = source_key(sources)
        existing = next((note for note in self.review_notes
                         if note.get("source_key") == key and note.get("kind") == kind
                         and note.get("path") == relative
                         and (kind == "path" or note.get("anchor") == hunk_data.get("anchor"))), None)
        stale = existing is not None and note_is_stale(existing, key, fingerprint)
        editor = ReviewNoteDialog(existing, stale=stale, can_reattach=stale, parent=self)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        if existing is None:
            existing = {"id": uuid4().hex, "kind": kind, "path": relative,
                        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.review_notes.append(existing)
        existing.update({"state": editor.state.currentText(),
                         "text": editor.body.toPlainText().strip(),
                         "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if not stale or editor.reattach.isChecked():
            existing.update({"source_key": key, "fingerprint": fingerprint})
            existing["hunk_stale"] = False
            existing["stale"] = False
        existing.update(hunk_data)
        self._record_recent_workspace()
        self._schedule_recovery()
        self.status.setText("Review note saved")

    def _show_review_notes(self) -> None:
        if not self.review_notes:
            QMessageBox.information(self, "Review notes", "No review notes in this workspace yet.")
            return
        sources = self._review_sources()
        if sources is None:
            QMessageBox.information(self, "Review notes", "Open the comparison sources to review notes.")
            return
        key = source_key(sources)
        relatives = [note.get("path") for note in self.review_notes if note.get("source_key") == key]
        fingerprints = self._review_fingerprints(sources, relatives)
        if fingerprints is None:
            return
        dialog = ReviewNotesDialog(self._notes_with_buffer_status(), key, fingerprints, self)
        dialog.exec()
        if dialog.changed:
            self.review_notes = dialog.notes
            self._record_recent_workspace()
            self._schedule_recovery()

    def _review_report_context(self, sources: list[dict]) -> dict:
        moves = []
        if self._comparison_mode() == "folder":
            statuses = list(self._folder_rows.values())
            folder_panels = [panel for panel in self.panels if panel["kind"] == "folder"]
            moves = [{"from_panel": folder_panels[match.from_panel]["name"].text(),
                      "from_path": match.from_path,
                      "to_panel": folder_panels[match.to_panel]["name"].text(),
                      "to_path": match.to_path,
                      "sha256": match.sha256} for match in self._folder_moves]
            counts = {
                "Files discovered": len(statuses),
                "Files changed": sum(item is not None and (item[0] == "error" or any(
                                     value not in {"identical", "ignored"} for value in item[1:]))
                                     for item in statuses) - (len(moves) if len(folder_panels) == 2 else 0),
                "Detected exact moves": len(moves),
                "Files pending": sum(item is None for item in statuses),
                "Metadata-only paths": sum(item is not None and "metadata" in item[1:] for item in statuses),
                "Linked paths": len(self._folder_links),
                "Comparison partial": self._folder_partial or self._folder_worker is not None,
            }
        else:
            counts = {"Text hunks": len(self._hunks),
                      "Text comparison": "; ".join(result["summary"] for result in self._text_comparisons) or "pending"}
        return {"sources": sources,
                "rules": {"Excluded paths": self.settings["excluded_paths"] or "none",
                          "Use .gitignore": self.settings["ignore_gitignore"],
                          "Follow internal links": self.settings.get("follow_internal_links", False),
                          "Ignore whitespace": self.settings["ignore_whitespace"],
                          "Ignore line endings": self.settings["ignore_line_endings"],
                          "Compare file metadata": self.metadata_compare_rule,
                          "Text masks": "; ".join(f"{r['name']} ({r['scope']}: {r['pattern']})"
                                                   for r in self._active_text_rules()) or "none"},
                "counts": counts, "moves": moves}

    def _review_excerpt(self, note: dict, sources: list[dict]) -> str:
        if note.get("kind") != "hunk" or note.get("stale"):
            return ""
        relative = note.get("path")
        paths = []
        for source in sources:
            path = Path(str(source["path"]))
            if source["kind"] == "folder":
                if not relative:
                    return ""
                path = path / Path(str(relative))
            paths.append(path)
        excerpts = []
        for source, path in zip(sources[1:], paths[1:]):
            try:
                excerpt = unified_excerpt(paths[0], path, int(note.get("base_start", 0)))
            except (OSError, ValueError):
                continue
            if excerpt:
                excerpts.append(f"# {sources[0]['name']} → {source['name']}\n{excerpt}")
        return "\n".join(excerpts)

    def _export_review_report(self) -> None:
        sources = self._review_sources()
        if sources is None:
            QMessageBox.information(self, "Review report",
                                    "Add at least two file or folder sources before exporting a report.")
            return
        key = source_key(sources)
        current_notes = [note for note in self._notes_with_buffer_status() if note.get("source_key") == key]
        fingerprints = self._review_fingerprints(sources, [note.get("path") for note in current_notes])
        if fingerprints is None:
            return
        for note in current_notes:
            note["stale"] = note_is_stale(note, key, fingerprints.get(note.get("path")))
        options = ReviewReportDialog(current_notes, self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        selected_ids = options.selected_ids()
        selected = [note for note in current_notes if str(note["id"]) in selected_ids]
        html_format = options.format.currentText() == "HTML"
        suffix = ".html" if html_format else ".md"
        filename, _filter = QFileDialog.getSaveFileName(
            self, "Export review report", f"D1ffler-review-report{suffix}",
            "HTML files (*.html)" if html_format else "Markdown files (*.md)")
        if not filename:
            return
        excerpts = ({str(note["id"]): self._review_excerpt(note, sources) for note in selected}
                    if options.excerpts.isChecked() else {})
        content = report_text(self._review_report_context(sources), selected,
                              html_format=html_format, excerpts=excerpts)
        try:
            Path(filename).write_text(content, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Review report", f"Could not export report:\n{exc}")
            return
        self.status.setText(f"Exported review report: {Path(filename).name}")

    def _comparison_export_snapshot(self, folder_mode: bool, visible_only: bool,
                                    include_paths: bool, include_metadata: bool = False) -> dict:
        panels = ([panel for panel in self.panels if panel["kind"] == "folder"]
                  if folder_mode else self.panels)
        sources = []
        for panel in panels:
            display_name = panel["name"].text()
            display_path = (panel["path"] if folder_mode else
                            panel.get("editing_path") or
                            (panel["path"] if panel["kind"] == "file" else None))
            if not panel.get("custom_name") and display_path is not None:
                display_name += f" · {display_path.name}"
            source = {"name": display_name}
            if folder_mode:
                source["type"] = "ZIP archive" if is_zip_source(panel["path"]) else "folder"
            if include_paths:
                if display_path is not None:
                    source["path"] = str(display_path.resolve())
            if not folder_mode:
                source.update(self._text_comparison_sources([panel])[0])
            sources.append(source)
        if not folder_mode:
            return {"sources": sources, "text_rules": self._active_text_rules(),
                    **self._text_comparison_options()}
        hidden = self._folder_move_hidden_paths if len(panels) == 2 else set()
        rows = [(relative, list(statuses) if statuses is not None else None)
                for relative, statuses in self._folder_rows.items()
                if relative not in hidden and
                (not visible_only or self._folder_row_visible(relative, statuses))]
        rows.sort(key=lambda row: row[0].casefold())
        matches = [match for match in self._folder_moves
                   if not visible_only or self._folder_move_visible(match)]
        moves = [{"from_panel": match.from_panel, "from_path": match.from_path,
                  "to_panel": match.to_panel, "to_path": match.to_path, "sha256": match.sha256}
                 for match in matches]
        if visible_only:
            filters = []
            if self._folder_path_query:
                filters.append(f"path contains '{self._folder_path_query}'")
            if self._folder_status_filter != "all":
                filters.append(f"status {self._folder_status_filter}")
            if self.show_changed_files_only:
                filters.append("Changed only")
            scope = "Current view" + (" (" + ", ".join(filters) + ")" if filters else "")
        else:
            scope = "All comparison rows"
        return {"sources": sources, "rows": rows, "moves": moves,
                "include_paths": include_paths,
                "include_metadata": include_metadata,
                "metadata": {relative: self._folder_metadata[relative]
                             for relative, _statuses in rows if include_metadata and relative in self._folder_metadata},
                "links": {relative: [link.as_dict() if link else None for link in self._folder_links[relative]]
                          for relative, _statuses in rows if relative in self._folder_links},
                "archive_issues": {
                    relative: [entry.detail if isinstance(entry, ArchiveIssue) else ""
                               for entry in (entries.get(relative) for entries in self._folder_entry_maps)]
                    for relative, _statuses in rows
                    if any(isinstance(entries.get(relative), ArchiveIssue)
                           for entries in self._folder_entry_maps)
                },
                "scope": scope, "discovered": len(self._folder_rows),
                "excluded": self._folder_excluded,
                "partial": self._folder_partial or self._folder_worker is not None or
                           any(statuses is None for statuses in self._folder_rows.values()),
                "stale": self._folder_stale,
                "rules": {"Excluded paths": self.settings["excluded_paths"] or "none",
                          "Use .gitignore": self.settings["ignore_gitignore"],
                          "Follow internal links": self.settings.get("follow_internal_links", False),
                          "ZIP archives": "Read-only; .gitignore is not applied inside ZIPs; "
                                          "64 MiB per entry and 512 MiB total limit"
                                          if any(is_zip_source(panel["path"]) for panel in panels) else "none",
                          "Ignore whitespace": self.settings["ignore_whitespace"],
                          "Ignore line endings": self.settings["ignore_line_endings"],
                          "Compare file metadata": self.metadata_compare_rule,
                          "Metadata details included": include_metadata,
                          "Text masks": "; ".join(f"{r['name']} ({r['scope']}: {r['pattern']})"
                                                   for r in self._active_text_rules()) or "none"}}

    def _run_comparison_export(self, path: Path, chunks_factory, total: int) -> bool:
        cancel_event = threading.Event()
        messages = queue.Queue()
        progress = QProgressDialog("Writing comparison report…", "Cancel", 0, max(1, total), self)
        progress.setWindowTitle("Export comparison")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(cancel_event.set)
        loop = QEventLoop(self)
        timer = QTimer(self)
        timer.setInterval(40)
        outcome = {"error": None, "done": False}

        def run() -> None:
            try:
                write_export(path, chunks_factory(cancel_event.is_set), cancel_event.is_set,
                             lambda count: messages.put(("progress", count)))
                messages.put(("finished", None))
            except (OSError, ExportCancelled) as exc:
                messages.put(("finished", exc))

        def poll() -> None:
            for kind, value in job_messages(thread, messages, limit=64):
                if kind == "progress":
                    progress.setValue(min(total, value))
                else:
                    if kind == "failed":
                        value = RuntimeError(value.summary)
                    elif kind == "cancelled":
                        value = ExportCancelled()
                    outcome["error"] = value
                    outcome["done"] = True
                    if value is None:
                        progress.setValue(max(1, total))
                    timer.stop()
                    progress.close()
                    loop.quit()

        timer.timeout.connect(poll)
        thread = self._comparison_thread(SimpleNamespace(run=run, cancel_event=cancel_event), "Comparison export")
        timer.start()
        thread.start()
        progress.show()
        loop.exec()
        thread.join()
        progress.deleteLater()
        timer.deleteLater()
        if isinstance(outcome["error"], ExportCancelled):
            self.status.setText("Comparison export cancelled")
            return False
        if outcome["error"] is not None:
            QMessageBox.warning(self, "Export comparison", f"Could not export comparison:\n{outcome['error']}")
            return False
        self.status.setText(f"Exported comparison: {path.name}")
        return True

    def _export_comparison(self) -> None:
        folder_mode = self._comparison_mode() == "folder" and self.folder_back_button.isHidden()
        panels = ([panel for panel in self.panels if panel["kind"] == "folder"]
                  if folder_mode else self.panels)
        if len(panels) < 2 or not any(panel["kind"] for panel in panels):
            QMessageBox.information(self, "Export comparison", "Add at least two comparison sources first.")
            return
        if not folder_mode and any(panel.get("binary") for panel in panels):
            QMessageBox.information(self, "Export comparison",
                                    "Binary previews cannot be exported as a text diff.")
            return
        partial = folder_mode and (self._folder_partial or self._folder_worker is not None or
                                   any(statuses is None for statuses in self._folder_rows.values()))
        options = ComparisonExportDialog(folder_mode, partial, self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        format_name = str(options.format.currentData())
        visible_only = folder_mode and options.scope.currentData() == "visible"
        snapshot = self._comparison_export_snapshot(folder_mode, visible_only,
                                                    options.include_paths.isChecked(),
                                                    options.include_metadata.isChecked())
        suffix = {"csv": ".csv", "html": ".html", "diff": ".diff"}[format_name]
        filename, _filter = QFileDialog.getSaveFileName(
            self, "Export comparison", f"D1ffler-comparison{suffix}",
            {"csv": "CSV files (*.csv)", "html": "HTML files (*.html)",
             "diff": "Diff files (*.diff *.patch)"}[format_name])
        if not filename:
            return
        path = Path(filename)
        if not path.suffix:
            path = path.with_suffix(suffix)
        resolved_target = path.resolve()
        for panel in self.panels:
            source_path = panel.get("path")
            if panel["kind"] == "folder" and source_path is not None and (
                resolved_target == source_path.resolve() or
                resolved_target.is_relative_to(source_path.resolve())
            ):
                QMessageBox.warning(self, "Export comparison",
                                    "Choose a report location outside the compared folders.")
                return
            source_file = panel.get("editing_path") or (source_path if panel["kind"] == "file" else None)
            if source_file is not None and resolved_target == source_file.resolve():
                QMessageBox.warning(self, "Export comparison",
                                    "A report cannot replace a compared source file.")
                return
        if path.exists():
            choice = QMessageBox.warning(
                self, "Replace export", f"Replace existing report {path}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Yes:
                return
        if folder_mode:
            chunks_factory = (lambda cancelled: folder_csv_chunks(snapshot, cancelled) if format_name == "csv"
                              else folder_html_chunks(snapshot, cancelled))
            total = len(snapshot["rows"]) + len(snapshot["moves"])
        else:
            chunks_factory = (lambda cancelled: (text_unified_chunks if format_name == "diff" else text_html_chunks)(
                snapshot["sources"], cancelled, snapshot["text_rules"], snapshot["ignore_whitespace"], snapshot["ignore_line_endings"]))
            baseline = snapshot["sources"][0]["text"].splitlines()
            total = sum(max(len(baseline), len(source["text"].splitlines()))
                        for source in snapshot["sources"][1:])
        self._run_comparison_export(path, chunks_factory, total)
