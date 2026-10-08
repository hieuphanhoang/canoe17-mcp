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
from canoe17_mcp.com.process import canoe_processes
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
    TestEnvironmentInfo,
    TesterPresentInfo,
    TestModuleInfo,
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


OPEN_SETTLE_S = 3.0
"""Quiet time after the last OnOpen before an open completes (api-evidence C6)."""
OPEN_EVENT_WAIT_S = 15.0
"""Longest wait for the first OnOpen after Open() returned (it came ~1.8 s later, C1)."""


@dataclass
class _OpenWatcher:
    """Completes open_config once CANoe's OnOpen events have settled.

    Every OnOpen bumps the epoch, ours or not (review R1): nothing is
    suppressed, so a GUI reopen always invalidates older previews. Waiting for
    the events to settle only makes the operation's reported epoch the one in
    force after CANoe's own (sometimes duplicated) OnOpen notifications.
    """

    operation_id: str
    deadline: float
    attached: bool
    discarded_changes: bool
    saved_previous_to: str | None
    backup_path: str | None
    settle_until: float | None = None


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
        self._watchers: dict[str, _Watcher | _OpenWatcher] = {}
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
        """Refused synchronously; no operation is created (contract rule 2)."""
        raise BackendError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            f"{kind} is not implemented in this backend yet.",
            details=(("blocked_by", BlockReason.UNSUPPORTED.value),),
        )

    def _require_connected(self) -> None:
        if not self.session.connected:
            raise BackendError(ErrorCode.NOT_CONNECTED, "Not connected to CANoe.")

    def _read(self, name: str, fn: Any) -> Observed[Any]:
        """Live read on the worker; cached snapshot (with its age) when busy.

        The epoch is captured on the worker together with the value. Only the
        worker bumps the epoch, so it cannot change while ``fn`` runs; reading
        it again on the caller afterwards could label old data with a newer
        session (review C1).

        Attach-on-read (AGENT-006 S1, PLAN.md section 4): when not connected, the
        read first attaches to an already running CANoe on the worker, under the
        session lock. It never launches, opens, saves or discards, and there is no
        fallback after an attach error. The epoch is captured after attaching.
        Status, availability and operation polling never come through here.
        """

        def on_worker(step: StepContext) -> tuple[Any, int]:
            if not self.session.connected:
                self._connect_on_worker(step, launch=False)
            epoch = self.store.epoch
            return fn(epoch), epoch

        step_s = self.settings.quick_step_s
        if not self.session.connected:
            step_s = max(step_s, self.settings.open_step_s)  # first attach may be slow
        try:
            value, epoch = self.worker.call(
                on_worker,
                wait_s=min(2.0, self.settings.dispatch_timeout_s),
                step_s=step_s,
            )
        except BackendError as exc:
            if exc.code is ErrorCode.BUSY:
                hit = self.store.get_cache(name)
                if hit is not None:
                    epoch, age, cached = hit
                    return self.store.observed(cached, epoch=epoch, age_s=age)
            raise
        self.store.put_cache(name, value, epoch)  # ignored if the epoch moved on
        return self.store.observed(value, epoch=epoch)

    def _bump_epoch(self, *, opening: bool = False) -> int:
        """New session state: invalidate IDs and watchers of the old one (review C4).

        ``opening=True`` only for the bumps an open in progress expects (its own
        Open, CANoe's OnOpen, a save-copy): open watchers survive those. Quit,
        connection loss and shutdown retire every watcher (review R4).
        """
        epoch = self.store.bump_epoch()
        self.session.ids.clear()
        self._seen.clear()
        for w in list(self._watchers.values()):
            if opening and isinstance(w, _OpenWatcher):
                continue  # an open in progress expects this bump; see _OpenWatcher
            kind = "open_config" if isinstance(w, _OpenWatcher) else w.kind
            # The session this operation acted on is gone; its confirming event can
            # no longer arrive, and events of the new session must not certify it.
            self._resolve_uncertain(
                w.operation_id,
                f"The CANoe session changed (epoch {self._op_epoch(w)} -> {epoch}) before "
                f"{kind} was confirmed. It may or may not have taken effect in the old "
                "session. Re-read the current state.",
            )
        return epoch

    def _op_epoch(self, w: _Watcher | _OpenWatcher) -> int | str:
        op = self.store.get(w.operation_id)
        return op.epoch if op is not None else "?"

    def _resolve_uncertain(self, operation_id: str, message: str) -> None:
        """Remove a watcher and end its operation as failed with an uncertain outcome."""
        self._watchers.pop(operation_id, None)
        op = self.store.get(operation_id)
        if op is None or op.state not in (
            OpState.RUNNING,
            OpState.STOPPING,
            OpState.OUTCOME_UNKNOWN,
        ):
            return
        self.store.finish(
            operation_id,
            OpState.FAILED,
            error=ErrorInfo(
                ErrorCode.OUTCOME_UNKNOWN, message, details=(("effects_possible", True),)
            ),
        )

    def _save_with_backup(self, step: StepContext, cfg: Any, target: str | None) -> str | None:
        """The only overwrite path (review C5). Back up the file that will be
        overwritten, refuse to save if the backup fails, then save. Returns the
        backup path, or None when nothing existed to overwrite."""
        dest = Path(target) if target else Path(str(cfg.FullName))
        backup = None
        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            candidate = dest.with_name(f"{dest.name}.bak-{stamp}")
            n = 1
            while candidate.exists():
                candidate = dest.with_name(f"{dest.name}.bak-{stamp}-{n}")
                n += 1
            try:
                shutil.copy2(dest, candidate)
            except OSError as exc:
                raise BackendError(
                    ErrorCode.INTERNAL,
                    f"Could not back up {dest} before saving ({exc}); nothing was saved.",
                ) from None
            backup = str(candidate)
        if target:
            step.dispatch("saving")
            cfg.Save(target, False)
        else:
            step.dispatch("saving")
            cfg.Save()
        return backup

    def _refuse_if_measuring(self, action: str) -> None:
        """Checked before any side effect of a configuration-level action (review C6)."""
        if bool(self.session.app.Measurement.Running):
            raise BackendError(
                ErrorCode.MEASUREMENT_RUNNING, f"Stop the measurement before {action}."
            )

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
        self._bump_epoch()
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
                    self._bump_epoch()

    def _handle_event(self, name: str, args: tuple[Any, ...]) -> None:
        if name == "App.OnOpen":
            path = args[0] if args else ""
            self._bump_epoch(opening=True)  # always bumps: ours or a GUI open (R1)
            self.store.update_session(configuration_path=path or None)
            settle = time.monotonic() + OPEN_SETTLE_S
            for w in self._watchers.values():
                if isinstance(w, _OpenWatcher):
                    w.settle_until = settle
        elif name == "App.OnQuit":
            self._disconnect_on_worker()
            self._bump_epoch()
        elif name == "Meas.OnStart":
            self.store.update_session(measurement_running=True)
            self._seen.add("start")
        elif name == "Meas.OnStop":
            self.store.update_session(measurement_running=False)
            self._seen.add("stop")

    def _advance(self, w: _Watcher | _OpenWatcher, now: float) -> None:
        if isinstance(w, _OpenWatcher):
            self._advance_open(w, now)
            return
        flag = "start" if w.kind == "measurement.start" else "stop"
        op = self.store.get(w.operation_id)
        if op is None:
            self._watchers.pop(w.operation_id, None)
            return
        if flag in self._seen and op.epoch == self.store.epoch:
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

    def _advance_open(self, w: _OpenWatcher, now: float) -> None:
        settled = w.settle_until is not None and now >= w.settle_until
        if not settled and now < w.deadline:
            return
        op = self.store.get(w.operation_id)
        if op is None or op.state not in (OpState.RUNNING, OpState.OUTCOME_UNKNOWN):
            self._watchers.pop(w.operation_id, None)
            return
        if w.settle_until is None:
            # Open() returned but no OnOpen arrived: still a new session.
            self._bump_epoch(opening=True)
        try:
            if not self.session.connected:
                raise BackendError(ErrorCode.NOT_CONNECTED, "CANoe connection lost.")
            fields = self.session.session_fields()
        except Exception as exc:  # noqa: BLE001 - must never orphan the operation (R4)
            self._resolve_uncertain(
                w.operation_id,
                "open_config: the configuration could not be read back after Open "
                f"({exc!r}). It may or may not be open. Re-read the current state.",
            )
            return
        self._watchers.pop(w.operation_id, None)
        self.store.update_session(**fields)
        self.store.finish(
            w.operation_id,
            OpState.COMPLETED,
            result=OpenResult(
                active_path=str(fields["configuration_path"] or ""),
                attached=w.attached,
                discarded_changes=w.discarded_changes,
                saved_previous_to=w.saved_previous_to,
                epoch_after=self.store.epoch,
                backup_path=w.backup_path,
            ),
        )

    def _on_shutdown(self) -> None:
        self._disconnect_on_worker()
        if self._watchers:
            self._bump_epoch()  # retire pending watchers; their outcome is unknown

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
        def job(step: StepContext) -> Any:
            attached = self._connect_on_worker(step, launch=launch_if_absent)
            app = self.session.app
            cfg = app.Configuration
            self._refuse_if_measuring("opening a configuration")
            modified = bool(cfg.Modified)  # pre-dispatch dirty check (contract rule 4)
            saved_to = backup = None
            if modified and on_dirty == "refuse":
                raise BackendError(
                    ErrorCode.DIRTY_CONFIG,
                    "The open configuration has unsaved changes. Save it, or open with "
                    "on_dirty='save' or 'discard'.",
                    details=(("configuration", str(cfg.FullName)),),
                )
            if modified and on_dirty == "save":
                backup = self._save_with_backup(step, cfg, None)
                saved_to = str(cfg.FullName)
            step.dispatch("opening")
            app.Open(path, False, False)
            self._bump_epoch(opening=True)
            self.store.update_session(**self.session.session_fields())
            assert step.operation_id is not None
            step.phase("awaiting_on_open")
            self._watchers[step.operation_id] = _OpenWatcher(
                step.operation_id,
                time.monotonic() + OPEN_EVENT_WAIT_S,
                attached,
                modified and on_dirty == "discard",
                saved_to,
                backup,
            )
            return PENDING

        step_s = self.settings.open_step_s + (
            self.settings.launch_step_s if launch_if_absent else 0.0
        )
        return self._submit("open_config", job, ctx, step_s=step_s)

    def save_config(self, as_path: str | None, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> SaveResult:
            self._require_connected()
            cfg = self.session.require_configuration()
            self._refuse_if_measuring("saving the configuration")
            before = str(cfg.FullName)
            backup = self._save_with_backup(step, cfg, as_path)
            active = str(self.session.app.Configuration.FullName)
            changed = active.lower() != before.lower()
            epoch = self._bump_epoch(opening=True) if changed else self.store.epoch
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
        def job(step: StepContext) -> SaveResult | None:
            self._require_connected()
            app = self.session.app
            self._refuse_if_measuring("quitting CANoe")
            cfg = app.Configuration
            saved: SaveResult | None = None
            if bool(cfg.Modified):
                if on_dirty == "refuse":
                    raise BackendError(
                        ErrorCode.DIRTY_CONFIG, "Unsaved changes; quit refused (on_dirty=refuse)."
                    )
                if on_dirty == "save":
                    path = str(cfg.FullName)
                    backup = self._save_with_backup(step, cfg, None)
                    saved = SaveResult(path, path, False, backup, self.store.epoch)
            step.dispatch("quitting")
            app.Quit()
            self._disconnect_on_worker()
            self._bump_epoch()
            return saved

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
        notes: list[str] = []
        active_nodes: tuple[str, ...] = ()
        auto_tests: tuple[str, ...] = ()
        read_epochs: list[int] = []
        if request.action == "measurement.start":
            # What may transmit or run once measurement starts (skill + safety review).
            try:
                nodes = self.nodes()
                setup = self.test_setup()
                read_epochs += [nodes.epoch, setup.epoch]
                active_nodes = tuple(n.id for n in nodes.value if n.active and not n.test_module)
                auto_tests = tuple(t.id for t in setup.value.simulation_test_nodes if t.enabled)
                if any(e.modules for e in setup.value.environments):
                    notes.append(
                        "Test Setup modules may also start automatically; CANoe 17 COM does "
                        "not expose their start-on-measurement setting. Check in CANoe."
                    )
            except BackendError as exc:
                notes.append(
                    f"Could not list nodes and test modules ({exc.code.value}); the "
                    "simulation nodes that may transmit are unknown."
                )
        st = self.store.status(self.backend_name)
        launches = False
        discards = False
        overwrites: tuple[str, ...] = ()
        blocked: BlockReason | None = None
        saves_first = False
        if request.action in ("open_config", "quit"):
            on_dirty = params.get("on_dirty", "refuse")
            if request.action == "open_config":
                launches = (
                    not st.connected
                    and bool(params.get("launch_if_absent", True))
                    and not canoe_processes()  # attach, not launch, when CANoe runs
                )
            if st.configuration_modified:
                if on_dirty == "refuse":
                    # Not a block: the backend's own pre-dispatch check refuses with the
                    # precise DIRTY_CONFIG code, without dispatching anything.
                    notes.append("The open configuration has unsaved changes; it will be refused.")
                discards = on_dirty == "discard"
                saves_first = on_dirty == "save"
                if saves_first and st.configuration_path:
                    overwrites = (st.configuration_path,)
                    notes.append("The configuration is backed up, then saved, first.")
            notes.append(
                "CANoe tracks most but not all changes as unsaved (Node.Active is not); "
                "changes it does not track are lost."
            )
        elif request.action == "save_config":
            as_path = params.get("as_path")
            target = as_path if isinstance(as_path, str) else st.configuration_path
            if target and Path(target).exists():
                overwrites = (target,)
                notes.append("The existing file is backed up before it is overwritten.")
        if request.action == "node.set_active":
            notes.append(
                "CANoe does not mark this change as unsaved (api-evidence C5/N2): it is "
                "lost on reopen or on_dirty='refuse' will not protect it."
            )
        needs_licence = request.action in ("save_config", "measurement.start") or saves_first
        if needs_licence and st.licensed is False:
            blocked = BlockReason.NO_LICENSE
        if st.measurement_running and request.action in ("open_config", "quit", "save_config"):
            # The jobs refuse these first, before any side effect (C6); the preview
            # reports the same reason, ahead of the licence (review C9).
            blocked = BlockReason.MEASUREMENT_RUNNING
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
            auto_start_test_modules=auto_tests,
            active_simulation_nodes=active_nodes,
            blocked_by=blocked,
            notes=tuple(notes),
        )
        # If the lists came from an older session, the oldest epoch authorises the
        # confirmation, so a session change in between makes it stale (safe).
        epoch = min([st.epoch, *read_epochs])
        return self.store.observed(preview, epoch=epoch, age_s=st.snapshot_age_s)

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
        return self._read(
            f"can_controller:{bus}:{channel}",
            lambda e: self.session.can_controller(bus, channel),
        )

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
            self._refuse_if_measuring("changing a database channel")
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

    # ============================================ licence-free configuration edits
    # Verified live on sample copies without a licence (api-evidence N1-N4, T3-T5).
    # Saving the result still needs a licence (C3).

    def _existing_file(self, path: str, what: str) -> str:
        if not Path(path).is_file():
            raise BackendError(ErrorCode.NOT_FOUND, f"{what} {path} does not exist.")
        return str(Path(path))

    def _channel_in_range(self, bus_obj: Any, bus: str, channel: int) -> None:
        count = int(bus_obj.Channels.Count)
        if not 1 <= channel <= count:
            raise BackendError(
                ErrorCode.INVALID_ARGUMENT,
                f"Bus {bus!r} has channels 1..{count}; CANoe rejects channel {channel} "
                "(api-evidence N3).",
            )

    def add_database(self, path: str, bus: str, channel: int, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> DatabaseInfo:
            self._require_connected()
            self._refuse_if_measuring("adding a database")
            file = self._existing_file(path, "Database")
            b = self.session.bus_named(bus)
            self._channel_in_range(b, bus, channel)
            for d in self.session.databases(self.store.epoch):
                if d.bus == bus and d.path.lower() == file.lower():
                    raise BackendError(ErrorCode.ALREADY_EXISTS, f"{file} is already on {bus}.")
            dbs = late(b.Databases)
            step.dispatch("adding")
            dbs.Add(file)
            new = late(dbs.Item(int(dbs.Count)))
            if int(new.Channel) != channel:
                step.phase("setting_channel")
                new.Channel = channel
            infos = self.session.databases(self.store.epoch)
            return next(
                d for d in reversed(infos) if d.bus == bus and d.path.lower() == file.lower()
            )

        return self._submit("database.add", job, ctx)

    def remove_database(self, database_id: str, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> Removed:
            self._require_connected()
            self._refuse_if_measuring("removing a database")
            objs = self.session.database_objects()
            entries = self.session.database_entries()
            idx = self.session.ids.resolve("databases", self.store.epoch, entries, database_id)
            _, bus_name = objs[idx]
            dbs = late(self.session.bus_named(bus_name).Databases)
            # database_objects() lists each bus's databases in collection order, so
            # the 1-based position on that bus is the count of same-bus entries up
            # to idx. Never search by path: duplicates share it (review R2).
            position = sum(1 for _, b in objs[: idx + 1] if b == bus_name)
            step.dispatch("removing")
            dbs.Remove(position)
            self.session.ids.clear()
            return Removed(database_id)

        return self._submit("database.remove", job, ctx)

    def add_bus(self, name: str, bus_type: Literal["CAN"], ctx: CallContext) -> OperationStatus:
        # Buses.Remove removed nothing and buses were renamed (api-evidence B1).
        return self._not_implemented("bus.add")

    def remove_bus(self, bus_id: str, ctx: CallContext) -> OperationStatus:
        return self._not_implemented("bus.remove")

    def _node(self, node_id: str) -> tuple[int, Any]:
        objs = self.session.node_objects()
        idx = self.session.ids.resolve(
            "nodes", self.store.epoch, self.session.node_entries(), node_id
        )
        return idx, late(objs[idx])

    def add_node(
        self, name: str, bus: str, capl_path: str | None, ctx: CallContext
    ) -> OperationStatus:
        def job(step: StepContext) -> NodeInfo:
            self._require_connected()
            self._refuse_if_measuring("adding a node")
            file = self._existing_file(capl_path, "CAPL file") if capl_path else None
            b = self.session.bus_named(bus)
            if any(n.name == name for n in self.session.nodes(self.store.epoch)):
                raise BackendError(ErrorCode.ALREADY_EXISTS, f"A node named {name!r} exists.")
            nodes = late(self.session.require_configuration().SimulationSetup.Nodes)
            step.dispatch("adding")
            nodes.Add(name)
            node = next(late(n) for n in self.session.node_objects() if str(n.Name) == name)
            if file:
                step.phase("setting_capl")
                node.FullName = file
            if not bool(node.IsBusAttached(b)):
                step.phase("attaching")
                node.AttachBus(b)
            return next(n for n in self.session.nodes(self.store.epoch) if n.name == name)

        return self._submit("node.add", job, ctx)

    def remove_node(self, node_id: str, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> Removed:
            self._require_connected()
            self._refuse_if_measuring("removing a node")
            idx, _ = self._node(node_id)
            nodes = late(self.session.require_configuration().SimulationSetup.Nodes)
            step.dispatch("removing")
            nodes.Remove(idx + 1)
            self.session.ids.clear()
            return Removed(node_id)

        return self._submit("node.remove", job, ctx)

    def set_node_active(self, node_id: str, active: bool, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> NodeInfo:
            self._require_connected()
            self._refuse_if_measuring("activating or deactivating a node")
            idx, node = self._node(node_id)
            step.dispatch("setting_active")
            node.Active = active
            return self.session.nodes(self.store.epoch)[idx]

        return self._submit("node.set_active", job, ctx)

    def attach_node_bus(
        self, node_id: str, bus: str, attach: bool, ctx: CallContext
    ) -> OperationStatus:
        def job(step: StepContext) -> NodeInfo:
            self._require_connected()
            self._refuse_if_measuring("changing node bus attachment")
            idx, node = self._node(node_id)
            b = self.session.bus_named(bus)
            if not attach and bool(node.IsBusAttached(b)):
                attached = list(node.AttachedBuses or ())
                if len(attached) <= 1:
                    raise BackendError(
                        ErrorCode.INVALID_ARGUMENT,
                        f"{bus!r} is the node's only bus; CANoe refuses to detach it "
                        "(api-evidence N5). Attach another bus first.",
                    )
            if bool(node.IsBusAttached(b)) != attach:
                step.dispatch("attaching" if attach else "detaching")
                if attach:
                    node.AttachBus(b)
                else:
                    node.DetachBus(b)
            return self.session.nodes(self.store.epoch)[idx]

        return self._submit("node.attach_bus" if attach else "node.detach_bus", job, ctx)

    def set_can_bitrate(
        self, bus: str, channel: int, bitrate_bps: int, ctx: CallContext
    ) -> OperationStatus:
        # CANController.Baudrate writes read back inconsistent values (api-evidence K2).
        return self._not_implemented("can_controller.set_bitrate")

    def add_test_environment(self, tse_path: str, ctx: CallContext) -> OperationStatus:
        def job(step: StepContext) -> TestEnvironmentInfo:
            self._require_connected()
            self._refuse_if_measuring("adding a test environment")
            file = self._existing_file(tse_path, "Test environment")  # Add needs a file (T3)
            for env in self.session.test_setup(self.store.epoch).environments:
                if env.path and env.path.lower() == file.lower():
                    raise BackendError(ErrorCode.ALREADY_EXISTS, f"{file} is already loaded.")
            envs = late(self.session.require_configuration().TestSetup.TestEnvironments)
            step.dispatch("adding")
            envs.Add(file)
            setup = self.session.test_setup(self.store.epoch)
            return next(e for e in setup.environments if (e.path or "").lower() == file.lower())

        return self._submit("test_setup.add_environment", job, ctx)

    def add_test_module(
        self, environment_id: str, can_path: str, ctx: CallContext
    ) -> OperationStatus:
        def job(step: StepContext) -> TestModuleInfo:
            self._require_connected()
            self._refuse_if_measuring("adding a test module")
            file = self._existing_file(can_path, "Test module")
            envs = self.session.test_environment_objects()
            idx = self.session.ids.resolve(
                "test_environments", self.store.epoch, [e for e, _ in envs], environment_id
            )
            mods = late(envs[idx][1].TestModules)
            before = int(mods.Count)
            step.dispatch("adding")
            mods.Add(file)  # appended at the end (api-evidence T4)
            setup = self.session.test_setup(self.store.epoch)
            env = setup.environments[idx]
            if len(env.modules) != before + 1:
                raise BackendError(
                    ErrorCode.CANOE_REJECTED, f"CANoe did not add {file} to {environment_id}."
                )
            return env.modules[-1]

        return self._submit("test_setup.add_module", job, ctx)

    def set_test_module_enabled(
        self, module_id: str, enabled: bool, ctx: CallContext
    ) -> OperationStatus:
        def job(step: StepContext) -> TestModuleInfo:
            self._require_connected()
            self._refuse_if_measuring("enabling or disabling a test module")
            if module_id.startswith("tm-sim:"):
                raise BackendError(
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                    "Simulation Setup test nodes are switched with node.set_active.",
                )
            if "@" in module_id:
                raise BackendError(
                    ErrorCode.AMBIGUOUS_ID,
                    f"{module_id} is one of several same-named modules; rename one in CANoe.",
                )
            found = [
                m
                for mid, _, _, m in self.session.test_module_objects(self.store.epoch)
                if mid == module_id
            ]
            if not found:
                raise BackendError(ErrorCode.NOT_FOUND, f"No test module {module_id}.")
            module = late(found[0])
            step.dispatch("setting_enabled")
            module.Enabled = enabled
            setup = self.session.test_setup(self.store.epoch)
            return next(m for e in setup.environments for m in e.modules if m.id == module_id)

        return self._submit("test_setup.set_enabled", job, ctx)

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
