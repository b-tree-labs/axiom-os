# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""pgvector vector-store provider."""

from __future__ import annotations

# Self-register.
from ..registry import default_vector_store_registry
from .provider import PgvectorVectorStoreProvider

if not default_vector_store_registry().has("pgvector"):
    default_vector_store_registry().register(PgvectorVectorStoreProvider())

__all__ = ["PgvectorVectorStoreProvider"]
