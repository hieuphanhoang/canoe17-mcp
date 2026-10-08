"""Bounded mutation journal with explicit selectors and policy-validated paths."""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO
from uuid import uuid4

from . import contracts as c
from .settings import Settings

log = logging.getLogger(__name__)
_LOCK_WAIT_S = 10.0
_LOCK_RETRY_S = 0.05
_TEXT_SELECTORS = frozenset({
    "action", "database_id", "node_id", "environment_id", "module_id", "operation_id",
    "qualifier", "bus", "network", "window", "section", "on_dirty",
})
_PATH_FIELDS = frozenset({"path", "as_path", "capl_path", "tse_path", "can_path", "xml_path"})
_SCALAR_CONTROLS = frozenset({
    "confirm", "channel", "active", "enabled", "open_console", "launch_if_absent",
    "timeout_s", "wait", "bitrate_bps",
})


@dataclass(frozen=True, slots=True)
class Ticket:
    id: str
    tool: str
    params: dict[str, Any]
    backend: str
    started: float
    configuration_path: str | None


class AuditLog:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.audit_path
        self.max_bytes = settings.audit_max_bytes
        self.backup_count = settings.audit_backup_count
        self._lock = RLock()
        self._pending: dict[str, tuple[Ticket, dict[str, Any] | None]] = {}

    @staticmethod
    def _acquire_lock(guard: BinaryIO) -> None:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError("Audit append lock wait expired") from exc
                time.sleep(min(_LOCK_RETRY_S, remaining))

    def _append(self, ticket: Ticket, result: dict[str, Any]) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(), "tool": ticket.tool,
            "params": ticket.params, "result": result,
            "duration": max(0, time.monotonic() - ticket.started),
            "call_id": ticket.id, "backend": ticket.backend,
            "configuration_path": ticket.configuration_path,
        }
        data = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(data) > self.max_bytes:
            raise OSError("Audit record exceeds configured bound")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize rotation across server processes as well as tool threads.
        lock_path = self.path.with_name(self.path.name + ".lock")
        with lock_path.open("a+b") as guard:
            if guard.seek(0, os.SEEK_END) == 0:
                guard.write(b"0")
                guard.flush()
            guard.seek(0)
            self._acquire_lock(guard)
            try:
                if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                    if self.backup_count:
                        for number in range(self.backup_count, 1, -1):
                            source = Path(f"{self.path}.{number - 1}")
                            if source.exists():
                                source.replace(Path(f"{self.path}.{number}"))
                        self.path.replace(Path(f"{self.path}.1"))
                    else:
                        self.path.unlink()
                with self.path.open("ab") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(guard.fileno(), fcntl.LOCK_UN)

    def begin(
        self, tool: str, args: dict[str, Any], backend: str, *,
        validated_paths: dict[str, str] | None = None,
        configuration_path: str | None = None,
    ) -> Ticket:
        # Schema-known scalar controls are useful; caller-supplied text can hold secrets.
        params = {
            key: value if key in _SCALAR_CONTROLS and (
                type(value) in {bool, int, float} or value is None
            ) else "[redacted]"
            for key, value in args.items()
        }
        for key in _TEXT_SELECTORS & args.keys():
            if isinstance(args[key], str):
                params[key] = args[key]
        # Only the policy supplies resolved, allowed-root-validated path values.
        for key, value in (validated_paths or {}).items():
            if key in _PATH_FIELDS and key in args:
                params[key] = value
        ticket = Ticket(uuid4().hex, tool, params, backend, time.monotonic(), configuration_path)
        with self._lock:
            if len(self._pending) >= 128:
                raise OSError("Poll outstanding operations before issuing more mutations")
            self._append(ticket, {"state": "attempt"})
            self._pending[ticket.id] = (ticket, None)
        return ticket

    @staticmethod
    def _operation(operation: dict[str, Any]) -> dict[str, Any]:
        error = operation.get("error")
        return {
            key: operation.get(key)
            for key in ("operation_id", "state", "epoch", "dispatched", "effects_possible")
        } | {"error_code": error.get("code") if error else None}

    def _safe_append(self, ticket: Ticket, result: dict[str, Any]) -> bool:
        try:
            self._append(ticket, result)
            return True
        except OSError:
            log.error(
                "Audit outcome write failed for call %s; effects may have occurred", ticket.id
            )
            return False

    def finish(self, ticket: Ticket, result: dict[str, Any]) -> bool:
        operation = result.get("operation")
        summary = self._operation(operation) if operation else {"state": "no_operation"}
        with self._lock:
            if ticket.id not in self._pending:
                return True
            # Retain accepted operation IDs even when the outcome write itself fails.
            self._pending[ticket.id] = (ticket, summary)
            if not self._safe_append(ticket, summary):
                return False
            if operation and operation["state"] not in c.TERMINAL_STATES:
                self._pending[ticket.id] = (ticket, summary)
            else:
                self._pending.pop(ticket.id, None)
            return True

    def finish_error(self, ticket: Ticket, error: Exception) -> None:
        with self._lock:
            previous = self._pending.get(ticket.id)
            summary = {
                "state": "call_error",
                "error_code": error.code.value if isinstance(error, c.BackendError) else "internal",
                # No accepted operation means no dispatch through the typed adapters.
                "effects_possible": previous is not None and previous[1] is not None,
            }
            self._safe_append(ticket, summary)
            if previous is not None and previous[1] is None:
                self._pending.pop(ticket.id, None)

    def observe(self, operation: dict[str, Any]) -> bool:
        with self._lock:
            success = True
            for ticket, previous in tuple(self._pending.values()):
                if (
                    previous is not None
                    and previous.get("operation_id") == operation["operation_id"]
                ):
                    success = self.finish(ticket, {"operation": operation}) and success
            return success
