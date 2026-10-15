"""Portable, comparison-only file metadata snapshots."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from d1ffler.folder_archives import ArchiveIssue, ArchiveMember


def _local_time(nanoseconds: int) -> str:
    seconds, fraction = divmod(nanoseconds, 1_000_000_000)
    local = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    offset = local.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return f"{local:%Y-%m-%d %H:%M:%S}.{fraction:09d} {zone}"


def metadata_snapshot(entry: Path | ArchiveMember | ArchiveIssue | None, *, linked: bool = False) -> dict:
    """Return display fields and comparable values; unsupported fields remain None."""
    result = {"size": None, "modified": None, "modified_ns": None,
              "permissions": None, "permission_key": None, "reason": ""}
    if entry is None:
        result["reason"] = "Missing in this source"
        return result
    if isinstance(entry, ArchiveIssue):
        result["reason"] = f"ZIP entry unavailable: {entry.detail}"
        return result
    if isinstance(entry, ArchiveMember):
        result["size"] = entry.size
        result["reason"] = "Modified time and permissions unavailable for ZIP entries"
        return result
    if linked:
        result["reason"] = "Linked path metadata unavailable; link target is shown separately"
        return result
    try:
        info = entry.stat()
    except OSError as exc:
        result["reason"] = f"Metadata unavailable: {exc.strerror or type(exc).__name__}"
        return result
    result["size"] = info.st_size
    try:
        result["modified"] = _local_time(info.st_mtime_ns)
        result["modified_ns"] = info.st_mtime_ns
    except (OverflowError, OSError, ValueError):
        result["reason"] = "Modification time unavailable on this platform"
    if os.name == "nt":
        writable = bool(info.st_mode & stat.S_IWUSR)
        result["permission_key"] = int(writable)
        result["permissions"] = "Writable" if writable else "Read-only"
    else:
        mode = stat.S_IMODE(info.st_mode)
        result["permission_key"] = mode
        result["permissions"] = f"0o{mode:03o}"
    return result


def metadata_differences(left: dict, right: dict) -> list[str]:
    fields = []
    if left["modified_ns"] is not None and right["modified_ns"] is not None and (
            left["modified_ns"] != right["modified_ns"]):
        fields.append("modified time")
    if left["permission_key"] is not None and right["permission_key"] is not None and (
            left["permission_key"] != right["permission_key"]):
        fields.append("permissions")
    return fields


def metadata_label(value: dict) -> str:
    if value["size"] is None:
        return value["reason"]
    pieces = [f"Size: {value['size']:,} bytes"]
    pieces.append(f"Modified: {value['modified'] or 'unavailable'}")
    pieces.append(f"Permissions: {value['permissions'] or 'unavailable'}")
    if value["reason"]:
        pieces.append(value["reason"])
    return " · ".join(pieces)
