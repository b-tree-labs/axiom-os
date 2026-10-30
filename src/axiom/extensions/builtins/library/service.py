# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The library's one implementation: upload, browse, change, remove, search.

HTTP, the CLI, MCP and agent tools all call these functions with a
:class:`~.scope.Caller`; none of them has its own path to the bytes, the catalog
or the retrieval store.

An upload is the push-ingest path the data platform already runs, applied to one
file a person chose:

1. **Guards** (:mod:`.limits`): name, type, size, content signature, container
   bomb, optional scanner. Nothing is written before all of them pass.
2. **Bronze** — the same :class:`BronzeWriter` and :class:`FilesystemBronzeSink`
   the push ``IngestSink`` composes, rooted at the caller's site prefix. The
   bytes land content-addressed with a sidecar recording who sent them, from
   which site, and the decision.
3. **Catalog** — one ``library_items`` row and one ``library_events`` row,
   committed before indexing starts, so a document is never findable by search
   without being listed, and never lost because retrieval was down.
4. **Intake** (:func:`.intake.intake`) — classification and placement under the
   active taxonomy.
5. **Retrieval** — :func:`embed_bronze_record`, the adapter every connector pull
   uses: extract, chunk, the ingestion guard, embed, upsert into the corpus the
   caller's visibility selects (:func:`.scope.corpus_for`).

**Delete** removes the document from retrieval at once (its chunks are deleted
from its corpus) and from every listing; the catalog row becomes a tombstone and
bronze gains a ``_deleted`` sidecar. The bytes stay in bronze, because bronze is
the substrate of record and :func:`restore` re-indexes from it. Purging bytes is
a retention decision for an operator, not something a delete does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.library import limits
from axiom.extensions.builtins.library.db_models import LibraryEvent, LibraryItem
from axiom.extensions.builtins.library.scope import (
    DEFAULT_VISIBILITY,
    VISIBILITIES,
    Caller,
    bronze_root,
    can_change,
    can_read,
    corpus_for,
    readable_corpora,
    site_root,
)
from axiom.extensions.builtins.library.store import rag_store, session_scope

log = logging.getLogger(__name__)

SOURCE_NAME = "library"
MAX_TAGS = 32
MAX_TAG_CHARS = 64
MAX_COLLECTION_CHARS = 128
MAX_TITLE_CHARS = 255
MAX_LIST = 500


class NotFound(LookupError):
    """Nothing this caller may see by that id. Out of scope reads the same as absent."""


class Forbidden(PermissionError):
    """The caller may see it but not change it."""


@dataclass
class UploadResult:
    item: dict[str, Any]
    duplicate: bool = False
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def to_dict(item: LibraryItem) -> dict[str, Any]:
    """What a client sees. Storage paths stay on the server."""
    return {
        "id": item.id,
        "site": item.site,
        "owner": item.owner,
        "visibility": item.visibility,
        "file_name": item.file_name,
        "title": item.title,
        "mime_type": item.mime_type,
        "size": item.size,
        "sha256": item.sha256,
        "scan": item.scan,
        "taxonomy": item.taxonomy,
        "document_type": item.document_type,
        "category": item.category,
        "label": item.label,
        "confidence": item.confidence,
        "classifier": item.classifier,
        "attributes": dict(item.attributes or {}),
        "tags": list(item.tags or []),
        "collection": item.collection or "",
        "corpus": item.corpus,
        "index_status": item.index_status,
        "index_reason": item.index_reason or "",
        "chunk_count": item.chunk_count,
        "inline": _inline(item.file_name),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "deleted_at": _iso(item.deleted_at),
        "folder": item.folder or "",
        "folder_labels": list((item.placement or {}).get("labels") or []),
        "placement": {
            k: (item.placement or {}).get(k)
            for k in ("confidence", "source", "needs_look", "explanation")
            if (item.placement or {}).get(k) is not None
        },
        "pipeline_state": item.pipeline_state or "",
        "job_id": item.job_id or None,
        "ec": dict(item.ec or {}),
    }


def _inline(name: str) -> bool:
    try:
        return limits.file_type_for(name).inline
    except limits.UploadRefused:
        return False


def _event(session: Any, item: LibraryItem, caller: Caller, action: str, **detail: Any) -> None:
    session.add(
        LibraryEvent(
            item_id=item.id,
            site=item.site,
            principal=caller.principal,
            action=action,
            surface=caller.surface,
            detail=detail,
            at=_now(),
        )
    )


