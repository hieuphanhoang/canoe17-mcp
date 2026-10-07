"""Plain-data contract between the MCP/policy layer and a CANoe backend.

Everything that crosses the backend boundary is defined here: identifiers,
evidence and availability, errors, settings, the call context, the operation
lifecycle, result types and the ``Backend`` protocol. Only plain data crosses:
str, int, float, bool, bytes, None, tuples of these, enums and the frozen
dataclasses below. No COM proxy or pywin32 value ever leaves the backend.

Snapshots are deeply immutable: sequences are tuples and maps are tuples of
key/value tuples, never list or dict.

Version 0.2. Design notes and the review record: BACKEND_CONTRACT_DRAFT.md
and handoff thread AGENT-002. Observed COM behaviour: docs/com/api-evidence.md.
Items marked SPIKE may change after runtime evidence; such a change is a
reviewed contract change, not a silent edit.

Rules every backend follows
---------------------------

1. **Epoch.** ``epoch`` increments on connect, ``Application.OnOpen`` and
   ``OnQuit``. Every session-scoped mutation takes a :class:`CallContext`
   whose ``expected_epoch`` is the epoch of the snapshot or preview the request
   was built from. The STA worker compares it immediately before dispatch, in
   the same job, and fails with ``STALE_SESSION`` on mismatch. The policy layer
   never refreshes the epoch to authorise an older request; when the client has
   no earlier snapshot, the policy computes the preview first and uses the
   preview's epoch.
2. **Every mutation is an operation.** Mutating methods return an
   :class:`OperationStatus` at once (test runs and diagnostic requests return
   their domain status, which carries the operation_id); :meth:`Backend.wait` waits on the state
   store (never on the worker) up to the caller's timeout. ``BUSY`` and
   ``DEADLINE_EXCEEDED`` mean the job was never dispatched and has been
   cancelled atomically. Once a COM call with side effects is dispatched, an
   expired step deadline gives ``OUTCOME_UNKNOWN``: the operation stays
   pollable, the backend is degraded (no new mutations) until it resolves, and
   a late return resolves it with ``late_resolution=True``. No blind retry.
3. **Reads report freshness.** Reads return :class:`Observed`, which carries
   the epoch the data belongs to, its age, and what the worker is busy with.
   Status-type reads are served from the state store while the worker is busy.
4. **Dirty configuration.** ``DIRTY_CONFIG`` is raised only by the backend's
   own pre-dispatch check (``Configuration.Modified`` read on the worker in
   the same job as the ``Open``). CANoe 17.6 does not refuse
   ``Open(path, False, False)`` on a modified configuration; it discards the
   changes (api-evidence C2). Any failure of a dispatched call is
   ``CANOE_REJECTED`` with its HRESULT, unless positively identified as
   ``LICENSE_REQUIRED`` (description text, api-evidence C3/M1; HRESULT kept). ``Modified`` does not cover every
   change (api-evidence C5), so the dirty check is necessary, not sufficient.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# Plain values
# --------------------------------------------------------------------------

Scalar = int | float | bool | str
Pairs = tuple[tuple[str, Scalar], ...]
"""Frozen scalar map: ``(("key", value), ...)``. Keys unique, order kept."""

type PlainValue = Scalar | bytes | None | tuple[PlainValue, ...]
"""Deeply immutable value. Maps are tuples of ``(key, value)`` tuples."""

Params = tuple[tuple[str, PlainValue], ...]
"""Frozen map of plain values."""

Value = Scalar | tuple[int | float, ...]

DirtyPolicy = Literal["refuse", "save", "discard"]
DiagWindow = Literal["console", "session", "fault_memory", "all"]
SummarySection = Literal[
    "all", "networks", "databases", "nodes", "diagnostics", "tests", "logging", "panels"
]

# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------
# Server tokens are opaque and process-local: ``op-<12 hex>``, ``run-<12 hex>``.
#
# Object IDs are built by the backend and passed back by clients:
#   bus:<Bus>  db:<Database>  node:<Node>  diag:<Qualifier>  env:<Env>
#   env:<Env>/<Folder>[/<Folder>...]  tm:<Env>/[<Folder>/...]<Module>
#   tm-sim:<Node> (Simulation Setup test node)  log:<index>:<File>
# Each name segment is escaped with :func:`escape_id_segment` (% / @).
# Duplicate siblings all get ``@<n>`` (1-based position). An ``@n`` ID is
# valid only while its collection is unchanged: the backend records a
# fingerprint of the collection (names and paths, in order) when it issues
# the ID and re-checks it at dispatch; any difference is ``STALE_SESSION``.
# A short name matching several objects is ``AMBIGUOUS_ID``; the backend never
# picks one. Duplicate diagnostic qualifiers are possible (api-evidence D1).

OPERATION_ID_RE = re.compile(r"^op-[0-9a-f]{12}$")
RUN_ID_RE = re.compile(r"^run-[0-9a-f]{12}$")


def new_operation_id() -> str:
    return "op-" + secrets.token_hex(6)


def new_run_id() -> str:
    return "run-" + secrets.token_hex(6)


def escape_id_segment(name: str) -> str:
    """Escape one CANoe name for a qualified ID: ``%``, ``/`` and ``@``."""
    return name.replace("%", "%25").replace("/", "%2F").replace("@", "%40")


def unescape_id_segment(segment: str) -> str:
    """Inverse of :func:`escape_id_segment`."""
    return segment.replace("%40", "@").replace("%2F", "/").replace("%25", "%")


# --------------------------------------------------------------------------
# Evidence, support and availability
# --------------------------------------------------------------------------


class Evidence(StrEnum):
    """How we know an operation works.

    Real-COM evidence is ordered by :data:`REAL_EVIDENCE_ORDER`. FAKE is a
    separate axis: it describes the fake backend only and never satisfies a
    real-backend evidence floor.
    """

    DOCUMENTED = "documented"
    FAKE = "fake"
    DEMO_VERIFIED = "demo_verified"
    BENCH_VERIFIED = "bench_verified"


REAL_EVIDENCE_ORDER: tuple[Evidence, ...] = (
    Evidence.DOCUMENTED,
    Evidence.DEMO_VERIFIED,
    Evidence.BENCH_VERIFIED,
)


class Support(StrEnum):
    IMPLEMENTED = "implemented"
    NOT_IMPLEMENTED = "not_implemented"
    UNSUPPORTED_BY_CANOE = "unsupported_by_canoe"


class BlockReason(StrEnum):
    NOT_CONNECTED = "not_connected"
    NO_CONFIGURATION = "no_configuration"
    MEASUREMENT_RUNNING = "measurement_running"
    MEASUREMENT_STOPPED = "measurement_stopped"
    BELOW_EVIDENCE_FLOOR = "below_evidence_floor"
    UNSUPPORTED = "unsupported"
    MISSING_PREREQUISITE = "missing_prerequisite"
    NO_LICENSE = "no_license"
    """CANoe: "Function is only available with valid application license"."""
    BUSY = "busy"
    DEGRADED = "degraded"
    LOCKED_BY_OTHER_SERVER = "locked_by_other_server"


@dataclass(frozen=True, slots=True)
class Capability:
    operation: str
    """Dotted name, e.g. ``"database.set_channel"`` or ``"test_run.start"``."""
    support: Support
    evidence: Evidence
    evidence_ref: str | None = None
    """docs/com/api-evidence.md row or probe test id."""
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Availability:
    operation: str
    available: bool
    blocked_by: BlockReason | None = None
    detail: str | None = None


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ErrorCode(StrEnum):
    # Policy layer: validated before the backend is called.
    INVALID_ARGUMENT = "invalid_argument"
    PATH_NOT_ALLOWED = "path_not_allowed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    READ_ONLY_MODE = "read_only_mode"
    # Backend.
    NOT_CONNECTED = "not_connected"
    NO_ACTIVE_INSTANCE = "no_active_instance"
    """No CANoe process is running and launch was not requested."""
    ATTACH_FAILED = "attach_failed"
    LOCKED_BY_OTHER_SERVER = "locked_by_other_server"
    NO_CONFIGURATION = "no_configuration"
    DIRTY_CONFIG = "dirty_config"
    """Only from the pre-dispatch Modified check; see module rule 4."""
    MEASUREMENT_RUNNING = "measurement_running"
    MEASUREMENT_NOT_RUNNING = "measurement_not_running"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    AMBIGUOUS_ID = "ambiguous_id"
    STALE_SESSION = "stale_session"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    LICENSE_REQUIRED = "license_required"
    CAPL_FUNCTION_UNBOUND = "capl_function_unbound"
    BUSY = "busy"
    """Not dispatched: the worker stayed busy past the dispatch deadline."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """Not dispatched: the operation deadline passed while queued."""
    OUTCOME_UNKNOWN = "outcome_unknown"
    """Dispatched; the CANoe result is not known yet."""
    CANOE_REJECTED = "canoe_rejected"
    INTERNAL = "internal"


POLICY_ERROR_CODES = frozenset(
    {
        ErrorCode.INVALID_ARGUMENT,
        ErrorCode.PATH_NOT_ALLOWED,
        ErrorCode.NEEDS_CONFIRMATION,
        ErrorCode.READ_ONLY_MODE,
    }
)


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Immutable error data, safe to keep inside snapshots and results."""

    code: ErrorCode
    message: str
    retryable: bool = False
    hresult: int | None = None
    details: Pairs = ()


