"""The single-threaded-apartment worker that owns every COM proxy.

A job is one COM step. The loop takes a job (control lane first), runs it,
pumps COM messages so events are delivered, lets watchers advance, and
repeats. Waiting never happens inside a job; long operations are watchers.

Timing rules (contract rule 2):

* A queued job that has not started by its dispatch deadline is cancelled
  undispatched (BUSY / DEADLINE_EXCEEDED). A watchdog thread does this, so
  it happens even while the worker is blocked in a COM call.
* A job calls ``step.dispatch()`` immediately before the COM call with side
  effects. If that call is still running at the step deadline, the watchdog
  marks the operation ``outcome_unknown``; when the call finally returns,
  the operation resolves with ``late_resolution=True``.
* The expected epoch is compared in the job, on the worker, before dispatch.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from canoe17_mcp.com.state import StateStore
from canoe17_mcp.contracts import (
    BackendError,
    ErrorCode,
    ErrorInfo,
    OperationKind,
    OperationResult,
    OperationStatus,
    OpState,
)

log = logging.getLogger(__name__)

Lane = Literal["control", "normal"]
GC_INTERVAL_S = 5.0


def _detached(exc: BackendError) -> BackendError:
    """Strip frames before an exception crosses to another thread: frames hold
    COM proxies, which must be released on the worker's apartment."""
    exc.__traceback__ = None
    exc.__context__ = None
    exc.__cause__ = None
    return exc


class _Pending:
    """Returned by a job whose operation a watcher will finish later."""

    def __repr__(self) -> str:
        return "PENDING"


PENDING: Any = _Pending()


class StepContext:
    """Handed to a job function on the worker thread."""

    def __init__(self, worker: StaWorker, operation_id: str | None) -> None:
        self.worker = worker
        self.operation_id = operation_id

    @property
    def store(self) -> StateStore:
        return self.worker.store

    def dispatch(self, phase: str | None = None) -> None:
        """Declare that the next COM call may have side effects."""
        if self.operation_id is not None:
            self.store.mark_dispatched(self.operation_id, phase)

    def phase(self, phase: str) -> None:
        if self.operation_id is not None:
            self.store.set_phase(self.operation_id, phase)


JobFn = Callable[[StepContext], Any]


@dataclass
class Job:
    fn: JobFn
    step_s: float
    dispatch_deadline: float
    lane: Lane = "normal"
    operation_id: str | None = None
    """None for read jobs, which are not operations."""
    expected_epoch: int | None = None
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


ComErrorHandler = Callable[[BaseException, str], ErrorInfo | None]
"""Turns a COM exception into ErrorInfo; returns None if it is not a COM error."""


