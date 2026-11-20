# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""VectorStoreProvider + registry — RAG vector layer's install abstraction.

Same shape as :mod:`data_platform.sources` and
:mod:`data_platform.database`. The platform names no vector store; each
kind (pgvector, Qdrant, Weaviate, Milvus, Chroma, LanceDB, …) ships a
provider package.

DP-1 ships pgvector as the reference impl. When co-located with the
Postgres database kind (the default), they share one instance via the
pgvector Postgres extension; the chart skips deploying a separate
vector service.
"""

from __future__ import annotations

from .contracts import VectorStoreProvider
from .pgvector import PgvectorVectorStoreProvider  # noqa: F401 — self-register
from .registry import VectorStoreRegistry, default_vector_store_registry

__all__ = [
    "PgvectorVectorStoreProvider",
    "VectorStoreProvider",
    "VectorStoreRegistry",
    "default_vector_store_registry",
]
