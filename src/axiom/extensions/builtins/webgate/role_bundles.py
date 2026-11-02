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

Axiom ships ADR-151's six roles (``owner``, ``admin``, ``operator``,
``creator``, ``viewer``, ``guest``), which separate governing from reading, and
ADR-222's persona roles, which say what a new colleague came to do:

- ``reader`` — data, documents and chat (the first sign-in's role)
- ``contributor`` — a reader who also submits data kits and documents
- ``operator`` — a contributor who also governs (ADR-151's ``operator``)

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
#: A deployment's role file (``[role.<name>] scopes = [...]``), applied over the
#: built-ins by :func:`load_overrides`. Unset, the built-in roles stand alone.
ROLES_FILE_ENV = "AXIOM_GATE_ROLES_FILE"
#: The floor a first sign-in lands on: ``reader`` (ADR-222). It was ``viewer``,
#: which cannot chat, so the first thing a new colleague tried answered 403.
DEFAULT_JIT_ROLE = "reader"


class OverrideWidensError(ValueError):
    """A site override tried to grant a scope the default bundles cannot."""


#: Roles whose grant must be named surface by surface rather than by a
#: wildcard — the floor of the model (ADR-151 §3).
_ALLOWLIST_ONLY = frozenset({"guest"})


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
    #: Sites beyond the node's home site that a holder of this role may mint a
    #: self-service key for (a guest site this node hosts). Empty: home only.
    sites: tuple[str, ...] = ()

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
        """Register ``bundle``; refuses to silently redefine a role.

        An allowlist-only role (``guest``) is refused a wildcard mount, per
        ADR-151 §3. The floor names what a deployment has PUBLISHED; a tier
        defined by subtraction is wrong the moment somebody mounts something
        new, and wrong silently, because the new surface is public until a
        person happens to notice. Refusing here rather than trusting the next
        author is the whole mechanism.
        """
        if bundle.role in _ALLOWLIST_ONLY:
            wild = [s for s in bundle.scopes if s.split(":", 1)[0].strip() == "*"]
            if wild:
                raise ValueError(
                    f"role bundle {bundle.role!r} may not hold a wildcard mount "
                    f"({', '.join(wild)}): the floor names the surfaces this "
                    f"deployment has published, one by one (ADR-151 §3)"
                )
        if not replace and bundle.role in self._bundles:
            raise ValueError(f"role bundle {bundle.role!r} is already registered")
        self._bundles[bundle.role] = bundle

    def get(self, role: str) -> ScopeBundle | None:
        return self._bundles.get(role)

    def roles(self) -> tuple[str, ...]:
        """Registered role names, sorted."""
        return tuple(sorted(self._bundles))

    def sites_for(self, roles: Iterable[str]) -> tuple[str, ...]:
        """The extra sites any of ``roles`` lists, in a stable order."""
        out: list[str] = []
        for role in roles:
            bundle = self._bundles.get(role)
            for site in bundle.sites if bundle else ():
                if site not in out:
                    out.append(site)
        return tuple(out)

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
#: The six roles of ADR-151, each with the cloud-provider pattern it copies.
#: The shape to hold on to: GOVERNING IS NOT READING. An administrator who
#: needs to read something asks for `operator`, and the ask is visible.

OWNER_BUNDLE = ScopeBundle(
    role="owner",
    scopes=("*",),
    description="everything, including appointing administrators",
)

ADMIN_BUNDLE = ScopeBundle(
    role="admin",
    scopes=("*:govern",),
    description="governs every mount and reads nothing",
)

OPERATOR_BUNDLE = ScopeBundle(
    role="operator",
    scopes=("*:read", "*:invoke", "*:govern"),
    description="runs the node day to day, data included",
)

CREATOR_BUNDLE = ScopeBundle(
    role="creator",
    scopes=("*:read", "*:invoke"),
    description="builds and writes, and grants nothing",
)
VIEWER_BUNDLE = ScopeBundle(
    role="viewer",
    scopes=("*:read",),
    description="read-only on every mount",
)

# ---- the three persona roles (ADR-222) -------------------------------------
#
# ADR-151's roles say what kind of act somebody may perform. The first hour of
# a new colleague needs a different cut: what they came to DO. Three names, each
# the one before plus one thing, so a role can never quietly lack a permission
# the role below it has:
#
#   reader       data, documents and chat
#   contributor  reader, plus kits and submissions
#   operator     contributor, plus governance (ADR-151's operator, unchanged)
#
# Mounts named here are the owning extensions of the composed app: ``gateway``
# is the served chat API (/v1/chat/completions), ``chat`` the agent loop,
# ``data_platform`` the data door (its query path is declared a read, its
# submit path stays invoke) and ``library`` document uploads. Every data and
# document read is already ``*:read``.

#: The mounts whose POSTs are a conversation, not a change: chat.
CHAT_SCOPES = ("gateway:invoke", "chat:invoke")
#: The mounts whose POSTs submit something for review: a data kit (``/data/submit``,
#: and its withdraw request) and a document upload to the library.
SUBMIT_SCOPES = ("data_platform:invoke", "library:invoke")

READER_BUNDLE = ScopeBundle(
    role="reader",
    scopes=("*:read", *CHAT_SCOPES),
    description="reads data and documents, and chats",
)

CONTRIBUTOR_BUNDLE = ScopeBundle(
    role="contributor",
    scopes=("*:read", *CHAT_SCOPES, *SUBMIT_SCOPES),
    description="a reader who also submits data kits and documents",
)

#: The persona ladder, lowest first. Each role holds every scope of the one before it
#: (``test_the_persona_roles_reach_what_they_should`` checks it on every build).
PERSONA_ROLES = ("reader", "contributor", "operator")

#: The floor, and deliberately empty here. A deployment names the surfaces it
#: has published; nothing reaches `guest` by default (ADR-151 §3). A bundle
#: that tried to say "everything except" is refused on registration, because a
#: tier defined by subtraction is wrong the moment somebody mounts something
#: new, and wrong silently.
GUEST_BUNDLE = ScopeBundle(
    role="guest",
    scopes=(),
    description="only the surfaces this deployment has published",
)


_default: BundleRegistry | None = None


def default_bundle_registry() -> BundleRegistry:
    """The process-local default registry: ADR-151's six roles and ADR-222's personas."""
    global _default
    if _default is None:
        _default = BundleRegistry()
        _default.register(OWNER_BUNDLE)
        _default.register(ADMIN_BUNDLE)
        _default.register(OPERATOR_BUNDLE)
        _default.register(CREATOR_BUNDLE)
        _default.register(VIEWER_BUNDLE)
        _default.register(READER_BUNDLE)
        _default.register(CONTRIBUTOR_BUNDLE)
        _default.register(GUEST_BUNDLE)
        roles_file = os.environ.get(ROLES_FILE_ENV, "").strip()
        if roles_file:
            # A deployment's own roles, inside what the built-ins can grant
            # (load_overrides refuses anything wider). A file that is named and
            # missing raises: silence would leave every grant of that role
            # refused at mint time, far from the cause.
            _default = load_overrides(Path(roles_file).expanduser(), _default)
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

        [role.contributor]            # may also mint keys for a guest site
        scopes = ["*:read", "data_platform:invoke"]
        sites = ["guest-loop"]        # beyond this node's home site

    ``sites`` lists the sites, besides the node's home site, a holder of the role
    may mint a self-service key for. It widens no scope: a key for a guest site
    holds the same scopes, held to that site's rows.

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
        sites = table.get("sites", ())
        if isinstance(sites, str) or not all(isinstance(x, str) and x.strip() for x in sites):
            raise ValueError(f"{path}: [role.{name}] sites must be a list of site ids")
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
                sites=tuple(x.strip() for x in sites) or (base.sites if base else ()),
            ),
            replace=True,
        )
    return out


# ---------------------------------------------------------------- JIT role


def jit_default_role() -> str:
    """The role a just-in-time-created SSO account receives when the IdP
    sends no roles claim, and the role a self-signed-up account starts with:
    ``$AXIOM_GATE_JIT_ROLE``, defaulting to ``"reader"`` (ADR-222). An
    explicitly empty value opts out (no default role)."""
    return os.environ.get(JIT_ROLE_ENV, DEFAULT_JIT_ROLE).strip()


__all__ = [
    "ADMIN_BUNDLE",
    "CHAT_SCOPES",
    "CONTRIBUTOR_BUNDLE",
    "PERSONA_ROLES",
    "READER_BUNDLE",
    "SUBMIT_SCOPES",
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
