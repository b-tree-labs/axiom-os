# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Retrieval's reaper — removing one tenant's corpora, and nothing else.

The whole difficulty here is one distinction. A tenant **reads** more than it
**owns**: a flow-loop facility draws on general reactor and molten-salt
literature that is often more pertinent to it than the host's own TRIGA
material, and it owns none of that. So an offboard deletes the corpora
namespaced to the tenant and leaves the library standing.

That is not a nicety. Deleting a shared corpus while removing one tenant would
silently degrade every *other* tenant's retrieval, and nothing in the system
would report it — the corpus would simply stop being found. It is the failure
this reaper is shaped to make impossible: it never asks what a tenant may
read, only what it owns.
"""

from __future__ import annotations

from collections.abc import Iterable

from axiom.infra.tenancy import TenantResource

from .retriever import owned_corpora


class RagReaper:
    """Deletes a tenant's own corpora from the retrieval store."""

    subsystem = "rag"

    def __init__(self, store) -> None:  # noqa: ANN001 — a RagStore-like
        self._store = store

    def _inventory(self) -> dict[str, int]:
        stats = self._store.stats() or {}
        return dict(stats.get("chunks_by_corpus") or {})

    def find(self, site: str) -> Iterable[TenantResource]:
        inventory = self._inventory()
        return [
            TenantResource(
                subsystem=self.subsystem,
                kind="corpus",
                identifier=corpus,
                detail=f"{inventory.get(corpus, 0)} chunk(s)",
            )
            for corpus in owned_corpora(site, sorted(inventory))
        ]

    def reap(self, site: str) -> Iterable[str]:
        removed = []
        for corpus in owned_corpora(site, sorted(self._inventory())):
            count = self._store.delete_corpus(corpus)
            removed.append(f"rag: deleted corpus {corpus!r} ({count} chunk(s))")
        return removed


__all__ = ["RagReaper"]
