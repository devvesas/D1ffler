"""File decoding, fingerprints, and binary comparison helpers."""

from __future__ import annotations

import codecs
import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from d1ffler.comparison_workers import ComparisonCancelled
from d1ffler.folder_archives import ArchiveMember

@dataclass(frozen=True)
class FileContent:
    text: str | None
    encoding: str | None
    size: int
    too_large: bool = False
    fingerprint: str | None = None
    bom: bytes = b""


MAX_EDITOR_FILE_BYTES = 2 * 1024 * 1024
ENCODING_CHOICES = (
    ("UTF-8", "utf-8", b""),
    ("UTF-8 with BOM", "utf-8", codecs.BOM_UTF8),
    ("UTF-16 LE", "utf-16-le", b""),
    ("UTF-16 LE with BOM", "utf-16-le", codecs.BOM_UTF16_LE),
    ("UTF-16 BE", "utf-16-be", b""),
    ("UTF-16 BE with BOM", "utf-16-be", codecs.BOM_UTF16_BE),
    ("UTF-32 LE", "utf-32-le", b""),
    ("UTF-32 LE with BOM", "utf-32-le", codecs.BOM_UTF32_LE),
    ("UTF-32 BE", "utf-32-be", b""),
    ("UTF-32 BE with BOM", "utf-32-be", codecs.BOM_UTF32_BE),
    ("Windows-1252", "cp1252", b""),
)


def encoding_label(encoding: str, bom: bytes) -> str:
    return next((label for label, codec, marker in ENCODING_CHOICES
                 if (codec, marker) == (encoding, bom)), encoding)


def encode_text(text: str, encoding: str, bom: bytes) -> bytes:
    return bom + text.encode(encoding, errors="strict")


def inspect_file_content(path: Path | ArchiveMember, preview_limit: bool = False,
                         forced_encoding: tuple[str, bytes] | None = None) -> FileContent:
    with path.open("rb") as file:
        sample = file.read(8192)
    size = path.stat().st_size
    if preview_limit and size > MAX_EDITOR_FILE_BYTES:
        return FileContent(None, None, size, True, disk_fingerprint(path))
    known_bom = any(sample.startswith(marker) for marker in (
        codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, codecs.BOM_UTF8,
        codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
    if forced_encoding is None and not known_bom:
        control_ratio = (sum(byte < 9 or 14 <= byte < 32 for byte in sample) / len(sample)
                         if sample else 0)
        if control_ratio > .20 and b"\0" not in sample:
            return FileContent(None, None, size, fingerprint=disk_fingerprint(path))
        if b"\0" in sample:
            looks_unicode = False
            for candidate, unit in (("utf-32-le", 4), ("utf-32-be", 4),
                                    ("utf-16-le", 2), ("utf-16-be", 2)):
                prefix = sample[:len(sample) // unit * unit]
                try:
                    decoded = prefix.decode(candidate)
                except UnicodeDecodeError:
                    continue
                if decoded and sum(ch.isprintable() or ch in "\r\n\t" for ch in decoded) / len(decoded) > .9:
                    looks_unicode = True
                    break
            if not looks_unicode:
                return FileContent(None, None, size, fingerprint=disk_fingerprint(path))
    data = path.read_bytes()
    fingerprint = hashlib.sha256(data).hexdigest()
    if forced_encoding is not None:
        encoding, bom = forced_encoding
        payload = data[len(bom):] if bom and data.startswith(bom) else data
        return FileContent(payload.decode(encoding), encoding, size,
                           fingerprint=fingerprint, bom=bom if data.startswith(bom) else b"")
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            try:
                return FileContent(data[len(bom):].decode(encoding), encoding, size,
                                   fingerprint=fingerprint, bom=bom)
            except UnicodeDecodeError:
                return FileContent(None, None, size, fingerprint=fingerprint)
    if b"\0" in sample:
        for encoding in ("utf-32-le", "utf-32-be", "utf-16-le", "utf-16-be"):
            if len(data) % (4 if "32" in encoding else 2):
                continue
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            if text and sum(character.isprintable() or character in "\r\n\t" for character in text) / len(text) > .9:
                return FileContent(text, encoding, size, fingerprint=fingerprint)
        return FileContent(None, None, size, fingerprint=fingerprint)
    if sample and sum(byte < 9 or 14 <= byte < 32 for byte in sample) / len(sample) > 0.20:
        return FileContent(None, None, size, fingerprint=fingerprint)
    for encoding in ("utf-8", "cp1252"):
        try:
            return FileContent(data.decode(encoding), encoding, size, fingerprint=fingerprint)
        except UnicodeDecodeError:
            continue
    return FileContent(None, None, size, fingerprint=fingerprint)


def disk_fingerprint(path: Path) -> str | None:
    """Hash an editable file; absence is a distinct disk state."""
    try:
        stat = path.stat()
        if stat.st_size > MAX_EDITOR_FILE_BYTES:
            return f"stat:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_ino}"
        return file_sha256(path)
    except FileNotFoundError:
        return None


def file_sha256(path: Path | ArchiveMember, cancelled=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(65536):
            if cancelled and cancelled():
                raise ComparisonCancelled()
            digest.update(chunk)
    return digest.hexdigest()


def read_file_content(path: Path | ArchiveMember) -> str:
    content = inspect_file_content(path)
    if content.text is None:
        raise ValueError(f"Binary file cannot be read as text: {path}")
    return content.text


def unloaded_file_message(path: Path, content: FileContent) -> str:
    if content.too_large:
        return (f"Large file: {path.name}\n{content.size:,} bytes\n"
                "Editor preview is limited to 2 MiB. File metadata is compared in the background.")
    return f"Binary file: {path.name}\n{content.size:,} bytes\nAdd another file to compare hashes and byte offsets."


def binary_file_summary(left: Path, right: Path, cancelled=None) -> str:
    left_size, right_size = left.stat().st_size, right.stat().st_size
    left_hash = file_sha256(left, cancelled)
    right_hash = left_hash if left == right else file_sha256(right, cancelled)
    if left_hash == right_hash:
        difference = "Files are byte-identical."
    else:
        offset = 0
        with left.open("rb") as left_file, right.open("rb") as right_file:
            while True:
                if cancelled and cancelled():
                    raise ComparisonCancelled()
                left_chunk, right_chunk = left_file.read(65536), right_file.read(65536)
                for index, (left_byte, right_byte) in enumerate(zip(left_chunk, right_chunk)):
                    if left_byte != right_byte:
                        offset += index
                        break
                else:
                    offset += min(len(left_chunk), len(right_chunk))
                    if len(left_chunk) != len(right_chunk) or not left_chunk:
                        break
                    continue
                break
        difference = f"First differing byte: offset {offset:,}."
    return (
        f"Byte comparison\n\nLeft:  {left.name} ({left_size:,} bytes)\n"
        f"Right: {right.name} ({right_size:,} bytes)\n\n{difference}\n"
        f"Left SHA-256:  {left_hash}\nRight SHA-256: {right_hash}"
    )


def read_text(path: Path) -> list[str]:
    return read_file_content(path).splitlines(keepends=True)


def compare_files(left: Path, right: Path) -> list[str]:
    return list(difflib.unified_diff(
        read_text(left), read_text(right), fromfile=str(left), tofile=str(right), lineterm=""
    ))
