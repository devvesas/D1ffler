"""Modern native file and folder diff tool."""

from __future__ import annotations

import difflib
import fnmatch
import os
import sys
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from d1ffler.recovery import RecoveryStore
from d1ffler.comparison_workers import (
    ComparisonCancelled, FolderComparisonWorker as _FolderComparisonWorker,
    BinarySummaryWorker as _BinarySummaryWorker, SyncPlanWorker as _SyncPlanWorker,
    TextDiffWorker as _TextDiffWorker,
)
from d1ffler.three_way_merge import Conflict, merge_text
from d1ffler.folder_links import LinkInfo, inspect_link
from d1ffler.folder_archives import ArchiveIssue, ArchiveMember, is_zip_source, zip_inventory
from d1ffler.text_rules import RuleError
from d1ffler.text_comparison import comparison_key
from d1ffler.app_assets import icon_asset_path, logo_asset_path
from d1ffler.editor_widgets import DiffHighlighter, LineNumberTextEdit, PathRow
from d1ffler.ui_dialogs import TextRulesDialog, SettingsDialog, InfoDialog, ReviewReportDialog, ComparisonExportDialog
from d1ffler.ui_theme import THEMES, apply_theme
from d1ffler.git_worktrees import GitError, GitIgnoreMatcher
from d1ffler.shared_sources import SharedSources, SourceScope
from d1ffler.source_paths import absolute_source, absolute_worktrees

from PyQt6.QtCore import QEvent, QFileSystemWatcher, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QIcon,
    QTextOption,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QDialog, QComboBox, QInputDialog, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QStackedWidget,
    QSlider, QStyle, QTabWidget, QTextEdit, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from d1ffler.file_content import (
    FileContent, MAX_EDITOR_FILE_BYTES, ENCODING_CHOICES, encoding_label,
    encode_text, inspect_file_content, disk_fingerprint, file_sha256,
    read_file_content, unloaded_file_message, binary_file_summary,
    read_text, compare_files,
)
from d1ffler.folder_sync_dialogs import SyncReportDialog, RestoreReportDialog, RestorePlanDialog
from d1ffler.review_dialogs import FingerprintDialog, ReviewNoteDialog, ReviewNotesDialog
from d1ffler.recovery_dialog import RecoveryDialog
from d1ffler.git_dialog import CommitDialog
from d1ffler.sync_plan_dialog import SyncPlanDialog
from d1ffler.review_export_mixin import ReviewExportMixin
from d1ffler.comparison_models import FileDifference, FolderEntry, MoveMatch, FOLDER_STATUS_FILTERS
from d1ffler.panel_mixin import PanelMixin
from d1ffler.folder_view_mixin import FolderViewMixin
from d1ffler.app_settings import DEFAULT_SETTINGS, SettingsState, settings_candidates, load_app_settings, save_app_settings
from d1ffler.workspace_mixin import WorkspaceMixin
from d1ffler.git_source_mixin import GitSourceMixin
from d1ffler.comparison_mixin import ComparisonMixin
from d1ffler.text_search_mixin import TextSearchMixin
from d1ffler.comparison_tabs import ComparisonWindow, BackgroundTask, command_line_source_sets
from d1ffler.diagnostics import DiagnosticStore, ExceptionHooks
from d1ffler.diagnostics_dialog import DiagnosticsDialog
from d1ffler.app_version import APP_VERSION
from d1ffler.image_compare import ImageDecodeWorker
from d1ffler.patch_dialog import PatchDialog
from d1ffler.unified_patch import PatchError, parse_patch
from d1ffler.folder_content_search_dialog import FolderContentSearchDialog

FILE_ICON_MAP = {
    ".py": "python.svg",
    ".js": "javascript.svg", ".jsx": "javascript.svg",
    ".ts": "typescript.svg", ".tsx": "typescript.svg",
    ".rs": "rust.svg", ".go": "go.svg",
    ".c": "c.svg", ".h": "h.svg",
    ".cpp": "cpp.svg", ".cc": "cpp.svg", ".cxx": "cpp.svg",
    ".hpp": "hpp.svg", ".hh": "hpp.svg", ".hxx": "hpp.svg",
    ".html": "html.svg", ".htm": "html.svg", ".css": "css.svg",
    ".json": "json.svg", ".md": "markdown.svg", ".markdown": "markdown.svg",
    ".yaml": "yaml.svg", ".yml": "yaml.svg", ".cs": "csharp.svg",
    ".ps1": "powershell.svg", ".psm1": "powershell.svg", ".psd1": "powershell.svg",
    ".cmd": "cmd.svg", ".bat": "cmd.svg",
    ".sh": "shell.svg", ".bash": "shell.svg", ".zsh": "shell.svg", ".fish": "shell.svg",
    ".tf": "terraform.svg", ".tfvars": "terraform.svg",
    ".txt": "text.svg", ".log": "text.svg", ".rtf": "rtf.svg", ".pdf": "pdf.svg", ".csv": "csv.svg",
    ".doc": "word.svg", ".docx": "word.svg", ".dot": "word.svg", ".dotx": "word.svg",
    ".xls": "excel.svg", ".xlsx": "excel.svg", ".xlsm": "excel.svg", ".xlt": "excel.svg", ".xltx": "excel.svg",
    ".ppt": "powerpoint.svg", ".pptx": "powerpoint.svg", ".pptm": "powerpoint.svg", ".pot": "powerpoint.svg", ".potx": "powerpoint.svg",
    ".odt": "odt.svg", ".ods": "ods.svg", ".odp": "odp.svg",
    ".jpg": "jpeg.svg", ".jpeg": "jpeg.svg", ".jpe": "jpeg.svg",
    ".png": "png.svg", ".gif": "gif.svg", ".svg": "svg.svg", ".svgz": "svg.svg", ".webp": "webp.svg",
    ".bmp": "bmp.svg", ".dib": "bmp.svg", ".tif": "tiff.svg", ".tiff": "tiff.svg",
    ".ico": "ico.svg", ".heic": "heic.svg", ".heif": "heic.svg",
    ".raw": "raw.svg", ".cr2": "raw.svg", ".cr3": "raw.svg", ".nef": "raw.svg", ".arw": "raw.svg",
    ".dng": "raw.svg", ".orf": "raw.svg", ".rw2": "raw.svg",
}
FILENAME_ICON_MAP = {"dockerfile": "docker.svg"}


@dataclass(frozen=True)
class ClosePlan:
    discard_panels: tuple[int, ...]
    discard_workspace: bool
    tab_close: bool


def parse_exclusion_patterns(value: str) -> list[str]:
    """Parse one-per-line or semicolon-separated relative folder/file rules."""
    return [part.strip().replace("\\", "/") for part in value.replace(";", "\n").splitlines() if part.strip()]


def is_excluded_path(relative: str, patterns: list[str]) -> bool:
    relative = relative.strip("/")
    for raw_pattern in patterns:
        pattern = raw_pattern.strip().strip("/")
        if not pattern:
            continue
        if raw_pattern.rstrip().endswith(("/", "\\")):
            if relative == pattern or relative.startswith(pattern + "/") or f"/{pattern}/" in f"/{relative}/":
                return True
        if fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(relative, f"*/{pattern}"):
            return True
    return False


def folder_inventory(
    root: Path, patterns: list[str], ignore_gitignore: bool = False,
    cancelled=None, discovered=None, links: dict[str, LinkInfo] | None = None,
    follow_internal_links: bool = False,
    strict_errors: bool = False,
) -> tuple[dict[str, FolderEntry], list[str]]:
    if is_zip_source(root):
        return zip_inventory(root, patterns, is_excluded_path, cancelled, discovered)
    included: dict[str, FolderEntry] = {}
    excluded: list[str] = []
    context = GitIgnoreMatcher(root) if ignore_gitignore else nullcontext(None)
    with context as matcher:
        stack = [(root, frozenset())]
        while stack:
            if cancelled and cancelled():
                break
            parent_path, ancestors = stack.pop()
            try:
                identity = parent_path.resolve(strict=True)
                if identity in ancestors:
                    continue
                entries = list(os.scandir(parent_path))
            except (OSError, RuntimeError):
                if strict_errors:
                    raise
                continue
            entries.sort(key=lambda entry: entry.name.casefold())
            next_ancestors = ancestors | {identity}
            candidates = []
            details = []
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                link = inspect_link(path, root)
                directory = entry.is_dir(follow_symlinks=False) if link is None else link.directory
                details.append((path, relative, link, directory))
                if not is_excluded_path(relative, patterns):
                    candidates.append(relative)
            ignored = matcher.ignored(candidates) if matcher else set()
            for path, relative, link, directory in reversed(details):
                if cancelled and cancelled():
                    break
                if is_excluded_path(relative, patterns) or relative in ignored:
                    excluded.append(relative)
                else:
                    if link is not None or not directory:
                        included[relative] = path
                        if link is not None and links is not None:
                            links[relative] = link
                        if discovered:
                            discovered(relative)
                    if directory and (link is None or
                                      (follow_internal_links and link.state == "internal")):
                        stack.append((path, next_ancestors))
    return included, excluded


def _files_match_cancellable(left: Path | ArchiveMember, right: Path | ArchiveMember, ignore_whitespace: bool,
                             ignore_line_endings: bool, cancelled, text_rules=None) -> str:
    """Return a status while checking cancellation between bounded reads."""
    before_left, before_right = left.stat(), right.stat()
    equal = before_left.st_size == before_right.st_size
    if equal:
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                if cancelled():
                    return "cancelled"
                first, second = a.read(1024 * 1024), b.read(1024 * 1024)
                if first != second:
                    equal = False
                    break
                if not first:
                    break
    byte_equal = equal
    if not equal and (ignore_whitespace or ignore_line_endings or text_rules):
        if cancelled():
            return "cancelled"
        if text_rules and (before_left.st_size > 8 * 1024 * 1024 or before_right.st_size > 8 * 1024 * 1024):
            raise RuleError("Text exceeds the 8 MiB text rule limit.")
        left_content, right_content = inspect_file_content(left), inspect_file_content(right)
        if left_content.text is not None and right_content.text is not None:
            # Folder workers already scope rules to the relative filename.
            equal = (comparison_key(left_content.text, text_rules or [], ignore_whitespace, ignore_line_endings)
                     == comparison_key(right_content.text, text_rules or [], ignore_whitespace, ignore_line_endings)
                     and (left_content.encoding, left_content.bom) == (right_content.encoding, right_content.bom))
    after_left, after_right = left.stat(), right.stat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if signature(before_left) != signature(after_left) or signature(before_right) != signature(after_right):
        return "error"
    return "identical" if byte_equal else "ignored" if equal else "modified"


