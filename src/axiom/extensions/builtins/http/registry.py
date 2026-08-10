# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Router registry for the composed HTTP substrate (spec-serve §3).

Extensions contribute a :class:`MountSpec` — a ``(prefix, router, …)``
triple — to a process-global :class:`RouterRegistry`. ``compose_app``
(see :mod:`.compose`) reads the registry and mounts every spec onto one
FastAPI app.

This is the *mechanism* of the serving substrate's first piece. It has
no opinion on authorization, transport, or federation — those ride the
registry (authz seam) or the middleware chain (peer-sig seam) without
the registry knowing about them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter


class PrefixConflictError(ValueError):
    """Two mounts claim conflicting path prefixes (SRV-004).

    Raised at registration time (and re-checkable at compose time) so a
    collision fails loudly before a socket is ever bound.
    """


@dataclass(frozen=True)
class MountSpec:
    """A router an extension contributes to the composed app.

    The router itself carries its own route paths (today's three
    consumers already decorate ``@router.post("/ingest")`` etc.), so
    ``prefix`` is primarily the *namespace* used for the route table,
    conflict detection, and sorted compose order. ``compose_app`` mounts
    routers without re-prefixing to avoid double-prefixing existing
    consumers; ``prefix`` remains the authoritative namespace claim.
    """

    prefix: str
    """e.g. ``"/ingest"`` — must start with ``"/"``, no trailing slash."""

    router: APIRouter
    """The FastAPI router to mount."""

    extension: str
    """Owning extension name (for ``--list`` + conflict reporting)."""

    requires_authz: bool = True
    """Opt out only for genuinely public routes (e.g. ``/.well-known``)."""

    profiles: tuple[str, ...] = ()
    """Empty = all profiles. e.g. ``("server",)`` gates the mount."""

    bind: str | None = None
    """Optional per-mount bind hint (e.g. ``"127.0.0.1"`` loopback vs a
    LAN address). Carried through for the deployment-profile seam; not
    enforced in this cut (SRV-041 is a seam)."""

    trust_zone: str | None = None
    """Optional per-mount trust-zone label (e.g. ``"loopback"`` /
    ``"lan"``). Carried through; enforcement is minimal in this cut."""

    def __post_init__(self) -> None:
        if not self.prefix.startswith("/"):
            raise ValueError(f"prefix must start with '/': {self.prefix!r}")
        if len(self.prefix) > 1 and self.prefix.endswith("/"):
            raise ValueError(
                f"prefix must not have a trailing slash: {self.prefix!r}"
            )


def _conflicts(a: str, b: str) -> bool:
    """Two prefixes conflict if either is a path-segment prefix of the
    other (SRV-004). ``/classroom`` conflicts with ``/classroom`` and
    with ``/classroom/coordinator``; ``/classroomx`` does not conflict
    with ``/classroom``.
    """
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    # Treat "/" as a universal prefix; otherwise require a segment boundary.
    if short == "/":
        return True
    return long.startswith(short + "/")


@dataclass
class RouterRegistry:
    """Process-global registry of contributed routers."""

    _specs: dict[str, MountSpec] = field(default_factory=dict)

    def register(self, spec: MountSpec) -> None:
        """Register a router. Raises :class:`PrefixConflictError` if
        ``spec.prefix`` collides with an already-registered prefix.
        """
        for existing in self._specs.values():
            if _conflicts(existing.prefix, spec.prefix):
                raise PrefixConflictError(
                    f"mount prefix {spec.prefix!r} (from {spec.extension!r}) "
                    f"conflicts with {existing.prefix!r} "
                    f"(from {existing.extension!r})"
                )
        self._specs[spec.prefix] = spec

    def specs(self, *, profile: str | None = None) -> list[MountSpec]:
        """Return registered specs sorted by prefix (SRV-006), filtered
        to ``profile`` when given (a spec with empty ``profiles`` matches
        any profile).
        """
        out = [
            s
            for s in self._specs.values()
            if profile is None or not s.profiles or profile in s.profiles
        ]
        return sorted(out, key=lambda s: s.prefix)

    def clear(self) -> None:
        """Drop all registrations (tests + idempotent re-discovery)."""
        self._specs.clear()


_REGISTRY = RouterRegistry()


def default_registry() -> RouterRegistry:
    """The process-global registry instance."""
    return _REGISTRY


def register_router(spec: MountSpec) -> None:
    """Module-level convenience over the process-global registry."""
    _REGISTRY.register(spec)


__all__ = [
    "MountSpec",
    "PrefixConflictError",
    "RouterRegistry",
    "default_registry",
    "register_router",
]
