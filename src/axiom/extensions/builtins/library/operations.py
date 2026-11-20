# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What the browser does to files beyond filing them: copy, versions, delete forever, zip.

**Copy.** A copy is a new item, owned by the person who made it, whose bytes
are the original's bronze bytes. Bronze is content-addressed
(``<site>/_content/<sha[:2]>/<sha>``), so the copy's row points at the same
blob and the same sidecar and nothing is written twice; ``attributes.copied_from``
names the original. The copy then runs the pipeline from ``identified`` like a
re-extraction, so it has its own extractions, Book placement and retrieval
chunks (its own ``source_path``). A folder copy makes the folder (and its
subfolders) again under the destination and copies what is in them.

**Versions.** A new version lands its bytes in bronze exactly as an upload
does, then the item row moves to them and the pipeline runs again under a new
job. Extraction rows are keyed by job, so each version keeps its own results.
Restoring an older version makes a new version that points at the older one's
bytes; nothing is destroyed. Only the person who filed an item adds or
restores versions; anyone who can read it lists and downloads them.

**Delete forever.** Only from Deleted files, only by the person who filed it.
A file under an export-control hold (its ``ec`` verdict is ``held``) is not
hard-deleted by its owner: a reviewer releases it first. With the library's
labels on (MVP-22), the item's retention class is checked too: a purge before
the class's floor, of a lifetime item, or under a legal hold is refused, in the
floor's own words. :func:`_purge_refusal` is the one place every hold and floor
is checked. Every attempt, refused or done, is written to
the action ledger (:mod:`axiom.policy.action_ledger`, always on, HMAC-chained
when a key is configured) and as a ``purge`` library event. The bytes are
removed from bronze only when no other item or version points at them; a
purge record stays beside them either way.

**Zip.** A selection of files and folders as one archive, streamed: each file
is read in chunks into the archive and each chunk is handed to the response
as it is produced, so memory is bounded by the chunk size whatever the total.
The total is checked against a cap before anything is sent, so a refusal is a
clear message rather than a broken download.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_

from axiom.extensions.builtins.library import labels, limits
from axiom.extensions.builtins.library.db_models import (
    LibraryExtraction,
    LibraryItem,
    LibraryJob,
    LibraryMark,
    LibraryRoute,
    LibraryVersion,
)
from axiom.extensions.builtins.library.scope import (
    Caller,
    bronze_root,
    corpus_for,
    site_root,
)
from axiom.extensions.builtins.library.service import (
    SOURCE_NAME,
    Forbidden,
    NotFound,
    _event,
    _unindex,
    _visible,
    to_dict,
)
from axiom.extensions.builtins.library.store import session_scope

ZIP_CAP_ENV = "AXIOM_LIBRARY_MAX_ZIP_BYTES"
DEFAULT_ZIP_CAP = 2 * 1024 * 1024 * 1024
ZIP_MAX_ITEMS = 1000
ZIP_CHUNK = 1024 * 1024


