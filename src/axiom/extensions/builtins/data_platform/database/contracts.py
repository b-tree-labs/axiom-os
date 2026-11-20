# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``DatabaseKindProvider`` — the OLTP/metadata-DB install seam.

The platform's ``axi data install`` doesn't speak Postgres or MySQL
or SQLite — it asks a registered provider for everything kind-specific.
Same shape as ``SourceKindProvider`` and ``VectorStoreProvider``.

A provider owns:

1. **kind name** (``"postgres"``, ``"mysql"``, …), keyed in the
   registry and used by ``--db-kind``.
2. **CLI args** that attach to ``axi data install`` (e.g. Postgres's
   ``--db-password``).
3. **Helm values** generated from parsed args (the chart consumes a
   generic ``database.*`` block; the provider supplies the kind-specific
   shape).
4. **DSN construction** so other providers (e.g. VectorStore in
   co-located mode) can build connect strings without re-implementing.

Per ADR-052 (`axiom.infra.db.session_for("<ext>")`), the actual
runtime DB access goes through the platform's DatabaseProvider — this
extension-side abstraction is for the **install** decision (which DB
to deploy + how to wire it).
"""

from __future__ import annotations

import argparse
from typing import Protocol, runtime_checkable


@runtime_checkable
class DatabaseKindProvider(Protocol):
    """Self-describing OLTP database kind provider."""

    kind: str
    """Stable identifier (``"postgres"``, ``"mysql"``, ``"sqlite"``).
    Lowercase, kebab-case allowed."""

    description: str
    """One-line human description; shown in `axi data list db-kinds`."""

    def add_install_args(self, parser: argparse.ArgumentParser) -> None:
        """Attach kind-specific install-time flags to ``axi data install``.

        The platform owns the kind-agnostic ``--db-mode`` /
        ``--db-dsn`` flags. The provider adds anything else (e.g.
        Postgres adds ``--db-password`` for the internal-mode
        bootstrap).
        """
        ...

    def helm_values(self, args: argparse.Namespace) -> dict[str, str]:
        """Return Helm ``--set`` key/value pairs the install renders.

        Keys are dotted paths into the chart's ``database.*`` block
        (e.g. ``database.internal.image.repository``). The platform's
        install command concatenates these from every active provider.
        """
        ...

    def construct_dsn(self, args: argparse.Namespace) -> str:
        """Build a connect string the chart's bundled instance would
        accept after install (or the external DSN if ``--db-mode
        external``). Used by VectorStore providers in co-located mode
        and by `axi data diagnose` for connectivity checks.
        """
        ...


__all__ = ["DatabaseKindProvider"]