class BackendError(Exception):
    """Raised by a backend; carries an :class:`ErrorInfo`.

    A normal exception (not a frozen dataclass) so ``__traceback__`` can be
    assigned by contextlib and asyncio. Snapshots hold ``ErrorInfo`` only.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        hresult: int | None = None,
        details: Pairs = (),
    ) -> None:
        super().__init__(message)
        self.info = ErrorInfo(code, message, retryable, hresult, details)

    @classmethod
    def from_info(cls, info: ErrorInfo) -> BackendError:
        return cls(
            info.code,
            info.message,
            retryable=info.retryable,
            hresult=info.hresult,
            details=info.details,
        )

    @property
    def code(self) -> ErrorCode:
        return self.info.code

    @property
    def message(self) -> str:
        return self.info.message

    def __repr__(self) -> str:
        return f"BackendError({self.info.code.value!r}, {self.info.message!r})"


# --------------------------------------------------------------------------
# Settings and call context
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendSettings:
    """What a backend receives at construction. Codex's settings model builds it."""

    lock_key: str = "default"
    lock_acquire_timeout_s: float = 0.0
    dispatch_timeout_s: float = 10.0
    """Longest a job may wait in the queue before it is cancelled undispatched."""
    operation_default_timeout_s: float = 30.0
    """Default for test-run and diagnostic deadlines and for caller waits."""
    operation_max_timeout_s: float = 3600.0
    """Ceiling for any caller-supplied timeout; requests above it are invalid."""
    launch_step_s: float = 180.0
    """Launching CANoe took about 37 s on the demo PC (api-evidence A4)."""
    open_step_s: float = 120.0
    save_step_s: float = 120.0
    compile_step_s: float = 120.0
    quick_step_s: float = 15.0
    """Step deadline for any other single COM call with side effects."""
    report_finalize_timeout_s: float = 30.0
    min_evidence: Evidence = Evidence.DEMO_VERIFIED
    capl_allowlist: tuple[str, ...] = ()
    allow_launch: bool = True


