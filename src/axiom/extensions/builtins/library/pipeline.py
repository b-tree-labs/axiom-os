# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The upload pipeline: received, stored, identified, extracted, conformed, indexed (ADR-213).

Every file a person gives the Library, by drag, by chat attachment, by the CLI
or by an agent, runs these six stages, and each stage's outcome is written to
the job before the next starts, so "what happened to this file" is always
answerable from the catalog, mid-run or after a crash.

**The file is never lost.** Once ``stored`` is done the bytes are in bronze and
the item is listed; everything after that can fail, and a failure is a status
on the job and the item with a remedy, never a missing document. Retry and
re-extract start from bronze.

**One extractor's failure is one extractor's.** Each runs isolated with its own
timeout (:mod:`.extract.runner`); the stage is ``partial`` when some failed and
the others' outputs still route.

**Re-extraction replaces in place** (ADR-206's rebuild): a new result for the
same (item, extractor, member) becomes current, older rows stay as history, and
every route is offered the new result again (routes are idempotent on that key).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.library import limits
from axiom.extensions.builtins.library.db_models import (
    LibraryEvent,
    LibraryExtraction,
    LibraryItem,
    LibraryJob,
    LibraryRoute,
)
from axiom.extensions.builtins.library.scope import Caller, bronze_root, corpus_for, site_root
from axiom.extensions.builtins.library.store import rag_store, session_scope

log = logging.getLogger(__name__)

STAGES = ("received", "stored", "identified", "extracted", "conformed", "indexed")
TERMINAL = ("done", "partial", "failed", "held", "cancelled")
SOURCE_NAME = "library"
WORKERS_ENV = "AXIOM_LIBRARY_PIPELINE_WORKERS"
INLINE_ENV = "AXIOM_LIBRARY_PIPELINE_INLINE"
MAX_ROUTE_TABLE_ROWS = 50_000


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    return (v if v.tzinfo else v.replace(tzinfo=UTC)).isoformat()


# ------------------------------------------------------------------------- jobs


def _fresh_stages() -> list[dict[str, Any]]:
    return [{"stage": s, "status": "pending", "started_at": None, "finished_at": None, "detail": ""} for s in STAGES]


def new_job(
    session: Any,
    caller: Caller,
    file_name: str,
    size: int,
    *,
    kind: str = "upload",
    item_id: str = "",
    upload_id: str = "",
    options: dict[str, Any] | None = None,
    bytes_received: int = 0,
) -> LibraryJob:
    job = LibraryJob(
        id=str(uuid.uuid4()), site=caller.site, owner=caller.principal, kind=kind, item_id=item_id,
        upload_id=upload_id, file_name=file_name, size=size, bytes_received=bytes_received, state="queued",
        stage="received", stages=_fresh_stages(), progress=0.0, error=None, options=dict(options or {}),
        created_at=_now(), updated_at=_now(),
    )
    if kind != "upload":
        # The bytes are already in bronze: received and stored are history.
        for s in job.stages[:2]:
            s.update(status="done", started_at=_iso(_now()), finished_at=_iso(_now()), detail="already in bronze")
        job.bytes_received = size
        job.stage = "identified"
    session.add(job)
    return job


def _progress(job: LibraryJob) -> float:
    done = sum(1 for s in job.stages or [] if s["status"] in ("done", "skipped", "failed"))
    received = (job.bytes_received / job.size) if job.size else 1.0
    return round(min(1.0, 0.5 * min(1.0, received) + 0.5 * done / len(STAGES)), 4)


def job_json(job: LibraryJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "item_id": job.item_id or None,
        "upload_id": job.upload_id or None,
        "file_name": job.file_name,
        "size": job.size,
        "bytes_received": job.bytes_received,
        "state": job.state,
        "stage": job.stage,
        "stages": list(job.stages or []),
        "progress": _progress(job),
        "error": job.error or None,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }


def update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    """Apply changes to a job in its own session (one commit per session; see service.py)."""
    stage = changes.pop("stage_update", None)
    with session_scope() as session:
        job = session.get(LibraryJob, job_id)
        if job is None:
            return {}
        if stage is not None:
            name, status, detail = stage
            stages = [dict(s) for s in job.stages or _fresh_stages()]
            for s in stages:
                if s["stage"] == name:
                    if status == "running" and not s.get("started_at"):
                        s["started_at"] = _iso(_now())
                    if status in ("done", "failed", "skipped"):
                        s["started_at"] = s.get("started_at") or _iso(_now())
                        s["finished_at"] = _iso(_now())
                    s["status"] = status
                    if detail is not None:
                        s["detail"] = detail
            job.stages = stages
            job.stage = name
        for k, v in changes.items():
            setattr(job, k, v)
        job.progress = _progress(job)
        job.updated_at = _now()
        session.commit()
        return job_json(job)


def _stage(job_id: str, name: str, status: str, detail: str | None = None, **changes: Any) -> None:
    update_job(job_id, stage_update=(name, status, detail), **changes)


def _fail(job_id: str, stage: str, message: str, remedy: str, *, state: str = "failed") -> None:
    _stage(job_id, stage, "failed", message, state=state, error={"stage": stage, "message": message, "remedy": remedy})


def get_job(caller: Caller, job_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        job = session.get(LibraryJob, job_id)
        if job is None or job.site != caller.site or job.owner != caller.principal:
            return None
        return job_json(job)


def list_jobs(caller: Caller, *, ids: list[str] | None = None, active: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as session:
        q = session.query(LibraryJob).filter(LibraryJob.site == caller.site, LibraryJob.owner == caller.principal)
        if ids:
            q = q.filter(LibraryJob.id.in_(ids[:200]))
        if active:
            q = q.filter(LibraryJob.state.notin_(TERMINAL))
        rows = q.order_by(LibraryJob.updated_at.desc()).limit(max(1, min(int(limit), 200))).all()
        return [job_json(j) for j in rows]


# ------------------------------------------------------------------- execution

_executor: ThreadPoolExecutor | None = None
_exec_lock = threading.Lock()


def _inline() -> bool:
    return (os.environ.get(INLINE_ENV) or "").strip() in ("1", "true", "yes")


def submit(fn: Any, *args: Any, **kwargs: Any) -> None:
    """Run a pipeline in the background, or now when ``AXIOM_LIBRARY_PIPELINE_INLINE=1`` (tests)."""
    global _executor
    if _inline():
        fn(*args, **kwargs)
        return
    with _exec_lock:
        if _executor is None:
            try:
                n = max(1, int(os.environ.get(WORKERS_ENV, "2")))
            except ValueError:
                n = 2
            _executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="library-pipeline")
    _executor.submit(_guarded, fn, *args, **kwargs)


def _guarded(fn: Any, *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - a crashed pipeline is a failed job, not a dead worker
        log.exception("library pipeline crashed")
        job_id = kwargs.get("job_id") or (args[1] if len(args) > 1 else None)
        if isinstance(job_id, str):
            _fail(job_id, "extracted", "the pipeline stopped unexpectedly", "Retry; the file is safe if it was stored.")


# --------------------------------------------------------------------- storing


@dataclass
class Stored:
    item: dict[str, Any]
    duplicate: bool = False
    warnings: list[str] = field(default_factory=list)


def _land(site: str, staged: Path, sha: str, fetched: Any, corpus: str) -> tuple[Path, Path]:
    """Move the staged bytes into bronze, content-addressed, and write the sidecar. No second copy in memory."""
    from axiom.extensions.builtins.data_platform.bronze import FilesystemBronzeSink
    from axiom.rag.ingest_router import Disposition, RouteDecision

    root = site_root(site)
    blob_dir = root / "_content" / sha[:2]
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob = blob_dir / sha
    if blob.exists():
        staged.unlink(missing_ok=True)
    else:
        tmp = blob.with_suffix(".part")
        shutil.move(str(staged), tmp)
        tmp.replace(blob)
    sink = FilesystemBronzeSink(root=root)
    record = sink.write_record(
        item=fetched,
        decision=RouteDecision(disposition=Disposition.ALLOW, tier=corpus, matched=None, reason="library upload"),
        tier=corpus,
        content_hash=sha,
        fetched_at=_now(),
    )
    return blob, record


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(bronze_root().resolve()))
    except ValueError:
        return str(path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def store(
    caller: Caller,
    staged: Path,
    raw_name: str,
    *,
    job_id: str,
    options: dict[str, Any] | None = None,
    declared_sha256: str = "",
) -> Stored | None:
    """Stage ``stored``: guards, checksum, duplicate check, bronze, catalog. ``None`` when refused (job says why)."""
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem
    from axiom.extensions.builtins.library.defaults import active_taxonomy, chain_for
    from axiom.extensions.builtins.library.intake import intake
    from axiom.extensions.builtins.library.service import (
        MAX_COLLECTION_CHARS,
        MAX_TITLE_CHARS,
        _clean_tags,
        _clean_visibility,
        _event,
        to_dict,
    )

    opts = dict(options or {})
    _stage(job_id, "received", "done", None, state="running")
    _stage(job_id, "stored", "running", "verifying")
    try:
        name, kind, scanned = limits.validate_path(raw_name, staged)
        vis = _clean_visibility(opts.get("visibility"))
        tags = _clean_tags(opts.get("tags"))
    except (limits.UploadRefused, ValueError) as exc:
        staged.unlink(missing_ok=True)
        remedy = {
            413: "Split the file or ask the operator to raise the limit.",
            415: "Convert it to an accepted type, or install an extension that reads this type.",
        }.get(getattr(exc, "status", 422), "Check the file and upload it again.")
        _fail(job_id, "stored", str(exc), remedy)
        return None
    sha = _sha256_file(staged)
    if declared_sha256 and declared_sha256.lower() != sha:
        staged.unlink(missing_ok=True)
        _fail(job_id, "stored", "the file's checksum does not match what the client sent",
              "The bytes changed in transit. Upload it again.")
        return None
    size = staged.stat().st_size
    with staged.open("rb") as f:
        head = f.read(65536)

    with session_scope() as session:
        for existing in (
            session.query(LibraryItem)
            .filter(LibraryItem.site == caller.site, LibraryItem.sha256 == sha, LibraryItem.deleted_at.is_(None))
            .all()
        ):
            from axiom.extensions.builtins.library.scope import can_read

            if can_read(caller, existing) and existing.visibility == vis:
                _event(session, existing, caller, "duplicate", file_name=name)
                session.commit()
                staged.unlink(missing_ok=True)
                out = to_dict(existing)
                _stage(job_id, "stored", "done", f"already in the library as {existing.title!r}", item_id=existing.id)
                for s in STAGES[2:]:
                    _stage(job_id, s, "skipped", "not run again for identical bytes")
                update_job(job_id, state="done")
                return Stored(item=out, duplicate=True, warnings=[f"{name!r} is already in the library as {existing.title!r}; not filed twice"])

        taxonomy = active_taxonomy()
        placed = intake(name, chain=chain_for(taxonomy.name), taxonomy=taxonomy, mime_type=kind.mime, content=head)
        item_id = str(uuid.uuid4())
        corpus = corpus_for(caller, vis)
        title = opts.get("title")
        item = LibraryItem(
            id=item_id, site=caller.site, owner=caller.principal, visibility=vis, file_name=name,
            title=(" ".join((title or "").split()) or name)[:MAX_TITLE_CHARS], mime_type=kind.mime, size=size,
            sha256=sha, scan=scanned, taxonomy=taxonomy.name, document_type=placed.document_type,
            category=placed.category, label=placed.label, confidence=float(placed.confidence),
            classifier=placed.classifier, attributes=dict(placed.attributes), tags=tags,
            collection=" ".join((opts.get("collection") or "").split())[:MAX_COLLECTION_CHARS], corpus=corpus,
            source_path=f"{SOURCE_NAME}/{caller.site}/{item_id}/{name}", index_status="pending",
            folder="", placement={}, pipeline_state="running", job_id=job_id, ec={},
            created_at=_now(), updated_at=_now(),
        )
        if opts.get("folder"):
            item.placement = {"requested_folder": str(opts["folder"])[:255]}
        fetched = FetchedItem(
            source_name=SOURCE_NAME, item_id=item_id, display_name=name, content=b"", content_type=kind.mime,
            size=size, modified_at=_now(), etag=sha, source_path=item.source_path,
            extra={"site": caller.site, "owner": caller.principal, "visibility": vis, "sha256": sha, "mime_type": kind.mime},
        )
        blob, record = _land(caller.site, staged, sha, fetched, corpus)
        item.bronze_record = _rel(record)
        item.bronze_content = _rel(blob)
        session.add(item)
        _event(session, item, caller, "upload", file_name=name, size=size, sha256=sha, visibility=vis, scan=scanned,
               placed=placed.describe(), job_id=job_id)
        session.commit()
        out = to_dict(item)
    _stage(job_id, "stored", "done", f"in bronze ({limits.format_bytes(size)}, sha256 {sha[:12]}…)", item_id=item_id)
    return Stored(item=out)


# ------------------------------------------------------------------ processing


def _content_path(item: LibraryItem) -> Path:
    root = bronze_root().resolve()
    path = (root / item.bronze_content).resolve()
    if root not in path.parents:
        raise FileNotFoundError(f"no stored content for {item.id!r}")
    return path


def derived_dir(item: LibraryItem | dict[str, Any]) -> Path:
    site = item["site"] if isinstance(item, dict) else item.site
    iid = item["id"] if isinstance(item, dict) else item.id
    d = site_root(site) / SOURCE_NAME / "_derived" / iid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _member_key(member: str) -> str:
    return "" if not member else "~" + hashlib.sha256(member.encode()).hexdigest()[:12]


@dataclass
class _Done:
    manifest: Any
    member: str
    result: Any
    row_id: int


def process(item_id: str, job_id: str, *, only: list[str] | None = None, stale_only: bool = False) -> None:
    """Stages identified through indexed, for an item already in bronze."""
    from axiom.extensions.builtins.library.extract.contract import ExtractionContext
    from axiom.extensions.builtins.library.extract.registry import plan

    with session_scope() as session:
        item = session.get(LibraryItem, item_id)
        if item is None:
            _fail(job_id, "identified", "the item is gone", "Upload it again.")
            return
        path = _content_path(item)
        snapshot = {"site": item.site, "id": item.id, "file_name": item.file_name, "mime": item.mime_type,
                    "size": item.size, "sha256": item.sha256}
        params = {}
        job_row = session.get(LibraryJob, job_id)
        if job_row is not None:
            params = dict((job_row.options or {}).get("extract_params") or {})
        if not params and item.job_id and item.job_id != job_id:
            first = session.get(LibraryJob, item.job_id)
            params = dict(((first.options if first else None) or {}).get("extract_params") or {})
        current_versions = {
            (r.extractor, r.member): r.version
            for r in session.query(LibraryExtraction).filter(LibraryExtraction.item_id == item_id, LibraryExtraction.current.is_(True)).all()
        }

    _stage(job_id, "identified", "running", None, state="running", item_id=item_id)
    ctx = ExtractionContext(path=path, file_name=snapshot["file_name"], size=snapshot["size"], sha256=snapshot["sha256"],
                            mime=snapshot["mime"], site=snapshot["site"], item_id=item_id, params=params)
    planned = plan(ctx, only=set(only) if only else None)
    if stale_only:
        planned = [p for p in planned if current_versions.get((p.name, "")) != p.entry.manifest.version]
    running = [p for p in planned if not p.skip]
    _stage(job_id, "identified", "done", (f"{len(running)} extractor(s): " + ", ".join(p.name for p in running)) if running else "no extractor takes this file; stored and listed")

    _stage(job_id, "extracted", "running", None)
    done: list[_Done] = []
    failures: list[str] = []
    _extract_all(item_id, job_id, ctx, planned, done, failures, depth=0)
    if only or stale_only:
        # Partial re-extraction: the extractors not re-run keep their current results.
        rerun = {(d.manifest.name, d.member) for d in done}
        done.extend(_current_results(item_id, exclude=rerun))
    detail = f"{len(done)} result(s)" + (f"; not extracted: {'; '.join(failures[:5])}" if failures else "")
    _stage(job_id, "extracted", "done", detail)

    _stage(job_id, "conformed", "running", None)
    route_failures = _route_all(item_id, job_id, done)
    _book(item_id, done)
    _stage(job_id, "conformed", "done", "routed" + (f"; {len(route_failures)} route(s) did not complete" if route_failures else ""))

    _stage(job_id, "indexed", "running", None)
    verdict, index_note = _index_stage(item_id, done)
    status = "done"
    _stage(job_id, "indexed", status, index_note)

    state = "held" if verdict == "held" else ("partial" if (failures or route_failures) else "done")
    err = None
    if state == "partial":
        err = {"stage": "extracted" if failures else "conformed",
               "message": "; ".join((failures + route_failures)[:3]),
               "remedy": "The file is stored and listed. Re-extract after the cause is fixed; see the item's pipeline for each step."}
    elif state == "held":
        err = {"stage": "indexed", "message": index_note,
               "remedy": "An export-control reviewer releases held items; until then it is stored, listed and registered, not searchable."}
    update_job(job_id, state=state, error=err)
    with session_scope() as session:
        row = session.get(LibraryItem, item_id)
        if row is not None:
            row.pipeline_state = state
            row.job_id = job_id
            row.updated_at = _now()
            session.commit()


def _extract_all(item_id: str, job_id: str, ctx: Any, planned: list[Any], done: list[_Done], failures: list[str], *, depth: int) -> None:
    from axiom.extensions.builtins.library.extract.contract import ExtractionContext
    from axiom.extensions.builtins.library.extract.registry import plan
    from axiom.extensions.builtins.library.extract.runner import Outcome, run

    for p in planned:
        m = p.entry.manifest
        if p.skip:
            if not p.skip.startswith("superseded") and "exclusive" not in p.skip:
                _record(item_id, job_id, m, ctx.member, Outcome("skipped" if "unavailable" not in p.skip else "unavailable", error=p.skip), ctx)
            continue
        outcome = run(p.entry, ctx)
        row_id = _record(item_id, job_id, m, ctx.member, outcome, ctx)
        if outcome.status == "done" and outcome.result is not None:
            ctx.prior[m.name] = outcome.result
            done.append(_Done(m, ctx.member, outcome.result, row_id))
            siblings = {Path(c.member).name: c.path for c in outcome.result.children}
            for child in outcome.result.children:
                try:
                    cctx = ExtractionContext.for_path(
                        child.path, file_name=Path(child.member).name, site=ctx.site, item_id=item_id,
                        member=f"{ctx.member}!/{child.member}" if ctx.member else child.member, depth=depth + 1,
                        siblings={n: p for n, p in siblings.items() if p != child.path}, params=dict(ctx.params),
                    )
                    cctx.mime = _mime_for(cctx.file_name)
                    _extract_all(item_id, job_id, cctx, plan(cctx), done, failures, depth=depth + 1)
                except Exception as exc:  # noqa: BLE001 - one bad member is that member's
                    failures.append(f"{child.member}: {exc}")
            if outcome.result.children:
                unpacked = (outcome.result.metadata or {}).pop("unpacked_dir", "")
                if unpacked:
                    shutil.rmtree(unpacked, ignore_errors=True)
        elif outcome.status in ("failed", "timeout"):
            failures.append(f"{m.name}{(' on ' + ctx.member) if ctx.member else ''}: {outcome.error}")


def _current_results(item_id: str, *, exclude: set[tuple[str, str]]) -> list[_Done]:
    """The item's current successful extractions, rebuilt from their bronze-derived records."""
    from types import SimpleNamespace

    from axiom.extensions.builtins.library.extract.contract import (
        ExtractionResult,
        Registration,
        Table,
        TextSection,
    )
    from axiom.extensions.builtins.library.extract.registry import get

    out: list[_Done] = []
    with session_scope() as session:
        rows = (
            session.query(LibraryExtraction)
            .filter(LibraryExtraction.item_id == item_id, LibraryExtraction.current.is_(True), LibraryExtraction.status == "done")
            .all()
        )
        rows = [(r.id, r.extractor, r.version, r.member, list(r.tags or []), r.derived_path) for r in rows]
    for row_id, name, version, member, tags, derived in rows:
        if (name, member) in exclude or not derived:
            continue
        try:
            data = json.loads((bronze_root() / derived).read_text())
        except (OSError, ValueError):
            continue
        reg = get(name)
        manifest = reg.manifest if reg is not None and reg.manifest.version == version else SimpleNamespace(
            name=name, version=version, tags=tuple(tags), ec=reg.manifest.ec if reg else "screen", ref=f"{name}@{version}",
        )
        result = ExtractionResult(
            metadata=data.get("metadata") or {}, features=data.get("features") or {},
            text=[TextSection(t["text"], t.get("title", "")) for t in data.get("text_sections") or []],
            tables=[Table(name=t["name"], columns=t["columns"], rows=t.get("rows") or [], row_count=t.get("row_count"),
                          types=t.get("types") or {}, units=t.get("units") or {}, schema_ref=t.get("schema_ref", ""))
                    for t in data.get("tables") or []],
            registrations=[Registration(r["target"], r.get("payload") or {}) for r in data.get("registrations") or []],
            preview=data.get("preview"), tags=list(data.get("tags") or []), code=data.get("code", ""),
            summary=data.get("summary", ""),
        )
        out.append(_Done(manifest, member, result, row_id))
    return out


def _mime_for(name: str) -> str:
    try:
        return limits.file_type_for(name).mime
    except limits.UploadRefused:
        return "application/octet-stream"


def _record(item_id: str, job_id: str, m: Any, member: str, outcome: Any, ctx: Any) -> int:
    """Persist one extraction: a derived record in bronze, a catalog row, the previous one made history."""
    result_json: dict[str, Any] = {}
    derived = ""
    out_sha = ""
    if outcome.result is not None:
        result_json = outcome.result.to_json()
        full = {**result_json, "text_sections": [{"title": t.title, "text": t.text} for t in outcome.result.text],
                "extractor": m.name, "version": m.version, "input_sha256": ctx.sha256, "member": member,
                "produced_at": _iso(_now())}
        body = json.dumps(full, sort_keys=True, default=str).encode()
        out_sha = hashlib.sha256(body).hexdigest()
        with session_scope() as session:
            item = session.get(LibraryItem, item_id)
            d = derived_dir(item)
        f = d / f"{m.name}@{m.version}{_member_key(member)}.json"
        f.write_bytes(body)
        derived = _rel(f)
        for art in outcome.result.artifacts:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in art.name)[:120]
            (d / f"{m.name}{_member_key(member)}-{safe}").write_bytes(art.content)
    with session_scope() as session:
        for old in (
            session.query(LibraryExtraction)
            .filter(LibraryExtraction.item_id == item_id, LibraryExtraction.extractor == m.name,
                    LibraryExtraction.member == member, LibraryExtraction.current.is_(True))
            .all()
        ):
            old.current = False
        row = LibraryExtraction(
            item_id=item_id, job_id=job_id, extractor=m.name, version=m.version, scope=m.scope,
            contributed_by=m.contributed_by, member=member, status=outcome.status,
            summary=(outcome.result.summary if outcome.result is not None else "")[:2000], result=result_json,
            tags=sorted(set(m.tags) | set(outcome.result.tags if outcome.result is not None else [])),
            derived_path=derived, input_sha256=ctx.sha256, output_sha256=out_sha, duration_ms=outcome.duration_ms,
            error=outcome.error[:2000], remedy=outcome.remedy[:2000], current=True, started_at=_now(),
        )
        session.add(row)
        session.commit()
        return row.id


# ---------------------------------------------------------------------- routing


def _item_dict(item_id: str) -> dict[str, Any]:
    from axiom.extensions.builtins.library.service import to_dict

    with session_scope() as session:
        return to_dict(session.get(LibraryItem, item_id))


def _route_all(item_id: str, job_id: str, done: list[_Done]) -> list[str]:
    from axiom.extensions.builtins.library.extract.routes import (
        RouteContext,
        RouteOutcome,
        routes_for,
    )

    item = _item_dict(item_id)
    with session_scope() as session:
        row = session.get(LibraryItem, item_id)
        path = _content_path(row)
        ddir = derived_dir(row)
    with session_scope() as session:
        for old in session.query(LibraryRoute).filter(LibraryRoute.item_id == item_id, LibraryRoute.current.is_(True)).all():
            old.current = False
        session.commit()
    failures: list[str] = []
    for d in done:
        tags = sorted((set(d.manifest.tags) | set(d.result.tags)) - set(d.result.drop_tags))
        for tag in tags:
            if tag == "rag":
                continue  # the indexed stage
            regs = [r for r in d.result.registrations if r.target == tag]
            outcomes: list[tuple[str, RouteOutcome]] = []
            if tag in ("training-data", "reference-data"):
                outcomes.append(("core.route.mark", RouteOutcome("done", target=tag, detail=f"marked {tag} in the catalog")))
            if tag == "dataset" and d.result.tables:
                outcomes.append(("core.route.bronze_derived", _dataset_derived(ddir, d)))
            handlers = routes_for(tag)
            claimed = False
            for route in handlers:
                ctx = RouteContext(tag=tag, item=item, path=path, extractor=d.manifest.name,
                                   extractor_version=d.manifest.version, result=d.result,
                                   registration=regs[0] if regs else None, tables=list(d.result.tables),
                                   derived_dir=ddir, member=d.member)
                try:
                    out = route.resolve()(ctx)
                    if out is None:
                        continue  # not this route's
                    if not isinstance(out, RouteOutcome):
                        raise TypeError(f"returned {type(out).__name__}, not a RouteOutcome")
                except Exception as exc:  # noqa: BLE001 - a route failing is that route's
                    out = RouteOutcome("failed", target=route.name, detail=f"{type(exc).__name__}: {exc}"[:500],
                                       remedy="The output is kept in bronze; retry after the route is fixed.")
                claimed = True
                outcomes.append((route.name, out))
            if not claimed and tag in ("input-deck", "rom-artifact", "dataset") and not (tag == "dataset" and d.result.tables):
                outcomes.append(("", RouteOutcome(
                    "no-route", target=tag,
                    detail=f"no {tag} route is installed on this node; the output is kept and is routed when one is",
                    remedy="Install the extension that registers this kind of output, then re-extract.",
                )))
            with session_scope() as session:
                for name, out in outcomes:
                    session.add(LibraryRoute(
                        item_id=item_id, job_id=job_id, extractor=d.manifest.name, tag=tag, route=name,
                        status=out.status, target=out.target[:255], detail=out.detail[:2000], link=out.link,
                        remedy=out.remedy[:2000], current=True, at=_now(),
                    ))
                    if out.status == "failed":
                        failures.append(f"{name or tag}: {out.detail}")
                session.commit()
    return failures


def _dataset_derived(ddir: Path, d: _Done) -> Any:
    """Every table as bronze-derived JSON lines, the conform input a dataset route reads."""
    from axiom.extensions.builtins.library.extract.contract import jsonable
    from axiom.extensions.builtins.library.extract.routes import RouteOutcome

    written = []
    for t in d.result.tables:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in t.name)[:80] or "table"
        f = ddir / f"{d.manifest.name}{_member_key(d.member)}-{safe}.table.jsonl"
        with f.open("w") as out:
            out.write(json.dumps({"columns": t.columns, "types": t.types, "units": t.units, "row_count": t.row_count,
                                  "schema_ref": t.schema_ref, "extractor": d.manifest.ref, "member": d.member}) + "\n")
            for r in (t.records or t.rows)[:MAX_ROUTE_TABLE_ROWS]:
                out.write(json.dumps(jsonable(r)) + "\n")
        written.append(f.name)
    return RouteOutcome(
        "done", target="bronze-derived",
        detail=f"{len(written)} table(s) kept as bronze-derived for conform",
        link={"kind": "dataset", "label": ", ".join(t.name for t in d.result.tables)[:120], "ref": written[0] if written else ""},
    )


# ------------------------------------------------------------------- Data Book


def _signals(item: LibraryItem, done: list[_Done]) -> Any:
    from axiom.extensions.builtins.library.book.place import Signals

    metadata: dict[str, Any] = {}
    features: dict[str, Any] = {}
    tags: set[str] = set()
    text = ""
    for d in done:
        if d.member:
            continue  # an archive files as itself; its members inform it only through the archive's listing
        metadata.update({k: v for k, v in (d.result.metadata or {}).items() if isinstance(v, (str, int, float, bool))})
        features.update({k: v for k, v in (d.result.features or {}).items() if isinstance(v, (str, int, float, bool))})
        if d.result.code:
            features.setdefault("code", d.result.code)
        tags |= set(d.manifest.tags) | set(d.result.tags)
        if not text:
            text = "\n".join(t.text[:4000] for t in d.result.text)[:8000]
    return Signals(
        name=item.file_name, mime=item.mime_type, extractors=sorted({d.manifest.name for d in done if not d.member}),
        tags=sorted(tags), metadata=metadata, features=features, text=text, title=item.title,
        document_type=item.document_type,
    )


def _book(item_id: str, done: list[_Done]) -> None:
    from axiom.extensions.builtins.library.book import service as book

    with session_scope() as session:
        item = session.get(LibraryItem, item_id)
        if item is None:
            return
        current = item.placement or {}
        if current.get("source") == "manual":
            return  # a person's move is never undone by a re-extraction
        signals = _signals(item, done)
        requested = current.get("requested_folder")
        if requested and book.folder_exists(session, item.site, requested):
            placement = {
                "path": requested, "labels": book._user_labels(session, item.site, requested, book.active_scheme()),
                "confidence": 1.0, "source": "manual", "needs_look": False, "because": "it was uploaded into this folder",
                "signature": sorted(signals.signature()),
            }
            placement["explanation"] = f"Filed under {' / '.join(placement['labels'])} because it was uploaded into this folder."
        else:
            placed, _ = book.file_item(session, item.site, signals)
            placement = book.placement_json(placed, signals)
        item.folder = placement["path"]
        item.placement = placement
        session.commit()


# ------------------------------------------------------------------- indexing


def _index_stage(item_id: str, done: list[_Done]) -> tuple[str, str]:
    """EC hook, then the embed: returns (verdict, what happened)."""
    from axiom.extensions.builtins.library.extract import ec

    texts: list[tuple[str, str, str]] = []  # (heading, text, member)
    codes: list[str] = []
    code_like = False
    rag_bound = False
    for d in done:
        if d.manifest.ec == "payload" and d.result.code:
            codes.append(d.result.code)
        if (d.result.features or {}).get("code_like"):
            code_like = True
        if "rag" in (set(d.manifest.tags) | set(d.result.tags)) - set(d.result.drop_tags):
            for t in d.result.text:
                if t.text.strip():
                    rag_bound = True
                    heading = t.title or (d.member and f"{d.member}") or ""
                    texts.append((heading, t.text, d.member))
    with session_scope() as session:
        item = session.get(LibraryItem, item_id)
        verdict = ec.assess(item.file_name, texts=[t for _, t, _ in texts], payload_codes=codes, code_like=code_like)
        item.ec = verdict.to_json()
        if not limits.file_type_for(item.file_name).indexable and not rag_bound:
            item.index_status, item.index_reason, item.chunk_count = "unsupported", "not a text format", 0
        elif verdict.holds_rag:
            item.index_status, item.index_reason, item.chunk_count = "held", "; ".join(verdict.reasons)[:2000], 0
        elif not rag_bound:
            item.index_status, item.index_reason, item.chunk_count = "unsupported", "no text was extracted", 0
        session.commit()
        status = item.index_status
    if status in ("unsupported", "held"):
        with session_scope() as session:
            item = session.get(LibraryItem, item_id)
            reason = item.index_reason
        return verdict.verdict, ("held: " + reason) if status == "held" else reason
    doc = _rag_document(texts)
    with session_scope() as session:
        item = session.get(LibraryItem, item_id)
        f = derived_dir(item) / "rag.md"
        f.write_text(doc)
    return verdict.verdict, embed(item_id)


def _rag_document(texts: list[tuple[str, str, str]]) -> str:
    if len(texts) == 1 and not texts[0][0]:
        return texts[0][1]
    parts = []
    for heading, text, _member in texts:
        parts.append((f"## {heading}\n\n" if heading else "") + text.strip())
    return "\n\n".join(parts)


def embed(item_id: str) -> str:
    """Embed the item's retrieval document (bronze-derived ``rag.md``) into its corpus."""
    from axiom.extensions.builtins.data_platform.bronze import BronzeWriteResult
    from axiom.extensions.builtins.data_platform.contracts import FetchedItem
    from axiom.rag.ingest_router import Disposition

    with session_scope() as session:
        item = session.get(LibraryItem, item_id)
        doc = derived_dir(item) / "rag.md"
        if (item.ec or {}).get("verdict") == "held":
            return "held"
        if not doc.exists():
            item.index_status, item.index_reason = "pending", "no retrieval document yet; re-extract"
            session.commit()
            return item.index_reason
        store_ = rag_store()
        if store_ is None:
            item.index_status, item.index_reason = "pending", "no retrieval store on this install"
            session.commit()
            return item.index_reason
        text_sha = hashlib.sha256(doc.read_bytes()).hexdigest()
        fetched = FetchedItem(
            source_name=SOURCE_NAME, item_id=item.id, display_name=item.file_name + ".md", content=b"",
            content_type="text/markdown", size=doc.stat().st_size, modified_at=_now(), etag=text_sha,
            source_path=item.source_path,
            extra={"site": item.site, "owner": item.owner, "visibility": item.visibility, "sha256": item.sha256},
        )
        record = BronzeWriteResult(
            item_id=item.id, disposition=Disposition.ALLOW, tier=item.corpus, content_hash=text_sha,
            record_path=bronze_root() / item.bronze_record, content_path=doc, reason="library extraction",
            matched_rule=None, fetched_at=_now(),
        )
        from axiom.extensions.builtins.data_platform.rag_embed import embed_bronze_record

        try:
            try:
                store_.delete_document(item.source_path, corpus=item.corpus)
            except Exception:  # noqa: BLE001 - nothing to replace yet
                pass
            stats = embed_bronze_record(record, fetched, store_)
        except Exception as exc:  # noqa: BLE001 - an index failure is a status, not a lost upload
            log.warning("library: indexing %s failed: %s", item.id, exc)
            item.index_status, item.index_reason = "pending", f"indexing failed: {type(exc).__name__}"
            session.commit()
            return item.index_reason
        if stats.indexed:
            item.index_status, item.index_reason, item.chunk_count = "indexed", "", stats.chunks_created
            note = f"{stats.chunks_created} chunk(s) in {item.corpus}"
        elif stats.skipped_reason == "embed_failed":
            item.index_status, item.index_reason = "pending", "the embedding service did not answer; retried later"
            note = item.index_reason
        else:
            item.index_status, item.index_reason, item.chunk_count = "unsupported", stats.skipped_reason or "", 0
            note = item.index_reason
        session.commit()
        return note


# --------------------------------------------------------------------- entry points


def run_upload(caller: Caller, staged: Path, raw_name: str, job_id: str, options: dict[str, Any] | None = None,
               declared_sha256: str = "") -> Stored | None:
    stored = store(caller, staged, raw_name, job_id=job_id, options=options, declared_sha256=declared_sha256)
    if stored is None or stored.duplicate:
        return stored
    process(stored.item["id"], job_id)
    from axiom.extensions.builtins.library.service import to_dict

    with session_scope() as session:
        stored.item = to_dict(session.get(LibraryItem, stored.item["id"]))
    return stored


def start_reextract(caller: Caller, item_id: str, *, extractors: list[str] | None = None, stale_only: bool = False,
                    kind: str = "reextract") -> dict[str, Any]:
    from axiom.extensions.builtins.library.scope import can_change
    from axiom.extensions.builtins.library.service import Forbidden, NotFound, _visible

    with session_scope() as session:
        item = _visible(session, caller, item_id)
        if not can_change(caller, item):
            raise Forbidden("only the person who filed a document re-extracts it")
        job = new_job(session, caller, item.file_name, item.size, kind=kind, item_id=item.id,
                      options={"extractors": extractors or [], "stale_only": stale_only})
        session.add(LibraryEvent(item_id=item.id, site=item.site, principal=caller.principal, action=kind,
                                 surface=caller.surface, detail={"job_id": job.id, "extractors": extractors or [],
                                                                 "stale_only": stale_only}, at=_now()))
        session.commit()
        out = job_json(job)
    if not item_id:
        raise NotFound(item_id)
    submit(process, item_id, out["id"], only=extractors or None, stale_only=stale_only)
    return out


def retry(caller: Caller, job_id: str) -> dict[str, Any]:
    """Re-run a job: from bronze when stored, else the person uploads again (the bytes never arrived)."""
    from axiom.extensions.builtins.library.service import NotFound

    found = get_job(caller, job_id)
    if found is None:
        raise NotFound(f"no job {job_id!r}")
    if not found["item_id"]:
        raise ValueError("this upload never reached bronze; upload the file again")
    return start_reextract(caller, found["item_id"], kind="retry")


def stale_items(caller: Caller) -> list[dict[str, Any]]:
    """Items whose current extraction is from an older extractor version than this node has."""
    from axiom.extensions.builtins.library.extract.registry import all_extractors

    versions = {n: r.manifest.version for n, r in all_extractors().items()}
    out: dict[str, dict[str, Any]] = {}
    with session_scope() as session:
        rows = (
            session.query(LibraryExtraction, LibraryItem)
            .join(LibraryItem, LibraryItem.id == LibraryExtraction.item_id)
            .filter(LibraryItem.site == caller.site, LibraryItem.deleted_at.is_(None), LibraryExtraction.current.is_(True))
            .all()
        )
        for ext, item in rows:
            have = versions.get(ext.extractor)
            if have and have != ext.version:
                e = out.setdefault(item.id, {"id": item.id, "file_name": item.file_name, "stale": []})
                e["stale"].append({"extractor": ext.extractor, "from": ext.version, "to": have})
    return list(out.values())


__all__ = [
    "STAGES",
    "Stored",
    "derived_dir",
    "embed",
    "get_job",
    "job_json",
    "list_jobs",
    "new_job",
    "process",
    "retry",
    "run_upload",
    "stale_items",
    "start_reextract",
    "store",
    "submit",
    "update_job",
]
