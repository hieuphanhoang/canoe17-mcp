"""COM backend implementing ``contracts.Backend`` for CANoe 17.

First slice: session, open/save/quit, compile, measurement start/stop,
configuration reads, database channel, diagnostic descriptions and the Write
window. Everything else returns ``CAPABILITY_UNAVAILABLE`` (not implemented)
without touching CANoe.

Caller threads only build jobs and read the state store. Every COM call runs
on the STA worker (``worker.py``).
"""

from __future__ import annotations

import logging
import shutil
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from canoe17_mcp.com.errors import classify
from canoe17_mcp.com.lock import SessionLock
from canoe17_mcp.com.session import ComSession, late
from canoe17_mcp.com.state import StateStore
from canoe17_mcp.com.worker import PENDING, StaWorker, StepContext
from canoe17_mcp.contracts import (
    REAL_EVIDENCE_ORDER,
    Availability,
    BackendError,
    BackendSettings,
    BlockReason,
    BusInfo,
    CallContext,
    CanControllerInfo,
    CanFrame,
    Capability,
    CompileResult,
    ConfigSummary,
    DatabaseInfo,
    DiagDescriptionInfo,
    DiagRequestSpec,
    DiagRequestStatus,
    DiagWindow,
    DirtyPolicy,
    EffectPreview,
    EffectRequest,
    ErrorCode,
    ErrorInfo,
    Evidence,
    HelperStatus,
    NodeInfo,
    Observed,
    OpenResult,
    OperationKind,
    OperationStatus,
    OpState,
    Removed,
    ReportRef,
    SaveResult,
    SessionStatus,
    SummarySection,
    Support,
    TesterPresentInfo,
    TestReportLocation,
    TestRunSpec,
    TestRunStatus,
    TestSetupInfo,
    Value,
    ValueReading,
    ValueSelector,
    WriteWindowText,
)

log = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_S = 0.5
DIAG_WINDOW_CODES: dict[str, int | None] = {
    "console": 1,
    "session": 2,
    "fault_memory": 4,
    "all": None,
}


def load_evidence() -> dict[str, Capability]:
    text = resources.files("canoe17_mcp.com").joinpath("evidence.toml").read_text("utf-8")
    data = tomllib.loads(text)
    out = {}
    for name, row in data.get("operations", {}).items():
        out[name] = Capability(
            operation=name,
            support=Support(row["support"]),
            evidence=Evidence(row["evidence"]),
            evidence_ref=row.get("ref"),
            note=row.get("note"),
        )
    return out


@dataclass
class _Watcher:
    operation_id: str
    kind: Literal["measurement.start", "measurement.stop"]
    deadline: float


def _com_error_handler(store: StateStore) -> Any:
    def handle(exc: BaseException, action: str) -> ErrorInfo | None:
        try:
            import pywintypes
        except ImportError:  # pragma: no cover - non-Windows
            return None
        if not isinstance(exc, pywintypes.com_error):
            return None
        hresult, _text, excepinfo = (tuple(exc.args) + (None, None, None))[:3]
        info = classify(int(hresult or 0), excepinfo, action=action)
        if info.code is ErrorCode.LICENSE_REQUIRED:
            store.update_session(licensed=False)
        return info

    return handle


