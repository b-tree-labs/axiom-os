# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Storage connector surface — ADR-062.

The four-capability storage contract (read / write / watch / reply
ingest) all vendor adapters under the ``storage/`` sub-package satisfy.

Concrete adapters land in follow-up PRs (see ADR-062 § Implementation
phases). This module ships only the Protocol + capability enum +
reference types so the wizard handler and contract tests have a stable
surface to import.
"""

from __future__ import annotations

from .protocol import (
    FileContent,
    FileRef,
    ListParams,
    PutReceipt,
    ReplyEvent,
    ReplyParams,
    StorageCapability,
    StorageConnectorProvider,
    WatchHandle,
    WatchParams,
)


__all__ = [
    "FileContent",
    "FileRef",
    "ListParams",
    "PutReceipt",
    "ReplyEvent",
    "ReplyParams",
    "StorageCapability",
    "StorageConnectorProvider",
    "WatchHandle",
    "WatchParams",
]
