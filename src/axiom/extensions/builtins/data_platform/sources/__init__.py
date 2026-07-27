# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Source-kind providers + registry.

The platform's CLI and Dagster sensor never speak a specific source's
vocabulary. They look up the kind via :func:`default_source_kind_registry`
and dispatch to the kind's :class:`SourceKindProvider` for everything
kind-specific (CLI args, validation, runtime construction).

DP-1 ships the Box provider; future kinds (gdrive, sharepoint, s3, …)
land in sibling packages and register themselves the same way.
"""

from __future__ import annotations

from ..contracts import FetchedItem, IngestSource, RowBatch, TabularIngestSource
from .box import BoxApiClient, BoxBrowserApiClient, BoxIngestSource  # noqa: F401
from .contracts import SourceKindProvider, source_shape
from .http_tabular import HttpTabularProvider, HttpTabularSource  # noqa: F401
from .registry import SourceKindRegistry, default_source_kind_registry
from .sql_tabular import SqlTabularProvider, SqlTabularSource  # noqa: F401

# Re-export the platform-level types FetchedItem / IngestSource so
# consumer extensions importing `data_platform.sources` get them
# alongside the registry primitives.

__all__ = [
    "BoxApiClient",
    "BoxBrowserApiClient",
    "BoxIngestSource",
    "FetchedItem",
    "HttpTabularProvider",
    "HttpTabularSource",
    "IngestSource",
    "RowBatch",
    "SourceKindProvider",
    "SourceKindRegistry",
    "SqlTabularProvider",
    "SqlTabularSource",
    "TabularIngestSource",
    "default_source_kind_registry",
    "source_shape",
]
