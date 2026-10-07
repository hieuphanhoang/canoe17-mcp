"""Portable TOML/environment settings; no COM import or machine discovery."""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .contracts import REAL_EVIDENCE_ORDER, BackendSettings, Evidence


def validate_backend(settings: BackendSettings) -> None:
    for field in fields(settings):
        if field.name.endswith("_s"):
            value = getattr(settings, field.name)
            minimum = 0 if field.name == "lock_acquire_timeout_s" else 0.000001
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"{field.name} must be a finite number")
            if not math.isfinite(value) or value < minimum:
                raise ValueError(f"{field.name} is outside its permitted range")
    if not 0 < settings.operation_default_timeout_s <= settings.operation_max_timeout_s <= 3600:
        raise ValueError("operation timeouts must satisfy 0 < default <= maximum <= 3600")
    if settings.min_evidence not in REAL_EVIDENCE_ORDER:
        raise ValueError("min_evidence must be real-COM evidence; fake is a separate axis")
    if not isinstance(settings.lock_key, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", settings.lock_key
    ):
        raise ValueError("lock_key must be 1..64 letters, digits, dots, underscores or hyphens")
    if type(settings.allow_launch) is not bool:
        raise ValueError("allow_launch must be a boolean")
    if not isinstance(settings.capl_allowlist, tuple) or any(
        not isinstance(name, str) or not name for name in settings.capl_allowlist
    ):
        raise ValueError("capl_allowlist must contain nonempty strings")


@dataclass(frozen=True, slots=True)
class Settings:
    read_only: bool = True
    allowed_roots: tuple[Path, ...] = ()
    backend: BackendSettings = BackendSettings()

    def __post_init__(self) -> None:
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if not isinstance(self.allowed_roots, tuple) or any(
            not isinstance(root, Path) or not root.is_absolute() for root in self.allowed_roots
        ):
            raise ValueError("allowed_roots must be absolute paths")
        object.__setattr__(self, "allowed_roots", tuple(p.resolve() for p in self.allowed_roots))
        validate_backend(self.backend)

    def wait_seconds(self, value: float | None) -> float:
        value = self.backend.operation_default_timeout_s if value is None else value
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise ValueError("wait must be numeric")
        if not math.isfinite(value) or not 0 <= value <= self.backend.operation_max_timeout_s:
            raise ValueError("wait exceeds the configured range")
        return float(value)


def load_settings(
    path: str | Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Settings:
    """Environment overrides TOML. Arrays/booleans/numbers use JSON syntax.

    Backend keys use CANOE17_MCP_<UPPERCASE_FIELD>, e.g. LOCK_KEY.
    Root paths must be absolute; neither the package nor cwd is implicitly allowed.
    """
    data: dict[str, Any] = {}
    if path is not None:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    unknown = set(data) - {"read_only", "allowed_roots", "backend"}
    if unknown:
        raise ValueError(f"Unknown settings: {sorted(unknown)}")
    backend_table = data.pop("backend", {})
    if not isinstance(backend_table, dict):
        raise ValueError("backend must be a TOML table")
    backend = dict(backend_table)
    env = os.environ if environ is None else environ
    backend_names = {field.name for field in fields(BackendSettings)}
    for name in {"read_only", "allowed_roots"} | backend_names:
        key = f"CANOE17_MCP_{name.upper()}"
        if key in env:
            value = env[key] if name in {"lock_key", "min_evidence"} else json.loads(env[key])
            (backend if name in backend_names else data)[name] = value
    if "min_evidence" in backend:
        backend["min_evidence"] = Evidence(backend["min_evidence"])
    if "capl_allowlist" in backend:
        if not isinstance(backend["capl_allowlist"], list):
            raise ValueError("capl_allowlist must be an array")
        backend["capl_allowlist"] = tuple(backend["capl_allowlist"])
    roots = data.get("allowed_roots", [])
    if not isinstance(roots, list) or any(not isinstance(p, str) or not p for p in roots):
        raise ValueError("allowed_roots must be an array of nonempty paths")
    try:
        return Settings(
            read_only=data.get("read_only", True),
            allowed_roots=tuple(Path(p) for p in roots),
            backend=BackendSettings(**backend),
        )
    except TypeError as exc:
        raise ValueError(f"Invalid backend setting: {exc}") from exc
