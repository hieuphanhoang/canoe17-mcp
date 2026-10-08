from __future__ import annotations

import builtins
import json
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import anyio
import pytest

from canoe17_mcp import contracts as c
from canoe17_mcp.fake import FakeBackend, FakeConfiguration
from canoe17_mcp.server import ToolService, main, make_backend
from canoe17_mcp.settings import Settings


@pytest.fixture
def service(tmp_path: Path) -> ToolService:
    settings = Settings(
        read_only=False, allowed_roots=(tmp_path,), backend_kind="fake",
        audit_path=tmp_path / "audit.jsonl",
    )
    backend = FakeBackend(
        configurations=(
            FakeConfiguration(
                str(tmp_path / "demo.cfg"),
                databases=(
                    c.DatabaseInfo("db:Demo", "Demo", str(tmp_path / "demo.dbc"), 1, "CAN"),
                ),
            ),
        ),
        licensed=True,
    )
    backend.wait(backend.connect().operation_id, 1)
    return ToolService(backend, settings)


def test_fake_discovery_narrows_actions_and_never_registers_catalogue_blindly(
    service: ToolService,
) -> None:
    definitions = {tool.name: tool for tool in service.definitions}
    assert set(definitions) == {
        "canoe_status",
        "canoe_get_config_summary",
        "canoe_open_config",
        "canoe_save_config",
        "canoe_quit",
        "canoe_compile",
        "canoe_measurement",
        "canoe_operation",
        "canoe_database",
        "canoe_diag_description",
        "canoe_write_window",
        "canoe_node",
        "canoe_test_setup",
        "canoe_can_controller",
    }
    database = definitions["canoe_database"].inputSchema
    assert database["properties"]["action"]["enum"] == ["list", "add", "remove", "set_channel"]
    assert "path" in database["properties"]
    for definition in definitions.values():
        assert not {"oneOf", "anyOf", "allOf"} & definition.inputSchema.keys()
    with pytest.raises(c.BackendError) as exc:
        service.invoke("canoe_bus", {"action": "remove", "bus_id": "bus:CAN"})
    assert exc.value.code == c.ErrorCode.CAPABILITY_UNAVAILABLE


def test_real_floor_never_accepts_fake_evidence(service: ToolService) -> None:
    filtered = ToolService(service.backend, replace(service.settings, backend_kind="com"))
    assert set(filtered.actions) == {"canoe_status", "canoe_measurement", "canoe_operation"}
    assert set(filtered.actions["canoe_measurement"]) == {"status"}


def test_real_evidence_filters_each_action(service: ToolService, monkeypatch: Any) -> None:
    capabilities = (
        c.Capability("database.list", c.Support.IMPLEMENTED, c.Evidence.BENCH_VERIFIED),
        c.Capability("database.set_channel", c.Support.IMPLEMENTED, c.Evidence.DOCUMENTED),
        c.Capability("compile", c.Support.NOT_IMPLEMENTED, c.Evidence.BENCH_VERIFIED),
    )
    monkeypatch.setattr(service.backend, "capabilities", lambda: capabilities)
    filtered = ToolService(service.backend, replace(service.settings, backend_kind="com"))
    assert filtered.actions["canoe_database"] == {"list": "database.list"}
    definition = next(t for t in filtered.definitions if t.name == "canoe_database")
    assert definition.annotations is not None and definition.annotations.readOnlyHint
    assert "confirm" not in definition.inputSchema["properties"]
    assert "canoe_compile" not in filtered.actions


def test_preview_confirm_poll_and_remember_returned_ids(service: ToolService) -> None:
    result = service.invoke("canoe_database", {"action": "list"})
    assert result["evidence_axis"] == "fake"
    args = {"action": "set_channel", "database_id": "db:Demo", "channel": 2}
    preview = service.invoke("canoe_database", args)["result"]
    assert preview["needs_confirmation"] and preview["operation"] is None
    confirmed = service.invoke("canoe_database", {**args, "confirm": True})["result"]
    operation = confirmed["operation"]
    assert operation["state"] == "completed" and operation["result"]["channel"] == 2
    polled = service.invoke(
        "canoe_operation",
        {
            "action": "status",
            "operation_id": operation["operation_id"],
        },
    )["result"]
    assert polled["value"] == operation