class Held(PermissionError):
    """A hold forbids this; the message says who can lift it."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(bronze_root().resolve()))
    except ValueError:
        return str(path)


def _blob(rel: str) -> Path:
    root = bronze_root().resolve()
    path = (root / rel).resolve()
    if root not in path.parents:
        raise NotFound("no stored content")
    return path


# ------------------------------------------------------------------------ copy


def _copy_row(
    session: Any, caller: Caller, src: LibraryItem, folder: str
) -> tuple[LibraryItem, str]:
    from axiom.extensions.builtins.library import pipeline

    new_id = str(uuid.uuid4())
    vis = src.visibility
    title = src.title if folder != (src.folder or "") else f"{src.title} (copy)"
    copy = LibraryItem(
        id=new_id,
        site=caller.site,
        owner=caller.principal,
        visibility=vis,
        file_name=src.file_name,
        title=title[:255],
        mime_type=src.mime_type,
        size=src.size,
        sha256=src.sha256,
        scan=src.scan,
        taxonomy=src.taxonomy,
        document_type=src.document_type,
        category=src.category,
        label=src.label,
        confidence=src.confidence,
        classifier=src.classifier,
        attributes={
            **dict(src.attributes or {}),
            "copied_from": {"item_id": src.id, "sha256": src.sha256},
        },
        tags=list(src.tags or []),
        collection=src.collection or "",
        # The original's bronze bytes and sidecar: provenance points at them, nothing is duplicated.
        bronze_record=src.bronze_record,
        bronze_content=src.bronze_content,
        corpus=corpus_for(caller, vis),
        source_path=f"{SOURCE_NAME}/{caller.site}/{new_id}/{src.file_name}",
        index_status="pending",
        folder="",
        placement={"requested_folder": folder} if folder else {},
        pipeline_state="running",
        ec={},
        created_at=_now(),
        updated_at=_now(),
    )
    job = pipeline.new_job(
        session,
        caller,
        src.file_name,
        src.size,
        kind="copy",
        item_id=new_id,
        options={"copied_from": src.id},
    )
    copy.job_id = job.id
    session.add(copy)
    _event(session, copy, caller, "copy", copied_from=src.id, sha256=src.sha256, folder=folder)
    return copy, job.id


def copy_item(caller: Caller, item_id: str, folder: str | None = None) -> dict[str, Any]:
    """Copy a document you can read into ``folder`` (default: beside the original)."""
    from axiom.extensions.builtins.library import pipeline
    from axiom.extensions.builtins.library.book import service as book

    from axiom.extensions.builtins.library import access

    with session_scope() as session:
        src = _visible(session, caller, item_id, verb="library.copy")
        target = (folder if folder is not None else (src.folder or "")).strip().strip("/")
        if target and not book.folder_exists(session, caller.site, target):
            raise NotFound(f"no folder {target!r} in this Book")
        folders = access.user_folders(session, caller.site)
        if target in folders:
            access.require(session, caller, "library.folder.file_into", access.folder_object(folders[target], folders))
        copy, job_id = _copy_row(session, caller, src, target)
        session.commit()
        out = to_dict(copy)
    pipeline.submit(pipeline.process, out["id"], job_id)
    return out


def copy_folder(caller: Caller, path: str, to: str | None = None) -> dict[str, Any]:
    """Make the folder again under ``to`` (default: beside it) with copies of what you can read in it."""
    from axiom.extensions.builtins.library import pipeline
    from axiom.extensions.builtins.library.book import service as book

    from axiom.extensions.builtins.library import access

    src = (path or "").strip().strip("/")
    with session_scope() as session:
        tree = book.tree(session, caller.site, caller=caller)
        folders = {f["path"]: f for f in tree["folders"]}
        if src not in folders:
            raise NotFound(f"no folder {src!r} in this Book")
        dest = (to if to is not None else (folders[src].get("parent") or "")).strip().strip("/")
        if dest == book.USER_ROOT:
            dest = ""  # "My folders" is where a new folder goes by default
        if dest and not book.folder_exists(session, caller.site, dest):
            raise NotFound(f"no folder {dest!r} in this Book")
        if dest == src or dest.startswith(src + "/"):
            raise ValueError("a folder cannot be copied into itself")
        made = {
            src: book.create_folder(
                session, caller.site, caller.principal, folders[src]["label"], dest
            )["path"]
        }
        for p in sorted(
            (p for p in folders if p.startswith(src + "/")), key=lambda x: x.count("/")
        ):
            parent = folders[p].get("parent") or p.rsplit("/", 1)[0]
            made[p] = book.create_folder(
                session, caller.site, caller.principal, folders[p]["label"], made[parent]
            )["path"]
        session.commit()
        rows = (
            session.query(LibraryItem)
            .filter(
                LibraryItem.site == caller.site,
                LibraryItem.deleted_at.is_(None),
                or_(LibraryItem.folder == src, LibraryItem.folder.like(src + "/%")),
            )
            .all()
        )
        started: list[tuple[str, str]] = []
        seen = access.readable_ids(session, caller, rows)
        for row in rows:
            if row.id not in seen or row.folder not in made or not access.may_item(session, caller, "library.copy", row):
                continue
            copy, job_id = _copy_row(session, caller, row, made[row.folder])
            started.append((copy.id, job_id))
        session.commit()
    for item_id, job_id in started:
        pipeline.submit(pipeline.process, item_id, job_id)
    return {"path": made[src], "folders": len(made), "copied": len(started)}


# ------------------------------------------------------------------------ versions


def _version_json(v: LibraryVersion, current: int, extractions: int, base: str) -> dict[str, Any]:
    return {
        "id": v.id,
        "version_number": v.number,
        "file_name": v.file_name,
        "mime_type": v.mime_type,
        "file_size": v.size,
        "sha256": v.sha256,
        "created_at": _iso(v.created_at),
        "created_by": v.created_by,
        "source": v.source,
        "restored_from": v.restored_from,
        "job_id": v.job_id or None,
        "extractions": extractions,
        "current": v.number == current,
        "download_href": f"{base}/items/{v.item_id}/versions/{v.number}/content",
    }


def _ensure_first(session: Any, item: LibraryItem) -> list[LibraryVersion]:
    rows = (
        session.query(LibraryVersion)
        .filter(LibraryVersion.item_id == item.id)
        .order_by(LibraryVersion.number)
        .all()
    )
    if rows:
        return rows
    first = LibraryVersion(
        id=str(uuid.uuid4()),
        site=item.site,
        item_id=item.id,
        number=1,
        file_name=item.file_name,
        mime_type=item.mime_type,
        size=item.size,
        sha256=item.sha256,
        scan=item.scan,
        bronze_content=item.bronze_content,
        bronze_record=item.bronze_record,
        job_id=item.job_id or "",
        source="upload",
        created_by=item.owner,
        created_at=item.created_at or _now(),
    )
    session.add(first)
    session.flush()
    return [first]


def list_versions(
    caller: Caller, item_id: str, *, base: str = "/api/v1/library"
) -> list[dict[str, Any]]:
    """Every version, newest first; the first is the current one."""
    with session_scope() as session:
        item = _visible(session, caller, item_id, verb="library.versions")
        rows = (
            session.query(LibraryVersion)
            .filter(LibraryVersion.item_id == item.id)
            .order_by(LibraryVersion.number.desc())
            .all()
        )
        if not rows:
            rows = list(reversed(_ensure_first(session, item)))
            session.rollback()  # listing never writes; version 1 is materialized on the first change
        current = rows[0].number
        counts: dict[str, int] = {}
        jobs = [r.job_id for r in rows if r.job_id]
        if jobs:
            for e in (
                session.query(LibraryExtraction)
                .filter(LibraryExtraction.item_id == item.id, LibraryExtraction.job_id.in_(jobs))
                .all()
            ):
                counts[e.job_id] = counts.get(e.job_id, 0) + 1
        return [_version_json(r, current, counts.get(r.job_id, 0), base) for r in rows]


def _move_to(
    session: Any, caller: Caller, item: LibraryItem, v: LibraryVersion, kind: str, *, job_id: str = ""
) -> str:
    """Point the item at version ``v``'s bytes and start a pipeline run for them (or join ``job_id``'s)."""
    from axiom.extensions.builtins.library import pipeline

    _unindex(item)
    job = session.get(LibraryJob, job_id) if job_id else None
    if job is None:
        job = pipeline.new_job(
            session,
            caller,
            v.file_name,
            v.size,
            kind=kind,
            item_id=item.id,
            options={"version": v.number},
        )
    item.file_name, item.mime_type, item.size, item.sha256, item.scan = (
        v.file_name,
        v.mime_type,
        v.size,
        v.sha256,
        v.scan,
    )
    item.bronze_content, item.bronze_record = v.bronze_content, v.bronze_record
    item.job_id, item.pipeline_state, item.index_status, item.chunk_count = (
        job.id,
        "running",
        "pending",
        0,
    )
    item.updated_at = _now()
    v.job_id = job.id
    return job.id


def add_version(caller: Caller, item_id: str, file_name: str, content: bytes) -> dict[str, Any]:
    """Upload new bytes as the item's next version: guarded and landed in bronze like any upload."""
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem
    from axiom.extensions.builtins.library import pipeline

    name, kind, scanned = limits.validate(file_name, content)
    sha = hashlib.sha256(content).hexdigest()
    with session_scope() as session:
        item = _visible(session, caller, item_id, verb="library.version_add")
        rows = _ensure_first(session, item)
        incoming = site_root(caller.site) / "_incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        staged = incoming / f"{uuid.uuid4()}.part"
        staged.write_bytes(content)
        fetched = FetchedItem(
            source_name=SOURCE_NAME,
            item_id=item.id,
            display_name=name,
            content=b"",
            content_type=kind.mime,
            size=len(content),
            modified_at=_now(),
            etag=sha,
            source_path=item.source_path,
            extra={
                "site": item.site,
                "owner": item.owner,
                "visibility": item.visibility,
                "sha256": sha,
                "mime_type": kind.mime,
                "version": rows[-1].number + 1,
            },
        )
        blob, record = pipeline._land(caller.site, staged, sha, fetched, item.corpus)
        v = LibraryVersion(
            id=str(uuid.uuid4()),
            site=item.site,
            item_id=item.id,
            number=rows[-1].number + 1,
            file_name=name,
            mime_type=kind.mime,
            size=len(content),
            sha256=sha,
            scan=scanned,
            bronze_content=_rel(blob),
            bronze_record=_rel(record),
            source="version",
            created_by=caller.principal,
            created_at=_now(),
        )
        session.add(v)
        job_id = _move_to(session, caller, item, v, "version")
        _event(
            session,
            item,
            caller,
            "version",
            number=v.number,
            sha256=sha,
            size=len(content),
            job_id=job_id,
        )
        session.commit()
        number = v.number
    pipeline.submit(pipeline.process, item_id, job_id)
    return {"item_id": item_id, "version_number": number, "job_id": job_id}


