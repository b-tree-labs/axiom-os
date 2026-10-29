# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/api/v1/library`` — the library's HTTP face.

Served as its own mount nested in ``/api/v1`` (see :mod:`.mount`), so the
serving stack's authz hook runs on every request: a browser's session and a
bound laptop's gate key reach the same handlers and the same decision, and the
principal and site a handler sees are the ones that hook verified.

Every handler is a thin call into :mod:`.service`. On a bound install the route
class relays the request to the bound node before any handler runs, so nothing
here reads or writes this machine's library while it is bound (ADR-212).

Out-of-scope reads answer 404, never 403: whether a document exists in someone
else's site is not the caller's to learn.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

SEARCH_PATH = "/search"
CLASSIFY_PATH = "/classify"


class ForwardWhenBound(APIRoute):
    """Relay to the bound node when this install is bound; serve here otherwise."""

    def get_route_handler(self) -> Callable:
        local = super().get_route_handler()

        async def handler(request: Request) -> Response:
            from axiom.extensions.builtins.library.forward import bound_node, forward_http

            node = bound_node()
            if node is not None:
                return await forward_http(request, node)
            return await local(request)

        return handler


class ItemPatch(BaseModel):
    """What a PATCH may change. Site, owner and bytes are not among them."""

    title: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = None
    collection: str | None = Field(default=None, max_length=128)
    visibility: str | None = Field(default=None, max_length=16)


def _caller(request: Request):
    from axiom.extensions.builtins.library.scope import CallerUnknown, caller_from_request

    try:
        return caller_from_request(request)
    except CallerUnknown as exc:
        raise HTTPException(401, str(exc)) from exc


def _answer(fn: Callable[[], Any]) -> Any:
    from axiom.extensions.builtins.library.limits import UploadRefused
    from axiom.extensions.builtins.library.service import Forbidden, NotFound

    try:
        return fn()
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except UploadRefused as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _files(request: Request) -> tuple[list[tuple[str, bytes]], dict[str, str]]:
    """The uploaded files and plain fields, bounded before anything is kept."""
    from axiom.extensions.builtins.library import limits

    declared = int(request.headers.get("content-length") or 0)
    if declared > limits.max_batch_bytes() + 1024 * 1024:
        raise HTTPException(413, f"one upload may carry at most {limits.format_bytes(limits.max_batch_bytes())}")
    try:
        form = await request.form(max_files=limits.max_files(), max_fields=32)
    except Exception as exc:  # noqa: BLE001 - a malformed body is the client's to fix
        raise HTTPException(422, f"expected multipart form data with one or more files ({type(exc).__name__})") from exc
    files: list[tuple[str, bytes]] = []
    fields: dict[str, str] = {}
    cap = limits.max_file_bytes()
    for name, value in form.multi_items():
        if hasattr(value, "read") and hasattr(value, "filename"):
            data = await value.read(cap + 1)
            if len(data) > cap:
                raise HTTPException(
                    413, f"{value.filename!r} is larger than {limits.format_bytes(cap)}, the most this library takes"
                )
            files.append((value.filename or "", data))
        elif name in ("visibility", "title", "tags", "collection"):
            fields[name] = str(value)
    if not files:
        raise HTTPException(422, "no file in the upload")
    try:
        limits.check_batch([len(d) for _, d in files])
    except limits.UploadRefused as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return files, fields


