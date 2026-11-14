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


class UploadStart(BaseModel):
    """A resumable upload's declaration (ADR-213 D1)."""

    file_name: str = Field(max_length=1024)
    size: int = Field(gt=0)
    sha256: str = Field(default="", max_length=64)
    visibility: str | None = Field(default=None, max_length=16)
    title: str | None = Field(default=None, max_length=255)
    tags: list[str] | str | None = None
    collection: str | None = Field(default=None, max_length=128)
    folder: str | None = Field(default=None, max_length=255)
    #: Values extractors may read (``ctx.params``): a run's start time, a channel map name.
    extract_params: dict[str, str | int | float | bool | None] | None = None
    #: MVP-22: the export-control label the uploader confirms, and the retention class.
    ec_label: str | None = Field(default=None, max_length=16)
    retention_class: str | None = Field(default=None, max_length=16)
    sample: bool = False
    #: Same name, other bytes: ``ask`` (409 naming the file), ``replace`` (a new version) or ``keep_both``.
    on_conflict: str | None = Field(default=None, max_length=16)
    replace_item_id: str | None = Field(default=None, max_length=64)


class Reextract(BaseModel):
    extractors: list[str] | None = None
    stale_only: bool = False


class FolderNew(BaseModel):
    label: str = Field(max_length=128)
    parent: str | None = Field(default=None, max_length=255)


class FolderPatch(BaseModel):
    label: str | None = Field(default=None, max_length=128)
    parent: str | None = Field(default=None, max_length=255)


class Move(BaseModel):
    folder: str = Field(max_length=255)


class EcDecision(BaseModel):
    #: Required: one line saying why (the review trail keeps it).
    reason: str = Field(max_length=2000)


class CopyTo(BaseModel):
    """Where a copy goes: a Data Book folder path; absent, beside the original."""

    folder: str | None = Field(default=None, max_length=255)


class TidyPropose(BaseModel):
    """Which files Tidy reads: absent, all of the caller's own (up to ``tidy.MAX_CONSIDERED``)."""

    item_ids: list[str] | None = Field(default=None, max_length=500)


class TidyAccepted(BaseModel):
    item_id: str = Field(max_length=64)
    action: str = Field(max_length=32)
    to_folder: str | None = Field(default=None, max_length=255)


class TidyApply(BaseModel):
    """The proposals the person accepted; nothing else changes."""

    items: list[TidyAccepted] = Field(default_factory=list, max_length=500)


class ItemPatch(BaseModel):
    """What a PATCH may change. Site, owner and bytes are not among them."""

    title: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = None
    collection: str | None = Field(default=None, max_length=128)
    visibility: str | None = Field(default=None, max_length=16)


class FolderRestore(BaseModel):
    """Which folder delete to undo: the ``undo`` the delete answered with."""

    act: str = Field(max_length=36)


