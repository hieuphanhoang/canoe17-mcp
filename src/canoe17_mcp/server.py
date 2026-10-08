"""Official SDK stdio transport over the reviewed catalogue and SafetyPolicy.

FastMCP owns transport/lifecycle. Its low-level decorators preserve the jointly
reviewed JSON schemas instead of regenerating schemas from Python signatures.
Blocking backend reads and waits run outside the async protocol loop.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from typing import Any

import anyio
from mcp import types
from mcp.server.fastmcp import FastMCP

from . import contracts as c
from .audit import AuditLog
from .catalogue import Catalogue
from .safety import SafetyPolicy
from .settings import Settings, load_settings

log = logging.getLogger(__name__)

# None denotes state-store/control plumbing, which is always implemented by Backend.
_ACTIONS: dict[str, dict[str, str | None]] = {
    "canoe_status": {"read": None},
    "canoe_get_config_summary": {"read": "summary"},
    "canoe_open_config": {"open": "open_config"},
    "canoe_save_config": {"save": "save_config"},
    "canoe_quit": {"quit": "quit"},
    "canoe_compile": {"compile": "compile"},
    "canoe_measurement": {
        "status": None,
        "start": "measurement.start",
        "stop": "measurement.stop",
    },
    "canoe_operation": {"status": None, "cancel": None},
    "canoe_database": {
        action: f"database.{action}" for action in ("list", "add", "remove", "set_channel")
    },
    "canoe_node": {
        action: f"node.{action}"
        for action in ("list", "add", "remove", "set_active", "attach_bus", "detach_bus")
    },
    "canoe_test_setup": {
        action: f"test_setup.{action}"
        for action in ("list", "add_environment", "add_module", "set_enabled")
    },
    "canoe_can_controller": {"read": "can_controller.read"},
    "canoe_diag_description": {
        action: f"diag_description.{action}"
        for action in ("list", "add", "remove", "open_windows", "close_windows")
    },
    "canoe_write_window": {"read": "write_window.read", "clear": "write_window.clear"},
}


def plain(value: Any) -> Any:
    """Serialize immutable contract snapshots without dropping freshness or errors."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, bytes):
        return value.hex()
    return value


def make_backend(settings: Settings) -> c.Backend:
    if settings.backend_kind == "fake":
        from .fake import FakeBackend, FakeConfiguration

        return FakeBackend(
            settings.backend,
            configurations=tuple(
                FakeConfiguration(str(path)) for path in settings.fake_config_paths
            ),
            licensed=settings.fake_licensed,
        )
    # Deliberately no fallback. Even a missing pywin32 dependency is a startup failure.
    from .com.backend import ComBackend

    return ComBackend(settings.backend)