@dataclass(frozen=True, slots=True)
class CallContext:
    """Per-request context for every session-scoped mutation."""

    expected_epoch: int
    """Epoch of the snapshot or preview this request was built from."""
    wait_s: float | None = None
    """How long the caller will wait for completion; None = settings default.
    Separate from domain deadlines such as ``TestRunSpec.timeout_s``."""
    request_id: str | None = None
    """Audit correlation id from the policy layer."""


@dataclass(frozen=True, slots=True)
class Observed[T]:
    """A read result with the session state it belongs to."""

    value: T
    epoch: int
    snapshot_age_s: float
    """0 for a live COM read; >0 when served from the state store."""
    busy_with: str | None = None
    """operation_id of the COM step currently occupying the worker."""
    degraded: bool = False


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionStatus:
    connected: bool
    canoe_version: str | None
    canoe_exe: str | None
    configuration_path: str | None
    configuration_modified: bool | None
    measurement_running: bool | None
    epoch: int
    lock_held: bool
    lock_holder_pid: int | None
    degraded: bool
    busy_with: str | None
    snapshot_age_s: float
    backend: Literal["com", "fake"]
    licensed: bool | None = None
    """False once CANoe has reported the licence error; None if unknown."""


# --------------------------------------------------------------------------
# Configuration objects and results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SaveResult:
    saved_path: str
    active_path: str
    """Save(path) makes the copy the active configuration (help; SPIKE 6)."""
    active_changed: bool
    backup_path: str | None
    epoch_after: int


