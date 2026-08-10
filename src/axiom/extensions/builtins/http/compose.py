# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``compose_app`` — build one FastAPI app from many registered routers.

This is the second piece of the serving substrate (spec-serve §5): take
:func:`create_app`'s app, install the shared middleware chain, run
manifest discovery, then mount every registered :class:`MountSpec` in
sorted order. A prefix collision raises :class:`PrefixConflictError`
before a socket is ever bound (SRV-002/004).

Discovery (spec §4) walks installed extensions' AEOS ``service``
manifests, imports each entry that declares a ``prefix``, calls it to
obtain a ``MountSpec``, and registers it — so installing an HTTP-serving
extension is enough to mount it. The three built-in consumers
(ingest_sink, classroom, herald) are also available as built-in
factories (:mod:`.mounts`) for the zero-config path.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, replace

from fastapi import APIRouter, FastAPI

from .middleware import MiddlewareConfig, install_middleware
from .mounts import BUILTIN_MOUNT_FACTORIES
from .registry import MountSpec, RouterRegistry, default_registry
from .server import create_app

_LOGGER = logging.getLogger("axi.serve")


@dataclass(frozen=True)
class RouteTableEntry:
    """One row of the composed route table (``axi serve --list``)."""

    prefix: str
    extension: str
    requires_authz: bool
    profiles: tuple[str, ...]
    trust_zone: str | None


def discover_mounts(
    registry: RouterRegistry,
    *,
    manifests: list[dict] | None = None,
) -> None:
    """Discover service mounts from extension manifests (SRV-003).

    ``manifests`` is the list of loaded extension manifest dicts; when
    ``None`` discovery is a no-op (the extension loader supplies them in a
    wired deployment). Each ``service`` provide-block has its ``entry``
    imported and called; an entry that returns a :class:`MountSpec` is
    registered (an entry returning anything else — e.g. ``create_app`` —
    is skipped). The serve-specific ``prefix`` field, when present on the
    block, is the documented namespace; the authoritative prefix is the
    one carried by the returned ``MountSpec``. Idempotent: a prefix
    already registered is skipped.
    """
    if not manifests:
        return
    for manifest in manifests:
        provides = manifest.get("extension", {}).get("provides", [])
        for block in provides:
            if block.get("kind") != "service":
                continue
            entry = block.get("entry")
            if not entry:
                continue
            spec = _load_entry(entry)
            if spec is None:
                continue
            if any(s.prefix == spec.prefix for s in registry.specs()):
                continue  # idempotent
            registry.register(spec)


def _load_entry(entry: str) -> MountSpec | None:
    """Import ``module:attr`` and call it to get a MountSpec."""
    module_name, _, attr = entry.partition(":")
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attr)
        spec = factory()
    except Exception:  # noqa: BLE001 — a broken mount must not sink the app
        _LOGGER.exception("failed to load service mount entry %s", entry)
        return None
    if not isinstance(spec, MountSpec):
        _LOGGER.warning("entry %s did not return a MountSpec", entry)
        return None
    return spec


def register_builtin_mounts(registry: RouterRegistry) -> None:
    """Register the three built-in consumer mounts (SRV-005).

    Skips any prefix already registered so this composes cleanly with
    manifest discovery and explicit programmatic registration.
    """
    for factory in BUILTIN_MOUNT_FACTORIES:
        try:
            spec = factory()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("built-in mount factory %r failed", factory)
            continue
        if any(s.prefix == spec.prefix for s in registry.specs()):
            continue
        registry.register(spec)