class LabelBody(BaseModel):
    """Confirm an item's export-control label, change its retention class, or both (MVP-22)."""

    label: str | None = Field(default=None, max_length=16)
    retention_class: str | None = Field(default=None, max_length=16)
    #: Required to move an item off a legal hold: a hold ends only when it is released.
    release_hold: bool = False


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
    except PermissionError as exc:
        # Unlabelled, above clearance, a sample item (MVP-22-A1, MVP-06-A1, MVP-25-A5).
        raise HTTPException(403, str(exc)) from exc
    except UploadRefused as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
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
        elif name in ("visibility", "title", "tags", "collection", "folder", "ec_label", "retention_class", "sample",
                      "on_conflict", "replace_item_id"):
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
        from axiom.extensions.builtins.library.conflicts import NameConflict
        from axiom.extensions.builtins.library.limits import UploadRefused
        from axiom.extensions.builtins.library.service import Forbidden

        for name, data in files:
            try:
                # Extraction and embedding block; they run off the event loop.
                result = await run_in_threadpool(
                    service.upload, caller, name, data,
                    visibility=fields.get("visibility"), title=fields.get("title") if len(files) == 1 else None,
                    tags=fields.get("tags"), collection=fields.get("collection"), folder=fields.get("folder"),
                    ec_label=fields.get("ec_label"), retention_class=fields.get("retention_class"),
                    sample=(fields.get("sample") or "").lower() in ("1", "true", "yes", "on"),
                    on_conflict=fields.get("on_conflict"),
                    replace_item_id=fields.get("replace_item_id") if len(files) == 1 else None,
                )
            except NameConflict as exc:
                refused.append({"file_name": name, "reason": str(exc), "status": 409, "conflict": exc.payload})
                continue
            except Forbidden as exc:
                refused.append({"file_name": name, "reason": str(exc), "status": 403})
                continue
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
        folder: str = "",
        below: bool = False,
        root: bool = False,
        cursor: str = "",
        sort_by: str = "",
        sort_dir: str = "desc",
    ) -> dict:
        """A page of documents; ``next_cursor`` fetches the next (absent on the last page)."""
        caller = _caller(request)
        page = _answer(lambda: service.list_page(
            caller, q=q, category=category, collection=collection, tag=tag,
            visibility=visibility, status=status, deleted=deleted, limit=limit, folder=folder, below=below,
            root=root, cursor=cursor, sort_by=sort_by, sort_dir=sort_dir,
        ))
        return {"items": page["items"], "next_cursor": page["next_cursor"], "site": caller.site}

    @router.get(p + "/facets", tags=["library"])
    def facets(request: Request) -> dict:
        return service.facets(_caller(request))

    @router.get(p + SEARCH_PATH, tags=["library"])
    def search(request: Request, q: str = "", limit: int = 20) -> dict:
        """Documents whose contents answer ``q`` (retrieval-backed), in the caller's scope."""
        caller = _caller(request)
        return {"q": q, "results": _answer(lambda: service.search(caller, q, limit=limit))}

    # --- the browser's common views (Starred, Recent, Shared with me, As uploaded) ---
    # Per person: a star or an open is the caller's own, at the caller's site.
    from axiom.extensions.builtins.library import views

    @router.get(p + "/starred", tags=["library"])
    def starred(request: Request) -> dict:
        """Your starred documents and Data Book folders."""
        return views.starred(_caller(request))

    @router.put(p + "/items/{item_id}/star", tags=["library"])
    def star_item(request: Request, item_id: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: views.set_item_star(caller, item_id, True))

    @router.delete(p + "/items/{item_id}/star", tags=["library"])
    def unstar_item(request: Request, item_id: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: views.set_item_star(caller, item_id, False))

    @router.put(p + "/stars/folders/{path:path}", tags=["library"])
    def star_folder(request: Request, path: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: views.set_folder_star(caller, path, True))

    @router.delete(p + "/stars/folders/{path:path}", tags=["library"])
    def unstar_folder(request: Request, path: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: views.set_folder_star(caller, path, False))

    @router.post(p + "/items/{item_id}/opened", tags=["library"])
    def opened(request: Request, item_id: str) -> dict:
        """Note that you opened a document, for your Recent."""
        caller = _caller(request)
        return _answer(lambda: views.record_open(caller, item_id))

    @router.get(p + "/recent", tags=["library"])
    def recent(request: Request, limit: int = views.RECENT_MAX) -> dict:
        """What you opened or uploaded, most recent first."""
        return {"items": views.recent(_caller(request), limit=limit)}

    @router.get(p + "/shared", tags=["library"])
    def shared(request: Request, limit: int = 200) -> dict:
        """Shared with me: what colleagues filed for your site, until person-to-person grants exist."""
        return views.shared_with_me(_caller(request), limit=limit)

    @router.get(p + "/originals", tags=["library"])
    def originals(request: Request, page: int = 1, page_size: int = 50, job: str = "") -> dict:
        """As uploaded: every file exactly as it came in, read-only, download only."""
        return views.originals(_caller(request), page=page, page_size=page_size, job=job, base=p or views.DEFAULT_BASE)

    # --- copy, versions, delete forever, zip (operations.py) ---
    from axiom.extensions.builtins.library import operations as ops

    def _held(fn: Callable[[], Any]) -> Any:
        def guarded() -> Any:
            try:
                return fn()
            except ops.Held as exc:
                raise HTTPException(423, str(exc)) from exc
            except ops.TooLarge as exc:
                raise HTTPException(413, str(exc)) from exc

        return _answer(guarded)

    @router.post(p + "/tidy/proposals", tags=["library"])
    def tidy_proposals(request: Request, body: TidyPropose | None = None) -> dict:
        """Tidy up: what would move or go to Deleted files, and why. Changes nothing."""
        from axiom.extensions.builtins.library import tidy

        caller = _caller(request)
        return _answer(lambda: tidy.propose(caller, item_ids=body.item_ids if body else None))

    @router.post(p + "/tidy/apply", tags=["library"])
    def tidy_apply(request: Request, body: TidyApply) -> dict:
        """Apply the accepted proposals, each re-checked; what no longer holds is skipped with the reason."""
        from axiom.extensions.builtins.library import tidy

        caller = _caller(request)
        return _answer(lambda: tidy.apply(caller, [i.model_dump() for i in body.items]))

    @router.post(p + "/items/{item_id}/copy", tags=["library"], status_code=201)
    def copy_item(request: Request, item_id: str, body: CopyTo | None = None) -> dict:
        """A new item of yours over the original's bronze bytes (nothing is duplicated)."""
        caller = _caller(request)
        return _answer(lambda: ops.copy_item(caller, item_id, body.folder if body else None))

    @router.post(p + "/book/folders/{path:path}/copy", tags=["library"], status_code=201)
    def copy_folder(request: Request, path: str, body: CopyTo | None = None) -> dict:
        caller = _caller(request)
        return _answer(lambda: ops.copy_folder(caller, path, body.folder if body else None))

    @router.get(p + "/items/{item_id}/versions", tags=["library"])
    def versions(request: Request, item_id: str) -> dict:
        caller = _caller(request)
        return {"versions": _answer(lambda: ops.list_versions(caller, item_id, base=p or views.DEFAULT_BASE))}

    @router.post(p + "/items/{item_id}/versions", tags=["library"], status_code=201)
    async def version_new(request: Request, item_id: str) -> dict:
        """Upload new bytes as the next version (multipart, one file)."""
        from starlette.concurrency import run_in_threadpool

        caller = _caller(request)
        files, _fields = await _files(request)
        if len(files) != 1:
            raise HTTPException(422, "a new version is one file")
        name, data = files[0]
        return await run_in_threadpool(lambda: _answer(lambda: ops.add_version(caller, item_id, name, data)))

    @router.post(p + "/items/{item_id}/versions/{number}/restore", tags=["library"])
    def version_restore(request: Request, item_id: str, number: int) -> dict:
        caller = _caller(request)
        return _answer(lambda: ops.restore_version(caller, item_id, number))

    @router.get(p + "/items/{item_id}/versions/{number}/content", tags=["library"])
    def version_content(request: Request, item_id: str, number: int) -> Response:
        caller = _caller(request)
        data, meta = _answer(lambda: ops.version_content(caller, item_id, number))
        return Response(content=data, media_type=meta["mime_type"], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(meta['file_name'])}",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        })

    @router.post(p + "/items/{item_id}/purge", tags=["library"])
    def purge(request: Request, item_id: str) -> dict:
        """Delete forever, from Deleted files. 423 under an export-control hold. Every attempt is audited."""
        caller = _caller(request)
        return _held(lambda: ops.purge_item(caller, item_id))

    @router.post(p + "/zip", tags=["library"])
    async def zip_selection(request: Request, check: bool = False):
        """The selection as one streamed archive; ``check`` answers its size or the reason it is refused.

        Takes JSON ``{"items": [...], "folders": [...]}`` or a form with comma-separated
        ``items`` / ``folders`` (so a browser can stream the download straight to disk).
        """
        from fastapi.responses import StreamingResponse

        caller = _caller(request)
        if (request.headers.get("content-type") or "").startswith("application/json"):
            body = await request.json()
            item_ids, folders = list(body.get("items") or []), list(body.get("folders") or [])
        else:
            form = await request.form(max_fields=4)
            split = lambda k: [x for x in str(form.get(k) or "").split(",") if x]  # noqa: E731
            item_ids, folders = split("items"), split("folders")
        if not item_ids and not folders:
            raise HTTPException(422, "select at least one file or folder")
        entries = _held(lambda: ops.zip_plan(caller, item_ids, folders))
        if check:
            return {"files": len(entries), "bytes": sum(e[2] for e in entries), "cap": ops.zip_cap()}
        return StreamingResponse(ops.zip_stream(entries), media_type="application/zip", headers={
            "Content-Disposition": "attachment; filename=\"library-selection.zip\"",
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        })

    @router.get(p + "/items/{item_id}", tags=["library"])
    def item(request: Request, item_id: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: service.get_item(caller, item_id))

    @router.get(p + "/items/{item_id}/content", tags=["library"])
    def content(request: Request, item_id: str, download: bool = False) -> Response:
        """The stored bytes, as the type the library recorded (never the client's claim)."""
        caller = _caller(request)
        data, meta = _answer(lambda: service.read_content(caller, item_id, export=download))
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

    @router.post(p + "/items/{item_id}/label", tags=["library"])
    def label(request: Request, item_id: str, body: LabelBody) -> dict:
        """Confirm the export-control label and set the retention class (MVP-22)."""
        caller = _caller(request)
        return _answer(lambda: service.label_item(
            caller, item_id, label=body.label, retention_class=body.retention_class, release_hold=body.release_hold,
        ))


