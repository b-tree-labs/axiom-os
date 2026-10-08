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

    functions: tuple[str, ...] = ()
    """The node functions this mount serves (ADR-177), e.g. ``("ingest",)``.

    A *function profile* (:data:`FUNCTION_PROFILES`) composes a node from
    what it is for rather than from what happens to be installed: only mounts
    that declare one of its functions are served, and a mount that declares
    none is left off. That is the opposite of ``profiles``, where empty means
    everywhere, and it has to be: a node exposed to the internet must be
    built from an allow-list, so a mount added next month is not served on
    it by default."""

    bind: str | None = None
    """Optional per-mount bind hint (e.g. ``"127.0.0.1"`` loopback vs a
    LAN address). Carried through for the deployment-profile seam; not
    enforced in this cut (SRV-041 is a seam)."""

    trust_zone: str | None = None
    """Optional per-mount trust-zone label (e.g. ``"loopback"`` /
    ``"lan"``). Carried through; enforcement is minimal in this cut."""

    governs: tuple[str, ...] = ()
    """Paths under this mount that are acts of **governance** rather than of
    reading or writing — issuing a credential, granting a role, changing a
    policy, reading an audit trail (ADR-151 §2).

    Path prefixes as they appear in the URL, each matching itself and
    anything below it. A request to a declared path carries the ``govern``
    verb **whatever its method**, because the method cannot tell: ``POST``
    is ``invoke`` whether it writes a document or grants somebody a role,
    and ``GET`` is ``read`` whether it returns a measurement or the record
    of who was granted what.

    So the owning extension declares, here, beside the router — a mount that
    moves takes its declaration with it, and a reviewer sees both in one
    diff. Declaring nothing is the safe default for a mount that governs
    nothing, and the only wrong answer for one that does: until it declares,
    its governing routes are indistinguishable from its writing ones, and
    ``admin`` cannot be enforced for it at all.

    Note this makes a declared route unreadable to a ``viewer``, which is
    the intent rather than an oversight: listing who holds a grant is an act
    of governance however it arrives."""

    scope_aliases: tuple[str, ...] = ()
    """Former names of ``extension``. A credential scope that names one covers
    this mount exactly as the current name would: same verb rule, same mount,
    nothing wider. Without it, renaming an extension refuses every key issued
    under the old name from the moment a node upgrades, though each grant was
    correct when it was made."""

    def __post_init__(self) -> None:
        if not self.prefix.startswith("/"):
            raise ValueError(f"prefix must start with '/': {self.prefix!r}")
        if len(self.prefix) > 1 and self.prefix.endswith("/"):
            raise ValueError(
                f"prefix must not have a trailing slash: {self.prefix!r}"
            )
        for declared in self.governs:
            # A declaration that silently never matches is worse than no
            # declaration: it reads as enforced and enforces nothing.
            if not declared.startswith("/"):
                raise ValueError(
                    f"governs paths must start with '/': {declared!r} "
                    f"(mount {self.extension!r}) — a path that cannot match "
                    f"any request reads as governed and is not"
                )
            if len(declared) > 1 and declared.endswith("/"):
                raise ValueError(
                    f"governs paths must not have a trailing slash: "
                    f"{declared!r} (mount {self.extension!r})"
                )

    def governs_path(self, path: str) -> bool:
        """Is ``path`` one this mount declared as governing?

        A declaration matches itself and whatever is below it, by path
        segment: ``/admit`` covers ``/admit/bulk`` and not ``/admittance``.
        """
        return any(
            path == declared or path.startswith(declared.rstrip("/") + "/")
            for declared in self.governs
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
        any profile). A function profile serves only the mounts that declare
        one of its functions (see :attr:`MountSpec.functions`).
        """
        wanted = FUNCTION_PROFILES.get(profile or "")
        if wanted is not None:
            out = [s for s in self._specs.values() if set(s.functions) & set(wanted)]
            return sorted(out, key=lambda s: s.prefix)
        out = [
            s
            for s in self._specs.values()
            if profile is None or not s.profiles or profile in s.profiles
        ]
        return sorted(out, key=lambda s: s.prefix)

    def clear(self) -> None:
        """Drop all registrations (tests + idempotent re-discovery)."""
        self._specs.clear()


#: Profiles that compose a node from its functions (an allow-list), rather
#: than from everything installed. ``ingest-edge`` is the public landing node of
#: ADR-177: producers push to it and the owning node pulls from it.
FUNCTION_PROFILES: dict[str, tuple[str, ...]] = {
    "ingest-edge": ("ingest", "health"),
}


_REGISTRY = RouterRegistry()


def default_registry() -> RouterRegistry:
    """The process-global registry instance."""
    return _REGISTRY


def register_router(spec: MountSpec) -> None:
    """Module-level convenience over the process-global registry."""
    _REGISTRY.register(spec)


__all__ = [
    "FUNCTION_PROFILES",
    "MountSpec",
    "PrefixConflictError",
    "RouterRegistry",
    "default_registry",
    "register_router",
]
