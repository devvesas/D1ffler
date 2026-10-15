"""Bounded, read-only ZIP entries for folder comparisons."""

from __future__ import annotations

import os
import stat
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Callable


MAX_ENTRY_SIZE = 64 * 1024 * 1024
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_ENTRIES = 50_000
MAX_EXPANSION_RATIO = 200
SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                         zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
WINDOWS_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)),
                        *(f"LPT{number}" for number in range(1, 10))}


def is_zip_source(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() == ".zip"


class _BoundedReader:
    def __init__(self, stream, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, amount: int = -1) -> bytes:
        remaining = self.limit - self.total
        requested = remaining + 1 if amount < 0 else min(amount, remaining + 1)
        try:
            data = self.stream.read(requested)
        except Exception as exc:
            raise OSError(f"Could not decompress ZIP entry: {exc}") from exc
        self.total += len(data)
        if self.total > self.limit:
            raise OSError("ZIP entry exceeds the decompression limit")
        return data


@dataclass(frozen=True)
class ArchiveMember:
    archive: Path
    relative: str
    raw_name: str
    size: int
    compressed_size: int
    crc: int
    header_offset: int
    archive_identity: tuple[int, int, int, int, int]

    @property
    def name(self) -> str:
        return PurePosixPath(self.relative).name

    def __str__(self) -> str:
        return f"{self.archive}!/{self.relative}"

    def _check_archive(self) -> os.stat_result:
        try:
            entry = self.archive.stat()
        except OSError as exc:
            raise OSError(f"ZIP archive unavailable: {self.archive}: {exc}") from exc
        identity = (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns)
        if identity != self.archive_identity:
            raise OSError(f"ZIP archive changed since scan: {self.archive}")
        return entry

    def stat(self):
        entry = self._check_archive()
        return SimpleNamespace(st_dev=entry.st_dev, st_ino=(entry.st_ino ^ self.header_offset),
                               st_size=self.size, st_mtime_ns=entry.st_mtime_ns,
                               st_ctime_ns=entry.st_ctime_ns)

    @contextmanager
    def open(self, mode: str = "rb"):
        if mode != "rb":
            raise OSError("ZIP entries are read-only")
        self._check_archive()
        try:
            with zipfile.ZipFile(self.archive) as archive:
                info = archive.getinfo(self.raw_name)
                if (info.file_size, info.compress_size, info.CRC, info.header_offset) != (
                        self.size, self.compressed_size, self.crc, self.header_offset):
                    raise OSError("ZIP entry changed since scan")
                with archive.open(info, "r") as stream:
                    yield _BoundedReader(stream, min(MAX_ENTRY_SIZE, self.size))
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError,
                NotImplementedError, EOFError, KeyError) as exc:
            raise OSError(f"Could not read ZIP entry {self.relative}: {exc}") from exc

    def read_bytes(self) -> bytes:
        with self.open("rb") as stream:
            return stream.read()


@dataclass(frozen=True)
class ArchiveIssue:
    archive: Path
    relative: str
    detail: str

    @property
    def name(self) -> str:
        return PurePosixPath(self.relative).name

    def __str__(self) -> str:
        return f"{self.archive}!/{self.relative}"

    def stat(self):
        raise OSError(self.detail)

    def open(self, _mode: str = "rb"):
        raise OSError(self.detail)


def _normalized(raw: str) -> tuple[str | None, str]:
    name = unicodedata.normalize("NFC", raw.replace("\\", "/"))
    if name.startswith("/") or name.startswith("//") or "\0" in name:
        return None, "Absolute or invalid ZIP entry path"
    parts = [part for part in name.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None, "ZIP entry path escapes its archive or is empty"
    if parts[0] == "[ZIP errors]":
        return None, "ZIP entry uses the reserved error-path prefix"
    if (len(name) > 4096 or any(len(part) > 255 or ":" in part or
                                any(ord(character) < 32 for character in part) or
                                part.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES
                                for part in parts)):
        return None, "ZIP entry has an unsafe path component"
    relative = "/".join(parts)
    if any(part.endswith((" ", ".")) for part in parts):
        return None, "ZIP entry path has an unsafe Windows component"
    return relative, ""


def zip_inventory(
    source: Path, patterns: list[str], is_excluded: Callable[[str, list[str]], bool],
    cancelled: Callable[[], bool] | None = None,
    discovered: Callable[[str], None] | None = None,
) -> tuple[dict[str, ArchiveMember | ArchiveIssue], list[str]]:
    """List archive files without extraction; unsafe entries become error rows."""
    included: dict[str, ArchiveMember | ArchiveIssue] = {}
    excluded: list[str] = []
    issue_number = 0
    seen: dict[str, tuple[str, str]] = {}
    total_size = 0

    def add(relative: str, entry: ArchiveMember | ArchiveIssue) -> None:
        included[relative] = entry
        if discovered:
            discovered(relative)

    def issue(raw: str, reason: str) -> None:
        nonlocal issue_number
        issue_number += 1
        label = "".join(character if character.isprintable() else "?"
                        for character in raw.replace("/", "∕").replace("\\", "∖"))[:100] or "empty name"
        relative = f"[ZIP errors]/{issue_number:05d} {label}"
        add(relative, ArchiveIssue(source, relative, f"{reason}: {raw}"))

    try:
        archive_stat = source.stat()
        identity = (archive_stat.st_dev, archive_stat.st_ino, archive_stat.st_size,
                    archive_stat.st_mtime_ns, archive_stat.st_ctime_ns)
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            for index, info in enumerate(infos):
                if cancelled and cancelled():
                    break
                raw = info.filename
                relative, error = _normalized(raw)
                if index >= MAX_ENTRIES:
                    issue(raw, f"ZIP has more than {MAX_ENTRIES:,} entries")
                    break
                if error:
                    issue(raw, error)
                    continue
                key = relative.casefold()
                previous = seen.get(key)
                if previous is not None:
                    first_relative, first_raw = previous
                    detail = f"Duplicate normalized ZIP path: {first_raw!r} and {raw!r}"
                    add(first_relative, ArchiveIssue(source, first_relative, detail))
                    continue
                seen[key] = (relative, raw)
                if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                    add(relative, ArchiveIssue(source, relative, f"ZIP symbolic link is not followed: {raw}"))
                    continue
                if info.is_dir():
                    if info.flag_bits & 1:
                        add(relative, ArchiveIssue(source, relative,
                                                   f"Encrypted ZIP directory (passwords are not supported): {raw}"))
                    continue
                reason = ("Encrypted ZIP entry (passwords are not supported)" if info.flag_bits & 1 else
                          "Unsupported ZIP compression method" if info.compress_type not in SUPPORTED_COMPRESSION else
                          "ZIP entry exceeds 64 MiB decompression limit" if info.file_size > MAX_ENTRY_SIZE else
                          "ZIP entry exceeds 200:1 expansion limit" if info.file_size >
                          max(1, info.compress_size) * MAX_EXPANSION_RATIO else "")
                if reason:
                    add(relative, ArchiveIssue(source, relative, f"{reason}: {raw}"))
                elif is_excluded(relative, patterns):
                    excluded.append(relative)
                else:
                    total_size += info.file_size
                    if total_size > MAX_TOTAL_SIZE:
                        add(relative, ArchiveIssue(source, relative,
                                                   f"ZIP exceeds 512 MiB total decompression limit: {raw}"))
                    else:
                        add(relative, ArchiveMember(source, relative, raw, info.file_size,
                                                    info.compress_size, info.CRC, info.header_offset,
                                                    identity))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        issue(source.name, f"Cannot read ZIP archive ({exc})")
    return included, excluded
