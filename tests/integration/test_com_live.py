"""Live probes of ComBackend against CANoe 17 on sample copies (opt-in).

Each probe records evidence for docs/com/api-evidence.md. Probes that need a
CANoe licence assert the licence error when CANoe reports one, so the same
test documents both situations instead of being skipped.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from canoe17_mcp.com.backend import ComBackend
from canoe17_mcp.contracts import (
    BackendSettings,
    CallContext,
    EffectRequest,
    ErrorCode,
    OpState,
)

pytestmark = pytest.mark.canoe

WAIT = 240.0


@pytest.fixture(scope="module")
def backend(sandbox: Path, tmp_path_factory: pytest.TempPathFactory):
    settings = BackendSettings(lock_key="live-probe")
    b = ComBackend(settings, lock_dir=tmp_path_factory.mktemp("lock"))
    yield b
    b.shutdown()


def run(b: ComBackend, op, wait: float = WAIT):
    done = b.wait(op.operation_id, wait).value
    assert done.state is not OpState.OUTCOME_UNKNOWN, done
    return done


def ctx(b: ComBackend) -> CallContext:
    return CallContext(expected_epoch=b.status().epoch)


def preview_ctx(b: ComBackend, action: str, **params) -> CallContext:
    pv = b.preview(EffectRequest(action, tuple(params.items())))
    return CallContext(expected_epoch=pv.epoch)


def test_open_inspect_compile(backend: ComBackend, sandbox: Path):
    cfg = str(sandbox / "UDSBasic" / "UDSBasic.cfg")
    on_dirty = "refuse"
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
    done = run(backend, op)
    assert done.state is OpState.COMPLETED, done.error
    assert done.result.active_path.lower() == cfg.lower()
    st = backend.status()
    assert st.connected and st.lock_held and st.epoch == done.result.epoch_after

    diags = backend.diag_descriptions()
    assert diags.epoch == st.epoch
    door = [d for d in diags.value if d.qualifier == "Door"]
    assert door and door[0].id == "diag:Door" and door[0].mode == "ecu_simulation"

    setup = backend.test_setup().value
    assert any(t.id == "tm-sim:Test 3" and not t.startable for t in setup.simulation_test_nodes)

    summary = backend.summary("all").value
    assert summary.configuration_path.lower() == cfg.lower()
    assert {n.name for n in summary.nodes} >= {"Tester", "SimDiagECU"}

    comp = run(backend, backend.compile(ctx(backend)))
    assert comp.state is OpState.COMPLETED and comp.result.success


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
    assert dup.state is OpState.FAILED and dup.error.code is ErrorCode.ALREADY_EXISTS
    assert not dup.dispatched


def test_stale_epoch_is_refused(backend: ComBackend, sandbox: Path):
    old = ctx(backend)
    cfg = str(sandbox / "UDSBasic" / "UDSBasic.cfg")
    reopened = run(backend, backend.open_config(cfg, "refuse", False, old))
    assert reopened.state is OpState.COMPLETED
    stale = run(backend, backend.compile(old))
    assert stale.state is OpState.FAILED and stale.error.code is ErrorCode.STALE_SESSION
    assert not stale.dispatched


def test_dirty_refuse_then_discard(backend: ComBackend, sandbox: Path):
    src = sandbox / "UDSBasic" / "Cdd" / "UDS-ExampleEcu-6.0.1.cdd"
    extra = sandbox / "UDSBasic" / "Cdd" / "probe-extra.cdd"
    shutil.copy2(src, extra)
    added = run(backend, backend.add_diag_description("CAN", str(extra), None, False, ctx(backend)))
    assert added.state is OpState.COMPLETED, added.error
    # A different file with a taken ECU qualifier is renamed by CANoe (api-evidence D6)
    assert added.result.qualifier == "Door_1"
    ids = [d.id for d in backend.diag_descriptions().value]
    assert ids == ["diag:Door", "diag:Door_1"]
    assert backend.status().configuration_modified is True

    easy = str(sandbox / "Easy" / "Easy.cfg")
    refused = run(backend, backend.open_config(easy, "refuse", False, ctx(backend)))
    assert refused.state is OpState.FAILED and refused.error.code is ErrorCode.DIRTY_CONFIG
    assert not refused.dispatched

    discarded = run(backend, backend.open_config(easy, "discard", False, ctx(backend)))
    assert discarded.state is OpState.COMPLETED, discarded.error
    assert discarded.result.discarded_changes
    assert backend.status().configuration_modified is False


def test_licensed_operations_report_licence(backend: ComBackend, sandbox: Path):
    cfg = str(sandbox / "UDSBasic" / "UDSBasic.cfg")
    run(backend, backend.open_config(cfg, "refuse", False, ctx(backend)))
    start = run(backend, backend.measurement_start(ctx(backend)), wait=60)
    if start.state is OpState.FAILED and start.error.code is ErrorCode.LICENSE_REQUIRED:
        assert backend.status().licensed is False
        avail = {a.operation: a for a in backend.availability().value}
        assert avail["measurement.start"].blocked_by is not None
        pytest.xfail("CANoe has no application licence on this PC (api-evidence M1)")
    assert start.state is OpState.COMPLETED, start.error
    stop = run(backend, backend.measurement_stop(ctx(backend)), wait=60)
    assert stop.state is OpState.COMPLETED, stop.error