def register_routes(router: APIRouter, *, subpath: str = "") -> None:
    """Attach the library endpoints. ``subpath`` is prefixed to every path."""
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.taxonomy import (
        TaxonomyError,
        get_taxonomy,
        registered_taxonomies,
    )

    p = subpath

    @router.get(p + "/taxonomies", tags=["library"])
    def taxonomies() -> dict:
        """Which document vocabularies this installation has."""
        from axiom.extensions.builtins.library.defaults import active_taxonomy

        return {"taxonomies": list(registered_taxonomies()), "active": active_taxonomy().name}

    @router.get(p + "/taxonomies/{name}", tags=["library"])
    def taxonomy(name: str) -> dict:
        """One vocabulary: its categories in order, and the types that file there."""
        try:
            found = get_taxonomy(name)
        except TaxonomyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "name": found.name,
            "category_order": list(found.category_order),
            "fallback_category": found.fallback_category,
            "types": [{"key": t.key, "label": t.label, "category": t.category} for t in found.types],
        }

    @router.get(p + "/limits", tags=["library"])
    def limits_() -> dict:
        """The caps the server enforces, so a client can refuse first in the same words."""
        return service.limits_report()

    @router.post(p + CLASSIFY_PATH, tags=["library"])
    async def classify(request: Request) -> dict:
        """What each file would be filed as. Writes nothing."""
        from starlette.concurrency import run_in_threadpool

        _caller(request)
        files, _ = await _files(request)
        return {"files": [await run_in_threadpool(service.classify, name, data) for name, data in files]}

    @router.post(p + "/items", tags=["library"], status_code=201)
    async def upload(request: Request) -> JSONResponse:
        """File one or more documents. Each is landed, catalogued and indexed, or refused with a reason."""
        from starlette.concurrency import run_in_threadpool

        caller = _caller(request)
        files, fields = await _files(request)
        filed: list[dict] = []
        refused: list[dict] = []
        from axiom.extensions.builtins.library.limits import UploadRefused

        for name, data in files:
            try:
                # Extraction and embedding block; they run off the event loop.
                result = await run_in_threadpool(
                    service.upload, caller, name, data,
                    visibility=fields.get("visibility"), title=fields.get("title") if len(files) == 1 else None,
                    tags=fields.get("tags"), collection=fields.get("collection"),
                )
            except UploadRefused as exc:
                refused.append({"file_name": name, "reason": str(exc), "status": exc.status})
                continue
            except ValueError as exc:
                refused.append({"file_name": name, "reason": str(exc), "status": 422})
                continue
            filed.append({**result.item, "duplicate": result.duplicate, "warnings": result.warnings})
        status = 201 if filed else max((r["status"] for r in refused), default=422)
        return JSONResponse({"items": filed, "refused": refused}, status_code=status)

    @router.get(p + "/items", tags=["library"])
    def items(
        request: Request,
        q: str = "",
        category: str = "",
        collection: str = "",
        tag: str = "",
        visibility: str = "",
        status: str = "",
        deleted: bool = False,
        limit: int = 200,
    ) -> dict:
        caller = _caller(request)
        found = _answer(lambda: service.list_items(
            caller, q=q, category=category, collection=collection, tag=tag,
            visibility=visibility, status=status, deleted=deleted, limit=limit,
        ))
        return {"items": found, "site": caller.site}

    @router.get(p + "/facets", tags=["library"])
    def facets(request: Request) -> dict:
        return service.facets(_caller(request))

    @router.get(p + SEARCH_PATH, tags=["library"])
    def search(request: Request, q: str = "", limit: int = 20) -> dict:
        """Documents whose contents answer ``q`` (retrieval-backed), in the caller's scope."""
        caller = _caller(request)
        return {"q": q, "results": _answer(lambda: service.search(caller, q, limit=limit))}

    @router.get(p + "/items/{item_id}", tags=["library"])
    def item(request: Request, item_id: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: service.get_item(caller, item_id))

    @router.get(p + "/items/{item_id}/content", tags=["library"])
    def content(request: Request, item_id: str, download: bool = False) -> Response:
        """The stored bytes, as the type the library recorded (never the client's claim)."""
        caller = _caller(request)
        data, meta = _answer(lambda: service.read_content(caller, item_id))
        disposition = "inline" if (meta["inline"] and not download) else "attachment"
        headers = {
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(meta['file_name'])}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if meta["mime_type"] != "application/pdf":
            # Nothing the library accepts runs script, and this says so to the
            # browser too. PDFs are left out: the viewer will not render one
            # served into a sandbox.
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:"
        media = meta["mime_type"]
        if media.startswith("text/") or media in ("application/json", "application/xml"):
            media += "; charset=utf-8"
        return Response(content=data, media_type=media, headers=headers)

    @router.patch(p + "/items/{item_id}", tags=["library"])
    def patch(request: Request, item_id: str, body: ItemPatch) -> dict:
        caller = _caller(request)
        return _answer(lambda: service.update_item(
            caller, item_id, title=body.title, tags=body.tags, collection=body.collection, visibility=body.visibility,
        ))

    @router.delete(p + "/items/{item_id}", tags=["library"])
    def delete(request: Request, item_id: str) -> dict:
        """Remove from retrieval and listings now; the bytes stay in bronze behind a tombstone."""
        caller = _caller(request)
        return _answer(lambda: service.delete_item(caller, item_id))

    @router.post(p + "/items/{item_id}/restore", tags=["library"])
    def restore(request: Request, item_id: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: service.restore_item(caller, item_id))


def build_router() -> APIRouter:
    """The library router, every route carrying its full ``/api/v1/library`` path.

    One flat router: the composed app builds its route table from
    ``router.routes`` and cannot see a nested ``include_router``.
    """
    from axiom.extensions.builtins.library.forward import API_BASE

    router = APIRouter(route_class=ForwardWhenBound)
    register_routes(router, subpath=API_BASE)
    return router


__all__ = ["CLASSIFY_PATH", "ForwardWhenBound", "SEARCH_PATH", "build_router", "register_routes"]
