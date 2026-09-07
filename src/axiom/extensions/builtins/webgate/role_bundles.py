# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Role bundles — named sets of gate scopes a role resolves to (S1 mechanism).

The gate already carries free-form ``roles`` on accounts and honors an IdP's
roles claim; scopes (``<mount>[:read|invoke|access]``) already gate what an
API key may reach. This module adds the mapping between the two: a
:class:`ScopeBundle` names a role and the scopes it grants, and a
:class:`BundleRegistry` resolves any set of roles to the union of their
scopes.

Axiom itself ships only two generic bundles as examples and test fixtures:

- ``admin`` — every mount, every governance verb (``*``)
- ``viewer`` — read-only on every mount (``*:read``)

Domain consumers — e.g. a downstream distribution — register their own role
vocabulary at startup via :func:`register_default_bundles`; the platform never
hardcodes consumer role names.

Operators may further *narrow* what a role grants per deployment via
:func:`load_overrides` — a TOML file of ``[role.<name>] scopes = [...]``
entries. Overrides are narrow-only: an override may remove scopes from a
default bundle, or define a brand-new role whose scopes stay inside what the
defaults can already grant. Any scope not already grantable via the defaults
raises :class:`OverrideWidensError` — a config file must never be a privilege
escalation path.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.http.authz_hook import parse_scope
from axiom.infra.toml_compat import tomllib

#: Env var naming the role a just-in-time-created SSO account receives when
#: the IdP sends no roles claim. Set it empty to opt out of any default role.
JIT_ROLE_ENV = "AXIOM_GATE_JIT_ROLE"
DEFAULT_JIT_ROLE = "viewer"


class OverrideWidensError(ValueError):
    """A site override tried to grant a scope the default bundles cannot."""