def _visible(session: Any, caller: Caller, item_id: str, *, deleted: bool = False) -> LibraryItem:
    item = session.get(LibraryItem, str(item_id))
    if item is None or not can_read(caller, item) or (item.deleted_at is not None) != deleted:
        raise NotFound(f"no library document {item_id!r}")
    return item


def _clean_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [t for t in tags.split(",")]
    out: list[str] = []
    for raw in tags:
        tag = " ".join(str(raw).split())[:MAX_TAG_CHARS]
        if tag and tag not in out:
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags per document")
    return out


def _clean_visibility(visibility: str | None) -> str:
    value = (visibility or DEFAULT_VISIBILITY).strip().lower()
    if value not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}")
    return value


def _rel(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return str(Path(path).resolve().relative_to(bronze_root().resolve()))
    except ValueError:
        return str(path)


def _content_path(item: LibraryItem) -> Path:
    root = bronze_root().resolve()
    path = (root / item.bronze_content).resolve()
    # The stored path is ours, but a catalog row is still not trusted to point
    # outside the library's own root.
    if root not in path.parents:
        raise NotFound(f"no stored content for {item.id!r}")
    return path


# ------------------------------------------------------------------------ bronze + RAG


def _writer(site: str, corpus: str):
    from axiom.extensions.builtins.data_platform.bronze import BronzeWriter, FilesystemBronzeSink
    from axiom.rag.ingest_router import Disposition

    # No path rules: a person's own upload is ALLOW by construction (they chose
    # it, the guards passed). The tier IS the corpus, which is how the embed
    # adapter knows where the chunks go.
    return BronzeWriter(
        rules=[],
        sink=FilesystemBronzeSink(root=site_root(site)),
        default_disposition=Disposition.ALLOW,
        default_tier=corpus,
    )


def _fetched(item: LibraryItem, content: bytes):
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem

    kind = limits.file_type_for(item.file_name)
    # Text-shaped files go to the extractor as text whatever their exact type,
    # so a CSV or a JSON document is indexed rather than refused as binary.
    content_type = "text/plain" if kind.sniff == "text" else kind.mime
    return FetchedItem(
        source_name=SOURCE_NAME,
        item_id=item.id,
        display_name=item.file_name,
        content=content,
        content_type=content_type,
        size=len(content),
        modified_at=_now(),
        etag=item.sha256,
        source_path=item.source_path,
        extra={
            "site": item.site,
            "owner": item.owner,
            "visibility": item.visibility,
            "sha256": item.sha256,
            "mime_type": item.mime_type,
            "document_type": item.document_type,
            "category": item.category,
        },
    )


def _index(item: LibraryItem, fetched: Any, result: Any) -> None:
    if not limits.file_type_for(item.file_name).indexable:
        item.index_status, item.index_reason, item.chunk_count = "unsupported", "not a text format", 0
        return
    store = rag_store()
    if store is None:
        item.index_status, item.index_reason = "pending", "no retrieval store on this install"
        return
    from axiom.extensions.builtins.data_platform.rag_embed import embed_bronze_record

    try:
        stats = embed_bronze_record(result, fetched, store)
    except Exception as exc:  # noqa: BLE001 - an index failure is a status, not a lost upload
        log.warning("library: indexing %s failed: %s", item.id, exc)
        item.index_status, item.index_reason = "pending", f"indexing failed: {type(exc).__name__}"
        return
    if stats.indexed:
        item.index_status, item.index_reason, item.chunk_count = "indexed", "", stats.chunks_created
    elif stats.skipped_reason == "embed_failed":
        item.index_status, item.index_reason = "pending", "the embedding service did not answer; retried later"
    else:
        item.index_status, item.index_reason = "unsupported", stats.skipped_reason or ""
        item.chunk_count = 0


def _reindex_from_bronze(item: LibraryItem) -> None:
    """Re-index after a visibility change or a restore: the pipeline's retrieval document when it has one."""
    from axiom.extensions.builtins.library.pipeline import derived_dir

    if (item.ec or {}).get("verdict") == "held":
        item.index_status, item.chunk_count = "held", 0
        return
    doc = derived_dir(item) / "rag.md"
    if doc.exists():
        _embed_document(item, doc)
        return
    _reindex_legacy(item)


def _embed_document(item: LibraryItem, doc: Path) -> None:
    from axiom.extensions.builtins.data_platform.bronze import BronzeWriteResult
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem
    from axiom.rag.ingest_router import Disposition

    text_sha = hashlib.sha256(doc.read_bytes()).hexdigest()
    fetched = FetchedItem(
        source_name=SOURCE_NAME, item_id=item.id, display_name=item.file_name + ".md", content=b"",
        content_type="text/markdown", size=doc.stat().st_size, modified_at=_now(), etag=text_sha,
        source_path=item.source_path, extra={"site": item.site, "owner": item.owner, "visibility": item.visibility},
    )
    result = BronzeWriteResult(
        item_id=item.id, disposition=Disposition.ALLOW, tier=item.corpus, content_hash=text_sha,
        record_path=bronze_root() / item.bronze_record, content_path=doc, reason="library re-index",
        matched_rule=None, fetched_at=_now(),
    )
    item.index_status = "pending"
    _index_text(item, fetched, result)


def _index_text(item: LibraryItem, fetched: Any, result: Any) -> None:
    store = rag_store()
    if store is None:
        item.index_status, item.index_reason = "pending", "no retrieval store on this install"
        return
    from axiom.extensions.builtins.data_platform.rag_embed import embed_bronze_record

    try:
        stats = embed_bronze_record(result, fetched, store)
    except Exception as exc:  # noqa: BLE001
        log.warning("library: indexing %s failed: %s", item.id, exc)
        item.index_status, item.index_reason = "pending", f"indexing failed: {type(exc).__name__}"
        return
    if stats.indexed:
        item.index_status, item.index_reason, item.chunk_count = "indexed", "", stats.chunks_created
    elif stats.skipped_reason == "embed_failed":
        item.index_status, item.index_reason = "pending", "the embedding service did not answer; retried later"
    else:
        item.index_status, item.index_reason, item.chunk_count = "unsupported", stats.skipped_reason or "", 0


def _reindex_legacy(item: LibraryItem) -> None:
    from axiom.extensions.builtins.data_platform.bronze import BronzeWriteResult
    from axiom.rag.ingest_router import Disposition

    content = _read_verified(item)
    fetched = _fetched(item, content)
    result = BronzeWriteResult(
        item_id=item.id,
        disposition=Disposition.ALLOW,
        tier=item.corpus,
        content_hash=item.sha256,
        record_path=bronze_root() / item.bronze_record,
        content_path=_content_path(item),
        reason="library re-index",
        matched_rule=None,
        fetched_at=_now(),
    )
    _index(item, fetched, result)


def _unindex(item: LibraryItem) -> None:
    store = rag_store()
    if store is None or not item.source_path:
        return
    try:
        store.delete_document(item.source_path, corpus=item.corpus)
    except Exception as exc:  # noqa: BLE001 - reported on the item, retried by restore/delete
        log.warning("library: removing %s from retrieval failed: %s", item.id, exc)
        raise


def _tombstone(item: LibraryItem, caller: Caller) -> None:
    day = _now().date().isoformat()
    target = site_root(item.site) / SOURCE_NAME / "_deleted" / day
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{item.id}.json").write_text(
        json.dumps(
            {
                "item_id": item.id,
                "site": item.site,
                "deleted_by": caller.principal,
                "deleted_at": _iso(item.deleted_at),
                "content_sha256": item.sha256,
                "retention": "bytes retained; removed from retrieval and listings",
            },
            indent=2,
            sort_keys=True,
        )
    )