class ToolService:
    def __init__(self, backend: c.Backend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings
        self.policy = SafetyPolicy(backend, settings)
        self.audit = AuditLog(settings)
        self.catalogue = Catalogue(maximum_timeout_s=settings.backend.operation_max_timeout_s)
        self.actions: dict[str, dict[str, str | None]] = {}
        self.definitions: list[types.Tool] = []
        capabilities = {item.operation: item for item in backend.capabilities()}
        floor = c.REAL_EVIDENCE_ORDER.index(settings.backend.min_evidence)

        def supported(operation: str | None) -> bool:
            if operation is None:
                return True
            cap = capabilities.get(operation)
            if cap is None or cap.support != c.Support.IMPLEMENTED:
                return False
            if settings.backend_kind == "fake":
                return cap.evidence == c.Evidence.FAKE
            return (
                cap.evidence in c.REAL_EVIDENCE_ORDER
                and c.REAL_EVIDENCE_ORDER.index(cap.evidence) >= floor
            )

        for entry in self.catalogue.discovery():
            name = entry["name"]
            actions = {action: op for action, op in _ACTIONS.get(name, {}).items() if supported(op)}
            if not actions:
                continue
            self.actions[name] = actions
            schema = entry["inputSchema"]
            if "action" in schema["properties"]:
                schema["properties"]["action"]["enum"] = list(actions)
                # Retain only fields used by the remaining action variants.
                variants = self.catalogue.tools[name]["validationSchema"]["oneOf"]
                used = set().union(
                    *(
                        set(row["properties"])
                        for row in variants
                        if row["properties"]["action"]["const"] in actions
                    )
                )
                schema["properties"] = {
                    key: value for key, value in schema["properties"].items() if key in used
                }
            writes = any(
                self.catalogue.tools[name]["actions"][action]["effect"] == "write"
                for action in actions
            )
            entry["annotations"]["readOnlyHint"] = not writes
            if not writes:
                entry["annotations"]["destructiveHint"] = False
            self.definitions.append(types.Tool(**entry))

    async def list_tools(self) -> list[types.Tool]:
        return copy.deepcopy(self.definitions)

    def _remember(self, value: Any) -> Any:
        if isinstance(value, c.Observed):
            self.policy.remember(value)
        return value

    def _read(self, name: str, args: dict[str, Any]) -> Any:
        self.policy.check_access(write=False)
        if name in {"canoe_status", "canoe_measurement"}:
            return {
                "session": self.backend.status(),
                "read_only": self.settings.read_only,
                "allowed_roots": tuple(str(path) for path in self.settings.allowed_roots),
                "capabilities": self.backend.capabilities(),
                "availability": self.backend.availability(),
            }
        if name == "canoe_get_config_summary":
            return self._remember(self.backend.summary(args.get("section", "all")))
        if name == "canoe_database":
            return self._remember(self.backend.databases())
        if name == "canoe_node":
            return self._remember(self.backend.nodes())
        if name == "canoe_test_setup":
            return self._remember(self.backend.test_setup())
        if name == "canoe_can_controller":
            return self._remember(self.backend.can_controller(args["bus"], args["channel"]))
        if name == "canoe_diag_description":
            return self._remember(self.backend.diag_descriptions())
        if name == "canoe_write_window":
            return self._remember(self.backend.write_window(args.get("max_chars", 8192)))
        if name == "canoe_operation":
            return self._remember(self.backend.operation(args["operation_id"]))
        raise c.BackendError(c.ErrorCode.CAPABILITY_UNAVAILABLE, "Read handler unavailable")

    def _mutation(
        self, name: str, action: str, args: dict[str, Any]
    ) -> tuple[dict[str, Any], Callable[[c.CallContext], c.OperationStatus]]:
        b = self.backend
        if name == "canoe_open_config":
            params = {
                "path": self.policy.allowed_path(args["path"]),
                "on_dirty": args.get("on_dirty", "refuse"),
                "launch_if_absent": args.get("launch_if_absent", True),
            }
            return params, lambda ctx: b.open_config(**params, ctx=ctx)
        if name == "canoe_save_config":
            path = self.policy.allowed_path(args["as_path"]) if "as_path" in args else None
            return {"as_path": path}, lambda ctx: b.save_config(path, ctx)
        if name == "canoe_quit":
            dirty = args.get("on_dirty", "refuse")
            return {"on_dirty": dirty}, lambda ctx: b.quit(dirty, ctx)
        if name == "canoe_compile":
            return {}, b.compile
        if name == "canoe_measurement":
            return {}, b.measurement_start if action == "start" else b.measurement_stop
        if name == "canoe_database":
            if action == "add":
                params = {
                    "path": self.policy.allowed_path(args["path"]),
                    "bus": args["bus"], "channel": args["channel"],
                }
                return params, lambda ctx: b.add_database(**params, ctx=ctx)
            if action == "remove":
                identifier = args["database_id"]
                return {"database_id": identifier}, lambda ctx: b.remove_database(identifier, ctx)
            params = {"database_id": args["database_id"], "channel": args["channel"]}
            return params, lambda ctx: b.set_database_channel(**params, ctx=ctx)
        if name == "canoe_node":
            if action == "add":
                params = {
                    "name": args["name"], "bus": args["bus"],
                    "capl_path": self.policy.allowed_path(args["capl_path"])
                    if "capl_path" in args else None,
                }
                return params, lambda ctx: b.add_node(**params, ctx=ctx)
            identifier = args["node_id"]
            if action == "remove":
                return {"node_id": identifier}, lambda ctx: b.remove_node(identifier, ctx)
            if action == "set_active":
                params = {"node_id": identifier, "active": args["active"]}
                return params, lambda ctx: b.set_node_active(**params, ctx=ctx)
            params = {"node_id": identifier, "bus": args["bus"]}
            return params, lambda ctx: b.attach_node_bus(
                **params, attach=action == "attach_bus", ctx=ctx
            )
        if name == "canoe_test_setup":
            if action == "add_environment":
                params = {"tse_path": self.policy.allowed_path(args["tse_path"])}
                return params, lambda ctx: b.add_test_environment(**params, ctx=ctx)
            if action == "add_module":
                params = {
                    "environment_id": args["environment_id"],
                    "can_path": self.policy.allowed_path(args["can_path"]),
                }
                return params, lambda ctx: b.add_test_module(**params, ctx=ctx)
            params = {"module_id": args["module_id"], "enabled": args["enabled"]}
            return params, lambda ctx: b.set_test_module_enabled(**params, ctx=ctx)
        if name == "canoe_diag_description":
            if action == "add":
                params = {
                    "network": args["network"],
                    "path": self.policy.allowed_path(args["path"]),
                    "ecu_identifier": args.get("ecu_identifier"),
                    "open_console": args.get("open_console", True),
                }
                return params, lambda ctx: b.add_diag_description(**params, ctx=ctx)
            qualifier = args["qualifier"]
            if action == "remove":
                return {"qualifier": qualifier}, lambda ctx: b.remove_diag_description(
                    qualifier, ctx
                )
            window = args.get("window", "console")
            return {"qualifier": qualifier, "window": window}, lambda ctx: b.diag_windows(
                qualifier,
                window,
                action == "open_windows",
                ctx,
            )
        if name == "canoe_write_window":
            return {}, b.clear_write_window
        raise c.BackendError(c.ErrorCode.CAPABILITY_UNAVAILABLE, "Mutation handler unavailable")

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args, write = self.catalogue.validate(name, arguments)
        ticket = None
        audit_attempted = False

        def start_audit(preview: c.Observed[c.EffectPreview] | None = None) -> None:
            nonlocal ticket, audit_attempted
            if not write or not args["confirm"]:
                return
            paths = {}
            configuration_path = None
            if preview is not None:
                for key in ("path", "as_path", "capl_path", "tse_path", "can_path", "xml_path"):
                    if key in args:
                        paths[key] = self.policy.allowed_path(args[key])
                if preview.value.configuration_path is not None:
                    try:
                        configuration_path = self.policy.allowed_path(
                            preview.value.configuration_path
                        )
                    except c.BackendError:
                        # Open/quit/cancel may leave a configuration outside allowed roots.
                        configuration_path = "[redacted]"
            audit_attempted = True
            try:
                ticket = self.audit.begin(
                    name, args, self.settings.backend_kind,
                    validated_paths=paths, configuration_path=configuration_path,
                )
            except OSError as exc:
                raise c.BackendError(
                    c.ErrorCode.CAPABILITY_UNAVAILABLE,
                    "Audit log unavailable; no effect dispatched",
                ) from exc

        def accepted(operation: c.OperationStatus) -> None:
            if ticket is not None:
                self.audit.finish(ticket, {"operation": plain(operation)})

        try:
            payload = self._invoke(name, args, write, start_audit, accepted)
        except Exception as exc:
            if write and args["confirm"] and not audit_attempted:
                # Refusal before a usable preview: do not persist unvalidated paths.
                start_audit()
            if ticket is not None:
                self.audit.finish_error(ticket, exc)
            raise
        if ticket is not None:
            if not self.audit.finish(ticket, payload["result"]):
                payload["audit_error"] = "Outcome logging failed; effect may already have occurred"
            operation = payload["result"].get("operation")
            if operation and name == "canoe_operation":
                if not self.audit.observe(operation):
                    payload["audit_error"] = "Operation outcome logging failed"
        elif name == "canoe_operation" and args.get("action") == "status":
            if not self.audit.observe(payload["result"]["value"]):
                payload["audit_error"] = "Operation outcome logging failed"
        return payload

    def _invoke(
        self, name: str, args: dict[str, Any], write: bool,
        before_dispatch: Callable[[c.Observed[c.EffectPreview]], None],
        accepted: Callable[[c.OperationStatus], None],
    ) -> dict[str, Any]:
        action = args.get("action", next(iter(self.catalogue.tools[name]["actions"])))
        if name not in self.actions or action not in self.actions[name]:
            raise c.BackendError(
                c.ErrorCode.CAPABILITY_UNAVAILABLE, "Tool/action is not registered"
            )
        self.policy.check_access(write=write)
        if not write:
            result = self._read(name, args)
        else:
            if name == "canoe_operation":
                result = self.policy.cancel_operation(
                    args["operation_id"], confirm=args["confirm"], before_dispatch=before_dispatch,
                )
            else:
                params, dispatch = self._mutation(name, action, args)
                operation = self.actions[name][action]
                assert operation is not None
                result = self.policy.invoke(
                    c.EffectRequest(operation, tuple(sorted(params.items()))),
                    confirm=args["confirm"],
                    dispatch=dispatch,
                    wait_s=0,
                    before_dispatch=before_dispatch,
                )
                if result.operation is not None:
                    accepted(result.operation)
                    observed = self.backend.wait(
                        result.operation.operation_id, self.settings.wait_seconds(None)
                    )
                    self._remember(observed)
                    result = replace(result, operation=observed.value)
        return {
            "backend": self.settings.backend_kind,
            "evidence_axis": "fake" if self.settings.backend_kind == "fake" else "real",
            "result": plain(result),
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        failed = False
        try:
            payload = await anyio.to_thread.run_sync(lambda: self.invoke(name, arguments))
        except c.BackendError as exc:
            failed = True
            payload = {"backend": self.settings.backend_kind, "error": plain(exc.info)}
        except Exception:
            log.exception("Unexpected tool failure: %s", name)
            failed = True
            payload = {"error": {"code": "internal", "message": "Unexpected server error"}}
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))
            ],
            structuredContent=payload,
            isError=failed,
        )