def restore_version(caller: Caller, item_id: str, number: int) -> dict[str, Any]:
    """Make an older version current again, as a new version; nothing is destroyed."""
    from axiom.extensions.builtins.library import pipeline

    with session_scope() as session:
        item = _visible(session, caller, item_id, verb="library.version_restore")
        rows = _ensure_first(session, item)
        old = next((r for r in rows if r.number == int(number)), None)
        if old is None:
            raise NotFound(f"no version {number} of {item_id!r}")
        if old.number == rows[-1].number:
            raise ValueError("that version is already the current one")
        v = LibraryVersion(
            id=str(uuid.uuid4()),
            site=item.site,
            item_id=item.id,
            number=rows[-1].number + 1,
            file_name=old.file_name,
            mime_type=old.mime_type,
            size=old.size,
            sha256=old.sha256,
            scan=old.scan,
            bronze_content=old.bronze_content,
            bronze_record=old.bronze_record,
            source="restore",
            restored_from=old.number,
            created_by=caller.principal,
            created_at=_now(),
        )
        session.add(v)
        job_id = _move_to(session, caller, item, v, "restore")
        _event(
            session,
            item,
            caller,
            "version",
            number=v.number,
            restored_from=old.number,
            job_id=job_id,
        )
        session.commit()
        new_number = v.number
    pipeline.submit(pipeline.process, item_id, job_id)
    return {
        "item_id": item_id,
        "version_number": new_number,
        "restored_from": int(number),
        "job_id": job_id,
    }


