"""Shared folder comparison result types and status categories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from d1ffler.folder_archives import ArchiveIssue, ArchiveMember

FolderEntry = Path | ArchiveMember | ArchiveIssue


@dataclass(frozen=True)
class FileDifference:
    relative_path: str
    status: str  # added, removed, modified, metadata, identical, error


@dataclass(frozen=True)
class MoveMatch:
    from_panel: int
    from_path: str
    to_panel: int
    to_path: str
    sha256: str


FOLDER_STATUS_FILTERS = ("all", "added", "removed", "modified", "identical", "error", "pending", "moved", "linked", "ignored", "metadata")
