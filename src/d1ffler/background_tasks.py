"""Thread-compatible tasks with consumed futures and validated terminal messages."""

from concurrent.futures import Future, wait
import queue
import threading

from d1ffler.diagnostics import DiagnosticStore


class MissingTerminalResult(RuntimeError):
    pass


class BackgroundTask:
    def __init__(self, executor, worker, on_queued=None, *, diagnostics=None, operation=None):
        self.executor, self.worker = executor, worker
        self.cancel_event = getattr(worker, "cancel_event", getattr(worker, "cancelled", None))
        self.on_queued = on_queued
        self.diagnostics = diagnostics
        self.operation = operation or type(worker).__name__
        self.future = None
        self.failure = None
        self._failure_lock = threading.Lock()
        self._terminal = []
        self._delivered = False

    def _fail(self, exception):
        with self._failure_lock:
            if self.failure is None:
                store = self.diagnostics
                if store is None:
                    from d1ffler.app_settings import user_state_directory
                    store = DiagnosticStore(user_state_directory() / "diagnostics")
                self.failure = store.record_exception(self.operation, exception)

    def _run(self):
        try:
            self.worker.run()
        except BaseException as exc:
            self._fail(exc)

    def _consume(self, future):
        if not future.cancelled():
            exception = future.exception()
            if exception is not None:
                self._fail(exception)

    def start(self):
        try:
            if self.executor is None:
                self.future = Future()

                def run():
                    if self.future.set_running_or_notify_cancel():
                        try:
                            self._run()
                        except BaseException as exc:
                            self.future.set_exception(exc)
                        else:
                            self.future.set_result(None)

                self.future.add_done_callback(self._consume)
                threading.Thread(target=run, daemon=True).start()
            else:
                self.future = self.executor.submit(self._run)
                self.future.add_done_callback(self._consume)
                if not self.future.running() and not self.future.done() and self.on_queued is not None:
                    self.on_queued()
        except BaseException as exc:
            self._fail(exc)
            if self.future is None:
                self.future = Future()
            if not self.future.done() and not self.future.running():
                self.future.set_result(None)

    def is_alive(self):
        return self.future is not None and not self.future.done()

    def join(self):
        if self.future is not None:
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.future.cancel()
            if self.future.done():
                self._consume(self.future)
                return
            wait([self.future])
            self._consume(self.future)

    def read_messages(self, messages, limit=4, *, terminal=None):
        if self.future is not None and self.cancel_event is not None and self.cancel_event.is_set():
            self.future.cancel()  # Cancel a queued job without waiting for a running scan.
        terminal = terminal or (lambda message: message[0] in {"finished", "error"})
        result = []
        for _index in range(limit):
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break
            if terminal(message):
                self._terminal.append(message)
            else:
                result.append(message)
        # A worker may queue a success and then raise. Hold terminal results until
        # its function has actually returned, and give failures precedence.
        if not self.is_alive() and messages.empty() and not self._delivered:
            self.join()
            if self.future is not None and self.future.cancelled():
                result.append(("cancelled", None))
            elif self.failure is not None:
                result.append(("failed", self.failure))
            elif self._terminal:
                result.extend(self._terminal)
            else:
                try:
                    raise MissingTerminalResult()
                except MissingTerminalResult as exc:
                    self._fail(exc)
                result.append(("failed", self.failure))
            self._terminal.clear()
            self._delivered = True
        return result


def job_messages(task, messages, limit=4, *, terminal=None):
    if isinstance(task, BackgroundTask):
        return task.read_messages(messages, limit, terminal=terminal)
    # Compatibility with thread-shaped test doubles and callers outside the app.
    result = []
    for _index in range(limit):
        try:
            result.append(messages.get_nowait())
        except queue.Empty:
            break
    return result