def _read_verified(item: LibraryItem) -> bytes:
    data = _content_path(item).read_bytes()
    if hashlib.sha256(data).hexdigest() != item.sha256:
        raise NotFound(f"stored content for {item.id!r} does not match its checksum")
    return data


# ---------------------------------------------------------------------------- public


def limits_report() -> dict[str, Any]:
    from axiom.extensions.builtins.library.defaults import active_taxonomy

    t = active_taxonomy()
    return {**limits.effective_limits(), "taxonomy": t.name, "visibilities": list(VISIBILITIES)}


def classify(file_name: str, content: bytes) -> dict[str, Any]:
    """What an upload would be filed as, without writing anything."""
    from axiom.extensions.builtins.library.defaults import active_taxonomy, chain_for
    from axiom.extensions.builtins.library.intake import intake

    try:
        name, kind, _ = limits.validate(file_name, content)
    except limits.UploadRefused as exc:
        return {"file_name": file_name, "accepted": False, "reason": str(exc), "status": exc.status}
    taxonomy = active_taxonomy()
    placed = intake(name, chain=chain_for(taxonomy.name), taxonomy=taxonomy, mime_type=kind.mime, content=content)
    return {
        "file_name": name,
        "accepted": True,
        "mime_type": kind.mime,
        "size": len(content),
        "indexable": kind.indexable,
        "document_type": placed.document_type,
        "category": placed.category,
        "label": placed.label,
        "confidence": placed.confidence,
        "classifier": placed.classifier,
        "described": placed.describe(),
    }


