import subprocess
import sys
from dataclasses import replace

import pytest

from canoe17_mcp import contracts as c
from canoe17_mcp.fake import FakeBackend, FakeConfiguration


def backend(*, licensed: bool = False) -> FakeBackend:
    fake = FakeBackend(
        configurations=(
            FakeConfiguration(
                "a.cfg",
                databases=(c.DatabaseInfo("db:A", "A", "a.dbc", 1, "CAN"),),
                nodes=(c.NodeInfo("node:N", "N", True, None),),
            ),
            FakeConfiguration("b.cfg"),
        ),
        licensed=licensed,
    )
    assert fake.wait(fake.connect().operation_id, 1).value.state == c.OpState.COMPLETED
    return fake


def finish(fake: FakeBackend, op: c.OperationStatus) -> c.OperationStatus:
    return fake.wait(op.operation_id, 1).value


def context(fake: FakeBackend) -> c.CallContext:
    return c.CallContext(fake.status().epoch)


def test_full_protocol_and_no_pywin32_import() -> None:
    fake: c.Backend = backend()
    assert isinstance(fake, c.Backend)
    assert fake.status().backend == "fake"
    assert all(cap.evidence == c.Evidence.FAKE for cap in fake.capabilities())
    # Fresh interpreter actively refuses imports, even if pywin32 is installed.
    program = """
import sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pythoncom', 'pywintypes', 'win32com', 'win32api'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Guard())
from canoe17_mcp.fake import FakeBackend
from canoe17_mcp.settings import Settings
from canoe17_mcp.safety import SafetyPolicy
assert FakeBackend().status().backend == 'fake'
assert not any(n.startswith(('win32', 'pythoncom', 'pywintypes')) for n in sys.modules)
"""
    subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True)


def test_connect_never_launches() -> None:
    fake = FakeBackend(process_running=False)
    result = finish(fake, fake.connect())
    assert result.error and result.error.code == c.ErrorCode.NO_ACTIVE_INSTANCE
    assert not result.dispatched
    assert not fake.status().connected


def test_open_dirty_refuses_then_discards() -> None:
    fake = backend()
    fake.simulate_edit(modified=True)
    refused = finish(fake, fake.open_config("b.cfg", "refuse", False, context(fake)))
    assert refused.error and refused.error.code == c.ErrorCode.DIRTY_CONFIG
    assert not refused.dispatched
    assert fake.status().configuration_path == "a.cfg"
    result = finish(fake, fake.open_config("b.cfg", "discard", False, context(fake)))
    assert isinstance(result.result, c.OpenResult) and result.result.discarded_changes
    assert fake.status().configuration_path == "b.cfg"
    assert not fake.status().configuration_modified


def test_bad_open_is_not_misattributed_to_dirty() -> None:
    fake = backend()
    fake.simulate_edit(modified=True)
    result = finish(fake, fake.open_config("missing.cfg", "discard", False, context(fake)))
    assert result.error and result.error.code == c.ErrorCode.CANOE_REJECTED
    assert result.dispatched
    assert fake.status().configuration_modified


@pytest.mark.parametrize(
    "method", ["save", "save_current", "measurement", "measurement_stop", "dirty_save", "quit_save"]
)
def test_licence_blocks_without_successful_effects(method: str) -> None:
    fake = backend()
    fake.simulate_edit(modified=True)
    op = {
        "save": lambda: fake.save_config("copy.cfg", context(fake)),
        "save_current": lambda: fake.save_config(None, context(fake)),
        "measurement": lambda: fake.measurement_start(context(fake)),
        "measurement_stop": lambda: fake.measurement_stop(context(fake)),
        "dirty_save": lambda: fake.open_config("b.cfg", "save", False, context(fake)),
        "quit_save": lambda: fake.quit("save", context(fake)),
    }[method]()
    result = finish(fake, op)
    assert result.error and result.error.code == c.ErrorCode.LICENSE_REQUIRED
    assert result.error.hresult == -2147352567
    assert result.state == c.OpState.FAILED
    assert fake.status().configuration_path == "a.cfg"
    assert fake.status().configuration_modified
    assert not fake.status().measurement_running


def test_unlicensed_compile_and_diag_edit_are_available() -> None:
    fake = backend()
    assert finish(fake, fake.compile(context(fake))).state == c.OpState.COMPLETED
    result = finish(fake, fake.add_diag_description("CAN", "Door.cdd", None, True, context(fake)))
    assert isinstance(result.result, c.DiagDescriptionInfo)
    assert result.result.mode == "tester"
    assert fake.status().configuration_modified
    assert (
        finish(fake, fake.diag_windows(result.result.id, "console", True, context(fake))).state
        == c.OpState.COMPLETED
    )
    duplicate = finish(
        fake, fake.add_diag_description("CAN", "Door.cdd", None, True, context(fake))
    )
    assert duplicate.error and duplicate.error.code == c.ErrorCode.ALREADY_EXISTS
    assert not duplicate.dispatched


