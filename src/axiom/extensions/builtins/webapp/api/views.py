# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/api/v1/views`` — what somebody had configured, kept for when they return.

A surface a person configures and loses is a surface they configure once and
then stop using. Choosing four channels across two feeds, setting a window and
turning on a comparison is real work.

The document is OPAQUE. What belongs in a chart's saved state is a question
about charts; a schema here would make the platform learn every surface's
shape and break whenever any of them changed. The consumer that wrote a
document is the one that reads it.

A view belongs to a PRINCIPAL, resolved from the request rather than taken
from the body. A surface that can name whose view it is saving can read
somebody else's, and "which account" is not a field a client gets to fill in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

# Imported at MODULE level on purpose. `from __future__ import annotations`
# makes every annotation a string, and FastAPI resolves them against the
# module's globals — a `Request` imported inside the registering function is
# invisible to it, and every route then treats `request` as a missing QUERY
# parameter. The failure is a 422 on a request that was perfectly well formed.

#: Named so a refusal can say what it is, rather than 401 on a page that has a
#: perfectly good session and simply is not signed in to THIS.
ANONYMOUS = ""


#: Where a principal's stable identity is found, in the order it is trusted.
#: `sub` first because that is the token's own subject claim and survives a
#: display-name change; an email is a fallback, not the key.
IDENTITY_FIELDS = ("sub", "user_id", "principal", "email", "name")


def _field(holder: Any, name: str) -> str:
    """One field, whether the session came back as an object or a mapping.

    `session_from_cookies` returns the decoded CLAIMS — a dict — while a
    middleware that resolved the user leaves an object on `request.state`.
    Reading only one shape silently produced "not signed in" for a request
    carrying a perfectly good session.
    """
    if holder is None:
        return ""
    if isinstance(holder, Mapping):
        return str(holder.get(name) or "")
    return str(getattr(holder, name, "") or "")


def principal_of(request: Any) -> str:
    """Whose views these are.

    From the session the platform already established. Falls back to an API
    principal, because a key-authenticated caller is somebody too, and then to
    nothing — which is refused rather than shared, since one anonymous bucket
    would hand every reader everyone else's views.
    """
    from axiom.webauth import session_from_cookies

    holders = [getattr(getattr(request, "state", None), "user", None)]
    try:
        holders.append(session_from_cookies(dict(request.cookies)))
    except Exception:  # noqa: BLE001 — an unreadable cookie is not a principal
        pass
    for holder in holders:
        for name in IDENTITY_FIELDS:
            found = _field(holder, name)
            if found:
                return found
    key = getattr(getattr(request, "state", None), "api_key", None)
    return _field(key, "key_id") or ANONYMOUS


class SaveBody(BaseModel):
    """What a PUT carries. Declared rather than a bare dict so the refusal for
    a malformed body comes from the framework, in its own words."""

    document: dict[str, Any] = Field(default_factory=dict)
    #: Absent means "leave pinning as it is". Re-saving a document is not a
    #: statement about whether the view is pinned.
    pinned: bool | None = None


def register_view_routes(router: APIRouter) -> None:
    """Attach the saved-view endpoints to the versioned router."""
    from axiom.extensions.builtins.webapp.views import (
        delete_view,
        list_views,
        read_view,
        write_view,
    )
    from axiom.extensions.builtins.webapp.views.models import LAST
    from axiom.extensions.builtins.webapp.views.store import TooManyViews, ViewTooLarge

    def _who(request: Request) -> str:
        principal = principal_of(request)
        if not principal:
            raise HTTPException(
                401,
                "a saved view belongs to somebody, and this request is not "
                "signed in. Sign in, or use an API key.",
            )
        return principal

    @router.get("/views/{surface}", tags=["views"])
    def views(request: Request, surface: str) -> dict:
        """Every view this principal holds of this surface."""
        return {"surface": surface, "views": list_views(_who(request), surface)}

    @router.get("/views/{surface}/{name}", tags=["views"])
    def view(request: Request, surface: str, name: str = LAST) -> dict:
        """One view's document, or an empty one.

        Never a 404: a surface opening for the first time has no saved view,
        and that is the normal case rather than a failure. It opens fresh.
        """
        document = read_view(_who(request), surface, name)
        return {"surface": surface, "name": name, "document": document or {}, "saved": document is not None}

    @router.put("/views/{surface}/{name}", tags=["views"])
    def save(
        request: Request,
        surface: str,
        name: str = LAST,
        body: SaveBody | None = None,
    ) -> dict:
        document = body.document if body else {}
        pinned = body.pinned if body else None
        try:
            write_view(
                _who(request),
                surface,
                document,
                name=name,
                pinned=pinned if isinstance(pinned, bool) else None,
            )
        except ViewTooLarge as exc:
            raise HTTPException(413, str(exc)) from None
        except TooManyViews as exc:
            raise HTTPException(409, str(exc)) from None
        return {"surface": surface, "name": name, "saved": True}

    @router.delete("/views/{surface}/{name}", tags=["views"])
    def forget(request: Request, surface: str, name: str) -> dict:
        return {"surface": surface, "name": name, "deleted": delete_view(_who(request), surface, name)}
