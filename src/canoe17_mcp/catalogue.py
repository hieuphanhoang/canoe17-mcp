"""Accepted discovery schemas and strict pre-dispatch validation.

This is a catalogue foundation, not an MCP transport. Capability filtering and
tool handlers will register the implemented slice, never the entire catalogue.
"""

from __future__ import annotations

import copy
import json
import math
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .contracts import BackendError, ErrorCode


def load_catalogue() -> dict[str, Any]:
    resource = files("canoe17_mcp").joinpath("schemas/tools.json")
    if not resource.is_file():
        # Hatch's editable install exposes src directly, without force-includes.
        resource = Path(__file__).resolve().parents[2] / "contracts" / "tools.json"
    return json.loads(resource.read_text(encoding="utf-8"))


class Catalogue:
    def __init__(self, *, maximum_timeout_s: float = 3600) -> None:
        if (
            isinstance(maximum_timeout_s, bool)
            or not math.isfinite(maximum_timeout_s)
            or not 0 < maximum_timeout_s <= 3600
        ):
            raise ValueError("maximum_timeout_s must be finite and within 0..3600")
        self.tools = {tool["name"]: tool for tool in load_catalogue()["tools"]}
        self.maximum_timeout_s = maximum_timeout_s

    def discovery(self) -> list[dict[str, Any]]:
        """Return metadata only; never leak validationSchema into discovery."""
        result = []
        for tool in self.tools.values():
            entry = copy.deepcopy(
                {key: tool[key] for key in ("name", "description", "inputSchema", "annotations")}
            )
            timeout = entry["inputSchema"]["properties"].get("timeout_s")
            if timeout is not None:
                timeout["maximum"] = min(timeout.get("maximum", 3600), self.maximum_timeout_s)
            result.append(entry)
        return result

    def validate(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Validate the action and default confirmation; handlers apply other defaults."""
        if name not in self.tools:
            raise BackendError(ErrorCode.INVALID_ARGUMENT, f"Unknown tool: {name}")
        tool = self.tools[name]
        args = copy.deepcopy(arguments)
        # Validate first: adding defaults for other actions would make reads invalid.
        validator = Draft202012Validator(tool["validationSchema"])
        error = next(validator.iter_errors(args), None)
        if error is not None:
            raise BackendError(ErrorCode.INVALID_ARGUMENT, error.message)
        action = args.get("action", next(iter(tool["actions"])))
        write = tool["actions"][action]["effect"] == "write"
        if "timeout_s" in args and args["timeout_s"] > self.maximum_timeout_s:
            raise BackendError(ErrorCode.INVALID_ARGUMENT, "timeout_s exceeds configured maximum")
        for key in ("raw_hex", "data_hex"):
            if key not in args:
                continue
            try:
                payload = bytes.fromhex(args[key])
            except ValueError as exc:
                raise BackendError(
                    ErrorCode.INVALID_ARGUMENT, "Hex must contain whole bytes"
                ) from exc
            if key == "raw_hex" and not payload:
                raise BackendError(ErrorCode.INVALID_ARGUMENT, "Diagnostic bytes must be nonempty")
            if key == "data_hex":
                lengths = set(range(9)) | (
                    {12, 16, 20, 24, 32, 48, 64} if args.get("fd") else set()
                )
                if len(payload) not in lengths:
                    raise BackendError(ErrorCode.INVALID_ARGUMENT, "Invalid CAN payload length")
        if write:
            args.setdefault("confirm", False)
        return args, write
