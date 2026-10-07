"""STA worker, state store and session lock against the real Windows COM
runtime and kernel objects. No CANoe needed, so these are not marked canoe."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from canoe17_mcp.com.lock import SessionLock, _kernel32
from canoe17_mcp.com.state import StateStore
from canoe17_mcp.com.worker import StaWorker
from canoe17_mcp.contracts import BackendError, ErrorCode, OpState

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows COM/kernel objects")


@pytest.fixture
def worker():
    store = StateStore()
    w = StaWorker(store, tick_s=0.01)
    w.start()
    yield w
    w.stop()


def test_operation_completes_and_wait_returns(worker):
    op = worker.submit_operation(
        "compile", lambda step: (step.dispatch(), 42)[1], expected_epoch=0,
        step_s=1, dispatch_timeout_s=1,
    )
    done = worker.store.wait(op.operation_id, 2)
    assert done.state is OpState.COMPLETED
    assert done.result == 42
    assert done.dispatched and not done.late_resolution


def test_stale_epoch_fails_without_dispatch(worker):
    worker.store.bump_epoch()
    op = worker.submit_operation(
        "compile", lambda step: step.dispatch(), expected_epoch=0,
        step_s=1, dispatch_timeout_s=1,
    )
    done = worker.store.wait(op.operation_id, 2)
    assert done.state is OpState.FAILED
    assert done.error.code is ErrorCode.STALE_SESSION
    assert not done.dispatched


def test_queued_job_cancelled_undispatched_when_worker_stays_busy(worker):
    release = threading.Event()
    blocker = worker.submit_operation(
        "open_config", lambda step: (step.dispatch(), release.wait(5))[1],
        expected_epoch=0, step_s=10, dispatch_timeout_s=1,
    )
    ran = threading.Event()
    queued = worker.submit_operation(
        "compile", lambda step: ran.set(), expected_epoch=0, step_s=1, dispatch_timeout_s=0.2,
    )
    done = worker.store.wait(queued.operation_id, 2)
    assert done.state is OpState.CANCELLED
    assert done.error.code is ErrorCode.BUSY
    assert not done.dispatched
    release.set()
    assert worker.store.wait(blocker.operation_id, 2).state is OpState.COMPLETED
    time.sleep(0.1)
    assert not ran.is_set(), "a cancelled job must never run"


def test_step_deadline_gives_outcome_unknown_then_late_resolution(worker):
    release = threading.Event()
    op = worker.submit_operation(
        "database.set_channel", lambda step: (step.dispatch(), release.wait(5), "ok")[2],
        expected_epoch=0, step_s=0.2, dispatch_timeout_s=1,
    )
    unknown = worker.store.wait(op.operation_id, 2)
    assert unknown.state is OpState.OUTCOME_UNKNOWN
    assert unknown.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert worker.store.degraded
    # status answers from the store while the worker is blocked
    st = worker.store.status()
    assert st.busy_with == op.operation_id and st.degraded
    # new mutations are refused while degraded, without dispatch
    refused = worker.submit_operation(
        "compile", lambda step: None, expected_epoch=0, step_s=1, dispatch_timeout_s=1
    )
    refused = worker.store.get(refused.operation_id)
    assert refused.state is OpState.CANCELLED
    assert refused.error.code is ErrorCode.CAPABILITY_UNAVAILABLE
    release.set()
    deadline = time.monotonic() + 2
    while worker.store.get(op.operation_id).state is OpState.OUTCOME_UNKNOWN:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    final = worker.store.get(op.operation_id)
    assert final.state is OpState.COMPLETED and final.late_resolution
    assert final.result == "ok"
    assert not worker.store.degraded


def test_undispatched_slow_step_is_not_outcome_unknown(worker):
    release = threading.Event()
    op = worker.submit_operation(
        "compile", lambda step: release.wait(5), expected_epoch=0,
        step_s=0.1, dispatch_timeout_s=1,
    )
    time.sleep(0.4)
    assert worker.store.get(op.operation_id).state is OpState.RUNNING
    release.set()
    assert worker.store.wait(op.operation_id, 2).state is OpState.COMPLETED


def test_backend_error_in_job_fails_operation(worker):
    def fn(step):
        step.dispatch()
        raise BackendError(ErrorCode.LICENSE_REQUIRED, "needs licence")

    op = worker.submit_operation(
        "save_config", fn, expected_epoch=0, step_s=1, dispatch_timeout_s=1
    )
    done = worker.store.wait(op.operation_id, 2)
    assert done.state is OpState.FAILED and done.error.code is ErrorCode.LICENSE_REQUIRED


def test_control_lane_runs_before_normal_lane(worker):
    order: list[str] = []
    release = threading.Event()
    worker.submit_operation(
        "open_config", lambda step: release.wait(5), expected_epoch=0, step_s=10,
        dispatch_timeout_s=1,
    )
    time.sleep(0.05)
    a = worker.submit_operation(
        "compile", lambda step: order.append("normal"), expected_epoch=0, step_s=1,
        dispatch_timeout_s=5,
    )
    b = worker.submit_operation(
        "measurement.stop", lambda step: order.append("control"), expected_epoch=0, step_s=1,
        dispatch_timeout_s=5, lane="control",
    )
    release.set()
    worker.store.wait(a.operation_id, 2)
    worker.store.wait(b.operation_id, 2)
    assert order == ["control", "normal"]


def test_read_call_runs_on_worker_thread(worker):
    tid = worker.call(lambda step: threading.get_ident(), wait_s=1, step_s=1)
    assert tid == worker.thread_id != threading.get_ident()


def test_lock_excludes_second_process(tmp_path):
    key = f"test-{time.monotonic_ns()}"
    lock = SessionLock(key, sidecar_dir=tmp_path)
    assert lock.acquire()
    assert lock.holder_pid() is not None
    code = (
        "import sys; from pathlib import Path;"
        "from canoe17_mcp.com.lock import SessionLock;"
        f"l = SessionLock({key!r}, sidecar_dir=Path({str(tmp_path)!r}));"
        "sys.exit(0 if l.acquire(0.2) else 3)"
    )
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert other.returncode == 3, other.stderr
    lock.release()
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert other.returncode == 0, other.stderr


def test_lock_abandoned_by_dead_process_is_reacquired(tmp_path):
    key = f"test-{time.monotonic_ns()}"
    code = (
        "import os; from pathlib import Path;"
        "from canoe17_mcp.com.lock import SessionLock;"
        f"l = SessionLock({key!r}, sidecar_dir=Path({str(tmp_path)!r}));"
        "assert l.acquire(); os._exit(0)"
    )
    # Hold a handle (without ownership) so the mutex object outlives the child;
    # otherwise acquire() would just create a fresh mutex, not exercise WAIT_ABANDONED.
    k = _kernel32()
    keeper = k.CreateMutexW(None, False, SessionLock(key).name)
    assert keeper
    try:
        subprocess.run([sys.executable, "-c", code], check=True)
        lock = SessionLock(key, sidecar_dir=tmp_path)
        assert lock.acquire(1.0)
        assert lock.abandoned_previous
        lock.release()
    finally:
        k.CloseHandle(keeper)


def test_lock_rejects_bad_key():
    with pytest.raises(ValueError):
        SessionLock("bad\\key")
