"""Licensed bench probes (opt-in: CANOE17_MCP_PROBE=1 and CANOE17_MCP_BENCH=1).

These cover what the unlicensed demo PC cannot verify (api-evidence C3, M1):
save-copy with backup and persistence, ``on_dirty="save"`` before an open, and
measurement start/stop confirmed by events. They use only the sandbox copies.

Passing proves licensed real-COM behaviour (demo_verified on a licensed PC).
None of these probes needs or observes Vector hardware, so passing them on the
bench is still not bench_verified; that needs per-operation physical evidence.
Record the result in docs/com/api-evidence.md. Not yet run anywhere: written
for the first licensed run.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_com_live import ctx, error_code, result_as, run

from canoe17_mcp.com.backend import ComBackend
from canoe17_mcp.contracts import (
    BackendError,
    BackendSettings,
    DiagDescriptionInfo,
    ErrorCode,
    OpenResult,
    OpState,
    SaveResult,
)

pytestmark = [pytest.mark.canoe, pytest.mark.licensed]


@pytest.fixture(scope="module")
def licensed_backend(
    sandbox: Path, bench: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ComBackend]:
    b = ComBackend(BackendSettings(lock_key="bench-probe"), lock_dir=tmp_path_factory.mktemp("l"))
    try:
        yield b
    finally:
        b.shutdown()  # releases COM and the lock; never stops CANoe or a measurement


@pytest.fixture
def workdir(sandbox: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    """A fresh copy of UDSBasic per probe, so saves never touch the shared copy.

    Never deleted here: CANoe may still hold the files open. Clean
    ``<sandbox>/bench-work`` by hand after the run.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = sandbox / "bench-work" / f"{request.node.name}-{stamp}" / "UDSBasic"
    shutil.copytree(sandbox / "UDSBasic", target)
    yield target


def _open(b: ComBackend, cfg: Path, sandbox: Path) -> OpenResult:
    if not b.status().connected:
        run(b, b.connect())  # attach only, so the dirty state below is real
    status = b.status()
    on_dirty = "refuse"
    if status.configuration_modified:
        current = Path(status.configuration_path or "")
        if not current.is_relative_to(sandbox):
            pytest.fail(f"CANoe has unsaved changes in {current}; not touching them")
        on_dirty = "discard"  # left over from an earlier probe on a sandbox copy
    return result_as(run(b, b.open_config(str(cfg), on_dirty, True, ctx(b))), OpenResult)


def _dirty(b: ComBackend, workdir: Path) -> None:
    extra = workdir / "Cdd" / "bench-extra.cdd"
    shutil.copy2(workdir / "Cdd" / "UDS-ExampleEcu-6.0.1.cdd", extra)
    result_as(
        run(b, b.add_diag_description("CAN", str(extra), None, False, ctx(b))),
        DiagDescriptionInfo,
    )
    assert b.status().configuration_modified is True


def test_save_copy_backs_up_and_persists(
    licensed_backend: ComBackend, workdir: Path, sandbox: Path
):
    b = licensed_backend
    cfg = workdir / "UDSBasic.cfg"
    copy = workdir / "UDSBasic_bench_copy.cfg"
    copy.write_text("placeholder to force a backup", encoding="ascii")
    _open(b, cfg, sandbox)
    assert b.status().licensed is not False
    _dirty(b, workdir)

    saved = result_as(run(b, b.save_config(str(copy), ctx(b))), SaveResult)
    assert saved.active_changed and saved.active_path.lower() == str(copy).lower()
    assert saved.backup_path is not None
    assert Path(saved.backup_path).read_text(encoding="ascii") == "placeholder to force a backup"
    assert saved.epoch_after == b.status().epoch
    assert b.status().configuration_modified is False

    # Persistence: reopen the original, then the copy; the added description must be there.
    _open(b, cfg, sandbox)
    assert [d.qualifier for d in b.diag_descriptions().value] == ["Door"]
    _open(b, copy, sandbox)
    assert [d.qualifier for d in b.diag_descriptions().value] == ["Door", "Door_1"]


def test_on_dirty_save_saves_with_backup_before_opening(
    licensed_backend: ComBackend, workdir: Path, sandbox: Path
):
    b = licensed_backend
    cfg = workdir / "UDSBasic.cfg"
    _open(b, cfg, sandbox)
    _dirty(b, workdir)
    easy = sandbox / "Easy" / "Easy.cfg"
    opened = result_as(run(b, b.open_config(str(easy), "save", False, ctx(b))), OpenResult)
    assert opened.saved_previous_to is not None
    assert opened.saved_previous_to.lower() == str(cfg).lower()
    assert opened.backup_path is not None and Path(opened.backup_path).is_file()
    _open(b, cfg, sandbox)
    assert "Door_1" in [d.qualifier for d in b.diag_descriptions().value]


def ensure_measurement_stopped(b: ComBackend) -> None:
    """Bounded cleanup: make sure no measurement is left running on the bench.

    Does nothing only when CANoe reports the measurement explicitly stopped
    (``measurement_running is False``); unknown counts as possibly running.
    Otherwise one stop is attempted; if that cannot be confirmed, the test fails
    loudly so the operator stops it by hand.
    """
    if b.status().measurement_running is False:
        return
    outcome: object
    try:
        outcome = b.wait(b.measurement_stop(ctx(b)).operation_id, 60).value
    except BackendError as exc:  # e.g. degraded: refused before queuing
        outcome = exc
    if b.status().measurement_running is not False:
        pytest.fail(
            f"CLEANUP FAILED: measurement may still be running in CANoe; stop it by hand. "
            f"Last stop attempt: {outcome!r}"
        )


def test_measurement_start_stop_confirmed_by_events(
    licensed_backend: ComBackend, workdir: Path, sandbox: Path
):
    b = licensed_backend
    _open(b, workdir / "UDSBasic.cfg", sandbox)
    confirmed_stopped = False
    try:
        started = run(b, b.measurement_start(ctx(b)), wait=60)
        assert started.state is OpState.COMPLETED, started.error
        assert b.status().measurement_running is True
        # Configuration-level actions are refused while measuring (C6).
        blocked = run(b, b.remove_diag_description("diag:Door", ctx(b)))
        assert error_code(blocked) is ErrorCode.MEASUREMENT_RUNNING and not blocked.dispatched
        stopped = run(b, b.measurement_stop(ctx(b)), wait=60)
        assert stopped.state is OpState.COMPLETED, stopped.error
        assert b.status().measurement_running is False
        confirmed_stopped = True
    finally:
        # Cleanup depends on a confirmed stop, not on a stop having been attempted
        # (review R1): a failed stop or a measurement still running gets one more
        # bounded stop, or a loud CLEANUP FAILED.
        if not confirmed_stopped:
            ensure_measurement_stopped(b)
