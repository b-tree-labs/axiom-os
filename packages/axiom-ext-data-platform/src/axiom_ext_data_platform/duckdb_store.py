# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DuckDBBronzeReceiptStore — single-node SQL-queryable Bronze backend.

Satisfies axiom.medallion.receipts.BronzeReceiptStore. Receipts persist
as JSON text in DuckDB tables; consumers issue analytical SQL via
`store._conn` (time-travel queries, cross-receipt joins, kernel-cohort
aggregations).

Phase 6b will add an Iceberg-on-SeaweedFS backend with the same protocol;
consumer code (assess workflows, scorecard assembly, promotion gates) is
unchanged across the swap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

try:
    import duckdb
except ImportError as e:  # pragma: no cover - exercised when extra missing
    raise ImportError(
        "DuckDBBronzeReceiptStore requires the [duckdb] extra. "
        "Install with: pip install 'axiom-ext-data-platform[duckdb]'"
    ) from e


_SCHEMA = """
CREATE TABLE IF NOT EXISTS compute_receipts (
    uri          TEXT,
    receipt_json TEXT
);
CREATE TABLE IF NOT EXISTS agreement_receipts (
    uri          TEXT,
    receipt_json TEXT
);
"""


class DuckDBBronzeReceiptStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.db_path))
        self._conn.execute(_SCHEMA)

    def write_compute_receipt(self, receipt: dict[str, Any]) -> None:
        self._insert("compute_receipts", receipt)

    def write_agreement_receipt(self, receipt: dict[str, Any]) -> None:
        self._insert("agreement_receipts", receipt)

    def lookup(self, uri: str) -> dict[str, Any] | None:
        for table in ("compute_receipts", "agreement_receipts"):
            row = self._conn.execute(
                f"SELECT receipt_json FROM {table} WHERE uri = ? LIMIT 1",
                [uri],
            ).fetchone()
            if row is not None:
                return json.loads(row[0])
        return None

    def iter_compute_receipts(self) -> Iterator[dict[str, Any]]:
        return self._iter("compute_receipts")

    def iter_agreement_receipts(self) -> Iterator[dict[str, Any]]:
        return self._iter("agreement_receipts")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def _insert(self, table: str, receipt: dict[str, Any]) -> None:
        uri = receipt.get("uri")
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        self._conn.execute(
            f"INSERT INTO {table} (uri, receipt_json) VALUES (?, ?)",
            [uri, payload],
        )

    def _iter(self, table: str) -> Iterator[dict[str, Any]]:
        rows = self._conn.execute(
            f"SELECT receipt_json FROM {table} ORDER BY rowid"
        ).fetchall()
        for (payload,) in rows:
            yield json.loads(payload)