def test_cached_preview_remains_stale_after_status(service: ToolService) -> None:
    args = {"action": "set_channel", "database_id": "db:Demo", "channel": 2}
    service.invoke("canoe_database", args)
    assert isinstance(service.backend, FakeBackend)
    service.backend.simulate_gui_open(str(service.settings.allowed_roots[0] / "demo.cfg"))
    service.invoke("canoe_status", {})
    result = service.invoke("canoe_database", {**args, "confirm": True})["result"]
    assert result["operation"]["error"]["code"] == "stale_session"
    assert not result["operation"]["dispatched"]


def test_readonly_and_roots_fail_before_effects(service: ToolService, tmp_path: Path) -> None:
    readonly = ToolService(service.backend, replace(service.settings, read_only=True))
    readonly.invoke("canoe_status", {})
    for confirm in (False, True):
        with pytest.raises(c.BackendError) as exc:
            readonly.invoke("canoe_compile", {"confirm": confirm})
        assert exc.value.code == c.ErrorCode.READ_ONLY_MODE
    with pytest.raises(c.BackendError) as exc:
        service.invoke("canoe_open_config", {"path": str(tmp_path.parent / "outside.cfg")})
    assert exc.value.code == c.ErrorCode.PATH_NOT_ALLOWED


def test_cancel_queued_operation_uses_operation_epoch_and_no_file_access(
    service: ToolService,
) -> None:
    operation = service.backend.compile(c.CallContext(service.backend.status().epoch, wait_s=0))
    assert isinstance(service.backend, FakeBackend)
    service.backend.simulate_gui_open(str(service.settings.allowed_roots[0] / "demo.cfg"))
    restricted = ToolService(service.backend, replace(service.settings, allowed_roots=()))
    args = {"action": "cancel", "operation_id": operation.operation_id}
    assert restricted.invoke("canoe_operation", args)["result"]["needs_confirmation"]
    assert (
        restricted.invoke("canoe_operation", {**args, "confirm": True})["result"]["operation"][
            "state"
        ]
        == "cancelled"
    )


def test_cancel_uncertain_operation_does_not_claim_undo(service: ToolService) -> None:
    assert isinstance(service.backend, FakeBackend)
    service.backend.hold_next_step()
    operation = service.backend.compile(c.CallContext(service.backend.status().epoch, wait_s=0))
    service.backend.advance()
    result = service.invoke(
        "canoe_operation",
        {
            "action": "cancel",
            "operation_id": operation.operation_id,
            "confirm": True,
        },
    )["result"]
    assert result["operation"]["state"] == "running"
    assert result["operation"]["effects_possible"]


def test_fake_import_is_isolated_and_com_failure_does_not_fallback(monkeypatch: Any) -> None:
    original = builtins.__import__

    def reject_com(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "com.backend" or name.startswith("canoe17_mcp.com"):
            raise ImportError("blocked COM import")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_com)
    assert isinstance(make_backend(Settings(backend_kind="fake")), FakeBackend)
    with pytest.raises(ImportError, match="blocked COM import"):
        make_backend(Settings(backend_kind="com"))


def test_shutdown_even_when_transport_fails(service: ToolService, monkeypatch: Any) -> None:
    from canoe17_mcp import server

    class BrokenTransport:
        def run(self, **kwargs: Any) -> None:
            raise OSError("broken pipe")

    called: list[bool] = []
    monkeypatch.setattr(server, "make_backend", lambda settings: service.backend)
    monkeypatch.setattr(server, "create_server", lambda backend, settings: BrokenTransport())
    monkeypatch.setattr(service.backend, "shutdown", lambda: called.append(True))
    assert main(["--backend", "fake"]) == 1
    assert called == [True]


