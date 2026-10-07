"""Thread-safe state store shared by the STA worker and caller threads.

Callers never touch COM. Everything they read - session status, operation
status, cached snapshots - comes from here, so it answers while the worker
is blocked inside a long COM call. All operation state transitions go
through this store under one lock, which is what makes "cancel before
dispatch" and "start" mutually exclusive.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from canoe17_mcp.contracts import (
    OP_TRANSITIONS,
    TERMINAL_STATES,
    ErrorInfo,
    Observed,
    OperationKind,
    OperationResult,
    OperationStatus,
    OpState,
    SessionStatus,
    new_operation_id,
)

MAX_FINISHED_OPERATIONS = 500


def wall_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._ops: OrderedDict[str, OperationStatus] = OrderedDict()
        # session
        self.epoch = 0
        self.connected = False
        self.canoe_version: str | None = None
        self.canoe_exe: str | None = None
        self.configuration_path: str | None = None
        self.configuration_modified: bool | None = None
        self.measurement_running: bool | None = None
        self.licensed: bool | None = None
        self.lock_held = False
        self.lock_holder_pid: int | None = None
        self._session_time = time.monotonic()
        # worker
        self.busy_with: str | None = None
        self.busy_deadline: float | None = None
        # cached read snapshots: name -> (epoch, monotonic time, value)
        self._cache: dict[str, tuple[int, float, Any]] = {}

    # ------------------------------------------------------------------ session

    def update_session(self, **fields: Any) -> None:
        with self._lock:
            for name, value in fields.items():
                if not hasattr(self, name) or name.startswith("_"):
                    raise AttributeError(name)
                setattr(self, name, value)
            self._session_time = time.monotonic()
            self._changed.notify_all()

    def bump_epoch(self) -> int:
        """Called on connect, Application.OnOpen and OnQuit."""
        with self._lock:
            self.epoch += 1
            self._cache.clear()
            self._changed.notify_all()
            return self.epoch

    @property
    def degraded(self) -> bool:
        with self._lock:
            return any(op.state is OpState.OUTCOME_UNKNOWN for op in self._ops.values())

    def status(self, backend: str = "com") -> SessionStatus:
        with self._lock:
            return SessionStatus(
                connected=self.connected,
                canoe_version=self.canoe_version,
                canoe_exe=self.canoe_exe,
                configuration_path=self.configuration_path,
                configuration_modified=self.configuration_modified,
                measurement_running=self.measurement_running,
                epoch=self.epoch,
                lock_held=self.lock_held,
                lock_holder_pid=self.lock_holder_pid,
                degraded=self.degraded,
                busy_with=self.busy_with,
                snapshot_age_s=round(time.monotonic() - self._session_time, 3),
                backend="fake" if backend == "fake" else "com",
                licensed=self.licensed,
            )

    def observed[T](self, value: T, *, epoch: int | None = None, age_s: float = 0.0) -> Observed[T]:
        with self._lock:
            return Observed(
                value=value,
                epoch=self.epoch if epoch is None else epoch,
                snapshot_age_s=round(age_s, 3),
                busy_with=self.busy_with,
                degraded=self.degraded,
            )

    # -------------------------------------------------------------------- cache

    def put_cache(self, name: str, value: Any, epoch: int) -> None:
        with self._lock:
            if epoch == self.epoch:
                self._cache[name] = (epoch, time.monotonic(), value)

    def get_cache(self, name: str) -> tuple[int, float, Any] | None:
        """Return ``(epoch, age_s, value)`` for the current epoch, or None."""
        with self._lock:
            hit = self._cache.get(name)
            if hit is None or hit[0] != self.epoch:
                return None
            return hit[0], time.monotonic() - hit[1], hit[2]

    # --------------------------------------------------------------- operations

    def new_operation(self, kind: OperationKind, epoch: int) -> OperationStatus:
        op = OperationStatus(
            operation_id=new_operation_id(),
            kind=kind,
            state=OpState.QUEUED,
            requested_wall=wall_now(),
            epoch=epoch,
        )
        with self._lock:
            self._ops[op.operation_id] = op
            self._trim()
            self._changed.notify_all()
        return op

    def get(self, operation_id: str) -> OperationStatus | None:
        with self._lock:
            return self._ops.get(operation_id)

    def _set(self, op: OperationStatus, **changes: Any) -> OperationStatus:
        new_state = changes.get("state", op.state)
        if new_state is not op.state and new_state not in OP_TRANSITIONS[op.state]:
            raise RuntimeError(f"illegal transition {op.state} -> {new_state} for {op.kind}")
        updated = dataclasses.replace(op, **changes)
        self._ops[op.operation_id] = updated
        self._changed.notify_all()
        return updated

    def try_start(self, operation_id: str) -> bool:
        """Atomically QUEUED -> RUNNING. False if it was cancelled meanwhile."""
        with self._lock:
            op = self._ops.get(operation_id)
            if op is None or op.state is not OpState.QUEUED:
                return False
            self._set(op, state=OpState.RUNNING, started_wall=wall_now())
            return True

    def cancel_queued(self, operation_id: str, error: ErrorInfo | None) -> bool:
        """Atomically QUEUED -> CANCELLED (never dispatched). False if already started."""
        with self._lock:
            op = self._ops.get(operation_id)
            if op is None or op.state is not OpState.QUEUED:
                return False
            self._set(op, state=OpState.CANCELLED, finished_wall=wall_now(), error=error)
            return True

    def mark_dispatched(self, operation_id: str, phase: str | None = None) -> None:
        with self._lock:
            op = self._ops[operation_id]
            changes: dict[str, Any] = {"dispatched": True, "effects_possible": True}
            if phase is not None:
                changes["phase"] = phase
            self._set(op, **changes)

    def set_phase(self, operation_id: str, phase: str) -> None:
        with self._lock:
            self._set(self._ops[operation_id], phase=phase)

    def finish(
        self,
        operation_id: str,
        state: OpState,
        *,
        result: OperationResult = None,
        error: ErrorInfo | None = None,
    ) -> OperationStatus:
        if state not in TERMINAL_STATES:
            raise ValueError(f"finish() needs a terminal state, got {state}")
        with self._lock:
            op = self._ops[operation_id]
            late = op.state is OpState.OUTCOME_UNKNOWN
            return self._set(
                op,
                state=state,
                result=result,
                error=error,
                finished_wall=wall_now(),
                late_resolution=late or op.late_resolution,
            )

    def mark_unknown(self, operation_id: str, error: ErrorInfo) -> bool:
        """Step deadline passed while a dispatched call is in flight."""
        with self._lock:
            op = self._ops.get(operation_id)
            if op is None or not op.dispatched:
                return False
            if op.state not in (OpState.RUNNING, OpState.STOPPING):
                return False
            self._set(op, state=OpState.OUTCOME_UNKNOWN, error=error)
            return True

    def set_busy(self, operation_id: str | None, deadline: float | None) -> None:
        with self._lock:
            self.busy_with = operation_id
            self.busy_deadline = deadline
            self._changed.notify_all()

    def wait(self, operation_id: str, wait_s: float) -> OperationStatus | None:
        """Block the calling thread until the operation is terminal or
        outcome_unknown, or ``wait_s`` passes. Never touches the worker."""
        end = time.monotonic() + max(0.0, wait_s)
        with self._lock:
            while True:
                op = self._ops.get(operation_id)
                if op is None:
                    return None
                if op.state in TERMINAL_STATES or op.state is OpState.OUTCOME_UNKNOWN:
                    return op
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return op
                self._changed.wait(remaining)

    def _trim(self) -> None:
        finished = [k for k, v in self._ops.items() if v.state in TERMINAL_STATES]
        for key in finished[: max(0, len(finished) - MAX_FINISHED_OPERATIONS)]:
            del self._ops[key]