def register_pipeline_routes(router: APIRouter, *, subpath: str = "") -> None:
    """ADR-213: resumable uploads, jobs and their stream, an item's pipeline, previews, the Data Book."""
    from starlette.concurrency import run_in_threadpool

    from axiom.extensions.builtins.library import pipeline, service, uploads
    from axiom.extensions.builtins.library.conflicts import NameConflict

    p = subpath

    def up(fn: Callable[[], Any]) -> Any:
        try:
            return _answer(fn)
        except uploads.OffsetMismatch as exc:
            raise HTTPException(409, {"detail": str(exc), "offset": exc.offset}) from exc
        except uploads.ChunkChecksumMismatch as exc:
            raise HTTPException(460, str(exc)) from exc
        except uploads.SessionGone as exc:
            raise HTTPException(404, str(exc)) from exc
        except NameConflict as exc:
            raise HTTPException(409, exc.payload) from exc

    @router.post(p + "/uploads", tags=["library"], status_code=201)
    def upload_start(request: Request, body: UploadStart) -> dict:
        """Start a resumable upload. The name, type and size guards run now, before any byte is sent."""
        caller = _caller(request)
        return up(lambda: uploads.create(
            caller, file_name=body.file_name, size=body.size, sha256=body.sha256, visibility=body.visibility,
            title=body.title, tags=body.tags, collection=body.collection, folder=body.folder,
            extract_params=body.extract_params, ec_label=body.ec_label, retention_class=body.retention_class,
            sample=body.sample, on_conflict=body.on_conflict, replace_item_id=body.replace_item_id,
        ))

    @router.get(p + "/uploads/{upload_id}", tags=["library"])
    def upload_status(request: Request, upload_id: str) -> dict:
        caller = _caller(request)
        return up(lambda: uploads.status(caller, upload_id))

    @router.patch(p + "/uploads/{upload_id}", tags=["library"])
    async def upload_chunk(request: Request, upload_id: str) -> JSONResponse:
        """One chunk at ``Upload-Offset``. A wrong offset answers 409 with the server's offset."""
        caller = _caller(request)
        try:
            offset = int(request.headers.get("upload-offset", ""))
        except ValueError:
            raise HTTPException(422, "Upload-Offset is required (the byte offset this chunk starts at)") from None
        declared = int(request.headers.get("content-length") or 0)
        if declared > uploads.max_chunk_bytes():
            raise HTTPException(413, f"a chunk may carry at most {uploads.max_chunk_bytes()} bytes")
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > uploads.max_chunk_bytes():
                raise HTTPException(413, f"a chunk may carry at most {uploads.max_chunk_bytes()} bytes")
        checksum = (request.headers.get("upload-checksum") or "").strip()
        algo, _, value = checksum.partition(" ")
        if checksum and algo.lower() != "sha256":
            raise HTTPException(422, "Upload-Checksum must be 'sha256 <hex>'")
        result = await run_in_threadpool(lambda: up(lambda: uploads.append(caller, upload_id, offset, bytes(body), chunk_sha256=value)))
        return JSONResponse(result, headers={"Upload-Offset": str(result["offset"])})

    @router.delete(p + "/uploads/{upload_id}", tags=["library"])
    def upload_cancel(request: Request, upload_id: str) -> dict:
        caller = _caller(request)
        return up(lambda: uploads.cancel(caller, upload_id))

    def _ids(raw: str) -> list[str]:
        return [x for x in (raw or "").split(",") if x][:200]

    @router.get(p + "/jobs", tags=["library"])
    def jobs(request: Request, ids: str = "", active: bool = False, limit: int = 50) -> dict:
        """What happened to each of my files: stages, bytes, errors with remedies."""
        caller = _caller(request)
        return {"jobs": pipeline.list_jobs(caller, ids=_ids(ids), active=active, limit=limit)}

    @router.get(p + "/jobs/stream", tags=["library"])
    async def jobs_stream(request: Request, ids: str = "", active: bool = True):
        """Server-sent events: one ``job`` event per change, a comment every 15 s, until every job is terminal."""
        import asyncio
        import json as _json

        from starlette.responses import StreamingResponse

        caller = _caller(request)
        wanted = _ids(ids)

        async def events():
            seen: dict[str, str] = {}
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                found = await run_in_threadpool(
                    lambda: pipeline.list_jobs(caller, ids=wanted or None, active=False if wanted else active, limit=100)
                )
                for j in found:
                    stamp = f"{j['updated_at']}|{j['state']}|{j['bytes_received']}"
                    if seen.get(j["id"]) != stamp:
                        seen[j["id"]] = stamp
                        idle = 0.0
                        yield f"event: job\nid: {j['id']}\ndata: {_json.dumps(j)}\n\n"
                if wanted and found and all(j["state"] in pipeline.TERMINAL for j in found) and len(found) >= len(wanted):
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)
                idle += 0.5
                if idle >= 15:
                    idle = 0.0
                    yield ": still here\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.get(p + "/jobs/{job_id}", tags=["library"])
    def job(request: Request, job_id: str) -> dict:
        caller = _caller(request)
        found = pipeline.get_job(caller, job_id)
        if found is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return found

    @router.post(p + "/jobs/{job_id}/retry", tags=["library"])
    def job_retry(request: Request, job_id: str) -> dict:
        caller = _caller(request)
        return _answer(lambda: pipeline.retry(caller, job_id))

    @router.get(p + "/items/{item_id}/pipeline", tags=["library"])
    def item_pipeline(request: Request, item_id: str) -> dict:
        """Every stage, extraction, route and decision for one file."""
        caller = _caller(request)
        return _answer(lambda: service.item_pipeline(caller, item_id))

    @router.post(p + "/items/{item_id}/reextract", tags=["library"])
    def reextract(request: Request, item_id: str, body: Reextract | None = None) -> dict:
        caller = _caller(request)
        b = body or Reextract()
        return _answer(lambda: pipeline.start_reextract(caller, item_id, extractors=b.extractors, stale_only=b.stale_only))

    @router.get(p + "/items/{item_id}/preview", tags=["library"])
    def item_preview(request: Request, item_id: str) -> dict:
        """How to show this file: a representation the client draws with its own components."""
        caller = _caller(request)
        return _answer(lambda: service.preview(caller, item_id))

    @router.post(p + "/items/{item_id}/move", tags=["library"])
    def move(request: Request, item_id: str, body: Move) -> dict:
        caller = _caller(request)
        return _answer(lambda: service.move_item(caller, item_id, body.folder))

    # ---- sharing (ADR-263): the same skill functions every surface calls (SHR-R3.5)

    def _sharing_skill(fn, request: Request, item_id: str, body: dict) -> Any:
        from axiom.extensions.builtins.library.scope import CallerUnknown

        caller = _caller(request)

        class _Ctx:
            library_caller = caller
            principal = type("P", (), {"handle": caller.principal})()
            surface = "http"

        try:
            result = fn({**body, "id": item_id}, _Ctx())
        except CallerUnknown as exc:
            raise HTTPException(401, str(exc)) from exc
        if result.ok:
            return result.value
        detail = (result.errors or ["refused"])[0]
        status = 404 if "need access" in detail or detail.startswith("no ") else (
            422 if detail.startswith("missing") or "is one of" in detail else 403)
        raise HTTPException(status, detail)

    async def _json(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @router.get(p + "/items/{item_id}/access", tags=["library"])
    def item_access(request: Request, item_id: str) -> dict:
        """Who has access and why; what this caller may do."""
        from axiom.extensions.builtins.library.skills import verbs

        return _sharing_skill(verbs.access, request, item_id, {})

    @router.post(p + "/items/{item_id}/share", tags=["library"])
    async def item_share(request: Request, item_id: str) -> dict:
        """Share with a person or group, remove them, set general access or the two settings."""
        from axiom.extensions.builtins.library.skills import verbs

        body = await _json(request)
        return await run_in_threadpool(lambda: _sharing_skill(verbs.share, request, item_id, body))

    @router.post(p + "/items/{item_id}/transfer", tags=["library"])
    async def item_transfer(request: Request, item_id: str) -> dict:
        from axiom.extensions.builtins.library.skills import verbs

        body = await _json(request)
        return await run_in_threadpool(lambda: _sharing_skill(verbs.transfer, request, item_id, body))

    @router.post(p + "/items/{item_id}/requests", tags=["library"])
    async def item_requests(request: Request, item_id: str) -> Any:
        """Ask for access (the default), or status, withdraw, remind, list, approve, decline."""
        from axiom.extensions.builtins.library.skills import verbs

        body = await _json(request)
        return await run_in_threadpool(lambda: _sharing_skill(verbs.request, request, item_id, body))

    # ---- export-control review (ADR-213 D9): release, keep held, re-hold

    @router.get(p + "/items/{item_id}/ec", tags=["library"])
    def ec_state(request: Request, item_id: str) -> dict:
        """Why a file is held, its review trail, and what this caller may decide."""
        from axiom.extensions.builtins.library import ec_review

        caller = _caller(request)
        return _answer(lambda: ec_review.state(caller, item_id))

    @router.post(p + "/items/{item_id}/ec/release", tags=["library"])
    def ec_release(request: Request, item_id: str, body: EcDecision) -> dict:
        """An ec-reviewer (never the uploader) releases this file version, with a reason."""
        from axiom.extensions.builtins.library import ec_review

        caller = _caller(request)
        return _answer(lambda: ec_review.release(caller, item_id, body.reason))

    @router.post(p + "/items/{item_id}/ec/keep-held", tags=["library"])
    def ec_keep_held(request: Request, item_id: str, body: EcDecision) -> dict:
        from axiom.extensions.builtins.library import ec_review

        caller = _caller(request)
        return _answer(lambda: ec_review.keep_held(caller, item_id, body.reason))

    @router.post(p + "/items/{item_id}/ec/re-hold", tags=["library"])
    def ec_rehold(request: Request, item_id: str, body: EcDecision) -> dict:
        from axiom.extensions.builtins.library import ec_review

        caller = _caller(request)
        return _answer(lambda: ec_review.rehold(caller, item_id, body.reason))

    @router.get(p + "/extractors", tags=["library"])
    def extractors() -> dict:
        from axiom.extensions.builtins.library.extract.registry import describe_all

        return {"extractors": describe_all()}

    @router.get(p + "/previewers", tags=["library"])
    def previewers() -> dict:
        from axiom.extensions.builtins.library.preview.service import describe_all

        return {"previewers": describe_all()}

    @router.get(p + "/stale", tags=["library"])
    def stale(request: Request) -> dict:
        """Items extracted by an older extractor version than this node has."""
        caller = _caller(request)
        return {"items": pipeline.stale_items(caller)}

    @router.get(p + "/book", tags=["library"])
    def book_tree(request: Request) -> dict:
        """The Data Book as this person sees it: every category, their folders and those shared with them."""
        from axiom.extensions.builtins.library.book import service as book
        from axiom.extensions.builtins.library.store import session_scope

        caller = _caller(request)
        with session_scope() as session:
            return book.tree(session, caller.site, caller=caller)

    @router.post(p + "/book/folders", tags=["library"], status_code=201)
    def folder_new(request: Request, body: FolderNew) -> dict:
        from axiom.extensions.builtins.library import folders

        caller = _caller(request)
        return _answer(lambda: folders.create(caller, body.label, body.parent or ""))

    @router.post(p + "/book/folders/restore", tags=["library"])
    def folder_restore(request: Request, body: FolderRestore) -> dict:
        """Undo a folder delete: the folders and every item it took come back (SHR-R4.5)."""
        from axiom.extensions.builtins.library import folders

        caller = _caller(request)
        return _answer(lambda: folders.restore(caller, body.act))

    @router.patch(p + "/book/folders/{path:path}", tags=["library"])
    def folder_change(request: Request, path: str, body: FolderPatch) -> dict:
        """Rename or move: Editor of a person's folder; a Book manager for a Data Book category."""
        from axiom.extensions.builtins.library import folders

        caller = _caller(request)
        return _answer(lambda: folders.change(caller, path, label=body.label, parent=body.parent))

    @router.delete(p + "/book/folders/{path:path}", tags=["library"])
    def folder_delete(request: Request, path: str) -> dict:
        """The folder and what it holds to Deleted files, one act with one undo; each item checked."""
        from axiom.extensions.builtins.library import folders

        caller = _caller(request)
        return _answer(lambda: folders.delete(caller, path))


def build_router() -> APIRouter:
    """The library router, every route carrying its full ``/api/v1/library`` path.

    One flat router: the composed app builds its route table from
    ``router.routes`` and cannot see a nested ``include_router``.
    """
    from axiom.extensions.builtins.library.forward import API_BASE

    router = APIRouter(route_class=ForwardWhenBound)
    # Pipeline routes first: ``/items/{id}/pipeline`` must not be read as an item id.
    register_pipeline_routes(router, subpath=API_BASE)
    register_routes(router, subpath=API_BASE)
    return router


__all__ = ["CLASSIFY_PATH", "ForwardWhenBound", "SEARCH_PATH", "build_router", "register_pipeline_routes", "register_routes"]