def test_all_first_slice_adapters_preserve_defaults_and_return_ids(service: ToolService) -> None:
    root = service.settings.allowed_roots[0]
    added = service.invoke(
        "canoe_diag_description",
        {
            "action": "add",
            "network": "CAN",
            "path": str(root / "Demo.cdd"),
            "confirm": True,
        },
    )["result"]["operation"]
    assert added["state"] == "completed"
    identifier = added["result"]["id"]
    listed = service.invoke("canoe_diag_description", {"action": "list"})["result"]
    assert listed["value"][0]["id"] == identifier
    for action in ("open_windows", "close_windows", "remove"):
        result = service.invoke(
            "canoe_diag_description",
            {
                "action": action,
                "qualifier": identifier,
                "confirm": True,
            },
        )["result"]["operation"]
        assert result["state"] == "completed"
    for action in ("start", "stop"):
        result = service.invoke(
            "canoe_measurement",
            {
                "action": action,
                "confirm": True,
            },
        )["result"]["operation"]
        assert result["state"] == "completed"
        status = service.invoke("canoe_measurement", {"action": "status"})["result"]
        assert status["session"]["measurement_running"] == (action == "start")
    assert service.invoke("canoe_write_window", {"action": "read"})["result"]["value"] == {
        "text": "",
        "truncated": False,
    }
    assert (
        service.invoke(
            "canoe_write_window",
            {
                "action": "clear",
                "confirm": True,
            },
        )["result"]["operation"]["state"]
        == "completed"
    )
    assert service.invoke("canoe_save_config", {"confirm": True})["result"]["operation"]["result"][
        "backup_path"
    ].endswith(".bak-FAKE")
    service.invoke("canoe_quit", {"on_dirty": "discard", "confirm": True})
    assert not service.backend.status().connected


@pytest.mark.anyio
async def test_status_and_control_remain_responsive_while_mutation_waits(
    service: ToolService,
    monkeypatch: Any,
) -> None:
    entered, release = Event(), Event()
    original_wait = service.backend.wait

    def blocking_wait(operation_id: str, wait_s: float) -> c.Observed[c.OperationStatus]:
        entered.set()
        assert release.wait(5), "test did not release backend wait"
        return original_wait(operation_id, wait_s)

    monkeypatch.setattr(service.backend, "wait", blocking_wait)
    responses = []

    async def mutation() -> None:
        responses.append(await service.call_tool("canoe_compile", {"confirm": True}))

    async with anyio.create_task_group() as group:
        group.start_soon(mutation)
        try:
            assert await anyio.to_thread.run_sync(lambda: entered.wait(3))
            with anyio.fail_after(2):
                status = await service.call_tool("canoe_status", {})
                assert not status.isError
                assert isinstance(service.backend, FakeBackend)
                # The first operation is connect; find the queued compile.
                operation_id = next(
                    key for key, op in service.backend._ops.items() if op.kind == "compile"
                )
                cancelled = await service.call_tool(
                    "canoe_operation",
                    {
                        "action": "cancel",
                        "operation_id": operation_id,
                        "confirm": True,
                    },
                )
                assert not cancelled.isError
        finally:
            release.set()
    assert responses[0].structuredContent is not None
    assert responses[0].structuredContent["result"]["operation"]["state"] == "cancelled"