def upload(
    caller: Caller,
    file_name: str,
    content: bytes,
    *,
    visibility: str | None = None,
    title: str | None = None,
    tags: Any = None,
    collection: str | None = None,
    folder: str | None = None,
    extract_params: dict[str, Any] | None = None,
) -> UploadResult:
    """File one document through the upload pipeline (ADR-213), synchronously.

    The bytes-in-hand path (multipart, the CLI, a skill): staged to disk, then
    the same six stages a resumable upload runs. A refusal at the guards raises
    :class:`limits.UploadRefused` as before; anything after ``stored`` is a
    status on the item, never an exception.
    """
    from axiom.extensions.builtins.library import pipeline
    from axiom.extensions.builtins.library.scope import site_root

    name, _kind, _ = limits.validate(file_name, content)
    _clean_visibility(visibility)
    _clean_tags(tags)
    incoming = site_root(caller.site) / "_incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    staged = incoming / f"{uuid.uuid4()}.part"
    staged.write_bytes(content)
    from axiom.extensions.builtins.library.uploads import _clean_params

    options = {"visibility": visibility, "title": title, "tags": tags, "collection": collection, "folder": folder,
               "extract_params": _clean_params(extract_params)}
    with session_scope() as session:
        job = pipeline.new_job(session, caller, name, len(content), options=options, bytes_received=len(content))
        session.commit()
        job_id = job.id
    stored = pipeline.run_upload(caller, staged, name, job_id, options)
    if stored is None:
        found = pipeline.get_job(caller, job_id) or {}
        err = found.get("error") or {}
        raise limits.UploadRefused(err.get("message") or "the upload was refused")
    return UploadResult(item={**stored.item, "job_id": job_id}, duplicate=stored.duplicate, warnings=stored.warnings)


def item_pipeline(caller: Caller, item_id: str) -> dict[str, Any]:
    """What happened to this file: its job, every extraction, every route, the EC verdict, the placement."""
    from axiom.extensions.builtins.library.db_models import (
        LibraryExtraction,
        LibraryJob,
        LibraryRoute,
    )
    from axiom.extensions.builtins.library.pipeline import job_json

    with session_scope() as session:
        item = _visible(session, caller, item_id)
        job = session.get(LibraryJob, item.job_id) if item.job_id else None
        extractions = (
            session.query(LibraryExtraction)
            .filter(LibraryExtraction.item_id == item.id)
            .order_by(LibraryExtraction.current.desc(), LibraryExtraction.id.desc())
            .limit(200)
            .all()
        )
        routes = (
            session.query(LibraryRoute)
            .filter(LibraryRoute.item_id == item.id, LibraryRoute.current.is_(True))
            .order_by(LibraryRoute.id)
            .all()
        )
        events = (
            session.query(LibraryEvent)
            .filter(LibraryEvent.item_id == item.id)
            .order_by(LibraryEvent.id.desc())
            .limit(50)
            .all()
        )
        rag_link = None
        if item.index_status == "indexed":
            rag_link = {"kind": "rag", "label": f"{item.chunk_count} chunks in {item.corpus}", "ref": item.source_path}
        return {
            "item": to_dict(item),
            "job": job_json(job) if job is not None else None,
            "extractions": [
                {
                    "extractor": e.extractor, "version": e.version, "scope": e.scope, "contributed_by": e.contributed_by,
                    "member": e.member, "status": e.status, "summary": e.summary, "result": e.result or {},
                    "tags": list(e.tags or []), "duration_ms": e.duration_ms, "error": e.error, "remedy": e.remedy,
                    "current": bool(e.current), "started_at": _iso(e.started_at), "output_sha256": e.output_sha256,
                }
                for e in extractions
            ],
            "routes": [
                {"tag": r.tag, "route": r.route, "extractor": r.extractor, "status": r.status, "target": r.target,
                 "detail": r.detail, "link": r.link, "remedy": r.remedy}
                for r in routes
            ]
            + ([{"tag": "rag", "route": "core.route.rag", "extractor": "", "status": "done", "target": item.corpus,
                 "detail": f"{item.chunk_count} chunk(s)", "link": rag_link, "remedy": ""}] if rag_link else [])
            + ([{"tag": "rag", "route": "core.route.rag", "extractor": "", "status": item.index_status,
                 "target": item.corpus, "detail": item.index_reason or "", "link": None,
                 "remedy": "An export-control reviewer releases held items." if item.index_status == "held" else ""}]
               if item.index_status in ("held", "pending") else []),
            "ec": dict(item.ec or {}),
            "placement": dict(item.placement or {}),
            "events": [
                {"action": ev.action, "principal": ev.principal, "surface": ev.surface, "at": _iso(ev.at), "detail": ev.detail}
                for ev in events
            ],
        }