MAX_MOVE_HASHES = 5000
def detect_exact_moves(maps: list[dict[str, FolderEntry]], cancelled,
                       progress=None, max_hashes: int = MAX_MOVE_HASHES) -> tuple[list[MoveMatch], bool]:
    """Pair uniquely matching unmatched paths; skip whole size groups if capped."""
    hashes: dict[FolderEntry, str | None] = {}
    matches: list[MoveMatch] = []
    limited = False

    def digest(path: FolderEntry) -> str | None:
        if path in hashes:
            return hashes[path]
        if cancelled():
            raise ComparisonCancelled()
        try:
            before = path.stat()
            value = file_sha256(path, cancelled)
            after = path.stat()
            signature = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size,
                                      stat.st_mtime_ns, stat.st_ctime_ns)
            value = value if signature(before) == signature(after) else None
        except OSError:
            value = None
        hashes[path] = value
        if progress and len(hashes) % 32 == 0:
            progress(len(hashes), max_hashes)
        return value

    for left_index in range(len(maps)):
        for right_index in range(left_index + 1, len(maps)):
            if cancelled():
                raise ComparisonCancelled()
            left, right = maps[left_index], maps[right_index]
            left_only = sorted(left.keys() - right.keys(), key=str.casefold)
            right_only = sorted(right.keys() - left.keys(), key=str.casefold)
            by_size_left: dict[int, list[tuple[str, FolderEntry]]] = {}
            by_size_right: dict[int, list[tuple[str, FolderEntry]]] = {}
            for entries, source, grouping in ((left_only, left, by_size_left),
                                              (right_only, right, by_size_right)):
                for relative in entries:
                    if cancelled():
                        raise ComparisonCancelled()
                    path = source[relative]
                    if isinstance(path, Path) and inspect_link(path, path.parent):
                        continue
                    try:
                        grouping.setdefault(path.stat().st_size, []).append((relative, path))
                    except OSError:
                        continue
            for size in sorted(by_size_left.keys() & by_size_right.keys()):
                if cancelled():
                    raise ComparisonCancelled()
                candidates = by_size_left[size] + by_size_right[size]
                needed = sum(path not in hashes for _relative, path in candidates)
                if len(hashes) + needed > max_hashes:
                    limited = True
                    continue
                left_hashes: dict[str, list[str]] = {}
                right_hashes: dict[str, list[str]] = {}
                unreliable = False
                for entries, bucket in ((by_size_left[size], left_hashes),
                                        (by_size_right[size], right_hashes)):
                    for relative, path in entries:
                        value = digest(path)
                        if value is None:
                            unreliable = True
                        else:
                            bucket.setdefault(value, []).append(relative)
                if unreliable:
                    continue
                for value in left_hashes.keys() & right_hashes.keys():
                    if len(left_hashes[value]) == len(right_hashes[value]) == 1:
                        matches.append(MoveMatch(left_index, left_hashes[value][0],
                                                 right_index, right_hashes[value][0], value))
    matches.sort(key=lambda match: (match.from_panel, match.to_panel,
                                    match.from_path.casefold(), match.to_path.casefold()))
    return matches, limited


class FolderComparisonWorker(_FolderComparisonWorker):
    def __init__(self, roots, exclusions, ignore_whitespace, ignore_endings,
                 ignore_gitignore, cache, cancel_event, messages, follow_internal_links=False,
                 text_rules=None, compare_metadata=False):
        super().__init__(
            roots, exclusions, ignore_whitespace, ignore_endings, ignore_gitignore,
            cache, cancel_event, messages, follow_internal_links, text_rules, compare_metadata,
            inventory=folder_inventory, files_match=_files_match_cancellable,
            detect_moves=detect_exact_moves, hash_file=file_sha256,
            max_move_hashes=MAX_MOVE_HASHES,
        )


class BinarySummaryWorker(_BinarySummaryWorker):
    def __init__(self, paths, cancel_event, messages):
        super().__init__(paths, cancel_event, messages, summary=binary_file_summary)


class SyncPlanWorker(_SyncPlanWorker):
    def __init__(self, left, right, exclusions, ignore_whitespace,
                 ignore_endings, ignore_gitignore, cancel_event, messages):
        super().__init__(left, right, exclusions, ignore_whitespace, ignore_endings,
                         ignore_gitignore, cancel_event, messages,
                         inventory=folder_inventory, files_match=_files_match_cancellable)


class TextDiffWorker(_TextDiffWorker):
    def __init__(self, lines, cancel_event, messages, compare_lines=None, rules_by_panel=None, **kwargs):
        super().__init__(lines, cancel_event, messages, compare_lines, rules_by_panel,
                         build_rows=DiffApp._build_aligned_rows, **kwargs)


def folder_files(root: Path, patterns: list[str], ignore_gitignore: bool = False) -> tuple[dict[str, FolderEntry], int]:
    files, excluded = folder_inventory(root, patterns, ignore_gitignore)
    return files, len(excluded)


def is_probably_text(path: Path | ArchiveMember) -> bool:
    try:
        return inspect_file_content(path).text is not None
    except (OSError, UnicodeDecodeError):
        return False


def files_match(left: Path | ArchiveMember, right: Path | ArchiveMember,
                ignore_whitespace: bool, ignore_line_endings: bool) -> bool:
    return _files_match_cancellable(left, right, ignore_whitespace,
                                    ignore_line_endings, lambda: False) in {"identical", "ignored"}


def compare_folders(
    left: Path, right: Path, exclusions: list[str] | None = None,
    ignore_whitespace: bool = False, ignore_line_endings: bool = False,
    ignore_gitignore: bool = False,
) -> list[FileDifference]:
    left_files, _left_excluded = folder_files(left, exclusions or [], ignore_gitignore)
    right_files, _right_excluded = folder_files(right, exclusions or [], ignore_gitignore)
    return compare_folder_file_maps(left_files, right_files, ignore_whitespace, ignore_line_endings)


def compare_folder_file_maps(
    left_files: dict[str, FolderEntry], right_files: dict[str, FolderEntry],
    ignore_whitespace: bool = False, ignore_line_endings: bool = False,
) -> list[FileDifference]:
    results: list[FileDifference] = []
    for relative in sorted(left_files.keys() | right_files.keys(), key=str.casefold):
        left_file, right_file = left_files.get(relative), right_files.get(relative)
        if isinstance(left_file, ArchiveIssue) or isinstance(right_file, ArchiveIssue):
            status = "error"
        elif left_file is None:
            status = "added"
        elif right_file is None:
            status = "removed"
        else:
            try:
                left_link = inspect_link(left_file, left_file.parent) if isinstance(left_file, Path) else None
                right_link = inspect_link(right_file, right_file.parent) if isinstance(right_file, Path) else None
                if isinstance(left_file, ArchiveIssue) or isinstance(right_file, ArchiveIssue):
                    status = "error"
                elif (left_link and left_link.state in {"broken", "inaccessible"}) or (right_link and right_link.state in {"broken", "inaccessible"}):
                    status = "error"
                elif left_link or right_link:
                    status = ("identical" if left_link and right_link and
                              (left_link.kind, left_link.target) == (right_link.kind, right_link.target)
                              else "modified")
                else:
                    status = "identical" if files_match(left_file, right_file, ignore_whitespace, ignore_line_endings) else "modified"
            except OSError:
                status = "error"
        results.append(FileDifference(relative, status))
    return results


