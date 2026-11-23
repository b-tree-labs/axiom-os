# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``VectorStoreProvider`` — the RAG vector layer's install seam.

The platform's ``axi data install`` doesn't speak pgvector or Qdrant or
Weaviate. It asks a registered provider for everything kind-specific.

A provider declares:

1. **kind name** (``"pgvector"``, ``"qdrant"``, …).
2. **CLI args** that attach to ``axi data install``.
3. **Helm values** for the chart's ``vectorStore.*`` block.
4. **Co-location compatibility** — which database kinds this vector
   store can share an instance with (e.g. pgvector says ``["postgres"]``;
   Qdrant says ``[]``). When co-located, the chart skips a separate
   vector deploy and folds the vector extension into the DB.

For DP-1 only pgvector ships; the abstraction is in place + ADR-noted
for the next vector-store kind that lands.
"""

from __future__ import annotations

import argparse
from typing import Protocol, runtime_checkable


@runtime_checkable
class VectorStoreProvider(Protocol):
    kind: str
    description: str

    colocates_with_database: list[str]
    """Database kinds this vector store can share an instance with.
    pgvector → ``["postgres"]``; Qdrant → ``[]`` (always separate)."""

    def add_install_args(self, parser: argparse.ArgumentParser) -> None:
        """Attach kind-specific install-time flags."""
        ...

    def helm_values(self, args: argparse.Namespace, *, db_kind: str) -> dict[str, str]:
        """Return Helm `--set` pairs for the chart's `vectorStore.*`
        block. ``db_kind`` is the active database kind so the provider
        can emit ``vectorStore.colocated = true`` when the DB is in
        ``colocates_with_database``."""
        ...


__all__ = ["VectorStoreProvider"]
