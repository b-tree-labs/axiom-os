# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The session seam between the memory layer and its Postgres schema (ADR-052, ADR-174).

A thin face over :class:`axiom.infra.schema_seam.SchemaSeam` for the ``memory`` schema, so the
ledger and the graph keep one import point. Tests bind a SQLite provider through
``set_provider``; production uses ``session_for('memory')``.
"""

from __future__ import annotations

from axiom.infra.schema_seam import SchemaSeam, StoreUnavailable

_seam = SchemaSeam("memory", what="the memory layer")

#: The memory layer's name for the shared error, kept so callers catch one type.
MemoryStoreUnavailable = StoreUnavailable

set_provider = _seam.set_provider
reset_provider = _seam.reset_provider
ensure_provisioned = _seam.ensure_provisioned
session_scope = _seam.session_scope
