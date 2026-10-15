"""Bounded, cancellable content search over a folder comparison snapshot."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from d1ffler.file_content import MAX_EDITOR_FILE_BYTES, inspect_file_content
from d1ffler.folder_archives import ArchiveIssue, ArchiveMember
from d1ffler.image_compare import is_image_source


MAX_SEARCH_FILE_BYTES = MAX_EDITOR_FILE_BYTES
MAX_SEARCH_TOTAL_BYTES = 128 * 1024 * 1024
MAX_SEARCH_HITS = 5_000


@dataclass(frozen=True)
class SearchTask:
    relative: str
    panel_index: int
    panel_name: str
    entry: Path | ArchiveMember | ArchiveIssue | None
    linked: bool = False


@dataclass(frozen=True)
class SearchHit:
    relative: str
    panel_index: int
    panel_name: str
    line: int
    column: int
    match_text: str
    excerpt: str
    fingerprint: str
    entry: Path | ArchiveMember


def _excerpt(line: str, start: int, length: int) -> str:
    begin = max(0, start - 48)
    end = min(len(line), start + max(length, 1) + 72)
    value = line[begin:end].replace("\t", "    ")
    return ("…" if begin else "") + value + ("…" if end < len(line) else "")


class FolderContentSearchWorker:
    def __init__(self, tasks: list[SearchTask], query: str, case_sensitive: bool,
                 cancelled: Event, messages, *, pending: int = 0, snapshot_partial: bool = False):
        self.tasks = tasks
        self.query = query
        self.case_sensitive = case_sensitive
        self.cancelled = cancelled
        self.messages = messages
        self.pending = pending
        self.snapshot_partial = snapshot_partial

    def run(self) -> None:
        counts = Counter(pending=self.pending)
        total_bytes = 0
        hits = 0
        scanned = 0
        partial = self.snapshot_partial
        expression = re.compile(re.escape(self.query), 0 if self.case_sensitive else re.IGNORECASE)
        batch: list[SearchHit] = []
        try:
            for task in self.tasks:
                if self.cancelled.is_set():
                    partial = True
                    break
                entry = task.entry
                scanned += 1
                if entry is None:
                    counts["missing"] += 1
                elif isinstance(entry, ArchiveIssue):
                    counts["unreadable"] += 1
                elif task.linked:
                    counts["linked"] += 1
                else:
                    try:
                        before = entry.stat()
                        size = before.st_size
                        if size > MAX_SEARCH_FILE_BYTES:
                            counts["oversized"] += 1
                        elif total_bytes + size > MAX_SEARCH_TOTAL_BYTES:
                            counts["budget"] += len(self.tasks) - scanned + 1
                            partial = True
                            break
                        elif is_image_source(entry):
                            counts["binary"] += 1
                        else:
                            total_bytes += size
                            content = inspect_file_content(entry, preview_limit=True)
                            after = entry.stat()
                            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                                counts["unreadable"] += 1
                            elif content.text is None:
                                counts["binary"] += 1
                            else:
                                counts["searched"] += 1
                                for line_number, line in enumerate(content.text.splitlines(), 1):
                                    if self.cancelled.is_set():
                                        partial = True
                                        break
                                    for match in expression.finditer(line):
                                        batch.append(SearchHit(
                                            task.relative, task.panel_index, task.panel_name,
                                            line_number, match.start() + 1, match.group(),
                                            _excerpt(line, match.start(), match.end() - match.start()),
                                            content.fingerprint or "", entry))
                                        hits += 1
                                        if len(batch) >= 32:
                                            self.messages.put(("hits", batch))
                                            batch = []
                                        if hits >= MAX_SEARCH_HITS:
                                            counts["hit_limit"] = len(self.tasks) - scanned
                                            partial = True
                                            break
                                    if hits >= MAX_SEARCH_HITS or self.cancelled.is_set():
                                        break
                    except (OSError, UnicodeError):
                        counts["unreadable"] += 1
                if scanned % 20 == 0:
                    self.messages.put(("progress", scanned, len(self.tasks), hits))
                if hits >= MAX_SEARCH_HITS or self.cancelled.is_set():
                    partial = True
                    break
            if batch:
                self.messages.put(("hits", batch))
            self.messages.put(("finished", dict(counts), scanned, total_bytes, hits, partial))
        except (OSError, UnicodeError) as exc:
            self.messages.put(("error", str(exc)))
