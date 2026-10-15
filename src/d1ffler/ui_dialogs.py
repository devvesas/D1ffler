"""Self-contained settings, help, rules, and export dialogs."""

from __future__ import annotations

import html
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)
from d1ffler.app_assets import logo_asset_path, license_document_path
from d1ffler.text_rules import RuleError, normalize_line, validate_rule
from d1ffler.ui_theme import THEMES
from d1ffler.app_version import APP_VERSION

class TextRulesDialog(QDialog):
    """Edit workspace rules and preview every newly enabled mask."""

    def __init__(self, workspace_rules, default_rules, sample, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Text comparison rules")
        self.setMinimumWidth(620)
        self.rules = ([dict(rule, origin="default", previewed=bool(rule.get("enabled")))
                      for rule in default_rules if isinstance(rule, dict)] +
                      [dict(rule, origin="workspace", previewed=bool(rule.get("enabled")))
                       for rule in workspace_rules if isinstance(rule, dict)])
        self.current = -1
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Rules affect comparison only. File contents and sync use original bytes."))
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(self._select)
        layout.addWidget(self.selector)
        row = QHBoxLayout()
        add = QPushButton("Add workspace rule")
        add.clicked.connect(self._add)
        row.addWidget(add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove)
        row.addWidget(remove)
        layout.addLayout(row)
        form = QFormLayout()
        self.name = QLineEdit()
        self.scope = QLineEdit()
        self.scope.setPlaceholderText("*.log")
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText(r"Timestamp:\s+\S+")
        self.mode = QComboBox()
        self.mode.addItem("Matched text", "match")
        self.mode.addItem("Whole matching line", "line")
        self.enabled = QCheckBox("Enabled")
        self.promote = QCheckBox("Save selected rule as an app default")
        for label, widget in (("Name", self.name), ("Filename pattern", self.scope),
                              ("Regular expression", self.pattern), ("Mask", self.mode)):
            form.addRow(label, widget)
        form.addRow(self.enabled)
        form.addRow(self.promote)
        layout.addLayout(form)
        layout.addWidget(QLabel("Preview source text (edit to try a sample):"))
        self.sample = QPlainTextEdit(sample[:8000])
        self.sample.setFixedHeight(100)
        layout.addWidget(self.sample)
        preview = QPushButton("Preview exact masked text")
        preview.clicked.connect(self._preview)
        layout.addWidget(preview)
        self.preview_result = QTextEdit()
        self.preview_result.setReadOnly(True)
        self.preview_result.setFixedHeight(110)
        layout.addWidget(self.preview_result)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._reload()

    def _save_current(self):
        if self.current < 0 or self.current >= len(self.rules):
            return
        rule = self.rules[self.current]
        old = (rule.get("name"), rule.get("scope"), rule.get("pattern"), rule.get("mode"))
        rule.update(name=self.name.text(), scope=self.scope.text(), pattern=self.pattern.text(),
                    mode=self.mode.currentData(), enabled=self.enabled.isChecked(),
                    origin="default" if self.promote.isChecked() else rule.get("origin", "workspace"))
        if old != (rule["name"], rule["scope"], rule["pattern"], rule["mode"]):
            rule["previewed"] = False

    def _reload(self, selected=0):
        self.selector.blockSignals(True)
        self.selector.clear()
        for rule in self.rules:
            self.selector.addItem(f"{rule.get('name') or '(new)'} · {rule.get('origin', 'workspace')}")
        self.selector.blockSignals(False)
        self.current = -1
        self._select(min(selected, len(self.rules) - 1))

    def _select(self, index):
        self._save_current()
        self.current = index
        rule = self.rules[index] if 0 <= index < len(self.rules) else {}
        self.selector.blockSignals(True)
        self.selector.setCurrentIndex(index)
        self.selector.blockSignals(False)
        self.name.setText(str(rule.get("name", "")))
        self.scope.setText(str(rule.get("scope", "*")))
        self.pattern.setText(str(rule.get("pattern", "")))
        self.mode.setCurrentIndex(max(0, self.mode.findData(rule.get("mode", "match"))))
        self.enabled.setChecked(rule.get("enabled") is True)
        self.promote.setChecked(rule.get("origin") == "default")
        self.preview_result.clear()

    def _add(self):
        self._save_current()
        self.rules.append({"name": "", "scope": "*", "pattern": "", "mode": "match",
                           "enabled": False, "origin": "workspace", "previewed": False})
        self._reload(len(self.rules) - 1)

    def _remove(self):
        if self.current < 0:
            return
        self.rules.pop(self.current)
        self._reload()

    def _preview(self):
        self._save_current()
        if self.current < 0:
            return
        try:
            rule = validate_rule(self.rules[self.current])
            sample = self.sample.toPlainText()
            if len(sample) > 8000:
                raise RuleError("Preview is limited to 8,000 characters.")
            output = []
            matches = 0
            for line in sample.split("\n"):
                _key, spans = normalize_line(line, [rule])
                end = 0
                for start, stop in spans:
                    output.append(html.escape(line[end:start]))
                    output.append(f'<span style="background:#e8b747;color:#121212">{html.escape(line[start:stop])}</span>')
                    end = stop
                    matches += 1
                output.append(html.escape(line[end:]) + "<br>")
            self.preview_result.setHtml(f"<p>{matches} masked span(s)</p><pre>{''.join(output)}</pre>")
            self.rules[self.current]["previewed"] = True
        except RuleError as exc:
            self.preview_result.setPlainText(f"Rule error: {exc}")
            self.rules[self.current]["previewed"] = False

    def _accept(self):
        self._save_current()
        try:
            for rule in self.rules:
                validate_rule(rule)
                if rule.get("enabled") and not rule.get("previewed"):
                    raise RuleError(f"Preview '{rule['name']}' before enabling it.")
        except RuleError as exc:
            QMessageBox.warning(self, "Text rule", str(exc))
            return
        self.accept()

    def result_rules(self):
        return ([validate_rule(rule) for rule in self.rules if rule.get("origin") == "workspace"],
                [validate_rule(rule) for rule in self.rules if rule.get("origin") == "default"])



class SettingsDialog(QDialog):
    def __init__(self, settings: dict[str, object], parent=None) -> None:
        super().__init__(parent)
        self._existing_settings = settings.copy()
        self.setWindowTitle("Settings")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        appearance = QWidget()
        appearance_layout = QVBoxLayout(appearance)
        form = QFormLayout()
        self.theme_combo = QComboBox()
        for theme_id, theme_name in THEMES.items():
            self.theme_combo.addItem(theme_name, theme_id)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings["theme"])))
        form.addRow("Appearance", self.theme_combo)
        appearance_layout.addLayout(form)
        appearance_layout.addStretch()
        tabs.addTab(appearance, "Appearance")

        rules = QWidget()
        rules_layout = QVBoxLayout(rules)
        rules_layout.addWidget(QLabel("Exclude paths (one per line or separated with semicolons):"))
        self.excluded_paths_edit = QPlainTextEdit()
        self.excluded_paths_edit.setPlainText(str(settings["excluded_paths"]))
        self.excluded_paths_edit.setPlaceholderText(".git/;__pycache__/\nbuild/;out/\ngenerated/")
        self.excluded_paths_edit.setFixedHeight(130)
        rules_layout.addWidget(self.excluded_paths_edit)
        self.ignore_gitignore_check = QCheckBox("Ignore files listed in .gitignore")
        self.ignore_gitignore_check.setChecked(bool(settings["ignore_gitignore"]))
        rules_layout.addWidget(self.ignore_gitignore_check)
        self.follow_links_check = QCheckBox("Follow linked paths within each selected folder (comparison only)")
        self.follow_links_check.setChecked(bool(settings.get("follow_internal_links", False)))
        self.follow_links_check.setToolTip("External, broken, and inaccessible links are never followed. Folder sync never follows links.")
        rules_layout.addWidget(self.follow_links_check)
        self.ignore_whitespace_check = QCheckBox("Ignore whitespace-only changes in text files")
        self.ignore_whitespace_check.setChecked(bool(settings["ignore_whitespace"]))
        self.ignore_whitespace_check.setToolTip("Applies to text views and files. Ignores all whitespace, including line breaks and final newline; encoding and BOM still count.")
        rules_layout.addWidget(self.ignore_whitespace_check)
        self.ignore_line_endings_check = QCheckBox("Ignore line-ending-only changes in text files")
        self.ignore_line_endings_check.setChecked(bool(settings["ignore_line_endings"]))
        self.ignore_line_endings_check.setToolTip("Applies to text views and files. Treats CR, LF and CRLF alike while still comparing the presence of a final newline, encoding and BOM.")
        rules_layout.addWidget(self.ignore_line_endings_check)
        self.compare_metadata_check = QCheckBox("Compare modification time and supported permissions")
        self.compare_metadata_check.setChecked(bool(settings.get("compare_metadata", False)))
        self.compare_metadata_check.setToolTip("Metadata-only changes appear in Changed results. ZIP entry time and permissions are unavailable. Metadata sync requires an explicit action in the reviewed folder sync plan.")
        rules_layout.addWidget(self.compare_metadata_check)
        rules_layout.addStretch()
        tabs.addTab(rules, "Folder rules")
        settings_path = getattr(parent, "settings_path", None)
        note = QLabel(f"Settings are saved to {settings_path}." if settings_path else "Settings are saved in your user data directory.")
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, object]:
        return self._existing_settings | {
            "theme": str(self.theme_combo.currentData()),
            "excluded_paths": self.excluded_paths_edit.toPlainText().strip(),
            "ignore_gitignore": self.ignore_gitignore_check.isChecked(),
            "follow_internal_links": self.follow_links_check.isChecked(),
            "ignore_whitespace": self.ignore_whitespace_check.isChecked(),
            "ignore_line_endings": self.ignore_line_endings_check.isChecked(),
            "compare_metadata": self.compare_metadata_check.isChecked(),
        }



