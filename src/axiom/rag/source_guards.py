# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Per-item access at retrieval: a source that owns its chunks says who may read each one.

A corpus answers "who reads this pile" (``rag-org`` for the site, a person's own
corpus for their Restricted files). It cannot answer "this one file was shared
with three people" (prd-sharing-and-access SHR-R8.3, ADR-263 consequences). A
**source guard** can: it claims the chunks whose ``source_path`` starts with its
prefix, says which of them a principal may read, and names the extra corpora
that hold what was shared with that principal by name.

:func:`axiom.rag.retriever.retrieve` applies every guard:

- the extra corpora are searched beside the caller's own, and a chunk found
  *only* because of them is kept only when a guard claims it and permits it;
- every claimed chunk, from any corpus, is kept only when the guard permits it
  for the principal asking. With no principal the guard answers for nobody in
  particular (a source's general access), and a guard that cannot answer
  denies: a broken guard narrows retrieval, never widens it.

Guards are named here by import path, so this module depends on no extension;
a source that is not installed contributes no guard.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable
from typing import Protocol

log = logging.getLogger(__name__)


class SourceGuard(Protocol):
    #: The ``source_path`` prefix this guard owns (``library/``).
    prefix: str

    def permitted(self, principal: str | None, paths: Iterable[str]) -> set[str]:
        """The subset of ``paths`` ``principal`` may read."""
        ...

    def extra_corpora(self, principal: str | None) -> set[str]:
        """Corpora beyond the caller's own that hold what was shared with ``principal`` by name."""
        ...


#: Built-in guards, by import path (``module:attribute``), loaded on first use.
BUILTIN: tuple[str, ...] = ("axiom.extensions.builtins.library.retrieval:guard",)

_loaded: list[SourceGuard] | None = None
_override: list[SourceGuard] | None = None


def set_guards(guards: list[SourceGuard] | None) -> None:
    """Replace the guards (tests); ``None`` restores the built-ins."""
    global _override, _loaded
    _override, _loaded = guards, None


def guards() -> list[SourceGuard]:
    global _loaded
    if _override is not None:
        return list(_override)
    if _loaded is None:
        found: list[SourceGuard] = []
        for ref in BUILTIN:
            module, _, attr = ref.partition(":")
            try:
                found.append(getattr(importlib.import_module(module), attr))
            except ImportError:
                continue  # that source is not installed here, so it has no chunks to guard
        _loaded = found
    return list(_loaded)


def claimant(path: str) -> SourceGuard | None:
    for g in guards():
        if path.startswith(g.prefix):
            return g
    return None


def extra_corpora(principal: str | None) -> set[str]:
    out: set[str] = set()
    if not principal:
        return out
    for g in guards():
        try:
            out |= set(g.extra_corpora(principal))
        except Exception as exc:  # noqa: BLE001 - a guard that cannot answer adds nothing
            log.warning("source guard %s could not name extra corpora: %s", g.prefix, type(exc).__name__)
    return out


def permitted(principal: str | None, paths: Iterable[str]) -> set[str]:
    """Of ``paths``, those a guard claims and permits; unclaimed paths are not in the answer."""
    by_guard: dict[int, tuple[SourceGuard, list[str]]] = {}
    for p in paths:
        g = claimant(p)
        if g is not None:
            by_guard.setdefault(id(g), (g, []))[1].append(p)
    out: set[str] = set()
    for g, claimed in by_guard.values():
        try:
            out |= set(g.permitted(principal, claimed))
        except Exception as exc:  # noqa: BLE001 - fail closed
            log.warning("source guard %s could not decide; its chunks are withheld: %s", g.prefix,
                        type(exc).__name__)
    return out


__all__ = ["BUILTIN", "SourceGuard", "claimant", "extra_corpora", "guards", "permitted", "set_guards"]
