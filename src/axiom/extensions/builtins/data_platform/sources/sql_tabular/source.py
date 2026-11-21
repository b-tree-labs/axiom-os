# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``SqlTabularSource`` — tabular rows from a read-only SQL extract query.

The row-lane source for "the authoritative store is an external OLTP DB":
:meth:`fetch_rows` runs a declared read-only ``SELECT`` (or ``WITH``) against a
DSN and returns the result set as a :class:`RowBatch`. The connection is opened
**read-only** (``conn.read_only = True``) as defense-in-depth — a bug in the
platform can never write the source of record.

``psycopg`` (v3) is imported lazily inside the query path, so importing this
module (e.g. to register the kind) never requires the driver.
"""

from __future__ import annotations

import json as _json
from datetime import datetime

from ...contracts import RowBatch


def _rows_to_bytes(rows: list[dict]) -> bytes:
    """Canonical bytes of the result set — content-addressed for replay/audit."""
    return _json.dumps(rows, sort_keys=True, default=str).encode("utf-8")


class SqlTabularSource:
    """A pollable tabular source backed by a read-only SQL extract."""

    def __init__(
        self,
        *,
        name: str,
        dsn: str,
        query: str,
        schema_ref: str,
        connect_timeout: int = 30,
    ) -> None:
        self.name = name
        self._dsn = dsn
        self.query = query
        self.schema_ref = schema_ref
        self._connect_timeout = connect_timeout

    def list_changed(self, since: datetime | None = None) -> list[str]:
        # One extract = one logical batch; content_hash dedup handles "unchanged".
        return ["current"]

    def fetch_rows(self, item: str) -> RowBatch:
        rows = self._run_query(self.query)
        return RowBatch(
            source_name=self.name,
            item_id=item,
            etag=None,
            modified_at=None,
            schema_ref=self.schema_ref,
            rows=rows,
            raw=_rows_to_bytes(rows),
            source_path=f"sql:{self.name}",
        )

    def _run_query(self, sql: str) -> list[dict]:
        import psycopg

        conn = psycopg.connect(self._dsn, autocommit=True, connect_timeout=self._connect_timeout)
        try:
            conn.read_only = True  # defense-in-depth: never write the source of record
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d.name for d in cur.description] if cur.description else []
                return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
        finally:
            conn.close()


__all__ = ["SqlTabularSource"]
