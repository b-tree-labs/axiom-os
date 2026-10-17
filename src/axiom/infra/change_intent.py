# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Record a change to this node before making it (ADR-182 D5a).

Axiom claims it never goes down from its own causes. That claim is measured,
not asserted: every downtime interval is attributed to a cause, and it is
"ours" when it overlaps a change Axiom recorded itself making — a deploy, an
update, a schema migration or a configuration change.

The record is written and fsynced **before** the change acts. Written after,
the change most likely to cause an outage — the one that crashed half way —
would leave no trace, and its outage would read as unexplained or as somebody
else's. An intent with no outcome is therefore still a change, and it stays
open: an outage after a crashed change is ours until shown otherwise.

The ledger is append-only JSON lines in the machine state directory
(``<state>/availability/changes.jsonl``), one ``intent`` line and, when the
change finishes, one ``outcome`` line sharing its id.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["CHANGE_KINDS", "Change", "changes", "ledger_path", "overlapping", "record_change"]

#: The four kinds of change that count as "ours". Closed, because the
#: attribution reads them and a free-text kind cannot be counted.
CHANGE_KINDS = ("deploy", "update", "migration", "config")


@dataclass
class Change:
    id: str
    kind: str
    subject: str
    node: str
    started: datetime
    ended: datetime | None = None
    outcome: str | None = None
    detail: str = ""


class _Open:
    """Handed to the body of :func:`record_change`; it may name its outcome."""

    def __init__(self, change_id: str) -> None:
        self.id = change_id
        self.outcome: str | None = None
        self.detail = ""


def ledger_path() -> Path:
    """``AXI_CHANGE_LEDGER`` if set, else ``<state>/availability/changes.jsonl``."""
    override = os.environ.get("AXI_CHANGE_LEDGER")
    if override:
        return Path(override)
    from axiom.infra.paths import get_platform_state_dir

    return get_platform_state_dir() / "availability" / "changes.jsonl"


def _append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def _now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def record_change(
    kind: str,
    subject: str,
    *,
    detail: str = "",
    node: str | None = None,
    path: Path | None = None,
) -> Iterator[_Open]:
    """Record ``kind`` of change to ``subject``, then run the body.

    The intent is on disk before the body runs. On exit the outcome is
    ``ok``, or ``failed`` if the body raised (the exception propagates), or
    whatever the body set on the yielded handle (``rolled_back``, ...).
    """
    if kind not in CHANGE_KINDS:
        raise ValueError(f"change kind {kind!r} is not one of {', '.join(CHANGE_KINDS)}")
    target = path or ledger_path()
    handle = _Open(uuid.uuid4().hex)
    _append(
        target,
        {
            "phase": "intent",
            "id": handle.id,
            "kind": kind,
            "subject": subject,
            "node": node or socket.gethostname(),
            "at": _now().isoformat(),
            "detail": detail,
        },
    )
    try:
        yield handle
    except BaseException as exc:
        _append(
            target,
            {
                "phase": "outcome",
                "id": handle.id,
                "outcome": "failed",
                "at": _now().isoformat(),
                "detail": f"{type(exc).__name__}: {exc}"[:500],
            },
        )
        raise
    _append(
        target,
        {
            "phase": "outcome",
            "id": handle.id,
            "outcome": handle.outcome or "ok",
            "at": _now().isoformat(),
            "detail": handle.detail,
        },
    )


def changes(*, path: Path | None = None) -> list[Change]:
    """Every recorded change, oldest first. A torn line is skipped."""
    target = path or ledger_path()
    if not target.exists():
        return []
    by_id: dict[str, Change] = {}
    for raw in target.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw)
            at = datetime.fromisoformat(entry["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if entry.get("phase") == "intent":
            by_id[entry["id"]] = Change(
                id=entry["id"],
                kind=entry.get("kind", ""),
                subject=entry.get("subject", ""),
                node=entry.get("node", ""),
                started=at,
                detail=entry.get("detail", ""),
            )
        elif entry.get("phase") == "outcome" and entry.get("id") in by_id:
            c = by_id[entry["id"]]
            c.ended = at
            c.outcome = entry.get("outcome")
            if entry.get("detail"):
                c.detail = entry["detail"]
    return sorted(by_id.values(), key=lambda c: c.started)


def overlapping(start: datetime, end: datetime, *, path: Path | None = None) -> list[Change]:
    """Changes that touch ``[start, end]``. An unfinished change is open-ended."""
    return [
        c
        for c in changes(path=path)
        if c.started <= end and (c.ended is None or c.ended >= start)
    ]
