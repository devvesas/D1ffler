"""Parse, preview, and safely apply text-only unified patches."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from d1ffler.file_content import encode_text, inspect_file_content
from d1ffler.folder_sync import FileState, SyncError, file_state, safe_path


class PatchError(ValueError):
    pass


class PatchApplyError(PatchError):
    def __init__(self, cause, outcomes, partial_operation=None):
        self.outcomes = tuple(outcomes)
        self.partial_operation = partial_operation
        message = f"Stopped after {len(outcomes)} completed operation(s): {cause}"
        if outcomes:
            message += "\nCompleted:\n" + "\n".join(outcomes)
        if partial_operation:
            message += "\nPartial operation: " + partial_operation
        super().__init__(message)


@dataclass(frozen=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[tuple[str, str], ...]
    header: str


@dataclass(frozen=True)
class PatchFile:
    old_path: str | None
    new_path: str | None
    hunks: tuple[PatchHunk, ...]


@dataclass(frozen=True)
class HunkResult:
    status: str
    location: int | None
    detail: str


@dataclass(frozen=True)
class FolderPatchOperation:
    source_relative: str | None
    target_relative: str | None
    effect: str
    content: bytes | None
    source_state: FileState | None
    target_state: FileState | None
    selected_hunks: tuple[int, ...]
    directory_states: tuple[tuple[Path, int, int], ...] = ()


@dataclass
class PatchPlan:
    files: tuple[PatchFile, ...]
    selected: dict[int, tuple[int, ...]]
    hunk_results: dict[int, list[HunkResult]] = field(default_factory=dict)
    operations: list[FolderPatchOperation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _path(value: str, prefix: str) -> str | None:
    value = value.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith('"') or value.endswith('"'):
        raise PatchError("Quoted patch paths are not supported")
    if value.startswith(prefix):
        value = value[len(prefix):]
    parts = value.split("/")
    if (not value or value.startswith("/") or "\\" in value or "\x00" in value or
            any(part in {"", ".", ".."} for part in parts) or
            re.match(r"^[A-Za-z]:", value)):
        raise PatchError(f"Unsafe patch path: {value!r}")
    return value


def parse_patch(text: str) -> tuple[PatchFile, ...]:
    """Accept ordinary and Git unified text patches; reject unsupported operations."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    files: list[PatchFile] = []
    old_path = new_path = None
    rename_from = rename_to = None
    hunks: list[PatchHunk] = []
    in_file = False
    saw_headers = False

    def finish() -> None:
        nonlocal old_path, new_path, rename_from, rename_to, hunks, in_file, saw_headers
        if not in_file:
            return
        old = old_path if saw_headers else rename_from
        new = new_path if saw_headers else rename_to
        if old is None and new is None:
            raise PatchError("Patch has a file without usable paths")
        if rename_from is not None and old != rename_from:
            raise PatchError("Rename source does not match patch header")
        if rename_to is not None and new != rename_to:
            raise PatchError("Rename destination does not match patch header")
        if not hunks and not (old and new and old != new):
            raise PatchError(f"No text hunks for {new or old}")
        files.append(PatchFile(old, new, tuple(hunks)))
        old_path = new_path = rename_from = rename_to = None
        hunks = []
        in_file = saw_headers = False

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            raise PatchError("Binary patches are not supported")
        if line.startswith(("copy from ", "copy to ", "old mode ", "new mode ")):
            raise PatchError("Copy and permission changes in patches are not supported")
        if line.startswith("diff --git "):
            finish()
            in_file = True
            index += 1
            continue
        if line.startswith("rename from "):
            in_file = True
            rename_from = _path(line[len("rename from "):], "")
            index += 1
            continue
        if line.startswith("rename to "):
            in_file = True
            rename_to = _path(line[len("rename to "):], "")
            index += 1
            continue
        if line.startswith("--- "):
            if saw_headers:
                finish()
            in_file = saw_headers = True
            old_path = _path(line[4:], "a/")
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise PatchError("Missing +++ file header")
            new_path = _path(lines[index + 1][4:], "b/")
            index += 2
            continue
        if line.startswith("@@ "):
            if not saw_headers:
                raise PatchError("Hunk has no --- and +++ file headers")
            match = _HEADER.match(line)
            if match is None:
                raise PatchError(f"Invalid hunk header: {line}")
            old_start, old_count, new_start, new_count = (
                int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1))
            body: list[tuple[str, str]] = []
            index += 1
            while index < len(lines):
                item = lines[index]
                if item.startswith("\\ No newline at end of file"):
                    if not body:
                        raise PatchError("No-newline marker has no preceding line")
                    kind, value = body[-1]
                    body[-1] = (kind, value.rstrip("\n"))
                    index += 1
                    continue
                if (sum(kind != "+" for kind, _ in body) == old_count and
                        sum(kind != "-" for kind, _ in body) == new_count):
                    break
                if item and item[0] in " +-":
                    body.append((item[0], item[1:] + "\n"))
                    index += 1
                    continue
                break
            if sum(kind != "+" for kind, _ in body) != old_count or sum(
                    kind != "-" for kind, _ in body) != new_count:
                raise PatchError(f"Hunk line counts do not match: {line}")
            hunks.append(PatchHunk(old_start, old_count, new_start, new_count, tuple(body), line))
            continue
        index += 1
    finish()
    if not files:
        raise PatchError("No unified text changes found")
    keys = [item.new_path or item.old_path for item in files]
    if len(keys) != len(set(keys)):
        raise PatchError("Patch contains the same target path more than once")
    return tuple(files)


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _ending(lines: list[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        if line.endswith("\r"):
            return "\r"
    return "\n"


def apply_hunks(text: str, patch: PatchFile, selected: tuple[int, ...]) -> tuple[str, list[HunkResult]]:
    """Preview selected hunks using exact context; a displaced match must be unique."""
    lines = text.splitlines(keepends=True)
    results = [HunkResult("skipped", None, "Not selected") for _ in patch.hunks]
    delta = 0
    last_end = 0
    for hunk_index in selected:
        hunk = patch.hunks[hunk_index]
        before = [value for kind, value in hunk.lines if kind != "+"]
        after = [value for kind, value in hunk.lines if kind != "-"]
        expected = max(0, hunk.old_start - (0 if hunk.old_count == 0 else 1) + delta)

        def matches(position: int) -> bool:
            return (position >= last_end and position + len(before) <= len(lines) and
                    all(_body(actual) == _body(want) and
                        want.endswith("\n") == actual.endswith(("\n", "\r"))
                        for actual, want in zip(lines[position:position + len(before)], before)))

        if not before:
            candidates = [expected] if 0 <= expected <= len(lines) and expected >= last_end else []
        elif matches(expected):
            candidates = [expected]
        else:
            candidates = [position for position in range(last_end, len(lines) - len(before) + 1)
                          if matches(position)]
        if not candidates:
            results[hunk_index] = HunkResult("failed", None, "Context does not match target")
            continue
        if len(candidates) > 1:
            results[hunk_index] = HunkResult("ambiguous", None,
                                             f"Context matches {len(candidates)} locations")
            continue
        location = candidates[0]
        ending = _ending(lines)
        old_segment = iter(lines[location:location + len(before)])
        replacement = []
        for kind, value in hunk.lines:
            if kind == " ":
                replacement.append(next(old_segment))
            elif kind == "-":
                next(old_segment)
            else:
                replacement.append(_body(value) + ending if value.endswith("\n") else value)
        lines[location:location + len(before)] = replacement
        delta += len(after) - len(before) + (location - expected)
        last_end = location + len(after)
        status = "matching" if location == expected else "offset"
        results[hunk_index] = HunkResult(status, location + 1, f"Line {location + 1}" +
                                           (f" (expected {expected + 1})" if status == "offset" else ""))
    return "".join(lines), results


def _read_target(path: Path) -> tuple[str, str, bytes]:
    content = inspect_file_content(path, preview_limit=True)
    if content.text is None or content.encoding is None:
        raise PatchError(f"Patch target is binary or exceeds the 2 MiB editor limit: {path}")
    return content.text, content.encoding, content.bom


def _directory_states(root, paths):
    root = root.resolve(strict=True)
    directories = {root}
    for path in paths:
        if path is not None:
            parent = path.parent
            if not parent.is_relative_to(root):
                raise PatchError("Target folder changed while previewing")
            while parent != root:
                directories.add(parent)
                parent = parent.parent
    result = []
    for directory in sorted(directories, key=lambda p: (len(p.parts), str(p))):
        try:
            info = directory.stat()
        except FileNotFoundError:
            continue
        if not directory.is_dir() or not info.st_ino:
            raise PatchError(f"Directory identity is unavailable: {directory}")
        result.append((directory, info.st_dev, info.st_ino))
    return tuple(result)


def _check_directories(root, directory_states):
    if directory_states:
        if root.resolve(strict=True) != directory_states[0][0]:
            raise PatchError("Folder changed since preview")
        for directory, device, inode in directory_states:
            info = directory.stat()
            if (info.st_dev, info.st_ino) != (device, inode):
                raise PatchError(f"Directory changed since preview: {directory}")


def plan_folder_patch(root: Path, files: tuple[PatchFile, ...],
                      selected: dict[int, tuple[int, ...]]) -> PatchPlan:
    plan = PatchPlan(files, selected)
    seen_targets: set[str] = set()
    touched_paths: set[str] = set()
    for file_index, patch in enumerate(files):
        hunks = selected.get(file_index)
        if hunks is None:
            continue
        if patch.hunks and not hunks and not (
                patch.old_path and patch.new_path and patch.old_path != patch.new_path):
            continue
        label = patch.new_path or patch.old_path or "Unknown"
        try:
            source = safe_path(root, patch.old_path) if patch.old_path else None
            target = safe_path(root, patch.new_path) if patch.new_path else None
            if source is not None and not source.is_file():
                raise PatchError(f"Source file is missing: {patch.old_path}")
            if source is None and target is None:
                raise PatchError("Patch has no source or target")
            if target is not None and patch.new_path in seen_targets:
                raise PatchError(f"Multiple changes target {patch.new_path}")
            if target is not None:
                seen_targets.add(patch.new_path)
            touched = {path for path in (patch.old_path, patch.new_path) if path is not None}
            if touched & touched_paths:
                raise PatchError("Multiple selected patch files touch the same path")
            touched_paths.update(touched)
            if source is None and target is not None and target.exists():
                raise PatchError(f"Create target already exists: {patch.new_path}")
            if source is not None and target is not None and source != target and target.exists():
                raise PatchError(f"Rename destination already exists: {patch.new_path}")
            before = _read_target(source) if source is not None else ("", "utf-8", b"")
            result, statuses = apply_hunks(before[0], patch, hunks)
            plan.hunk_results[file_index] = statuses
            failed = [item for item in statuses if item.status in {"failed", "ambiguous"}]
            if failed:
                raise PatchError("; ".join(item.detail for item in failed))
            if target is None:
                if len(hunks) != len(patch.hunks) or result:
                    raise PatchError("Delete requires all hunks and an empty resulting file")
                effect = "delete"
                encoded = None
            else:
                effect = ("create" if source is None else "rename" if source != target else "overwrite")
                encoded = encode_text(result, before[1], before[2])
            plan.operations.append(FolderPatchOperation(
                patch.old_path, patch.new_path, effect, encoded,
                file_state(source) if source is not None else None,
                file_state(target) if target is not None else None, hunks,
                _directory_states(root, (source, target))))
        except (OSError, SyncError, PatchError, UnicodeError) as exc:
            plan.errors.append(f"{label}: {exc}")
    return plan


def _atomic_bytes(path: Path, content: bytes, mode_source: Path | None = None,
                  *, before_replace, copy_metadata=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    before_replace()
    parent_info = path.parent.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=".D1ffler-patch-", dir=path.parent)
    temporary = Path(temporary)
    temporary_info = os.fstat(descriptor)
    failure = None

    def check_temporary():
        current_parent = path.parent.stat()
        current_temporary = temporary.lstat()
        if ((current_parent.st_dev, current_parent.st_ino) != (parent_info.st_dev, parent_info.st_ino) or
                (current_temporary.st_dev, current_temporary.st_ino) != (temporary_info.st_dev, temporary_info.st_ino)):
            raise PatchError(f"Temporary path identity changed: {temporary}")

    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        before_replace()
        check_temporary()
        if mode_source is not None:
            (shutil.copystat if copy_metadata else shutil.copymode)(mode_source, temporary)
        check_temporary()
        if file_state(temporary) != FileState(len(content), hashlib.sha256(content).hexdigest()):
            raise PatchError(f"Temporary patch content changed: {temporary}")
        # Writing, flushing and copying metadata can run long enough for another
        # process to change either the reviewed files or their path ancestry.
        before_replace()
        os.replace(temporary, path)
        temporary = None
    except Exception as exc:
        failure = exc
        raise
    finally:
        if temporary is not None:
            try:
                current_parent = path.parent.stat()
                if (current_parent.st_dev, current_parent.st_ino) != (parent_info.st_dev, parent_info.st_ino):
                    raise PatchError(f"Temporary directory changed; temporary file retained: {temporary}")
                current = temporary.lstat()
                if (current.st_dev, current.st_ino) != (temporary_info.st_dev, temporary_info.st_ino):
                    raise PatchError(f"Temporary file identity changed; file retained: {temporary}")
                temporary.unlink()
            except FileNotFoundError:
                pass
            except (OSError, PatchError) as exc:
                raise PatchError(f"{failure}; temporary cleanup failed: {exc}" if failure else str(exc)) from exc


def _checked_paths(root, operation, *, expected_target=None,
                   check_states=True):
    old = safe_path(root, operation.source_relative) if operation.source_relative else None
    new = safe_path(root, operation.target_relative) if operation.target_relative else None
    _check_directories(root, operation.directory_states)
    if check_states:
        target_state = operation.target_state if expected_target is None else expected_target
        if old is not None and file_state(old) != operation.source_state:
            raise PatchError(f"Source changed during application: {operation.source_relative}")
        if new is not None and file_state(new) != target_state:
            raise PatchError(f"Target changed during application: {operation.target_relative}")
    return old, new


def apply_folder_plan(root: Path, plan: PatchPlan, backup_root: Path | None = None) -> list[str]:
    """Recheck reviewed states and make each operation atomic where possible."""
    if plan.errors or not plan.operations:
        raise PatchError("The patch plan has errors or no selected changes")
    if backup_root is not None:
        backup_root = backup_root.resolve()
        if backup_root == root.resolve() or root.resolve() in backup_root.parents:
            raise PatchError("Backup directory must be outside the target folder")
        if backup_root.exists() and not backup_root.is_dir():
            raise PatchError("Backup location is not a directory")
    used: set[Path] = set()
    for operation in plan.operations:
        old, new = _checked_paths(root, operation)
        touched = {path for path in (old, new) if path is not None}
        if touched & used:
            raise PatchError("Multiple patch operations touch the same path")
        used.update(touched)
        if backup_root is not None and old is not None:
            backup = safe_path(backup_root, operation.source_relative)
            if backup.exists():
                raise PatchError(f"Backup already exists: {backup}")
    outcomes = []
    for operation in plan.operations:
        partial_operation = None
        try:
            old, new = _checked_paths(root, operation)
            backup = None
            if backup_root is not None and old is not None:
                backup = safe_path(backup_root, operation.source_relative)
                original = old.read_bytes()
                if FileState(len(original), hashlib.sha256(original).hexdigest()) != operation.source_state:
                    raise PatchError(f"Source changed while reading backup: {operation.source_relative}")

                def check_backup():
                    _checked_paths(root, operation, check_states=False)
                    if safe_path(backup_root, operation.source_relative) != backup or backup.exists():
                        raise PatchError(f"Backup location changed or already exists: {backup}")

                _atomic_bytes(backup, original, old, before_replace=check_backup, copy_metadata=True)
                if file_state(backup) != operation.source_state:
                    raise PatchError(f"Backup does not match reviewed source: {backup}")
                backup_directories = _directory_states(backup_root, (backup,))

            def check(expected_target=None):
                if backup is not None:
                    if safe_path(backup_root, operation.source_relative) != backup:
                        raise PatchError(f"Backup location changed: {backup}")
                    _check_directories(backup_root, backup_directories)
                    if file_state(backup) != operation.source_state:
                        raise PatchError(f"Backup does not match reviewed source: {backup}")
                _checked_paths(root, operation, expected_target=expected_target)

            if operation.effect == "delete":
                check()
                old.unlink()
            elif operation.effect == "rename":
                _atomic_bytes(new, operation.content, old, before_replace=check)
                partial_operation = (f"rename: {operation.target_relative} was written; "
                                     f"source removal did not complete for {operation.source_relative}")
                check(expected_target=FileState(
                    len(operation.content), hashlib.sha256(operation.content).hexdigest()))
                old.unlink()
            else:
                _atomic_bytes(new, operation.content, old, before_replace=check)
            outcomes.append(f"{operation.effect}: {operation.source_relative or 'new file'} → "
                            f"{operation.target_relative or 'deleted'}")
        except (OSError, SyncError, PatchError) as exc:
            raise PatchApplyError(exc, outcomes, partial_operation) from exc
    return outcomes
