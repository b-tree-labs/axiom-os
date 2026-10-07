# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""BronzeReceiptStore — persistence protocol for signed `axiom://` receipts.

Distinct from `axiom.medallion.bronze.BronzeStore` (generic source/day-partitioned
event ingest). This protocol is purpose-built for content-addressed,
URI-keyed receipts: compute receipts (kernel runs) and agreement receipts
(reference / cross-validator / sensor agreements).

Implementations live in extensions, not in core, so axiom doesn't pull
storage backends as required deps:

- JSONL (zero-dep dev fallback) — ships with consumer extensions
- DuckDB / Iceberg / SeaweedFS-backed — ship in `axiom-ext-data-platform`

Consumers (assess workflows, scorecard assemblers, promotion gates) hold
the protocol type, so swapping backends is a constructor change, not a
code change.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class BronzeReceiptStore(Protocol):
    """Structural protocol for Bronze-tier receipt persistence."""

    def write_compute_receipt(self, receipt: dict[str, Any]) -> None: ...
    def write_agreement_receipt(self, receipt: dict[str, Any]) -> None: ...
    def lookup(self, uri: str) -> dict[str, Any] | None: ...
    def iter_compute_receipts(self) -> Iterator[dict[str, Any]]: ...
    def iter_agreement_receipts(self) -> Iterator[dict[str, Any]]: ...