class InfoDialog(QDialog):
    """Task-oriented, offline help for the controls in the main window."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("D1ffler help")
        self.resize(860, 650)
        self.setMinimumSize(680, 480)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        start = self._add_tab("Get started", [
            ("Compare sources", "Right-click a panel to add a file, folder, or ZIP archive, paste text into an empty panel, "
             "or drop paths onto panels. Use + for up to four panels. The leftmost panel is the baseline. "
             "A workspace uses either folder/ZIP sources or file/text sources."),
            ("Read the result", "Text changes align across panels; colored lines and highlights show differences. "
             "Double-click a folder file to open its comparison, then use Back to folders to return. "
             "Binary files show size, SHA-256 hashes, and the first differing byte. Supported images open "
             "visually with linked zoom, overlay, and pixel difference; Show byte summary restores the hashes. "
             "Files above 2 MiB show metadata instead of editor contents."),
            ("Return later", "Save workspace stores a named comparison of file or folder sources. "
             "Workspaces opens saved and recent comparisons in new tabs. Saving stores only the current tab; "
             "use Save workspace again to update a named setup. It resumes the selected file, expanded folders, "
             "cursor, hunk, and scroll when current sources support them. Review notes and retained report references "
             "are stored with it; unsaved text remains separate in recovery. "
             "The gear opens themes and folder rules."),
            ("Keep comparisons open", "Choose + New comparison or Ctrl+T for a separate source set. Each tab "
             "keeps its edits, search, folder filters, selection, review position, notes, and reports. Switching "
             "tabs never saves or discards edits. Source or workspace names identify tabs; * means unsaved buffers. "
             "Close a tab with its close button or Ctrl+W and review its own unsaved edits. Up to eight tabs "
             "can stay open; at most two background jobs run, with the others queued. Cancel work affects the "
             "current comparison. Use --new-tab between command-line source sets to open separate comparisons."),
            ("Recover unfinished edits", "D1ffler keeps local snapshots of unsaved edits, pasted text, and "
             "manually edited merge Results, together with rules, search, filters, notes, workspace name, and review position. "
             "Restored notes are checked against current content; retained reports are historical. "
             "On startup, inspect recovered text beside the current disk file, "
             "then restore, discard, or decide later. Restoring never writes a source file; changed disk "
             "files still need confirmation before saving."),
            ("Failures and diagnostics", "An unexpected background failure marks its own comparison incomplete. "
             "Refresh retries the comparison; open edits remain available. Diagnostics shows the local log "
             "location and exports incident IDs, operation types, versions, and traceback locations. Logs omit "
             "file contents, exception messages, absolute paths, and local variables. Including source paths "
             "in an exported report requires checking the sharing option."),
            ("Manage local history", "Workspaces → Local history and storage shows locations and sizes, saved/recent "
             "comparisons, recovery, reports and diagnostics. Inspect or restore abandoned recovery into a new tab; "
             "live snapshots remain protected. Remove selected history or recovery explicitly and clear local logs. "
             "Preview unreferenced report cleanup before deleting copies; optional age retention runs on startup. "
             "Unreadable references block cleanup. Sources, worktrees, sync backups, exports and open edits stay intact. "
             "Close all instances before backing up or restoring the complete user-state directory, including reports. "
             "Backups and recovered text can contain private source content and paths; review exports before sharing."),
        ])
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(logo_asset_path()))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaled(110, 110, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
            start.insertWidget(0, logo)
        heading = QLabel(f"D1ffler {APP_VERSION} — file and folder comparison")
        heading.setObjectName("aboutTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        start.insertWidget(1 if not pixmap.isNull() else 0, heading)

        self._add_tab("Text & merge", [
            ("Text results and formats", "The result notice distinguishes identical text and format, equivalence under "
             "active rules, content changes, and format-only changes. Zero content hunks can still mean different "
             "final newline, line endings, encoding or BOM. Settings in Folder rules also apply to text views: "
             "Ignore whitespace removes all whitespace including line breaks; Ignore line endings treats CR, LF "
             "and CRLF alike but still compares the presence of a final newline. Encoding and BOM always count. "
             "Rules change comparison only. Unsaved preview results describe the buffers, while folder rows describe saved files."),
            ("Apply a patch", "Use Apply patch in the toolbar to open a unified .patch or .diff file. Review "
             "the source, target, selected hunks, match location, and result before applying. Editor targets "
             "change in memory as one undoable edit; Save writes them later. Folder targets show a reviewed "
             "create, overwrite, delete, or rename plan and offer optional backups before a separate confirmation. "
             "Ambiguous or failed hunks must be deselected or resolved. Read-only revisions, ZIP targets, "
             "binary patches, and unsafe paths are unavailable."),
            ("Find and replace text", "Ctrl+F opens a search bar across text panels. F3 and Shift+F3 move "
             "forward and backward, reporting when the search wraps. Use Case sensitive or Whole word to "
             "narrow matches. Ctrl+H focuses the replacement field. Replace Next and Replace All edit only "
             "the active editable panel; Replace All confirms the panel and count. Edits are undoable and "
             "remain unsaved until you save. "
             "Read-only Git revisions and ZIP entries cannot be replaced."),
            ("Review and apply text changes", "Use F7 and Shift+F7 to move between changed hunks. "
             "Right-click a changed hunk to apply it to another editable panel, or select text and right-click "
             "to apply that selection. These actions edit the target in memory until you save it. "
             "Undo/Redo in the toolbar or pane menu reverses edits, applied hunks, selections, and merge choices. "
             "Comparison refreshes retain history. Loading a new source or rebuilding Result starts new history; "
             "recovery restores the latest buffer, not its undo stack."),
            ("Save edits", "Save or Ctrl+S writes the active panel; Save all confirms the list of modified files. "
             "Right-click a panel for Save file as…. Read-only Git revisions and binary previews cannot be edited. "
             "A changed disk version needs explicit confirmation before overwrite; the panel also offers compare, "
             "save as copy, and reload. The panel shows its encoding; Save preserves it, while Save with encoding "
             "converts it after confirmation. Reopen with encoding previews alternate decoding without losing edits."),
            ("Three-way merge", "Choose Three-way merge in the toolbar for Base, Local, Incoming, and Result. "
             "Independent changes go into Result automatically. Choose a conflict, then Use Local, Use Incoming, "
             "or Use Base; you can also edit its markers in Result and choose Mark Manual. If an input changes "
             "after you edit Result, use Rebuild merge to replace the old result. In merge mode, Save writes only Result."),
        ])

        self._add_tab("Folders & sync", [
            ("Compare folders", "Add a folder to each panel. The tree shows added, removed, modified, identical, "
             "error, pending, and linked paths. Linked rows show target and state. Changed hides identical and pending rows. A large scan runs in the "
             "background; Cancel work leaves partial results, and Refresh starts a new scan. After scanning, "
             "Detected moves links unique exact-content matches; double-click a move to compare both paths."),
            ("Compare ZIP archives", "Add ZIP as folder… treats a .zip file as a read-only folder. Compare it with "
             "another ZIP or a directory, then search, filter, preview, and export internal paths. Unsafe, "
             "encrypted, duplicate, oversized, or corrupt entries appear as errors. ZIPs are never sync targets "
             "or sources, and .gitignore is not applied inside them."),
            ("Find and triage paths", "Use the path search above folder panels, or Ctrl+F in folder view, "
             "to find a relative path while scanning. The status menu shows counts and filters errors or pending "
             "files directly. F7 and Shift+F7 select the next or previous visible changed file across panels. "
             "Refresh keeps search, filters, expansion, selection, and scroll position where possible."),
            ("Search file contents", "Choose Search contents… beside the folder path field to search literal text "
             "inside discovered files in selected panels. Search all discovered paths or the current path/status "
             "filter, then double-click a hit to open its file at the matching line. The search is read-only, "
             "runs separately from the folder scan, and can be cancelled. Binary, oversized, linked, unreadable, "
             "missing, and pending entries are counted as skipped; partial and stale results are labeled."),
            ("Set comparison rules", "Use the gear → Folder rules to exclude relative paths, honor .gitignore, "
             "ignore whitespace-only and line-ending-only text changes, or compare file modification time and "
             "supported permissions. Metadata-only changes have their own folder status and filter; selecting "
             "a file shows each source's details. Folder sync offers explicit modification time and permission "
             "actions, with exact before/after previews, dry run, conflict checks, backups, and reports. "
             "Metadata differences default to Skip. Windows permissions use writable/read-only state; "
             "POSIX uses permission bits. Filesystem support is verified when applying. "
             "Linked paths compare metadata by default; "
             "the optional follow setting reads only internal targets for comparison and stops cycles. "
             "External links are never followed. Git on PATH is needed for .gitignore rules."),
            ("Review a sync plan", "Right-click a folder panel or a tree entry and choose Plan folder sync…. "
             "Choose two panels and an action for each changed path or subtree. Dry run checks the plan without "
             "writing. Apply plan… asks for confirmation; choose a target-change policy and optional backups "
             "outside both compared folders. Linked paths are unavailable with a reason in the plan. "
             "Completed operations remain in place if the plan stops."),
            ("Inspect outcomes", "The sync report records each file outcome and can be exported as JSON. "
             "Use View sync reports… from a panel menu to reopen reports saved with the workspace. "
             "For a completed sync with backups, choose Review restore… in its report. Select files, "
             "dry run, then apply with a target-change policy. Restore reports are saved separately; "
             "original backups remain in place."),
        ])

        self._add_tab("Git", [
            ("Compare revisions", "Add a Git repository root as the baseline folder. Right-click another "
             "folder panel and choose Choose revision… to select a local branch, tag, or commit. D1ffler opens "
             "the revision in a linked worktree; a branch with local changes shows those current files."),
            ("Edit a revision", "Branch worktrees are editable. Tags and commits open read-only; use Create branch "
             "from this revision… on that panel to make an editable branch. Manage D1ffler worktrees… lets you "
             "keep or remove a clean managed worktree after clearing its panel."),
            ("Create a commit", "Save writes edited files to the worktree; it does not make a commit. On an "
             "editable Git panel, choose Create commit…, select saved changed files, and review their patch "
             "before entering a message and committing. The patch uses saved disk content, which can differ "
             "from staged content. Unselected staged files stay out of that commit. Save or discard open edits "
             "in that worktree first."),
        ])

        self._add_tab("Review", [
            ("Record decisions", "With at least two file or folder sources, right-click a text hunk or a "
             "folder-tree file entry and choose Review this hunk… or Review this file…. Give it a note and "
             "state: Open, Accepted, Needs follow-up, or Ignored. A review state never applies or saves a change."),
            ("Maintain notes", "Review notes lists notes saved with the workspace. If compared sources or file "
             "contents change, a note is marked stale. Reattaching it to the current content requires an "
             "explicit choice."),
            ("Share a report", "Review report exports selected notes as Markdown or HTML, with source paths, "
             "active rules, and counts. File content is excluded by default. You may explicitly add unified "
             "excerpts for current text hunk notes; stale notes are labeled."),
            ("Export the comparison", "Export comparison writes a unified diff or HTML text report, or a CSV "
             "or HTML folder manifest. It does not require review notes. Folder exports can include all rows "
             "or the current filter and label incomplete or stale results. Absolute source paths are optional."),
        ])

        self._add_tab("Shortcuts", [
            ("Keyboard", "Ctrl+F  Find text across open editor panels\n"
             "Ctrl+H  Focus text replacement\n"
             "F3 / Shift+F3  Next / previous text match\n"
             "F7 / Shift+F7  Next / previous text change\n"
             "Ctrl+Z  Undo in the active editable panel\n"
             "Ctrl+Y / Ctrl+Shift+Z  Redo\n"
             "Ctrl+S  Save the active editable panel"),
            ("Mouse and toolbar", "Ctrl + mouse wheel zooms the interface. The ¶ toolbar button shows spaces, "
             "tabs, and line endings. Right-click a panel for source, save, apply, sync, Git, and note actions "
             "available in the current view."),
        ])

        self.license_documents = {
            "Application license — GPL-3.0-or-later": "LICENSE",
            "Copyright and license scope": "COPYRIGHT.md",
            "Third-party notices": "THIRD_PARTY_NOTICES.md",
            "Corresponding source and rebuilding": "SOURCE_ACCESS.md",
        }
        legal_page = QWidget()
        legal_layout = QVBoxLayout(legal_page)
        legal_layout.addWidget(QLabel("D1ffler is free software under GPL-3.0-or-later. Commercial reuse is allowed.\n"
                                      "Copyright © 2026 D1ffler contributors. No warranty.\n"
                                      "PyQt6 uses GPLv3; Qt and other dependencies retain their own licenses."))
        self.license_selector = QComboBox()
        self.license_selector.addItems(self.license_documents)
        legal_layout.addWidget(self.license_selector)
        self.license_text = QPlainTextEdit()
        self.license_text.setReadOnly(True)
        legal_layout.addWidget(self.license_text, 1)
        self.license_selector.currentTextChanged.connect(self._show_license_document)
        self._show_license_document(self.license_selector.currentText())
        self.tabs.addTab(legal_page, "Licenses")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _show_license_document(self, label):
        path = license_document_path(self.license_documents[label])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = f"License document unavailable: {path.name}. Restore the complete source or release package."
        self.license_text.setPlainText(text)

    def _add_tab(self, title: str, sections: list[tuple[str, str]]) -> QVBoxLayout:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(18, 14, 24, 18)
        page_layout.setSpacing(14)
        for heading, body in sections:
            section = QWidget()
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(4)
            label = QLabel(heading)
            label.setStyleSheet("font-size: 12pt; font-weight: 600;")
            section_layout.addWidget(label)
            detail = QLabel(body)
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            section_layout.addWidget(detail)
            page_layout.addWidget(section)
        page_layout.addStretch()
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)
        return page_layout



class ReviewReportDialog(QDialog):
    def __init__(self, notes: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export review report")
        self.resize(720, 450)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select notes for the report. File contents are excluded by default."))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Path or hunk", "State", "Status"])
        self.tree.setColumnWidth(0, 390)
        layout.addWidget(self.tree, 1)
        self.rows = {}
        for note in notes:
            row = QTreeWidgetItem([
                str(note.get("path") or "File comparison"),
                str(note.get("state", "Open")),
                "STALE" if note.get("stale") else "Current",
            ])
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(0, Qt.CheckState.Checked)
            self.tree.addTopLevelItem(row)
            self.rows[str(note["id"])] = row
        self.excerpts = QCheckBox("Include unified hunk excerpts for current text notes")
        layout.addWidget(self.excerpts)
        self.format = QComboBox()
        self.format.addItems(["Markdown", "HTML"])
        layout.addWidget(self.format)
        buttons = QHBoxLayout()
        buttons.addStretch()
        export = QPushButton("Choose report file…")
        export.setObjectName("primaryButton")
        export.clicked.connect(self.accept)
        buttons.addWidget(export)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def selected_ids(self) -> set[str]:
        return {identifier for identifier, row in self.rows.items()
                if row.checkState(0) == Qt.CheckState.Checked}



class ComparisonExportDialog(QDialog):
    def __init__(self, folder_mode: bool, partial: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export comparison")
        self.resize(470, 230)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Export the current comparison without creating review notes."))
        form = QFormLayout()
        self.format = QComboBox()
        if folder_mode:
            self.format.addItem("CSV manifest", "csv")
            self.format.addItem("HTML manifest", "html")
        else:
            self.format.addItem("Unified diff", "diff")
            self.format.addItem("HTML side-by-side", "html")
        form.addRow("Format", self.format)
        self.scope = QComboBox()
        if folder_mode:
            self.scope.addItem("All comparison rows", "all")
            self.scope.addItem("Current path and status filter", "visible")
            form.addRow("Rows", self.scope)
        self.include_paths = QCheckBox("Include absolute source paths")
        form.addRow("Privacy", self.include_paths)
        self.include_metadata = QCheckBox("Include file size, modification time, and permissions")
        if folder_mode:
            form.addRow("File metadata", self.include_metadata)
        layout.addLayout(form)
        if partial:
            layout.addWidget(QLabel("The scan is incomplete. The export will be marked PARTIAL."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