def version_content(caller: Caller, item_id: str, number: int) -> tuple[bytes, dict[str, Any]]:
    """An older (or the current) version's original bytes, checksum-verified."""
    with session_scope() as session:
        item = _visible(session, caller, item_id, verb="library.download")
        rows = session.query(LibraryVersion).filter(LibraryVersion.item_id == item.id).all() or [
            LibraryVersion(
                number=1,
                file_name=item.file_name,
                mime_type=item.mime_type,
                sha256=item.sha256,
                bronze_content=item.bronze_content,
            )
        ]
        v = next((r for r in rows if r.number == int(number)), None)
        if v is None:
            raise NotFound(f"no version {number} of {item_id!r}")
        data = _blob(v.bronze_content).read_bytes()
        if hashlib.sha256(data).hexdigest() != v.sha256:
            raise NotFound(f"stored content for version {number} does not match its checksum")
        return data, {"file_name": v.file_name, "mime_type": v.mime_type}


# ------------------------------------------------------------------------ delete forever


def _ledger(
    caller: Caller, item_id: str, outcome: str, *, rule: str | None = None, **meta: Any
) -> None:
    from axiom.policy.action_ledger import ActionLedger, _default_state_dir

    try:
        ActionLedger(state_dir=_default_state_dir(None)).record_action(
            agent=caller.principal,
            op_class="library.purge",
            name="delete forever",
            candidate=item_id,
            guards=["owner", "deleted-first", "export-control-hold", "retention-floor", "legal-hold"],
            outcome=outcome,
            refusing_rule=rule,
            metadata={"site": caller.site, "surface": caller.surface, **meta},
        )
    except Exception:  # noqa: BLE001 - the library event below is the record of last resort
        import logging

        logging.getLogger(__name__).exception(
            "library: the action ledger did not take the purge record"
        )