def test_edit_adapters_preview_then_complete_and_read_back(service: ToolService) -> None:
    root = service.settings.allowed_roots[0]

    def edit(tool: str, args: dict[str, Any]) -> Any:
        preview = service.invoke(tool, args)["result"]
        assert preview["needs_confirmation"] and preview["operation"] is None
        result = service.invoke(tool, {**args, "confirm": True})["result"]["operation"]
        assert result["state"] == "completed", result
        return result["result"]

    node = edit("canoe_node", {
        "action": "add", "name": "ECU", "bus": "CAN", "capl_path": str(root / "ECU.can"),
    })
    assert node["capl_path"] == str(root / "ECU.can") and node["buses"] == ["CAN"]
    node_id = node["id"]
    assert service.invoke("canoe_node", {"action": "list"})["result"]["value"] == [node]
    edit("canoe_node", {"action": "attach_bus", "node_id": node_id, "bus": "CAN2"})
    updated = edit("canoe_node", {"action": "detach_bus", "node_id": node_id, "bus": "CAN"})
    assert updated["buses"] == ["CAN2"]
    updated = edit("canoe_node", {"action": "set_active", "node_id": node_id, "active": False})
    assert not updated["active"]
    assert edit("canoe_node", {"action": "remove", "node_id": node_id}) == {"id": node_id}
    assert service.invoke("canoe_node", {"action": "list"})["result"]["value"] == []
    no_path = edit("canoe_node", {"action": "add", "name": "Empty", "bus": "CAN"})
    assert no_path["capl_path"] is None

    database = edit("canoe_database", {
        "action": "add", "path": str(root / "New.dbc"), "bus": "CAN", "channel": 2,
    })
    assert database["channel"] == 2 and database["path"] == str(root / "New.dbc")
    listed = service.invoke("canoe_database", {"action": "list"})["result"]["value"]
    assert database in listed
    edit("canoe_database", {"action": "remove", "database_id": database["id"]})
    assert len(service.invoke("canoe_database", {"action": "list"})["result"]["value"]) == 1

    env = edit("canoe_test_setup", {"action": "add_environment", "tse_path": str(root / "A.tse")})
    module = edit("canoe_test_setup", {
        "action": "add_module", "environment_id": env["id"], "can_path": str(root / "Test.can"),
    })
    edit("canoe_test_setup", {"action": "set_enabled", "module_id": module["id"], "enabled": False})
    setup = service.invoke("canoe_test_setup", {"action": "list"})["result"]["value"]
    assert setup["environments"][0]["modules"][0]["enabled"] is False


@pytest.mark.parametrize("tool,args,key", [
    ("canoe_node", {"action": "add", "name": "ECU", "bus": "CAN"}, "capl_path"),
    ("canoe_database", {"action": "add", "bus": "CAN", "channel": 1}, "path"),
    ("canoe_test_setup", {"action": "add_environment"}, "tse_path"),
    ("canoe_test_setup", {"action": "add_module", "environment_id": "env:A"}, "can_path"),
])
def test_edit_source_paths_refused_before_queue(
    service: ToolService, tool: str, args: dict[str, Any], key: str,
) -> None:
    assert isinstance(service.backend, FakeBackend)
    before = len(service.backend._ops)
    outside = str(service.settings.allowed_roots[0].parent / "outside.can")
    for confirm in (False, True):
        with pytest.raises(c.BackendError) as raised:
            service.invoke(tool, {**args, key: outside, "confirm": confirm})
        assert raised.value.code == c.ErrorCode.PATH_NOT_ALLOWED
    assert len(service.backend._ops) == before


def test_controller_read_is_registered_in_readonly_mode(service: ToolService) -> None:
    readonly = ToolService(service.backend, replace(service.settings, read_only=True))
    definition = next(t for t in readonly.definitions if t.name == "canoe_can_controller")
    assert definition.inputSchema["properties"]["action"]["enum"] == ["read"]
    assert "bitrate_bps" not in definition.inputSchema["properties"]
    result = readonly.invoke("canoe_can_controller", {"action": "read", "bus": "CAN", "channel": 1})
    assert result["result"]["value"]["bitrate_bps"] == 500_000
    assert not service.settings.audit_path.exists()


@pytest.mark.parametrize("tool,args", [
    ("canoe_node", {"action": "remove", "node_id": "node:ECU"}),
    ("canoe_test_setup", {"action": "set_enabled", "module_id": "tm:A/Test", "enabled": False}),
])
def test_new_edit_cached_previews_cannot_cross_epochs(
    service: ToolService, tool: str, args: dict[str, Any],
) -> None:
    service.invoke(tool, args)
    assert isinstance(service.backend, FakeBackend)
    service.backend.simulate_gui_open(str(service.settings.allowed_roots[0] / "demo.cfg"))
    op = service.invoke(tool, {**args, "confirm": True})["result"]["operation"]
    assert op["error"]["code"] == "stale_session" and not op["dispatched"]


