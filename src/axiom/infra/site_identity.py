# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which site a site id means.

A site id was whatever string a credential or a manifest happened to
carry. Nothing resolved it to an identity, so two spellings of one site
were two sites — permanently, silently, and only visible later as a
question that got a partial answer reading like a complete one.

It has happened twice in deployment, and both shapes are worth naming
because they are what a consumer will hit:

* A site renamed itself. The published tier held 246 GB under the old
  spelling and the studio served 7,307 rows under the new one, and
  ``GET /studio/api/<old-spelling>/channels`` returned 404 — so the surface
  a person asks could not name the dataset holding the nine-year record.
* A site id and the id of one rig at that site drifted apart, found while
  moving that site's channel map into its own repo.

Neither was a typo. Both are what happens when a name changes and nothing
in the platform can say the two names are the same thing.

**An alias is not a redirect.** It is a statement that a site was once
called something else, and the canonical id is what everything downstream
sees — so history under the old name and readings under the new one are
one dataset rather than two that happen to be about the same rig.

**Declaration is separate from resolution**, and deliberately opt-in.
Resolving aliases is pure gain and is always on. REFUSING an id nobody
declared is a policy a deployment turns on once it has declared its sites;
default-on would reject every site that predates the declaration, which is
all of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class UnknownSite(ValueError):
    """A site id that no declaration covers, where declarations are enforced."""


@dataclass
class SiteIdentities:
    """The sites a deployment knows, and what each has been called.

    Held as data rather than derived from whatever has posted, because a
    registry built from traffic cannot tell a new site from a misspelling
    of an old one — which is the whole failure.
    """

    #: canonical id -> the ids that have ever meant it, itself included.
    _aliases: dict[str, set[str]] = field(default_factory=dict)
    #: Refuse an id no declaration covers. Off until a deployment has
    #: declared its sites; see the module docstring.
    require_declaration: bool = False

    def declare(self, canonical: str, *, also_known_as: tuple[str, ...] = ()) -> None:
        """Record *canonical* as a site, optionally with its former names."""
        canonical = canonical.strip()
        if not canonical:
            raise ValueError("a site id cannot be blank")
        known = self._aliases.setdefault(canonical, {canonical})
        for name in also_known_as:
            name = name.strip()
            if not name:
                continue
            owner = self._owner(name)
            if owner is not None and owner != canonical:
                # Two canonical sites claiming one former name would make
                # the resolution order decide which dataset a reader gets,
                # which is exactly the ambiguity this exists to remove.
                raise ValueError(
                    f"{name!r} is already an alias of {owner!r}; one former "
                    f"name cannot mean two sites"
                )
            known.add(name)

    def _owner(self, name: str) -> str | None:
        for canonical, known in self._aliases.items():
            if name in known:
                return canonical
        return None

    def resolve(self, site: str) -> str:
        """The canonical id for *site*.

        An id nobody declared is returned unchanged unless declarations are
        enforced — a deployment that has declared nothing must keep
        working, and silently rewriting an id it has never heard of would
        be worse than passing it through.
        """
        site = (site or "").strip()
        if not site:
            return site
        owner = self._owner(site)
        if owner is not None:
            return owner
        if self.require_declaration:
            known = ", ".join(sorted(self._aliases)) or "none"
            raise UnknownSite(
                f"no site {site!r} is declared. Declared sites: {known}. "
                f"A site that changed name is declared with its former name "
                f"as an alias, so the two are one dataset rather than two."
            )
        return site

    def known(self) -> tuple[str, ...]:
        """Canonical ids, sorted."""
        return tuple(sorted(self._aliases))

    def aliases_of(self, canonical: str) -> tuple[str, ...]:
        """Every id that has meant *canonical*, itself last."""
        known = self._aliases.get(canonical, set())
        return tuple(sorted(known - {canonical})) + ((canonical,) if known else ())


_IDENTITIES = SiteIdentities()


def identities() -> SiteIdentities:
    """The process-wide registry."""
    return _IDENTITIES


def resolve(site: str) -> str:
    """Canonical id for *site*, through the process-wide registry."""
    return _IDENTITIES.resolve(site)


def declare(canonical: str, *, also_known_as: tuple[str, ...] = ()) -> None:
    """Declare a site process-wide."""
    _IDENTITIES.declare(canonical, also_known_as=also_known_as)


def load_from_env() -> None:
    """Declare sites from ``AXIOM_SITE_IDENTITIES``.

    ``canonical=old1,old2;other=was``. An environment variable rather than
    a file because the first consumers are a node's own processes, and the
    declaration has to reach a container that ships no config of its own.
    A file-backed source belongs beside the other site config when one
    exists; this is deliberately the smaller thing.
    """
    raw = os.environ.get("AXIOM_SITE_IDENTITIES", "").strip()
    if not raw:
        return
    for clause in raw.split(";"):
        clause = clause.strip()
        if not clause:
            continue
        canonical, _, olds = clause.partition("=")
        declare(
            canonical.strip(),
            also_known_as=tuple(o.strip() for o in olds.split(",") if o.strip()),
        )
    if os.environ.get("AXIOM_SITE_REQUIRE_DECLARATION", "").strip().lower() in (
        "1", "true", "yes",
    ):
        _IDENTITIES.require_declaration = True


__all__ = [
    "SiteIdentities",
    "UnknownSite",
    "declare",
    "identities",
    "load_from_env",
    "resolve",
]