def _purge_refusal(item: LibraryItem) -> tuple[str, str] | None:
    """Why this item may not be hard-deleted by its owner, as ``(rule, message)``, or None.

    The export-control hold is the platform's one hold on every library item.
    With the labels on (MVP-22) the retention class is checked as well: its
    floor, a lifetime class and a legal hold each refuse, naming the floor and
    the date it ends.
    """
    if (item.ec or {}).get("verdict") == "held":
        return (
            "export-control hold",
            "This file is under an export-control hold. It cannot be deleted forever until an "
            "export-control reviewer releases it.",
        )
    if not labels.enforced():
        return None
    from axiom.governance import retention as gr

    cls = labels.class_of(item)
    created = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=UTC)
    try:
        gr.check_deletion(cls, created, now=_now(), policy=gr.policy_for(item.site))
    except gr.RetentionRefused as exc:
        rule = "legal hold" if cls == gr.RetentionClass.LEGAL_HOLD else "retention floor"
        return rule, f"{item.title!r} cannot be deleted forever: {exc}"
    return None


def purge_item(caller: Caller, item_id: str) -> dict[str, Any]:
    """Delete forever: the row, its versions, marks, extractions and routes, and the bytes nobody else uses."""
    with session_scope() as session:
        try:
            item = _visible(session, caller, item_id, deleted=True, verb="library.purge")
        except Forbidden:
            # Delete forever is the Owner's alone; to anyone else it is not a thing they can do here.
            _ledger(caller, item_id, "refused", rule="not the owner")
            raise NotFound(f"no library document {item_id!r}") from None
        refusal = _purge_refusal(item)
        if refusal:
            rule, why = refusal
            _ledger(caller, item_id, "refused", rule=rule, file_name=item.file_name)
            _event(session, item, caller, "purge-refused", reason=why)
            session.commit()
            raise Held(why)
        versions = session.query(LibraryVersion).filter(LibraryVersion.item_id == item.id).all()
        paths = {item.bronze_content, *(v.bronze_content for v in versions)} - {""}
        meta = {"file_name": item.file_name, "sha256": item.sha256, "versions": len(versions) or 1}
        for model in (LibraryVersion, LibraryExtraction, LibraryRoute):
            session.query(model).filter(model.item_id == item.id).delete(synchronize_session=False)
        session.query(LibraryMark).filter(
            LibraryMark.kind == "item", LibraryMark.target == item.id
        ).delete(synchronize_session=False)
        _event(session, item, caller, "purge", **meta)
        site = item.site
        session.delete(item)
        session.commit()
        removed: list[str] = []
        for rel in paths:
            still = (
                session.query(LibraryItem).filter(LibraryItem.bronze_content == rel).count()
                + session.query(LibraryVersion).filter(LibraryVersion.bronze_content == rel).count()
            )
            if still:
                continue
            try:
                _blob(rel).unlink(missing_ok=True)
                removed.append(rel)
            except NotFound:
                continue
    shutil.rmtree(site_root(site) / SOURCE_NAME / "_derived" / item_id, ignore_errors=True)
    day = _now().date().isoformat()
    target = site_root(site) / SOURCE_NAME / "_deleted" / day
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{item_id}.purged.json").write_text(
        json.dumps(
            {
                "item_id": item_id,
                "site": site,
                "purged_by": caller.principal,
                "purged_at": _iso(_now()),
                "bytes_removed": len(removed),
                "bytes_kept_shared": len(paths) - len(removed),
                **meta,
            },
            indent=2,
            sort_keys=True,
        )
    )
    _ledger(caller, item_id, "proceeded", bytes_removed=len(removed), **meta)
    return {
        "id": item_id,
        "purged": True,
        "bytes_removed": len(removed),
        "bytes_kept_shared": len(paths) - len(removed),
    }


