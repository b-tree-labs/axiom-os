# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""BronzeStore — in-memory, partitioned, append-only raw-event store.

Partitions are (source, day). No update/delete/upsert. Scans are ordered
by insert order within a partition. Production backing is SeaweedFS +
Apache Iceberg; swap at the BronzeStore seam without touching callers.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class BronzeStore:
    def __init__(self) -> None:
        # {source: {day: [rows]}}
        self._partitions: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def append(self, *, source: str, day: str, row: dict[str, Any]) -> None:
        self._partitions.setdefault(source, {}).setdefault(day, []).append(dict(row))

    def scan(self, *, source: str, day: str) -> Iterator[dict[str, Any]]:
        return iter(self._partitions.get(source, {}).get(day, []))

    def scan_range(
        self, *, source: str, start_day: str, end_day: str
    ) -> Iterator[dict[str, Any]]:
        """Yield rows from partitions with start_day <= day <= end_day.
        String comparison works because ISO dates sort lexicographically."""
        by_day = self._partitions.get(source, {})
        for day in sorted(by_day):
            if start_day <= day <= end_day:
                yield from by_day[day]
