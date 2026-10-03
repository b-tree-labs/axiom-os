# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DatabaseKindProvider + registry — the OLTP layer's install abstraction.

Mirrors :mod:`data_platform.sources` for SourceKindProvider. The
platform names no RDBMS; each kind (Postgres, MySQL, SQLite, …) ships
a provider package that owns its CLI flags, validation, and DSN
construction.

DP-1 ships Postgres as the reference impl (with pgvector when the
vector-store kind co-locates). MySQL / SQLite / cloud-managed (RDS,
Cloud SQL, Azure DB) follow the same pattern.
"""

from __future__ import annotations

from .contracts import DatabaseKindProvider
from .postgres import PostgresDatabaseProvider  # noqa: F401  — self-register
from .registry import DatabaseKindRegistry, default_database_kind_registry

__all__ = [
    "DatabaseKindProvider",
    "DatabaseKindRegistry",
    "PostgresDatabaseProvider",
    "default_database_kind_registry",
]
