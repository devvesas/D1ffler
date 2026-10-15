"""Link inspection shared by folder comparison and synchronization."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LinkInfo:
    kind: str
    target: str
    state: str
    directory: bool

    def label(self) -> str:
        return f"{self.kind} → {self.target} ({self.state})"

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "target": self.target,
                "state": self.state, "directory": self.directory}


def inspect_link(path: Path, root: Path) -> LinkInfo | None:
    """Inspect a link without reading its target's content."""
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    is_symlink = stat.S_ISLNK(entry.st_mode)
    reparse = bool(getattr(entry, "st_file_attributes", 0) &
                   getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if not (is_symlink or reparse):
        return None
    tag = getattr(entry, "st_reparse_tag", None)
    kind = ("symbolic link" if is_symlink else
            "junction" if tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1) else
            "reparse point")
    try:
        target = os.readlink(path)
    except OSError:
        target = "[target unavailable]"
    try:
        resolved = path.resolve(strict=True)
        state = "internal" if resolved.is_relative_to(root.resolve(strict=True)) else "external"
        directory = resolved.is_dir()
    except FileNotFoundError:
        state, directory = "broken", False
    except (OSError, RuntimeError):
        state, directory = "inaccessible", False
    return LinkInfo(kind, target, state, directory)
