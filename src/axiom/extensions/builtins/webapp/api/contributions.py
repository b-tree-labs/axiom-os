# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How a verb adds itself to ``/api/v1`` without ``webapp`` knowing it exists.

A unified experience means the client talks to one surface: one base URL, one
auth posture, one versioning story. Axiom's mount registry cannot express that
on its own — it refuses conflicting prefixes, so verbs cannot each mount
``/api/v1/<verb>`` beside the one webapp already holds.

The alternative that does not work is webapp importing every verb. That inverts
the dependency the wrong way: the platform's API shell would depend on chat, rag
and every domain pack, and installing one verb would drag in the rest.

So a verb *declares* its surface in its own manifest and webapp discovers the
declaration:

    [[extension.provides]]
    kind = "api"
    subpath = "/chat"
    entry = "axiom.extensions.builtins.chat.api:register_routes"

webapp resolves the entry lazily at compose time and never imports a verb at
module load. A verb that is not installed simply is not discovered; a verb that
is installed needs no change to webapp at all.

Three rules, each because the alternative fails quietly:

*Subpaths must not collide.* Two verbs claiming ``/search`` is a conflict, and
mirrors what the mount registry already does for prefixes. Silently letting the
second win would make the live API depend on import order.

*A broken contribution costs its own verb, not the API.* One verb whose import
raises must not take the whole surface down with it — but it must say so at
ERROR, because a route that silently fails to register is a 404 that looks like
a client bug and gets debugged in the wrong place.

*Contributions are ordered deterministically.* Discovery order varies with the
filesystem; sorting by subpath means the composed app is the same on every
machine and a route list is diffable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


class ApiSubpathConflictError(ValueError):
    """Two extensions claimed the same ``/api/v1`` subpath."""


@dataclass(frozen=True)
class ApiContribution:
    """One verb's declared slice of the versioned API."""

    subpath: str
    entry: str
    extension: str

    def __post_init__(self) -> None:
        if not self.subpath.startswith("/"):
            raise ValueError(
                f"api subpath {self.subpath!r} from {self.extension!r} must "
                "start with '/' — it is joined onto /api/v1"
            )
        if self.subpath.rstrip("/") == "":
            raise ValueError(
                f"{self.extension!r} declared the whole of /api/v1 as its "
                "subpath; a verb takes a slice, not the surface"
            )
        if ":" not in self.entry:
            raise ValueError(
                f"api entry {self.entry!r} from {self.extension!r} must be "
                "'module:function'"
            )


def collect_contributions(extensions) -> list[ApiContribution]:
    """Read ``kind = "api"`` declarations from discovered extensions.

    Raises :class:`ApiSubpathConflictError` if two extensions claim the same
    subpath — the composed API must not depend on which was discovered first.
    """
    found: dict[str, ApiContribution] = {}
    for ext in extensions:
        if not getattr(ext, "enabled", True):
            continue
        for declared in getattr(ext, "api_surfaces", ()):
            # The manifest says what and where; the extension name says who.
            # Carrying the declarer is what lets a conflict name both sides and
            # a failure name the verb that owns it.
            contribution = ApiContribution(
                subpath=declared.subpath,
                entry=declared.entry,
                extension=getattr(ext, "name", "unknown"),
            )
            key = contribution.subpath.rstrip("/")
            existing = found.get(key)
            if existing is not None:
                raise ApiSubpathConflictError(
                    f"/api/v1{key} is claimed by both {existing.extension!r} "
                    f"and {contribution.extension!r}"
                )
            found[key] = contribution
    return [found[k] for k in sorted(found)]


def apply_contributions(router, contributions) -> list[str]:
    """Resolve each entry and let it register its routes. Returns what landed.

    A contribution that cannot be imported or run is logged at ERROR and
    skipped: its verb loses its routes, the rest of the API keeps serving.
    """
    from importlib import import_module

    landed: list[str] = []
    for contribution in contributions:
        module_path, _, function_name = contribution.entry.partition(":")
        try:
            module = import_module(module_path)
            register = getattr(module, function_name)
            register(router, subpath=contribution.subpath)
        except Exception:  # noqa: BLE001 - one verb must not sink the surface
            log.exception(
                "api contribution from %r failed to register; /api/v1%s will "
                "404. The rest of the API is unaffected.",
                contribution.extension, contribution.subpath,
            )
            continue
        landed.append(contribution.subpath)
    return landed


__all__ = [
    "ApiContribution",
    "ApiSubpathConflictError",
    "apply_contributions",
    "collect_contributions",
]
