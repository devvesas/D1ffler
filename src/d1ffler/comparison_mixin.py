"""Text, binary, and folder comparison lifecycle for the main window."""

from __future__ import annotations

import difflib
import json
import queue
import threading
import time

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QTextEdit

from d1ffler.comparison_models import FOLDER_STATUS_FILTERS, MoveMatch
from d1ffler.background_tasks import job_messages
from d1ffler.comparison_workers import (
    BinarySummaryWorker, FolderComparisonWorker,
)
from d1ffler.editor_widgets import LineNumberTextEdit
from d1ffler.image_compare import ImageDecodeWorker, difference_image, is_image_source
from d1ffler.text_rules import RuleError, applicable_rules
from d1ffler.text_comparison import build_text_layout, line_key
from d1ffler.editor_history import utf16_length


class ComparisonMixin:
    def _clear_comparison_failure(self, kind):
        self._background_failures.pop(kind, None)
        self._update_failure_notice()

    def _update_failure_notice(self):
        self.failure_notice.setText("\n".join(self._background_failures.values()))
        self.failure_notice.setVisible(bool(self._background_failures))

    def _comparison_failed(self, kind, generation, worker, failure, panels=None, lines=None):
        summary = failure.summary if hasattr(failure, "summary") else str(failure)
        if kind == "text":
            self._text_finished(generation, worker, panels, lines, None)
        if self._closed or generation != getattr(self, f"_{kind}_generation"):
            return
        if worker.cancel_event.is_set():
            if kind == "folder":
                self._folder_finished(generation, worker, True, "")
            elif getattr(self, f"_{kind}_worker") is worker:
                setattr(self, f"_{kind}_worker", None)
            return
        if kind == "folder":
            self._folder_finished(generation, worker, False, summary)
        elif kind == "binary":
            self._binary_worker = None
            self._binary_results = None
            self._binary_job_key = None
            binary_panels = [panel for panel in self.panels if panel.get("binary") and not panel.get("dirty")]
            self._show_binary_results(binary_panels, ["Binary comparison failed. Refresh to retry."] * len(binary_panels))
        elif kind == "image":
            self._image_worker = None
            self._image_results = None
            self._image_job_key = None
            self._image_difference_cache = {}
            self.image_controls.hide()
            for panel in self._image_panel_refs:
                panel["view_stack"].setCurrentIndex(0)
            if self._binary_results is not None:
                self._show_binary_results(self._image_panel_refs, self._binary_results)
        elif kind == "text":
            self._change_rows, self._hunks, self._aligned_rows = [], [], []
            self._change_index = -1
            for panel in panels:
                panel["editor"].setExtraSelections([])
            self._update_change_navigation()
            self._text_comparisons = []
            self.text_comparison_notice.setText("Text comparison failed · incomplete; Refresh to retry.")
            self.text_comparison_notice.show()
        message = f"{kind.title()} comparison failed · incomplete; Refresh to retry. {summary}"
        self._background_failures[kind] = message
        self._update_failure_notice()
        self.status.setText(f"{kind.title()} comparison failed · incomplete")

    def _handle_gui_failure(self, failure):
        if self._closed:
            return
        self._cancel_text_diff()
        self._cancel_binary_summary()
        self._cancel_image_comparison()
        self._cancel_folder_comparison()
        if self._comparison_mode() == "folder":
            self._folder_partial = True
            self._folder_stale = True
            self._update_folder_counts_label()
        message = f"An action failed. Review its results before retrying. {failure.summary}"
        self._background_failures["action"] = message
        self._update_failure_notice()
        try:
            backed_up = self._write_recovery_snapshot()
        except Exception as exc:
            self.diagnostics.record_exception("Recovery after GUI failure", exc)
            backed_up = False
        if not backed_up:
            message += " Unsaved edits could not be backed up; save or copy them before closing."
            self._background_failures["action"] = message
            self._update_failure_notice()
        self.status.setText("Action failed · review before retrying" if backed_up else
                            "Action failed · save or copy open edits")

    def _refresh_diff(self) -> None:
        if self._rendering_diff or self._closed or self._loading_workspace:
            return
        self._rendering_diff = True
        try:
            self._refresh_diff_now()
            self._restore_context_text_view()
        finally:
            self._rendering_diff = False
            self.comparison_changed.emit()

    def _refresh_diff_now(self) -> None:
        mode = self._comparison_mode()
        self._sync_search_bar_visibility()
        self.folder_triage_bar.setVisible(mode == "folder" and self.folder_back_button.isHidden())
        if mode != "folder" and self._folder_job_key is not None:
            self._cancel_folder_comparison(superseded=True)
            self._folder_generation += 1
            self._folder_job_key = None
        self.changed_files_button.setEnabled(mode == "folder")
        populated = [panel for panel in self.panels if panel["kind"]]
        if mode == "folder":
            if not self.folder_back_button.isHidden():
                self._render_panels_text_diff(populated)
                return
            self._change_rows = []
            self.text_comparison_notice.hide()
            self._text_comparisons = []
            self._change_index = -1
            self._hunks = []
            self._aligned_rows = []
            self._update_change_navigation()
            self._render_panels_folder_diff(populated)
        else:
            self._render_panels_text_diff(self.panels)

    def _render_panels_text_diff(self, panels: list[dict]) -> None:
        if any(panel.get("binary") for panel in panels):
            self.text_comparison_notice.hide()
            self._text_comparisons = []
            self._cancel_text_diff()
            self._render_binary_comparison(panels)
            return
        self._cancel_image_comparison()
        self.image_controls.hide()
        for panel in panels:
            if panel["view_stack"].currentIndex() == 2:
                panel["view_stack"].setCurrentIndex(0)
        self._cancel_binary_summary()
        if not any(panel["kind"] for panel in panels):
            self.text_comparison_notice.hide()
            self._text_comparisons = []
            self._cancel_text_diff()
            self._change_rows = []
            self._change_index = -1
            self._hunks = []
            self._aligned_rows = []
            self._update_change_navigation()
            for panel in panels:
                panel["editor"].setExtraSelections([])
                lines = self._panel_text(panel).splitlines()
                self._set_editor_alignment(panel["editor"], list(range(len(lines))), lines)
            return

        all_lines = [self._panel_text(panel).splitlines() for panel in panels]
        if sum(len(line) for lines in all_lines for line in lines) > 65536:
            rules_by_panel = []
            for panel in panels:
                path = panel.get("editing_path") or panel.get("path")
                rules_by_panel.append(applicable_rules(self._active_text_rules(), path.name if path else ""))
            self._start_text_diff(panels, all_lines, rules_by_panel=rules_by_panel)
            return
        try:
            layout, comparisons = build_text_layout(self._text_comparison_sources(panels), self._active_text_rules(),
                                                    self._text_comparison_options(), self._build_aligned_rows)
        except RuleError as exc:
            layout, comparisons = self._build_aligned_rows(all_lines), None
            self.text_comparison_notice.setText(f"Text rule error: {exc} · Showing raw content differences; equivalence is incomplete.")
            self.text_comparison_notice.show()
        self._cancel_text_diff()
        self._apply_text_diff_layout(panels, all_lines, layout, comparisons)

    def _text_comparison_options(self):
        return {"ignore_whitespace": bool(self.settings["ignore_whitespace"]),
                "ignore_line_endings": bool(self.settings["ignore_line_endings"])}

    def _text_comparison_sources(self, panels=None):
        sources = []
        for panel in self.panels if panels is None else panels:
            path = panel.get("editing_path") or panel.get("image_source") or panel.get("preview_path") or panel.get("path")
            sources.append({"text": self._panel_text_with_line_endings(panel),
                            "scope": path.name if path else "", "encoding": panel.get("encoding") or "utf-8",
                            "bom": panel.get("bom", b"").hex(),
                            "file_backed": (panel.get("editing_path") is not None or panel.get("image_source") is not None)
                                           and not panel.get("dirty")})
        return sources

    def _normalized_panel_lines(self, panels, all_lines):
        result = []
        for panel, lines in zip(panels, all_lines):
            path = panel.get("editing_path") or panel.get("path")
            scope = str(path.name) if path else ""
            rules = applicable_rules(self._active_text_rules(), scope)
            result.append([line_key(line, rules, self.settings["ignore_whitespace"])[0] for line in lines])
        return result

    def _apply_text_diff_layout(self, panels, all_lines, result, comparisons=None) -> None:
        self._text_comparisons = comparisons or []
        if comparisons is not None:
            messages = []
            sources = self._text_comparison_sources(panels)
            for index, comparison in enumerate(comparisons, 1):
                summary = comparison["summary"]
                if comparison["status"] == "identical" and sources[0]["file_backed"] and sources[index]["file_backed"]:
                    summary = "Byte-identical · " + summary
                messages.append(f"{panels[0]['name'].text()} → {panels[index]['name'].text()}: {summary}")
            if any(panel.get("dirty") for panel in panels):
                messages.append("Comparing unsaved buffers; folder results describe the saved files."
                                if self._comparison_mode() == "folder" else "Comparing unsaved buffers.")
            self.text_comparison_notice.setText("\n".join(messages))
            self.text_comparison_notice.setVisible(bool(messages))
        aligned_rows, line_statuses, inline_pairs = result
        changed_rows = [
            row_index for row_index, row in enumerate(aligned_rows)
            if any(
                line is not None and line_statuses[panel_index][line] not in {"equal", "ignored"}
                for panel_index, line in enumerate(row)
            )
        ]
        self._aligned_rows = aligned_rows
        self._hunks = []
        for row in changed_rows:
            if not self._hunks or row > self._hunks[-1][1] + 1:
                self._hunks.append((row, row))
            else:
                self._hunks[-1] = (self._hunks[-1][0], row)
        self._change_rows = [start for start, _end in self._hunks]
        if self._change_index >= len(self._change_rows):
            self._change_index = -1
        self._update_change_navigation()
        colors = self._diff_colors()

        # The aligned view is separate from the logical document's undo history.
        visual_line_numbers: list[dict[int, int]] = []
        for panel_index, (panel, lines) in enumerate(zip(panels, all_lines)):
            panel_rows = [row[panel_index] for row in aligned_rows]
            self._set_editor_alignment(panel["editor"], panel_rows, lines)
            visual_line_numbers.append({line: row for row, line in enumerate(panel_rows) if line is not None})

        for panel_index, (panel, lines) in enumerate(zip(panels, all_lines)):
            editor = panel["editor"]
            selections = []
            for line_number, status in enumerate(line_statuses[panel_index]):
                if status == "equal":
                    continue
                block = editor.document().findBlockByNumber(visual_line_numbers[panel_index][line_number])
                if not block.isValid():
                    continue
                line_selection = QTextEdit.ExtraSelection()
                line_selection.cursor = editor.textCursor()
                line_selection.cursor.setPosition(block.position())
                line_selection.cursor.movePosition(line_selection.cursor.MoveOperation.EndOfBlock, line_selection.cursor.MoveMode.KeepAnchor)
                line_selection.format.setBackground(QColor(colors[status]))
                selections.append(line_selection)
            for other_panel, base_line, target_line in inline_pairs[panel_index]:
                current_line = base_line if panel_index == 0 else target_line
                other_text = all_lines[other_panel][target_line] if panel_index == 0 else all_lines[0][base_line]
                path = panel.get("editing_path") or panel.get("path")
                rules = applicable_rules(self._active_text_rules(), path.name if path else "")
                try:
                    _key, ignored_spans = line_key(lines[current_line], rules, self.settings["ignore_whitespace"])
                except RuleError:
                    ignored_spans = []
                block = editor.document().findBlockByNumber(visual_line_numbers[panel_index][current_line])
                for tag, start, end, _other_start, _other_end in difflib.SequenceMatcher(
                    None, lines[current_line], other_text, autojunk=False
                ).get_opcodes():
                    if tag == "equal" or start == end:
                        continue
                    segments = [(start, end)]
                    for mask_start, mask_end in ignored_spans:
                        segments = [(a, min(b, mask_start)) for a, b in segments if a < mask_start] + [
                            (max(a, mask_end), b) for a, b in segments if b > mask_end]
                    for span_start, span_end in segments:
                        if span_start >= span_end:
                            continue
                        span = QTextEdit.ExtraSelection()
                        span.cursor = editor.textCursor()
                        span.cursor.setPosition(block.position() + utf16_length(lines[current_line][:span_start]))
                        span.cursor.setPosition(block.position() + utf16_length(lines[current_line][:span_end]), span.cursor.MoveMode.KeepAnchor)
                        span.format.setBackground(QColor(colors["inline"]))
                        selections.append(span)
            editor.setExtraSelections(selections)
        self._restore_context_text_view()

    def _start_text_diff(self, panels, all_lines, compare_lines=None, rules_by_panel=None) -> None:
        self._text_generation += 1
        self._text_pending = (list(panels), all_lines, compare_lines, rules_by_panel,
                              self._text_comparison_sources(panels), self._text_comparison_options(), self._active_text_rules())
        if self._text_worker is not None:
            self._text_worker.cancel_event.set()
            return
        self._launch_pending_text_diff()

    def _launch_pending_text_diff(self) -> None:
        if self._text_pending is None:
            return
        panels, all_lines, compare_lines, rules_by_panel, sources, options, rules = self._text_pending
        self._text_pending = None
        generation = self._text_generation
        event = threading.Event()
        messages = queue.Queue()
        worker = self.text_diff_worker_type(all_lines, event, messages, compare_lines, rules_by_panel,
                                          sources=sources, options=options, text_rules=rules)
        self._clear_comparison_failure("text")
        thread = self._comparison_thread(worker, "Text comparison")
        self._text_worker = worker
        self._hunks, self._change_rows, self._aligned_rows = [], [], []
        self._change_index = -1
        self._update_change_navigation()
        self._text_jobs.append((thread, worker, messages, generation, panels, all_lines))
        self.cancel_comparison_button.show()
        self.status.setText("Matching text lines…")
        self._text_comparisons = []
        self.text_comparison_notice.setText("Comparing text and format…")
        self.text_comparison_notice.show()
        self._background_timer.start()
        thread.start()

    def _text_finished(self, generation, worker, panels, all_lines, result) -> None:
        if self._text_worker is worker:
            self._text_worker = None
        if self._text_pending is not None:
            self._launch_pending_text_diff()
            return
        if generation != self._text_generation or result is None:
            return
        self.cancel_comparison_button.hide()
        if result.rule_error:
            self.text_comparison_notice.setText(f"Text rule error: {result.rule_error} · Showing raw content differences; equivalence is incomplete.")
        self._apply_text_diff_layout(panels, all_lines, result.layout, result.comparisons)
        self.status.setText(f"Text rule error: {result.rule_error}" if result.rule_error
                            else "Text comparison ready")
        self._position_folder_content_hit()

    def _cancel_text_diff(self) -> None:
        self._clear_comparison_failure("text")
        self._text_generation += 1
        self._text_pending = None
        if self._text_worker is not None:
            self._text_worker.cancel_event.set()
            self.cancel_comparison_button.hide()

    def _render_binary_comparison(self, panels: list[dict]) -> None:
        """Compute binary metadata without blocking the GUI thread."""
        self._change_rows = []
        self._change_index = -1
        self._hunks = []
        self._aligned_rows = []
        self._update_change_navigation()
        paths = [panel.get("image_source") or panel.get("editing_path") or
                 (panel.get("path") if panel.get("kind") == "file" else None) for panel in panels]
        self._render_image_comparison(panels, paths)
        try:
            key = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns,
                         path.stat().st_ctime_ns) if path is not None else None for path in paths)
        except OSError as exc:
            self.status.setText(f"Could not inspect binary files: {exc}")
            return
        if key == self._binary_job_key:
            if self._binary_results is not None:
                self._show_binary_results(panels, self._binary_results)
            return
        self._cancel_binary_summary()
        self._binary_job_key = key
        self._binary_results = None
        self._binary_generation += 1
        generation = self._binary_generation
        event = threading.Event()
        messages = queue.Queue()
        worker = self.binary_summary_worker_type(paths, event, messages)
        thread = self._comparison_thread(worker, "Binary comparison")
        self._binary_worker = worker
        self._binary_jobs.append((thread, worker, messages, generation))
        self._show_binary_results(panels, ["Comparing binary files…"] * len(panels))
        self.cancel_comparison_button.show()
        self.status.setText("Comparing binary files…")
        self._background_timer.start()
        thread.start()

    def _show_binary_results(self, panels: list[dict], results: list[str]) -> None:
        self._loading_panel = True
        try:
            for panel, content in zip(panels, results):
                if panel.get("dirty") or panel.get("kind") == "text":
                    continue  # A metadata placeholder must never replace an editable buffer.
                if self._image_results is not None and self._image_panel_refs == panels:
                    image_result = self._image_results[panels.index(panel)]
                    if image_result.error and panel.get("image_source") is not None:
                        content = f"Image preview unavailable: {image_result.error}\n\n" + content
                editor = panel["editor"]
                if panel.get("too_large"):
                    content = "Editor preview limited to 2 MiB.\n\n" + content
                editor.setPlainText(content)
                editor.setReadOnly(True)
                editor.setExtraSelections([])
        finally:
            self._loading_panel = False

    def _binary_finished(self, generation: int, worker: BinarySummaryWorker, results) -> None:
        if generation != self._binary_generation:
            return
        self._binary_worker = None
        if self._image_worker is None:
            self.cancel_comparison_button.hide()
        if results is None:
            return
        self._binary_results = results
        panels = [panel for panel in self.panels if panel["kind"]]
        self._show_binary_results(panels, results)
        if not self._background_failures:
            self.status.setText("Image comparison ready" if not self.image_controls.isHidden()
                                else "Byte comparison · hashes and byte offsets shown")

    def _cancel_binary_summary(self) -> None:
        self._clear_comparison_failure("binary")
        if self._binary_worker is not None:
            self._binary_worker.cancel_event.set()
            self._binary_worker = None
            if self._image_worker is None:
                self.cancel_comparison_button.hide()
        self._binary_generation += 1
        self._binary_job_key = None
        self._binary_results = None

    def _render_image_comparison(self, panels, sources) -> None:
        if not any(is_image_source(source) for source in sources):
            self._cancel_image_comparison()
            return
        def signature(source):
            if source is None:
                return None
            try:
                info = source.stat()
                return (str(source), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            except OSError:
                return (str(source), "unavailable")

        key = tuple(signature(source) for source in sources)
        if key == self._image_job_key and self._image_panel_refs == panels:
            if self._image_results is not None:
                self._update_image_view()
            return
        self._cancel_image_comparison()
        self.image_summary_button.setChecked(False)
        self._image_job_key = key
        self._image_panel_refs = list(panels)
        self._image_generation += 1
        generation = self._image_generation
        event = threading.Event()
        messages = queue.Queue()
        worker = self.image_decode_worker_type(sources, event, messages)
        thread = self._comparison_thread(worker, "Image comparison")
        self._image_worker = worker
        self._image_jobs.append((thread, worker, messages, generation))
        self.cancel_comparison_button.show()
        self._background_timer.start()
        thread.start()

    def _image_finished(self, generation, worker, results) -> None:
        if generation != self._image_generation:
            return
        self._image_worker = None
        if self._binary_worker is None:
            self.cancel_comparison_button.hide()
        if results is None:
            return
        self._image_results = results
        self._image_difference_cache = {}
        panels = self._image_panel_refs
        sources = [panel.get("image_source") for panel in panels]
        available = [index for index, result in enumerate(results) if result.image is not None]
        if not available or any(source is not None and result.image is None
                                for source, result in zip(sources, results)):
            self.image_controls.hide()
            if self._binary_results is not None:
                self._show_binary_results(panels, self._binary_results)
            return
        base = available[0]
        self._image_base_index = base
        self.image_target.blockSignals(True)
        self.image_target.clear()
        for index in available[1:]:
            self.image_target.addItem(panels[index]["name"].text(), index)
        self.image_target.blockSignals(False)
        self.image_controls.show()
        self._update_image_view()
        if self._binary_worker is None and not self._background_failures:
            self.status.setText("Image comparison ready")

    def _update_image_view(self, _value=None) -> None:
        if self._image_results is None or self.image_controls.isHidden():
            return
        panels = self._image_panel_refs
        results = self._image_results
        base = self._image_base_index
        target = self.image_target.currentData()
        mode = self.image_mode.currentIndex()
        self.image_opacity.setEnabled(mode == 1 and target is not None)
        self.image_target.setEnabled(len(results) > 2)
        for index, (panel, result) in enumerate(zip(panels, results)):
            pane = panel["image_pane"]
            pane.details.setText(result.details)
            if result.image is None:
                panel["view_stack"].setCurrentIndex(0)
                continue
            image = result.image
            overlay = None
            if index == base and target is not None and mode == 1:
                overlay = results[target].image
                pane.details.setText(f"Overlay: {results[base].details} / {results[target].details}")
            elif index == base and target is not None and mode == 2:
                other = results[target].image
                if image.size() == other.size():
                    cache_key = (base, target)
                    if cache_key not in self._image_difference_cache:
                        self._image_difference_cache[cache_key] = difference_image(image, other)
                    image = self._image_difference_cache[cache_key]
                    pane.details.setText(f"Pixel difference · {results[base].details}")
                else:
                    pane.details.setText("Pixel difference unavailable: image dimensions differ. " + result.details)
            pane.view.set_images(image, overlay, self.image_opacity.value() / 100)
            pane.view.set_zoom(self._image_zoom)
            panel["view_stack"].setCurrentIndex(0 if self.image_summary_button.isChecked() else 2)

    def _change_image_zoom(self, step: int) -> None:
        self._image_zoom = max(25, min(800, self._image_zoom + step))
        self.image_zoom_label.setText(f"{self._image_zoom}%")
        for panel in self._image_panel_refs:
            panel["image_pane"].view.set_zoom(self._image_zoom)

    def _sync_image_scroll(self, source_widget) -> None:
        if self._syncing_image_scroll or self._image_results is None:
            return
        source_panel = next((panel for panel in self._image_panel_refs if panel["widget"] is source_widget), None)
        if source_panel is None:
            return
        self._syncing_image_scroll = True
        try:
            source_view = source_panel["image_pane"].view
            for panel in self._image_panel_refs:
                if panel is source_panel or panel["view_stack"].currentIndex() != 2:
                    continue
                target_view = panel["image_pane"].view
                for orientation in ("horizontalScrollBar", "verticalScrollBar"):
                    current = getattr(source_view, orientation)()
                    target = getattr(target_view, orientation)()
                    fraction = (current.value() - current.minimum()) / max(1, current.maximum() - current.minimum())
                    target.setValue(target.minimum() + round(fraction * (target.maximum() - target.minimum())))
        finally:
            self._syncing_image_scroll = False

    def _cancel_image_comparison(self) -> None:
        self._clear_comparison_failure("image")
        if self._image_worker is not None:
            self._image_worker.cancel_event.set()
            self._image_worker = None
            if self._binary_worker is None:
                self.cancel_comparison_button.hide()
        self._image_generation += 1
        self._image_job_key = None
        self._image_results = None
        self._image_difference_cache = {}
        self.image_controls.hide()
        for panel in self._image_panel_refs:
            if panel["view_stack"].currentIndex() == 2:
                panel["view_stack"].setCurrentIndex(0)
        self._image_panel_refs = []

    def _diff_colors(self) -> dict[str, str]:
        """Return semantic diff colors that remain distinct in every built-in theme."""
        if self.settings["theme"] == "light":
            return {
                "added": "#e2f3e5", "removed": "#fbe3e5", "modified": "#fff0c2",
                "ignored": "#e4e9f4",
                "inline": "#b8e5bf",
            }
        return {
            "added": "#12351c", "removed": "#401e25", "modified": "#493b17",
            "ignored": "#28374b",
            "inline": "#2f7d4e",
        }

    @staticmethod
    def _mark_status(statuses: list[str], line: int, status: str) -> None:
        """Prefer a deletion/addition label when changes overlap across panels."""
        priority = {"equal": 0, "ignored": 0, "modified": 1, "added": 2, "removed": 2}
        if priority[status] >= priority[statuses[line]]:
            statuses[line] = status

    @classmethod
    def _build_aligned_rows(
        cls, all_lines: list[list[str]], compare_lines: list[list[str]] | None = None
    ) -> tuple[list[list[int | None]], list[list[str]], list[list[tuple[int, int, int]]]]:
        """Create one visual row sequence shared by all panels.

        Each non-baseline panel is matched to the baseline with SequenceMatcher.
        Insertions (including surplus replacement lines) are allocated at a baseline
        boundary, and the largest insertion at that boundary becomes a shared gap.
        Logical documents retain their original text; aligned views render the gaps.
        """
        baseline = all_lines[0] if all_lines else []
        compare_lines = compare_lines or all_lines
        panel_count = len(all_lines)
        mappings: list[dict[int, int]] = [{index: index for index in range(len(baseline))}]
        extras: list[dict[int, list[int]]] = [{}]
        statuses = [["equal"] * len(lines) for lines in all_lines]
        inline_pairs: list[list[tuple[int, int, int]]] = [[] for _ in all_lines]

        for panel_index, lines in enumerate(all_lines[1:], 1):
            mapping: dict[int, int] = {}
            panel_extras: dict[int, list[int]] = {}
            matcher = difflib.SequenceMatcher(None, compare_lines[0], compare_lines[panel_index], autojunk=False)
            opcodes = ([("equal", 0, len(baseline), 0, len(lines))]
                       if compare_lines[0] == compare_lines[panel_index] else matcher.get_opcodes())
            for tag, base_start, base_end, target_start, target_end in opcodes:
                if tag == "equal":
                    mapping.update(zip(range(base_start, base_end), range(target_start, target_end)))
                    for base_line, target_line in zip(range(base_start, base_end), range(target_start, target_end)):
                        if baseline[base_line] != lines[target_line]:
                            cls._mark_status(statuses[0], base_line, "ignored")
                            cls._mark_status(statuses[panel_index], target_line, "ignored")
                    continue
                if tag == "insert":
                    panel_extras.setdefault(base_start, []).extend(range(target_start, target_end))
                    for target_line in range(target_start, target_end):
                        cls._mark_status(statuses[panel_index], target_line, "added")
                    continue
                if tag == "delete":
                    for base_line in range(base_start, base_end):
                        cls._mark_status(statuses[0], base_line, "removed")
                    continue

                paired = min(base_end - base_start, target_end - target_start)
                for offset in range(paired):
                    base_line, target_line = base_start + offset, target_start + offset
                    mapping[base_line] = target_line
                    cls._mark_status(statuses[0], base_line, "modified")
                    cls._mark_status(statuses[panel_index], target_line, "modified")
                    pair = (panel_index, base_line, target_line)
                    inline_pairs[0].append(pair)
                    inline_pairs[panel_index].append(pair)
                for base_line in range(base_start + paired, base_end):
                    cls._mark_status(statuses[0], base_line, "removed")
                surplus_targets = list(range(target_start + paired, target_end))
                if surplus_targets:
                    panel_extras.setdefault(base_end, []).extend(surplus_targets)
                    for target_line in surplus_targets:
                        cls._mark_status(statuses[panel_index], target_line, "added")
            mappings.append(mapping)
            extras.append(panel_extras)

        rows: list[list[int | None]] = []
        for boundary in range(len(baseline) + 1):
            gap_count = max((len(panel_extras.get(boundary, [])) for panel_extras in extras), default=0)
            for gap_index in range(gap_count):
                rows.append([
                    None if panel_index == 0 else (
                        extras[panel_index].get(boundary, [])[gap_index]
                        if gap_index < len(extras[panel_index].get(boundary, [])) else None
                    )
                    for panel_index in range(panel_count)
                ])
            if boundary < len(baseline):
                rows.append([boundary] + [mappings[index].get(boundary) for index in range(1, panel_count)])
        return rows, statuses, inline_pairs

    def _set_editor_alignment(self, editor: LineNumberTextEdit, rows: list[int | None], lines: list[str]) -> None:
        editor.set_alignment(rows, lines)

    def _render_panels_folder_diff(self, panels: list[dict]) -> None:
        self.text_comparison_notice.hide()
        self._text_comparisons = []
        self._cancel_image_comparison()
        self._cancel_binary_summary()
        self._cancel_text_diff()
        self._populate_folder_trees(panels)
        return

    def _folder_compare_options(self) -> tuple[list[str], bool, bool, bool]:
        exclusions = self.parse_exclusion_patterns(str(self.settings["excluded_paths"]))
        if any(panel.get("git") for panel in self.panels):
            exclusions.append(".git")
        return (
            exclusions,
            bool(self.settings["ignore_whitespace"]),
            bool(self.settings["ignore_line_endings"]),
            bool(self.settings["ignore_gitignore"]),
        )

    def _populate_folder_trees(self, panels: list[dict]) -> None:
        if not panels:
            return
        exclusions, ignore_whitespace, ignore_line_endings, ignore_gitignore = self._folder_compare_options()
        key = (tuple(str(panel["path"]) for panel in panels), tuple(exclusions),
               ignore_whitespace, ignore_line_endings, ignore_gitignore,
               self.metadata_compare_rule,
               bool(self.settings.get("follow_internal_links", False)),
               json.dumps(self._active_text_rules(), sort_keys=True))
        if key != self._folder_job_key:
            self._clear_comparison_failure("folder")
            self._cancel_folder_comparison(superseded=True)
            self._folder_generation += 1
            self._folder_job_key = key
            self._folder_rows = {}
            self._folder_metadata = {}
            for panel in panels:
                panel["metadata_display"].hide()
            self._folder_entry_maps = []
            self._folder_links = {}
            self._folder_moves = []
            self._folder_move_hidden_paths = set()
            self._folder_status_counts = {status: 0 for status in FOLDER_STATUS_FILTERS}
            self._folder_visible_count = 0
            self._folder_partial = False
            self._folder_stale = False
            self._folder_excluded = 0
            self._folder_rule_error = ""
            self._folder_started = time.monotonic()
            event = threading.Event()
            messages = queue.Queue()
            worker = self.folder_comparison_worker_type(
                [panel["path"] for panel in panels], exclusions, ignore_whitespace,
                ignore_line_endings, ignore_gitignore, self._folder_cache, event, messages,
                bool(self.settings.get("follow_internal_links", False)), self._active_text_rules(),
                self.metadata_compare_rule)
            thread = self._comparison_thread(worker, "Folder comparison")
            generation = self._folder_generation
            self._folder_worker = worker
            self._folder_jobs.append((thread, worker, messages, generation))
            self.cancel_comparison_button.show()
            self.status.setText("Scanning folders · 0 paths found")
            self._background_timer.start()
            thread.start()
        self._rebuild_folder_display(panels)

    def _cancel_folder_comparison(self, superseded: bool = False) -> None:
        if self._folder_worker is not None:
            self._folder_worker.cancel_event.set()
            if not superseded:
                self._folder_partial = True
                self.status.setText("Cancelling folder comparison…")
                self._update_folder_counts_label()
            self.cancel_comparison_button.hide()

    def _cancel_active_comparison(self) -> None:
        if self._text_worker is not None:
            self._cancel_text_diff()
            self.status.setText("Text comparison cancelled")
            self._text_comparisons = []
            self.text_comparison_notice.setText("Text comparison cancelled · incomplete; Refresh to retry.")
        elif self._binary_worker is not None or self._image_worker is not None:
            self._cancel_binary_summary()
            self._cancel_image_comparison()
            self.status.setText("Binary comparison cancelled")
        else:
            self._cancel_folder_comparison()

    def _poll_background_jobs(self) -> None:
        if self._closed:
            return
        for job in list(self._folder_jobs):
            thread, worker, messages, generation = job
            for message in job_messages(thread, messages):
                if message[0] == "batch":
                    self._receive_folder_rows(generation, worker, message[1])
                elif message[0] == "metadata" and generation == self._folder_generation:
                    self._folder_metadata.update(message[1])
                elif message[0] == "inventory":
                    if generation == self._folder_generation:
                        self._folder_entry_maps = message[1]
                elif message[0] == "links":
                    if generation == self._folder_generation:
                        self._folder_links = {relative: [panel.get(relative) for panel in message[1]]
                                              for relative in set().union(*(set(panel) for panel in message[1]))}
                elif message[0] == "moves":
                    self._receive_folder_moves(generation, worker, message[1])
                elif message[0] == "move_progress":
                    self._folder_move_progress(generation, message[1], message[2])
                elif message[0] == "progress":
                    self._folder_progress(generation, message[1], message[2], message[3])
                elif message[0] == "rule_error" and generation == self._folder_generation:
                    self._folder_rule_error = f"Text rule error in {message[1]}: {message[2]}"
                    self.status.setText(self._folder_rule_error)
                elif message[0] == "finished":
                    self._folder_finished(generation, worker, message[1], message[2])
                elif message[0] == "failed":
                    self._comparison_failed("folder", generation, worker, message[1])
                elif message[0] == "cancelled":
                    self._folder_finished(generation, worker, True, "")
            if not thread.is_alive() and messages.empty():
                self._folder_jobs.remove(job)
        for job in list(self._binary_jobs):
            thread, worker, messages, generation = job
            for message in job_messages(thread, messages):
                if message[0] in {"failed", "error"}:
                    self._comparison_failed("binary", generation, worker, message[1])
                else:
                    self._binary_finished(generation, worker, message[1])
            if not thread.is_alive() and messages.empty():
                self._binary_jobs.remove(job)
        for job in list(self._image_jobs):
            thread, worker, messages, generation = job
            for message in job_messages(thread, messages):
                if message[0] == "failed":
                    self._comparison_failed("image", generation, worker, message[1])
                else:
                    self._image_finished(generation, worker, message[1])
            if not thread.is_alive() and messages.empty():
                self._image_jobs.remove(job)
        for job in list(self._text_jobs):
            thread, worker, messages, generation, panels, all_lines = job
            for message in job_messages(thread, messages):
                if message[0] == "failed":
                    self._comparison_failed("text", generation, worker, message[1], panels, all_lines)
                else:
                    self._text_finished(generation, worker, panels, all_lines, message[1])
            if not thread.is_alive() and messages.empty():
                self._text_jobs.remove(job)
        self._poll_context_jobs()
        if not (self._folder_jobs or self._binary_jobs or self._image_jobs or self._text_jobs or self._context_jobs):
            self._background_timer.stop()
        self.cancel_comparison_button.setVisible(any(worker is not None for worker in (
            self._folder_worker, self._text_worker, self._binary_worker, self._image_worker)))

    def _refresh_folder_comparison(self) -> None:
        self._background_failures.clear()
        self._update_failure_notice()
        if self._comparison_mode() != "folder":
            self._cancel_binary_summary()
            self._cancel_image_comparison()
            self._refresh_diff()
            return
        self._folder_job_key = None
        self._folder_cache = {}
        self._folder_stale = False
        self._refresh_diff()

    def _receive_folder_rows(self, generation: int, worker: FolderComparisonWorker, rows) -> None:
        if generation != self._folder_generation:
            worker.permits.release()
            return
        panels = [panel for panel in self.panels if panel["kind"] == "folder"]
        for relative, statuses in rows:
            old = self._folder_rows.get(relative)
            if statuses is None and old is not None:
                continue
            if relative in self._folder_rows and relative not in self._folder_move_hidden_paths:
                for category in self._folder_row_categories(old):
                    self._folder_status_counts[category] -= 1
                self._folder_visible_count -= self._folder_row_visible(relative, old)
            self._folder_rows[relative] = statuses
            if relative not in self._folder_move_hidden_paths:
                for category in self._folder_row_categories(statuses):
                    self._folder_status_counts[category] += 1
                self._folder_visible_count += self._folder_row_visible(relative, statuses)
            if not self._folder_row_visible(relative, statuses):
                for panel_items in self._folder_tree_items:
                    item = panel_items.get(relative)
                    if item is not None:
                        item.setHidden(True)
                        self._refresh_folder_ancestor_visibility(item)
                continue
            for index, panel in enumerate(panels):
                self._upsert_folder_item(index, panel, relative, statuses)
        self._update_folder_counts_label()
        self._update_change_navigation()
        self._refresh_selected_folder_metadata()
        self._folder_scroll_timer.start(60)
        worker.permits.release()

    def _receive_folder_moves(self, generation: int, worker: FolderComparisonWorker,
                              matches: list[MoveMatch]) -> None:
        if generation != self._folder_generation:
            worker.permits.release()
            return
        for match in matches:
            self._folder_moves.append(match)
            self._add_folder_move_item(match)
            if len(self._folder_move_groups) == 2:
                for relative in (match.from_path, match.to_path):
                    if relative in self._folder_move_hidden_paths:
                        continue
                    statuses = self._folder_rows.get(relative)
                    if relative in self._folder_rows:
                        for category in self._folder_row_categories(statuses):
                            self._folder_status_counts[category] -= 1
                        self._folder_visible_count -= self._folder_row_visible(relative, statuses)
                    self._folder_move_hidden_paths.add(relative)
                for panel_items in self._folder_tree_items:
                    for relative in (match.from_path, match.to_path):
                        item = panel_items.get(relative)
                        if item is not None:
                            item.setHidden(True)
                            self._refresh_folder_ancestor_visibility(item)
            self._folder_visible_count += self._folder_move_visible(match)
        self._update_folder_counts_label()
        self._update_change_navigation()
        worker.permits.release()

    def _folder_move_progress(self, generation: int, done: int, limit: int) -> None:
        if generation == self._folder_generation:
            self.status.setText(f"Detecting exact moves · {done:,} files hashed · limit {limit:,}")

    def _folder_progress(self, generation: int, done: int, total: int, path: str) -> None:
        if generation != self._folder_generation:
            return
        elapsed = int(time.monotonic() - self._folder_started)
        path = path[-80:]
        if total:
            self.status.setText(f"Comparing {done:,}/{total:,} paths · {total - done:,} left · {elapsed}s · {path}")
        else:
            self.status.setText(f"Scanning folders · {len(self._folder_rows):,} paths found · {elapsed}s · {path}")

    def _folder_finished(self, generation: int, worker: FolderComparisonWorker,
                         cancelled: bool, error: str) -> None:
        if generation != self._folder_generation:
            return
        self._folder_worker = None
        self.cancel_comparison_button.hide()
        self._folder_partial = cancelled or bool(error)
        self._update_folder_counts_label()
        self._folder_scroll_timer.start(60)
        self._folder_excluded = worker.excluded_count
        for relative, statuses in self._folder_rows.items():
            if statuses is None and self._folder_row_visible(relative, statuses) and relative not in self._folder_display_queued:
                self._folder_display_queued.add(relative)
                self._folder_display_pending.append(relative)
        self._schedule_folder_display()
        if error:
            self.status.setText(f"Folder comparison incomplete: {error}" +
                                " · Refresh to rescan" + (" · stale" if self._folder_stale else ""))
        else:
            changed = sum(statuses is not None and (statuses[0] == "error" or any(
                          value not in {"identical", "ignored"} for value in statuses[1:]))
                          for statuses in self._folder_rows.values())
            if len(self._folder_move_groups) == 2:
                changed -= len(self._folder_moves)
            suffix = (f" · {len(self._folder_moves):,} exact moves" if self._folder_moves else "")
            suffix += " · partial" if cancelled else ""
            suffix += " · move search limited" if worker.move_detection_limited else ""
            suffix += " · stale; Refresh to rescan" if self._folder_stale else ""
            suffix += f" · {self._folder_rule_error}" if self._folder_rule_error else ""
            self.status.setText(f"Folder comparison · {len(self._folder_rows):,} paths · "
                                f"{changed:,} changed · {self._folder_excluded:,} excluded{suffix}")
        self._finish_context_folder_view()
