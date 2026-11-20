# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``edge`` source kind: pull what an ingest edge holds (ADR-177).

Importing this package registers the provider in the default
SourceKindRegistry, the same idiom as the other kinds.
"""

from __future__ import annotations

from ..registry import default_source_kind_registry
from .provider import EdgeProvider
from .puller import EdgeIntegrityError, EdgePuller, FileCursor, HttpEdge, PullReport

if not default_source_kind_registry().has("edge"):
    default_source_kind_registry().register(EdgeProvider())

__all__ = ["EdgeIntegrityError", "EdgeProvider", "EdgePuller", "FileCursor", "HttpEdge", "PullReport"]