class DiffApp(PanelMixin, FolderViewMixin, ReviewExportMixin, WorkspaceMixin, GitSourceMixin, ComparisonMixin, TextSearchMixin, QMainWindow):
    comparison_changed = pyqtSignal()
    sync_plan_worker = SyncPlanWorker
    folder_comparison_worker_type = FolderComparisonWorker
    binary_summary_worker_type = BinarySummaryWorker
    text_diff_worker_type = TextDiffWorker
    image_decode_worker_type = ImageDecodeWorker
    parse_exclusion_patterns = staticmethod(parse_exclusion_patterns)

    def __init__(self, settings: dict[str, object], settings_path: Path, *, comparison_host=None) -> None:
        settings_path = absolute_source(settings_path)
        super().__init__()
        self.comparison_host = comparison_host
        self.shared_sources = comparison_host.shared_sources if comparison_host else SharedSources(self)
        self._closed = False
        self.setWindowTitle("D1ffler — File & Folder Comparison")
        self.setWindowIcon(QIcon(str(logo_asset_path())))
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)
        self.folder_results: dict[str, FileDifference] = {}
        self.folder_left: Path | None = None
        self.folder_right: Path | None = None
        self.settings = settings if isinstance(settings, SettingsState) else SettingsState(settings)
        self.metadata_compare_rule = bool(settings.get("compare_metadata", False))
        self.settings_path = settings_path
        from d1ffler.local_data import register_report_user
        register_report_user(settings_path.parent)
        self.comparison_changed.connect(self._protect_local_sources)
        self.diagnostics = (comparison_host.diagnostics if comparison_host else
                            DiagnosticStore(settings_path.parent / "diagnostics"))
        self._background_failures = {}
        self._recovery_store = RecoveryStore(settings_path)
        self._recovery_restoring = False
        self._context_jobs = []
        self._pending_context_view = None
        self._pending_context_preview = None
        self._folder_preview_target = None
        self._context_folder_restoring = False
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.timeout.connect(self._write_recovery_snapshot)
        self.panels: list[dict] = []
        self._loading_panel = False
        self._rendering_diff = False
        self._syncing_panel_scroll = False
        self._change_rows: list[int] = []
        self._change_index = -1
        self._hunks: list[tuple[int, int]] = []
        self._aligned_rows: list[list[int | None]] = []
        self._search_query = ""
        self._search_bar_requested = False
        self._search_update_timer = QTimer(self)
        self._search_update_timer.setSingleShot(True)
        self._search_update_timer.timeout.connect(self._update_search_state)
        self._active_panel_index = 0
        self.show_changed_files_only = False
        self._folder_path_query = ""
        self._content_search_query = ""
        self._pending_folder_content_hit = None
        self._folder_status_filter = "all"
        self._folder_status_counts = {status: 0 for status in FOLDER_STATUS_FILTERS}
        self._folder_visible_count = 0
        self._folder_expanded_paths: list[set[str]] = []
        self._folder_move_group_expanded: list[bool] = []
        self._folder_view_state: list[dict] = []
        self._folder_scroll_restore_active: list[bool] = []
        self._rebuilding_folder_trees = False
        self._folder_navigation_anchor: str | None = None
        self._folder_search_needs_reveal = False
        self.zoom_percent = 100
        self.show_special_characters = False
        self._loading_workspace = False
        self._folder_generation = 0
        self._folder_job_key = None
        self._folder_worker = None
        self._folder_jobs = []
        self._folder_rows = {}
        self._folder_metadata = {}
        self._folder_entry_maps = []
        self._folder_links: dict[str, list[LinkInfo | None]] = {}
        self._folder_moves: list[MoveMatch] = []
        self._folder_move_hidden_paths: set[str] = set()
        self._folder_move_groups: list[QTreeWidgetItem | None] = []
        self._folder_move_items: list[dict[MoveMatch, QTreeWidgetItem]] = []
        self._syncing_folder_selection = False
        self._folder_tree_items = []
        self._folder_tree_folders = []
        self._folder_display_pending = deque()
        self._folder_display_queued = set()
        self._folder_display_scheduled = False
        self._folder_excluded = 0
        self._folder_rule_error = ""
        self._folder_started = 0.0
        self._folder_partial = False
        self._folder_stale = False
        self._folder_cache = {}
        self._disk_watcher = QFileSystemWatcher(self)
        self._disk_watcher.fileChanged.connect(self._watched_path_changed)
        self._disk_watcher.directoryChanged.connect(self._watched_path_changed)
        self._disk_check_timer = QTimer(self)
        self._disk_check_timer.setSingleShot(True)
        self._disk_check_timer.timeout.connect(self._check_external_changes)
        self._binary_generation = 0
        self._binary_job_key = None
        self._binary_results = None
        self._binary_worker = None
        self._binary_jobs = []
        self._image_generation = 0
        self._image_job_key = None
        self._image_results = None
        self._image_worker = None
        self._image_jobs = []
        self._image_panel_refs = []
        self._image_difference_cache = {}
        self._syncing_image_scroll = False
        self._image_zoom = 100
        self._text_generation = 0
        self._text_worker = None
        self._text_pending = None
        self._text_jobs = []
        self._background_timer = QTimer(self)
        self._background_timer.setInterval(20)
        self._background_timer.timeout.connect(self._poll_background_jobs)
        self.sync_reports: list[dict] = []
        self.restore_reports: list[dict] = []
        self.review_notes: list[dict] = []
        self.workspace_text_rules: list[dict] = []
        self._active_workspace_name: str | None = None
        self._pending_named_workspace = None
        self._named_workspace_baselines = {}
        self._workspace_save_baseline = None
        self._settings_save_error = ""
        self.merge_mode = False
        self.managed_worktrees: dict[str, str] = absolute_worktrees(settings.get("managed_worktrees", []))
        self.merge_conflicts: list[Conflict] = []
        self.merge_resolutions: dict[int, str] = {}
        self.merge_automatic = 0
        self.merge_stale = False
        self._build_ui()
        self.comparison_changed.connect(self._update_save_action)
        self._update_save_action()
        QApplication.instance().installEventFilter(self)

    def _comparison_thread(self, worker, operation=None):
        executor = self.comparison_host.worker_pool if self.comparison_host else None
        return BackgroundTask(executor, worker, diagnostics=self.diagnostics, operation=operation,
                              on_queued=lambda: self.status.setText("Queued work · waiting for a background worker"))

    def _build_ui(self) -> None:
        toolbar = QToolBar("Tools", self)
        toolbar.setObjectName("leftToolbar")
        toolbar.setMovable(False)
        clear_button = QPushButton("Clear all")
        clear_button.setObjectName("toolbarButton")
        clear_button.setToolTip("Clear all comparison panels")
        clear_button.clicked.connect(self._clear_all)
        toolbar.addWidget(clear_button)
        self.merge_mode_button = QPushButton("Three-way merge")
        self.merge_mode_button.setObjectName("toolbarButton")
        self.merge_mode_button.setCheckable(True)
        self.merge_mode_button.setToolTip("Merge Base, Local, and Incoming into Result")
        self.merge_mode_button.toggled.connect(self._toggle_merge_mode)
        toolbar.addWidget(self.merge_mode_button)
        self.special_characters_button = QPushButton("¶")
        self.special_characters_button.setObjectName("toolbarButton")
        self.special_characters_button.setCheckable(True)
        self.special_characters_button.setToolTip("Show spaces, tabs, and line endings")
        self.special_characters_button.toggled.connect(self._toggle_special_characters)
        toolbar.addWidget(self.special_characters_button)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("toolbarButton")
        self.save_button.setToolTip("Save active panel (Ctrl+S)")
        self.save_button.clicked.connect(self._save_active_panel)
        toolbar.addWidget(self.save_button)
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        for button, redo in ((self.undo_button, False), (self.redo_button, True)):
            button.setObjectName("toolbarButton")
            button.setToolTip("Redo (Ctrl+Y / Ctrl+Shift+Z)" if redo else "Undo (Ctrl+Z)")
            button.clicked.connect(lambda _checked=False, r=redo: self._undo_active_panel(r))
            toolbar.addWidget(button)
        save_all_button = QPushButton("Save all")
        save_all_button.setObjectName("toolbarButton")
        save_all_button.setToolTip("Save all modified files")
        save_all_button.clicked.connect(self._save_all_dirty)
        toolbar.addWidget(save_all_button)
        save_workspace_button = QPushButton("Save workspace")
        save_workspace_button.setObjectName("toolbarButton")
        save_workspace_button.setToolTip("Save the current comparison setup")
        save_workspace_button.clicked.connect(self._save_named_workspace)
        toolbar.addWidget(save_workspace_button)
        self.workspaces_button = QPushButton("Workspaces")
        self.workspaces_button.setObjectName("toolbarButton")
        self.workspaces_button.setToolTip("Open a recent or saved workspace")
        self.workspaces_button.clicked.connect(self._show_workspace_menu)
        toolbar.addWidget(self.workspaces_button)
        review_notes_button = QPushButton("Review notes")
        review_notes_button.setObjectName("toolbarButton")
        review_notes_button.clicked.connect(self._show_review_notes)
        toolbar.addWidget(review_notes_button)
        export_review_button = QPushButton("Review report")
        export_review_button.setObjectName("toolbarButton")
        export_review_button.clicked.connect(self._export_review_report)
        toolbar.addWidget(export_review_button)
        export_comparison_button = QPushButton("Export comparison")
        export_comparison_button.setObjectName("toolbarButton")
        export_comparison_button.setToolTip("Export the current text or folder comparison without review notes")
        export_comparison_button.clicked.connect(self._export_comparison)
        toolbar.addWidget(export_comparison_button)
        apply_patch_button = QPushButton("Apply patch")
        apply_patch_button.setObjectName("toolbarButton")
        apply_patch_button.setToolTip("Preview and apply a unified text patch")
        apply_patch_button.clicked.connect(self._open_patch)
        toolbar.addWidget(apply_patch_button)
        self.previous_change_button = QPushButton("↑")
        self.previous_change_button.setObjectName("toolbarButton")
        self.previous_change_button.setToolTip("Previous change (Shift+F7)")
        self.previous_change_button.clicked.connect(lambda: self._navigate_change(-1))
        toolbar.addWidget(self.previous_change_button)
        self.next_change_button = QPushButton("↓")
        self.next_change_button.setObjectName("toolbarButton")
        self.next_change_button.setToolTip("Next change (F7)")
        self.next_change_button.clicked.connect(lambda: self._navigate_change(1))
        toolbar.addWidget(self.next_change_button)
        find_button = QPushButton("Find")
        find_button.setObjectName("toolbarButton")
        find_button.setToolTip("Find a folder path or search open text (Ctrl+F)")
        find_button.clicked.connect(self._open_find)
        toolbar.addWidget(find_button)
        self.changed_files_button = QPushButton("Changed")
        self.changed_files_button.setObjectName("toolbarButton")
        self.changed_files_button.setCheckable(True)
        self.changed_files_button.setToolTip("Show changed files only in folder comparisons")
        self.changed_files_button.toggled.connect(self._toggle_changed_files_only)
        toolbar.addWidget(self.changed_files_button)
        refresh_button = QPushButton("Refresh")
        refresh_button.setObjectName("toolbarButton")
        refresh_button.setToolTip("Refresh or retry the current comparison")
        refresh_button.clicked.connect(self._refresh_folder_comparison)
        toolbar.addWidget(refresh_button)
        diagnostics_button = QPushButton("Diagnostics…")
        diagnostics_button.setObjectName("toolbarButton")
        diagnostics_button.setToolTip("Inspect local error logs or export diagnostics")
        diagnostics_button.clicked.connect(self._open_diagnostics)
        toolbar.addWidget(diagnostics_button)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, toolbar)

        root = QWidget()
        self.setCentralWidget(root)
        self.zoom_indicator = QLabel(self)
        self.zoom_indicator.setObjectName("zoomIndicator")
        self.zoom_indicator.setVisible(False)
        self._zoom_hide_timer = QTimer(self)
        self._zoom_hide_timer.setSingleShot(True)
        self._zoom_hide_timer.timeout.connect(self.zoom_indicator.hide)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 24, 28, 16)
        outer.setSpacing(16)

        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("D1ffler")
        title.setObjectName("title")
        subtitle = QLabel("Compare files and folders with clarity")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        heading.addLayout(title_box)
        heading.addStretch()
        self.status = QLabel("Ready for a comparison")
        self.status.setObjectName("statusPill")
        heading.addWidget(self.status, alignment=Qt.AlignmentFlag.AlignTop)
        self.cancel_comparison_button = QPushButton("Cancel work")
        self.cancel_comparison_button.setObjectName("smallButton")
        self.cancel_comparison_button.clicked.connect(self._cancel_active_comparison)
        self.cancel_comparison_button.hide()
        heading.addWidget(self.cancel_comparison_button, alignment=Qt.AlignmentFlag.AlignTop)
        info_button = QPushButton("ⓘ")
        info_button.setObjectName("infoButton")
        info_button.setToolTip("About D1ffler")
        info_button.setFixedSize(40, 40)
        info_button.clicked.connect(self._open_info)
        heading.addWidget(info_button, alignment=Qt.AlignmentFlag.AlignTop)
        settings_button = QPushButton("⚙")
        settings_button.setObjectName("settingsButton")
        settings_button.setToolTip("Settings")
        settings_button.setFixedSize(40, 40)
        settings_button.clicked.connect(self._open_settings)
        heading.addWidget(settings_button, alignment=Qt.AlignmentFlag.AlignTop)
        text_rules_button = QPushButton("Text rules")
        text_rules_button.setObjectName("secondaryButton")
        text_rules_button.setToolTip("Configure comparison-only text masks")
        text_rules_button.clicked.connect(self._open_text_rules)
        heading.addWidget(text_rules_button, alignment=Qt.AlignmentFlag.AlignTop)
        self.folder_back_button = QPushButton("Back to folders")
        self.folder_back_button.setObjectName("secondaryButton")
        self.folder_back_button.setVisible(False)
        self.folder_back_button.clicked.connect(self._back_to_folders)
        heading.addWidget(self.folder_back_button, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(heading)

        self.folder_triage_bar = QWidget()
        triage_row = QHBoxLayout(self.folder_triage_bar)
        triage_row.setContentsMargins(0, 0, 0, 0)
        self.folder_path_search = QLineEdit()
        self.folder_path_search.setPlaceholderText("Find relative path in folders…")
        self.folder_path_search.setToolTip("Search file paths in the folder tree; Ctrl+F still searches open text")
        self.folder_path_search.textChanged.connect(self._folder_search_changed)
        triage_row.addWidget(self.folder_path_search, 2)
        search_contents_button = QPushButton("Search contents…")
        search_contents_button.setToolTip("Search text inside discovered folder files")
        search_contents_button.clicked.connect(self._open_content_search)
        triage_row.addWidget(search_contents_button)
        self.folder_status_combo = QComboBox()
        for status in FOLDER_STATUS_FILTERS:
            self.folder_status_combo.addItem(status.title(), status)
        self.folder_status_combo.setToolTip("Filter folder paths by status; counts may overlap across panels")
        self.folder_status_combo.currentIndexChanged.connect(self._folder_status_changed)
        triage_row.addWidget(self.folder_status_combo)
        self.folder_counts_label = QLabel("0 paths")
        triage_row.addWidget(self.folder_counts_label)
        self.folder_triage_bar.hide()
        outer.addWidget(self.folder_triage_bar)

        self.search_bar = QWidget()
        search_layout = QVBoxLayout(self.search_bar)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Find text across panels…")
        self.search_input.textChanged.connect(self._search_query_changed)
        self.search_input.returnPressed.connect(lambda: self._find_next(1))
        search_row.addWidget(self.search_input, 2)
        previous_match = QPushButton("Previous")
        previous_match.clicked.connect(lambda: self._find_next(-1))
        search_row.addWidget(previous_match)
        next_match = QPushButton("Next")
        next_match.clicked.connect(lambda: self._find_next(1))
        search_row.addWidget(next_match)
        self.search_case = QCheckBox("Case sensitive")
        self.search_case.toggled.connect(self._schedule_search_update)
        search_row.addWidget(self.search_case)
        self.search_whole_word = QCheckBox("Whole word")
        self.search_whole_word.toggled.connect(self._schedule_search_update)
        search_row.addWidget(self.search_whole_word)
        self.search_count = QLabel("0 matches")
        search_row.addWidget(self.search_count)
        close_search = QPushButton("×")
        close_search.setToolTip("Close text search (Esc)")
        close_search.clicked.connect(self._close_find)
        search_row.addWidget(close_search)
        search_layout.addLayout(search_row)
        replace_row = QHBoxLayout()
        self.replace_target = QLabel("Replace in Left")
        replace_row.addWidget(self.replace_target)
        self.replacement_input = QLineEdit()
        self.replacement_input.setPlaceholderText("Replacement text…")
        self.replacement_input.textChanged.connect(self._schedule_recovery)
        self.replacement_input.returnPressed.connect(self._replace_next)
        replace_row.addWidget(self.replacement_input, 1)
        self.replace_next_button = QPushButton("Replace Next")
        self.replace_next_button.clicked.connect(self._replace_next)
        replace_row.addWidget(self.replace_next_button)
        self.replace_all_button = QPushButton("Replace All…")
        self.replace_all_button.clicked.connect(self._replace_all)
        replace_row.addWidget(self.replace_all_button)
        search_layout.addLayout(replace_row)
        self.search_bar.hide()
        outer.addWidget(self.search_bar)
        self._folder_search_timer = QTimer(self)
        self._folder_search_timer.setSingleShot(True)
        self._folder_search_timer.timeout.connect(self._apply_folder_filters)
        self._folder_scroll_timer = QTimer(self)
        self._folder_scroll_timer.setSingleShot(True)
        self._folder_scroll_timer.timeout.connect(self._restore_folder_scroll_positions)

        self.merge_controls = QWidget()
        merge_row = QHBoxLayout(self.merge_controls)
        merge_row.setContentsMargins(0, 0, 0, 0)
        self.merge_summary = QLabel()
        merge_row.addWidget(self.merge_summary, 1)
        self.merge_conflict_choice = QComboBox()
        self.merge_conflict_choice.currentIndexChanged.connect(self._show_merge_conflict)
        merge_row.addWidget(self.merge_conflict_choice)
        for title, choice in (("Use Local", "local"), ("Use Incoming", "incoming"),
                              ("Use Base", "base"), ("Mark Manual", "manual")):
            button = QPushButton(title)
            button.setObjectName("smallButton")
            button.clicked.connect(lambda _checked=False, value=choice: self._resolve_merge_conflict(value))
            merge_row.addWidget(button)
        rebuild = QPushButton("Rebuild merge")
        rebuild.setObjectName("smallButton")
        rebuild.clicked.connect(self._build_merge_result)
        merge_row.addWidget(rebuild)
        self.merge_controls.hide()
        outer.addWidget(self.merge_controls)

        self.image_controls = QWidget()
        image_row = QHBoxLayout(self.image_controls)
        image_row.setContentsMargins(0, 0, 0, 0)
        image_row.addWidget(QLabel("Images"))
        self.image_mode = QComboBox()
        self.image_mode.addItems(["Side by side", "Overlay", "Pixel difference"])
        self.image_mode.currentIndexChanged.connect(self._update_image_view)
        image_row.addWidget(self.image_mode)
        self.image_target = QComboBox()
        self.image_target.currentIndexChanged.connect(self._update_image_view)
        image_row.addWidget(self.image_target)
        image_row.addWidget(QLabel("Overlay"))
        self.image_opacity = QSlider(Qt.Orientation.Horizontal)
        self.image_opacity.setRange(0, 100)
        self.image_opacity.setValue(50)
        self.image_opacity.setFixedWidth(110)
        self.image_opacity.valueChanged.connect(self._update_image_view)
        image_row.addWidget(self.image_opacity)
        zoom_out = QPushButton("−")
        zoom_out.clicked.connect(lambda: self._change_image_zoom(-25))
        image_row.addWidget(zoom_out)
        self.image_zoom_label = QLabel("100%")
        image_row.addWidget(self.image_zoom_label)
        zoom_in = QPushButton("+")
        zoom_in.clicked.connect(lambda: self._change_image_zoom(25))
        image_row.addWidget(zoom_in)
        image_row.addStretch()
        self.image_summary_button = QPushButton("Show byte summary")
        self.image_summary_button.setCheckable(True)
        self.image_summary_button.toggled.connect(self._update_image_view)
        image_row.addWidget(self.image_summary_button)
        self.image_controls.hide()
        outer.addWidget(self.image_controls)

        self.panel_area = QHBoxLayout()
        self.failure_notice = QLabel()
        self.failure_notice.setWordWrap(True)
        self.failure_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.failure_notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.failure_notice.setObjectName("comparisonFailure")
        self.failure_notice.hide()
        outer.addWidget(self.failure_notice)
        self.context_notice = QLabel()
        self.context_notice.setWordWrap(True)
        self.context_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.context_notice.hide()
        outer.addWidget(self.context_notice)
        self.text_comparison_notice = QLabel()
        self.text_comparison_notice.setWordWrap(True)
        self.text_comparison_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.text_comparison_notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_comparison_notice.hide()
        self._text_comparisons = []
        outer.addWidget(self.text_comparison_notice)
        self.panel_area.setSpacing(10)
        outer.addLayout(self.panel_area, 1)
        self.add_panel_button = QPushButton("+")
        self.add_panel_button.setObjectName("addPanelButton")
        self.add_panel_button.setToolTip("Add comparison panel")
        self.add_panel_button.setFixedWidth(42)
        self.add_panel_button.clicked.connect(self._add_panel)
        self.panel_area.addWidget(self.add_panel_button, 0, Qt.AlignmentFlag.AlignTop)

        self._add_panel()
        self._add_panel()
        self._refresh_diff()
        self.comparison_changed.connect(self._schedule_recovery)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.settings | {"compare_metadata": self.metadata_compare_rule}, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        old_rules = (self.settings["excluded_paths"], self.settings["ignore_gitignore"],
                     self.settings.get("follow_internal_links", False), self.metadata_compare_rule)
        new_settings = dialog.values()
        rules_changed = old_rules != (new_settings["excluded_paths"], new_settings["ignore_gitignore"],
                                      new_settings.get("follow_internal_links", False),
                                      new_settings.get("compare_metadata", False))
        if rules_changed:
            for index, panel in enumerate(self.panels):
                if panel["kind"] == "folder" and not self._confirm_replace_panel(index):
                    return
        self.settings.update(new_settings)
        self.metadata_compare_rule = bool(new_settings.get("compare_metadata", False))
        saved = self._persist_workspaces()
        if not saved:
            QMessageBox.warning(self, "Settings", f"Settings were applied but could not be saved to {self.settings_path}:\n"
                                f"{self._settings_save_error}\nUse Settings again to retry.")
        apply_theme(QApplication.instance(), self.settings["theme"], self.zoom_percent)
        for editor in self.findChildren(LineNumberTextEdit):
            editor.set_line_number_theme(self.settings["theme"])
        if self._comparison_mode() == "folder" and rules_changed:
            self._show_folder_trees()
            self._loading_panel = True
            for panel in self.panels:
                if panel["kind"] == "folder":
                    panel["editor"].clear()
                    panel["binary"] = False
                    panel["too_large"] = False
                    self._set_panel_clean(panel)
            self._loading_panel = False
        self._refresh_diff()
        self.status.setText(f"Settings applied · {THEMES[str(self.settings['theme'])]}" +
                            (" · not saved; use Settings to retry" if not saved else ""))

    def _active_text_rules(self):
        return [rule for rule in self.settings.get("text_rules", []) + self.workspace_text_rules
                if isinstance(rule, dict) and rule.get("enabled")]

    def _open_text_rules(self) -> None:
        sample = next((self._panel_text(panel) for panel in self.panels
                       if panel.get("kind") in {"file", "text"} and self._panel_text(panel)), "")
        dialog = TextRulesDialog(self.workspace_text_rules, self.settings.get("text_rules", []), sample, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.workspace_text_rules, self.settings["text_rules"] = dialog.result_rules()
        self._record_recent_workspace(persist=False)
        saved = self._persist_workspaces()
        if not saved:
            QMessageBox.warning(self, "Text rules", f"Could not save app defaults to {self.settings_path}:\n"
                                f"{self._settings_save_error}\nOpen Text rules again to retry.")
        self._folder_cache.clear()
        self._folder_job_key = None
        self._refresh_diff()
        self.status.setText("Text comparison rules applied" +
                            (" · not saved; open Text rules to retry" if not saved else ""))

    def _open_info(self) -> None:
        InfoDialog(self).exec()

    def _open_diagnostics(self) -> None:
        DiagnosticsDialog(self.diagnostics, self).exec()

    def _open_local_data(self) -> None:
        from d1ffler.local_data_dialog import LocalDataDialog
        LocalDataDialog(self).exec()

    def _protect_local_sources(self) -> None:
        from d1ffler.local_data import protect_local_sources
        try:
            paths = [panel.get(key) for panel in getattr(self, "panels", []) for key in ("path", "editing_path")]
            protect_local_sources(self.settings_path.parent, paths)
        except OSError as exc:
            self.status.setText(f"Local source protection could not be updated: {exc}")

    def eventFilter(self, watched, event) -> bool:
        if self._closed:
            return False
        if watched is self and event.type() == QEvent.Type.WindowActivate:
            self._disk_check_timer.start(100)
        if event.type() in {QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn}:
            for index, panel in enumerate(self.panels):
                editor = panel["editor"]
                if watched is editor or watched is editor.viewport():
                    self._active_panel_index = index
                    self._update_save_action()
                    self._schedule_search_update()
                    break
        if event.type() == QEvent.Type.KeyPress and isinstance(watched, QWidget) and self.isAncestorOf(watched):
            if event.key() == Qt.Key.Key_F7:
                self._navigate_change(-1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
                return True
            if event.key() == Qt.Key.Key_F3:
                self._find_next(-1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
                return True
            if event.key() == Qt.Key.Key_F and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._open_find()
                return True
            if event.key() == Qt.Key.Key_H and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._open_replace()
                return True
            if event.key() == Qt.Key.Key_Escape and not self.search_bar.isHidden() and (
                    watched is self.search_input or watched is self.replacement_input):
                self._close_find()
                return True
            if event.key() == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._save_active_panel()
                return True
        if event.type() == QEvent.Type.Wheel and isinstance(watched, QWidget) and self.isAncestorOf(watched):
            if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                for panel in self.panels:
                    tree = panel["tree"]
                    if watched is tree or watched is tree.viewport():
                        self._stop_folder_scroll_restore(panel["widget"])
                        break
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y() or event.pixelDelta().y()
                if delta:
                    steps = max(1, abs(delta) // 120)
                    self._set_zoom(self.zoom_percent + (10 * steps if delta > 0 else -10 * steps))
                return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_zoom_indicator()

    def _position_zoom_indicator(self) -> None:
        if not hasattr(self, "zoom_indicator"):
            return
        self.zoom_indicator.adjustSize()
        self.zoom_indicator.move((self.width() - self.zoom_indicator.width()) // 2, 18)

    def _set_zoom(self, percent: int) -> None:
        self.zoom_percent = max(50, min(300, percent))
        apply_theme(QApplication.instance(), self.settings["theme"], self.zoom_percent)
        self._refresh_diff()
        self.zoom_indicator.setText(f"{self.zoom_percent}%")
        self._position_zoom_indicator()
        self.zoom_indicator.show()
        self.zoom_indicator.raise_()
        self._zoom_hide_timer.start(1800)

    def _set_editor_special_characters(self, editor: QPlainTextEdit) -> None:
        option = editor.document().defaultTextOption()
        flags = option.flags()
        special_flags = QTextOption.Flag.ShowTabsAndSpaces
        option.setFlags(flags | special_flags if self.show_special_characters else flags & ~special_flags)
        editor.document().setDefaultTextOption(option)
        if isinstance(editor, LineNumberTextEdit):
            editor.set_show_line_endings(self.show_special_characters)
        editor.viewport().update()

    def _toggle_special_characters(self, checked: bool) -> None:
        self.show_special_characters = checked
        for editor in self.findChildren(QPlainTextEdit):
            self._set_editor_special_characters(editor)

    def _panel_name(self, index: int) -> str:
        if self.merge_mode:
            return ["Base", "Local", "Incoming", "Result"][index]
        return ["baseline", "2nd", "3rd", "4th"][index]

    def _toggle_merge_mode(self, enabled: bool) -> None:
        if enabled and self._comparison_mode() == "folder":
            QMessageBox.information(self, "Three-way merge", "Open text files or clear the folder comparison first.")
            self.merge_mode_button.blockSignals(True)
            self.merge_mode_button.setChecked(False)
            self.merge_mode_button.blockSignals(False)
            return
        if enabled and any(panel.get("binary") for panel in self.panels[:3]):
            QMessageBox.information(self, "Three-way merge", "Three-way merge requires text inputs.")
            self.merge_mode_button.blockSignals(True)
            self.merge_mode_button.setChecked(False)
            self.merge_mode_button.blockSignals(False)
            return
        if enabled and len(self.panels) == 4 and (
            self.panels[3]["kind"] is not None or self._panel_text(self.panels[3])
        ):
            choice = QMessageBox.question(
                self, "Three-way merge", "Replace the fourth panel with the merge Result?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                self.merge_mode_button.blockSignals(True)
                self.merge_mode_button.setChecked(False)
                self.merge_mode_button.blockSignals(False)
                return
        self.merge_mode = enabled
        self.merge_controls.setVisible(enabled)
        if enabled:
            self.merge_touched = False
            while len(self.panels) < 4:
                self._add_panel()
            result = self.panels[3]
            result["path"] = None
            result["editing_path"] = None
            result["saved_text"] = ""
            result["saved_text_with_endings"] = ""
            for index, panel in enumerate(self.panels):
                panel["name"].setText(self._panel_name(index))
                panel["custom_name"] = False
            self._build_merge_result()
        else:
            self.merge_conflicts = []
            self.merge_resolutions = {}
            self.merge_stale = False
            for index, panel in enumerate(self.panels):
                panel["name"].setText(self._panel_name(index))
                panel["custom_name"] = False
            self._refresh_diff()
        self.add_panel_button.setEnabled(not enabled and len(self.panels) < 4)
        for panel in self.panels:
            if panel.get("remove_button"):
                panel["remove_button"].setVisible(not enabled)
            panel["rename_button"].setEnabled(not enabled)
        self._update_panel_locking()
        self._schedule_recovery()

    def _merge_summary_text(self) -> None:
        unresolved = sum(conflict.number not in self.merge_resolutions for conflict in self.merge_conflicts)
        manual = sum(value == "manual" for value in self.merge_resolutions.values())
        suffix = " · sources changed; rebuild needed" if self.merge_stale else ""
        self.merge_summary.setText(
            f"Automatic merges: {self.merge_automatic} · Unresolved conflicts: {unresolved} · "
            f"Manual resolutions: {manual}{suffix}"
        )

    def _build_merge_result(self) -> None:
        if not self.merge_mode or len(self.panels) != 4:
            return
        if any(self.panels[index].get("binary") for index in range(3)):
            self.merge_summary.setText("Three-way merge requires text inputs")
            return
        if getattr(self, "merge_touched", False):
            choice = QMessageBox.question(
                self, "Rebuild merge", "Replace the current Result and its conflict resolutions?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        merged = merge_text(*(self._panel_text(self.panels[index]) for index in range(3)))
        self.merge_conflicts = list(merged.conflicts)
        self.merge_resolutions = {}
        self.merge_automatic = merged.automatic_merges
        self.merge_stale = False
        self.merge_touched = False
        result = self.panels[3]
        self._loading_panel = True
        result["editor"].setPlainText(merged.text)
        if result.get("editing_path") is None:
            local = self.panels[1]
            self._set_panel_encoding(result, local["encoding"], local["bom"])
        result["kind"] = "text"
        result["binary"] = False
        self._loading_panel = False
        self._update_panel_modified_state(result)
        self.merge_conflict_choice.clear()
        for conflict in self.merge_conflicts:
            location = f"Base line {conflict.start + 1}" if conflict.start == conflict.end else f"Base lines {conflict.start + 1}–{conflict.end}"
            self.merge_conflict_choice.addItem(f"Conflict {conflict.number} · {location}", conflict.number)
        self._merge_summary_text()
        self._refresh_diff()

    def _resolve_merge_conflict(self, choice: str) -> None:
        number = self.merge_conflict_choice.currentData()
        conflict = next((item for item in self.merge_conflicts if item.number == number), None)
        if conflict is None:
            return
        result = self.panels[3]
        if result["editor"].isReadOnly():
            return
        content = self._panel_text(result)
        if choice == "manual":
            if conflict.marker in content:
                self.status.setText("Edit or remove the conflict markers in Result before marking manual")
                return
        else:
            if conflict.marker not in content:
                self.status.setText("Conflict block was edited; resolve it manually")
                return
            replacement = {"local": conflict.local, "incoming": conflict.incoming, "base": conflict.base}[choice]
            content = content.replace(conflict.marker, replacement, 1)
        resolutions = {**self.merge_resolutions, number: choice}
        result["editor"].replace_logical_text(content, {str(key): value for key, value in resolutions.items()})
        self.merge_touched = True
        self._merge_summary_text()
        self.status.setText(f"Conflict {number}: {choice}")
        self._schedule_recovery()

    def _show_merge_conflict(self, _index: int) -> None:
        number = self.merge_conflict_choice.currentData()
        conflict = next((item for item in self.merge_conflicts if item.number == number), None)
        if conflict is None:
            return
        editor = self.panels[3]["editor"]
        cursor = editor.document().find(f"<<<<<<< LOCAL (conflict {number})")
        if not cursor.isNull():
            editor.setTextCursor(cursor)
            editor.ensureCursorVisible()

    @staticmethod
    def _file_icon(relative_path: str) -> QIcon:
        path = Path(relative_path)
        icon_name = FILENAME_ICON_MAP.get(path.name.casefold()) or FILE_ICON_MAP.get(path.suffix.casefold())
        if icon_name:
            asset_path = icon_asset_path(icon_name)
            if asset_path.is_file():
                return QIcon(str(asset_path))
        return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    def _confirm_replace_panel(self, index: int) -> bool:
        panel = self.panels[index]
        pasted = panel["kind"] == "text" and bool(self._panel_text(panel))
        if not panel.get("dirty") and not panel.get("recovery_pending") and not pasted:
            return True
        can_save = not (self.merge_mode and index != 3)
        buttons = (QMessageBox.StandardButton.Save if can_save else QMessageBox.StandardButton.NoButton)
        buttons |= QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        choice = QMessageBox.warning(
            self, "Unsaved file changes",
            f"Save changes in {panel['name'].text()} before replacing this panel?",
            buttons,
            QMessageBox.StandardButton.Save if can_save else QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        return choice != QMessageBox.StandardButton.Save or self._save_panel_file(index)

    def _clear_all(self) -> bool:
        for index in range(len(self.panels)):
            if not self._confirm_replace_panel(index):
                return False
        self._show_folder_trees()
        self._reset_comparison_context()
        self.sync_reports = []
        self.restore_reports = []
        self.review_notes = []
        self.workspace_text_rules = []
        self._active_workspace_name = None
        self._loading_panel = True
        while len(self.panels) > (4 if self.merge_mode else 2):
            panel = self.panels.pop()
            self._folder_expanded_paths.pop()
            self._folder_move_group_expanded.pop()
            panel["widget"].deleteLater()
        for index, panel in enumerate(self.panels):
            panel["kind"], panel["path"], panel["binary"] = None, None, False
            panel["image_source"] = None
            panel["too_large"] = False
            self._set_panel_encoding(panel, "utf-8")
            self._set_git_source(panel, None)
            panel["name"].setText(self._panel_name(index))
            panel["custom_name"] = False
            panel["editor"].clear()
            self._set_panel_clean(panel)
            panel["editor"].setReadOnly(False)
            panel["editor"].setPlaceholderText("Paste or type text here…\nRight-click to add a file or folder.")
            panel["view_stack"].setCurrentIndex(0)
            panel["metadata_display"].hide()
        self._loading_panel = False
        self.metadata_compare_rule = bool(self.settings.get("compare_metadata", False))
        self.add_panel_button.setEnabled(not self.merge_mode)
        if self.merge_mode:
            self.merge_touched = False
            self._build_merge_result()
        self._refresh_diff()
        self.status.setText("Ready for a comparison")
        self._schedule_recovery()
        return True

    def _open_sync_plan(
        self, source_index: int, target_index: int | None = None,
        selected_relative: str | None = None,
    ) -> None:
        if any(is_zip_source(panel["path"]) for panel in self.panels if panel["kind"] == "folder"):
            QMessageBox.information(self, "Folder sync", "ZIP archives are read-only. Sync with ZIP sources is unavailable.")
            return
        scopes = [SourceScope(panel["path"], folder=True) for panel in self.panels
                  if panel["kind"] == "folder"]
        if self.shared_sources.prepare(scopes, self, "Folder sync") is None:
            return
        self._show_folder_trees()
        SyncPlanDialog(self, source_index, target_index, selected_relative).exec()

    def _open_patch(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(
            self, "Open unified patch", "", "Patch files (*.patch *.diff);;All files (*)")
        if not filename:
            return
        try:
            data = Path(filename).read_bytes()
            if len(data) > 8 * 1024 * 1024:
                raise PatchError("Patch exceeds the 8 MiB preview limit")
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                source_text = data.decode("utf-16")
            else:
                try:
                    source_text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    source_text = data.decode("cp1252")
            files = parse_patch(source_text)
        except (OSError, UnicodeError, PatchError) as exc:
            QMessageBox.warning(self, "Open unified patch", f"Could not read patch:\n{exc}")
            return
        PatchDialog(self, source_text, files, Path(filename)).exec()

    def _open_content_search(self) -> None:
        if self._comparison_mode() != "folder":
            return
        FolderContentSearchDialog(self).exec()

    def _show_sync_reports(self) -> None:
        reports = list(reversed(self.sync_reports))
        labels = [f"{number}. {item.get('started_at', 'Unknown time')} · {len(item.get('outcomes', []))} operations"
                  for number, item in enumerate(reports, 1)]
        choice, accepted = QInputDialog.getItem(self, "Sync reports", "Report:", labels, 0, False)
        if accepted:
            SyncReportDialog(reports[labels.index(choice)], self, app=self).exec()

    def _show_restore_reports(self) -> None:
        reports = list(reversed(self.restore_reports))
        labels = [f"{number}. {item.get('started_at', 'Unknown time')} · {len(item.get('outcomes', []))} actions"
                  for number, item in enumerate(reports, 1)]
        choice, accepted = QInputDialog.getItem(self, "Restore reports", "Report:", labels, 0, False)
        if accepted:
            RestoreReportDialog(reports[labels.index(choice)], self).exec()

    def _shutdown_background_jobs(self) -> None:
        self._background_timer.stop()
        for thread, worker, *_rest in self._context_jobs:
            worker.cancel_event.set()
        for thread, _worker, *_rest in self._context_jobs:
            thread.join()
        self._context_jobs.clear()
        self._folder_generation += 1
        self._binary_generation += 1
        self._image_generation += 1
        self._text_generation += 1
        for thread, worker, _messages, _generation in list(self._folder_jobs):
            worker.cancel_event.set()
        for thread, worker, _messages, _generation in list(self._binary_jobs):
            worker.cancel_event.set()
        for thread, worker, _messages, _generation in list(self._image_jobs):
            worker.cancel_event.set()
        for thread, worker, _messages, _generation, _panels, _lines in list(self._text_jobs):
            worker.cancel_event.set()
        for thread, _worker, _messages, _generation in list(self._folder_jobs):
            thread.join()
        for thread, _worker, _messages, _generation in list(self._binary_jobs):
            thread.join()
        for thread, _worker, _messages, _generation in list(self._image_jobs):
            thread.join()
        for thread, _worker, _messages, _generation, _panels, _lines in list(self._text_jobs):
            thread.join()
        self._folder_jobs.clear()
        self._binary_jobs.clear()
        self._image_jobs.clear()
        self._text_jobs.clear()
        self._folder_worker = None
        self._binary_worker = None
        self._image_worker = None
        self._text_worker = None

    def _prepare_close(self, *, tab_close=False) -> ClosePlan | None:
        """Save explicitly requested data, but retain discards until close commits."""
        discard_workspace = False
        discard_panels = ()
        if any(job.view is self for job in self.shared_sources.pending):
            QMessageBox.information(self, "Operation in progress", "Wait for this comparison's disk operation to finish before closing it.")
            return None
        if self._pending_named_workspace is not None:
            choice = QMessageBox.warning(
                self, "Unsaved workspace", f"Workspace '{self._pending_named_workspace['name']}' has not been saved.\n"
                "Save it before closing this comparison?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save)
            if choice == QMessageBox.StandardButton.Cancel:
                return None
            if choice == QMessageBox.StandardButton.Save:
                self._save_named_workspace()
                if self._pending_named_workspace is not None:
                    return None
            else:
                discard_workspace = True
        dirty_panels = [
            index for index, panel in enumerate(self.panels)
            if ((panel.get("dirty") and panel.get("editing_path") is not None) or
                (tab_close and (panel.get("dirty") or panel.get("recovery_pending") or
                               (panel["kind"] == "text" and bool(self._panel_text(panel))))))
        ]
        if dirty_panels:
            files = "\n".join(f"• {self.panels[index]['editing_path'].name if self.panels[index]['editing_path'] else self.panels[index]['name'].text()}"
                              for index in dirty_panels)
            choice = QMessageBox.warning(
                self,
                "Unsaved file changes",
                f"Save changes to these files before closing?\n\n{files}",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                return None
            if choice == QMessageBox.StandardButton.Save:
                for index in dirty_panels:
                    if not self._save_panel_file(index):
                        return None
            else:
                discard_panels = tuple(dirty_panels)
        # Back up the live buffers, including proposed discards. A later tab or
        # recovery warning can still cancel; its peers must remain recoverable.
        if not self._write_recovery_snapshot():
            choice = QMessageBox.warning(
                self, "Recovery unavailable", "Unsaved comparison text could not be backed up. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return None
        return ClosePlan(discard_panels, discard_workspace, tab_close)

    def _commit_close(self, plan: ClosePlan) -> None:
        """Apply discards only after every cancellable close check has passed."""
        if plan.discard_workspace:
            self._pending_named_workspace = None
        for index in plan.discard_panels:
            panel = self.panels[index]
            if plan.tab_close and panel["kind"] == "text":
                self._loading_panel = True
                try:
                    panel["editor"].clear()
                finally:
                    self._loading_panel = False
            self._set_panel_clean(panel, panel["editing_path"], panel.get("disk_fingerprint"))
        self._recovery_timer.stop()
        if plan.discard_panels and not self._write_recovery_snapshot():
            # Close has committed: allowing Cancel here would leave other tabs'
            # discards applied. Retain the last backup if cleanup fails.
            QMessageBox.warning(
                self, "Recovery cleanup failed",
                "Discarded edits could not be removed from recovery and may be offered at the next launch.",
                QMessageBox.StandardButton.Ok,
            )

    def closeEvent(self, event) -> None:
        if self._closed:
            event.accept()
            return
        plan = self._prepare_close()
        if plan is None:
            event.ignore()
            return
        self._commit_close(plan)
        self._shutdown_background_jobs()
        self._closed = True
        for timer in self.findChildren(QTimer):
            timer.stop()
        QApplication.instance().removeEventFilter(self)
        self._recovery_store.release()
        from d1ffler.local_data import unregister_report_user
        unregister_report_user(self.settings_path.parent)
        event.accept()

    def _result_editor(self, name: str, content: str) -> tuple[QFrame, QPlainTextEdit]:
        frame = QFrame()
        frame.setObjectName("resultPanel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        label = QLabel(name)
        label.setObjectName("resultName")
        layout.addWidget(label)
        editor = LineNumberTextEdit()
        editor.setReadOnly(True)
        editor.setFont(QFont("Cascadia Mono", 10))
        editor.set_line_number_theme(self.settings["theme"])
        self._set_editor_special_characters(editor)
        editor.setPlainText(content)
        layout.addWidget(editor, 1)
        return frame, editor

    def _render_text_diff(self, panels: list[dict]) -> None:
        if not any(panel["kind"] for panel in panels):
            empty = QLabel("Add a file, folder, or paste text into the panels above to compare.")
            empty.setObjectName("emptyState")
            self.diff_area.addWidget(empty)
            return
        editors = []
        for panel in panels:
            content = panel["editor"].toPlainText()
            frame, editor = self._result_editor(panel["name"].text(), content)
            self.diff_area.addWidget(frame, 1)
            editors.append(editor)
        baseline = panels[0]["editor"].toPlainText().splitlines()
        all_lines = [panel["editor"].toPlainText().splitlines() for panel in panels]
        max_lines = max((len(lines) for lines in all_lines), default=0)
        line_color = "#eef7ee" if self.settings["theme"] == "light" else "#182b20"
        inline_color = "#b8e5bf" if self.settings["theme"] == "light" else "#2f7d4e"
        for editor, lines in zip(editors, all_lines):
            selections = []
            for line_number in range(max_lines):
                value = lines[line_number] if line_number < len(lines) else ""
                reference = baseline[line_number] if line_number < len(baseline) else ""
                if value == reference:
                    continue
                block = editor.document().findBlockByNumber(line_number)
                if not block.isValid():
                    continue
                line_selection = QTextEdit.ExtraSelection()
                line_selection.cursor = editor.textCursor()
                line_selection.cursor.setPosition(block.position())
                line_selection.cursor.movePosition(line_selection.cursor.MoveOperation.EndOfBlock, line_selection.cursor.MoveMode.KeepAnchor)
                line_selection.format.setBackground(QColor(line_color))
                selections.append(line_selection)
                if value:
                    for tag, start, end, _a, _b in difflib.SequenceMatcher(None, value, reference).get_opcodes():
                        if tag == "equal" or start == end:
                            continue
                        span = QTextEdit.ExtraSelection()
                        span.cursor = editor.textCursor()
                        span.cursor.setPosition(block.position() + start)
                        span.cursor.setPosition(block.position() + end, span.cursor.MoveMode.KeepAnchor)
                        span.format.setBackground(QColor(inline_color))
                        selections.append(span)
            editor.setExtraSelections(selections)

    def _render_folder_diff(self, panels: list[dict]) -> None:
        if not panels:
            return
        exclusions, ignore_whitespace, ignore_line_endings, ignore_gitignore = self._folder_compare_options()
        files_by_panel = []
        for panel in panels:
            files_by_panel.append(folder_files(panel["path"], exclusions, ignore_gitignore)[0])
        all_files = sorted(set().union(*files_by_panel))
        status_by_panel = [{relative: "identical" for relative in files_by_panel[0]}]
        for files in files_by_panel[1:]:
            result_map = {
                item.relative_path: item.status
                for item in compare_folder_file_maps(
                    files_by_panel[0], files, ignore_whitespace, ignore_line_endings
                )
            }
            status_by_panel.append(result_map)
        for panel_index, panel in enumerate(panels):
            files = files_by_panel[panel_index]
            lines = []
            for relative in all_files:
                if relative in files:
                    status = status_by_panel[panel_index].get(relative, "identical")
                    marker = "~ " if status == "modified" else "+ " if status == "added" else "  "
                    lines.append(marker + relative)
                else:
                    lines.append(f"  does not exist in {panel['name'].text()}: {relative}")
            frame, editor = self._result_editor(panel["name"].text(), "\n".join(lines))
            self.diff_area.addWidget(frame, 1)
            selections = []
            for row, relative in enumerate(all_files):
                if relative not in files or status_by_panel[panel_index].get(relative) in {"modified", "added"}:
                    block = editor.document().findBlockByNumber(row)
                    selection = QTextEdit.ExtraSelection()
                    selection.cursor = editor.textCursor()
                    selection.cursor.setPosition(block.position())
                    selection.cursor.movePosition(selection.cursor.MoveOperation.EndOfBlock, selection.cursor.MoveMode.KeepAnchor)
                    selection.format.setBackground(QColor("#eef7ee" if self.settings["theme"] == "light" else "#182b20"))
                    selections.append(selection)
            editor.setExtraSelections(selections)

    def _file_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 18, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName("card")
        controls = QVBoxLayout(card)
        controls.setContentsMargins(18, 16, 18, 16)
        self.file_left = PathRow("Left file", self._choose_file_left)
        self.file_right = PathRow("Right file", self._choose_file_right)
        controls.addWidget(self.file_left)
        controls.addWidget(self.file_right)
        actions = QHBoxLayout()
        actions.addStretch()
        compare = QPushButton("Compare files")
        compare.setObjectName("primaryButton")
        compare.clicked.connect(self._compare_files)
        actions.addWidget(compare)
        controls.addLayout(actions)
        self.file_input_tabs = QTabWidget()
        self.file_input_tabs.setDocumentMode(True)
        self.file_input_tabs.addTab(card, "Compare files")

        text_card = QFrame()
        text_card.setObjectName("card")
        text_layout = QVBoxLayout(text_card)
        text_layout.setContentsMargins(18, 16, 18, 16)
        text_inputs = QHBoxLayout()
        self.left_text_input = self._side_editor("Paste or type left text…", editable=True)
        self.right_text_input = self._side_editor("Paste or type right text…", editable=True)
        text_inputs.addWidget(self.left_text_input)
        text_inputs.addWidget(self.right_text_input)
        text_layout.addLayout(text_inputs)
        text_actions = QHBoxLayout()
        text_actions.addStretch()
        compare_text = QPushButton("Compare pasted text")
        compare_text.setObjectName("primaryButton")
        compare_text.clicked.connect(self._compare_pasted_text)
        text_actions.addWidget(compare_text)
        text_layout.addLayout(text_actions)
        self.file_input_tabs.addTab(text_card, "Paste text")
        layout.addWidget(self.file_input_tabs)

        view_bar = QHBoxLayout()
        view_bar.addWidget(QLabel("View"))
        self.unified_view_button = QPushButton("Unified")
        self.side_view_button = QPushButton("Side by side")
        self.unified_view_button.setCheckable(True)
        self.side_view_button.setCheckable(True)
        self.unified_view_button.clicked.connect(lambda: self._set_diff_view(0))
        self.side_view_button.clicked.connect(lambda: self._set_diff_view(1))
        view_bar.addWidget(self.unified_view_button)
        view_bar.addWidget(self.side_view_button)
        view_bar.addStretch()
        layout.addLayout(view_bar)

        self.file_views = QStackedWidget()
        self.file_text = QPlainTextEdit()
        self.file_text.setReadOnly(True)
        self.file_text.setPlaceholderText("Your unified diff will appear here…")
        self.file_text.setFont(QFont("Cascadia Mono", 10))
        self.file_highlighter = DiffHighlighter(self.file_text.document(), self.settings["theme"])
        self.file_views.addWidget(self.file_text)

        side_by_side = QWidget()
        side_layout = QHBoxLayout(side_by_side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.left_side_text = self._side_editor("Left file")
        self.right_side_text = self._side_editor("Right file")
        self.left_side_text.verticalScrollBar().valueChanged.connect(
            self.right_side_text.verticalScrollBar().setValue
        )
        self.right_side_text.verticalScrollBar().valueChanged.connect(
            self.left_side_text.verticalScrollBar().setValue
        )
        side_layout.addWidget(self.left_side_text)
        side_layout.addWidget(self.right_side_text)
        self.file_views.addWidget(side_by_side)
        layout.addWidget(self.file_views, 1)
        self._set_diff_view(0)
        return tab

    @staticmethod
    def _side_editor(placeholder: str, editable: bool = False) -> QPlainTextEdit:
        editor = QPlainTextEdit()
        editor.setReadOnly(not editable)
        editor.setPlaceholderText(placeholder)
        editor.setFont(QFont("Cascadia Mono", 10))
        return editor

    def _set_diff_view(self, index: int) -> None:
        self.file_views.setCurrentIndex(index)
        self.unified_view_button.setChecked(index == 0)
        self.side_view_button.setChecked(index == 1)

    def _show_side_by_side(self, left: Path, right: Path) -> None:
        self._show_side_by_side_between(
            read_text(left), read_text(right), self.left_side_text, self.right_side_text
        )

    def _show_side_by_side_between(
        self, left_lines: list[str], right_lines: list[str],
        left_editor: QPlainTextEdit, right_editor: QPlainTextEdit
    ) -> None:
        left_display: list[str] = []
        right_display: list[str] = []
        left_changed: set[int] = set()
        right_changed: set[int] = set()
        changed_pairs: list[tuple[int, int, str, str]] = []
        matcher = difflib.SequenceMatcher(None, left_lines, right_lines)

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                left_display.extend(line.rstrip("\n\r") for line in left_lines[i1:i2])
                right_display.extend(line.rstrip("\n\r") for line in right_lines[j1:j2])
                continue
            rows = max(i2 - i1, j2 - j1)
            for offset in range(rows):
                left_index, right_index = len(left_display), len(right_display)
                if offset < i2 - i1:
                    left_text = left_lines[i1 + offset].rstrip("\n\r")
                    left_display.append(left_text)
                    left_changed.add(left_index)
                else:
                    left_text = ""
                    left_display.append("")
                    left_changed.add(left_index)
                if offset < j2 - j1:
                    right_text = right_lines[j1 + offset].rstrip("\n\r")
                    right_display.append(right_text)
                    right_changed.add(right_index)
                else:
                    right_text = ""
                    right_display.append("")
                    right_changed.add(right_index)
                changed_pairs.append((left_index, right_index, left_text, right_text))

        left_editor.setPlainText("\n".join(left_display))
        right_editor.setPlainText("\n".join(right_display))
        if self.settings["theme"] == "light":
            left_color, right_color = "#fbe3e5", "#e2f3e5"
        else:
            left_color, right_color = "#401e25", "#12351c"
        if self.settings["theme"] == "light":
            left_inline, right_inline = "#f4a6ad", "#a7dfb0"
        else:
            left_inline, right_inline = "#8f3942", "#2f7d4e"
        left_spans = [(left_index, left_text, right_text, left_inline) for left_index, _, left_text, right_text in changed_pairs]
        right_spans = [(right_index, right_text, left_text, right_inline) for _, right_index, left_text, right_text in changed_pairs]
        self._mark_side_lines(left_editor, left_changed, left_color, left_spans)
        self._mark_side_lines(right_editor, right_changed, right_color, right_spans)

    def _compare_pasted_text(self) -> None:
        left_lines = self.left_text_input.toPlainText().splitlines(keepends=True)
        right_lines = self.right_text_input.toPlainText().splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            left_lines, right_lines, fromfile="Left pasted text", tofile="Right pasted text", lineterm=""
        ))
        self.file_text.setPlainText("Pasted text is identical.\n" if not diff else "".join(
            line if line.endswith("\n") else line + "\n" for line in diff
        ))
        self._show_side_by_side_between(
            left_lines, right_lines, self.left_side_text, self.right_side_text
        )
        self.status.setText("Compared pasted text")

    @staticmethod
    def _mark_side_lines(
        editor: QPlainTextEdit,
        lines: set[int],
        color: str,
        spans: list[tuple[int, str, str, str]] | None = None,
    ) -> None:
        selections = []
        for line_number in lines:
            block = editor.document().findBlockByNumber(line_number)
            selection = QTextEdit.ExtraSelection()
            selection.cursor = editor.textCursor()
            selection.cursor.setPosition(block.position())
            selection.cursor.movePosition(selection.cursor.MoveOperation.EndOfBlock, selection.cursor.MoveMode.KeepAnchor)
            selection.format.setBackground(QColor(color))
            selections.append(selection)
        for line_number, text, other_text, inline_color in spans or []:
            if not text:
                continue
            block = editor.document().findBlockByNumber(line_number)
            matcher = difflib.SequenceMatcher(None, text, other_text)
            for tag, start, end, _other_start, _other_end in matcher.get_opcodes():
                if tag == "equal" or start == end:
                    continue
                selection = QTextEdit.ExtraSelection()
                selection.cursor = editor.textCursor()
                selection.cursor.setPosition(block.position() + start)
                selection.cursor.setPosition(block.position() + end, selection.cursor.MoveMode.KeepAnchor)
                selection.format.setBackground(QColor(inline_color))
                selections.append(selection)
        editor.setExtraSelections(selections)

    def _folder_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 18, 0, 0)
        layout.setSpacing(12)
        card = QFrame()
        card.setObjectName("card")
        controls = QVBoxLayout(card)
        controls.setContentsMargins(18, 16, 18, 16)
        self.folder_left_entry = PathRow("Left folder", self._choose_folder_left)
        self.folder_right_entry = PathRow("Right folder", self._choose_folder_right)
        controls.addWidget(self.folder_left_entry)
        controls.addWidget(self.folder_right_entry)
        actions = QHBoxLayout()
        actions.addStretch()
        compare = QPushButton("Compare folders")
        compare.setObjectName("primaryButton")
        compare.clicked.connect(self._compare_folders)
        actions.addWidget(compare)
        controls.addLayout(actions)
        layout.addWidget(card)

        view_bar = QHBoxLayout()
        view_bar.addWidget(QLabel("Open file diff as"))
        self.folder_unified_button = QPushButton("Unified")
        self.folder_side_button = QPushButton("Side by side")
        self.folder_unified_button.setCheckable(True)
        self.folder_side_button.setCheckable(True)
        self.folder_unified_button.clicked.connect(lambda: self._set_folder_diff_view(0))
        self.folder_side_button.clicked.connect(lambda: self._set_folder_diff_view(1))
        view_bar.addWidget(self.folder_unified_button)
        view_bar.addWidget(self.folder_side_button)
        view_bar.addStretch()
        layout.addLayout(view_bar)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderLabels(["Relative path", "Status"])
        self.folder_tree.setColumnWidth(0, 650)
        self.folder_tree.itemDoubleClicked.connect(self._open_selected_file_diff)
        splitter.addWidget(self.folder_tree)
        self.folder_detail_views = QStackedWidget()
        self.folder_detail = QPlainTextEdit()
        self.folder_detail.setReadOnly(True)
        self.folder_detail.setPlaceholderText("Select a file to see its comparison details…")
        self.folder_detail_views.addWidget(self.folder_detail)
        folder_side = QWidget()
        folder_side_layout = QHBoxLayout(folder_side)
        folder_side_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_left_preview = self._side_editor("Left file")
        self.folder_right_preview = self._side_editor("Right file")
        folder_side_layout.addWidget(self.folder_left_preview)
        folder_side_layout.addWidget(self.folder_right_preview)
        self.folder_detail_views.addWidget(folder_side)
        self.folder_tree.itemSelectionChanged.connect(self._preview_selected_file_diff)
        splitter.addWidget(self.folder_detail_views)
        splitter.setSizes([420, 160])
        layout.addWidget(splitter, 1)
        self._set_folder_diff_view(0)
        return tab

    def _set_folder_diff_view(self, index: int) -> None:
        self.folder_unified_button.setChecked(index == 0)
        self.folder_side_button.setChecked(index == 1)
        if hasattr(self, "folder_detail_views"):
            self.folder_detail_views.setCurrentIndex(index)
            self._preview_selected_file_diff()

    def _preview_selected_file_diff(self) -> None:
        selected = self.folder_tree.selectedItems()
        if not selected or self.folder_left is None or self.folder_right is None:
            return
        item = selected[0]
        if item.data(0, Qt.ItemDataRole.UserRole + 1) != "file":
            return
        relative = item.data(0, Qt.ItemDataRole.UserRole)
        result = self.folder_results.get(relative)
        if result is None:
            return
        left, right = self.folder_left / Path(relative), self.folder_right / Path(relative)
        if result.status == "added":
            self.folder_detail.setPlainText(f"Only on right\n\n{right}")
            self.folder_right_preview.setPlainText("\n".join(read_text(right)))
            self.folder_left_preview.clear()
        elif result.status == "removed":
            self.folder_detail.setPlainText(f"Only on left\n\n{left}")
            self.folder_left_preview.setPlainText("\n".join(read_text(left)))
            self.folder_right_preview.clear()
        elif result.status == "identical":
            self.folder_detail.setPlainText("Files are identical.")
            content = "\n".join(read_text(left))
            self.folder_left_preview.setPlainText(content)
            self.folder_right_preview.setPlainText(content)
        else:
            self.folder_detail.setPlainText("Double-click to open the full file diff.")
            self._show_side_by_side_between(
                read_text(left), read_text(right), self.folder_left_preview, self.folder_right_preview
            )

    def _choose_file_left(self) -> None:
        self._set_entry(self.file_left.edit, QFileDialog.getOpenFileName(self, "Choose left file")[0])

    def _choose_file_right(self) -> None:
        self._set_entry(self.file_right.edit, QFileDialog.getOpenFileName(self, "Choose right file")[0])

    def _choose_folder_left(self) -> None:
        self._set_entry(self.folder_left_entry.edit, QFileDialog.getExistingDirectory(self, "Choose left folder"))

    def _choose_folder_right(self) -> None:
        self._set_entry(self.folder_right_entry.edit, QFileDialog.getExistingDirectory(self, "Choose right folder"))

    @staticmethod
    def _set_entry(entry: QLineEdit, value: str) -> None:
        if value:
            entry.setText(value)

    def _compare_files(self) -> None:
        left, right = Path(self.file_left.edit.text()), Path(self.file_right.edit.text())
        if not left.is_file() or not right.is_file():
            QMessageBox.warning(self, "File diff", "Select two existing files.")
            return
        try:
            diff = compare_files(left, right)
        except OSError as exc:
            QMessageBox.critical(self, "File diff", str(exc))
            return
        self.file_text.setPlainText("Files are identical.\n" if not diff else "".join(
            line if line.endswith("\n") else line + "\n" for line in diff
        ))
        self._show_side_by_side(left, right)
        self.status.setText(f"Compared {left.name} and {right.name}")

    def _compare_folders(self) -> None:
        left, right = Path(self.folder_left_entry.edit.text()), Path(self.folder_right_entry.edit.text())
        if not left.is_dir() or not right.is_dir():
            QMessageBox.warning(self, "Folder diff", "Select two existing folders.")
            return
        try:
            results = compare_folders(left, right, ignore_gitignore=bool(self.settings["ignore_gitignore"]))
        except (OSError, GitError) as exc:
            QMessageBox.critical(self, "Folder diff", str(exc))
            return
        self.folder_left, self.folder_right = left, right
        self.folder_results = {item.relative_path: item for item in results}
        self.folder_tree.clear()
        colors = {"added": "#72d49a", "removed": "#ff8585", "modified": "#f3c969", "error": "#ff8585"}

        def folder_status(prefix: str) -> str:
            statuses = [
                item.status for item in results
                if item.relative_path == prefix or item.relative_path.startswith(prefix + "/")
            ]
            if "error" in statuses:
                return "error"
            if "modified" in statuses or ("added" in statuses and "removed" in statuses):
                return "modified"
            if statuses and all(status == "added" for status in statuses):
                return "added"
            if statuses and all(status == "removed" for status in statuses):
                return "removed"
            return "identical"

        folder_items: dict[str, QTreeWidgetItem] = {}
        for result in results:
            parts = Path(result.relative_path).parts
            parent = None
            path_parts: list[str] = []
            for part in parts[:-1]:
                path_parts.append(part)
                folder_path = "/".join(path_parts)
                if folder_path not in folder_items:
                    status = folder_status(folder_path)
                    folder_item = QTreeWidgetItem([part, status.title()])
                    folder_item.setData(0, Qt.ItemDataRole.UserRole, folder_path)
                    folder_item.setData(0, Qt.ItemDataRole.UserRole + 1, "folder")
                    folder_item.setForeground(0, QColor(colors.get(status, "#98a2b3")))
                    folder_item.setForeground(1, QColor(colors.get(status, "#98a2b3")))
                    if parent is None:
                        self.folder_tree.addTopLevelItem(folder_item)
                    else:
                        parent.addChild(folder_item)
                    folder_items[folder_path] = folder_item
                parent = folder_items[folder_path]

            item = QTreeWidgetItem([parts[-1], result.status.title()])
            item.setData(0, Qt.ItemDataRole.UserRole, result.relative_path)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
            item.setForeground(1, QColor(colors.get(result.status, "#98a2b3")))
            item.setForeground(0, QColor(colors.get(result.status, "#98a2b3")))
            if parent is None:
                self.folder_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
        self.folder_tree.expandAll()
        changed = sum(result.status != "identical" for result in results)
        self.status.setText(f"{len(results)} files · {changed} changed")
        self.folder_detail.setPlainText("Double-click a changed file to open its file diff.")

    def _open_selected_file_diff(self, item: QTreeWidgetItem, _column: int) -> None:
        if self.folder_left is None or self.folder_right is None:
            return
        if item.data(0, Qt.ItemDataRole.UserRole + 1) != "file":
            return
        relative = item.data(0, Qt.ItemDataRole.UserRole)
        result = self.folder_results.get(relative)
        if result is None:
            return
        left, right = self.folder_left / Path(relative), self.folder_right / Path(relative)
        if result.status == "added":
            self.folder_detail.setPlainText(f"Only on right\n\n{right}")
        elif result.status == "removed":
            self.folder_detail.setPlainText(f"Only on left\n\n{left}")
        elif result.status == "identical":
            self.folder_detail.setPlainText("Files are identical.")
        else:
            self.file_left.edit.setText(str(left))
            self.file_right.edit.setText(str(right))
            self._compare_files()
            self._set_diff_view(1 if self.folder_side_button.isChecked() else 0)
            self.tabs.setCurrentIndex(0)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--verify-package":
        from d1ffler.packaged_verification import main as verify_package
        sys.exit(verify_package(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        from d1ffler.headless_compare import run_compare
        sys.exit(run_compare(sys.argv[2:], sys.modules[__name__]))
    app = QApplication(sys.argv)
    app.setApplicationName("D1ffler")
    app.setApplicationVersion(APP_VERSION)
    try:
        settings, settings_path = load_app_settings()
        apply_theme(app, settings["theme"])
        app.setWindowIcon(QIcon(str(logo_asset_path())))
        window = ComparisonWindow(settings, settings_path, DiffApp)
    except Exception as exc:
        from d1ffler.app_settings import user_state_directory
        failure = DiagnosticStore(user_state_directory() / "diagnostics").record_exception("GUI startup", exc)
        QMessageBox.critical(None, "D1ffler could not start", failure.summary)
        sys.exit(2)
    exception_hooks = ExceptionHooks(app, window.diagnostics, window)
    app.aboutToQuit.connect(exception_hooks.restore)
    window.show()
    def open_initial_sources() -> None:
        if settings.startup_warnings:
            QMessageBox.warning(window, "User state", "\n\n".join(settings.startup_warnings))
        restored = window.offer_recovery()
        for index, sources in enumerate(command_line_source_sets(sys.argv[1:])):
            window.open_paths(sources, new_tab=restored or index > 0)
    QTimer.singleShot(0, open_initial_sources)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
