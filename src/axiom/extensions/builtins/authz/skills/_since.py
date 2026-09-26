# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``--since`` parser shared by the audit skills.

Accepts ``Nm`` / ``Nh`` / ``Nd`` / ``Nw`` shorthand or an absolute ISO
timestamp. Returns a ``datetime`` floor; callers compare ``decided_at``
against it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_SHORTHAND = re.compile(r"^\s*(\d+)\s*(m|h|d|w)\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
    "w": 60 * 60 * 24 * 7,
}


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """Parse ``--since 7d`` / ``--since 24h`` / ISO-8601 → ``datetime``.

    Raises ``ValueError`` on garbage so the skill can surface a clean
    error rather than silently widening the window.
    """
    now = now or datetime.now(UTC)
    if not value:
        raise ValueError("--since requires a value (e.g. '7d', '24h', '2026-05-30T00:00:00Z')")

    m = _SHORTHAND.match(value)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        return now - timedelta(seconds=n * _UNIT_SECONDS[unit])

    # Absolute ISO-8601. ``fromisoformat`` accepts the trailing 'Z' in
    # Python 3.11+ but we normalize for the older shape too.
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"--since must be shorthand (Nm/Nh/Nd/Nw) or ISO-8601, got {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
