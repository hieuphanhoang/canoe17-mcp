"""Regression tests for Codex's review of the COM slice (AGENT-005, C1-C7).

They drive the real worker, state store and ComBackend job bodies against a
plain-object stand-in for the CANoe application, so no CANoe is needed. The
stand-in only records calls; it makes no claim about real CANoe behaviour.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from canoe17_mcp.com.backend import ComBackend, _Watcher
from canoe17_mcp.com.state import StateStore
from canoe17_mcp.com.worker import StaWorker
from canoe17_mcp.contracts import (
    BackendSettings,
    BlockReason,
    CallContext,
    EffectRequest,
    ErrorCode,
    OpenResult,
    OperationStatus,
    OpState,
    SaveResult,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows COM apartment")


class FakeApp:
    """Records the calls the backend makes; never a CANoe model."""

    def __init__(self, cfg_path: Path, *, modified: bool, running: bool) -> None:
        self.calls: list[str] = []
        app = self

        class Cfg:
            FullName = str(cfg_path)
            Modified = modified

            def Save(self, *args: Any) -> None:  # noqa: N802 - COM name
                app.calls.append("Save" + (f"({args[0]})" if args else "()"))

        self.Configuration = Cfg()
        self.Measurement = SimpleNamespace(Running=running)
        self.Version = SimpleNamespace(major=17, minor=6, Build=5)
        self.FullName = "fake-canoe.exe"

    on_open: Any = None
    """Set by attach(): emits App.OnOpen the way CANoe does after Open()."""

    def Open(self, path: str, *_: Any) -> None:  # noqa: N802
        self.calls.append(f"Open({path})")
        if self.on_open is not None:
            self.on_open(path)
        self.Configuration.FullName = path
        self.Configuration.Modified = False

    def Quit(self) -> None:  # noqa: N802
        self.calls.append("Quit")


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from canoe17_mcp.com import backend as backend_module

    monkeypatch.setattr(backend_module, "OPEN_SETTLE_S", 0.05)
    b = ComBackend(
        BackendSettings(lock_key=f"review-{time.monotonic_ns()}"), lock_dir=tmp_path / "lock"
    )
    yield b
    b.session.app = None  # nothing to detach from
    b.shutdown()


def attach(b: ComBackend, app: FakeApp) -> CallContext:
    app.on_open = lambda path: b.session.inbox.push("App.OnOpen", path)
    b.session.app = app
    b.store.update_session(connected=True)
    return CallContext(expected_epoch=b.store.epoch)


def finished(b: ComBackend, op: OperationStatus) -> OperationStatus:
    done = b.wait(op.operation_id, 5).value
    assert done.state in (OpState.COMPLETED, OpState.FAILED, OpState.CANCELLED), done
    return done


def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "Demo.cfg"
    path.write_text("original", encoding="utf-8")
    return path


# C1 -------------------------------------------------------------------------


def test_read_is_labelled_with_the_epoch_it_was_read_in(backend: ComBackend, tmp_path: Path):
    attach(backend, FakeApp(cfg_file(tmp_path), modified=False, running=False))
    before = backend.store.epoch

    def read(epoch: int) -> tuple[str, int]:
        backend.store.bump_epoch()  # the session moves on right after the read
        return ("data", epoch)

    observed = backend._read("probe", read)
    assert observed.value == ("data", before)
    assert observed.epoch == before != backend.store.epoch
    assert backend.store.get_cache("probe") is None  # not cached under the new epoch


# C2 / C3 --------------------------------------------------------------------


def test_expired_job_is_cancelled_when_claimed_not_only_by_watchdog():
    w = StaWorker(StateStore())  # not started: we drive _execute ourselves
    ran: list[bool] = []
    op = w.submit_operation(
        "compile", lambda step: (step.dispatch(), ran.append(True)), expected_epoch=0,
        step_s=1, dispatch_timeout_s=10,
    )
    job = w._normal.popleft()
    job.dispatch_deadline = time.monotonic() - 1
    w._execute(job)
    done = w.store.get(op.operation_id)
    assert done is not None and done.state is OpState.CANCELLED and not done.dispatched
    assert done.error is not None and done.error.code is ErrorCode.DEADLINE_EXCEEDED
    assert not ran


def test_job_queued_before_degraded_is_refused_at_start():
    w = StaWorker(StateStore())
    ran: list[bool] = []
    queued = w.submit_operation(
        "compile", lambda step: (step.dispatch(), ran.append(True)), expected_epoch=0,
        step_s=1, dispatch_timeout_s=10,
    )
    other = w.store.new_operation("measurement.start", 0)
    assert w.store.try_start(other.operation_id)
    w.store.mark_dispatched(other.operation_id)
    assert w.store.mark_unknown(other.operation_id, _err())
    assert w.store.degraded
    w._execute(w._normal.popleft())
    done = w.store.get(queued.operation_id)
    assert done is not None and done.state is OpState.CANCELLED and not done.dispatched
    assert done.error is not None and done.error.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert not ran


def _err():
    from canoe17_mcp.contracts import ErrorInfo

    return ErrorInfo(ErrorCode.OUTCOME_UNKNOWN, "test")


# C4 -------------------------------------------------------------------------


def test_new_session_event_does_not_complete_old_watcher(backend: ComBackend):
    op = backend.store.new_operation("measurement.start", backend.store.epoch)
    backend.store.try_start(op.operation_id)
    backend.store.mark_dispatched(op.operation_id)
    backend._watchers[op.operation_id] = _Watcher(
        op.operation_id, "measurement.start", time.monotonic() + 60
    )
    backend._bump_epoch()  # e.g. OnOpen of another configuration
    backend._seen.add("start")  # an OnStart of the new session
    backend._on_loop()
    done = backend.store.get(op.operation_id)
    assert done is not None and done.state is OpState.FAILED
    assert done.error is not None and done.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert done.effects_possible
    assert op.operation_id not in backend._watchers
    assert not backend.store.degraded


def test_watcher_ignores_event_from_other_epoch(backend: ComBackend):
    op = backend.store.new_operation("measurement.start", backend.store.epoch + 5)
    backend.store.try_start(op.operation_id)
    w = _Watcher(op.operation_id, "measurement.start", time.monotonic() + 60)
    backend._seen.add("start")
    backend._advance(w, time.monotonic())
    current = backend.store.get(op.operation_id)
    assert current is not None and current.state is OpState.RUNNING


# C5 -------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["save_current", "open_save", "quit_save"])
def test_every_overwrite_makes_a_backup_first(backend: ComBackend, tmp_path: Path, route: str):
    path = cfg_file(tmp_path)
    app = FakeApp(path, modified=True, running=False)
    ctx = attach(backend, app)
    if route == "save_current":
        op = backend.save_config(None, ctx)
    elif route == "open_save":
        op = backend.open_config(str(tmp_path / "Other.cfg"), "save", False, ctx)
    else:
        op = backend.quit("save", ctx)
    done = finished(backend, op)
    assert done.state is OpState.COMPLETED, done.error
    backups = list(tmp_path.glob("Demo.cfg.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "original"
    assert "Save()" in app.calls
    result = done.result
    backup = getattr(result, "backup_path", None)
    assert backup == str(backups[0])
    if route == "quit_save":
        assert isinstance(result, SaveResult)


def test_failed_backup_saves_nothing(backend: ComBackend, tmp_path: Path):
    path = tmp_path / "Dir.cfg"
    path.mkdir()  # copy2 of a directory fails
    app = FakeApp(path, modified=True, running=False)
    done = finished(backend, backend.save_config(None, attach(backend, app)))
    assert done.state is OpState.FAILED and not done.dispatched
    assert app.calls == []


def test_backup_names_do_not_collide(backend: ComBackend, tmp_path: Path):
    path = cfg_file(tmp_path)
    app = FakeApp(path, modified=True, running=False)
    for _ in range(3):
        done = finished(backend, backend.save_config(None, attach(backend, app)))
        assert done.state is OpState.COMPLETED, done.error
    assert len(list(tmp_path.glob("Demo.cfg.bak-*"))) == 3


# C6 -------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["open_save", "quit_discard", "save_current"])
def test_running_measurement_refused_before_any_side_effect(
    backend: ComBackend, tmp_path: Path, route: str
):
    app = FakeApp(cfg_file(tmp_path), modified=True, running=True)
    ctx = attach(backend, app)
    if route == "open_save":
        op = backend.open_config(str(tmp_path / "Other.cfg"), "save", False, ctx)
    elif route == "quit_discard":
        op = backend.quit("discard", ctx)
    else:
        op = backend.save_config(None, ctx)
    done = finished(backend, op)
    assert done.state is OpState.FAILED and not done.dispatched
    assert done.error is not None and done.error.code is ErrorCode.MEASUREMENT_RUNNING
    assert app.calls == []
    assert not list(tmp_path.glob("*.bak-*"))


# C7 -------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["open_config", "quit"])
def test_dirty_save_preview_shows_licence_block(backend: ComBackend, action: str):
    backend.store.update_session(
        connected=True, configuration_modified=True, licensed=False,
        configuration_path="C:/x/Demo.cfg", measurement_running=False,
    )
    pv = backend.preview(EffectRequest(action, (("on_dirty", "save"), ("path", "C:/x/O.cfg"))))
    assert pv.value.blocked_by is BlockReason.NO_LICENSE
    assert pv.value.overwrites == ("C:/x/Demo.cfg",)
    discard = backend.preview(EffectRequest(action, (("on_dirty", "discard"),)))
    assert discard.value.blocked_by is None and discard.value.discards_changes


# C9 -------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["open_config", "quit", "save_config"])
@pytest.mark.parametrize("licensed", [True, False])
def test_preview_blocks_while_measuring(backend: ComBackend, action: str, licensed: bool):
    backend.store.update_session(
        connected=True, licensed=licensed, measurement_running=True,
        configuration_modified=True, configuration_path="C:/demo/a.cfg",
    )
    pv = backend.preview(EffectRequest(action, (("on_dirty", "save"),)))
    assert pv.value.blocked_by is BlockReason.MEASUREMENT_RUNNING


# R1: every OnOpen invalidates; open completes after events settle -------------


def test_gui_reopen_of_same_file_bumps_epoch(backend: ComBackend, tmp_path: Path):
    path = cfg_file(tmp_path)
    ctx = attach(backend, FakeApp(path, modified=False, running=False))
    opened = finished(backend, backend.open_config(str(path), "refuse", False, ctx))
    result = opened.result
    assert isinstance(result, OpenResult)
    epoch = result.epoch_after
    assert epoch == backend.store.epoch
    # An operator reopens the same file in the GUI: same names survive, but every
    # earlier preview/ID authorisation must become stale.
    backend.session.inbox.push("App.OnOpen", str(path))
    deadline = time.monotonic() + 2
    while backend.store.epoch == epoch:
        assert time.monotonic() < deadline, "GUI reopen did not bump the epoch"
        time.sleep(0.01)
    stale = finished(backend, backend.compile(CallContext(expected_epoch=epoch)))
    assert stale.state is OpState.FAILED
    assert stale.error is not None and stale.error.code is ErrorCode.STALE_SESSION


def test_open_reports_epoch_after_duplicate_on_open(backend: ComBackend, tmp_path: Path):
    path = cfg_file(tmp_path)
    app = FakeApp(path, modified=False, running=False)
    ctx = attach(backend, app)
    first = app.on_open

    def twice(p: str) -> None:  # CANoe sometimes reports one Open twice (C6)
        first(p)
        first(p)

    app.on_open = twice
    done = finished(backend, backend.open_config(str(path), "refuse", False, ctx))
    result = done.result
    assert isinstance(result, OpenResult)
    assert result.epoch_after == backend.store.epoch  # usable right away


# R2: remove the exact duplicate --------------------------------------------------


def test_remove_database_removes_the_requested_duplicate(
    backend: ComBackend, monkeypatch: pytest.MonkeyPatch
):
    from canoe17_mcp.com import backend as backend_module
    from canoe17_mcp.com import session as session_module

    monkeypatch.setattr(backend_module, "late", lambda o: o)
    monkeypatch.setattr(session_module, "late", lambda o: o)
    removed: list[int] = []

    class Coll:
        def __init__(self, entries: list[Any]) -> None:
            self.entries = entries

        @property
        def Count(self) -> int:  # noqa: N802
            return len(self.entries)

        def Item(self, i: int) -> Any:  # noqa: N802
            return self.entries[i - 1]

        def Remove(self, i: int) -> None:  # noqa: N802
            removed.append(i)

    db = SimpleNamespace(Name="X", FullName="C:/db/x.dbc", Channel=1)
    bus = SimpleNamespace(Name="CAN", Databases=Coll([db, SimpleNamespace(**vars(db))]))
    app = SimpleNamespace(
        Configuration=SimpleNamespace(
            FullName="C:/a.cfg", Modified=False, SimulationSetup=SimpleNamespace(Buses=Coll([bus]))
        ),
        Measurement=SimpleNamespace(Running=False),
    )
    backend.session.app = app
    backend.store.update_session(connected=True)
    ids = [d.id for d in backend.databases().value]
    assert ids == ["db:X@1", "db:X@2"]
    done = finished(
        backend,
        backend.remove_database("db:X@2", CallContext(expected_epoch=backend.store.epoch)),
    )
    assert done.state is OpState.COMPLETED, done.error
    assert removed == [2]


# R3 ------------------------------------------------------------------------------


def test_node_set_active_preview_warns_about_untracked_change(backend: ComBackend):
    backend.store.update_session(connected=True, configuration_path="C:/a.cfg")
    pv = backend.preview(EffectRequest("node.set_active", (("node_id", "node:A"),)))
    assert any("not mark this change as unsaved" in n for n in pv.value.notes)


def test_open_preview_launches_only_without_a_canoe_process(
    backend: ComBackend, monkeypatch: pytest.MonkeyPatch
):
    from canoe17_mcp.com import backend as backend_module

    request = EffectRequest("open_config", (("launch_if_absent", True), ("path", "C:/a.cfg")))
    monkeypatch.setattr(backend_module, "canoe_processes", lambda: [(1, "CANoeTBE.exe")])
    assert backend.preview(request).value.launches_canoe is False
    monkeypatch.setattr(backend_module, "canoe_processes", lambda: [])
    assert backend.preview(request).value.launches_canoe is True


# R4: an open awaiting OnOpen must never be orphaned ------------------------------


def _pending_open(backend: ComBackend, *, settled: bool) -> str:
    from canoe17_mcp.com.backend import _OpenWatcher

    op = backend.store.new_operation("open_config", backend.store.epoch)
    backend.store.try_start(op.operation_id)
    backend.store.mark_dispatched(op.operation_id, "awaiting_on_open")
    backend._watchers[op.operation_id] = _OpenWatcher(
        op.operation_id,
        time.monotonic() + 60,
        True,
        False,
        None,
        None,
        settle_until=time.monotonic() - 1 if settled else None,
    )
    return op.operation_id


@pytest.mark.parametrize("event", ["App.OnQuit", "connection_lost"])
def test_quit_or_lost_connection_resolves_pending_open(
    backend: ComBackend, tmp_path: Path, event: str
):
    attach(backend, FakeApp(cfg_file(tmp_path), modified=False, running=False))
    op_id = _pending_open(backend, settled=False)
    if event == "App.OnQuit":
        backend._handle_event("App.OnQuit", ())
    else:
        backend._disconnect_on_worker()
        backend._bump_epoch()
    done = backend.store.get(op_id)
    assert done is not None and done.state is OpState.FAILED
    assert done.error is not None and done.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert done.effects_possible and op_id not in backend._watchers
    assert not backend.store.degraded


def test_failing_final_read_resolves_pending_open(backend: ComBackend, tmp_path: Path):
    app = FakeApp(cfg_file(tmp_path), modified=False, running=False)
    attach(backend, app)
    op_id = _pending_open(backend, settled=True)

    class Broken:
        @property
        def Configuration(self) -> Any:  # noqa: N802
            raise RuntimeError("COM call failed")

        Version = app.Version
        FullName = app.FullName

    backend.session.app = Broken()
    backend._on_loop()
    done = backend.store.get(op_id)
    assert done is not None and done.state is OpState.FAILED
    assert done.error is not None and done.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert op_id not in backend._watchers
    backend.session.app = None


def test_expected_open_bump_keeps_pending_open(backend: ComBackend, tmp_path: Path):
    attach(backend, FakeApp(cfg_file(tmp_path), modified=False, running=False))
    op_id = _pending_open(backend, settled=False)
    backend._handle_event("App.OnOpen", (str(tmp_path / "Demo.cfg"),))
    current = backend.store.get(op_id)
    assert current is not None and current.state is OpState.RUNNING
    assert op_id in backend._watchers