@dataclass(frozen=True, slots=True)
class OpenResult:
    active_path: str
    attached: bool
    """True when attached to a running CANoe, False when launched."""
    discarded_changes: bool
    saved_previous_to: str | None
    epoch_after: int


@dataclass(frozen=True, slots=True)
class CompileResult:
    success: bool
    error_message: str | None = None
    node_name: str | None = None
    source_file: str | None = None


@dataclass(frozen=True, slots=True)
class BusInfo:
    id: str
    name: str
    bus_type: str
    channels: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    id: str
    name: str
    path: str
    channel: int | None
    bus: str | None


@dataclass(frozen=True, slots=True)
class NodeInfo:
    id: str
    name: str
    active: bool | None
    capl_path: str | None
    buses: tuple[str, ...] = ()
    test_module: bool = False


@dataclass(frozen=True, slots=True)
class DiagDescriptionInfo:
    id: str
    qualifier: str
    network: str
    node: str | None
    file_path: str
    mode: Literal[
        "interpretation_only",
        "tester",
        "ecu_simulation",
        "physical_request",
        "functional_group",
        "other",
    ]
    """DiagDescription.Mode 0-4 (late-bound only); reported, never set."""
    variant: str | None = None


@dataclass(frozen=True, slots=True)
class CanControllerInfo:
    bus: str
    channel: int
    bitrate_bps: int | None
    raw_baudrate: float | None
    """COM value before unit conversion (SPIKE 9)."""


@dataclass(frozen=True, slots=True)
class TestModuleInfo:
    __test__ = False  # not a pytest test class
    id: str
    name: str
    path: str | None
    enabled: bool | None
    start_on_measurement: bool | None
    last_verdict: Verdict | None = None
    source: Literal["test_setup", "simulation_setup"] = "test_setup"
    startable: bool = True
    """False for Simulation Setup test nodes: no COM start (api-evidence T1)."""