@dataclass(frozen=True)
class ScopeBundle:
    """A role and the scopes it grants.

    ``scopes`` use the existing gate grammar ``<mount>[:read|invoke|access]``
    (``*`` for all mounts / all verbs); the grammar is validated on
    construction so a malformed bundle fails loudly at registration, not
    silently at enforcement.
    """

    role: str
    scopes: tuple[str, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.role or not self.role.strip():
            raise ValueError("a ScopeBundle needs a non-empty role name")
        for scope in self.scopes:
            parse_scope(scope)  # raises ValueError on bad grammar


def _split_scope(scope: str) -> tuple[str, str]:
    """``(mount, verb)`` of a validated scope; verb defaults to ``*``."""
    parse_scope(scope)
    mount, sep, verb = scope.strip().partition(":")
    return mount.strip(), (verb.strip() if sep else "*")


def _covers(granted: str, wanted: str) -> bool:
    """Does the ``granted`` scope include everything ``wanted`` grants?

    ``*`` covers any mount; a bare mount (verb ``*``) covers any verb on
    that mount. This is the same subsumption the authz hook enforces at
    request time, applied statically.
    """
    g_mount, g_verb = _split_scope(granted)
    w_mount, w_verb = _split_scope(wanted)
    return (g_mount == "*" or g_mount == w_mount) and (g_verb == "*" or g_verb == w_verb)


class BundleRegistry:
    """A name → :class:`ScopeBundle` index with union resolution."""

    def __init__(self) -> None:
        self._bundles: dict[str, ScopeBundle] = {}

    def register(self, bundle: ScopeBundle, *, replace: bool = False) -> None:
        """Register ``bundle``; refuses to silently redefine a role."""
        if not replace and bundle.role in self._bundles:
            raise ValueError(f"role bundle {bundle.role!r} is already registered")
        self._bundles[bundle.role] = bundle

    def get(self, role: str) -> ScopeBundle | None:
        return self._bundles.get(role)

    def roles(self) -> tuple[str, ...]:
        """Registered role names, sorted."""
        return tuple(sorted(self._bundles))

    def resolve(self, roles: Iterable[str]) -> tuple[str, ...]:
        """The union of scopes granted by ``roles`` — sorted, deduped.

        Roles with no registered bundle contribute nothing (they stay
        free-form gate roles the consumer layer interprets).
        """
        out: set[str] = set()
        for role in roles:
            bundle = self._bundles.get(role)
            if bundle is not None:
                out.update(bundle.scopes)
        return tuple(sorted(out))

    def grantable(self, scope: str) -> bool:
        """Is ``scope`` within what some registered bundle already grants?"""
        return any(
            _covers(granted, scope)
            for bundle in self._bundles.values()
            for granted in bundle.scopes
        )

    # ---- internals -------------------------------------------------------

    def _copy(self) -> BundleRegistry:
        clone = BundleRegistry()
        clone._bundles = dict(self._bundles)
        return clone

    def _unregister_for_tests(self, role: str) -> None:
        """Test hook: drop a role registered into the shared default registry."""
        self._bundles.pop(role, None)


# ---------------------------------------------------------------- defaults


#: The two generic bundles axiom ships (examples + test fixtures only).
ADMIN_BUNDLE = ScopeBundle(
    role="admin",
    scopes=("*",),
    description="every mount, every governance verb",
)
VIEWER_BUNDLE = ScopeBundle(
    role="viewer",
    scopes=("*:read",),
    description="read-only on every mount",
)

_default: BundleRegistry | None = None


def default_bundle_registry() -> BundleRegistry:
    """The process-local default registry, seeded with ``admin`` + ``viewer``."""
    global _default
    if _default is None:
        _default = BundleRegistry()
        _default.register(ADMIN_BUNDLE)
        _default.register(VIEWER_BUNDLE)
    return _default


def register_default_bundles(bundles: Iterable[ScopeBundle]) -> None:
    """Register a consumer layer's role vocabulary into the default registry.

    Domain consumers — e.g. a downstream distribution — call this at startup
    with their role sets; axiom ships only the two generic ``admin`` /
    ``viewer`` bundles as examples and test fixtures. Duplicate role names
    raise, so two layers cannot silently fight over one role's meaning.
    """
    registry = default_bundle_registry()
    for bundle in bundles:
        registry.register(bundle)


# ---------------------------------------------------------------- overrides


def load_overrides(path: Path, registry: BundleRegistry) -> BundleRegistry:
    """Apply a deployment's narrow-only role overrides to ``registry``.

    The TOML file holds ``[role.<name>]`` tables with a ``scopes`` array
    (and optional ``description``)::

        [role.viewer]
        scopes = ["docs:read"]        # narrow the shipped viewer bundle

        [role.auditor]                # a new role, inside existing grants
        scopes = ["docs:read", "llm:read"]

    Rules (fail-closed):

    - an override of an existing role may only *narrow* it — every scope must
      be covered by that bundle's own scopes;
    - a brand-new role's scopes must be a subset of what the registered
      bundles can already grant (their union, by subsumption);
    - anything wider raises :class:`OverrideWidensError` naming the scope.

    Returns a NEW registry (defaults carried forward, overridden roles
    replaced); the input registry is never mutated.
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)
    role_tables = data.get("role") or {}
    if not isinstance(role_tables, dict):
        raise ValueError(f"{path}: [role.<name>] tables expected")

    out = registry._copy()
    for name, table in role_tables.items():
        if not isinstance(table, dict):
            raise ValueError(f"{path}: [role.{name}] must be a table")
        scopes = tuple(str(s) for s in table.get("scopes", ()))
        base = registry.get(name)
        for scope in scopes:
            parse_scope(scope)  # grammar first, loudly
            narrow_enough = (
                any(_covers(granted, scope) for granted in base.scopes)
                if base is not None
                else registry.grantable(scope)
            )
            if not narrow_enough:
                raise OverrideWidensError(
                    f"override for role {name!r} widens access: scope {scope!r} "
                    "is not grantable via the default bundles (overrides may "
                    "only narrow)"
                )
        out.register(
            ScopeBundle(
                role=name,
                scopes=scopes,
                description=str(
                    table.get("description")
                    or (base.description if base else "deployment override")
                ),
            ),
            replace=True,
        )
    return out


# ---------------------------------------------------------------- JIT role


def jit_default_role() -> str:
    """The role a just-in-time-created SSO account receives when the IdP
    sends no roles claim: ``$AXIOM_GATE_JIT_ROLE``, defaulting to
    ``"viewer"``. An explicitly empty value opts out (no default role)."""
    return os.environ.get(JIT_ROLE_ENV, DEFAULT_JIT_ROLE).strip()


__all__ = [
    "ADMIN_BUNDLE",
    "DEFAULT_JIT_ROLE",
    "JIT_ROLE_ENV",
    "VIEWER_BUNDLE",
    "BundleRegistry",
    "OverrideWidensError",
    "ScopeBundle",
    "default_bundle_registry",
    "jit_default_role",
    "load_overrides",
    "register_default_bundles",
]
