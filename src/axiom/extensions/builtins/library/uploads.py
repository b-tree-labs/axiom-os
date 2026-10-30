# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Resumable uploads: a session, chunks at offsets, a checksum, then the pipeline (ADR-213 D1).

The protocol is the smallest one that survives a dropped connection on a
multi-gigabyte file, in the shape tus made familiar:

1. ``create`` declares the name, size and (optionally) the whole file's
   SHA-256. The name, type and size guards run here, so a file the library will
   never take is refused before a byte is sent. The session's job exists from
   this moment, so the tray shows the file at once.
2. ``append`` writes one chunk at the offset the client says it is at. A wrong
   offset is answered with the server's offset (409), never guessed at, so a
   resumed client continues exactly where the bytes stopped. A chunk may carry
   its own SHA-256; a mismatch refuses that chunk only.
3. When the offset reaches the size, the session completes and the pipeline
   runs (in the background): the whole-file checksum is verified, the content
   guards run, and the file lands in bronze.

Staged bytes live under ``<site root>/_incoming/``, on the same filesystem as
bronze, so landing is a rename. Idle sessions expire and their bytes are
removed (``sweep``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.library import limits
from axiom.extensions.builtins.library.db_models import LibraryJob, LibraryUpload
from axiom.extensions.builtins.library.scope import Caller, bronze_root, site_root
from axiom.extensions.builtins.library.store import session_scope

log = logging.getLogger(__name__)

CHUNK_ENV = "AXIOM_LIBRARY_CHUNK_BYTES"
TTL_ENV = "AXIOM_LIBRARY_UPLOAD_TTL_H"
DEFAULT_CHUNK = 8 * 1024 * 1024
#: Free space kept back beyond the declared size before a session is accepted.
HEADROOM = 1.05


class OffsetMismatch(Exception):  # noqa: N818 - a protocol answer
    def __init__(self, offset: int) -> None:
        super().__init__(f"the upload is at offset {offset}")
        self.offset = offset


class ChunkChecksumMismatch(Exception):  # noqa: N818
    pass


class SessionGone(LookupError):
    pass


def chunk_size() -> int:
    try:
        v = int(os.environ.get(CHUNK_ENV) or DEFAULT_CHUNK)
    except ValueError:
        v = DEFAULT_CHUNK
    return max(256 * 1024, min(v, 64 * 1024 * 1024))


def max_chunk_bytes() -> int:
    return chunk_size() * 4


def _ttl() -> timedelta:
    try:
        return timedelta(hours=max(1, int(os.environ.get(TTL_ENV) or 48)))
    except ValueError:
        return timedelta(hours=48)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def _staged(row: LibraryUpload) -> Path:
    root = bronze_root().resolve()
    p = (root / row.staged_path).resolve()
    if root not in p.parents:
        raise SessionGone("the session's staging path is outside the library")
    return p


def session_json(row: LibraryUpload) -> dict[str, Any]:
    return {
        "upload_id": row.id,
        "job_id": row.job_id,
        "file_name": row.file_name,
        "size": row.size,
        "offset": row.offset,
        "state": row.state,
        "chunk_size": row.chunk_size,
        "max_chunk_bytes": max_chunk_bytes(),
        "expires_at": _aware(row.expires_at).isoformat() if row.expires_at else None,
    }


def _clean_params(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Upload-time values for extractors: a few short scalars, never a payload."""
    out: dict[str, Any] = {}
    for k, v in list((raw or {}).items())[:20]:
        key = str(k)[:64]
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[key] = v[:500] if isinstance(v, str) else v
    return out


def create(
    caller: Caller,
    *,
    file_name: str,
    size: int,
    sha256: str = "",
    visibility: str | None = None,
    title: str | None = None,
    tags: Any = None,
    collection: str | None = None,
    folder: str | None = None,
    extract_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from axiom.extensions.builtins.library.pipeline import new_job

    name, _kind = limits.check_declared(file_name, int(size))
    if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha256)):
        raise ValueError("sha256 must be 64 hex characters")
    incoming = site_root(caller.site) / "_incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    try:
        free = os.statvfs(incoming).f_bavail * os.statvfs(incoming).f_frsize
        if free < size * HEADROOM:
            raise limits.UploadRefused(
                f"the node has {limits.format_bytes(free)} free, not enough for {name!r} ({limits.format_bytes(size)})",
                status=507,
            )
    except (AttributeError, OSError):
        pass
    upload_id = str(uuid.uuid4())
    staged = incoming / f"{upload_id}.part"
    staged.touch()
    options = {"visibility": visibility, "title": title, "tags": tags, "collection": collection, "folder": folder,
               "extract_params": _clean_params(extract_params)}
    with session_scope() as session:
        job = new_job(session, caller, name, int(size), upload_id=upload_id, options=options)
        job.stages[0]["status"] = "running"
        job.state = "running"
        row = LibraryUpload(
            id=upload_id, site=caller.site, owner=caller.principal, job_id=job.id, file_name=name, size=int(size),
            offset=0, declared_sha256=sha256.lower(), staged_path=str(staged.resolve().relative_to(bronze_root().resolve())),
            options=options, state="open", chunk_size=chunk_size(), created_at=_now(), updated_at=_now(),
            expires_at=_now() + _ttl(),
        )
        session.add(row)
        session.commit()
        return session_json(row)


def _owned(session: Any, caller: Caller, upload_id: str) -> LibraryUpload:
    row = session.get(LibraryUpload, upload_id)
    if row is None or row.site != caller.site or row.owner != caller.principal:
        raise SessionGone(f"no upload {upload_id!r}")
    return row


def status(caller: Caller, upload_id: str) -> dict[str, Any]:
    with session_scope() as session:
        return session_json(_owned(session, caller, upload_id))


def append(caller: Caller, upload_id: str, offset: int, chunk: bytes, *, chunk_sha256: str = "") -> dict[str, Any]:
    """Write one chunk at ``offset``. Completing the file starts the pipeline."""
    from axiom.extensions.builtins.library.pipeline import run_upload, submit, update_job

    if len(chunk) > max_chunk_bytes():
        raise limits.UploadRefused(f"a chunk may carry at most {limits.format_bytes(max_chunk_bytes())}", status=413)
    with session_scope() as session:
        row = _owned(session, caller, upload_id)
        if row.state != "open":
            raise SessionGone(f"the upload is {row.state}")
        if _aware(row.expires_at) and _aware(row.expires_at) < _now():
            raise SessionGone("the upload expired; start it again")
        if offset != row.offset:
            raise OffsetMismatch(row.offset)
        if offset + len(chunk) > row.size:
            raise limits.UploadRefused("the chunk runs past the declared size", status=413)
        if chunk_sha256 and hashlib.sha256(chunk).hexdigest() != chunk_sha256.lower():
            raise ChunkChecksumMismatch("the chunk's checksum does not match; send it again")
        staged = _staged(row)
        with staged.open("r+b") as f:
            f.seek(offset)
            f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        row.offset = offset + len(chunk)
        row.updated_at = _now()
        row.expires_at = _now() + _ttl()
        complete = row.offset >= row.size
        if complete:
            row.state = "complete"
        job_id, name, options, declared = row.job_id, row.file_name, dict(row.options or {}), row.declared_sha256
        new_offset, size = row.offset, row.size
        session.commit()
    detail = f"{limits.format_bytes(new_offset)} of {limits.format_bytes(size)}"
    update_job(job_id, bytes_received=new_offset, stage_update=("received", "running" if not complete else "done", detail))
    if complete:
        submit(run_upload, caller, staged, name, job_id, options, declared)
    return {"offset": new_offset, "complete": complete, "job_id": job_id}


def cancel(caller: Caller, upload_id: str) -> dict[str, Any]:
    from axiom.extensions.builtins.library.pipeline import update_job

    with session_scope() as session:
        row = _owned(session, caller, upload_id)
        if row.state == "complete":
            raise ValueError("the upload is complete; delete the document instead")
        row.state = "cancelled"
        row.updated_at = _now()
        _staged(row).unlink(missing_ok=True)
        job_id = row.job_id
        session.commit()
    update_job(job_id, state="cancelled", stage_update=("received", "failed", "cancelled"),
               error={"stage": "received", "message": "cancelled", "remedy": "Upload it again when you want it."})
    return {"upload_id": upload_id, "state": "cancelled"}


def sweep() -> int:
    """Remove expired sessions' bytes. Returns how many were swept."""
    from axiom.extensions.builtins.library.pipeline import update_job

    n = 0
    with session_scope() as session:
        rows = session.query(LibraryUpload).filter(LibraryUpload.state == "open").all()
        expired = [r for r in rows if _aware(r.expires_at) and _aware(r.expires_at) < _now()]
        jobs = []
        for r in expired:
            try:
                _staged(r).unlink(missing_ok=True)
            except SessionGone:
                pass
            r.state = "expired"
            jobs.append(r.job_id)
            n += 1
        session.commit()
    for j in jobs:
        update_job(j, state="cancelled", error={"stage": "received", "message": "the upload expired before it finished",
                                                 "remedy": "Start the upload again."})
    return n


def stuck_jobs_on_boot() -> int:
    """Jobs left ``running`` past ``stored`` by a restart are re-queued from bronze."""
    from axiom.extensions.builtins.library.pipeline import process, submit

    with session_scope() as session:
        rows = session.query(LibraryJob).filter(LibraryJob.state == "running", LibraryJob.item_id != "").all()
        todo = [(r.item_id, r.id) for r in rows if r.stage not in ("received", "stored")]
    for item_id, job_id in todo:
        submit(process, item_id, job_id)
    return len(todo)


__all__ = [
    "ChunkChecksumMismatch",
    "OffsetMismatch",
    "SessionGone",
    "append",
    "cancel",
    "chunk_size",
    "create",
    "max_chunk_bytes",
    "status",
    "stuck_jobs_on_boot",
    "sweep",
]
