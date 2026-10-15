"""Atomic writes and process locks shared by local user-state stores."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from PyQt6.QtCore import QLockFile


def state_lock(path: Path) -> QLockFile:
    lock = QLockFile(str(path))
    # An operation's age must never expire a live process's lock.
    lock.setStaleLockTime(0)
    return lock


@contextmanager
def locked_state(path: Path, timeout: int = 1000):
    lock = state_lock(path)
    if not lock.tryLock(timeout):
        raise OSError(f"Could not lock {path}. Another D1ffler instance may be using it; retry when it finishes.")
    try:
        yield
    finally:
        lock.unlock()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def cleanup_temporaries(path: Path) -> None:
    """Remove orphan writes only while the caller owns the target's process lock."""
    for temporary in path.parent.iterdir():
        if temporary.name.startswith(f".{path.name}-") and temporary.name.endswith(".tmp"):
            try:
                temporary.unlink()
            except OSError:
                pass
