"""Atomic recovery snapshots with live-process ownership and legacy import."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4
from d1ffler.user_state import atomic_write, cleanup_temporaries, locked_state, state_lock
from d1ffler.local_data import storage_guard, safe_local
from d1ffler.source_paths import absolute_source

MAX_SNAPSHOT_BYTES = 24 * 1024 * 1024
# Live snapshots consume slots, but all live owners survive even above this limit.
MAX_SNAPSHOTS = 5


class RecoveryError(Exception):
    pass


def _snapshot(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") not in {1, 2} or not isinstance(data.get("panels"), list):
            return None
        if not any(isinstance(panel, dict) and panel.get("recover") for panel in data["panels"]):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


class RecoveryStore:
    def __init__(self, settings_path: Path) -> None:
        self.directory = absolute_source(settings_path).parent / "D1ffler-recovery"
        self.session_path = self.directory / f"{uuid4().hex}.json"
        self.protected_paths = lambda: set()
        self._lease = None

    def snapshots(self, *, include_active=False) -> list[tuple[Path, dict]]:
        try:
            safe_local(self.directory.parent, self.directory)
        except OSError:
            return []
        if not self.directory.exists():
            return []
        for temporary in self.directory.glob(".*.tmp"):
            if not re.fullmatch(r"\.(?:[0-9a-f]{32}|legacy-[0-9a-f]{64})\.json-.*\.tmp", temporary.name):
                continue
            target = self.directory / temporary.name[1:temporary.name.index(".json-") + 5]
            lease = state_lock(target.with_suffix(".json.lock"))
            if lease.tryLock(0):
                try:
                    cleanup_temporaries(target)
                finally:
                    lease.unlock()
        found = []
        for path in self.directory.glob("*.json"):
            try:
                safe_local(self.directory.parent, path)
                safe_local(self.directory.parent, path.with_suffix(".json.lock"))
            except OSError:
                continue
            lease = None
            if not include_active:
                lease = state_lock(path.with_suffix(".json.lock"))
                if not lease.tryLock(0):
                    continue
            try:
                data = _snapshot(path)
                if data is not None:
                    found.append((path, data, path.stat().st_mtime))
            except OSError:
                continue
            finally:
                if lease is not None:
                    lease.unlock()
        return [(path, data) for path, data, _time in sorted(found, key=lambda item: item[2], reverse=True)]

    def release(self, *, new_session=False) -> None:
        if self._lease is not None:
            self._lease.unlock()
            self._lease = None
        if new_session:
            self.session_path = self.directory / f"{uuid4().hex}.json"

    def adopt(self, path: Path, *, expected: dict | None = None) -> None:
        safe_local(self.directory.parent, path)
        safe_local(self.directory.parent, path.with_suffix(".json.lock"))
        if path.parent.resolve() != self.directory.resolve() or path.suffix != ".json":
            raise RecoveryError("Invalid recovery snapshot path.")
        lease = state_lock(path.with_suffix(".json.lock"))
        if not lease.tryLock(0):
            raise RecoveryError("Another D1ffler instance is using this recovery snapshot. Its edits were retained.")
        data = _snapshot(path)
        if data is None or (expected is not None and data != expected):
            lease.unlock()
            raise RecoveryError("Recovery changed since it was offered. Reopen recovery to review the latest snapshot.")
        self.release()
        self.session_path, self._lease = path, lease

    def write(self, data: dict) -> None:
        with storage_guard(self.directory.parent):
            safe_local(self.directory.parent, self.session_path)
            safe_local(self.directory.parent, self.session_path.with_suffix(".json.lock"))
            self._write(data)

    def _write(self, data: dict) -> None:
        payload = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise RecoveryError("Recovery data exceeds the 24 MiB snapshot limit.")
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._lease is None:
                lease = state_lock(self.session_path.with_suffix(".json.lock"))
                if not lease.tryLock(0):
                    raise RecoveryError("Another D1ffler instance owns this recovery snapshot.")
                self._lease = lease
            cleanup_temporaries(self.session_path)
            atomic_write(self.session_path, payload)
        except OSError as exc:
            raise RecoveryError(f"Could not save recovery snapshot: {exc}") from exc
        self._prune()

    def clear(self) -> None:
        try:
            if self._lease is not None or self.directory.exists():
                with storage_guard(self.directory.parent):
                    safe_local(self.directory.parent, self.session_path)
                    safe_local(self.directory.parent, self.session_path.with_suffix(".json.lock"))
                    if self._lease is not None:
                        self.session_path.unlink(missing_ok=True)
                    else:
                        with locked_state(self.session_path.with_suffix(".json.lock"), timeout=0):
                            self.session_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RecoveryError(f"Could not clear recovery snapshot: {exc}") from exc
        self.release()

    def discard(self, path: Path, *, expected: dict | None = None) -> None:
        if path.parent.resolve() != self.directory.resolve() or path.suffix != ".json":
            raise RecoveryError("Invalid recovery snapshot path.")
        try:
            safe_local(self.directory.parent, path)
            safe_local(self.directory.parent, path.with_suffix(".json.lock"))
            with storage_guard(self.directory.parent), locked_state(path.with_suffix(".json.lock"), timeout=0):
                safe_local(self.directory.parent, path)
                if expected is not None and _snapshot(path) != expected:
                    raise RecoveryError("Recovery changed since inspection. Refresh before discarding it.")
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise RecoveryError(f"Could not discard recovery: {exc}") from exc

    def _prune(self) -> None:
        with storage_guard(self.directory.parent):
            snapshots = self.snapshots(include_active=True)
            actual = {path for path, _data in snapshots}
            # Empty/clean tabs reserve a UUID without ever writing a snapshot.
            keep = actual & ({self.session_path} | self.protected_paths())
            for path, _data in snapshots:
                if path in keep:
                    continue
                try:
                    safe_local(self.directory.parent, path)
                    safe_local(self.directory.parent, path.with_suffix(".json.lock"))
                    lease = state_lock(path.with_suffix(".json.lock"))
                    if lease.tryLock(0):
                        lease.unlock()
                    else:
                        keep.add(path)
                except OSError:
                    # Uncertain ownership must never make an entry deletable.
                    keep.add(path)
            limit = max(MAX_SNAPSHOTS, len(keep))
            for path, _data in snapshots:
                if len(keep) >= limit:
                    break
                keep.add(path)
            for path, data in snapshots:
                if path in keep:
                    continue
                try:
                    self.discard(path, expected=data)
                except RecoveryError:
                    # Ownership/content may change after enumeration. A failed
                    # removal retains the entry without deleting a substitute.
                    continue


def migrate_recovery(source: Path, destination: Path) -> None:
    """Copy abandoned legacy snapshots once, retaining originals."""
    if source.resolve() == destination.resolve() or not source.is_dir():
        return
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    index_path = destination / "migration-index.json"
    with storage_guard(destination.parent), locked_state(destination / "migration.lock"):
        cleanup_temporaries(index_path)
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(index, dict):
                raise ValueError("Invalid recovery migration index")
        except FileNotFoundError:
            index = {}
        except (ValueError, UnicodeError) as exc:
            raise RecoveryError(f"Could not read recovery migration index: {exc}") from exc
        for path in source.glob("*.json"):
            lease = None
            lock_path = path.with_suffix(".json.lock")
            # Legacy snapshots have no ownership marker. Importing them must not
            # require write permission in the old executable directory.
            if lock_path.exists():
                lease = state_lock(lock_path)
                if not lease.tryLock(0):
                    continue
            try:
                if _snapshot(path) is None:
                    continue
                payload = path.read_bytes()
                if len(payload) > MAX_SNAPSHOT_BYTES or (lease is None and lock_path.exists()):
                    continue
                # Legacy UUID filenames remain stable when a portable directory moves.
                identity = hashlib.sha256(path.name.encode("utf-8") + b"\0" + payload).hexdigest()
                if identity in index:
                    continue
                target = destination / f"legacy-{identity}.json"
                if not target.exists():
                    atomic_write(target, payload)
                index[identity] = target.name
                atomic_write(index_path, (json.dumps(index, indent=2) + "\n").encode("utf-8"))
            finally:
                if lease is not None:
                    lease.unlock()
