# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Who is asking, which site their documents belong to, and where those land.

Three answers, all derived from the verified caller and never from the request
body:

**The site.** The site the credential is bound to (``caller_site``, set by the
serving middleware from the checked key or session), else the site the
principal's handle names (``@name:site``), else this node's home site
(``AXIOM_SITE``), else ``default`` (ADR-209: "generic" means the default site,
never "no site"). A request cannot name a different one, so a cross-site write
is unrepresentable rather than refused.

**The corpus.** Where the chunks go decides who retrieval can show them to,
so it follows visibility (ADR-209 D2: visibility sets the default; ``site`` is
the default here, ``private`` the option):

==============  ======================================  ===========================
visibility      caller on a home site (or local)        caller from a tenant site
==============  ======================================  ===========================
``site``        ``rag-org``                             ``rag-tenant:<site>``
``private``     ``rag-personal:<principal>``            ``rag-personal:<principal>``
==============  ======================================  ===========================

``rag-org`` and ``rag-tenant:<site>`` are exactly the corpora a served
conversation already reads for that caller (``chat.scope.shared_corpora_for``),
so a site upload is in the site's chat with no second wiring. A private upload
is in a corpus only its owner's surfaces name (:func:`readable_corpora`).

**The storage prefix.** ``<library bronze root>/<site>/``: one directory tree
per site, so a path built for one tenant cannot reach another's bytes, and
removing a tenant is removing a directory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VISIBILITIES = ("site", "private")
DEFAULT_VISIBILITY = "site"
DEFAULT_SITE = "default"

#: Personal corpora are named by this prefix plus a slug of the principal.
#: ``rag-personal`` is the name ADR-105 gives the per-person tier.
CORPUS_PERSONAL_PREFIX = "rag-personal:"

BRONZE_ROOT_ENV = "AXIOM_LIBRARY_BRONZE_ROOT"

_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class CallerUnknown(PermissionError):
    """No principal could be established for this request."""


@dataclass(frozen=True)
class Caller:
    """The verified caller, as the library needs it."""

    principal: str
    site: str
    #: The caller's site is not one of this node's home sites.
    tenant: bool = False
    surface: str = ""


def _principal_slug(principal: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", principal.lower().lstrip("@")).strip("-.")
    return slug[:120] or "unknown"


def personal_corpus(principal: str) -> str:
    return f"{CORPUS_PERSONAL_PREFIX}{_principal_slug(principal)}"


def canonical_site(site: str | None) -> str:
    """The canonical slug for ``site``, or a refusal for one that could forge a path."""
    raw = (site or "").strip().lower() or DEFAULT_SITE
    try:
        from axiom.infra.site_identity import resolve

        raw = resolve(raw).strip().lower() or raw
    except Exception:  # noqa: BLE001 - an unreadable registry leaves the id as given
        pass
    if not _SITE_RE.match(raw):
        raise CallerUnknown(f"{site!r} is not a usable site id")
    return raw


def home_sites() -> set[str]:
    names = [os.environ.get("AXIOM_SITE", ""), DEFAULT_SITE]
    names += os.environ.get("AXIOM_HOME_SITES", "").split(",")
    out = set()
    for n in names:
        if n and n.strip():
            try:
                out.add(canonical_site(n))
            except CallerUnknown:
                continue
    return out


def _handle_site(principal: str) -> str | None:
    """The site a ``@name:site`` handle names. ``:local`` names this machine, not a site."""
    from axiom.webauth.users import principal_site

    site = principal_site(principal)
    return None if site in (None, "", "local") else site


def make_caller(principal: str, site: str | None = None, *, surface: str = "") -> Caller:
    if not (principal or "").strip():
        raise CallerUnknown("a library document belongs to somebody, and this caller is nobody")
    resolved = canonical_site(site or os.environ.get("AXIOM_SITE") or DEFAULT_SITE)
    return Caller(
        principal=principal.strip(), site=resolved, tenant=resolved not in home_sites(), surface=surface
    )


def caller_from_request(request: Any) -> Caller:
    """The caller behind an HTTP request, from what the serving stack verified.

    ``request.state.principal`` is set only from an authz decision that allowed
    (``http.middleware``); the session cookie and an API key are the fallbacks
    the saved-views surface uses. A loopback request with none of them is the
    machine's own operator, which is what a local web app with no gate is.
    """
    from axiom.infra.site_authority import caller_site

    state = getattr(request, "state", None)
    principal = getattr(state, "principal", None) if state is not None else None
    if principal is not None and not isinstance(principal, str):
        principal = getattr(principal, "handle", None) or str(principal)
    if not principal:
        from axiom.extensions.builtins.webapp.api.views import principal_of

        principal = principal_of(request) or None
    if not principal:
        host = getattr(getattr(request, "client", None), "host", "") or ""
        if host in ("127.0.0.1", "::1", "localhost"):
            from axiom.infra.principal import local_handle

            principal = local_handle()
    if not principal:
        raise CallerUnknown("sign in, or present an API key, to use the library")
    site = caller_site() or _handle_site(str(principal))
    return make_caller(str(principal), site, surface="http")


def caller_from_skill(ctx: Any, params: dict[str, Any]) -> Caller:
    """The caller behind a skill call: the context's principal, on this node's site."""
    principal = getattr(getattr(ctx, "principal", None), "handle", "") or ""
    if not principal:
        from axiom.infra.principal import local_handle

        principal = local_handle()
    return make_caller(principal, _handle_site(principal), surface=str(getattr(ctx, "surface", "") or "skill"))


def corpus_for(caller: Caller, visibility: str) -> str:
    if visibility not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {VISIBILITIES}, not {visibility!r}")
    if visibility == "private":
        return personal_corpus(caller.principal)
    if caller.tenant:
        from axiom.rag.store import tenant_corpus

        return tenant_corpus(caller.site)
    from axiom.rag.store import CORPUS_ORG

    return CORPUS_ORG


def readable_corpora(caller: Caller) -> list[str]:
    """What this caller's library search reads: their site's corpus and their own."""
    return [corpus_for(caller, "site"), corpus_for(caller, "private")]


def bronze_root() -> Path:
    raw = (os.environ.get(BRONZE_ROOT_ENV) or "").strip()
    if raw:
        return Path(raw)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "library" / "bronze"


def site_root(site: str) -> Path:
    """The site's own storage prefix; ``site`` is already canonical and path-safe."""
    root = bronze_root().resolve()
    target = (root / canonical_site(site)).resolve()
    if target.parent != root:
        raise CallerUnknown(f"{site!r} does not name a directory under the library root")
    return target


def can_read(caller: Caller, item: Any) -> bool:
    """ADR-209 D2 at the item: same site, and either shared there or the caller's own."""
    if item.site != caller.site:
        return False
    return item.visibility == "site" or item.owner == caller.principal


def can_change(caller: Caller, item: Any) -> bool:
    """Only the person who filed a document changes or removes it."""
    return item.site == caller.site and item.owner == caller.principal


__all__ = [
    "BRONZE_ROOT_ENV",
    "CORPUS_PERSONAL_PREFIX",
    "Caller",
    "CallerUnknown",
    "DEFAULT_SITE",
    "DEFAULT_VISIBILITY",
    "VISIBILITIES",
    "bronze_root",
    "can_change",
    "can_read",
    "caller_from_request",
    "caller_from_skill",
    "canonical_site",
    "corpus_for",
    "home_sites",
    "make_caller",
    "personal_corpus",
    "readable_corpora",
    "site_root",
]
