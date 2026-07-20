# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Box source-kind provider package.

Self-contained: source impl + API client + provider + tests live here.
Importing this package registers the Box provider in the default
:class:`SourceKindRegistry` — same idiom future kinds use.
"""

from __future__ import annotations

# Idiomatic self-registration on first import. Tests that need a clean
# registry build their own SourceKindRegistry() and skip this default.
from ..registry import default_source_kind_registry
from .api import BoxBrowserApiClient
from .provider import BoxSourceProvider
from .source import BoxApiClient, BoxIngestSource

if not default_source_kind_registry().has("box"):
    default_source_kind_registry().register(BoxSourceProvider())


__all__ = [
    "BoxApiClient",
    "BoxBrowserApiClient",
    "BoxIngestSource",
    "BoxSourceProvider",
]
