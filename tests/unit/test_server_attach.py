"""S1 server review: real STA/lock with all CANoe interactions replaced by stubs."""

from __future__ import annotations

import sys
from pathlib import Path
from threading import get_ident
from typing import Any
from uuid import uuid4

import pytest

from canoe17_mcp import contracts as c
from canoe17_mcp.server import ToolService
from canoe17_mcp.settings import Settings


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM apartment")
def test_readonly_server_list_attaches_dirty_session_and_remembers_new_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from canoe17_mcp.com.backend import ComBackend

    settings = Settings(
        read_only=True, audit_path=tmp_path / "audit.jsonl",
        backend=c.BackendSettings(lock_key="s1-server-" + uuid4().hex),
    )
    backend = ComBackend(settings.backend, lock_dir=tmp_path / "lock")
    calls: list[tuple[bool, int]] = []
    fields: dict[str, Any] = {
        "configuration_path": str(tmp_path / "dirty.cfg"),
        "configuration_modified": True, "measurement_running": False,
    }

    def attach(*, launch: bool) -> bool:
        calls.append((launch, get_ident()))
        backend.session.app = object()
        return True

    monkeypatch.setattr(backend.session, "attach", attach)
    monkeypatch.setattr(backend.session, "session_fields", lambda: fields)
    monkeypatch.setattr(backend.session, "cheap_fields", lambda: fields)
    monkeypatch.setattr(backend.session, "detach", lambda: setattr(backend.session, "app", None))
    monkeypatch.setattr(backend.session, "nodes", lambda epoch: (
        c.NodeInfo("node:ECU", "ECU", True, None, ("CAN",)),
    ))
    try:
        service = ToolService(backend, settings)
        assert not service.invoke("canoe_status", {})["result"]["session"]["connected"]
        operation = backend.store.new_operation("compile", backend.store.epoch)
        service.invoke("canoe_operation", {
            "action": "status", "operation_id": operation.operation_id,
        })
        assert calls == []
        result = service.invoke("canoe_node", {"action": "list"})["result"]
        assert calls == [(False, backend.worker.thread_id)]
        assert calls[0][1] != get_ident()
        assert result["epoch"] == backend.status().epoch == operation.epoch + 1
        assert service.policy._id_epochs["node:ECU"] == result["epoch"]
        assert backend.status().configuration_modified is True
        service.invoke("canoe_node", {"action": "list"})
        assert len(calls) == 1 and not settings.audit_path.exists()
    finally:
        backend.shutdown()
