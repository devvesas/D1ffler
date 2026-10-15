"""Cancellable background jobs for folder, binary, sync, and text comparison."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from d1ffler.folder_archives import ArchiveIssue, ArchiveMember
from d1ffler.file_metadata import metadata_differences, metadata_snapshot
from d1ffler.folder_sync import SyncCancelled, SyncError, build_items
from d1ffler.git_worktrees import GitError
from d1ffler.text_rules import RuleError, applicable_rules, normalize_line
from d1ffler.text_comparison import build_text_layout


class ComparisonCancelled(Exception):
    pass


@dataclass(frozen=True)
class TextDiffResult:
    layout: tuple
    rule_error: str | None = None
    comparisons: list | None = None



class FolderComparisonWorker:
    def __init__(self, roots, exclusions, ignore_whitespace, ignore_endings,
                 ignore_gitignore, cache, cancel_event, messages, follow_internal_links=False,
                 text_rules=None, compare_metadata=False, *, inventory, files_match, detect_moves, hash_file,
                 max_move_hashes):
        self.roots = roots
        self.exclusions = exclusions
        self.ignore_whitespace = ignore_whitespace
        self.ignore_endings = ignore_endings
        self.ignore_gitignore = ignore_gitignore
        self.cache = cache
        self.cancel_event = cancel_event
        self.messages = messages
        self.follow_internal_links = follow_internal_links
        self.text_rules = text_rules or []
        self.compare_metadata = compare_metadata
        self.pending = []
        self.permits = threading.Semaphore(4)
        self.excluded_count = 0
        self.move_detection_limited = False
        self.folder_inventory = inventory
        self.files_match = files_match
        self.detect_exact_moves = detect_moves
        self.file_sha256 = hash_file
        self.max_move_hashes = max_move_hashes

    def _send(self, rows):
        while not self.cancel_event.is_set():
            if self.permits.acquire(timeout=0.05):
                self.messages.put(("batch", rows))
                return

    def _match_files(self, left, right, rules):
        args = (left, right, self.ignore_whitespace, self.ignore_endings, self.cancel_event.is_set)
        return self.files_match(*args, rules) if rules else self.files_match(*args)

    def run(self):
        try:
            maps = []
            link_maps = []
            excluded = 0
            for root in self.roots:
                def discovered(relative):
                    self.pending.append((relative, None))
                    if len(self.pending) >= 64:
                        self._send(self.pending)
                        self.pending = []
                        self.messages.put(("progress", 0, 0, relative))
                links = {}
                files, skipped = self.folder_inventory(root, self.exclusions, self.ignore_gitignore,
                                                  self.cancel_event.is_set, discovered, links,
                                                  self.follow_internal_links)
                maps.append(files)
                link_maps.append(links)
                excluded += len(skipped)
                self.excluded_count = excluded
                if self.cancel_event.is_set():
                    break
            if self.pending:
                self._send(self.pending)
                self.pending = []
            if not self.cancel_event.is_set():
                self.messages.put(("inventory", maps))
                self.messages.put(("links", link_maps))
                names = sorted(set().union(*(set(files) for files in maps)), key=str.casefold)
                total = len(names)
                rows = []
                metadata_rows = []
                for index, relative in enumerate(names):
                    if self.cancel_event.is_set():
                        break
                    for files in maps:
                        member = files.get(relative)
                        if not isinstance(member, ArchiveMember):
                            continue
                        try:
                            before = member.stat()
                            self.file_sha256(member, self.cancel_event.is_set)
                            if member.stat() != before:
                                raise OSError("ZIP archive changed while reading")
                        except ComparisonCancelled:
                            raise
                        except OSError as exc:
                            files[relative] = ArchiveIssue(member.archive, relative, str(exc))
                    baseline = maps[0].get(relative)
                    snapshots = [metadata_snapshot(files.get(relative), linked=relative in links)
                                 for files, links in zip(maps, link_maps)]
                    matched_rules = applicable_rules(self.text_rules, relative)
                    baseline_link = link_maps[0].get(relative)
                    statuses = ["error" if isinstance(baseline, ArchiveIssue) or
                                (baseline_link and baseline_link.state in {"broken", "inaccessible"})
                                else "identical" if baseline else "removed"]
                    content_statuses = list(statuses)
                    changed_fields = [[] for _ in maps]
                    for panel_index, files in enumerate(maps[1:], 1):
                        path = files.get(relative)
                        link = link_maps[panel_index].get(relative)
                        if baseline is None:
                            status = ("error" if isinstance(path, ArchiveIssue) or
                                      (link and link.state in {"broken", "inaccessible"})
                                      else "added" if path else "removed")
                        elif path is None:
                            status = "removed"
                        elif isinstance(baseline, ArchiveIssue) or isinstance(path, ArchiveIssue):
                            status = "error"
                        elif baseline_link or link:
                            if ((baseline_link and baseline_link.state in {"broken", "inaccessible"}) or
                                    (link and link.state in {"broken", "inaccessible"})):
                                status = "error"
                            elif baseline_link is None or link is None:
                                status = "modified"
                            elif self.follow_internal_links and baseline_link.state == link.state == "internal" and not (baseline_link.directory or link.directory):
                                try:
                                    status = self._match_files(baseline, path, matched_rules)
                                except (OSError, RuleError) as exc:
                                    status = "error"
                                    if isinstance(exc, RuleError):
                                        self.messages.put(("rule_error", relative, str(exc)))
                            else:
                                status = ("identical" if (baseline_link.kind, baseline_link.target,
                                                          baseline_link.state, baseline_link.directory) ==
                                          (link.kind, link.target, link.state, link.directory)
                                          else "modified")
                        else:
                            try:
                                left_stat, right_stat = baseline.stat(), path.stat()
                                signature = lambda p, s: (str(p), s.st_dev, s.st_ino, s.st_size,
                                                          s.st_mtime_ns, s.st_ctime_ns)
                                key = (signature(baseline, left_stat), signature(path, right_stat),
                                       self.ignore_whitespace, self.ignore_endings)
                                key += (tuple((rule["name"], rule["scope"], rule["pattern"], rule["mode"])
                                              for rule in matched_rules),)
                                status = self.cache.get(key)
                                if status is None:
                                    status = self._match_files(baseline, path, matched_rules)
                                    if (signature(baseline, baseline.stat()),
                                            signature(path, path.stat())) != key[:2]:
                                        status = "error"
                                    if status in {"identical", "ignored", "modified"}:
                                        self.cache[key] = status
                            except (OSError, RuleError) as exc:
                                status = "error"
                                if isinstance(exc, RuleError):
                                    self.messages.put(("rule_error", relative, str(exc)))
                        content_statuses.append(status)
                        if baseline is not None and path is not None and not (baseline_link or link) and (
                                not isinstance(baseline, ArchiveIssue) and not isinstance(path, ArchiveIssue)):
                            changed_fields[panel_index] = metadata_differences(snapshots[0], snapshots[panel_index])
                            if self.compare_metadata and changed_fields[panel_index] and status in {"identical", "ignored"}:
                                status = "metadata"
                        statuses.append(status)
                    if self.cancel_event.is_set() or "cancelled" in statuses:
                        break
                    metadata_rows.append((relative, {"panels": snapshots,
                                                     "content_statuses": content_statuses,
                                                     "changed_fields": changed_fields}))
                    rows.append((relative, statuses))
                    if len(rows) >= 64:
                        self.messages.put(("metadata", metadata_rows))
                        self._send(rows)
                        rows = []
                        metadata_rows = []
                    if index % 64 == 0:
                        self.messages.put(("progress", index + 1, total, relative))
                if rows:
                    self.messages.put(("metadata", metadata_rows))
                    self._send(rows)
                self.messages.put(("progress", min(index + 1, total) if total else 0, total, ""))
                if not self.cancel_event.is_set() and len(maps) >= 2:
                    self.messages.put(("move_progress", 0, self.max_move_hashes))
                    matches, self.move_detection_limited = self.detect_exact_moves(
                        maps, self.cancel_event.is_set,
                        lambda done, limit: self.messages.put(("move_progress", done, limit)))
                    if not self.cancel_event.is_set():
                        for start in range(0, len(matches), 64):
                            self._send_moves(matches[start:start + 64])
            self.messages.put(("finished", self.cancel_event.is_set(), ""))
        except ComparisonCancelled:
            self.messages.put(("finished", True, ""))
        except (OSError, GitError) as exc:
            self.messages.put(("finished", False, str(exc)))

    def _send_moves(self, matches: list) -> None:
        while not self.cancel_event.is_set():
            if self.permits.acquire(timeout=0.05):
                self.messages.put(("moves", matches))
                return



class BinarySummaryWorker:
    def __init__(self, paths, cancel_event, messages, *, summary):
        self.paths = paths
        self.cancel_event = cancel_event
        self.messages = messages
        self.summary = summary

    def run(self):
        baseline = self.paths[0]
        results = []
        try:
            for path in self.paths:
                if self.cancel_event.is_set():
                    break
                if baseline is not None and path is not None:
                    results.append(self.summary(baseline, path, self.cancel_event.is_set))
                elif path is not None:
                    results.append(f"Binary file: {path.name}\n{path.stat().st_size:,} bytes")
                else:
                    results.append("A binary input cannot be compared with pasted text.")
        except ComparisonCancelled:
            pass
        except OSError as exc:
            self.messages.put(("error", f"Could not inspect binary files: {exc}"))
            return
        self.messages.put(("finished", results if len(results) == len(self.paths) else None))



class SyncPlanWorker:
    def __init__(self, left, right, exclusions, ignore_whitespace,
                 ignore_endings, ignore_gitignore, cancel_event, messages, *, inventory, files_match):
        self.left, self.right = left, right
        self.exclusions = exclusions
        self.ignore_whitespace = ignore_whitespace
        self.ignore_endings = ignore_endings
        self.ignore_gitignore = ignore_gitignore
        self.cancel_event = cancel_event
        self.messages = messages
        self.folder_inventory = inventory
        self.files_match = files_match

    def run(self):
        try:
            left_files, left_excluded = self.folder_inventory(
                self.left, self.exclusions, self.ignore_gitignore, self.cancel_event.is_set)
            if self.cancel_event.is_set():
                raise SyncCancelled()
            right_files, right_excluded = self.folder_inventory(
                self.right, self.exclusions, self.ignore_gitignore, self.cancel_event.is_set)
            if self.cancel_event.is_set():
                raise SyncCancelled()
            rows = build_items(
                self.left, self.right, left_files, right_files, left_excluded, right_excluded,
                equivalent=lambda a, b: self.files_match(
                    a, b, self.ignore_whitespace, self.ignore_endings,
                    self.cancel_event.is_set) in {"identical", "ignored"},
                cancelled=self.cancel_event.is_set,
            )
            self.messages.put(("finished", rows, ""))
        except SyncCancelled:
            self.messages.put(("finished", None, "Cancelled"))
        except (OSError, GitError, SyncError) as exc:
            self.messages.put(("finished", None, str(exc)))



class TextDiffWorker:
    def __init__(self, lines, cancel_event, messages, compare_lines=None, rules_by_panel=None, *, build_rows,
                 sources=None, options=None, text_rules=None):
        self.lines = lines
        self.compare_lines = compare_lines
        self.rules_by_panel = rules_by_panel
        self.cancel_event = cancel_event
        self.messages = messages
        self.build_rows = build_rows
        self.sources, self.options, self.text_rules = sources, options or {}, text_rules or []

    def run(self):
        if self.cancel_event.is_set():
            self.messages.put(("finished", None))
            return
        rule_error = None
        if self.sources is not None:
            try:
                layout, comparisons = build_text_layout(self.sources, self.text_rules, self.options, self.build_rows)
                result = TextDiffResult(layout, comparisons=comparisons)
            except RuleError as exc:
                result = TextDiffResult(self.build_rows(self.lines), str(exc))
            self.messages.put(("finished", None if self.cancel_event.is_set() else result))
            return
        try:
            compare_lines = ([[normalize_line(line, rules)[0] for line in lines] if rules else lines
                              for lines, rules in zip(self.lines, self.rules_by_panel)]
                             if self.rules_by_panel is not None else self.compare_lines)
        except RuleError as exc:
            compare_lines = self.lines
            rule_error = str(exc)
        if self.cancel_event.is_set():
            self.messages.put(("finished", None))
            return
        result = TextDiffResult(self.build_rows(self.lines, compare_lines), rule_error)
        self.messages.put(("finished", None if self.cancel_event.is_set() else result))

