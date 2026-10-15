"""Reviewed, per-file folder synchronization with conflict checks and backups."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from d1ffler.file_metadata import metadata_snapshot, _local_time
from d1ffler.folder_links import inspect_link
from d1ffler.source_paths import absolute_source


class SyncError(Exception):
    pass


class SyncCancelled(SyncError):
    pass


class MetadataConflict(SyncError):
    pass


class MetadataUnavailable(SyncError):
    pass


@dataclass(frozen=True)
class FileState:
    size: int
    sha256: str


@dataclass(frozen=True)
class MetadataState:
    modified_ns: int | None
    permission_key: int | None
    reason: str = field(default="", compare=False)


METADATA_ACTIONS = {
    f"{prefix}_{direction}": fields
    for prefix, fields in (("mtime", ("modified_ns",)),
                           ("permissions", ("permission_key",)),
                           ("metadata", ("modified_ns", "permission_key")))
    for direction in ("left_to_right", "right_to_left")
}


def metadata_state(path: Path) -> MetadataState:
    value = metadata_snapshot(path)
    return MetadataState(value["modified_ns"], value["permission_key"], value["reason"])


def metadata_value(field_name: str, value: int | None) -> str:
    if value is None:
        return "unavailable"
    if field_name == "modified_ns":
        return f"{_local_time(value)} ({value} ns)"
    return ("Writable" if value else "Read-only") if os.name == "nt" else f"0o{value:03o}"


@dataclass(frozen=True)
class SyncItem:
    relative: str
    status: str
    binary: bool
    left_state: FileState | None
    right_state: FileState | None
    excluded: bool = False
    error: str = ""
    left_metadata: MetadataState | None = None
    right_metadata: MetadataState | None = None


@dataclass(frozen=True)
class SyncOperation:
    relative: str
    action: str  # content direction, deletion, or an explicit METADATA_ACTIONS choice
    effect: str  # create, overwrite, delete, metadata
    source_root: Path | None
    target_root: Path
    source_state: FileState | None
    target_state: FileState | None
    binary: bool
    metadata_fields: tuple[str, ...] = ()
    source_metadata: MetadataState | None = None
    target_metadata: MetadataState | None = None


@dataclass
class SyncReport:
    left: str
    right: str
    started_at: str
    finished_at: str = ""
    dry_run: bool = False
    cancelled: bool = False
    stopped_on_conflict: bool = False
    backup_directory: str | None = None
    left_resolved: str = ""
    right_resolved: str = ""
    backup_resolved: str = ""
    outcomes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise SyncError(f"Unsafe relative path: {relative}")
    if inspect_link(root, root.parent) is not None:
        raise SyncError(f"Folder sync does not support linked roots: {root}")
    resolved_root = root.resolve()
    path = resolved_root.joinpath(*parts)
    current = resolved_root
    for part in parts:
        current = current / part
        link = inspect_link(current, resolved_root)
        if link is not None:
            raise SyncError(f"Folder sync does not support linked paths: {current} ({link.label()})")
    if not path.resolve().is_relative_to(resolved_root):
        raise SyncError(f"Path escapes its folder: {path}")
    return path


def file_state(path: Path, cancelled: Callable[[], bool] | None = None,
               stream: BinaryIO | None = None) -> FileState | None:
    link = inspect_link(path, path.parent)
    if link is not None:
        raise SyncError(f"Folder sync does not support linked paths: {path} ({link.label()})")
    if not path.exists():
        return None
    if not path.is_file():
        raise SyncError(f"Expected a regular file: {path}")
    before = path.stat()
    digest = hashlib.sha256()
    with nullcontext(stream) if stream is not None else path.open("rb") as reader:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MetadataConflict(f"File replaced while being reviewed: {path}")
        reader.seek(0)
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            if cancelled and cancelled():
                raise SyncCancelled()
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_mode, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_mode, after.st_ino):
        raise SyncError(f"File changed while being reviewed: {path}")
    return FileState(after.st_size, digest.hexdigest())


def reviewed_state(path: Path, cancelled=None, stream=None) -> tuple[FileState | None, MetadataState | None]:
    before = metadata_state(path)
    content = file_state(path, cancelled, stream)
    after = metadata_state(path) if content is not None else None
    if after is not None and before != after:
        raise MetadataConflict(f"File metadata changed while being reviewed: {path}")
    return content, after


def _binary(path: Path) -> bool:
    with path.open("rb") as stream:
        return b"\0" in stream.read(8192)


def _excluded(relative: str, excluded: set[str]) -> bool:
    return any(relative == path or relative.startswith(path.rstrip("/") + "/") for path in excluded)


def build_items(
    left_root: Path, right_root: Path,
    left_files: dict[str, Path], right_files: dict[str, Path],
    left_excluded: list[str] | None = None, right_excluded: list[str] | None = None,
    equivalent: Callable[[Path, Path], bool] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[SyncItem]:
    excluded = set(left_excluded or []) | set(right_excluded or [])
    names = set(left_files) | set(right_files) | excluded
    case_names: dict[str, set[str]] = {}
    for relative in names:
        case_names.setdefault(relative.casefold(), set()).add(relative)
    result = []
    for relative in sorted(names, key=str.casefold):
        if cancelled and cancelled():
            raise SyncCancelled()
        if len(case_names[relative.casefold()]) > 1:
            result.append(SyncItem(
                relative, "error", False, None, None,
                error="Paths differ only by letter case; resolve the names before syncing.",
            ))
            continue
        if _excluded(relative, excluded):
            result.append(SyncItem(relative, "excluded", False, None, None, excluded=True))
            continue
        try:
            left = safe_path(left_root, relative)
            right = safe_path(right_root, relative)
            left_state, left_metadata = reviewed_state(left, cancelled)
            right_state, right_metadata = reviewed_state(right, cancelled)
            if (relative in left_files and left_state is None) or (relative in right_files and right_state is None):
                raise SyncError(f"File disappeared during review: {relative}")
            binary = (left_state is not None and _binary(left)) or (right_state is not None and _binary(right))
            if left_state is None:
                status = "added"
            elif right_state is None:
                status = "removed"
            elif left_state == right_state or (equivalent and equivalent(left, right)):
                status = "identical"
            else:
                status = "modified"
            result.append(SyncItem(relative, status, binary, left_state, right_state,
                                   left_metadata=left_metadata, right_metadata=right_metadata))
        except SyncCancelled:
            raise
        except (OSError, SyncError) as exc:
            result.append(SyncItem(relative, "error", False, None, None, error=str(exc)))
    return result


def operation_for(item: SyncItem, action: str, left_root: Path, right_root: Path) -> SyncOperation | None:
    left_root, right_root = absolute_source(left_root), absolute_source(right_root)
    if action == "skip" or item.excluded or item.error:
        return None
    if action in METADATA_ACTIONS:
        reason = metadata_unavailable_reason(item, action)
        if reason:
            raise MetadataUnavailable(reason)
        to_right = action.endswith("left_to_right")
        source_metadata = item.left_metadata if to_right else item.right_metadata
        target_metadata = item.right_metadata if to_right else item.left_metadata
        fields = tuple(name for name in METADATA_ACTIONS[action]
                       if getattr(source_metadata, name) != getattr(target_metadata, name))
        if not fields:
            return None
        if (os.name == "nt" and len(fields) == 2 and
                not target_metadata.permission_key and source_metadata.permission_key):
            # A read-only Windows file must become writable before setting its time.
            fields = ("permission_key", "modified_ns")
        return SyncOperation(item.relative, action, "metadata", left_root if to_right else right_root,
                             right_root if to_right else left_root,
                             item.left_state if to_right else item.right_state,
                             item.right_state if to_right else item.left_state, item.binary,
                             fields, source_metadata, target_metadata)
    if action == "left_to_right":
        source_root, target_root = left_root, right_root
        source_state, target_state = item.left_state, item.right_state
    elif action == "right_to_left":
        source_root, target_root = right_root, left_root
        source_state, target_state = item.right_state, item.left_state
    elif action == "delete_left":
        source_root, target_root = None, left_root
        source_state, target_state = None, item.left_state
    elif action == "delete_right":
        source_root, target_root = None, right_root
        source_state, target_state = None, item.right_state
    else:
        raise SyncError(f"Unknown sync action: {action}")
    if source_root is not None and source_state is None:
        raise SyncError(f"Source is missing: {item.relative}")
    if source_root is None and target_state is None:
        raise SyncError(f"Delete target is missing: {item.relative}")
    effect = "delete" if source_root is None else "overwrite" if target_state else "create"
    return SyncOperation(item.relative, action, effect, source_root, target_root,
                         source_state, target_state, item.binary)


def metadata_unavailable_reason(item: SyncItem, action: str) -> str:
    if item.excluded or item.error:
        return item.error or "Excluded path"
    if not item.left_state or not item.right_state:
        return "Metadata sync requires an existing file in both folders"
    for side, value in (("A", item.left_metadata), ("B", item.right_metadata)):
        for name in METADATA_ACTIONS[action]:
            if value is None or getattr(value, name) is None:
                return f"{side}: {name} unavailable. " + (value.reason if value else "No reviewed metadata")
    return ""


def metadata_preview(operation: SyncOperation) -> list[dict]:
    return [{"field": name, "before": getattr(operation.target_metadata, name),
             "after": getattr(operation.source_metadata, name)} for name in operation.metadata_fields]


def metadata_description(changes: list[dict]) -> str:
    return "; ".join(f"{'Modified time' if change['field'] == 'modified_ns' else 'Permissions'}: "
                     f"{metadata_value(change['field'], change['before'])} → "
                     f"{metadata_value(change['field'], change['after'])}" for change in changes)


def _write_metadata(target: Path, name: str, value: int) -> None:
    try:
        if name == "modified_ns":
            os.utime(target, ns=(target.stat().st_atime_ns, value))
        elif name == "permission_key":
            target.chmod((stat.S_IREAD | stat.S_IWRITE if value else stat.S_IREAD)
                         if os.name == "nt" else value)
        else:
            raise MetadataUnavailable(f"Unknown metadata field: {name}")
    except NotImplementedError as exc:
        raise MetadataUnavailable(f"{name} unsupported on this filesystem: {exc}") from exc
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}:
            raise MetadataUnavailable(f"{name} unsupported on this filesystem: {exc}") from exc
        raise
    if getattr(metadata_state(target), name) != value:
        raise MetadataUnavailable(f"Filesystem did not retain the exact requested {name} value")


def _execute_metadata(operation, target, source, outcome, *, conflict_policy,
                      backup_directory, dry_run, cancelled):
    current, current_metadata = reviewed_state(target, cancelled)
    source_content, source_metadata = reviewed_state(source, cancelled)
    if (source_content, source_metadata) != (operation.source_state, operation.source_metadata):
        raise MetadataConflict("Source bytes or metadata changed since review; skipped.")
    if current is None:
        raise MetadataConflict("Metadata target is now missing; skipped.")
    for name in operation.metadata_fields:
        if getattr(current_metadata, name) is None or getattr(source_metadata, name) is None:
            raise MetadataUnavailable(f"{name} unavailable: {current_metadata.reason or source_metadata.reason}")
    if (current, current_metadata) != (operation.target_state, operation.target_metadata) and conflict_policy != "overwrite":
        raise MetadataConflict("Target bytes or metadata changed since review; skipped.")
    fields = operation.metadata_fields
    if (os.name == "nt" and set(fields) == {"modified_ns", "permission_key"} and
            not current_metadata.permission_key and source_metadata.permission_key):
        fields = ("permission_key", "modified_ns")
    changes = [{"field": name, "reviewed_before": getattr(operation.target_metadata, name),
                "before": getattr(current_metadata, name), "after": getattr(source_metadata, name),
                "status": "planned"} for name in fields]
    outcome["metadata_changes"] = changes
    outcome["detail"] = metadata_description(changes)
    if dry_run:
        outcome.update(status="dry run", detail="Would change metadata: " + outcome["detail"])
        return
    if backup_directory is not None:
        backup = _backup(target, backup_directory, outcome["target_side"], operation.relative)
        saved, saved_metadata = reviewed_state(backup, cancelled)
        if saved != current or reviewed_state(target, cancelled) != (current, current_metadata):
            raise MetadataConflict("Target changed while backing up; metadata skipped.")
        if saved_metadata != current_metadata:
            raise MetadataUnavailable("Backup filesystem cannot preserve the reviewed metadata; skipped.")
        outcome.update(backup_path=str(backup), backup_size=str(saved.size), backup_sha256=saved.sha256,
                       backup_metadata=asdict(saved_metadata))
    for change in changes:
        if cancelled and cancelled():
            raise SyncCancelled()
        # Resolve paths again and fingerprint bytes and all supported metadata before each field.
        target = safe_path(operation.target_root, operation.relative)
        source = safe_path(operation.source_root, operation.relative)
        if reviewed_state(source, cancelled) != (operation.source_state, operation.source_metadata):
            raise MetadataConflict("Source changed before metadata write; remaining fields skipped.")
        # Keep the read handle open so permission changes can remove read access
        # without preventing verification of the unchanged bytes.
        with target.open("rb") as stream:
            if reviewed_state(target, cancelled, stream) != (current, current_metadata):
                raise MetadataConflict("Target changed before metadata write; remaining fields skipped.")
            try:
                _write_metadata(target, change["field"], change["after"])
            except (OSError, SyncError) as exc:
                change["status"] = "unavailable" if isinstance(exc, MetadataUnavailable) else "error"
                change["actual_after"] = getattr(metadata_state(target), change["field"])
                raise
            change["status"] = "completed"
            current_metadata = MetadataState(
                change["after"] if change["field"] == "modified_ns" else current_metadata.modified_ns,
                change["after"] if change["field"] == "permission_key" else current_metadata.permission_key)
            # Do not adopt external changes as the expected state for the next field.
            if reviewed_state(target, cancelled, stream) != (current, current_metadata):
                raise MetadataConflict("Target changed during metadata write; remaining fields skipped.")
    outcome.update(status="completed", after_size=str(current.size), after_sha256=current.sha256,
                   after_metadata=asdict(current_metadata))


def _copy_atomic(source: Path, target: Path, expected_source: FileState | None,
                 expected_target: FileState | None) -> None:
    if expected_source is None:
        raise SyncError(f"Source was not reviewed: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".D1ffler-sync-", dir=target.parent)
    os.close(handle)
    try:
        shutil.copy2(source, temporary)
        if file_state(Path(temporary)) != expected_source:
            raise SyncError(f"Source changed while copying: {source}")
        if file_state(target) != expected_target:
            raise SyncError(f"Target changed while copying: {target}")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _backup(target: Path, backup_directory: Path, side: str, relative: str) -> Path:
    backup = safe_path(backup_directory / side, relative)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        raise SyncError(f"Backup already exists: {backup}")
    shutil.copy2(target, backup)
    return backup


def execute_plan(
    operations: list[SyncOperation], left_root: Path, right_root: Path,
    *, conflict_policy: str = "skip", backup_directory: Path | None = None,
    dry_run: bool = False, cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int, dict], None] | None = None,
) -> SyncReport:
    if conflict_policy not in {"skip", "stop", "overwrite"}:
        raise SyncError(f"Unknown conflict policy: {conflict_policy}")
    left_root, right_root = absolute_source(left_root), absolute_source(right_root)
    if backup_directory is not None:
        backup_directory = absolute_source(backup_directory)
        backup_resolved = backup_directory.resolve()
        if any(backup_resolved == root.resolve() or backup_resolved.is_relative_to(root.resolve())
               for root in (left_root, right_root)):
            raise SyncError("Backup directory must be outside both compared folders.")
    report = SyncReport(str(left_root), str(right_root), _now(), dry_run=dry_run,
                        backup_directory=str(backup_directory) if backup_directory else None,
                        left_resolved=str(left_root.resolve()), right_resolved=str(right_root.resolve()),
                        backup_resolved=str(backup_directory.resolve()) if backup_directory else "")
    for index, operation in enumerate(operations):
        if cancelled and cancelled():
            report.cancelled = True
            break
        outcome = {"path": operation.relative, "action": operation.action,
                   "effect": operation.effect, "status": "", "detail": "",
                   "target_side": ("left" if operation.target_root.resolve() == left_root.resolve()
                                   else "right")}
        if operation.metadata_fields:
            outcome["metadata_changes"] = [dict(change, status="planned")
                                           for change in metadata_preview(operation)]
        try:
            target = safe_path(operation.target_root, operation.relative)
            source = safe_path(operation.source_root, operation.relative) if operation.source_root else None
            if operation.effect == "metadata":
                _execute_metadata(operation, target, source, outcome, conflict_policy=conflict_policy,
                                  backup_directory=backup_directory, dry_run=dry_run, cancelled=cancelled)
            else:
                current_target = file_state(target)
                outcome["effect"] = "delete" if source is None else "overwrite" if current_target else "create"
                if source is not None and file_state(source) != operation.source_state:
                    outcome.update(status="conflict", detail="Source changed since review; skipped.")
                elif current_target != operation.target_state and conflict_policy != "overwrite":
                    outcome.update(status="conflict", detail="Target changed since review; skipped.")
                elif operation.source_root is None and current_target is None:
                    outcome.update(status="conflict", detail="Delete target is now missing; skipped.")
                elif dry_run:
                    outcome.update(status="dry run", detail=f"Would {operation.effect}.")
                else:
                    if current_target is not None and backup_directory is not None:
                        side = "left" if operation.target_root.resolve() == left_root.resolve() else "right"
                        backup = _backup(target, backup_directory, side, operation.relative)
                        outcome["detail"] = f"Backup: {backup}"
                        outcome["backup_path"] = str(backup)
                        backup_state = file_state(backup)
                        outcome["backup_size"] = str(backup_state.size)
                        outcome["backup_sha256"] = backup_state.sha256
                    if source is None:
                        if file_state(target) != current_target:
                            raise SyncError(f"Target changed before deletion: {target}")
                        target.unlink()
                    else:
                        _copy_atomic(source, target, operation.source_state, current_target)
                    outcome["status"] = "completed"
                    try:
                        after = file_state(target)
                        if after is not None:
                            outcome["after_size"] = str(after.size)
                            outcome["after_sha256"] = after.sha256
                    except (OSError, SyncError) as exc:
                        outcome["detail"] += f" Post-sync fingerprint unavailable: {exc}"
        except SyncCancelled:
            report.cancelled = True
            outcome.update(status="cancelled", detail=outcome["detail"] + " Remaining fields cancelled.")
        except MetadataConflict as exc:
            outcome.update(status="conflict", detail=str(exc))
        except MetadataUnavailable as exc:
            outcome.update(status="unavailable", detail=str(exc))
        except (OSError, SyncError) as exc:
            outcome.update(status="error", detail=str(exc))
        report.outcomes.append(outcome)
        if progress:
            progress(index + 1, len(operations), outcome)
        if report.cancelled:
            break
        if outcome["status"] == "conflict" and conflict_policy == "stop":
            report.stopped_on_conflict = True
            break
    report.finished_at = _now()
    return report
