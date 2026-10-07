"""Policy for the first slice. No operation is authorised by a refreshed epoch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from threading import RLock

from . import contracts as c
from .settings import Settings


@dataclass(frozen=True, slots=True)
class PolicyResult:
    preview: c.Observed[c.EffectPreview]
    operation: c.OperationStatus | None = None
    needs_confirmation: bool = True


class SafetyPolicy:
    """Own one instance per client session; previews are bound to exact arguments.

    Call remember() on ID-bearing results returned to that client. Call invoke()
    with validated, defaulted EffectRequest arguments and a typed backend adapter.
    A repeat confirm uses the saved preview, even if unrelated status reads happen.
    New confirm=true requests still compute a preview before dispatch. A fresh
    confirm=false explicitly replaces a prior preview after the client re-reads.
    """

    def __init__(self, backend: c.Backend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings
        self._previews: dict[c.EffectRequest, c.Observed[c.EffectPreview]] = {}
        self._id_epochs: dict[str, int] = {}
        self._lock = RLock()

    def check_access(self, *, write: bool) -> None:
        if write and self.settings.read_only:
            raise c.BackendError(c.ErrorCode.READ_ONLY_MODE, "Mutations are disabled")

    def allowed_path(self, value: str) -> str:
        """Resolve links and missing output tails; compare path components, not prefixes."""
        if not isinstance(value, str) or not value or "\x00" in value:
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "Path must be nonempty")
        path = Path(value)
        # Deny Windows alternate streams/device spellings even on non-Windows tests.
        remainder = value[2:] if len(value) > 1 and value[1] == ":" else value
        if ":" in remainder or value.startswith(("\\\\?\\", "\\\\.\\")):
            raise c.BackendError(c.ErrorCode.PATH_NOT_ALLOWED, "Device/stream paths are forbidden")
        if not path.is_absolute():
            raise c.BackendError(c.ErrorCode.PATH_NOT_ALLOWED, "An absolute path is required")
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise c.BackendError(c.ErrorCode.PATH_NOT_ALLOWED, "Cannot resolve path") from exc
        if not any(resolved.is_relative_to(root) for root in self.settings.allowed_roots):
            raise c.BackendError(c.ErrorCode.PATH_NOT_ALLOWED, "Path is outside allowed roots")
        return str(resolved)

    def remember(self, observed: c.Observed[object]) -> None:
        def visit(value: object) -> None:
            if is_dataclass(value) and not isinstance(value, type):
                identifier = getattr(value, "id", None)
                if isinstance(identifier, str):
                    self._id_epochs[identifier] = observed.epoch
                for field in fields(value):
                    visit(getattr(value, field.name))
            elif isinstance(value, tuple):
                for item in value:
                    visit(item)

        with self._lock:
            visit(observed.value)

    def _paths(self, request: c.EffectRequest, preview: c.EffectPreview | None = None) -> None:
        params = dict(request.params)
        for key in ("path", "as_path", "capl_path", "tse_path", "can_path", "xml_path"):
            value = params.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, f"{key} must be a path")
                self.allowed_path(value)
        if preview is not None:
            for value in preview.overwrites:
                self.allowed_path(value)
            # Current-configuration mutations have an implicit target too.
            # Open/quit may leave an out-of-root config, but cannot save it.
            saves_current = params.get("on_dirty") == "save"
            if request.action not in {"connect", "open_config", "quit"} or saves_current:
                if preview.configuration_path is not None:
                    self.allowed_path(preview.configuration_path)

    def invoke(
        self,
        request: c.EffectRequest,
        *,
        confirm: bool,
        dispatch: Callable[[c.CallContext], c.OperationStatus],
        wait_s: float | None = None,
    ) -> PolicyResult:
        self.check_access(write=True)
        if type(confirm) is not bool:
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "confirm must be a boolean")
        try:
            wait = self.settings.wait_seconds(wait_s)
            hash(request)
        except (ValueError, TypeError) as exc:
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        if len(dict(request.params)) != len(request.params):
            raise c.BackendError(c.ErrorCode.INVALID_ARGUMENT, "Duplicate effect parameters")
        with self._lock:
            self._paths(request)
            preview = self._previews.get(request) if confirm else None
            if preview is None:
                if len(self._previews) >= 128 and request not in self._previews:
                    raise c.BackendError(c.ErrorCode.BUSY, "Too many outstanding previews")
                preview = self.backend.preview(request)
                for key, value in request.params:
                    if key.endswith("_id") or key == "qualifier":
                        epoch = self._id_epochs.get(value) if isinstance(value, str) else None
                        if epoch is not None and epoch != preview.epoch:
                            raise c.BackendError(
                                c.ErrorCode.STALE_SESSION, "Re-read the object list"
                            )
                self._previews[request] = preview
            self._paths(request, preview.value)
            if not confirm:
                return PolicyResult(preview)
            if preview.value.blocked_by is not None:
                reason = preview.value.blocked_by
                code = {
                    c.BlockReason.MEASUREMENT_RUNNING: c.ErrorCode.MEASUREMENT_RUNNING,
                    c.BlockReason.MEASUREMENT_STOPPED: c.ErrorCode.MEASUREMENT_NOT_RUNNING,
                    c.BlockReason.NOT_CONNECTED: c.ErrorCode.NOT_CONNECTED,
                    c.BlockReason.NO_CONFIGURATION: c.ErrorCode.NO_CONFIGURATION,
                    c.BlockReason.LOCKED_BY_OTHER_SERVER: c.ErrorCode.LOCKED_BY_OTHER_SERVER,
                    c.BlockReason.NO_LICENSE: c.ErrorCode.LICENSE_REQUIRED,
                }.get(reason, c.ErrorCode.CAPABILITY_UNAVAILABLE)
                raise c.BackendError(
                    code, f"Preview blocked: {reason}", details=(("blocked_by", reason.value),)
                )
            # Do not replace preview.epoch with backend.status().epoch here.
            operation = dispatch(c.CallContext(expected_epoch=preview.epoch, wait_s=wait))
            del self._previews[request]
            return PolicyResult(preview, operation, needs_confirmation=False)