def create_server(backend: c.Backend, settings: Settings) -> FastMCP:
    service = ToolService(backend, settings)
    app = FastMCP("canoe17-mcp", log_level="WARNING")
    # SDK low-level handlers support explicit schemas and structured error results.
    app._mcp_server.list_tools()(service.list_tools)
    app._mcp_server.call_tool(validate_input=False)(service.call_tool)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CANoe 17 MCP server (stdio only)")
    parser.add_argument("--config", help="TOML settings path")
    parser.add_argument("--backend", choices=("com", "fake"), help="Override backend_kind")
    parser.add_argument(
        "--check", action="store_true",
        help="Print installation-check JSON and exit without connecting to CANoe",
    )
    options = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    backend: c.Backend | None = None
    try:
        settings = load_settings(options.config)
        if options.backend is not None:
            settings = replace(settings, backend_kind=options.backend)
        if options.check:
            from .check import installation_check

            report = installation_check(settings)
            print(json.dumps(report))
            return 0 if report["ok"] else 1
        backend = make_backend(settings)
        create_server(backend, settings).run(transport="stdio")
        return 0
    except (ValueError, OSError, ImportError, c.BackendError) as exc:
        if options.check:
            print(json.dumps({
                "ok": False,
                "checks": [{"name": "settings", "status": "fail", "detail": str(exc)}],
            }))
            return 1
        log.error("Server failed: %s", exc)
        return 1
    finally:
        if backend is not None:
            backend.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
