# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bronze layer — the substrate of record per ADR-049.

The bronze writer combines two seams:
1. the provenance gate (``axiom.rag.ingest_router``) — classifies an
   item as ALLOW / QUARANTINE / EXCLUDE based on path-rules a consumer
   supplies;
2. a :class:`BronzeSink` — the storage backend (filesystem default;
   Iceberg in the ``[heavy]`` extra).

The ingest runner wraps :class:`BronzeWriter` so the
same writer drives both the lean (filesystem) and heavy (Iceberg) tiers.
"""

from __future__ import annotations

from pathlib import Path

from .router import (
    BronzeSink,
    BronzeWriter,
    BronzeWriteResult,
    TabularBronzeSink,
    TabularBronzeWriter,
    TabularWriteResult,
)
from .sinks import FilesystemBronzeSink, FilesystemTabularBronzeSink

#: Where a node keeps bronze unless told otherwise, under the node's state
#: directory (``axiom.infra.paths.get_user_state_dir``; ``~/.axi`` by default).
#: The one default every ``axi data`` verb that reads bronze uses (rederive,
#: conform-run, reindex). Kept as a display string for help text.
DEFAULT_BRONZE_ROOT = "~/.axi/bronze"


def default_bronze_root() -> Path:
    """The default bronze root: ``bronze`` under the node's state directory."""
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "bronze"


__all__ = [
    "DEFAULT_BRONZE_ROOT",
    "BronzeSink",
    "BronzeWriter",
    "BronzeWriteResult",
    "FilesystemBronzeSink",
    "FilesystemTabularBronzeSink",
    "TabularBronzeSink",
    "TabularBronzeWriter",
    "TabularWriteResult",
    "default_bronze_root",
]
