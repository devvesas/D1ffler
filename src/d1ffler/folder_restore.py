"""Reviewed restoration of completed folder-sync operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from d1ffler.folder_links import inspect_link
from d1ffler.folder_sync import (FileState, MetadataState, METADATA_ACTIONS, SyncOperation,
                         SyncCancelled, SyncError, _copy_atomic, file_state, safe_path,
                         metadata_state, execute_plan)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RestoreItem:
    relative: str
    side: str
    action: str  # restore_backup, restore_metadata, or delete_created
    target_root: Path
    backup_root: Path
    target_root_identity: tuple[int, int]
    backup_root_identity: tuple[int, int] | None
    backup: Path | None
    expected_after: FileState | None
    after_known: bool
    reviewed_current: FileState | None
    reviewed_backup: FileState | None
    status: str  # ready, changed, unknown, unavailable
    detail: str = ""
    metadata_fields: tuple[str, ...] = ()
    expected_metadata: MetadataState | None = None
    reviewed_metadata: MetadataState | None = None
    backup_metadata: MetadataState | None = None


@dataclass
class RestoreReport:
    source_started_at: str
    source_backup_directory: str
    started_at: str
    finished_at: str = ""
    dry_run: bool = False
    cancelled: bool = False
    stopped_on_conflict: bool = False
    outcomes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _state_from_outcome(outcome: dict, prefix: str) -> tuple[FileState | None, bool]:
    digest = outcome.get(f"{prefix}_sha256")
    size = outcome.get(f"{prefix}_size")
    if digest is None or size is None:
        return None, False
    try:
        return FileState(int(size), str(digest)), True
    except (TypeError, ValueError):
        return None, False


def _side(action: str) -> str | None:
    if action in METADATA_ACTIONS:
        return "right" if action.endswith("left_to_right") else "left"
    return {"left_to_right": "right", "delete_right": "right",
            "right_to_left": "left", "delete_left": "left"}.get(action)


def _checked_root(raw: str, expected: str | None = None) -> Path:
    root = Path(raw)
    if not root.is_absolute() or not root.is_dir() or inspect_link(root, root.parent) is not None:
        raise SyncError(f"Restore root is unavailable or linked: {root}")
    resolved = root.resolve(strict=True)
    if expected and resolved != Path(expected):
        raise SyncError(f"Restore root changed since sync: {root}")
    return root


def _identity(path: Path) -> tuple[int, int]:
    entry = path.stat()
    return entry.st_dev, entry.st_ino


def _backup_path(outcome: dict, backup_root: Path, side: str, relative: str) -> Path:
    recorded = outcome.get("backup_path")
    if not recorded:
        detail = str(outcome.get("detail", ""))
        if detail.startswith("Backup: "):
            recorded = detail.splitlines()[0][8:].split(" Post-sync fingerprint unavailable:")[0]
    if not recorded:
        raise SyncError("No recorded backup path for this operation.")
    path = Path(str(recorded))
    expected = safe_path(backup_root, f"{side}/{relative}")
    if not path.is_absolute() or path.resolve() != expected.resolve():
        raise SyncError(f"Recorded backup path is outside its expected location: {recorded}")
    return expected


def build_restore_plan(report: dict, cancelled: Callable[[], bool] | None = None) -> list[RestoreItem]:
    """Only completed operations are eligible; unsafe rows remain visible as unavailable."""
    if report.get("dry_run") or not report.get("backup_directory"):
        return []
    left = _checked_root(str(report.get("left", "")), report.get("left_resolved"))
    right = _checked_root(str(report.get("right", "")), report.get("right_resolved"))
    backup_root = Path(str(report["backup_directory"]))
    needs_backup = any(isinstance(outcome, dict) and outcome.get("status") == "completed" and
                       outcome.get("effect") in {"overwrite", "delete", "metadata"}
                       for outcome in report.get("outcomes", []))
    if needs_backup:
        backup_root = _checked_root(str(backup_root), report.get("backup_resolved"))
    elif not backup_root.is_absolute():
        raise SyncError("Recorded backup directory is not absolute.")
    backup_resolved = backup_root.resolve(strict=needs_backup)
    if any(backup_resolved == root.resolve() or backup_resolved.is_relative_to(root.resolve()) or
           root.resolve().is_relative_to(backup_resolved)
           for root in (left, right)):
        raise SyncError("Backup directory overlaps a compared folder.")
    items = []
    seen = set()
    for outcome in report.get("outcomes", []):
        if cancelled and cancelled():
            raise SyncCancelled()
        if not isinstance(outcome, dict) or outcome.get("status") != "completed":
            continue
        relative, effect = str(outcome.get("path", "")), str(outcome.get("effect", ""))
        sync_action = str(outcome.get("action", ""))
        side = _side(sync_action)
        if side is None or effect not in {"create", "overwrite", "delete", "metadata"}:
            continue
        target_root = left if side == "left" else right
        action = ("restore_metadata" if effect == "metadata" else
                  "delete_created" if effect == "create" else "restore_backup")
        key = (side, relative)
        error = ""
        if ((effect == "delete" and sync_action not in {"delete_left", "delete_right"}) or
                (effect in {"create", "overwrite"} and sync_action not in {"left_to_right", "right_to_left"}) or
                (effect == "metadata" and sync_action not in METADATA_ACTIONS)):
            error = "Recorded effect disagrees with the sync action."
        if effect == "create" and (outcome.get("backup_path") or
                                   str(outcome.get("detail", "")).startswith("Backup: ")):
            error = "A created file unexpectedly has a recorded backup."
        if outcome.get("target_side") and outcome["target_side"] != side:
            error = "Recorded target side disagrees with the sync action."
        target = backup = None
        current = saved = expected = None
        fields = ()
        current_metadata = saved_metadata = expected_metadata = None
        after_known = effect == "delete"
        if key in seen:
            error = "The sync report contains duplicate completed operations for this path."
        seen.add(key)
        try:
            target = safe_path(target_root, relative)
            if action in {"restore_backup", "restore_metadata"}:
                backup = _backup_path(outcome, backup_root, side, relative)
                saved = file_state(backup, cancelled)
                if saved is None:
                    raise SyncError(f"Recorded backup is missing: {backup}")
                recorded_backup, backup_known = _state_from_outcome(outcome, "backup")
                if backup_known and recorded_backup != saved:
                    raise SyncError(f"Recorded backup changed since sync: {backup}")
            current = file_state(target, cancelled)
            if effect != "delete":
                expected, after_known = _state_from_outcome(outcome, "after")
            if action == "restore_metadata":
                changes = outcome.get("metadata_changes", [])
                if not isinstance(changes, list) or any(not isinstance(change, dict) or
                                                       not isinstance(change.get("field"), str) for change in changes):
                    raise SyncError("Invalid recorded metadata fields.")
                fields = tuple(change["field"] for change in changes)
                if not fields or len(set(fields)) != len(fields) or any(
                        name not in METADATA_ACTIONS[sync_action] for name in fields):
                    raise SyncError("Invalid recorded metadata fields.")
                current_metadata = metadata_state(target) if current is not None else None
                saved_metadata = metadata_state(backup)
                recorded_metadata = outcome.get("backup_metadata")
                if not isinstance(recorded_metadata, dict) or asdict(saved_metadata) != recorded_metadata:
                    raise SyncError("Backup metadata changed since sync or was not recorded.")
                recorded_after = outcome.get("after_metadata")
                if isinstance(recorded_after, dict):
                    try:
                        expected_metadata = MetadataState(**recorded_after)
                    except TypeError as exc:
                        raise SyncError("Invalid post-sync metadata.") from exc
                after_known = after_known and expected_metadata is not None
                if current is None:
                    raise SyncError("Metadata restore requires an existing target file.")
        except SyncCancelled:
            raise
        except (OSError, SyncError) as exc:
            error = str(exc)
        status = ("unavailable" if error else "unknown" if not after_known else
                  "ready" if current == expected and (action != "restore_metadata" or
                                                       current_metadata == expected_metadata) else "changed")
        detail = error or ("Post-sync fingerprint was not recorded; explicit overwrite is required."
                           if status == "unknown" else "Target changed since sync." if status == "changed" else "")
        if not error and action == "restore_backup" and not outcome.get("backup_sha256"):
            detail += (" " if detail else "") + "Legacy backup: original fingerprint unavailable."
        items.append(RestoreItem(relative, side, action, target_root, backup_root,
                                 _identity(target_root), _identity(backup_root) if needs_backup else None, backup,
                                 expected, after_known, current, saved, status, detail,
                                 fields, expected_metadata, current_metadata, saved_metadata))
    return items


def execute_restore(
    items: list[RestoreItem], source_report: dict, *, conflict_policy: str = "skip",
    dry_run: bool = False, cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int, dict], None] | None = None,
) -> RestoreReport:
    if conflict_policy not in {"skip", "stop", "overwrite"}:
        raise SyncError(f"Unknown restore conflict policy: {conflict_policy}")
    result = RestoreReport(str(source_report.get("started_at", "")),
                           str(source_report.get("backup_directory", "")),
                           _now(), dry_run=dry_run)
    for index, item in enumerate(items):
        if cancelled and cancelled():
            result.cancelled = True
            break
        outcome = {"path": item.relative, "side": item.side, "action": item.action,
                   "status": "", "detail": ""}
        try:
            if item.status == "unavailable":
                raise SyncError(item.detail)
            target_root = _checked_root(str(item.target_root))
            uses_backup = item.action in {"restore_backup", "restore_metadata"}
            backup_root = _checked_root(str(item.backup_root)) if uses_backup else item.backup_root
            if (_identity(target_root) != item.target_root_identity or
                    (uses_backup and _identity(backup_root) != item.backup_root_identity)):
                raise SyncError("A restore root changed since review.")
            target = safe_path(target_root, item.relative)
            current = file_state(target, cancelled)
            if uses_backup:
                backup = safe_path(backup_root, f"{item.side}/{item.relative}")
                if item.backup is None or backup.resolve() != item.backup.resolve():
                    raise SyncError("Backup path changed since review.")
                if file_state(backup, cancelled) != item.reviewed_backup:
                    raise SyncError("Backup changed since review; restore skipped.")
            else:
                backup = None
            changed = not item.after_known or current != item.expected_after or current != item.reviewed_current
            if item.action == "restore_metadata":
                if metadata_state(backup) != item.backup_metadata:
                    raise SyncError("Backup metadata changed since review; restore skipped.")
                current_metadata = metadata_state(target) if current is not None else None
                changed = changed or current_metadata != item.expected_metadata or current_metadata != item.reviewed_metadata
            if changed and conflict_policy != "overwrite":
                outcome.update(status="conflict", detail="Target changed or post-sync state is unknown; skipped.")
            elif item.action == "delete_created" and current is None:
                outcome.update(status="conflict", detail="Created file is already missing; skipped.")
            elif item.action == "restore_metadata":
                # Source and target have different relative paths in a saved backup.
                operation = SyncOperation(item.relative, "metadata_left_to_right", "metadata",
                                          backup_root / item.side, target_root, item.reviewed_backup, current, False,
                                          item.metadata_fields, item.backup_metadata, current_metadata)
                restored = execute_plan([operation], backup_root / item.side, target_root,
                                        dry_run=dry_run, cancelled=cancelled)
                if restored.outcomes:
                    outcome.update(restored.outcomes[0])
                outcome.update(side=item.side, action=item.action)
                if restored.cancelled:
                    result.cancelled = True
                    if not restored.outcomes:
                        outcome.update(status="cancelled", detail="Metadata restore cancelled.")
            elif dry_run:
                outcome.update(status="dry run", detail="Would restore backup." if backup else "Would delete created file.")
            else:
                if backup is None:
                    if file_state(target) != current:
                        raise SyncError(f"Target changed before deletion: {target}")
                    target.unlink()
                else:
                    _copy_atomic(backup, target, item.reviewed_backup, current)
                outcome["status"] = "completed"
        except SyncCancelled:
            result.cancelled = True
            break
        except (OSError, SyncError) as exc:
            outcome.update(status="error", detail=str(exc))
        result.outcomes.append(outcome)
        if progress:
            progress(index + 1, len(items), outcome)
        if result.cancelled:
            break
        if outcome["status"] == "conflict" and conflict_policy == "stop":
            result.stopped_on_conflict = True
            break
    result.finished_at = _now()
    return result
