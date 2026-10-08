"""Live probes of ComBackend against CANoe 17 on sample copies (opt-in).

Each probe records evidence for docs/com/api-evidence.md. Probes that need a
CANoe licence assert the licence error when CANoe reports one, so the same
test documents both situations instead of being skipped.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from canoe17_mcp.com.backend import ComBackend
from canoe17_mcp.contracts import (
    BackendError,
    BackendSettings,
    CallContext,
    CompileResult,
    DiagDescriptionInfo,
    DirtyPolicy,
    EffectRequest,
    ErrorCode,
    OpenResult,
    OperationStatus,
    OpState,
)

pytestmark = pytest.mark.canoe

WAIT = 240.0


@pytest.fixture(scope="module")
def backend(sandbox: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ComBackend]:
    settings = BackendSettings(lock_key="live-probe")
    b = ComBackend(settings, lock_dir=tmp_path_factory.mktemp("lock"))
    yield b
    b.shutdown()


def run(b: ComBackend, op: OperationStatus, wait: float = WAIT) -> OperationStatus:
    done = b.wait(op.operation_id, wait).value
    assert done.state is not OpState.OUTCOME_UNKNOWN, done
    return done


def result_as[T](done: OperationStatus, kind: type[T]) -> T:
    assert done.state is OpState.COMPLETED, done.error
    assert isinstance(done.result, kind), done.result
    return done.result


def error_code(done: OperationStatus) -> ErrorCode:
    assert done.error is not None, done
    return done.error.code


def ctx(b: ComBackend) -> CallContext:
    return CallContext(expected_epoch=b.status().epoch)


def preview_ctx(b: ComBackend, action: str, **params: str) -> CallContext:
    pv = b.preview(EffectRequest(action, tuple(params.items())))
    return CallContext(expected_epoch=pv.epoch)


def test_open_inspect_compile(backend: ComBackend, sandbox: Path):
    cfg = str(sandbox / "UDSBasic" / "UDSBasic.cfg")
    on_dirty: DirtyPolicy = "refuse"
    if run(backend, backend.connect()).state is OpState.COMPLETED:
        st = backend.status()
        if st.configuration_modified:
            current = Path(st.configuration_path or "")
            if not current.is_relative_to(sandbox):
                pytest.fail(f"CANoe has unsaved changes in {current}; not touching them")
            on_dirty = "discard"  # left over from an earlier probe on a sandbox copy
    op = backend.open_config(
        cfg, on_dirty, True, preview_ctx(backend, "open_config", path=cfg)
    )
    opened = result_as(run(backend, op), OpenResult)
    assert opened.active_path.lower() == cfg.lower()
    st = backend.status()
    assert st.connected and st.lock_held and st.epoch == opened.epoch_after

    diags = backend.diag_descriptions()
    assert diags.epoch == st.epoch
    door = [d for d in diags.value if d.qualifier == "Door"]
    assert door and door[0].id == "diag:Door" and door[0].mode == "ecu_simulation"

    setup = backend.test_setup().value
    assert any(t.id == "tm-sim:Test 3" and not t.startable for t in setup.simulation_test_nodes)

    summary = backend.summary("all").value
    assert summary.configuration_path.lower() == cfg.lower()
    assert {n.name for n in summary.nodes} >= {"Tester", "SimDiagECU"}

    comp = result_as(run(backend, backend.compile(ctx(backend))), CompileResult)
    assert comp.success


def test_diag_windows_and_duplicate_refused(backend: ComBackend, sandbox: Path):
    opened = run(backend, backend.diag_windows("diag:Door", "console", True, ctx(backend)))
    assert opened.state is OpState.COMPLETED, opened.error
    closed = run(backend, backend.diag_windows("diag:Door", "all", False, ctx(backend)))
    assert closed.state is OpState.COMPLETED, closed.error
    door = backend.diag_descriptions().value[0]
    dup = run(
        backend,
        backend.add_diag_description(door.network, door.file_path, None, False, ctx(backend)),
    )
    assert dup.state is OpState.FAILED and error_code(dup) is ErrorCode.ALREADY_EXISTS
    assert not dup.dispatched


def test_stale_epoch_is_refused(backend: ComBackend, sandbox: Path):
    old = ctx(backend)
    cfg = str(sandbox / "UDSBasic" / "UDSBasic.cfg")
    reopened = run(backend, backend.open_config(cfg, "refuse", False, old))
    assert reopened.state is OpState.COMPLETED
    stale = run(backend, backend.compile(old))
    assert stale.state is OpState.FAILED and error_code(stale) is ErrorCode.STALE_SESSION
    assert not stale.dispatched


def test_dirty_refuse_then_discard(backend: ComBackend, sandbox: Path):
    src = sandbox / "UDSBasic" / "Cdd" / "UDS-ExampleEcu-6.0.1.cdd"
    extra = sandbox / "UDSBasic" / "Cdd" / "probe-extra.cdd"
    shutil.copy2(src, extra)
    added = result_as(
        run(backend, backend.add_diag_description("CAN", str(extra), None, False, ctx(backend))),
        DiagDescriptionInfo,
    )
    # A different file with a taken ECU qualifier is renamed by CANoe (api-evidence D6)
    assert added.qualifier == "Door_1"
    ids = [d.id for d in backend.diag_descriptions().value]
    assert ids == ["diag:Door", "diag:Door_1"]
    assert backend.status().configuration_modified is True

    easy = str(sandbox / "Easy" / "Easy.cfg")
    refused = run(backend, backend.open_config(easy, "refuse", False, ctx(backend)))
    assert refused.state is OpState.FAILED and error_code(refused) is ErrorCode.DIRTY_CONFIG
    assert not refused.dispatched

    discarded = result_as(
        run(backend, backend.open_config(easy, "discard", False, ctx(backend))), OpenResult
    )
    assert discarded.discarded_changes
    assert backend.status().configuration_modified is False


def test_licensed_operations_report_licence(backend: ComBackend, sandbox: Path):
    cfg = str(sandbox / "UDSBasic" / "UDSBasic.cfg")
    run(backend, backend.open_config(cfg, "refuse", False, ctx(backend)))
    start = run(backend, backend.measurement_start(ctx(backend)), wait=60)
    if start.state is OpState.FAILED and error_code(start) is ErrorCode.LICENSE_REQUIRED:
        assert backend.status().licensed is False
        avail = {a.operation: a for a in backend.availability().value}
        assert avail["measurement.start"].blocked_by is not None
        pytest.xfail("CANoe has no application licence on this PC (api-evidence M1)")
    assert start.state is OpState.COMPLETED, start.error
    stop = run(backend, backend.measurement_stop(ctx(backend)), wait=60)
    assert stop.state is OpState.COMPLETED, stop.error


def test_licence_free_config_edits(backend: ComBackend, sandbox: Path):
    """Node, database and test-setup edits on the UDSBasic copy; all discarded at the end."""
    from canoe17_mcp.contracts import (
        CanControllerInfo,
        DatabaseInfo,
        NodeInfo,
        Removed,
        TestEnvironmentInfo,
        TestModuleInfo,
    )

    udsbasic = sandbox / "UDSBasic"
    dbc = sandbox / "Easy" / "CANdb" / "easy.dbc"
    tse = udsbasic / "ProbeTestSetup.tse"
    if not dbc.is_file() or not tse.is_file():
        pytest.skip("needs Easy/CANdb/easy.dbc and UDSBasic/ProbeTestSetup.tse in the sandbox")
    cfg = str(udsbasic / "UDSBasic.cfg")
    on_dirty: DirtyPolicy = "discard" if backend.status().configuration_modified else "refuse"
    result_as(run(backend, backend.open_config(cfg, on_dirty, False, ctx(backend))), OpenResult)

    ctl = backend.can_controller("CAN", 1).value
    assert isinstance(ctl, CanControllerInfo) and ctl.bitrate_bps == 500000
    assert backend.buses().value[0].channels == (1,)

    capl = udsbasic / "Nodes" / "ProbeNode.can"
    capl.write_text("variables {}\n", encoding="ascii")
    node = result_as(
        run(backend, backend.add_node("ProbeNode", "CAN", str(capl), ctx(backend))), NodeInfo
    )
    assert node.id == "node:ProbeNode" and node.capl_path == str(capl)
    off = result_as(run(backend, backend.set_node_active(node.id, False, ctx(backend))), NodeInfo)
    assert off.active is False
    only_bus = run(backend, backend.attach_node_bus(node.id, "CAN", False, ctx(backend)))
    assert error_code(only_bus) is ErrorCode.INVALID_ARGUMENT and not only_bus.dispatched
    removed = result_as(run(backend, backend.remove_node(node.id, ctx(backend))), Removed)
    assert removed.id == node.id
    assert all(n.name != "ProbeNode" for n in backend.nodes().value)

    bad = run(backend, backend.add_database(str(dbc), "CAN", 2, ctx(backend)))
    assert bad.state is OpState.FAILED and error_code(bad) is ErrorCode.INVALID_ARGUMENT
    assert not bad.dispatched
    db = result_as(
        run(backend, backend.add_database(str(dbc), "CAN", 1, ctx(backend))), DatabaseInfo
    )
    assert db.id == "db:easy" and db.channel == 1 and db.bus == "CAN"
    dup = run(backend, backend.add_database(str(dbc), "CAN", 1, ctx(backend)))
    assert error_code(dup) is ErrorCode.ALREADY_EXISTS
    result_as(run(backend, backend.remove_database(db.id, ctx(backend))), Removed)
    assert backend.databases().value == ()

    env = result_as(
        run(backend, backend.add_test_environment(str(tse), ctx(backend))), TestEnvironmentInfo
    )
    assert env.path == str(tse) and len(env.modules) == 2
    module = result_as(
        run(
            backend,
            backend.add_test_module(
                env.id, str(udsbasic / "Tester" / "TestModule.can"), ctx(backend)
            ),
        ),
        TestModuleInfo,
    )
    assert module.id == f"{'tm:' + env.id[len('env:'):]}/Test 4" and module.enabled is True
    disabled = result_as(
        run(backend, backend.set_test_module_enabled(module.id, False, ctx(backend))),
        TestModuleInfo,
    )
    assert disabled.enabled is False

    missing = run(backend, backend.add_test_environment(str(udsbasic / "nope.tse"), ctx(backend)))
    assert error_code(missing) is ErrorCode.NOT_FOUND and not missing.dispatched

    with pytest.raises(BackendError) as no_bus:
        backend.add_bus("ProbeBus", "CAN", ctx(backend))
    assert no_bus.value.code is ErrorCode.CAPABILITY_UNAVAILABLE

    assert backend.status().configuration_modified is True
    reset = result_as(
        run(backend, backend.open_config(cfg, "discard", False, ctx(backend))), OpenResult
    )
    assert reset.discarded_changes
    capl.unlink(missing_ok=True)


def test_write_window_read_and_clear(backend: ComBackend, sandbox: Path):
    from canoe17_mcp.contracts import WriteWindowText

    cfg = str(sandbox / "UDSBasic" / "UDSBasic.cfg")
    on_dirty: DirtyPolicy = "discard" if backend.status().configuration_modified else "refuse"
    result_as(run(backend, backend.open_config(cfg, on_dirty, False, ctx(backend))), OpenResult)
    text = backend.write_window(4000).value
    assert isinstance(text, WriteWindowText)
    cleared = run(backend, backend.clear_write_window(ctx(backend)))
    assert cleared.state is OpState.COMPLETED, cleared.error
    after = backend.write_window(4000).value
    assert after.text == "" and not after.truncated


def test_read_attaches_and_keeps_unsaved_work(
    backend: ComBackend, sandbox: Path, tmp_path_factory: pytest.TempPathFactory
):
    """Attach-on-read (S1): a fresh backend's first read attaches to the running
    CANoe and sees the operator's unsaved change; nothing is opened or discarded."""
    from canoe17_mcp.contracts import DiagDescriptionInfo

    udsbasic = sandbox / "UDSBasic"
    cfg = str(udsbasic / "UDSBasic.cfg")
    on_dirty: DirtyPolicy = "discard" if backend.status().configuration_modified else "refuse"
    result_as(run(backend, backend.open_config(cfg, on_dirty, False, ctx(backend))), OpenResult)
    extra = udsbasic / "Cdd" / "attach-extra.cdd"
    shutil.copy2(udsbasic / "Cdd" / "UDS-ExampleEcu-6.0.1.cdd", extra)
    result_as(
        run(backend, backend.add_diag_description("CAN", str(extra), None, False, ctx(backend))),
        DiagDescriptionInfo,
    )
    backend.shutdown()  # releases the lock; CANoe keeps the unsaved change

    fresh = ComBackend(
        BackendSettings(lock_key="live-probe-attach"),
        lock_dir=tmp_path_factory.mktemp("lock2"),
    )
    try:
        assert fresh.status().connected is False  # status alone never attaches
        summary = fresh.summary("all")
        st = fresh.status()
        assert st.connected and st.configuration_modified is True
        assert summary.epoch == st.epoch
        assert summary.value.configuration_path.lower() == cfg.lower()
        assert [d.id for d in summary.value.diag_descriptions] == ["diag:Door", "diag:Door_1"]
        reset = result_as(
            run(fresh, fresh.open_config(cfg, "discard", False, ctx(fresh))), OpenResult
        )
        assert reset.discarded_changes
    finally:
        fresh.shutdown()
        extra.unlink(missing_ok=True)
