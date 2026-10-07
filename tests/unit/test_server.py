from __future__ import annotations

import builtins
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
    settings = Settings(read_only=False, allowed_roots=(tmp_path,), backend_kind="fake")
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
    }
    database = definitions["canoe_database"].inputSchema
    assert database["properties"]["action"]["enum"] == ["list", "set_channel"]
    assert "path" not in database["properties"]
    for definition in definitions.values():
        assert not {"oneOf", "anyOf", "allOf"} & definition.inputSchema.keys()
    with pytest.raises(c.BackendError) as exc:
        service.invoke("canoe_database", {"action": "remove", "database_id": "db:Demo"})
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
