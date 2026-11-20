# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``push`` source-kind provider package (ADR-106).

Importing this package registers the provider in the default
SourceKindRegistry — the same idiom as the other kinds.
"""

from __future__ import annotations

from ..registry import default_source_kind_registry
from .provider import PushProvider
from .source import PushSource

if not default_source_kind_registry().has("push"):
    default_source_kind_registry().register(PushProvider())

__all__ = ["PushProvider", "PushSource"]