class ComBackend:
    """The real backend. Construct, use from any thread, then ``shutdown()``."""

    backend_name = "com"

    def __init__(self, settings: BackendSettings, *, lock_dir: Path | None = None) -> None:
        self.settings = settings
        self.store = StateStore()
        self.session = ComSession()
        self.lock = SessionLock(settings.lock_key, sidecar_dir=lock_dir)
        self._evidence = load_evidence()
        self._watchers: dict[str, _Watcher] = {}
        self._seen: set[str] = set()
        self._next_snapshot = 0.0
        self.worker = StaWorker(self.store, com_error=_com_error_handler(self.store))
        self.worker.add_loop_hook(self._on_loop)
        self.worker.add_shutdown_hook(self._on_shutdown)
        self.worker.start()

    # ================================================================ helpers

    def _bounded_wait(self, wait_s: float | None) -> float:
        value = self.settings.operation_default_timeout_s if wait_s is None else wait_s
        return max(0.0, min(value, self.settings.operation_max_timeout_s))

    def _submit(
        self,
        kind: OperationKind,
        fn: Any,
        ctx: CallContext | None,
        *,
        step_s: float | None = None,
        lane: Literal["control", "normal"] = "normal",
    ) -> OperationStatus:
        def job(step: StepContext) -> Any:
            try:
                return fn(step)
            finally:
                # A caller polling status right after must see this mutation's effect.
                if self.session.connected:
                    try:
                        self.store.update_session(**self.session.cheap_fields())
                    except Exception:  # noqa: BLE001 - the loop refresh will report it
                        pass

        return self.worker.submit_operation(
            kind,
            job,
            expected_epoch=None if ctx is None else ctx.expected_epoch,
            step_s=self.settings.quick_step_s if step_s is None else step_s,
            dispatch_timeout_s=self.settings.dispatch_timeout_s,
            lane=lane,
        )

    def _not_implemented(self, kind: OperationKind) -> OperationStatus:
        op = self.store.new_operation(kind, self.store.epoch)
        self.store.cancel_queued(
            op.operation_id,
            ErrorInfo(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                f"{kind} is not implemented in this backend yet.",
                details=(("blocked_by", BlockReason.UNSUPPORTED.value),),
            ),
        )
        return self.store.get(op.operation_id) or op

    def _require_connected(self) -> None:
        if not self.session.connected:
            raise BackendError(ErrorCode.NOT_CONNECTED, "Not connected to CANoe.")

    def _read(self, name: str, fn: Any) -> Observed[Any]:
        """Live read on the worker; cached snapshot (with its age) when busy."""
        epoch_before = self.store.epoch
        try:
            value = self.worker.call(
                lambda step: (self._require_connected(), fn(self.store.epoch))[1],
                wait_s=min(2.0, self.settings.dispatch_timeout_s),
                step_s=self.settings.quick_step_s,
            )
        except BackendError as exc:
            if exc.code is ErrorCode.BUSY:
                hit = self.store.get_cache(name)
                if hit is not None:
                    epoch, age, cached = hit
                    return self.store.observed(cached, epoch=epoch, age_s=age)
            raise
        epoch = self.store.epoch
        if epoch == epoch_before:
            self.store.put_cache(name, value, epoch)
        return self.store.observed(value, epoch=epoch)

    # ======================================================= worker callbacks

    def _connect_on_worker(self, step: StepContext, *, launch: bool) -> bool:
        """Attach (or launch) and take the session lock. Returns attached."""
        if self.session.connected:
            return True
        if not self.lock.acquire(self.settings.lock_acquire_timeout_s):
            holder = self.lock.holder_pid()
            self.store.update_session(lock_held=False, lock_holder_pid=holder)
            raise BackendError(
                ErrorCode.LOCKED_BY_OTHER_SERVER,
                f"Another canoe17-mcp server (pid {holder}) owns the CANoe session.",
                details=(("holder_pid", holder or 0),),
            )
        try:
            if launch:
                step.dispatch("launching")
            attached = self.session.attach(launch=launch and self.settings.allow_launch)
        except BaseException:
            self.lock.release()
            raise
        self.store.update_session(
            connected=True, lock_held=True, lock_holder_pid=None, **self.session.session_fields()
        )
        self.store.bump_epoch()
        return attached

    def _disconnect_on_worker(self) -> None:
        self.session.detach()
        if self.lock.held:
            self.lock.release()
        self.store.update_session(
            connected=False,
            lock_held=False,
            configuration_path=None,
            configuration_modified=None,
            measurement_running=None,
        )

    def _on_loop(self) -> None:
        for _, name, args in self.session.inbox.drain():
            self._handle_event(name, args)
        now = time.monotonic()
        for watcher in list(self._watchers.values()):
            self._advance(watcher, now)
        if self.session.connected and now >= self._next_snapshot:
            self._next_snapshot = now + SNAPSHOT_INTERVAL_S
            try:
                self.store.update_session(**self.session.cheap_fields())
            except Exception as exc:  # noqa: BLE001
                info = _com_error_handler(self.store)(exc, "status refresh")
                if info is not None and info.code is ErrorCode.NOT_CONNECTED:
                    log.warning("lost CANoe connection")
                    self._disconnect_on_worker()
                    self.store.bump_epoch()

    def _handle_event(self, name: str, args: tuple[Any, ...]) -> None:
        if name == "App.OnOpen":
            path = args[0] if args else ""
            if not self.session.is_own_open(path):
                self.store.bump_epoch()
                self.session.ids.clear()
            self.store.update_session(configuration_path=path or None)
        elif name == "App.OnQuit":
            self._disconnect_on_worker()
            self.store.bump_epoch()
        elif name == "Meas.OnStart":
            self.store.update_session(measurement_running=True)
            self._seen.add("start")
        elif name == "Meas.OnStop":
            self.store.update_session(measurement_running=False)
            self._seen.add("stop")

    def _advance(self, w: _Watcher, now: float) -> None:
        flag = "start" if w.kind == "measurement.start" else "stop"
        op = self.store.get(w.operation_id)
        if op is None:
            self._watchers.pop(w.operation_id, None)
            return
        if flag in self._seen:
            self._seen.discard(flag)
            self._watchers.pop(w.operation_id, None)
            self.store.finish(w.operation_id, OpState.COMPLETED)
            return
        if now > w.deadline and op.state in (OpState.RUNNING, OpState.STOPPING):
            self.store.mark_unknown(
                w.operation_id,
                ErrorInfo(
                    ErrorCode.OUTCOME_UNKNOWN,
                    f"No {'OnStart' if flag == 'start' else 'OnStop'} event within the "
                    "deadline. Poll this operation and the measurement status.",
                ),
            )

    def _on_shutdown(self) -> None:
        self._disconnect_on_worker()

    # ================================================================ session

    def status(self) -> SessionStatus:
        st = self.store.status(self.backend_name)
        if not st.connected and st.lock_holder_pid is None:
            holder = self.lock.holder_pid()
            if holder is not None:
                return SessionStatus(**{**_asdict(st), "lock_holder_pid": holder})
        return st

    def capabilities(self) -> tuple[Capability, ...]:
        return tuple(self._evidence.values())

    def availability(self) -> Observed[tuple[Availability, ...]]:
        st = self.store.status(self.backend_name)
        floor = REAL_EVIDENCE_ORDER.index(self.settings.min_evidence) if (
            self.settings.min_evidence in REAL_EVIDENCE_ORDER
        ) else len(REAL_EVIDENCE_ORDER)
        out = []
        for cap in self._evidence.values():
            reason: BlockReason | None = None
            detail = None
            if cap.support is not Support.IMPLEMENTED:
                reason = BlockReason.UNSUPPORTED
            elif cap.evidence not in REAL_EVIDENCE_ORDER or REAL_EVIDENCE_ORDER.index(
                cap.evidence
            ) < floor:
                reason, detail = BlockReason.BELOW_EVIDENCE_FLOOR, cap.evidence.value
            elif st.degraded and cap.operation not in ("summary",) and not cap.operation.endswith(
                (".list", ".read")
            ):
                reason = BlockReason.DEGRADED
            elif not st.connected and cap.operation not in ("connect", "open_config"):
                reason = BlockReason.NOT_CONNECTED
            elif cap.operation in ("save_config", "measurement.start") and st.licensed is False:
                reason, detail = BlockReason.NO_LICENSE, "licence"
            out.append(Availability(cap.operation, reason is None, reason, detail))
        return self.store.observed(tuple(out))

    def connect(self) -> OperationStatus:
        def job(step: StepContext) -> None:
            self._connect_on_worker(step, launch=False)

        return self._submit("connect", job, None, step_s=self.settings.open_step_s)

    def open_config(
        self, path: str, on_dirty: DirtyPolicy, launch_if_absent: bool, ctx: CallContext
    ) -> OperationStatus:
        def job(step: StepContext) -> OpenResult:
            attached = self._connect_on_worker(step, launch=launch_if_absent)
            app = self.session.app
            cfg = app.Configuration
            modified = bool(cfg.Modified)  # pre-dispatch dirty check (contract rule 4)
            saved_to = None
            if modified and on_dirty == "refuse":
                raise BackendError(
                    ErrorCode.DIRTY_CONFIG,
                    "The open configuration has unsaved changes. Save it, or open with "
                    "on_dirty='save' or 'discard'.",
                    details=(("configuration", str(cfg.FullName)),),
                )
            if modified and on_dirty == "save":
                step.dispatch("saving_previous")
                cfg.Save()
                saved_to = str(cfg.FullName)
            if bool(app.Measurement.Running):
                raise BackendError(
                    ErrorCode.MEASUREMENT_RUNNING, "Stop the measurement before opening."
                )
            self.session.expect_own_open(path)
            step.dispatch("opening")
            app.Open(path, False, False)
            epoch = self.store.bump_epoch()
            self.session.ids.clear()
            self.store.update_session(**self.session.session_fields())
            return OpenResult(
                active_path=str(app.Configuration.FullName),
                attached=attached,
                discarded_changes=modified and on_dirty == "discard",
                saved_previous_to=saved_to,
                epoch_after=epoch,
            )

        step_s = self.settings.open_step_s + (
            self.settings.launch_step_s if launch_if_absent else 0.0
        )
        return self._submit("open_config", job, ctx, step_s=step_s)

    def save_config(self, as_path: str | None, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> SaveResult:
            self._require_connected()
            cfg = self.session.require_configuration()
            before = str(cfg.FullName)
            backup = None
            if as_path:
                target = Path(as_path)
                if target.exists():
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    backup = str(target.with_name(f"{target.name}.bak-{stamp}"))
                    shutil.copy2(target, backup)
                self.session.expect_own_open(as_path)
                step.dispatch("saving")
                cfg.Save(as_path, False)
            else:
                step.dispatch("saving")
                cfg.Save()
            active = str(self.session.app.Configuration.FullName)
            changed = active.lower() != before.lower()
            epoch = self.store.bump_epoch() if changed else self.store.epoch
            self.store.update_session(**self.session.cheap_fields())
            return SaveResult(
                saved_path=as_path or before,
                active_path=active,
                active_changed=changed,
                backup_path=backup,
                epoch_after=epoch,
            )

        return self._submit("save_config", job, ctx, step_s=self.settings.save_step_s)

    def quit(self, on_dirty: DirtyPolicy, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> None:
            self._require_connected()
            app = self.session.app
            if bool(app.Configuration.Modified):
                if on_dirty == "refuse":
                    raise BackendError(
                        ErrorCode.DIRTY_CONFIG, "Unsaved changes; quit refused (on_dirty=refuse)."
                    )
                if on_dirty == "save":
                    step.dispatch("saving")
                    app.Configuration.Save()
            step.dispatch("quitting")
            app.Quit()
            self._disconnect_on_worker()
            self.store.bump_epoch()

        return self._submit("quit", job, ctx, step_s=self.settings.open_step_s)

    def operation(self, operation_id: str) -> Observed[OperationStatus]:
        op = self.store.get(operation_id)
        if op is None:
            raise BackendError(ErrorCode.NOT_FOUND, f"Unknown operation {operation_id}.")
        return self.store.observed(op)

    def wait(self, operation_id: str, wait_s: float) -> Observed[OperationStatus]:
        op = self.store.wait(operation_id, self._bounded_wait(wait_s))
        if op is None:
            raise BackendError(ErrorCode.NOT_FOUND, f"Unknown operation {operation_id}.")
        return self.store.observed(op)

    def cancel(self, operation_id: str) -> OperationStatus:
        op = self.store.get(operation_id)
        if op is None:
            raise BackendError(ErrorCode.NOT_FOUND, f"Unknown operation {operation_id}.")
        if op.state is OpState.QUEUED:
            self.worker.cancel_queued(
                operation_id, ErrorInfo(ErrorCode.DEADLINE_EXCEEDED, "Cancelled before dispatch.")
            )
        return self.store.get(operation_id) or op

    # ================================================================ previews

    def preview(self, request: EffectRequest) -> Observed[EffectPreview]:
        params = dict(request.params)
        st = self.store.status(self.backend_name)
        notes: list[str] = []
        launches = False
        discards = False
        overwrites: tuple[str, ...] = ()
        blocked: BlockReason | None = None
        if request.action == "open_config":
            on_dirty = params.get("on_dirty", "refuse")
            launches = not st.connected and bool(params.get("launch_if_absent", True))
            if st.configuration_modified:
                if on_dirty == "refuse":
                    blocked = BlockReason.MISSING_PREREQUISITE
                    notes.append("The open configuration has unsaved changes; refused.")
                discards = on_dirty == "discard"
            notes.append(
                "CANoe tracks most but not all changes as unsaved (Node.Active is not); "
                "changes it does not track are lost on open."
            )
        elif request.action == "save_config":
            as_path = params.get("as_path")
            if isinstance(as_path, str) and Path(as_path).exists():
                overwrites = (as_path,)
                notes.append("The existing file is backed up before it is overwritten.")
        if request.action in ("save_config", "measurement.start") and st.licensed is False:
            blocked = BlockReason.NO_LICENSE
        if st.degraded:
            blocked = BlockReason.DEGRADED
        preview = EffectPreview(
            action=request.action,
            affected=tuple(str(v) for k, v in request.params if k.endswith(("_id", "path"))),
            launches_canoe=launches,
            overwrites=overwrites,
            configuration_path=st.configuration_path,
            configuration_modified=st.configuration_modified,
            discards_changes=discards,
            blocked_by=blocked,
            notes=tuple(notes),
        )
        return self.store.observed(preview, epoch=st.epoch, age_s=st.snapshot_age_s)

    # ================================================================== reads

    def summary(self, section: SummarySection) -> Observed[ConfigSummary]:
        return self._read(f"summary:{section}", lambda e: self.session.summary(e, section))

    def databases(self) -> Observed[tuple[DatabaseInfo, ...]]:
        return self._read("databases", self.session.databases)

    def buses(self) -> Observed[tuple[BusInfo, ...]]:
        return self._read("buses", self.session.buses)

    def nodes(self) -> Observed[tuple[NodeInfo, ...]]:
        return self._read("nodes", self.session.nodes)

    def diag_descriptions(self) -> Observed[tuple[DiagDescriptionInfo, ...]]:
        return self._read("diag_descriptions", self.session.diag_descriptions)

    def test_setup(self) -> Observed[TestSetupInfo]:
        return self._read("test_setup", self.session.test_setup)

    def can_controller(self, bus: str, channel: int) -> Observed[CanControllerInfo]:
        raise BackendError(ErrorCode.CAPABILITY_UNAVAILABLE, "can_controller is not implemented.")

    def write_window(self, max_chars: int) -> Observed[WriteWindowText]:
        def read(epoch: int) -> WriteWindowText:
            text = self.session.write_window_text()
            truncated = len(text) > max_chars
            return WriteWindowText(text[-max_chars:] if truncated else text, truncated)

        return self._read(f"write:{max_chars}", read)

    # ======================================================= config mutations

    def set_database_channel(
        self, database_id: str, channel: int, ctx: CallContext
    ) -> OperationStatus:
        def job(step: StepContext) -> DatabaseInfo:
            self._require_connected()
            objs = self.session.database_objects()
            entries = self.session.database_entries()
            idx = self.session.ids.resolve("databases", self.store.epoch, entries, database_id)
            step.dispatch("setting_channel")
            objs[idx][0].Channel = channel
            return self.session.databases(self.store.epoch)[idx]

        return self._submit("database.set_channel", job, ctx)

    def add_diag_description(
        self,
        network: str,
        path: str,
        ecu_identifier: str | None,
        open_console: bool,
        ctx: CallContext,
    ) -> OperationStatus:
        def job(step: StepContext) -> DiagDescriptionInfo:
            self._require_connected()
            if bool(self.session.app.Measurement.Running):
                raise BackendError(
                    ErrorCode.MEASUREMENT_RUNNING,
                    "Diagnostic descriptions can only be added while the measurement is stopped.",
                )
            for d in self.session.diag_descriptions(self.store.epoch):
                if d.network.lower() == network.lower() and (
                    d.file_path.lower() == str(Path(path)).lower()
                ):
                    raise BackendError(
                        ErrorCode.ALREADY_EXISTS,
                        f"{path} is already loaded on {network} as {d.id}.",
                    )
            cfg = self.session.require_configuration()
            descs = cfg.GeneralSetup.DiagnosticsSetup.DiagDescriptions
            step.dispatch("adding")
            if ecu_identifier:
                descs.Add(network, path, ecu_identifier)
            else:
                descs.Add(network, path)
            infos = self.session.diag_descriptions(self.store.epoch)
            added = infos[-1]
            if open_console:
                step.phase("opening_console")
                late(self.session.diag_objects()[-1]).OpenWindows(1)
            return added

        return self._submit("diag_description.add", job, ctx)

    def remove_diag_description(self, diag_id: str, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> Removed:
            self._require_connected()
            if bool(self.session.app.Measurement.Running):
                raise BackendError(
                    ErrorCode.MEASUREMENT_RUNNING,
                    "Diagnostic descriptions can only be removed while the measurement is stopped.",
                )
            entries = self.session.diag_entries()
            idx = self.session.ids.resolve("diag_descriptions", self.store.epoch, entries, diag_id)
            cfg = self.session.require_configuration()
            step.dispatch("removing")
            cfg.GeneralSetup.DiagnosticsSetup.DiagDescriptions.Remove(idx + 1)
            self.session.ids.clear()
            return Removed(diag_id)

        return self._submit("diag_description.remove", job, ctx)

    def diag_windows(
        self, diag_id: str, window: DiagWindow, open_: bool, ctx: CallContext
    ) -> OperationStatus:
        def job(step: StepContext) -> None:
            self._require_connected()
            entries = self.session.diag_entries()
            idx = self.session.ids.resolve("diag_descriptions", self.store.epoch, entries, diag_id)
            desc = late(self.session.diag_objects()[idx])
            code = DIAG_WINDOW_CODES[window]
            step.dispatch("windows")
            method = desc.OpenWindows if open_ else desc.CloseWindows
            if code is None:
                method()
            else:
                method(code)

        kind: OperationKind = (
            "diag_description.open_windows" if open_ else "diag_description.close_windows"
        )
        return self._submit(kind, job, ctx)

    def compile(self, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> CompileResult:
            self._require_connected()
            self.session.require_configuration()
            capl = self.session.app.CAPL
            step.dispatch("compiling")
            capl.Compile()
            result = capl.CompileResult
            code = int(result.result)
            return CompileResult(
                success=code == 0,
                error_message=str(result.ErrorMessage) or None,
                node_name=str(result.NodeName) or None,
                source_file=str(getattr(result, "SourceFile", "") or "") or None,
            )

        return self._submit("compile", job, ctx, step_s=self.settings.compile_step_s)

    # ============================================================ measurement

    def measurement_start(self, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> Any:
            self._require_connected()
            self.session.require_configuration()
            meas = self.session.app.Measurement
            if bool(meas.Running):
                raise BackendError(ErrorCode.MEASUREMENT_RUNNING, "Measurement is already running.")
            self._seen.discard("start")
            step.dispatch("starting")
            meas.Start()
            assert step.operation_id is not None
            step.phase("awaiting_on_start")
            self._watchers[step.operation_id] = _Watcher(
                step.operation_id,
                "measurement.start",
                time.monotonic() + self.settings.operation_default_timeout_s,
            )
            return PENDING

        return self._submit("measurement.start", job, ctx)

    def measurement_stop(self, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> Any:
            self._require_connected()
            meas = self.session.app.Measurement
            if not bool(meas.Running):
                raise BackendError(ErrorCode.MEASUREMENT_NOT_RUNNING, "Measurement is not running.")
            self._seen.discard("stop")
            step.dispatch("stopping")
            meas.Stop()
            assert step.operation_id is not None
            step.phase("awaiting_on_stop")
            self._watchers[step.operation_id] = _Watcher(
                step.operation_id,
                "measurement.stop",
                time.monotonic() + self.settings.operation_default_timeout_s,
            )
            return PENDING

        return self._submit("measurement.stop", job, ctx, lane="control")

    def clear_write_window(self, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> None:
            self._require_connected()
            step.dispatch("clearing")
            self.session.app.UI.Write.Clear()

        return self._submit("write_window.clear", job, ctx)

    # ================================================= not in the first slice

    def add_database(self, path: str, bus: str, channel: int, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("database.add")

    def remove_database(self, database_id: str, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("database.remove")

    def add_bus(self, name: str, bus_type: Literal["CAN"], ctx: CallContext) -> OperationStatus:
        return self._not_implemented("bus.add")

    def remove_bus(self, bus_id: str, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("bus.remove")

    def add_node(
        self, name: str, bus: str, capl_path: str | None, ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("node.add")

    def remove_node(self, node_id: str, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("node.remove")

    def set_node_active(self, node_id: str, active: bool, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("node.set_active")

    def attach_node_bus(
        self, node_id: str, bus: str, attach: bool, ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("node.attach_bus" if attach else "node.detach_bus")

    def set_can_bitrate(
        self, bus: str, channel: int, bitrate_bps: int, ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("can_controller.set_bitrate")

    def add_test_environment(self, tse_path: str, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("test_setup.add_environment")

    def add_test_module(
        self, environment_id: str, can_path: str, ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("test_setup.add_module")

    def set_test_module_enabled(
        self, module_id: str, enabled: bool, ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("test_setup.set_enabled")

    def start_test_run(self, spec: TestRunSpec, ctx: CallContext) -> TestRunStatus:
        raise BackendError(ErrorCode.CAPABILITY_UNAVAILABLE, "Test runs are not implemented yet.")

    def test_run(self, run_id: str) -> Observed[TestRunStatus]:
        raise BackendError(ErrorCode.NOT_FOUND, f"Unknown run {run_id}.")

    def stop_test_run(self, run_id: str) -> TestRunStatus:
        raise BackendError(ErrorCode.NOT_FOUND, f"Unknown run {run_id}.")

    def test_report(self, ref: ReportRef) -> TestReportLocation:
        raise BackendError(ErrorCode.CAPABILITY_UNAVAILABLE, "Reports are not implemented yet.")

    def start_diag_request(self, spec: DiagRequestSpec, ctx: CallContext) -> DiagRequestStatus:
        raise BackendError(
            ErrorCode.CAPABILITY_UNAVAILABLE, "Diagnostic requests are not implemented yet."
        )

    def diag_request(self, operation_id: str) -> Observed[DiagRequestStatus]:
        raise BackendError(ErrorCode.NOT_FOUND, f"Unknown diagnostic request {operation_id}.")

    def tester_present(self, network: str, ecu: str) -> Observed[TesterPresentInfo]:
        raise BackendError(ErrorCode.CAPABILITY_UNAVAILABLE, "Tester Present is not implemented.")

    def set_tester_present(
        self, network: str, ecu: str, on: bool, ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("tester_present.start" if on else "tester_present.stop")

    def get_value(self, sel: ValueSelector, raw: bool) -> Observed[ValueReading]:
        raise BackendError(ErrorCode.CAPABILITY_UNAVAILABLE, "Values are not implemented yet.")

    def set_value(
        self, sel: ValueSelector, value: Value, raw: bool, ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("value.set")

    def call_capl(
        self, name: str, args: tuple[int | float, ...], ctx: CallContext
    ) -> OperationStatus:
        return self._not_implemented("capl.call")

    def helper_status(self) -> Observed[HelperStatus]:
        return self.store.observed(HelperStatus(installed=False, detail="not implemented"))

    def install_helper(self, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("helper.install")

    def send_can_frame(self, frame: CanFrame, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("can_frame.send")

    # ================================================================ shutdown

    def shutdown(self) -> None:
        """Stop the worker; its shutdown hook detaches and releases the lock."""
        self.worker.stop()


def _asdict(obj: Any) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in obj.__dataclass_fields__}