# ------------------------------------------------------------------------ zip


def zip_cap() -> int:
    raw = os.environ.get(ZIP_CAP_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_ZIP_CAP
    except ValueError:
        return DEFAULT_ZIP_CAP


class TooLarge(ValueError):
    """The selection is over the archive cap."""


def zip_plan(caller: Caller, item_ids: list[str], folders: list[str]) -> list[tuple[str, str, int]]:
    """``(name in the archive, bronze path, size)`` for every file you can read in the selection.

    Folders keep their structure (labels as directory names); a name that repeats
    gets " (2)". Raises :class:`TooLarge` over the cap, before anything is sent.
    """
    from axiom.extensions.builtins.library.book import service as book

    entries: list[tuple[str, str, int]] = []
    used: set[str] = set()

    def add(arcname: str, rel: str, size: int) -> None:
        stem, dot, ext = arcname.rpartition(".")
        name, n = arcname, 2
        while name in used:
            name = f"{stem} ({n}).{ext}" if dot and stem else f"{arcname} ({n})"
            n += 1
        used.add(name)
        entries.append((name, rel, size))

    with session_scope() as session:
        from axiom.extensions.builtins.library import access

        for iid in item_ids:
            item = _visible(session, caller, iid, verb="library.download")
            add(item.file_name, item.bronze_content, item.size)
        if folders:
            labels = {f["path"]: f["label"] for f in book.tree(session, caller.site, caller=caller)["folders"]}
            for path in folders:
                root = path.strip("/")
                if root not in labels:
                    raise NotFound(f"no folder {root!r} in this Book")
                parent = root.rsplit("/", 1)[0] if "/" in root else ""
                rows = (
                    session.query(LibraryItem)
                    .filter(
                        LibraryItem.site == caller.site,
                        LibraryItem.deleted_at.is_(None),
                        or_(LibraryItem.folder == root, LibraryItem.folder.like(root + "/%")),
                    )
                    .order_by(LibraryItem.folder, LibraryItem.file_name)
                    .all()
                )
                seen = access.readable_ids(session, caller, rows)
                for row in rows:
                    if row.id not in seen or not access.may_item(session, caller, "library.download", row):
                        continue
                    rel = row.folder[len(parent) + 1 :] if parent else row.folder
                    parts, acc = [], parent
                    for seg in rel.split("/"):
                        acc = f"{acc}/{seg}" if acc else seg
                        parts.append(labels.get(acc, seg))
                    add("/".join([*parts, row.file_name]), row.bronze_content, row.size)
    if len(entries) > ZIP_MAX_ITEMS:
        raise TooLarge(
            f"The selection has {len(entries)} files; one download holds at most {ZIP_MAX_ITEMS}. "
            "Select fewer files, or download folders one at a time."
        )
    total = sum(e[2] for e in entries)
    if total > zip_cap():
        raise TooLarge(
            f"The selection is {limits.format_bytes(total)}; one download is at most "
            f"{limits.format_bytes(zip_cap())}. Select fewer files, or download folders one at a time."
        )
    return entries


class _Spool:
    """A write-only sink the zip writer fills and the response drains: memory is one chunk."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.offset = 0

    def write(self, b: bytes) -> int:
        self.parts.append(bytes(b))
        self.offset += len(b)
        return len(b)

    def tell(self) -> int:
        return self.offset

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        out = b"".join(self.parts)
        self.parts.clear()
        return out


def zip_stream(entries: list[tuple[str, str, int]]) -> Iterator[bytes]:
    """Yield the archive in pieces as it is written."""
    spool = _Spool()
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for arcname, rel, _size in entries:
            src = _blob(rel)
            with src.open("rb") as f, zf.open(arcname, "w", force_zip64=True) as dest:
                while chunk := f.read(ZIP_CHUNK):
                    dest.write(chunk)
                    if spool.parts:
                        yield spool.drain()
            if spool.parts:
                yield spool.drain()
    if spool.parts:
        yield spool.drain()


__all__ = [
    "Held",
    "TooLarge",
    "add_version",
    "copy_folder",
    "copy_item",
    "list_versions",
    "purge_item",
    "restore_version",
    "version_content",
    "zip_cap",
    "zip_plan",
    "zip_stream",
]