def move_item(caller: Caller, item_id: str, folder: str) -> dict[str, Any]:
    """File an item where a person put it, and remember the move as a correction."""
    from axiom.extensions.builtins.library.book import service as book

    target = (folder or "").strip().strip("/")
    if not target:
        raise ValueError("name the folder to move it to")
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        if not book.folder_exists(session, item.site, target):
            raise NotFound(f"no folder {target!r} in this Book")
        before = item.folder
        placement = book.record_move(session, item, caller.principal, target)
        item.updated_at = _now()
        _event(session, item, caller, "move", from_folder=before, to_folder=target)
        session.commit()
        return {**to_dict(item), "placement": placement}


def preview(caller: Caller, item_id: str) -> dict[str, Any]:
    from axiom.extensions.builtins.library.db_models import LibraryExtraction
    from axiom.extensions.builtins.library.preview.service import preview_for

    with session_scope() as session:
        item = _visible(session, caller, item_id)
        path = _content_path(item)
        extractions = {
            e.extractor + (f"!{e.member}" if e.member else ""): (e.result or {})
            for e in session.query(LibraryExtraction)
            .filter(LibraryExtraction.item_id == item.id, LibraryExtraction.current.is_(True), LibraryExtraction.status == "done")
            .all()
        }
        meta = to_dict(item)
    return preview_for(path, meta, extractions)


def list_items(
    caller: Caller,
    *,
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
) -> list[dict[str, Any]]:
    with session_scope() as session:
        query = session.query(LibraryItem).filter(LibraryItem.site == caller.site)
        if folder:
            f = folder.strip("/")
            query = query.filter(
                (LibraryItem.folder == f) | (LibraryItem.folder.like(f + "/%")) if below else (LibraryItem.folder == f)
            )
        query = query.filter(LibraryItem.deleted_at.isnot(None) if deleted else LibraryItem.deleted_at.is_(None))
        if category:
            query = query.filter(LibraryItem.category == category)
        if collection:
            query = query.filter(LibraryItem.collection == collection)
        if visibility:
            query = query.filter(LibraryItem.visibility == _clean_visibility(visibility))
        if status:
            query = query.filter(LibraryItem.index_status == status)
        rows = query.order_by(LibraryItem.created_at.desc()).limit(MAX_LIST * 4).all()
    needle = q.strip().lower()
    out: list[dict[str, Any]] = []
    for item in rows:
        if not can_read(caller, item):
            continue
        if deleted and item.owner != caller.principal:
            continue
        if tag and tag not in (item.tags or []):
            continue
        if needle:
            hay = " ".join([item.title, item.file_name, item.label, item.collection or "", *(item.tags or [])]).lower()
            if needle not in hay:
                continue
        out.append(to_dict(item))
        if len(out) >= max(1, min(int(limit), MAX_LIST)):
            break
    return out


def get_item(caller: Caller, item_id: str) -> dict[str, Any]:
    with session_scope() as session:
        return to_dict(_visible(session, caller, item_id))


def read_content(caller: Caller, item_id: str) -> tuple[bytes, dict[str, Any]]:
    """The stored bytes, checksum-verified, with the item they belong to."""
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        return _read_verified(item), to_dict(item)


def update_item(
    caller: Caller,
    item_id: str,
    *,
    title: str | None = None,
    tags: Any = None,
    collection: str | None = None,
    visibility: str | None = None,
) -> dict[str, Any]:
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        if not can_change(caller, item):
            raise Forbidden("only the person who filed a document changes it")
        changed: dict[str, Any] = {}
        if title is not None:
            item.title = (" ".join(title.split()) or item.file_name)[:MAX_TITLE_CHARS]
            changed["title"] = item.title
        if tags is not None:
            item.tags = _clean_tags(tags)
            changed["tags"] = item.tags
        if collection is not None:
            item.collection = " ".join(collection.split())[:MAX_COLLECTION_CHARS]
            changed["collection"] = item.collection
        if visibility is not None:
            vis = _clean_visibility(visibility)
            if vis != item.visibility:
                # Moving visibility moves the chunks: out of the corpus the old
                # audience reads, into the one the new audience reads.
                _unindex(item)
                item.visibility = vis
                item.corpus = corpus_for(caller, vis)
                _reindex_from_bronze(item)
                changed["visibility"] = vis
        if changed:
            item.updated_at = _now()
            _event(session, item, caller, "update", **changed)
        session.commit()
        return to_dict(item)


