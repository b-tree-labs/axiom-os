# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Site-tunable values a declaration refers to by ``site_key``.

A declaration says ``every = { site_key = "x.y.z", default = 30 }``: the site
may set ``x.y.z``, otherwise the default holds. The extension that owns the
site's configuration registers one function from a site id to its settings
mapping; attest never reads site configuration itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

_source: Callable[[str], Mapping[str, Any]] | None = None


def register(source: Callable[[str], Mapping[str, Any]]) -> None:
    global _source
    _source = source


def unregister() -> None:
    global _source
    _source = None


def value(site_id: str, key: str | None, default: Any) -> Any:
    """``key`` (dotted) from the site's settings, else ``default``."""
    if not key or _source is None:
        return default
    node: Any = _source(site_id)
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


__all__ = ["register", "unregister", "value"]
