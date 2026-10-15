"""Per-user settings, legacy import, and concurrent atomic saves."""
from __future__ import annotations
from copy import deepcopy
import json
import os
import sys
from pathlib import Path
from d1ffler.ui_theme import THEMES
from d1ffler.user_state import atomic_write, cleanup_temporaries, locked_state
from d1ffler.local_data import storage_guard, safe_local

DEFAULT_SETTINGS = {
    "theme": "dark", "excluded_paths": "", "ignore_whitespace": False,
    "ignore_line_endings": False, "ignore_gitignore": False,
    "follow_internal_links": False, "compare_metadata": False, "text_rules": [],
    "recent_workspaces": [], "pinned_workspaces": [], "managed_worktrees": [],
}


class SettingsConflictError(OSError):
    """Concurrent edits to the same workspace or worktree registration."""


class SettingsState(dict):
    """Settings with a private durable baseline for shared-state merges."""
    def __init__(self, values=None, *, baseline=None):
        super().__init__(deepcopy(DEFAULT_SETTINGS if values is None else values))
        self.baseline = deepcopy(dict(self) if baseline is None else baseline)
        self.startup_warnings = []


def user_state_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "D1ffler"


def settings_candidates() -> list[Path]:
    """The durable user location first, followed by legacy import locations."""
    legacy_root = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                   else Path(__file__).resolve().parents[2])
    return list(dict.fromkeys([user_state_directory() / "appsettings.json",
                               legacy_root / "appsettings.json"]))


def _normalized(data: dict) -> dict:
    settings = deepcopy(DEFAULT_SETTINGS)
    for key, default in DEFAULT_SETTINGS.items():
        value = data.get(key, default)
        if isinstance(default, bool):
            settings[key] = value is True
        elif isinstance(default, list):
            settings[key] = deepcopy(value) if isinstance(value, list) else []
        else:
            settings[key] = str(value)
    if settings["theme"] not in THEMES:
        settings["theme"] = DEFAULT_SETTINGS["theme"]
    return settings


def read_app_settings(path: Path) -> SettingsState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version", 0) not in (0, 1):
            raise ValueError("Unsupported or invalid settings format")
    except FileNotFoundError:
        return SettingsState()
    except (ValueError, UnicodeError) as exc:
        raise OSError(f"Could not read {path}: {exc}. The original file has been retained.") from exc
    return SettingsState(_normalized(data))


def load_app_settings() -> tuple[SettingsState, Path]:
    candidates = settings_candidates()
    destination = candidates[0]
    warnings = []
    try:
        settings = read_app_settings(destination)
    except OSError as exc:
        settings = SettingsState()
        warnings.append(str(exc))
    if not destination.exists():
        for legacy in candidates[1:]:
            if not legacy.exists():
                continue
            try:
                settings = read_app_settings(legacy)
                settings.baseline = deepcopy(DEFAULT_SETTINGS)
                save_app_settings(settings, destination)
                break
            except OSError as exc:
                warnings.append(f"Could not import settings from {legacy}: {exc}")
    from d1ffler.recovery import RecoveryError, migrate_recovery
    for legacy in candidates[1:]:
        try:
            migrate_recovery(legacy.parent / "D1ffler-recovery",
                             destination.parent / "D1ffler-recovery")
        except (RecoveryError, OSError) as exc:
            warnings.append(f"Could not import recovery from {legacy.parent}: {exc}. Originals were retained.")
    settings.startup_warnings = warnings
    return settings, destination


def _merge_records(key: str, identity: str, baseline: dict, local: dict, disk: dict) -> list:
    def records(values):
        return {item[identity]: item for item in values.get(key, [])
                if isinstance(item, dict) and isinstance(item.get(identity), str)}
    before, desired, current = records(baseline), records(local), records(disk)
    merged = deepcopy(current)
    for name in dict.fromkeys([*before, *desired]):
        old, new, latest = before.get(name), desired.get(name), current.get(name)
        if new == old:
            continue
        if latest != old and latest != new:
            suggestion = ("Save the workspace under a different name to keep both versions."
                          if key == "pinned_workspaces" else "Reload the saved worktree registrations before retrying.")
            raise SettingsConflictError(
                f"Another D1ffler instance changed {key.replace('_', ' ')}: {name}. "
                f"Your local comparison is retained. {suggestion}")
        if new is None:
            merged.pop(name, None)
        else:
            merged[name] = deepcopy(new)
    return list(merged.values())


def settings_for_save(settings: dict, *, baseline=None, workspace_baseline=None) -> SettingsState:
    candidate = SettingsState(settings, baseline=baseline if baseline is not None else settings.baseline)
    if workspace_baseline is not None:
        name, expected = workspace_baseline
        candidate.baseline["pinned_workspaces"] = [item for item in candidate.baseline["pinned_workspaces"]
                                                  if isinstance(item, dict) and item.get("name") != name]
        if expected is not None:
            candidate.baseline["pinned_workspaces"].append(deepcopy(expected))
    return candidate


def save_app_settings(settings: dict[str, object], path: Path) -> None:
    baseline = getattr(settings, "baseline", DEFAULT_SETTINGS)
    desired = _normalized(settings)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_local(path.parent, path)
    safe_local(path.parent, path.with_name(path.name + ".lock"))
    with storage_guard(path.parent), locked_state(path.with_name(path.name + ".lock")):
        cleanup_temporaries(path)
        disk = dict(read_app_settings(path))
        # Unchanged preferences retain external updates; explicit changes take precedence.
        merged = {key: deepcopy(disk[key] if desired[key] == baseline.get(key, DEFAULT_SETTINGS[key])
                                else desired[key]) for key in DEFAULT_SETTINGS}
        for key, identity in (("pinned_workspaces", "name"), ("managed_worktrees", "path")):
            merged[key] = _merge_records(key, identity, baseline, desired, disk)
        if desired["recent_workspaces"] != baseline.get("recent_workspaces", []):
            # Apply additions/updates and explicit removals against the durable
            # baseline. A stale instance must not resurrect externally removed history.
            identity = lambda item: json.dumps(item.get("panels"), sort_keys=True)
            before = {identity(item): item for item in baseline.get("recent_workspaces", []) if isinstance(item, dict)}
            wanted = {identity(item): item for item in desired["recent_workspaces"] if isinstance(item, dict)}
            current = {identity(item): item for item in disk["recent_workspaces"] if isinstance(item, dict)}
            for key, old in before.items():
                if key not in wanted:
                    if key in current and current[key] != old:
                        raise SettingsConflictError("Another D1ffler instance changed recent history. Refresh before removing it.")
                    current.pop(key, None)
            recent, seen = [], set()
            changed = [item for key, item in wanted.items() if key not in before or item != before[key]]
            for item in changed + list(current.values()):
                if not isinstance(item, dict):
                    continue
                key = identity(item)
                if key not in seen:
                    recent.append(deepcopy(item))
                    seen.add(key)
            merged["recent_workspaces"] = recent[:10]
        atomic_write(path, (json.dumps({"schema_version": 1, **merged}, ensure_ascii=False,
                                      indent=2) + "\n").encode("utf-8"))
    settings.clear()
    settings.update(deepcopy(merged))
    if isinstance(settings, SettingsState):
        settings.baseline = deepcopy(merged)