def test_node_active_does_not_set_modified() -> None:
    fake = backend()
    assert (
        finish(fake, fake.set_node_active("node:N", False, context(fake))).state
        == c.OpState.COMPLETED
    )
    assert not fake.nodes().value[0].active
    assert not fake.status().configuration_modified


def test_fake_save_copy_switches_active_path_and_epoch() -> None:
    fake = backend(licensed=True)
    epoch = fake.status().epoch
    result = finish(fake, fake.save_config("copy.cfg", context(fake)))
    assert isinstance(result.result, c.SaveResult)
    assert result.result.active_path == "copy.cfg"
    assert result.result.epoch_after > epoch
    assert result.result.backup_path is None  # No filesystem IO in fake.


def test_gui_change_between_enqueue_and_dispatch_fails_closed() -> None:
    fake = backend()
    op = fake.set_database_channel("db:A", 2, context(fake))
    fake.simulate_gui_open("b.cfg")
    result = finish(fake, op)
    assert result.error and result.error.code == c.ErrorCode.STALE_SESSION
    assert not result.dispatched


def test_same_epoch_duplicate_reorder_invalidates_ids() -> None:
    fake = backend()
    first = c.DatabaseInfo("db:A@1", "A", "first.dbc", 1, "CAN")
    second = replace(first, id="db:A@2", path="second.dbc")
    fake.simulate_edit(databases=(first, second))
    fake.databases()
    epoch = fake.status().epoch
    fake.simulate_edit(databases=(second, first))
    result = finish(fake, fake.set_database_channel(first.id, 2, c.CallContext(epoch)))
    assert result.error and result.error.code == c.ErrorCode.STALE_SESSION
    assert not result.dispatched


def test_unknown_outcome_is_pollable_and_resolves_late() -> None:
    now = [0.0]
    fake = FakeBackend(configurations=(FakeConfiguration("a.cfg"),), clock=lambda: now[0])
    finish(fake, fake.connect())
    fake.hold_next_step()
    op = fake.compile(context(fake))
    assert finish(fake, op).state == c.OpState.RUNNING  # Caller wait is not RPC deadline.
    now[0] = 121
    snapshot = fake.operation(op.operation_id)
    assert snapshot.value.state == c.OpState.OUTCOME_UNKNOWN
    assert snapshot.degraded and snapshot.busy_with == op.operation_id
    assert fake.status().snapshot_age_s == 121
    assert fake.cancel(op.operation_id).state == c.OpState.OUTCOME_UNKNOWN
    with pytest.raises(c.BackendError, match="degraded"):
        fake.compile(context(fake))
    fake.resolve_held()
    result = fake.operation(op.operation_id).value
    assert result.state == c.OpState.COMPLETED and result.late_resolution
    assert not fake.status().degraded
    states = [state for token, state in fake.transitions if token == op.operation_id]
    assert states == [
        c.OpState.QUEUED,
        c.OpState.RUNNING,
        c.OpState.OUTCOME_UNKNOWN,
        c.OpState.COMPLETED,
    ]


def test_dispatch_timeout_cancels_before_effect_and_queue_cancel_is_final() -> None:
    now = [0.0]
    fake = FakeBackend(clock=lambda: now[0])
    op = fake.connect()
    now[0] = 11
    result = fake.operation(op.operation_id).value
    assert result.state == c.OpState.CANCELLED and not result.dispatched
    assert result.error and result.error.code == c.ErrorCode.BUSY
    fake.advance()
    assert not fake.status().connected
    op = fake.connect()
    fake.cancel(op.operation_id)
    fake.advance()
    assert not fake.status().connected


def test_held_old_session_cannot_mutate_replacement() -> None:
    fake = backend()
    fake.hold_next_step()
    op = fake.set_database_channel("db:A", 2, context(fake))
    fake.advance()
    fake.simulate_gui_open("b.cfg")
    fake.resolve_held()
    result = fake.operation(op.operation_id).value
    assert result.error and result.error.code == c.ErrorCode.STALE_SESSION
    assert fake.status().configuration_path == "b.cfg"


def test_extensions_remain_explicitly_unavailable() -> None:
    fake = backend()
    with pytest.raises(c.BackendError) as caught:
        fake.start_diag_request(c.DiagRequestSpec("CAN", "Door", raw=b"\x10\x01"), context(fake))
    assert caught.value.code == c.ErrorCode.CAPABILITY_UNAVAILABLE
    assert (
        next(cap for cap in fake.capabilities() if cap.operation == "diag_request").support
        == c.Support.NOT_IMPLEMENTED
    )


@pytest.mark.parametrize("name", ["Check@2", "x%40y", "a/b", "@", "%"])
def test_identifier_escaping(name: str) -> None:
    escaped = c.escape_id_segment(name)
    assert "@" not in escaped and "/" not in escaped
    assert c.unescape_id_segment(escaped) == name
