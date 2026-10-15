"""Stable accepted source paths and explicit resolution of older relative state."""
from copy import deepcopy
from pathlib import Path


def absolute_source(value, base=None):
    """Anchor a spelling without resolving away symlinks or junctions."""
    path = Path(value)
    if not path.is_absolute() and base is not None:
        # Drive-relative/root-relative Windows spellings need more than a base.
        if path.drive or path.root:
            raise ValueError(f"Choose an absolute source path instead of {value}")
        path = Path(base) / path
    return path.absolute()


def has_relative_sources(records):
    if not isinstance(records, (list, tuple)):
        return True
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("unresolved_source"):
            return True
        paths = [record.get(key) for key in ("path", "editing_path")]
        git = record.get("git")
        if isinstance(git, dict):
            paths.append(git.get("repo"))
        if any(isinstance(value, str) and value and not Path(value).is_absolute() for value in paths):
            return True
    return False


def resolve_source_records(records, base=None):
    """Use a user-selected base, or detach ambiguous recovery buffers from disk."""
    resolved = deepcopy(records)
    for record in resolved:
        if not isinstance(record, dict) or not has_relative_sources([record]):
            continue
        original = record.get("unresolved_source")
        if not isinstance(original, dict):
            original = {key: record.get(key) for key in ("kind", "path", "editing_path", "git")}
        if base is None:
            record.update(kind="text" if record.get("recover") else None, path=None,
                          editing_path=None, git=None, unresolved_source=original)
        else:
            record.update(original)
            record.pop("unresolved_source", None)
            for key in ("path", "editing_path"):
                if record.get(key):
                    record[key] = str(absolute_source(record[key], base))
            git = record.get("git")
            if isinstance(git, dict) and git.get("repo"):
                git["repo"] = str(absolute_source(git["repo"], base))
    return resolved


def absolute_worktrees(records):
    """Legacy registrations without a base must not authorize worktree removal."""
    return {item["path"]: item["repo"] for item in records
            if isinstance(item, dict) and all(isinstance(item.get(key), str) and
               Path(item[key]).is_absolute() for key in ("path", "repo"))}
