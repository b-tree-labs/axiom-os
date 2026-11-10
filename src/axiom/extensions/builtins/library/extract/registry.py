# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which extractors this install has, and which of them a file gets (ADR-213 D3, D4).

Sources, composed in this order (later scopes outrank earlier ones on a tie):

1. **core**: the generic extractors in :mod:`.core`, registered in code;
2. **pack**: ``[[extractors]]`` in every discovered extension's manifest, and the
   ``axiom.library_contributions`` entry point group;
3. **site** / **kit**: added by a host or a kit sandbox (:func:`register`,
   :func:`register_manifest`).

**Fan-out.** Every matching extractor runs, not just the best one: a PDF gets
text and a domain extractor; a CSV gets column profiling and a dataset reader.
**Order** is priority (high first), then scope rank (kit > site > pack > core),
then match strength (magic > sniff > name > extension > MIME), then name.
**Narrowing:** a matched extractor's ``supersedes`` removes the named ones, and
an ``exclusive`` one removes everything after it except ``always`` extractors.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.extract.contract import (
    SCOPE_RANK,
    AcceptedType,
    ExtractionContext,
    ExtractorManifest,
    ManifestError,
    manifest_from_dict,
)

log = logging.getLogger(__name__)


@dataclass
class Registered:
    manifest: ExtractorManifest
    #: The implementation, or ``None`` until first use (resolved from ``entry``).
    impl: Any = None

    def resolve(self) -> Any:
        if self.impl is None:
            if not self.manifest.entry:
                raise ManifestError(f"{self.manifest.name}: no entry and no implementation")
            self.impl = contrib.resolve_entry(self.manifest.entry)
        return self.impl

    def missing(self) -> list[str]:
        return [m for m in self.manifest.requires if importlib.util.find_spec(m.split(".")[0]) is None]


@dataclass
class Planned:
    """One extractor the pipeline will (or will not) run on a file, and why."""

    entry: Registered
    strength: int
    #: Empty when it runs; otherwise why not (too large, unavailable, superseded).
    skip: str = ""

    @property
    def name(self) -> str:
        return self.entry.manifest.name


_lock = threading.RLock()
_local: dict[str, Registered] = {}
_merged: dict[str, Registered] | None = None


def _reset_merged() -> None:
    global _merged
    with _lock:
        _merged = None


contrib.on_change(_reset_merged)


def register(manifest: ExtractorManifest | dict[str, Any], impl: Any = None, *, scope: str | None = None) -> ExtractorManifest:
    """Add one extractor (a host's own, a kit's, a test's). The name is the identity."""
    m = manifest if isinstance(manifest, ExtractorManifest) else manifest_from_dict(manifest, scope=scope or "")
    with _lock:
        _local[m.name] = Registered(m, impl)
    _reset_merged()
    return m


def unregister(name: str) -> None:
    with _lock:
        _local.pop(name, None)
    _reset_merged()


def clear_local() -> None:
    """Tests only."""
    with _lock:
        _local.clear()
    _reset_merged()


def _build() -> dict[str, Registered]:
    from axiom.extensions.builtins.library.extract.core import core_extractors

    out: dict[str, Registered] = {}
    for m, impl in core_extractors():
        out[m.name] = Registered(m, impl)
    for c in contrib.contributions("extractors"):
        if c.data.get("module") and not c.data.get("version"):
            continue  # a legacy sense extractor ([[extractors]] with module/file_patterns), not an upload extractor
        try:
            scope = c.scope if c.scope != "core" else "core"
            m = manifest_from_dict(c.data, scope=c.data.get("scope") or scope, contributed_by=c.source)
        except (ManifestError, KeyError, ValueError, TypeError) as exc:
            log.warning("library: extractor from %s refused: %s", c.source, exc)
            continue
        if m.scope == "kit":
            # Contributor code runs out of process (ADR-203 D6).
            m = _with(m, isolation="process")
        if m.name in out and out[m.name].manifest.scope == "core" and m.scope == "core":
            continue  # the library's own manifest restating a core extractor
        out[m.name] = Registered(m)
    out.update(_local)
    return out


def _with(m: ExtractorManifest, **changes: Any) -> ExtractorManifest:
    from dataclasses import replace

    return replace(m, **changes)


def all_extractors() -> dict[str, Registered]:
    global _merged
    with _lock:
        if _merged is None:
            _merged = _build()
        return dict(_merged)


def get(name: str) -> Registered | None:
    return all_extractors().get(name)


def describe_all() -> list[dict[str, Any]]:
    out = []
    for r in sorted(all_extractors().values(), key=lambda r: (SCOPE_RANK[r.manifest.scope], r.manifest.name)):
        d = r.manifest.describe()
        missing = r.missing()
        d["available"] = not missing
        if missing:
            d["unavailable_reason"] = f"needs {', '.join(missing)} (not installed on this node)"
        out.append(d)
    return out


def accepted_types() -> dict[str, AcceptedType]:
    """File types extractors add to the library's allowlist, by extension."""
    out: dict[str, AcceptedType] = {}
    for r in all_extractors().values():
        for a in r.manifest.accepts:
            if a.extension.startswith(".") and len(a.extension) > 1:
                # Extensionless text is accepted already; an empty entry would widen nothing safely.
                out.setdefault(a.extension, a)
    return out


def _sniffed(r: Registered, ctx: ExtractionContext) -> int:
    impl = r.impl
    if impl is None and r.manifest.entry:
        try:
            impl = r.resolve()
        except Exception:  # noqa: BLE001 - an unloadable sniff is no match
            return 0
    fn: Callable[[ExtractionContext], float] | None = getattr(impl, "sniff", None)
    if fn is None:
        return 0
    try:
        return 4 if float(fn(ctx)) >= 0.5 else 0
    except Exception:  # noqa: BLE001
        return 0


def plan(ctx: ExtractionContext, *, only: set[str] | None = None) -> list[Planned]:
    """The extractors for this file, ordered, with the narrowed-out ones marked skipped."""
    head_text = ctx.head_text
    matched: list[Planned] = []
    for r in all_extractors().values():
        if only is not None and r.manifest.name not in only:
            continue
        strength = r.manifest.match.strength(name=ctx.file_name, mime=ctx.mime, head=ctx.head, head_text=head_text)
        if strength == 0:
            strength = _sniffed(r, ctx)
        if strength == 0:
            continue
        matched.append(Planned(r, strength))
    matched.sort(
        key=lambda p: (
            -p.entry.manifest.priority,
            -SCOPE_RANK[p.entry.manifest.scope],
            -p.strength,
            p.entry.manifest.name,
        )
    )
    superseded: dict[str, str] = {}
    for p in matched:
        for s in p.entry.manifest.supersedes:
            superseded.setdefault(s, p.name)
    exclusive_by = ""
    for p in matched:
        m = p.entry.manifest
        if p.name in superseded:
            p.skip = f"superseded by {superseded[p.name]}"
        elif exclusive_by and not m.always:
            p.skip = f"{exclusive_by} is exclusive for this file"
        elif ctx.size > m.limits.max_bytes:
            p.skip = f"file is larger than this extractor takes ({m.limits.max_bytes} bytes)"
        elif missing := p.entry.missing():
            p.skip = f"unavailable: needs {', '.join(missing)}"
        if not p.skip and m.exclusive and not exclusive_by:
            exclusive_by = p.name
    return matched


__all__ = [
    "Planned",
    "Registered",
    "accepted_types",
    "all_extractors",
    "clear_local",
    "describe_all",
    "get",
    "plan",
    "register",
    "unregister",
]
