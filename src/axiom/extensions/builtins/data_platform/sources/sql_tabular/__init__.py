# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``sql-tabular`` source-kind provider package (ADR-001).

Self-contained: source impl + provider + tests. Importing this package
registers the provider in the default SourceKindRegistry — same idiom as Box.
"""

from __future__ import annotations

from ..registry import default_source_kind_registry
from .provider import SqlTabularProvider
from .source import SqlTabularSource

if not default_source_kind_registry().has("sql-tabular"):
    default_source_kind_registry().register(SqlTabularProvider())


__all__ = ["SqlTabularProvider", "SqlTabularSource"]