@dataclass(frozen=True, slots=True)
class TestEnvironmentInfo:
    __test__ = False  # not a pytest test class
    id: str
    name: str
    path: str | None
    enabled: bool | None
    modules: tuple[TestModuleInfo, ...] = ()
    folders: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TestSetupInfo:
    __test__ = False  # not a pytest test class
    environments: tuple[TestEnvironmentInfo, ...]
    simulation_test_nodes: tuple[TestModuleInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class LoggingBlockInfo:
    id: str
    full_name: str
    active: bool | None = None


@dataclass(frozen=True, slots=True)
class ConfigSummary:
    configuration_path: str
    buses: tuple[BusInfo, ...] = ()
    databases: tuple[DatabaseInfo, ...] = ()
    nodes: tuple[NodeInfo, ...] = ()
    diag_descriptions: tuple[DiagDescriptionInfo, ...] = ()
    test_setup: TestSetupInfo | None = None
    logging_blocks: tuple[LoggingBlockInfo, ...] = ()
    panels: tuple[str, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Removed:
    """Result of a successful remove."""

    id: str


# --------------------------------------------------------------------------
# Previews
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectRequest:
    """A validated mutating request, described for a read-only preview.

    ``action`` is the dotted operation name (as in :class:`Capability`);
    ``params`` carries every validated argument, including tuples and bytes.
    """

    action: str
    params: Params = ()


@dataclass(frozen=True, slots=True)
class EffectPreview:
    """Returned inside :class:`Observed`; its epoch authorises the confirm."""

    action: str
    affected: tuple[str, ...] = ()
    launches_canoe: bool = False
    overwrites: tuple[str, ...] = ()
    configuration_path: str | None = None
    configuration_modified: bool | None = None
    discards_changes: bool = False
    auto_start_test_modules: tuple[str, ...] = ()
    active_simulation_nodes: tuple[str, ...] = ()
    blocked_by: BlockReason | None = None
    notes: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Test runs and reports
# --------------------------------------------------------------------------


class Verdict(StrEnum):
    NOT_AVAILABLE = "not_available"
    PASSED = "passed"
    FAILED = "failed"
    ERROR_IN_TEST_SYSTEM = "error_in_test_system"
    UNKNOWN = "unknown"


class StopReason(StrEnum):
    END = "end"
    USER_ABORT = "user_abort"
    GENERAL_ERROR = "general_error"
    MEASUREMENT_STOPPED = "measurement_stopped"
    UNKNOWN = "unknown"


class ReportState(StrEnum):
    NOT_ENABLED = "not_enabled"
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STALE_REJECTED = "stale_rejected"
    UNCORRELATED = "uncorrelated"
    """A candidate exists but cannot be tied to this run; fail closed (SPIKE 7)."""


@dataclass(frozen=True, slots=True)
class ReportIdentity:
    path: str
    size: int
    mtime_ns: int
    head_sha256: str
    """SHA-256 of the first 64 KiB."""


@dataclass(frozen=True, slots=True)
class ReportArtifact:
    state: ReportState
    path: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ModuleRunResult:
    module_id: str
    state: Literal["pending", "running", "stopping", "done", "skipped"]
    verdict: Verdict
    stop_reason: StopReason | None
    xml: ReportArtifact
    html: ReportArtifact


@dataclass(frozen=True, slots=True)
class TestRunSpec:
    __test__ = False  # not a pytest test class
    module_ids: tuple[str, ...] | None
    environment_id: str | None
    start_measurement: bool = False
    timeout_s: float | None = None
    """Run deadline; exceeding it requests a stop. Not the caller's wait."""
    capture_write: bool = True
    logging_block_id: str | None = None

    def __post_init__(self) -> None:
        if (self.module_ids is None) == (self.environment_id is None):
            raise ValueError("exactly one of module_ids / environment_id is required")


@dataclass(frozen=True, slots=True)
class TestRunStatus:
    __test__ = False  # not a pytest test class
    run_id: str
    operation_id: str
    state: OpState
    phase: Literal[
        "queued", "starting_measurement", "running", "stopping", "finalizing_reports", "done"
    ]
    epoch: int
    """Epoch the run was started in; stop never redirects to a newer session."""
    modules: tuple[ModuleRunResult, ...] = ()
    current_module: str | None = None
    write_log_path: str | None = None
    logging_file: str | None = None
    started_wall: str | None = None
    finished_wall: str | None = None
    measurement_left_running: bool | None = None
    error: ErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class ReportRef:
    """Either ``run_id`` (+ optional ``module_id``) or an explicit ``xml_path``."""

    run_id: str | None = None
    module_id: str | None = None
    xml_path: str | None = None

    def __post_init__(self) -> None:
        if (self.run_id is None) == (self.xml_path is None):
            raise ValueError("exactly one of run_id / xml_path is required")
        if self.module_id is not None and self.run_id is None:
            raise ValueError("module_id requires run_id")


@dataclass(frozen=True, slots=True)
class TestReportLocation:
    """What the backend returns for a report; Codex's reports.py parses it."""

    __test__ = False  # not a pytest test class
    module_id: str | None
    xml: ReportArtifact
    html: ReportArtifact
    identity: ReportIdentity | None
    historical: bool


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiagRequestSpec:
    network: str
    ecu_qualifier: str
    service_qualifier: str | None = None
    parameters: Pairs = ()
    raw: bytes | None = None
    suppress_positive_response: bool = False
    timeout_s: float | None = None
    """Response deadline. Not the caller's wait."""

    def __post_init__(self) -> None:
        if (self.service_qualifier is None) == (self.raw is None):
            raise ValueError("exactly one of service_qualifier / raw is required")
        if self.raw is not None and self.parameters:
            raise ValueError("parameters apply to symbolic requests only")


@dataclass(frozen=True, slots=True)
class DiagResponse:
    positive: bool
    response_code: int | None
    sender: str | None
    raw: bytes
    parameters: Pairs = ()


@dataclass(frozen=True, slots=True)
class DiagRequestStatus:
    operation_id: str
    state: OpState
    phase: Literal["queued", "sent", "awaiting_response", "done"]
    sent: bool
    epoch: int
    responses: tuple[DiagResponse, ...] = ()
    no_response: bool = False
    error: ErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class TesterPresentInfo:
    __test__ = False  # not a pytest test class
    network: str
    ecu: str
    server_started: bool
    """Started by this server process; it ends with this COM client (help)."""
    com_status: bool | None = None


# --------------------------------------------------------------------------
# Runtime values, CAPL, helper, frames, Write window
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueSelector:
    kind: Literal["signal", "sysvar"]
    name: str
    bus: str | None = None
    channel: int | None = None
    message: str | None = None
    namespace: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "signal":
            if self.bus is None or self.channel is None or self.message is None:
                raise ValueError("signal selector requires bus, channel and message")
            if self.namespace is not None:
                raise ValueError("signal selector does not take namespace")
        else:
            if self.namespace is None:
                raise ValueError("sysvar selector requires namespace")
            if self.bus is not None or self.channel is not None or self.message is not None:
                raise ValueError("sysvar selector does not take bus, channel or message")


@dataclass(frozen=True, slots=True)
class ValueReading:
    value: Value
    raw: bool
    unit: str | None = None
    is_online: bool | None = None


@dataclass(frozen=True, slots=True)
class CaplCallResult:
    name: str
    return_value: int


@dataclass(frozen=True, slots=True)
class HelperStatus:
    installed: bool
    node_id: str | None = None
    version: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CanFrame:
    channel: int
    can_id: int
    extended: bool
    data: bytes
    fd: bool = False
    brs: bool = False


@dataclass(frozen=True, slots=True)
class FrameSendAck:
    accepted_by_helper: bool
    """Helper acceptance is not proof the frame was observed on a bus."""


@dataclass(frozen=True, slots=True)
class WriteWindowText:
    text: str
    truncated: bool


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


class OpState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"
    """Dispatched, result unknown. Not terminal: it can resolve later."""


TERMINAL_STATES = frozenset({OpState.COMPLETED, OpState.FAILED, OpState.CANCELLED})

OperationKind = Literal[
    "connect",
    "open_config",
    "save_config",
    "quit",
    "compile",
    "measurement.start",
    "measurement.stop",
    "database.add",
    "database.remove",
    "database.set_channel",
    "bus.add",
    "bus.remove",
    "node.add",
    "node.remove",
    "node.set_active",
    "node.attach_bus",
    "node.detach_bus",
    "diag_description.add",
    "diag_description.remove",
    "diag_description.open_windows",
    "diag_description.close_windows",
    "can_controller.set_bitrate",
    "test_setup.add_environment",
    "test_setup.add_module",
    "test_setup.set_enabled",
    "test_run",
    "diag_request",
    "tester_present.start",
    "tester_present.stop",
    "value.set",
    "capl.call",
    "helper.install",
    "can_frame.send",
    "write_window.clear",
]

OperationResult = (
    OpenResult
    | SaveResult
    | CompileResult
    | DatabaseInfo
    | BusInfo
    | NodeInfo
    | DiagDescriptionInfo
    | CanControllerInfo
    | TestEnvironmentInfo
    | TestModuleInfo
    | TesterPresentInfo
    | ValueReading
    | CaplCallResult
    | HelperStatus
    | FrameSendAck
    | Removed
    | None
)


@dataclass(frozen=True, slots=True)
class OperationStatus:
    operation_id: str
    kind: OperationKind
    state: OpState
    requested_wall: str
    epoch: int
    """The expected_epoch it was authorised against."""
    phase: str | None = None
    started_wall: str | None = None
    finished_wall: str | None = None
    dispatched: bool = False
    """A COM call with side effects has been issued."""
    effects_possible: bool = False
    result: OperationResult = None
    error: ErrorInfo | None = None
    late_resolution: bool = False


OP_TRANSITIONS: dict[OpState, frozenset[OpState]] = {
    OpState.QUEUED: frozenset({OpState.RUNNING, OpState.CANCELLED}),
    OpState.RUNNING: frozenset(
        {OpState.COMPLETED, OpState.FAILED, OpState.STOPPING, OpState.OUTCOME_UNKNOWN}
    ),
    OpState.STOPPING: frozenset(
        {OpState.CANCELLED, OpState.COMPLETED, OpState.FAILED, OpState.OUTCOME_UNKNOWN}
    ),
    OpState.OUTCOME_UNKNOWN: frozenset({OpState.COMPLETED, OpState.FAILED, OpState.CANCELLED}),
    OpState.COMPLETED: frozenset(),
    OpState.FAILED: frozenset(),
    OpState.CANCELLED: frozenset(),
}
"""Allowed state transitions. Watchers and the fake backend follow only these."""

# --------------------------------------------------------------------------
# Backend protocol
# --------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """What the policy layer may call. Implemented by the COM and fake backends.

    Reads marked "state store" never queue a COM job. Every mutation returns
    an :class:`OperationStatus` immediately; use :meth:`wait`. ``ctx`` rules
    are in the module docstring. Control calls keyed by an operation or run ID
    use the epoch captured by that operation and never act on a newer session.
    """

    # session and operations
    def status(self) -> SessionStatus: ...  # state store
    def capabilities(self) -> tuple[Capability, ...]: ...
    def availability(self) -> Observed[tuple[Availability, ...]]: ...  # state store
    def connect(self) -> OperationStatus: ...
    # Attach only: process check, then Dispatch; the ROT is empty for CANoe 17
    # (api-evidence A2/A3). No CANoe process -> NO_ACTIVE_INSTANCE. Launching
    # (~37 s, A4) happens only inside open_config.
    def open_config(
        self, path: str, on_dirty: DirtyPolicy, launch_if_absent: bool, ctx: CallContext
    ) -> OperationStatus: ...
    def save_config(self, as_path: str | None, ctx: CallContext) -> OperationStatus: ...
    def quit(self, on_dirty: DirtyPolicy, ctx: CallContext) -> OperationStatus: ...
    def operation(self, operation_id: str) -> Observed[OperationStatus]: ...  # state store
    def wait(self, operation_id: str, wait_s: float) -> Observed[OperationStatus]: ...
    # Blocks the calling (non-STA) thread on the state store until the operation
    # is terminal or outcome_unknown, or wait_s passes. Never blocks the worker.
    def cancel(self, operation_id: str) -> OperationStatus: ...  # control lane

    # previews (read-only; the returned epoch authorises the confirmed call)
    def preview(self, request: EffectRequest) -> Observed[EffectPreview]: ...

    # configuration reads
    def summary(self, section: SummarySection) -> Observed[ConfigSummary]: ...
    def databases(self) -> Observed[tuple[DatabaseInfo, ...]]: ...
    def buses(self) -> Observed[tuple[BusInfo, ...]]: ...
    def nodes(self) -> Observed[tuple[NodeInfo, ...]]: ...
    def diag_descriptions(self) -> Observed[tuple[DiagDescriptionInfo, ...]]: ...
    def can_controller(self, bus: str, channel: int) -> Observed[CanControllerInfo]: ...
    def test_setup(self) -> Observed[TestSetupInfo]: ...

    # configuration mutations
    def add_database(
        self, path: str, bus: str, channel: int, ctx: CallContext
    ) -> OperationStatus: ...
    def remove_database(self, database_id: str, ctx: CallContext) -> OperationStatus: ...
    def set_database_channel(
        self, database_id: str, channel: int, ctx: CallContext
    ) -> OperationStatus: ...
    def add_bus(self, name: str, bus_type: Literal["CAN"], ctx: CallContext) -> OperationStatus: ...
    def remove_bus(self, bus_id: str, ctx: CallContext) -> OperationStatus: ...
    def add_node(
        self, name: str, bus: str, capl_path: str | None, ctx: CallContext
    ) -> OperationStatus: ...
    def remove_node(self, node_id: str, ctx: CallContext) -> OperationStatus: ...
    def set_node_active(self, node_id: str, active: bool, ctx: CallContext) -> OperationStatus: ...
    def attach_node_bus(
        self, node_id: str, bus: str, attach: bool, ctx: CallContext
    ) -> OperationStatus: ...
    def add_diag_description(
        self,
        network: str,
        path: str,
        ecu_identifier: str | None,
        open_console: bool,
        ctx: CallContext,
    ) -> OperationStatus: ...
    # Refuses with ALREADY_EXISTS when the same file is already loaded on the
    # network; CANoe itself accepts duplicates (api-evidence D1).
    def remove_diag_description(self, diag_id: str, ctx: CallContext) -> OperationStatus: ...
    def diag_windows(
        self, diag_id: str, window: DiagWindow, open_: bool, ctx: CallContext
    ) -> OperationStatus: ...
    def set_can_bitrate(
        self, bus: str, channel: int, bitrate_bps: int, ctx: CallContext
    ) -> OperationStatus: ...
    def compile(self, ctx: CallContext) -> OperationStatus: ...

    # measurement
    def measurement_start(self, ctx: CallContext) -> OperationStatus: ...  # done on OnStart
    def measurement_stop(self, ctx: CallContext) -> OperationStatus: ...  # StopEx; on OnStop

    # tests
    def add_test_environment(self, tse_path: str, ctx: CallContext) -> OperationStatus: ...
    def add_test_module(
        self, environment_id: str, can_path: str, ctx: CallContext
    ) -> OperationStatus: ...
    def set_test_module_enabled(
        self, module_id: str, enabled: bool, ctx: CallContext
    ) -> OperationStatus: ...
    def start_test_run(self, spec: TestRunSpec, ctx: CallContext) -> TestRunStatus: ...
    def test_run(self, run_id: str) -> Observed[TestRunStatus]: ...  # state store
    def stop_test_run(self, run_id: str) -> TestRunStatus: ...  # control lane
    def test_report(self, ref: ReportRef) -> TestReportLocation: ...

    # diagnostics
    def start_diag_request(
        self, spec: DiagRequestSpec, ctx: CallContext
    ) -> DiagRequestStatus: ...
    def diag_request(self, operation_id: str) -> Observed[DiagRequestStatus]: ...  # state store
    def tester_present(self, network: str, ecu: str) -> Observed[TesterPresentInfo]: ...
    def set_tester_present(
        self, network: str, ecu: str, on: bool, ctx: CallContext
    ) -> OperationStatus: ...

    # runtime
    def get_value(self, sel: ValueSelector, raw: bool) -> Observed[ValueReading]: ...
    def set_value(
        self, sel: ValueSelector, value: Value, raw: bool, ctx: CallContext
    ) -> OperationStatus: ...
    def call_capl(
        self, name: str, args: tuple[int | float, ...], ctx: CallContext
    ) -> OperationStatus: ...
    def helper_status(self) -> Observed[HelperStatus]: ...
    def install_helper(self, ctx: CallContext) -> OperationStatus: ...
    def send_can_frame(self, frame: CanFrame, ctx: CallContext) -> OperationStatus: ...
    def write_window(self, max_chars: int) -> Observed[WriteWindowText]: ...
    def clear_write_window(self, ctx: CallContext) -> OperationStatus: ...

    def shutdown(self) -> None: ...  # stops watchers and Tester Present, releases lock
