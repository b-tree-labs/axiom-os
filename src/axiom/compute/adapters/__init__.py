# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Kernel adapters — one per physics code. Phase 0 ships only the mock kernel.

Per ADR-018 (revised 2026-05-04): physics = native; everything else (including
mock kernels) is plain Python.

Adapter discovery is two-tier:

1. **Static** — `_REGISTRY` carries the in-tree mock adapter, eagerly available
   without any extension installed.
2. **Entry-point** — packages that declare ``[project.entry-points."axiom.compute.adapters"]``
   are discovered on first lookup miss. This lets ``pip install axiom-ext-openmc``
   make the ``openmc`` adapter available without the user needing to import its
   adapter module manually. The discovery is cached after the first lookup; subsequent
   lookups don't re-scan.

Existing extensions can also call :func:`register_adapter` directly at import time;
both mechanisms compose. Entry-points are the AEOS-conformant default for new
physics-code extensions.
"""

from __future__ import annotations

from axiom.compute.adapters.base import CodeAdapter, KernelResult, KernelFault
from axiom.compute.adapters.mock import MockKernelAdapter

_REGISTRY: dict[str, CodeAdapter] = {
    "mock": MockKernelAdapter(),
}

# True after first entry-point scan; we only scan once per process.
_ENTRY_POINTS_LOADED: bool = False

_ENTRY_POINT_GROUP = "axiom.compute.adapters"


def _load_entry_point_adapters() -> None:
    """Discover and register adapters declared via Python entry points.

    Called lazily on the first :func:`get_adapter` miss. Each entry point in the
    ``axiom.compute.adapters`` group is loaded; the loaded object can be either
    a :class:`CodeAdapter` instance or a class (the latter is instantiated with
    no args).

    Failures during load are swallowed silently per entry point — a broken extension
    must not poison the whole registry. The entry point's name becomes the kernel name.
    """
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover — Python < 3.10 not supported
        return

    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        # Older importlib.metadata API (< Python 3.10) returns a dict.
        eps = entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[assignment]

    for ep in eps:
        if ep.name in _REGISTRY:
            continue  # Static or already-registered wins; no override surprise.
        try:
            obj = ep.load()
        except Exception:
            continue  # A broken extension's adapter must not crash dispatch.
        if isinstance(obj, type):
            try:
                obj = obj()
            except Exception:
                continue
        if isinstance(obj, CodeAdapter):
            _REGISTRY[ep.name] = obj


def get_adapter(name: str) -> CodeAdapter:
    """Look up a registered kernel adapter by name.

    On a miss, scans entry points (once per process) before raising. This is
    what makes ``pip install axiom-ext-openmc`` enough to use the ``openmc``
    kernel without manually importing its adapter module.
    """
    if name not in _REGISTRY and not _ENTRY_POINTS_LOADED:
        _load_entry_point_adapters()
    if name not in _REGISTRY:
        raise ValueError(
            f"unknown kernel {name!r}; registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def register_adapter(name: str, adapter: CodeAdapter) -> None:
    """Register a new kernel adapter (used by physics-code extensions on import).

    Existing pattern: an extension's ``adapter`` module calls ``register_adapter``
    at the bottom. New extensions should prefer the entry-point pattern (declare
    in pyproject.toml under ``[project.entry-points."axiom.compute.adapters"]``)
    so installation alone is enough — no manual import required.
    """
    _REGISTRY[name] = adapter


__all__ = [
    "CodeAdapter",
    "KernelResult",
    "KernelFault",
    "MockKernelAdapter",
    "get_adapter",
    "register_adapter",
]