def test_audit_refusals_errors_and_pending_resolution(service: ToolService) -> None:
    path = service.settings.audit_path
    service.invoke("canoe_compile", {})
    assert not path.exists()
    service.invoke("canoe_compile", {"confirm": True})
    readonly = ToolService(service.backend, replace(service.settings, read_only=True))
    with pytest.raises(c.BackendError):
        readonly.invoke("canoe_compile", {"confirm": True})
    assert isinstance(service.backend, FakeBackend)
    service.backend.hold_next_step()
    op = service.invoke("canoe_compile", {"confirm": True})["result"]["operation"]
    assert op["state"] == "running"
    service.backend.resolve_held()
    service.invoke("canoe_operation", {"action": "status", "operation_id": op["operation_id"]})
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(row["result"].get("error_code") == "read_only_mode" for row in rows)
    assert rows[-1]["result"]["state"] == "completed"
    assert rows[-1]["result"]["operation_id"] == op["operation_id"]
    assert not service.audit._pending


def test_audit_failure_blocks_dispatch_and_preserves_late_effects(
    service: ToolService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(service.backend, FakeBackend)
    before = len(service.backend._ops)
    original = service.audit._append

    def fail(*args: Any) -> None:
        raise OSError("secret failure message")

    monkeypatch.setattr(service.audit, "_append", fail)
    with pytest.raises(c.BackendError, match="no effect dispatched"):
        service.invoke("canoe_compile", {"confirm": True})
    assert len(service.backend._ops) == before

    def fail_outcomes(ticket: Any, result: dict[str, Any]) -> None:
        if result["state"] == "attempt":
            original(ticket, result)
        else:
            fail()

    monkeypatch.setattr(service.audit, "_append", fail_outcomes)
    result = service.invoke("canoe_compile", {"confirm": True})
    assert result["result"]["operation"]["state"] == "completed"
    assert "audit_error" in result


def test_real_manifest_registers_only_verified_edits(
    service: ToolService, monkeypatch: Any,
) -> None:
    from canoe17_mcp.com.backend import load_evidence

    monkeypatch.setattr(service.backend, "capabilities", lambda: tuple(load_evidence().values()))
    filtered = ToolService(service.backend, replace(service.settings, backend_kind="com"))
    assert set(filtered.actions["canoe_database"]) == {"list", "add", "remove"}
    assert set(filtered.actions["canoe_node"]) == {
        "list", "add", "remove", "set_active", "attach_bus", "detach_bus",
    }
    assert set(filtered.actions["canoe_test_setup"]) == {
        "list", "add_environment", "add_module", "set_enabled",
    }
    assert set(filtered.actions["canoe_can_controller"]) == {"read"}
    assert "canoe_bus" not in filtered.actions


def test_audit_records_unknown_then_late_completion(
    service: ToolService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(service.backend, FakeBackend)
    now = [service.backend._clock()]
    monkeypatch.setattr(service.backend, "_clock", lambda: now[0])
    service.backend.hold_next_step()
    op = service.invoke("canoe_compile", {"confirm": True})["result"]["operation"]
    now[0] += service.backend.settings.compile_step_s + 1
    polled = service.invoke("canoe_operation", {
        "action": "status", "operation_id": op["operation_id"],
    })
    assert polled["result"]["value"]["state"] == "outcome_unknown"
    rows = [json.loads(line) for line in service.settings.audit_path.read_text().splitlines()]
    assert rows[-1]["result"]["state"] == "outcome_unknown"
    assert rows[-1]["result"]["effects_possible"]
    service.backend.resolve_held()
    service.invoke("canoe_operation", {"action": "status", "operation_id": op["operation_id"]})
    rows = [json.loads(line) for line in service.settings.audit_path.read_text().splitlines()]
    assert rows[-1]["result"]["state"] == "completed"
    assert not service.audit._pending


def test_audit_failed_outcome_can_be_recovered_by_polling(
    service: ToolService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service.audit._append

    def fail_outcome(ticket: Any, result: dict[str, Any]) -> None:
        if result["state"] != "attempt":
            raise OSError("blocked")
        original(ticket, result)

    monkeypatch.setattr(service.audit, "_append", fail_outcome)
    result = service.invoke("canoe_compile", {"confirm": True})
    assert "audit_error" in result
    monkeypatch.setattr(service.audit, "_append", original)
    op = result["result"]["operation"]
    service.invoke("canoe_operation", {"action": "status", "operation_id": op["operation_id"]})
    rows = [json.loads(line) for line in service.settings.audit_path.read_text().splitlines()]
    assert rows[-1]["result"]["state"] == "completed"
    assert not service.audit._pending