class StaWorker:
    def __init__(
        self,
        store: StateStore,
        *,
        tick_s: float = 0.05,
        init_apartment: Callable[[], None] | None = None,
        uninit_apartment: Callable[[], None] | None = None,
        pump: Callable[[], None] | None = None,
        com_error: ComErrorHandler | None = None,
    ) -> None:
        self.store = store
        self.tick_s = tick_s
        self._init = init_apartment or _co_initialize
        self._uninit = uninit_apartment or _co_uninitialize
        self._pump = pump or _pump_messages
        self._com_error = com_error
        self._cv = threading.Condition()
        self._control: deque[Job] = deque()
        self._normal: deque[Job] = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._loop_hooks: list[Callable[[], None]] = []
        self._shutdown_hooks: list[Callable[[], None]] = []
        self._current: Job | None = None
        self._next_gc = 0.0
        self.thread_id: int | None = None

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(ready,), name="canoe-sta", daemon=True
        )
        self._thread.start()
        ready.wait(10)
        self._watchdog = threading.Thread(target=self._watch, name="canoe-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout_s)
        if self._watchdog is not None:
            self._watchdog.join(timeout_s)

    def add_loop_hook(self, hook: Callable[[], None]) -> None:
        """Run on the worker after every loop iteration (event drain, watchers)."""
        self._loop_hooks.append(hook)

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        """Run on the worker before the apartment is torn down."""
        self._shutdown_hooks.append(hook)

    @property
    def on_worker_thread(self) -> bool:
        return threading.get_ident() == self.thread_id

    # ---------------------------------------------------------------- submission

    def submit_operation(
        self,
        kind: OperationKind,
        fn: JobFn,
        *,
        expected_epoch: int | None,
        step_s: float,
        dispatch_timeout_s: float,
        lane: Lane = "normal",
        mutating: bool = True,
    ) -> OperationStatus:
        """Queue an operation and return its QUEUED status at once."""
        epoch = self.store.epoch if expected_epoch is None else expected_epoch
        op = self.store.new_operation(kind, epoch)
        if mutating and self.store.degraded:
            self.store.cancel_queued(
                op.operation_id,
                ErrorInfo(
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                    "Backend is degraded: an earlier operation has an unknown outcome. "
                    "Poll it until it resolves, then re-read the state before retrying.",
                    details=(("blocked_by", "degraded"),),
                ),
            )
            return self.store.get(op.operation_id) or op
        job = Job(
            fn=fn,
            step_s=step_s,
            dispatch_deadline=time.monotonic() + dispatch_timeout_s,
            lane=lane,
            operation_id=op.operation_id,
            expected_epoch=expected_epoch,
        )
        self._enqueue(job)
        return op

    def call(self, fn: JobFn, *, wait_s: float, step_s: float) -> Any:
        """Run a read job on the worker and wait for its value.

        Raises ``BackendError(BUSY)`` if it could not start within ``wait_s``
        (the job is then dropped, it never runs).
        """
        if self.on_worker_thread:
            return fn(StepContext(self, None))
        job = Job(fn=fn, step_s=step_s, dispatch_deadline=time.monotonic() + wait_s)
        self._enqueue(job)
        if not job.done.wait(wait_s + step_s):
            raise BackendError(ErrorCode.BUSY, "CANoe worker is busy; read not completed.")
        if job.error is not None:
            raise job.error
        return job.value

    def cancel_queued(self, operation_id: str, error: ErrorInfo) -> bool:
        """Cancel a job that has not started. Atomic with respect to start."""
        if not self.store.cancel_queued(operation_id, error):
            return False
        with self._cv:
            for lane in (self._control, self._normal):
                for job in list(lane):
                    if job.operation_id == operation_id:
                        lane.remove(job)
        return True

    def _enqueue(self, job: Job) -> None:
        with self._cv:
            (self._control if job.lane == "control" else self._normal).append(job)
            self._cv.notify_all()

    # ---------------------------------------------------------------- the loop

    def _take(self) -> Job | None:
        with self._cv:
            if not self._control and not self._normal:
                self._cv.wait(self.tick_s)
            if self._control:
                return self._control.popleft()
            if self._normal:
                return self._normal.popleft()
            return None

    def _run(self, ready: threading.Event) -> None:
        self.thread_id = threading.get_ident()
        self._init()
        ready.set()
        try:
            while not self._stop.is_set():
                job = self._take()
                if job is not None:
                    self._execute(job)
                self._safe_pump()
                if job is not None or time.monotonic() >= self._next_gc:
                    # Collect cycles here, on the STA: a COM proxy released by the
                    # garbage collector on another thread fails (CO_E_NOTINITIALIZED).
                    gc.collect()
                    self._next_gc = time.monotonic() + GC_INTERVAL_S
                for hook in list(self._loop_hooks):
                    try:
                        hook()
                    except Exception:  # noqa: BLE001 - a hook must not kill the worker
                        log.exception("worker loop hook failed")
        finally:
            for hook in list(self._shutdown_hooks):
                try:
                    hook()
                except Exception:  # noqa: BLE001
                    log.exception("worker shutdown hook failed")
            self._fail_pending("Backend shut down before the job started.")
            self._uninit()

    def _safe_pump(self) -> None:
        try:
            self._pump()
        except Exception:  # noqa: BLE001
            log.exception("message pump failed")

    def _execute(self, job: Job) -> None:
        now = time.monotonic()
        op_id = job.operation_id
        if op_id is None:
            # read job
            if now > job.dispatch_deadline:
                job.error = BackendError(ErrorCode.BUSY, "CANoe worker was busy; read dropped.")
                job.done.set()
                return
            self._current = job
            self.store.set_busy(None, now + job.step_s)
            try:
                job.value = job.fn(StepContext(self, None))
            except BaseException as exc:  # noqa: BLE001
                job.error = _detached(self._as_backend_error(exc, "read"))
            finally:
                self._current = None
                self.store.set_busy(None, None)
                job.done.set()
            return

        if not self.store.try_start(op_id):
            return  # cancelled while queued
        op = self.store.get(op_id)
        assert op is not None
        if job.expected_epoch is not None and job.expected_epoch != self.store.epoch:
            self.store.finish(
                op_id,
                OpState.FAILED,
                error=ErrorInfo(
                    ErrorCode.STALE_SESSION,
                    f"The CANoe session changed (epoch {job.expected_epoch} -> "
                    f"{self.store.epoch}). Re-read with list/summary, then retry.",
                    retryable=True,
                ),
            )
            return
        self._current = job
        self.store.set_busy(op_id, time.monotonic() + job.step_s)
        try:
            result: OperationResult = job.fn(StepContext(self, op_id))
            current = self.store.get(op_id)
            if result is PENDING:
                pass  # a watcher finishes it
            elif current is not None and current.state in (
                OpState.RUNNING,
                OpState.STOPPING,
                OpState.OUTCOME_UNKNOWN,
            ):
                self.store.finish(op_id, OpState.COMPLETED, result=result)
        except BaseException as exc:  # noqa: BLE001
            err = self._as_backend_error(exc, op.kind)
            current = self.store.get(op_id)
            if current is not None and current.state not in (
                OpState.COMPLETED,
                OpState.FAILED,
                OpState.CANCELLED,
            ):
                self.store.finish(op_id, OpState.FAILED, error=err.info)
            del err  # break frame <-> traceback cycle holding COM proxies
        finally:
            self._current = None
            self.store.set_busy(None, None)

    def _as_backend_error(self, exc: BaseException, action: str) -> BackendError:
        if isinstance(exc, BackendError):
            return exc
        if self._com_error is not None:
            info = self._com_error(exc, action)
            if info is not None:
                return BackendError.from_info(info)
        log.exception("unexpected error in %s", action, exc_info=exc)
        return BackendError(ErrorCode.INTERNAL, f"{action}: {exc!r}")

    def _fail_pending(self, message: str) -> None:
        with self._cv:
            pending = list(self._control) + list(self._normal)
            self._control.clear()
            self._normal.clear()
        for job in pending:
            if job.operation_id is not None:
                self.store.cancel_queued(
                    job.operation_id, ErrorInfo(ErrorCode.NOT_CONNECTED, message)
                )
            else:
                job.error = BackendError(ErrorCode.NOT_CONNECTED, message)
                job.done.set()

    # ---------------------------------------------------------------- watchdog

    def _watch(self) -> None:
        while not self._stop.wait(self.tick_s):
            now = time.monotonic()
            # 1. queued jobs past their dispatch deadline: cancel undispatched
            with self._cv:
                expired = [
                    j for lane in (self._control, self._normal) for j in lane
                    if now > j.dispatch_deadline
                ]
            for job in expired:
                if job.operation_id is None:
                    with self._cv:
                        for lane in (self._control, self._normal):
                            if job in lane:
                                lane.remove(job)
                    job.error = BackendError(ErrorCode.BUSY, "CANoe worker was busy; read dropped.")
                    job.done.set()
                    continue
                code = ErrorCode.BUSY if self.store.busy_with else ErrorCode.DEADLINE_EXCEEDED
                self.cancel_queued(
                    job.operation_id,
                    ErrorInfo(
                        code,
                        "Not dispatched: the CANoe worker stayed busy past the dispatch "
                        "deadline. Nothing was sent to CANoe.",
                        retryable=True,
                    ),
                )
            # 2. in-flight step past its deadline: outcome unknown
            job = self._current
            deadline = self.store.busy_deadline
            if job is not None and job.operation_id and deadline is not None and now > deadline:
                if self.store.mark_unknown(
                    job.operation_id,
                    ErrorInfo(
                        ErrorCode.OUTCOME_UNKNOWN,
                        "The COM call did not return within its step deadline. CANoe may "
                        "or may not have applied it. Poll this operation; when it "
                        "resolves, re-read the state before retrying.",
                    ),
                ):
                    log.warning("operation %s outcome unknown", job.operation_id)


# ------------------------------------------------------------------ pywin32 glue


def _co_initialize() -> None:
    import pythoncom

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)


def _co_uninitialize() -> None:
    import pythoncom

    pythoncom.CoUninitialize()


def _pump_messages() -> None:
    import pythoncom

    pythoncom.PumpWaitingMessages()