def compose_app(
    *,
    profile: str | None = None,
    middleware: MiddlewareConfig | None = None,
    registry: RouterRegistry | None = None,
    manifests: list[dict] | None = None,
    include_builtins: bool = True,
    allow_insecure: bool = False,
    auto_authz: bool = True,
) -> FastAPI:
    """Build the composed app (spec §5).

    Steps: ``create_app()`` → install middleware → manifest discovery →
    register built-ins → mount every registered ``MountSpec`` filtered by
    ``profile`` in sorted order. Raises :class:`PrefixConflictError` on a
    collision before any socket is bound.

    ``middleware=None`` installs logging + error normalization only (the
    authz / peer-sig seams stay off) — except that when ``auto_authz`` is set
    (the default) and no ``authz`` hook was supplied, ``compose_app`` asks
    :func:`authz_hook.maybe_default_authz_hook` to wire the real GUARD engine.
    The adapter returns ``None`` (leaving the seam off, so the fail-closed
    refusal below still applies) when there is nothing safe to wire — e.g.
    production with no API keys configured. Pass ``auto_authz=False`` to test
    or run the raw substrate without the default adapter.

    **Fail-closed authz (SRV-022).** A mount declaring ``requires_authz=True``
    is NOT served unless an authz hook is configured (``middleware.authz``).
    Serving an auth-required route with no enforcement is a security hole —
    e.g. the external-write ``POST /ingest`` would accept anonymous writes.
    Such mounts are skipped + loudly logged. Pass ``allow_insecure=True`` (or
    set ``AXIOM_SERVE_INSECURE=1``) to deliberately serve them without authz
    (dev/loopback only) — an explicit, audited opt-out, never the default.
    """
    reg = registry if registry is not None else default_registry()
    discover_mounts(reg, manifests=manifests)
    if include_builtins:
        register_builtin_mounts(reg)

    app = create_app(
        title="Axiom",
        version="0.1.0",
        description="Composed Axiom HTTP substrate (serve).",
    )
    mw = middleware or MiddlewareConfig()
    # Auto-wire the uniform authz adapter over GUARD when no hook was supplied
    # (the spec's intended posture: installing authz is enough to gate every
    # mount). maybe_default_authz_hook returns None when there is nothing safe
    # to wire, so the fail-closed refusal below still holds.
    if auto_authz and mw.authz is None:
        from .authz_hook import maybe_default_authz_hook

        hook = maybe_default_authz_hook()
        if hook is not None:
            mw = replace(mw, authz=hook)
    install_middleware(app, mw, specs=lambda: reg.specs(profile=profile))

    authz_enforced = mw.authz is not None
    insecure = allow_insecure or os.environ.get("AXIOM_SERVE_INSECURE") in {"1", "true", "yes"}

    for spec in reg.specs(profile=profile):
        # Fail-closed: never serve an auth-required mount with no authz hook.
        if spec.requires_authz and not authz_enforced and not insecure:
            _LOGGER.error(
                "mount %s (%s) requires authz but no authz hook is configured "
                "— REFUSED (fail-closed). Wire MiddlewareConfig.authz, or pass "
                "allow_insecure=True / AXIOM_SERVE_INSECURE=1 for dev/loopback.",
                spec.prefix, spec.extension)
            continue
        # The built-in consumer routers carry their own full paths, so
        # they mount at the app root; prefix is the namespace claim, not a
        # router prefix (avoids double-prefixing — see mounts.py).
        # Per-mount fault isolation: one router failing to include must NOT
        # sink the whole app (con-mitigation for the single-engine design —
        # spec-serve §14.1 "coupled blast radius"). Skip + log; siblings live.
        #
        # Validate the router type BEFORE include_router: on FastAPI >= 0.138 a
        # malformed router gets partway into app.router.routes before raising,
        # which then poisons the NEXT include (it iterates the corrupt entry).
        # Rejecting non-APIRouter mounts up front keeps isolation real — the
        # bad mount never mutates app state, so siblings include cleanly.
        if not isinstance(spec.router, APIRouter):
            _LOGGER.error(
                "mount %s (%s) is not an APIRouter (%s) — skipped; "
                "other mounts unaffected",
                spec.prefix, spec.extension, type(spec.router).__name__)
            continue
        try:
            app.include_router(spec.router)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "mount %s (%s) failed to include — skipped; other mounts unaffected",
                spec.prefix, spec.extension)
            continue
    return app


def route_table(
    *,
    profile: str | None = None,
    registry: RouterRegistry | None = None,
    manifests: list[dict] | None = None,
    include_builtins: bool = True,
) -> list[RouteTableEntry]:
    """Return the composed route table without binding a socket (SRV-013)."""
    reg = registry if registry is not None else default_registry()
    discover_mounts(reg, manifests=manifests)
    if include_builtins:
        register_builtin_mounts(reg)
    return [
        RouteTableEntry(
            prefix=s.prefix,
            extension=s.extension,
            requires_authz=s.requires_authz,
            profiles=s.profiles,
            trust_zone=s.trust_zone,
        )
        for s in reg.specs(profile=profile)
    ]


__all__ = [
    "RouteTableEntry",
    "compose_app",
    "discover_mounts",
    "register_builtin_mounts",
    "route_table",
]
