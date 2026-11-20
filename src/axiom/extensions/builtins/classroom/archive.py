# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom archive — FW-4 P1.

Establishes ARCHIVED as the terminal lifecycle state for a classroom.
Post-archive:

- Republish is refused (``publish_classroom`` checks ``is_archived``).
- Re-archive is an idempotent no-op — keeps the original archiver +
  timestamp + reason intact.
- The classroom record carries ``archived_at`` + ``archived_by`` +
  ``archive_reason`` for provenance.

Archive transitions require the classroom to be currently PUBLISHED.
A classroom that's never been published is in prep mode and the
correct action there is "unpublish and edit" or "delete", not
archive — archiving an unpublished classroom would produce a record
of a cohort that never happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

ARCHIVED = "archived"


def is_archived(classroom_id: str) -> bool:
    """Return True iff the classroom is in the terminal archived state."""
    from .operational_store import load_classroom_data

    data = load_classroom_data(classroom_id)
    if data is None:
        return False
    return data.get("state") == ARCHIVED


def archive_classroom(
    *,
    classroom_id: str,
    archiver: str,
    reason: str,
) -> dict[str, Any]:
    """Transition the classroom PUBLISHED → ARCHIVED.

    Idempotent: a second archive call on an already-archived classroom
    returns success with the ORIGINAL archiver/timestamp/reason so
    audit records aren't overwritten by accidental re-runs.
    """
    from .operational_store import _reg, load_classroom_data
    from .publish import PUBLISHED

    if not classroom_id or not archiver or not reason:
        return {
            "archived": False,
            "error": "classroom_id, archiver, and reason are all required",
        }

    data = load_classroom_data(classroom_id)
    if data is None:
        return {
            "archived": False,
            "error": f"classroom {classroom_id!r} not found",
        }

    current_state = data.get("state") or "unpublished"

    # Idempotent: re-archive preserves original audit fields.
    if current_state == ARCHIVED:
        return {
            "archived": True,
            "classroom_id": classroom_id,
            "state": ARCHIVED,
            "archiver": data.get("archived_by", ""),
            "archived_at": data.get("archived_at", ""),
            "reason": data.get("archive_reason", ""),
            "idempotent": True,
        }

    if current_state != PUBLISHED:
        return {
            "archived": False,
            "error": (
                f"classroom must be published before archive; current state "
                f"is {current_state!r}"
            ),
        }

    now = datetime.now(UTC).isoformat()
    updated = dict(data)
    updated["state"] = ARCHIVED
    updated["archived_at"] = now
    updated["archived_by"] = archiver
    updated["archive_reason"] = reason
    _reg().register(kind="classroom", name=classroom_id, data=updated)

    return {
        "archived": True,
        "classroom_id": classroom_id,
        "state": ARCHIVED,
        "archiver": archiver,
        "archived_at": now,
        "reason": reason,
    }
