# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The event ledger: every ``TriggerEvent``, appended to ``<state>/video/events.jsonl`` (VID-012).

spec-video §Events. The write is the platform's locked append
(``axiom.infra.state.locked_append_jsonl``), so the capture service and anything
else on the node can write without tearing a line. The ledger is the record and
the end of the pipeline: nothing here routes, publishes or stores an event
anywhere else. A line that does not parse (a write cut off by a power loss) is
skipped when read, never fatal.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .triggers import TriggerEvent

FILENAME = "events.jsonl"


_EXAMPLE = "2026-11-10T08:00:00+00:00"


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / "video" / FILENAME


def append(event: TriggerEvent, state_dir: Path) -> None:
    # Looked up at call time: the platform's one locked JSONL append, never a bare open("a").
    from axiom.infra import state as infra_state

    infra_state.locked_append_jsonl(ledger_path(state_dir), event.as_dict())


def parse_since(text: str) -> datetime:
    """An ISO-8601 time (a date alone is its midnight); without an offset it is local time.

    ``ValueError`` with an example otherwise.
    """
    try:
        when = datetime.fromisoformat(str(text).strip())
    except ValueError:
        raise ValueError(
            f"since {text!r} is not an ISO-8601 time; write one like {_EXAMPLE}, or a date like 2026-11-10"
        ) from None
    return when.astimezone()  # aware: kept; naive: read as local time


def read(
    state_dir: Path, *, stream: str | None = None, since: str | datetime | None = None
) -> list[dict[str, Any]]:
    """Every row in the order written, optionally one source's, optionally at or after ``since``."""
    after = parse_since(since) if isinstance(since, str) else since
    if after is not None and after.tzinfo is None:
        after = after.astimezone()
    path = ledger_path(state_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a line cut short by a power loss is not the record
        if not isinstance(row, dict):
            continue
        if stream is not None and row.get("stream") != stream:
            continue
        if after is not None:
            try:
                if datetime.fromisoformat(str(row.get("ts"))) < after:
                    continue
            except (TypeError, ValueError):
                continue
        rows.append(row)
    return rows


__all__ = ["FILENAME", "append", "ledger_path", "parse_since", "read"]
