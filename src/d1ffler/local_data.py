"""Scoped local-state cleanup with conservative, cross-process report protection."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from d1ffler.user_state import atomic_write, locked_state, state_lock
from d1ffler.source_paths import has_relative_sources

DIGEST = re.compile(r"[0-9a-f]{64}")
ALL_REPORTS = "*"
_mutex = threading.RLock()
_depth = threading.local()
_pins = {}
_users = {}


def register_report_user(root):
    with _mutex:
        key = str(Path(root).absolute())
        _users[key] = _users.get(key, 0) + 1


def unregister_report_user(root):
    with _mutex:
        key = str(Path(root).absolute())
        _users[key] = max(0, _users.get(key, 0) - 1)
        if not _users[key]:
            _users.pop(key, None)
            try:
                release_report_pins(root)
            except OSError:
                # Closing the last view must still release ownership if local
                # bookkeeping is unwritable or another process holds cleanup.
                record = _pins.pop(key, None)
                if record:
                    record[1].unlock()


def safe_local(root: Path, path: Path) -> None:
    """Reject links/junctions and paths outside this state root before any removal."""
    root, path = root.absolute(), path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise OSError("Path is outside local user state.") from exc
    if ".." in relative.parts:
        raise OSError("Invalid local user-state path.")
    for candidate in [root, *(root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1))]:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError(f"Linked local data was retained: {candidate}")


@contextmanager
def storage_guard(root: Path):
    """One lock order for report pins, settings, recovery writes and cleanup."""
    root = Path(root).absolute()
    with _mutex:
        depths = getattr(_depth, "roots", {})
        _depth.roots = depths
        key = str(root)
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        safe_local(root, root / "local-data.lock")
        root.mkdir(parents=True, exist_ok=True)
        with locked_state(root / "local-data.lock"):
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)


def pin_report(root: Path, digest: str) -> None:
    """Keep a monotonic set for this process, including clean open comparisons."""
    if not isinstance(digest, str) or (digest != ALL_REPORTS and not DIGEST.fullmatch(digest)):
        raise OSError("Invalid retained report reference.")
    root = Path(root).absolute()
    with storage_guard(root):
        key = str(root)
        if key not in _pins:
            directory = root / "D1ffler-report-users"
            safe_local(root, directory)
            directory.mkdir(exist_ok=True)
            path = directory / f"{uuid4().hex}.json"
            lease = state_lock(path.with_suffix(".json.lock"))
            if not lease.tryLock(0):
                raise OSError("Could not protect live report references.")
            _pins[key] = (path, lease, set())
        path, _lease, references = _pins[key]
        safe_local(root, path)
        references.add(digest)
        atomic_write(path, json.dumps({"version": 1, "reports": sorted(references)}).encode("utf-8"))


def release_report_pins(root: Path) -> None:
    """For shutdown only: callers must have disposed all comparisons using this root."""
    if str(Path(root).absolute()) not in _pins:
        return
    with storage_guard(root):
        record = _pins.pop(str(Path(root).absolute()), None)
        if record:
            path, lease, _references = record
            try:
                safe_local(Path(root), path)
                path.unlink(missing_ok=True)
            finally:
                lease.unlock()


def read_json(root, path, *, limit=24 * 1024 * 1024):
    safe_local(root, path)
    if path.stat().st_size > limit:
        raise OSError(f"Local state is too large to check safely: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise OSError(f"Unreadable local state was retained: {path}") from exc


def report_references(data):
    try:
        return _report_references(data)
    except RecursionError as exc:
        raise OSError("Reference state is too deeply nested; reports were retained.") from exc


def _report_references(data):
    references = set()
    if isinstance(data, dict):
        if "context_version" in data and data["context_version"] != 1:
            raise OSError("Unknown comparison context; reports were retained.")
        if "report_refs" in data:
            groups = data["report_refs"]
            if not isinstance(groups, dict) or any(not isinstance(items, list) for items in groups.values()):
                raise OSError("Invalid report reference set; reports were retained.")
            for items in groups.values():
                for reference in items:
                    if not isinstance(reference, dict) or not DIGEST.fullmatch(str(reference.get("sha256", ""))):
                        raise OSError("Invalid report reference; reports were retained.")
                    references.add(reference["sha256"])
        if "retained_ref" in data:
            reference = data["retained_ref"]
            if not isinstance(reference, dict) or not DIGEST.fullmatch(str(reference.get("sha256", ""))):
                raise OSError("Invalid historical report reference; reports were retained.")
            references.add(reference["sha256"])
        for value in data.values():
            references.update(_report_references(value))
    elif isinstance(data, list):
        for value in data:
            references.update(_report_references(value))
    return references


def _source_paths(data):
    if isinstance(data, dict):
        for key, value in data.items():
            if key in {"path", "editing_path", "repo", "backup_root"} and isinstance(value, str) and value:
                yield Path(value)
            yield from _source_paths(value)
    elif isinstance(data, list):
        for value in data:
            yield from _source_paths(value)


def _source_identity(path):
    """Resolve existing aliases strictly; missing ordinary paths are still useful.

    Walk to an existing ancestor rather than using non-strict resolve, which can
    suppress access errors. A dangling link is uncertain, not an absent source.
    """
    current = path.absolute()
    missing = []
    try:
        while True:
            try:
                canonical = current.resolve(strict=True)
                info = canonical.stat()
                if (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or
                        getattr(info, "st_file_attributes", 0) & 0x400):
                    raise OSError("Unsupported source identity")
                if missing:
                    return Path(os.path.normpath(str(canonical.joinpath(*reversed(missing))))), None
                return canonical, info
            except FileNotFoundError:
                try:
                    current.lstat()
                except FileNotFoundError:
                    if current == current.parent:
                        raise OSError("Source has no accessible ancestor")
                    missing.append(current.name)
                    current = current.parent
                else:
                    raise OSError("Source alias has an unavailable target")
    except (OSError, RuntimeError, ValueError) as exc:
        raise OSError(f"Could not check source identity; reports were retained: {path}") from exc


def source_report_references(data, directory):
    """Protect canonical source paths, ancestors and hard-linked report files."""
    directory = Path(directory)
    canonical_directory, directory_info = _source_identity(directory)
    references = set()
    file_ids = None
    ancestor_ids = None
    try:
        for path in set(_source_paths(data)):
            source, info = _source_identity(path)
            if canonical_directory == source or canonical_directory.is_relative_to(source):
                references.add(ALL_REPORTS)
                continue
            name = Path(os.path.normcase(source.name))
            if source.parent == canonical_directory and name.suffix == ".json" and DIGEST.fullmatch(name.stem):
                references.add(name.stem)
                continue
            if info is not None and stat.S_ISDIR(info.st_mode):
                if ancestor_ids is None:
                    ancestors = list(canonical_directory.parents)
                    if directory_info is not None:
                        ancestors.insert(0, canonical_directory)
                    ancestor_ids = set()
                    for ancestor in ancestors:
                        ancestor_info = ancestor.stat()
                        if not ancestor_info.st_ino:
                            raise OSError("Report directory identity is unavailable; reports were retained.")
                        ancestor_ids.add((ancestor_info.st_dev, ancestor_info.st_ino))
                if not info.st_ino:
                    raise OSError(f"Source directory identity is unavailable; reports were retained: {path}")
                if (info.st_dev, info.st_ino) in ancestor_ids:
                    references.add(ALL_REPORTS)
                continue
            if info is None or not stat.S_ISREG(info.st_mode) or directory_info is None:
                continue
            if file_ids is None:
                file_ids = {}
                for candidate in directory.iterdir():
                    if candidate.suffix != ".json" or not DIGEST.fullmatch(candidate.stem):
                        continue
                    safe_local(directory.parent, candidate)
                    candidate_info = candidate.stat()
                    if not stat.S_ISREG(candidate_info.st_mode):
                        continue
                    if not candidate_info.st_ino:
                        raise OSError("Report file identity is unavailable; reports were retained.")
                    file_ids.setdefault((candidate_info.st_dev, candidate_info.st_ino), set()).add(candidate.stem)
            if file_ids:
                if not info.st_ino:
                    raise OSError(f"Source file identity is unavailable; reports were retained: {path}")
                references.update(file_ids.get((info.st_dev, info.st_ino), ()))
    except RecursionError as exc:
        raise OSError("Source state is too deeply nested; reports were retained.") from exc
    return references


def protect_local_sources(root, paths):
    with storage_guard(root):
        directory = Path(root) / "D1ffler-reports"
        safe_local(Path(root), directory)
        try:
            references = source_report_references([{"path": str(path)} for path in paths if path], directory)
        except OSError:
            # An already open source may become inaccessible. Publish a live
            # conservative pin so another process cannot clean up uncertain data.
            pin_report(root, ALL_REPORTS)
            raise
        for reference in references:
            pin_report(root, reference)


@dataclass
class LocalEntry:
    kind: str
    path: Path
    size: int
    modified: float
    data: object = None
    status: str = ""
    fingerprint: tuple = ()


class LocalDataStore:
    def __init__(self, settings_path):
        self.settings_path = Path(settings_path)
        self.root = self.settings_path.parent
        self.reports = self.root / "D1ffler-reports"
        self.recovery = self.root / "D1ffler-recovery"
        self.policy_path = self.root / "local-data-policy.json"

    def _settings(self):
        if not self.settings_path.exists():
            return {"schema_version": 1, "pinned_workspaces": [], "recent_workspaces": []}
        data = read_json(self.root, self.settings_path)
        if not isinstance(data, dict) or data.get("schema_version", 0) not in (0, 1):
            raise OSError("Unsupported settings; local history and reports were retained.")
        for key in ("pinned_workspaces", "recent_workspaces"):
            if not isinstance(data.get(key, []), list) or any(not isinstance(item, dict) for item in data.get(key, [])):
                raise OSError("Invalid workspace history; reports were retained.")
        return data

    def _references(self):
        safe_local(self.root, self.reports)
        settings = self._settings()
        for key in ("pinned_workspaces", "recent_workspaces"):
            if any(has_relative_sources(item.get("panels", [])) for item in settings.get(key, [])):
                raise OSError("A workspace has unresolved relative sources; reports were retained. Locate its original folder first.")
        references = report_references(settings) | source_report_references(settings, self.reports)
        safe_local(self.root, self.recovery)
        if self.recovery.exists():
            for path in self.recovery.glob("*.json"):
                if path.name == "migration-index.json":
                    continue
                data = read_json(self.root, path)
                if (not isinstance(data, dict) or data.get("version") not in (1, 2) or
                        not isinstance(data.get("panels"), list) or
                        any(not isinstance(panel, dict) for panel in data["panels"]) or
                        ("context" in data and not isinstance(data["context"], dict))):
                    raise OSError(f"Unknown recovery state; reports were retained: {path}")
                references.update(report_references(data))
                if has_relative_sources(data["panels"]):
                    raise OSError("Recovery has unresolved relative sources; reports were retained. Locate its original folder first.")
                references.update(source_report_references(data, self.reports))
        directory = self.root / "D1ffler-report-users"
        safe_local(self.root, directory)
        if directory.exists():
            for path in directory.glob("*.json"):
                safe_local(self.root, path.with_suffix(".json.lock"))
                lease = state_lock(path.with_suffix(".json.lock"))
                if lease.tryLock(0):
                    lease.unlock()  # Abandoned process marker cannot represent an open comparison.
                    continue
                data = read_json(self.root, path)
                if (not isinstance(data, dict) or data.get("version") != 1 or
                        not isinstance(data.get("reports"), list) or
                        any(not isinstance(item, str) or (item != ALL_REPORTS and not DIGEST.fullmatch(item)) for item in data["reports"])):
                    raise OSError(f"Unreadable live references; reports were retained: {path}")
                references.update(data["reports"])
        return references

    def preview_reports(self, days=0):
        with storage_guard(self.root):
            references = self._references()
            safe_local(self.root, self.reports)
            entries = []
            if self.reports.exists():
                for path in sorted(self.reports.glob("*.json")):
                    if not DIGEST.fullmatch(path.stem):
                        continue
                    safe_local(self.root, path)
                    info = path.stat()
                    if not path.is_file() or ALL_REPORTS in references or path.stem in references or info.st_mtime > time.time() - days * 86400:
                        continue
                    if info.st_size > 24 * 1024 * 1024:
                        raise OSError(f"Oversized report retained: {path}")
                    entries.append(LocalEntry("report", path, info.st_size, info.st_mtime,
                                              fingerprint=(info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())))
            return entries

    def cleanup_reports(self, preview):
        removed = []
        with storage_guard(self.root):
            references = self._references()  # Recheck immediately before removing anything.
            for entry in preview:
                path = entry.path
                safe_local(self.root, path)
                if path.parent != self.reports or not DIGEST.fullmatch(path.stem) or path.suffix != ".json":
                    raise OSError("Invalid cleanup selection; remaining reports were retained.")
                if ALL_REPORTS in references or path.stem in references or not path.exists():
                    continue
                info = path.stat()
                if info.st_size > 24 * 1024 * 1024:
                    raise OSError(f"Report changed; remaining reports were retained: {path}")
                actual = (info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                if actual != entry.fingerprint:
                    raise OSError(f"Report changed since preview; remaining reports were retained: {path}")
                try:
                    path.unlink()
                except OSError as exc:
                    raise OSError(f"Removed {len(removed)} report(s). Cleanup stopped; remaining data was retained: {exc}") from exc
                removed.append(path)
        return removed

    def remove_history(self, selections):
        """Delete exact reviewed records, preserving concurrent changes and other settings."""
        safe_local(self.root, self.settings_path.with_name(self.settings_path.name + ".lock"))
        with storage_guard(self.root), locked_state(self.settings_path.with_name(self.settings_path.name + ".lock")):
            data = self._settings()
            for kind, expected in selections:
                if kind not in ("pinned_workspaces", "recent_workspaces") or expected not in data.get(kind, []):
                    raise OSError("History changed since it was displayed. Refresh before removing it.")
                data[kind].remove(expected)
            atomic_write(self.settings_path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        from d1ffler.app_settings import read_app_settings
        return read_app_settings(self.settings_path)

    def policy(self):
        if not self.policy_path.exists():
            return {"enabled": False, "days": 30}
        data = read_json(self.root, self.policy_path)
        if (not isinstance(data, dict) or type(data.get("enabled")) is not bool or
                type(data.get("days")) is not int or not 0 <= data["days"] <= 3650):
            raise OSError("Invalid retention rule; automatic cleanup was not run.")
        return data

    def save_policy(self, enabled, days):
        if type(enabled) is not bool or type(days) is not int or not 0 <= days <= 3650:
            raise OSError("Invalid retention rule.")
        with storage_guard(self.root):
            safe_local(self.root, self.policy_path)
            atomic_write(self.policy_path, json.dumps({"enabled": enabled, "days": days}).encode("utf-8"))

    def apply_retention(self):
        with storage_guard(self.root):
            rule = self.policy()
            return self.cleanup_reports(self.preview_reports(rule["days"])) if rule["enabled"] else []

    def inventory(self):
        """Return owned files and workspace records; never traverse sources or links."""
        entries, errors = [], []
        with storage_guard(self.root):
            try:
                settings = self._settings()
                for kind in ("pinned_workspaces", "recent_workspaces"):
                    for item in settings.get(kind, []):
                        entries.append(LocalEntry(kind, self.settings_path, len(json.dumps(item).encode("utf-8")), 0, item))
            except OSError as exc:
                errors.append(str(exc))
            try:
                references = self._references()
            except OSError as exc:
                references = None
                errors.append(str(exc))
            groups = [("settings", [self.settings_path, self.policy_path]),
                      ("recovery", list(self.recovery.glob("*.json"))),
                      ("report", list(self.reports.glob("*.json"))),
                      ("diagnostics", [self.root / "diagnostics" / name for name in
                                       ("diagnostics.jsonl", "diagnostics.jsonl.1", "diagnostics.jsonl.2")])]
            listed = {path for _kind, paths in groups for path in paths}
            metadata = []
            for directory in (self.root, self.recovery, self.reports, self.root / "diagnostics", self.root / "D1ffler-report-users"):
                try:
                    safe_local(self.root, directory)
                    if directory.exists():
                        metadata.extend(path for path in directory.iterdir() if path not in listed and path.is_file())
                except OSError as exc:
                    errors.append(str(exc))
            groups.append(("metadata", metadata))
            for kind, paths in groups:
                for path in paths:
                    if not path.exists():
                        continue
                    try:
                        safe_local(self.root, path)
                        info = path.stat()
                        if not path.is_file():
                            continue
                        data, status = None, ""
                        if kind == "report":
                            status = "Reference check unavailable" if references is None else (
                                "Referenced" if ALL_REPORTS in references or path.stem in references else "Unreferenced")
                        elif kind == "recovery" and path.name != "migration-index.json":
                            lease = state_lock(path.with_suffix(".json.lock"))
                            safe_local(self.root, path.with_suffix(".json.lock"))
                            if lease.tryLock(0):
                                lease.unlock()
                                data = read_json(self.root, path)
                                status = "Abandoned"
                            else:
                                status = "Live — protected"
                        elif kind == "recovery":
                            status = "Migration index — protected"
                        entries.append(LocalEntry(kind, path, info.st_size, info.st_mtime, data, status))
                    except OSError as exc:
                        errors.append(str(exc))
        return entries, errors
