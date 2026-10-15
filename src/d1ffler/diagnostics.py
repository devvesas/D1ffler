"""Bounded local incident records without source text, exception messages, or locals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import threading
import traceback
from uuid import uuid4

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR

from d1ffler.app_version import APP_VERSION
from d1ffler.user_state import atomic_write, locked_state
from d1ffler.local_data import safe_local


@dataclass(frozen=True)
class DiagnosticFailure:
    operation: str
    incident_id: str
    error_type: str
    logged: bool

    @property
    def summary(self) -> str:
        message = f"Unexpected {self.error_type}; diagnostic {self.incident_id}."
        if not self.logged:
            message += " The local diagnostic log could not be written."
        return message


class DiagnosticStore:
    MAX_BYTES = 256 * 1024
    BACKUPS = 2

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "diagnostics.jsonl"
        self._mutex = threading.Lock()

    def _paths(self):
        return [self.path, *(self.directory / f"diagnostics.jsonl.{index}"
                             for index in range(1, self.BACKUPS + 1))]

    def record_exception(self, operation: str, exception: BaseException, tb=None) -> DiagnosticFailure:
        """Never raise from an exception boundary, including when the log is unwritable."""
        failure = DiagnosticFailure(operation, uuid4().hex[:12], type(exception).__name__, False)
        try:
            chain, seen = [], set()
            current = exception
            while current is not None and id(current) not in seen and len(chain) < 8:
                seen.add(id(current))
                frames = [{"module_file": Path(frame.f_code.co_filename).name[:180],
                           "function": frame.f_code.co_name[:180], "line": line}
                          for frame, line in traceback.walk_tb(tb if not chain and tb is not None
                                                              else current.__traceback__)]
                chain.append({"exception_type": type(current).__name__[:100], "frames": frames[-48:]})
                current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
            record = {"schema_version": 1, "incident_id": failure.incident_id,
                      "time": datetime.now(timezone.utc).isoformat(), "app_version": APP_VERSION,
                      "python_version": platform.python_version(), "platform": sys.platform,
                      "os_version": platform.version(), "qt_version": QT_VERSION_STR,
                      "pyqt_version": PYQT_VERSION_STR,
                      "frozen": bool(getattr(sys, "frozen", False)),
                      "operation": operation[:100], "exception_chain": chain}
            payload = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
            while len(payload) > self.MAX_BYTES:
                if not any(part["frames"] for part in chain):
                    return failure
                for part in chain:
                    count = len(part["frames"]) // 2
                    part["frames"] = part["frames"][-count:] if count else []
                record["traceback_truncated"] = True
                payload = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
            with self._mutex:
                self.directory.mkdir(parents=True, exist_ok=True)
                with locked_state(self.directory / "diagnostics.lock"):
                    paths = self._paths()
                    if self.path.exists() and self.path.stat().st_size + len(payload) > self.MAX_BYTES:
                        paths[-1].unlink(missing_ok=True)
                        for source, target in reversed(list(zip(paths[:-1], paths[1:]))):
                            if source.exists():
                                source.replace(target)
                    with self.path.open("ab") as stream:
                        stream.write(payload)
                        stream.flush()
            return DiagnosticFailure(failure.operation, failure.incident_id, failure.error_type, True)
        except BaseException:
            return failure

    def read_events(self) -> list[dict]:
        records = []
        with self._mutex:
            self.directory.mkdir(parents=True, exist_ok=True)
            with locked_state(self.directory / "diagnostics.lock"):
                for path in reversed(self._paths()):
                    if not path.exists():
                        continue
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            continue  # A terminated process may leave an incomplete last record.
        return records

    def export(self, destination: Path, *, shared_context: dict | None = None) -> None:
        report = {"schema_version": 1, "app_version": APP_VERSION, "events": self.read_events()}
        if shared_context is not None:
            report["shared_context"] = shared_context
        atomic_write(destination, json.dumps(report, indent=2, ensure_ascii=True).encode("utf-8"))

    def clear(self) -> None:
        """Remove only the owned log files, serialized with recording and rotation."""
        with self._mutex:
            self.directory.mkdir(parents=True, exist_ok=True)
            safe_local(self.directory.parent, self.directory / "diagnostics.lock")
            with locked_state(self.directory / "diagnostics.lock"):
                for path in self._paths():
                    safe_local(self.directory.parent, path)
                    path.unlink(missing_ok=True)


class ExceptionHooks:
    """Install GUI/thread exception reporting; dispatch UI work on the Qt thread."""

    def __init__(self, app, store: DiagnosticStore, window):
        from PyQt6.QtCore import QObject, pyqtSignal

        class Bridge(QObject):
            incident = pyqtSignal(object, object)

        self.store, self.window = store, window
        self.bridge = Bridge(app)
        self.bridge.incident.connect(self._dispatch)
        self.previous_gui = sys.excepthook
        self.previous_thread = threading.excepthook
        self._handling = False
        sys.excepthook = self.gui_exception
        threading.excepthook = self.thread_exception

    def _owner(self, tb):
        views = self.window.comparisons
        owner = None
        for frame, _line in traceback.walk_tb(tb):
            candidate = frame.f_locals.get("self")
            if candidate in views or (getattr(candidate, "comparison_host", None) is self.window
                                      and getattr(candidate, "_closed", False)):
                owner = candidate
            elif getattr(candidate, "app", None) in views:
                owner = candidate.app
        return owner

    def gui_exception(self, exception_type, exception, tb):
        failure = self.store.record_exception("GUI action", exception, tb)
        try:
            owner = self._owner(tb)
            self.bridge.incident.emit(failure, owner)
        except BaseException as exc:
            self.store.record_exception("GUI exception notification", exc)

    def thread_exception(self, arguments):
        failure = self.store.record_exception("Background thread", arguments.exc_value, arguments.exc_traceback)
        try:
            self.bridge.incident.emit(failure, None)
        except BaseException as exc:
            self.store.record_exception("Thread exception notification", exc)

    def _dispatch(self, failure, owner):
        if self._handling:
            return
        self._handling = True
        try:
            self.window.report_gui_failure(failure, owner)
        except BaseException as exc:
            self.store.record_exception("GUI exception notification", exc)
        finally:
            self._handling = False

    def restore(self):
        if sys.excepthook == self.gui_exception:
            sys.excepthook = self.previous_gui
        if threading.excepthook == self.thread_exception:
            threading.excepthook = self.previous_thread
