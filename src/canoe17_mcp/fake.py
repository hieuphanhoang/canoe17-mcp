"""Deterministic, explicitly FAKE backend; never imports or contacts CANoe.

No files are written. Configurations and saves live only in memory. wait()/advance()
drive the fake queue; there is no STA thread. hold_next_step(), an injected clock,
and resolve_held() let tests model a blocked RPC and its late outcome without sleep.
Backup paths ending in .bak-FAKE are synthetic reporting markers for overwrites
of registered in-memory configurations; they are not files or restorable backups.
Diagnostic qualifiers default to the file stem (no CDD/ODX parsing); collisions
are renamed with _1, _2, etc. as in CANoe's D6 evidence.
Unimplemented extension actions fail closed and advertise NOT_IMPLEMENTED.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Never, Protocol

from . import contracts as c
from .settings import validate_backend


class _Identified(Protocol):
    @property
    def id(self) -> str: ...


_DEFAULT_SETTINGS = c.BackendSettings()


@dataclass(frozen=True, slots=True)
class FakeConfiguration:
    path: str
    modified: bool = False
    databases: tuple[c.DatabaseInfo, ...] = ()
    buses: tuple[c.BusInfo, ...] = ()
    nodes: tuple[c.NodeInfo, ...] = ()
    diagnostics: tuple[c.DiagDescriptionInfo, ...] = ()
    tests: c.TestSetupInfo = c.TestSetupInfo(())
    compile_success: bool = True


@dataclass(slots=True)
class _Job:
    ctx: c.CallContext
    check: Callable[[], None]
    effect: Callable[[], c.OperationResult]
    queued_at: float
    step_s: float
    started_at: float | None = None


_IMPLEMENTED = (
    "connect",
    "open_config",
    "save_config",
    "quit",
    "compile",
    "measurement.start",
    "measurement.stop",
    "database.list",
    "database.add",
    "database.remove",
    "database.set_channel",
    "diag_description.list",
    "diag_description.add",
    "diag_description.remove",
    "diag_description.open_windows",
    "diag_description.close_windows",
    "node.set_active",
    "node.list",
    "node.add",
    "node.remove",
    "node.attach_bus",
    "node.detach_bus",
    "can_controller.read",
    "test_setup.list",
    "test_setup.add_environment",
    "test_setup.add_module",
    "test_setup.set_enabled",
    "write_window.read",
    "write_window.clear",
    "summary",
)
_EXTENSIONS = (
    "bus.add",
    "bus.remove",
    "can_controller.set_bitrate",
    "test_run",
    "diag_request",
    "tester_present.start",
    "tester_present.stop",
    "value.set",
    "capl.call",
    "helper.install",
    "can_frame.send",
)


def _wall() -> str:
    return datetime.now(UTC).isoformat()


class FakeBackend:
    def __init__(
        self,
        settings: c.BackendSettings = _DEFAULT_SETTINGS,
        *,
        configurations: tuple[FakeConfiguration, ...] = (),
        process_running: bool = True,
        licensed: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_backend(settings)
        self.settings = settings
        self._clock = clock
        self._configs = {item.path: item for item in configurations}
        self._config = configurations[0] if configurations else None
        self._process_running = process_running
        self._connected = False
        self._licensed = licensed
        self._measurement = False
        self._epoch = 0
        self._updated = clock()
        self._ops: dict[str, c.OperationStatus] = {}
        self._jobs: dict[str, _Job] = {}
        self._queue: list[str] = []
        self._active: str | None = None
        self._degraded = False
        self._hold_next = False
        self._fingerprints: dict[str, tuple[object, ...]] = {}
        self._text = ""
        self.transitions: list[tuple[str, c.OpState]] = []

    def _observed[T](self, value: T) -> c.Observed[T]:
        return c.Observed(
            value,
            self._epoch,
            max(0, self._clock() - self._updated),
            self._active,
            self._degraded,
        )

    def _set(self, op: c.OperationStatus, state: c.OpState, **kwargs: object) -> c.OperationStatus:
        if state not in c.OP_TRANSITIONS[op.state]:
            raise AssertionError(f"Illegal fake transition {op.state} -> {state}")
        updated = replace(op, state=state, **kwargs)
        self._ops[op.operation_id] = updated
        self.transitions.append((op.operation_id, state))
        return updated

    def _expire(self) -> None:
        now = self._clock()
        for operation_id in tuple(self._queue):
            if now - self._jobs[operation_id].queued_at >= self.settings.dispatch_timeout_s:
                self._set(
                    self._ops[operation_id],
                    c.OpState.CANCELLED,
                    finished_wall=_wall(),
                    error=c.ErrorInfo(
                        c.ErrorCode.BUSY if self._active else c.ErrorCode.DEADLINE_EXCEEDED,
                        "Fake dispatch deadline; never dispatched",
                    ),
                )
                self._queue.remove(operation_id)
        if self._active is not None:
            op = self._ops[self._active]
            job = self._jobs[self._active]
            if (
                job.started_at is not None
                and now - job.started_at >= job.step_s
                and op.state == c.OpState.RUNNING
            ):
                self._set(
                    op,
                    c.OpState.OUTCOME_UNKNOWN,
                    error=c.ErrorInfo(
                        c.ErrorCode.OUTCOME_UNKNOWN, "Fake in-flight step exceeded deadline"
                    ),
                )
                self._degraded = True

    def _submit(
        self,
        kind: c.OperationKind,
        ctx: c.CallContext,
        effect: Callable[[], c.OperationResult],
        check: Callable[[], None] = lambda: None,
    ) -> c.OperationStatus:
        self._expire()
        if self._degraded:
            raise c.BackendError(c.ErrorCode.CAPABILITY_UNAVAILABLE, "Fake backend degraded")
        if ctx.wait_s is not None:
            from .settings import Settings

            try:
                Settings(backend=self.settings).wait_seconds(ctx.wait_s)
            except ValueError as exc:
                raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        op = c.OperationStatus(
            c.new_operation_id(), kind, c.OpState.QUEUED, _wall(), ctx.expected_epoch
        )
        step_s = {
            "open_config": self.settings.open_step_s
            if self._process_running
            else self.settings.launch_step_s,
            "save_config": self.settings.save_step_s,
            "compile": self.settings.compile_step_s,
        }.get(kind, self.settings.quick_step_s)
        self._ops[op.operation_id] = op
        self._jobs[op.operation_id] = _Job(ctx, check, effect, self._clock(), step_s)
        self._queue.append(op.operation_id)
        self.transitions.append((op.operation_id, c.OpState.QUEUED))
        return op

    def advance(self) -> None:
        """Test scheduler: dispatch at most one job; control/status stay callable."""
        self._expire()
        if self._active is not None or not self._queue:
            return
        operation_id = self._queue.pop(0)
        job = self._jobs[operation_id]
        op = self._set(self._ops[operation_id], c.OpState.RUNNING, started_wall=_wall())
        try:
            if job.ctx.expected_epoch != self._epoch:
                raise c.BackendError(c.ErrorCode.STALE_SESSION, "Fake session epoch changed")
            job.check()
        except c.BackendError as exc:
            self._set(op, c.OpState.FAILED, error=exc.info, finished_wall=_wall())
            return
        op = replace(op, dispatched=True, effects_possible=True)
        self._ops[operation_id] = op
        self._active = operation_id
        job.started_at = self._clock()
        if self._hold_next:
            self._hold_next = False
            return
        self.resolve_held()

    def hold_next_step(self) -> None:
        self._hold_next = True

    def resolve_held(self, error: c.ErrorInfo | None = None) -> None:
        """Resolve a dispatched fake step, preserving uncertainty and late resolution."""
        self._expire()
        if self._active is None:
            raise ValueError("No held fake step")
        op = self._ops[self._active]
        late = op.state == c.OpState.OUTCOME_UNKNOWN
        try:
            # A late operation must never apply its effect to a replacement configuration.
            if op.epoch != self._epoch:
                raise c.BackendError(c.ErrorCode.STALE_SESSION, "Fake session replaced in flight")
            if error is not None:
                raise c.BackendError.from_info(error)
            result = self._jobs[self._active].effect()
            self._set(
                op,
                c.OpState.COMPLETED,
                result=result,
                error=None,
                late_resolution=late,
                finished_wall=_wall(),
            )
        except c.BackendError as exc:
            self._set(
                op, c.OpState.FAILED, error=exc.info, late_resolution=late, finished_wall=_wall()
            )
        finally:
            self._active = None
            self._degraded = False
            self._updated = self._clock()

    def status(self) -> c.SessionStatus:
        self._expire()
        config = self._config if self._connected else None
        return c.SessionStatus(
            self._connected,
            "17.6.5 (FAKE)" if self._connected else None,
            None,
            config.path if config else None,
            config.modified if config else None,
            self._measurement if self._connected else None,
            self._epoch,
            self._connected,
            None,
            self._degraded,
            self._active,
            max(0, self._clock() - self._updated),
            "fake",
            self._licensed,
        )

    def capabilities(self) -> tuple[c.Capability, ...]:
        return tuple(
            c.Capability(
                name,
                c.Support.IMPLEMENTED,
                c.Evidence.FAKE,
                note="In-memory model only; not real CANoe verification",
            )
            for name in _IMPLEMENTED
        ) + tuple(
            c.Capability(
                name,
                c.Support.NOT_IMPLEMENTED,
                c.Evidence.FAKE,
            )
            for name in _EXTENSIONS
        )

    def _block(self, action: str) -> c.BlockReason | None:
        if action not in _IMPLEMENTED:
            return c.BlockReason.UNSUPPORTED
        if self._degraded:
            return c.BlockReason.DEGRADED
        if action not in {"open_config", "connect"}:
            if not self._connected:
                return c.BlockReason.NOT_CONNECTED
            if self._config is None and action != "quit":
                return c.BlockReason.NO_CONFIGURATION
        if not self._licensed and action in {
            "save_config",
            "measurement.start",
            "measurement.stop",
        }:
            return c.BlockReason.NO_LICENSE
        if self._measurement and (
            action in {"open_config", "save_config", "quit", "compile"}
            or action.startswith(("database.", "node.", "test_setup."))
            and not action.endswith(".list")
        ):
            return c.BlockReason.MEASUREMENT_RUNNING
        return None

    def availability(self) -> c.Observed[tuple[c.Availability, ...]]:
        self._expire()
        return self._observed(
            tuple(
                c.Availability(
                    item.operation, self._block(item.operation) is None, self._block(item.operation)
                )
                for item in self.capabilities()
            )
        )

    def _need_config(self) -> FakeConfiguration:
        if not self._connected:
            raise c.BackendError(c.ErrorCode.NOT_CONNECTED, "Fake backend is disconnected")
        if self._config is None:
            raise c.BackendError(c.ErrorCode.NO_CONFIGURATION, "No fake configuration")
        return self._config

    def _stopped(self) -> None:
        self._need_config()
        if self._measurement:
            raise c.BackendError(c.ErrorCode.MEASUREMENT_RUNNING, "Stop measurement first")

    def _licence(self) -> None:
        if not self._licensed:
            raise c.BackendError(
                c.ErrorCode.LICENSE_REQUIRED,
                "FAKE: Function is only available with valid application license.",
                hresult=-2147352567,
            )

    def connect(self) -> c.OperationStatus:
        def check() -> None:
            if not self._process_running:
                raise c.BackendError(c.ErrorCode.NO_ACTIVE_INSTANCE, "No fake CANoe process")

        def effect() -> None:
            if not self._connected:
                self._connected = True
                self._epoch += 1

        return self._submit("connect", c.CallContext(self._epoch), effect, check)

    def _dirty_check(self, policy: c.DirtyPolicy) -> None:
        if policy not in {"refuse", "save", "discard"}:
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "Invalid dirty policy")
        if self._measurement:
            raise c.BackendError(c.ErrorCode.MEASUREMENT_RUNNING, "Stop measurement first")
        if self._config is not None and self._config.modified:
            if policy == "refuse":
                raise c.BackendError(c.ErrorCode.DIRTY_CONFIG, "Dirty fake configuration refused")
            if policy == "save":
                self._licence()

    def open_config(
        self, path: str, on_dirty: c.DirtyPolicy, launch_if_absent: bool, ctx: c.CallContext
    ) -> c.OperationStatus:
        def check() -> None:
            self._dirty_check(on_dirty)
            if not self._process_running and not (launch_if_absent and self.settings.allow_launch):
                raise c.BackendError(c.ErrorCode.NO_ACTIVE_INSTANCE, "Fake launch is not allowed")

        def effect() -> c.OpenResult:
            if path not in self._configs:
                raise c.BackendError(c.ErrorCode.CANOE_REJECTED, "Unknown fake configuration")
            attached = self._process_running
            old = self._config
            saved = backup = None
            if old is not None and old.modified and on_dirty == "save":
                self._licence()
                backup = self._backup_marker(old.path)
                self._configs[old.path] = replace(old, modified=False)
                saved = old.path
            discarded = old is not None and old.modified and on_dirty == "discard"
            self._process_running = self._connected = True
            self._config = replace(self._configs[path], modified=False)
            self._epoch += 1
            self._fingerprints.clear()
            return c.OpenResult(path, attached, discarded, saved, self._epoch, backup)

        return self._submit("open_config", ctx, effect, check)

    def _backup_marker(self, path: str) -> str | None:
        return f"{path}.bak-FAKE" if path in self._configs else None

    def save_config(self, as_path: str | None, ctx: c.CallContext) -> c.OperationStatus:
        def effect() -> c.SaveResult:
            self._licence()
            old = self._need_config()
            target = as_path or old.path
            backup = self._backup_marker(target)
            changed = target != old.path
            self._config = replace(old, path=target, modified=False)
            self._configs[target] = self._config
            if changed:
                self._epoch += 1
                self._fingerprints.clear()
            return c.SaveResult(target, target, changed, backup, self._epoch)

        return self._submit("save_config", ctx, effect, self._stopped)

    def quit(self, on_dirty: c.DirtyPolicy, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._need_config()
            self._dirty_check(on_dirty)

        def effect() -> c.SaveResult | None:
            saved = None
            if self._config is not None and self._config.modified and on_dirty == "save":
                path = self._config.path
                saved = c.SaveResult(path, path, False, self._backup_marker(path), self._epoch)
                self._configs[path] = replace(self._config, modified=False)
            self._connected = self._process_running = False
            self._epoch += 1
            self._fingerprints.clear()
            return saved

        return self._submit("quit", ctx, effect, check)

    def operation(self, operation_id: str) -> c.Observed[c.OperationStatus]:
        self._expire()
        if operation_id not in self._ops:
            raise c.BackendError(c.ErrorCode.NOT_FOUND, "Unknown fake operation")
        return self._observed(self._ops[operation_id])

    def wait(self, operation_id: str, wait_s: float) -> c.Observed[c.OperationStatus]:
        from .settings import Settings

        try:
            Settings(backend=self.settings).wait_seconds(wait_s)
        except ValueError as exc:
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        self.operation(operation_id)
        if wait_s > 0:
            # A held RPC returns immediately in this deterministic fake. Its caller's
            # wait is not a step timeout and must not claim outcome_unknown by itself.
            while self._queue and self._active is None:
                self.advance()
                if self._ops[operation_id].state in c.TERMINAL_STATES:
                    break
        return self.operation(operation_id)

    def cancel(self, operation_id: str) -> c.OperationStatus:
        op = self.operation(operation_id).value
        if op.state == c.OpState.QUEUED:
            self._queue.remove(operation_id)
            return self._set(op, c.OpState.CANCELLED, finished_wall=_wall())
        # A dispatched generic call cannot be unsent. Preserve its pollable state.
        return op

    def preview(self, request: c.EffectRequest) -> c.Observed[c.EffectPreview]:
        self._expire()
        params = dict(request.params)
        config = self._config
        dirty = config.modified if config else None
        block = self._block(request.action)
        saves_first = (
            request.action in {"open_config", "quit"}
            and params.get("on_dirty") == "save"
            and dirty
        )
        if saves_first and not self._licensed and block != c.BlockReason.DEGRADED:
            block = c.BlockReason.NO_LICENSE
        overwrites = ()
        if request.action == "save_config":
            path = params.get("as_path") or (config.path if config else None)
            overwrites = (path,) if isinstance(path, str) and path in self._configs else ()
        elif saves_first and config is not None:
            overwrites = (config.path,)
        return self._observed(
            c.EffectPreview(
                request.action,
                affected=tuple(str(v) for k, v in request.params if k.endswith("_id")),
                launches_canoe=request.action == "open_config" and not self._process_running,
                overwrites=overwrites,
                configuration_path=config.path if config else None,
                configuration_modified=dirty,
                discards_changes=bool(dirty and params.get("on_dirty") == "discard"),
                active_simulation_nodes=tuple(node.id for node in config.nodes if node.active)
                if config
                else (),
                auto_start_test_modules=tuple(
                    module.id
                    for env in config.tests.environments
                    for module in env.modules
                    if module.enabled and module.start_on_measurement
                )
                if config
                else (),
                blocked_by=block,
                notes=(
                    "FAKE backend; no COM or hardware evidence.",
                    "Configuration.Modified does not cover every change (C5).",
                ),
            )
        )

    def simulate_gui_open(self, path: str) -> None:
        """Harness-only OnOpen simulation; invalidates the same IDs as a real event."""
        self._config = self._configs[path]
        self._epoch += 1
        self._fingerprints.clear()
        self._updated = self._clock()

    def simulate_edit(self, **changes: object) -> None:
        """Harness-only operator edit, including same-epoch collection reorder."""
        self._config = replace(self._need_config(), **changes)
        self._updated = self._clock()

    def _issued[T: _Identified](self, items: tuple[T, ...]) -> tuple[T, ...]:
        for item in items:
            identifier = item.id
            if "@" in identifier:
                self._fingerprints[identifier] = items
        return items

    def _find[T: _Identified](self, items: tuple[T, ...], identifier: str) -> T:
        if "@" in identifier and self._fingerprints.get(identifier) != items:
            raise c.BackendError(c.ErrorCode.STALE_SESSION, "Fake collection fingerprint changed")
        matches = [item for item in items if item.id == identifier]
        if not matches:
            raise c.BackendError(c.ErrorCode.NOT_FOUND, "Unknown fake qualified ID")
        if len(matches) != 1:
            raise c.BackendError(c.ErrorCode.AMBIGUOUS_ID, "Ambiguous fake qualified ID")
        return matches[0]

    def summary(self, section: c.SummarySection) -> c.Observed[c.ConfigSummary]:
        config = self._need_config()
        return self._observed(
            c.ConfigSummary(
                config.path,
                buses=config.buses if section in {"all", "networks"} else (),
                databases=self._issued(config.databases) if section in {"all", "databases"} else (),
                nodes=self._issued(config.nodes) if section in {"all", "nodes"} else (),
                diag_descriptions=self._issued(config.diagnostics)
                if section in {"all", "diagnostics"}
                else (),
                test_setup=self.test_setup().value if section in {"all", "tests"} else None,
            )
        )

    def databases(self) -> c.Observed[tuple[c.DatabaseInfo, ...]]:
        return self._observed(self._issued(self._need_config().databases))

    def buses(self) -> c.Observed[tuple[c.BusInfo, ...]]:
        return self._observed(self._issued(self._need_config().buses))

    def nodes(self) -> c.Observed[tuple[c.NodeInfo, ...]]:
        return self._observed(self._issued(self._need_config().nodes))

    def diag_descriptions(self) -> c.Observed[tuple[c.DiagDescriptionInfo, ...]]:
        return self._observed(self._issued(self._need_config().diagnostics))

    def test_setup(self) -> c.Observed[c.TestSetupInfo]:
        setup = self._need_config().tests
        self._issued(setup.environments)
        self._issued(tuple(module for env in setup.environments for module in env.modules))
        return self._observed(setup)

    def set_database_channel(
        self, database_id: str, channel: int, ctx: c.CallContext
    ) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            if type(channel) is not int or channel < 1:
                raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "Channel must be positive")
            self._find(self._need_config().databases, database_id)

        def effect() -> c.DatabaseInfo:
            config = self._need_config()
            old = self._find(config.databases, database_id)
            new = replace(old, channel=channel)
            self._config = replace(
                config,
                modified=True,
                databases=tuple(new if item == old else item for item in config.databases),
            )
            return new

        return self._submit("database.set_channel", ctx, effect, check)

    def set_node_active(self, node_id: str, active: bool, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            self._find(self._need_config().nodes, node_id)

        def effect() -> c.NodeInfo:
            config = self._need_config()
            old = self._find(config.nodes, node_id)
            new = replace(old, active=active)
            # C5: this edit does NOT set Modified in CANoe 17.
            self._config = replace(
                config, nodes=tuple(new if item == old else item for item in config.nodes)
            )
            return new

        return self._submit("node.set_active", ctx, effect, check)

    def add_diag_description(
        self,
        network: str,
        path: str,
        ecu_identifier: str | None,
        open_console: bool,
        ctx: c.CallContext,
    ) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            if Path(path).suffix.lower() == ".cdd" and ecu_identifier is not None:
                raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "CDD forbids ecu_identifier")
            if any(
                item.network.casefold() == network.casefold()
                and str(Path(item.file_path)).casefold() == str(Path(path)).casefold()
                for item in self._need_config().diagnostics
            ):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Description already loaded")

        def effect() -> c.DiagDescriptionInfo:
            config = self._need_config()
            qualifier = ecu_identifier or Path(path).stem
            existing = {entry.qualifier for entry in config.diagnostics}
            base = qualifier
            suffix = 1
            while qualifier in existing:
                qualifier = f"{base}_{suffix}"
                suffix += 1
            item = c.DiagDescriptionInfo(
                "diag:" + c.escape_id_segment(qualifier), qualifier, network, None, path, "tester"
            )
            self._config = replace(config, modified=True, diagnostics=config.diagnostics + (item,))
            return item

        return self._submit("diag_description.add", ctx, effect, check)

    def remove_diag_description(self, diag_id: str, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            self._find(self._need_config().diagnostics, diag_id)

        def effect() -> c.Removed:
            config = self._need_config()
            item = self._find(config.diagnostics, diag_id)
            self._config = replace(
                config,
                modified=True,
                diagnostics=tuple(entry for entry in config.diagnostics if entry != item),
            )
            return c.Removed(diag_id)

        return self._submit("diag_description.remove", ctx, effect, check)

    def diag_windows(
        self, diag_id: str, window: c.DiagWindow, open_: bool, ctx: c.CallContext
    ) -> c.OperationStatus:
        def check() -> None:
            if window not in {"console", "session", "fault_memory", "all"}:
                raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "Unknown diagnostic window")
            self._find(self._need_config().diagnostics, diag_id)

        return self._submit(
            "diag_description.open_windows" if open_ else "diag_description.close_windows",
            ctx,
            lambda: None,
            check,
        )

    def compile(self, ctx: c.CallContext) -> c.OperationStatus:
        return self._submit(
            "compile",
            ctx,
            lambda: c.CompileResult(
                self._need_config().compile_success,
                None if self._need_config().compile_success else "Fake compile error",
            ),
            self._stopped,
        )

    def measurement_start(self, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            if not self._need_config().compile_success:
                raise c.BackendError(c.ErrorCode.CANOE_REJECTED, "Fake compile errors")

        def effect() -> None:
            self._licence()
            self._measurement = True

        return self._submit("measurement.start", ctx, effect, check)

    def measurement_stop(self, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._need_config()

        def effect() -> None:
            self._licence()
            self._measurement = False

        return self._submit("measurement.stop", ctx, effect, check)

    def write_window(self, max_chars: int) -> c.Observed[c.WriteWindowText]:
        self._need_config()
        if type(max_chars) is not int or not 0 < max_chars <= 100_000:
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "max_chars must be 1..100000")
        return self._observed(
            c.WriteWindowText(self._text[:max_chars], len(self._text) > max_chars)
        )

    def clear_write_window(self, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._need_config()

        def effect() -> None:
            self._text = ""

        return self._submit("write_window.clear", ctx, effect, check)

    def shutdown(self) -> None:
        for operation_id in tuple(self._queue):
            self.cancel(operation_id)
        self._connected = False
        self._epoch += 1

    def _unsupported(self) -> Never:
        raise c.BackendError(
            c.ErrorCode.CAPABILITY_UNAVAILABLE, "Not implemented in first-slice fake"
        )

    def add_database(
        self, path: str, bus: str, channel: int, ctx: c.CallContext
    ) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            if any(item.path == path and item.bus == bus for item in self._need_config().databases):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Database already loaded")

        def effect() -> c.DatabaseInfo:
            config = self._need_config()
            name = Path(path).stem
            item = c.DatabaseInfo(
                "db:" + c.escape_id_segment(bus) + "/" + c.escape_id_segment(name),
                name, path, channel, bus,
            )
            if any(entry.id == item.id for entry in config.databases):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Database name already loaded")
            self._config = replace(config, modified=True, databases=config.databases + (item,))
            return item

        return self._submit("database.add", ctx, effect, check)

    def remove_database(self, database_id: str, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            self._find(self._need_config().databases, database_id)

        def effect() -> c.Removed:
            config = self._need_config()
            item = self._find(config.databases, database_id)
            self._config = replace(
                config, modified=True, databases=tuple(x for x in config.databases if x != item)
            )
            return c.Removed(database_id)

        return self._submit("database.remove", ctx, effect, check)

    def add_bus(self, name: str, bus_type: Literal["CAN"], ctx: c.CallContext) -> c.OperationStatus:
        self._unsupported()

    def remove_bus(self, bus_id: str, ctx: c.CallContext) -> c.OperationStatus:
        self._unsupported()

    def add_node(
        self, name: str, bus: str, capl_path: str | None, ctx: c.CallContext
    ) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            if any(item.name == name for item in self._need_config().nodes):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Node already exists")

        def effect() -> c.NodeInfo:
            config = self._need_config()
            item = c.NodeInfo("node:" + c.escape_id_segment(name), name, True, capl_path, (bus,))
            self._config = replace(config, modified=True, nodes=config.nodes + (item,))
            return item

        return self._submit("node.add", ctx, effect, check)

    def remove_node(self, node_id: str, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            self._find(self._need_config().nodes, node_id)

        def effect() -> c.Removed:
            config = self._need_config()
            item = self._find(config.nodes, node_id)
            self._config = replace(
                config, modified=True, nodes=tuple(x for x in config.nodes if x != item)
            )
            return c.Removed(node_id)

        return self._submit("node.remove", ctx, effect, check)

    def attach_node_bus(
        self, node_id: str, bus: str, attach: bool, ctx: c.CallContext
    ) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            self._find(self._need_config().nodes, node_id)

        def effect() -> c.NodeInfo:
            config = self._need_config()
            old = self._find(config.nodes, node_id)
            buses = old.buses + (bus,) if attach and bus not in old.buses else old.buses
            if not attach:
                buses = tuple(name for name in buses if name != bus)
            new = replace(old, buses=buses)
            self._config = replace(
                config, modified=True, nodes=tuple(new if x == old else x for x in config.nodes)
            )
            return new

        return self._submit("node.attach_bus" if attach else "node.detach_bus", ctx, effect, check)

    def can_controller(self, bus: str, channel: int) -> c.Observed[c.CanControllerInfo]:
        self._need_config()
        if not bus or type(channel) is not int or channel < 1:
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "Invalid controller selector")
        return self._observed(c.CanControllerInfo(bus, channel, 500_000, 500.0))

    def set_can_bitrate(
        self, bus: str, channel: int, bitrate_bps: int, ctx: c.CallContext
    ) -> c.OperationStatus:
        self._unsupported()

    def add_test_environment(self, tse_path: str, ctx: c.CallContext) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            if any(env.path == tse_path for env in self._need_config().tests.environments):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Environment already exists")

        def effect() -> c.TestEnvironmentInfo:
            config = self._need_config()
            name = Path(tse_path).stem
            item = c.TestEnvironmentInfo("env:" + c.escape_id_segment(name), name, tse_path, True)
            if any(env.id == item.id for env in config.tests.environments):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Environment name already exists")
            self._config = replace(
                config, modified=True,
                tests=replace(config.tests, environments=config.tests.environments + (item,)),
            )
            return item

        return self._submit("test_setup.add_environment", ctx, effect, check)

    def add_test_module(
        self, environment_id: str, can_path: str, ctx: c.CallContext
    ) -> c.OperationStatus:
        def check() -> None:
            self._stopped()
            env = self._find(self._need_config().tests.environments, environment_id)
            if any(module.path == can_path for module in env.modules):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Module already exists")

        def effect() -> c.TestModuleInfo:
            config = self._need_config()
            env = self._find(config.tests.environments, environment_id)
            name = Path(can_path).stem
            item = c.TestModuleInfo(
                "tm:" + c.escape_id_segment(env.name) + "/" + c.escape_id_segment(name),
                name, can_path, True, False,
            )
            if any(module.id == item.id for module in env.modules):
                raise c.BackendError(c.ErrorCode.ALREADY_EXISTS, "Module name already exists")
            new = replace(env, modules=env.modules + (item,))
            self._config = replace(
                config, modified=True,
                tests=replace(config.tests, environments=tuple(
                    new if x == env else x for x in config.tests.environments
                )),
            )
            return item

        return self._submit("test_setup.add_module", ctx, effect, check)

    def set_test_module_enabled(
        self, module_id: str, enabled: bool, ctx: c.CallContext
    ) -> c.OperationStatus:
        def modules() -> tuple[c.TestModuleInfo, ...]:
            return tuple(m for env in self._need_config().tests.environments for m in env.modules)

        def check() -> None:
            self._stopped()
            self._find(modules(), module_id)

        def effect() -> c.TestModuleInfo:
            config = self._need_config()
            old = self._find(modules(), module_id)
            new = replace(old, enabled=enabled)
            environments = tuple(replace(env, modules=tuple(
                new if m == old else m for m in env.modules
            )) for env in config.tests.environments)
            self._config = replace(
                config, modified=True, tests=replace(config.tests, environments=environments)
            )
            return new

        return self._submit("test_setup.set_enabled", ctx, effect, check)

    def start_test_run(self, spec: c.TestRunSpec, ctx: c.CallContext) -> c.TestRunStatus:
        self._unsupported()

    def test_run(self, run_id: str) -> c.Observed[c.TestRunStatus]:
        self._unsupported()

    def stop_test_run(self, run_id: str) -> c.TestRunStatus:
        self._unsupported()

    def test_report(self, ref: c.ReportRef) -> c.TestReportLocation:
        self._unsupported()

    def start_diag_request(
        self, spec: c.DiagRequestSpec, ctx: c.CallContext
    ) -> c.DiagRequestStatus:
        self._unsupported()

    def diag_request(self, operation_id: str) -> c.Observed[c.DiagRequestStatus]:
        self._unsupported()

    def tester_present(self, network: str, ecu: str) -> c.Observed[c.TesterPresentInfo]:
        self._unsupported()

    def set_tester_present(
        self, network: str, ecu: str, on: bool, ctx: c.CallContext
    ) -> c.OperationStatus:
        self._unsupported()

    def get_value(self, sel: c.ValueSelector, raw: bool) -> c.Observed[c.ValueReading]:
        self._unsupported()

    def set_value(
        self, sel: c.ValueSelector, value: c.Value, raw: bool, ctx: c.CallContext
    ) -> c.OperationStatus:
        self._unsupported()

    def call_capl(
        self, name: str, args: tuple[int | float, ...], ctx: c.CallContext
    ) -> c.OperationStatus:
        self._unsupported()

    def helper_status(self) -> c.Observed[c.HelperStatus]:
        return self._observed(c.HelperStatus(False, detail="First-slice fake has no helper"))

    def install_helper(self, ctx: c.CallContext) -> c.OperationStatus:
        self._unsupported()

    def send_can_frame(self, frame: c.CanFrame, ctx: c.CallContext) -> c.OperationStatus:
        self._unsupported()