def delete_item(caller: Caller, item_id: str) -> dict[str, Any]:
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        if not can_change(caller, item):
            raise Forbidden("only the person who filed a document removes it")
        _unindex(item)
        item.deleted_at = _now()
        item.deleted_by = caller.principal
        item.index_status, item.chunk_count = "pending", 0
        _tombstone(item, caller)
        _event(session, item, caller, "delete", retention="tombstone")
        session.commit()
        return to_dict(item)


def restore_item(caller: Caller, item_id: str) -> dict[str, Any]:
    with session_scope() as session:
        item = _visible(session, caller, item_id, deleted=True)
        if not can_change(caller, item):
            raise Forbidden("only the person who filed a document restores it")
        item.deleted_at, item.deleted_by = None, ""
        _reindex_from_bronze(item)
        item.updated_at = _now()
        _event(session, item, caller, "restore")
        session.commit()
        return to_dict(item)


def facets(caller: Caller) -> dict[str, Any]:
    """Collections, tags and categories in use, with counts, for the filters."""
    items = list_items(caller, limit=MAX_LIST)
    collections: dict[str, int] = {}
    tags: dict[str, int] = {}
    categories: dict[str, int] = {}
    for it in items:
        if it["collection"]:
            collections[it["collection"]] = collections.get(it["collection"], 0) + 1
        for t in it["tags"]:
            tags[t] = tags.get(t, 0) + 1
        categories[it["category"]] = categories.get(it["category"], 0) + 1
    from axiom.extensions.builtins.library.defaults import active_taxonomy

    t = active_taxonomy()
    return {
        "taxonomy": {
            "name": t.name,
            "category_order": list(t.category_order),
            "labels": {d.category: d.category.replace("_", " ").title() for d in t.types},
        },
        "collections": collections,
        "tags": tags,
        "categories": categories,
    }


def search(caller: Caller, q: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Documents whose CONTENT answers ``q``, from the corpora this caller reads.

    Retrieval ranks chunks; this maps them back to library documents and keeps
    only ones the caller may read and that are still filed. The store is asked
    for more chunks than results because the site corpus also holds documents
    that did not come through the library.
    """
    query = " ".join((q or "").split())
    if not query:
        return []
    store = rag_store()
    if store is None:
        return []
    embedding = None
    try:
        from axiom.rag.embeddings import embed_texts

        vectors = embed_texts([query])
        embedding = vectors[0] if vectors else None
    except Exception:  # noqa: BLE001 - keyword search still answers
        embedding = None
    want = max(1, min(int(limit), 50))
    hits = store.search(
        query_embedding=embedding, query_text=query, corpora=readable_corpora(caller), limit=min(200, want * 10)
    )
    prefix = f"{SOURCE_NAME}/{caller.site}/"
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for hit in hits:
        path = getattr(hit, "source_path", "") or ""
        if not path.startswith(prefix):
            continue
        item_id = path[len(prefix):].split("/", 1)[0]
        if item_id in by_id:
            continue
        by_id[item_id] = {"snippet": (getattr(hit, "chunk_text", "") or "")[:400], "score": getattr(hit, "combined_score", 0.0)}
        order.append(item_id)
    out: list[dict[str, Any]] = []
    with session_scope() as session:
        for item_id in order:
            item = session.get(LibraryItem, item_id)
            if item is None or item.deleted_at is not None or not can_read(caller, item):
                continue
            out.append({**to_dict(item), **by_id[item_id]})
            if len(out) >= want:
                break
    return out


__all__ = [
    "Forbidden",
    "item_pipeline",
    "move_item",
    "preview",
    "NotFound",
    "UploadResult",
    "classify",
    "delete_item",
    "facets",
    "get_item",
    "limits_report",
    "list_items",
    "read_content",
    "restore_item",
    "search",
    "to_dict",
    "update_item",
    "upload",
]
