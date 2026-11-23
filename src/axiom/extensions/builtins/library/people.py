# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""People at the caller's site, found by name or email, for the share dialog (decided 2026-11-16).

A person signs in through the gate and is known by the gate's handle
(``@<name>:<context>``), which nobody types and whose context is not always
their site. So the dialog's field takes a name or an email, as Google's does:
:func:`search` finds people at the caller's own site (Team's rows, never
another site's), and :func:`resolve` turns an email someone typed into that
person's handle. The access check at use is unchanged.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.library.scope import Caller

MIN_QUERY = 2
MAX_RESULTS = 10


class _SiteOnly:
    """A site scope that permits the caller's own site and nothing else."""

    unbounded = False

    def __init__(self, site: str) -> None:
        self.site = site

    def permits(self, site: str) -> bool:
        return (site or "") == self.site


def _people(site: str) -> list[dict[str, Any]]:
    from axiom.extensions.builtins.actors.directory import _people as team_people

    skipped: dict[str, str] = {}
    return [p for p in team_people(_SiteOnly(site), skipped)
            if p.get("status") == "active" and (p.get("site") or "") == site]


def search(caller: Caller, q: str, *, limit: int = MAX_RESULTS) -> list[dict[str, str]]:
    """People at the caller's site whose name, email or handle holds ``q``."""
    needle = " ".join((q or "").split()).lower()
    if len(needle) < MIN_QUERY:
        raise ValueError(f"type at least {MIN_QUERY} letters of a name or an email")
    out = []
    for p in _people(caller.site):
        hay = " ".join(str(p.get(k) or "") for k in ("name", "email", "handle")).lower()
        if needle in hay:
            out.append({"handle": p["handle"], "name": p.get("name") or "", "email": p.get("email") or ""})
    out.sort(key=lambda p: (p["name"].lower(), p["email"]))
    return out[: max(1, min(int(limit), 50))]


def resolve(caller: Caller, who: str) -> str:
    """A handle or ``group:<id>`` as given; an email, the handle of that person at the caller's site."""
    text = " ".join(str(who or "").split())
    if not text or text.startswith("@") or text.lower().startswith("group:") or "@" not in text:
        return text
    email = text.lower()
    found = next((p for p in _people(caller.site) if (p.get("email") or "").lower() == email), None)
    if found is None:
        raise ValueError(f"Nobody at this site signs in as {text}; a site administrator invites people")
    from axiom.extensions.builtins.actors.directory import handle_holders

    if handle_holders(found["handle"]) > 1:
        # Two accounts answer to one handle: a grant to it would name neither person for sure.
        raise ValueError(f"{text} shares a handle with more than one account at this site; "
                         "a site administrator must give each person their own")
    return found["handle"]


__all__ = ["MIN_QUERY", "resolve", "search"]
