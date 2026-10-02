# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""In-memory BronzeReceiptStore — for tests and ephemeral pipelines.

Satisfies axiom.medallion.receipts.BronzeReceiptStore. No persistence;
data lives only as long as the instance does. Use this in test suites
that need a real store object but don't want a temp file or DB.
"""

from __future__ import annotations

from typing import Any, Iterator


class MemoryBronzeReceiptStore:
    def __init__(self) -> None:
        self._compute: list[dict[str, Any]] = []
        self._agreement: list[dict[str, Any]] = []

    def write_compute_receipt(self, receipt: dict[str, Any]) -> None:
        self._compute.append(dict(receipt))

    def write_agreement_receipt(self, receipt: dict[str, Any]) -> None:
        self._agreement.append(dict(receipt))

    def lookup(self, uri: str) -> dict[str, Any] | None:
        for r in self._compute:
            if r.get("uri") == uri:
                return r
        for r in self._agreement:
            if r.get("uri") == uri:
                return r
        return None

    def iter_compute_receipts(self) -> Iterator[dict[str, Any]]:
        return iter(list(self._compute))

    def iter_agreement_receipts(self) -> Iterator[dict[str, Any]]:
        return iter(list(self._agreement))
