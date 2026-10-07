"""Qualified object IDs: building, fingerprinting and resolving.

An ID is ``<prefix>:<escaped name>``, with ``@<n>`` (1-based position among
same-named siblings) appended to every duplicate. A fingerprint of the
collection (names and paths, in order) is recorded when IDs are issued;
resolving an ``@n`` ID against a collection with a different fingerprint is
``STALE_SESSION``, because positions may have shifted.
"""

from __future__ import annotations

import hashlib
import threading
from collections import Counter
from dataclasses import dataclass

from canoe17_mcp.contracts import BackendError, ErrorCode, escape_id_segment


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    path: str = ""


def fingerprint(entries: list[Entry] | tuple[Entry, ...]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(e.name.encode("utf-8") + b"\0" + e.path.lower().encode("utf-8") + b"\n")
    return h.hexdigest()[:16]


def build_ids(prefix: str, entries: list[Entry] | tuple[Entry, ...]) -> list[str]:
    counts = Counter(e.name for e in entries)
    seen: Counter[str] = Counter()
    out = []
    for e in entries:
        base = f"{prefix}:{escape_id_segment(e.name)}"
        if counts[e.name] > 1:
            seen[e.name] += 1
            base += f"@{seen[e.name]}"
        out.append(base)
    return out


class IdRegistry:
    """Remembers, per collection and epoch, the fingerprint IDs were issued for."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issued: dict[str, tuple[int, str]] = {}

    def issue(self, collection: str, epoch: int, entries: list[Entry]) -> list[str]:
        ids = build_ids(_prefix(collection), entries)
        with self._lock:
            self._issued[collection] = (epoch, fingerprint(entries))
        return ids

    def clear(self) -> None:
        with self._lock:
            self._issued.clear()

    def resolve(self, collection: str, epoch: int, entries: list[Entry], object_id: str) -> int:
        """Return the 0-based index of ``object_id`` in ``entries`` (read live)."""
        prefix = _prefix(collection) + ":"
        if not object_id.startswith(prefix):
            raise BackendError(
                ErrorCode.INVALID_ARGUMENT, f"{object_id!r} is not a {collection} ID ({prefix}...)."
            )
        ids = build_ids(_prefix(collection), entries)
        if "@" in object_id[len(prefix):]:
            with self._lock:
                issued = self._issued.get(collection)
            if issued is None or issued[0] != epoch or issued[1] != fingerprint(entries):
                raise BackendError(
                    ErrorCode.STALE_SESSION,
                    f"{object_id} identifies one of several same-named objects and the "
                    f"{collection} list changed since it was issued. List again, then retry.",
                    retryable=True,
                )
        if object_id in ids:
            return ids.index(object_id)
        bare = [i for i, x in enumerate(ids) if x.split("@", 1)[0] == object_id]
        if len(bare) > 1:
            raise BackendError(
                ErrorCode.AMBIGUOUS_ID,
                f"{object_id} matches {len(bare)} objects; use one of the listed @n IDs.",
                details=tuple(("candidate", ids[i]) for i in bare),
            )
        raise BackendError(ErrorCode.NOT_FOUND, f"No {collection} with ID {object_id}.")


_PREFIXES = {
    "databases": "db",
    "buses": "bus",
    "nodes": "node",
    "diag_descriptions": "diag",
    "test_environments": "env",
    "simulation_test_nodes": "tm-sim",
}


def _prefix(collection: str) -> str:
    return _PREFIXES[collection]
