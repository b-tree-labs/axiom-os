# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Site-tunable values a declaration refers to by ``site_key``.

A declaration says ``every = { site_key = "x.y.z", default = 30 }``: the site
may set ``x.y.z``, otherwise the default holds. The extension that owns the
site's configuration registers one function from a site id to its settings
mapping; attest never reads site configuration itself.

A value that decides who may do what is read another way. ``authoritative_value`` consults only the
source the node's settings ledger registers, which checks that a person signed each change
(ADR-284), and never a plain source: a plain source is any extension's code, and what it returns
has no signature behind it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

_source: Callable[[str], Mapping[str, Any]] | None = None
_authoritative: Callable[[str], Mapping[str, Any]] | None = None


def register(source: Callable[[str], Mapping[str, Any]]) -> None:
    global _source
    _source = source


def register_authoritative(source: Callable[[str], Mapping[str, Any]]) -> None:
    """The one source authority keys are read from: the node's verifying settings ledger."""
    global _authoritative
    _authoritative = source


def has_authoritative() -> bool:
    return _authoritative is not None


def unregister() -> None:
    global _source, _authoritative
    _source = None
    _authoritative = None


def value(site_id: str, key: str | None, default: Any) -> Any:
    """``key`` (dotted) from the site's settings, else ``default``."""
    if key and _source is None:
        from . import registry

        registry.ensure()  # an extension's logbook_inputs registers it at discovery (#1799)
    if not key or _source is None:
        return default
    return _walk(_source(site_id), key, default)


def authoritative_value(site_id: str, key: str | None, default: Any) -> Any:
    """``key`` (dotted) from the authoritative source only, else ``default``. A plain source is
    never consulted, so a value no person signed cannot stand in for one."""
    if key and _authoritative is None:
        from . import registry

        registry.ensure()  # discovery binds the node's ledger (settings_authority.bind)
    if not key or _authoritative is None:
        return default
    return _walk(_authoritative(site_id), key, default)


def _walk(node: Any, key: str, default: Any) -> Any:
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


__all__ = [
    "authoritative_value",
    "has_authoritative",
    "register",
    "register_authoritative",
    "unregister",
    "value",
]
