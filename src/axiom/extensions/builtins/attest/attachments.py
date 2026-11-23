# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Attachments by content hash, served only through the attest door (ADR-251 D6, unit LD-4).

An attachment is an immutable **blob**, stored once in the node's object store under its
SHA-256, through :mod:`axiom.infra.storage`. Which provider holds the blobs is configuration
(the ``attest-blobs`` purpose); nothing here names one.

- **A draft carries attachments before it is signed** (:func:`attach`). The bytes are staged
  under the draft (``<site>/drafts/<draft_id>/<sha256>``) and the draft holds a *reference*:
  ``{sha256, media_type, bytes, name?}``, and for a video a rendition's hash with
  ``rendition``, ``policy_version`` and ``loss``. The reference is part of what the person is
  presented, so a change after presenting is refused at signing.
- **Signing** copies the references into the record's ``evidence.attachments``, inside the
  signed bytes, and promotes each staged blob to ``<site>/sha256/<sha256>`` (:func:`promote`).
  The same bytes, on one entry or many, are one blob. So a blob in the store's record prefix
  is always one a signed record names, and the chain row never holds a byte of it.
- **Serving**: the only path to a blob is the attest door ``attest.http.attachment``, which
  admits the *record* by the one check and serves a hash only when that record names it.
  Whether a hash is stored is never consulted before then, so a known hash confirms nothing.
  No public or pre-signed URL is ever made.
- **Verification** (:func:`check_rows`): every hash a record names must resolve to a blob of
  that hash; a changed byte fails the entry's verification.
- **Extraction** (:func:`index_record`): the Library's extraction path (ADR-213) reads the blob
  and its text or metadata becomes labelled rows in ``attest_attachment_index``, chunked by
  type: documents parsed and embedded, images read (OCR or a caption), video metadata only.
- **Restore validation** (:func:`restore_findings`) names every record whose hash does not
  resolve, and every blob no record names.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from . import store
from .db_models import AttestAttachmentIndex, AttestDraft, AttestRecord
from .service import AttestRefused

log = logging.getLogger("axiom.attest.attachments")

PURPOSE = "attest-blobs"
SHA = re.compile(r"^[0-9a-f]{64}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")

#: The keys a reference may hold. Never bytes, never a location.
REF_KEYS = frozenset({"sha256", "media_type", "bytes", "name", "rendition", "policy_version", "loss"})
#: The keys a rendition reference adds (ADR-251 D6, #1695): which rendition, which lossiness
#: policy version produced it, and what was lost. All three, or none.
RENDITION_KEYS = ("rendition", "policy_version", "loss")

#: Media types that run script in a viewer: never taken, whatever the door's headers say.
REFUSED_MEDIA = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "application/javascript",
    "text/javascript", "application/x-javascript", "text/xml-external-parsed-entity",
})  # fmt: skip

_DEFAULT_MAX_BYTES = 200 * 1024 * 1024


# -- the store ------------------------------------------------------------------------------

_provider: Any = None


def set_store(provider: Any = None) -> None:
    """Use ``provider`` for blobs (a test seam); None goes back to configuration."""
    global _provider
    _provider = provider


def blobs() -> Any:
    """The configured provider for attachment blobs."""
    if _provider is not None:
        return _provider
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.storage import configured

    return configured(PURPOSE, default_dir=Path(get_user_state_dir()) / "attest" / "blobs")


def _site(site: str) -> str:
    if not isinstance(site, str) or not _SITE.match(site) or ".." in site:
        raise AttestRefused(f"site {site!r} cannot name a blob prefix")
    return site


def blob_key(site: str, sha256: str) -> str:
    """Where a signed record's blob is: one prefix per site, keyed by the hash."""
    if not SHA.match(sha256 or ""):
        raise AttestRefused("an attachment is named by a 64-character lowercase sha256")
    return f"{_site(site)}/sha256/{sha256}"


def _staged_key(site: str, draft_id: str, sha256: str) -> str:
    return f"{_site(site)}/drafts/{draft_id}/{sha256}"


def _put(key: str, data: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "blob"
        path.write_bytes(data)
        blobs().upload(path, destination=key)


def _get(key: str) -> bytes | None:
    store_ = blobs()
    if not store_.exists(key):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return store_.download(key, Path(tmp) / "blob").read_bytes()
        except FileNotFoundError:
            return None


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Altered(Exception):  # noqa: N818 - an outcome a door answers
    """A stored blob whose bytes no longer hash to its name."""


def read(site: str, sha256: str) -> bytes | None:
    """The bytes of a signed record's blob, checked against the hash; None when absent. Only
    the attest door calls this, after it has admitted a record that names ``sha256``."""
    data = _get(blob_key(site, sha256))
    if data is not None and sha256_of(data) != sha256:
        raise Altered(sha256)
    return data


# -- references -----------------------------------------------------------------------------


def _max_bytes() -> int:
    try:
        from axiom.extensions.builtins.library import limits

        return limits.max_file_bytes()
    except Exception:  # noqa: BLE001 - the Library's limit when it is installed
        return _DEFAULT_MAX_BYTES


def make_ref(
    data: bytes,
    media_type: str,
    *,
    name: str | None = None,
    rendition: str | None = None,
    policy_version: str | None = None,
    loss: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The reference a record carries for ``data``. Refuses what cannot be served safely."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AttestRefused("an attachment has bytes")
    if len(data) > _max_bytes():
        raise AttestRefused(f"an attachment is at most {_max_bytes()} bytes")
    media = (media_type or "").split(";")[0].strip().lower()
    if not _MEDIA.match(media):
        raise AttestRefused(f"{media_type!r} is not a media type")
    if media in REFUSED_MEDIA:
        raise AttestRefused(f"{media} runs script in a viewer; attach it as a document instead")
    ref: dict[str, Any] = {"sha256": sha256_of(bytes(data)), "media_type": media, "bytes": len(data)}
    if name:
        clean = Path(str(name)).name.strip()
        if clean:
            ref["name"] = clean[:255]
    given = {k: v for k, v in (("rendition", rendition), ("policy_version", policy_version),
                               ("loss", loss)) if v not in (None, "")}  # fmt: skip
    if media.startswith("video/") and len(given) != len(RENDITION_KEYS):
        raise AttestRefused(
            "a video attachment is a rendition: name its rendition, the lossiness policy version "
            "that produced it and its loss record"
        )
    if given and len(given) != len(RENDITION_KEYS):
        raise AttestRefused("a rendition names rendition, policy_version and loss together")
    if given:
        if not isinstance(given["loss"], Mapping):
            raise AttestRefused("a rendition's loss record is an object")
        ref.update(rendition=str(given["rendition"]), policy_version=str(given["policy_version"]),
                   loss=dict(given["loss"]))  # fmt: skip
    return ref


def valid_ref(ref: Any) -> bool:
    return (isinstance(ref, Mapping) and set(ref) <= REF_KEYS and bool(SHA.match(str(ref.get("sha256", ""))))
            and isinstance(ref.get("bytes"), int) and bool(_MEDIA.match(str(ref.get("media_type", "")))))  # fmt: skip


def remove(site: str, sha256: str) -> bool:
    """Delete a signed record's blob (unit LD-6: its record's content was removed by a tombstone
    whose switch a named approver retired, and no live record names the hash). The caller checks
    both; this is the store's one delete. True when a blob was there."""
    key = blob_key(site, sha256)
    store_ = blobs()
    if not store_.exists(key):
        return False
    store_.delete(key)
    return True


def refs_of(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The attachment references a signed record carries, inside its signed bytes."""
    evidence = (record or {}).get("evidence") or {}
    got = evidence.get("attachments") if isinstance(evidence, Mapping) else None
    return [dict(r) for r in got or () if isinstance(r, Mapping)]


def named(record: Mapping[str, Any] | None, sha256: str) -> dict[str, Any] | None:
    """The reference ``record`` carries for ``sha256``, or None: the door serves nothing else."""
    for ref in refs_of(record):
        if ref.get("sha256") == sha256:
            return ref
    return None


# -- a draft carries attachments before it is signed ---------------------------------------


def attach(
    draft_id: str,
    data: bytes,
    media_type: str,
    *,
    by: str,
    name: str | None = None,
    rendition: str | None = None,
    policy_version: str | None = None,
    loss: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach ``data`` to the open draft ``draft_id`` of ``by``. The same bytes twice are one
    reference. Returns the reference. Anyone else is told there is no such draft."""
    from .service import _load_draft

    ref = make_ref(data, media_type, name=name, rendition=rendition, policy_version=policy_version, loss=loss)
    with store.session_scope() as s:
        d = _load_draft(s, draft_id, lock=True)
        if d.for_principal != by:
            raise AttestRefused(f"no draft {draft_id}")
        if d.status != "open":
            raise AttestRefused(f"draft {draft_id} is already {d.status}")
        held = list(d.attachments or [])
        if any(r.get("sha256") == ref["sha256"] for r in held):
            return next(r for r in held if r.get("sha256") == ref["sha256"])
        if not blobs().exists(blob_key(d.site_id, ref["sha256"])):
            _put(_staged_key(d.site_id, draft_id, ref["sha256"]), bytes(data))
        d.attachments = [*held, ref]
        s.commit()
    return ref


def detach(draft_id: str, sha256: str, *, by: str) -> None:
    """Take an attachment off the open draft ``draft_id`` of ``by``."""
    from .service import _load_draft

    with store.session_scope() as s:
        d = _load_draft(s, draft_id, lock=True)
        if d.for_principal != by:
            raise AttestRefused(f"no draft {draft_id}")
        if d.status != "open":
            raise AttestRefused(f"draft {draft_id} is already {d.status}")
        held = list(d.attachments or [])
        if not any(r.get("sha256") == sha256 for r in held):
            raise AttestRefused(f"draft {draft_id} carries no attachment {sha256}")
        d.attachments = [r for r in held if r.get("sha256") != sha256] or None
        s.commit()
    _drop_staged(d.site_id, draft_id, [sha256])


def promote(site: str, draft_id: str, refs: Iterable[Mapping[str, Any]]) -> None:
    """Before the record is chained: every blob the draft names is in the record prefix. A blob
    already there (the same bytes signed before) is not stored again."""
    store_ = blobs()
    for ref in refs:
        sha = str(ref["sha256"])
        final = blob_key(site, sha)
        if store_.exists(final):
            continue
        staged = _staged_key(site, draft_id, sha)
        data = _get(staged)
        if data is None or sha256_of(data) != sha:
            raise AttestRefused(f"the bytes of attachment {sha} are not on this node; attach it again")
        store_.move(staged, final)


def _drop_staged(site: str, draft_id: str, shas: Iterable[str]) -> None:
    for sha in shas:
        try:
            blobs().delete(_staged_key(site, draft_id, sha))
        except Exception:  # noqa: BLE001 - a staged copy left behind is reclaimed later
            pass


def forget_draft(site: str, draft_id: str, refs: Iterable[Mapping[str, Any]] | None) -> None:
    """A signed or discarded draft's staged copies go (best effort)."""
    _drop_staged(site, draft_id, [str(r.get("sha256")) for r in refs or ()])


# -- verification ---------------------------------------------------------------------------


def check_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[int, str] | None:
    """``(seq, reason)`` of the first record whose attachment does not resolve to a blob of its
    hash (``attachment_missing``, ``attachment_altered``), or None. One read per distinct blob."""
    seen: dict[tuple[str, str], str | None] = {}
    for row in rows:
        rec = row.get("record", row) if isinstance(row, Mapping) else row
        site = rec.get("site_id")
        for ref in refs_of(rec):
            key = (str(site), str(ref.get("sha256")))
            if key not in seen:
                seen[key] = _problem(*key)
            if seen[key] is not None:
                return int(rec.get("seq") or 0), seen[key]  # type: ignore[return-value]
    return None


def _problem(site: str, sha256: str) -> str | None:
    try:
        data = _get(blob_key(site, sha256))
    except AttestRefused:
        return "attachment_missing"
    if data is None:
        return "attachment_missing"
    return None if sha256_of(data) == sha256 else "attachment_altered"


@dataclass(frozen=True)
class Finding:
    """One problem restore validation found, naming the record (or the stray blob)."""

    problem: str
    sha256: str
    site_id: str
    attestation_id: str | None = None
    logbook: str | None = None
    seq: int | None = None

    def line(self) -> str:
        if self.attestation_id:
            return (f"{self.problem}: record {self.attestation_id} ({self.logbook} #{self.seq}) at "
                    f"{self.site_id} names sha256 {self.sha256}")  # fmt: skip
        return f"{self.problem}: sha256 {self.sha256} at {self.site_id} is named by no record"


def _records_with_attachments(site: str | None) -> list[tuple[str, str, str, int, dict]]:
    evidence = AttestRecord.record["evidence"]["attachments"]
    q = select(AttestRecord.attestation_id, AttestRecord.site_id, AttestRecord.logbook,
               AttestRecord.seq, AttestRecord.record).where(evidence.isnot(None))  # fmt: skip
    if site:
        q = q.where(AttestRecord.site_id == site)
    with store.session_scope() as s:
        return [tuple(r) for r in s.execute(q.order_by(AttestRecord.site_id, AttestRecord.logbook,
                                                         AttestRecord.seq))]  # fmt: skip


def restore_findings(site: str | None = None) -> list[Finding]:
    """Every hash a record names resolves to a blob of that hash; every blob in a record prefix
    is named by a record. A finding per record that does not, and per stray blob."""
    findings: list[Finding] = []
    problems: dict[tuple[str, str], str | None] = {}
    named_by_site: dict[str, set[str]] = {}
    for aid, site_id, logbook, seq, rec in _records_with_attachments(site):
        for ref in refs_of(rec):
            sha = str(ref.get("sha256"))
            named_by_site.setdefault(site_id, set()).add(sha)
            key = (site_id, sha)
            if key not in problems:
                problems[key] = _problem(site_id, sha)
            if problems[key]:
                findings.append(Finding(problems[key], sha, site_id, aid, logbook, int(seq)))  # type: ignore[arg-type]
    sites = {site} if site else set(named_by_site) | set(_stored_sites())
    for site_id in sorted(s for s in sites if s):
        for entry in list_blobs(site_id):
            if entry["sha256"] not in named_by_site.get(site_id, set()):
                findings.append(Finding("blob_unnamed", entry["sha256"], site_id))
    return findings


def _stored_sites() -> set[str]:
    out = set()
    for e in blobs().list_artifacts(""):
        parts = e.storage_id.split("/")
        if len(parts) == 3 and parts[1] == "sha256":
            out.add(parts[0])
    return out


def list_blobs(site: str) -> list[dict[str, Any]]:
    """The blobs in a site's record prefix: hash and size, nothing else. Operators only (the
    ``attest.blobs`` verb, which writes a read record); no door serves this."""
    prefix = f"{_site(site)}/sha256"
    out = []
    for e in blobs().list_artifacts(prefix):
        parts = e.storage_id.split("/")
        if len(parts) == 3 and parts[0] == site and parts[1] == "sha256" and SHA.match(parts[2]):
            out.append({"sha256": parts[2], "bytes": int(e.size_bytes or 0)})
    return sorted(out, key=lambda b: b["sha256"])


# -- extraction: a labelled derived index (ADR-213 path, chunked by type) -------------------

#: ``(path, media_type) -> (text, engine) | None``: OCR or a caption for an image.
ImageReader = Callable[[Path, str], "tuple[str, str] | None"]
#: ``(texts, routing_tier) -> (vectors, model) | None``.
Embedder = Callable[[list[str], str], "tuple[list[list[float]], str] | None"]

_image_reader: ImageReader | None = None
_embedder: Embedder | None = None

_DOCUMENT_TYPES = frozenset({
    "application/pdf", "application/json", "application/xml", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
})  # fmt: skip


def set_image_reader(reader: ImageReader | None) -> None:
    global _image_reader
    _image_reader = reader


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def kind_of(media_type: str) -> str:
    """How an attachment is chunked: ``document``, ``image``, ``video`` or ``other``."""
    if media_type.startswith("video/"):
        return "video"
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("text/") or media_type in _DOCUMENT_TYPES:
        return "document"
    return "other"


def _default_image_reader(path: Path, media_type: str) -> tuple[str, str] | None:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(str(path)) as im:
            text = pytesseract.image_to_string(im) or ""
    except Exception:  # noqa: BLE001 - an image no engine can read has no text
        return None
    return (text, "tesseract") if text.strip() else None


def _default_embedder(texts: list[str], routing_tier: str) -> tuple[list[list[float]], str] | None:
    from axiom.rag.embeddings import embed_texts_with_model

    vectors, model = embed_texts_with_model(texts, routing_tier=routing_tier)
    return (vectors, model or "") if vectors else None


def _extract(path: Path, ref: Mapping[str, Any], site: str) -> tuple[dict[str, Any], list[str]]:
    """The Library's extractors on one blob: merged metadata and text sections."""
    from axiom.extensions.builtins.library.extract import registry, runner
    from axiom.extensions.builtins.library.extract.contract import ExtractionContext

    ctx = ExtractionContext(path=path, file_name=str(ref.get("name") or f"{ref['sha256']}"),
                            size=int(ref["bytes"]), sha256=str(ref["sha256"]),
                            mime=str(ref["media_type"]), site=site)  # fmt: skip
    meta: dict[str, Any] = {}
    texts: list[str] = []
    for planned in registry.plan(ctx):
        if planned.skip:
            continue
        outcome = runner.run(planned.entry, ctx)
        if outcome.status != "done" or outcome.result is None:
            continue
        ctx.prior[planned.name] = outcome.result
        meta.update(outcome.result.to_json().get("metadata") or {})
        texts += [(f"## {t.title}\n\n" if t.title else "") + t.text for t in outcome.result.text if t.text.strip()]
    return meta, texts


def _chunks(text: str, path: str) -> list[str]:
    from axiom.rag.chunker import chunk_markdown

    return [c.text for c in chunk_markdown(text, path)]


def index_record(record: Mapping[str, Any]) -> int:
    """Extract what each attachment of a signed record holds into labelled index rows, replacing
    any earlier rows for the record. Returns the rows written. A record whose log carries no
    valid labels is refused: an extraction row without labels is never written."""
    from axiom.governance.doors import labels_valid

    from .doors import labels_for

    refs = refs_of(record)
    if not refs:
        return 0
    labels = labels_for(record.get("logbook"))
    if not labels_valid(labels):
        raise AttestRefused(f"{record.get('logbook')} carries no labels; nothing is extracted from its attachments")
    site, aid = str(record["site_id"]), str(record["attestation_id"])
    tier = "export_controlled" if labels["export_controlled"] else "any"  # type: ignore[index]
    rows: list[dict[str, Any]] = []
    for ref in refs:
        kind = kind_of(str(ref["media_type"]))
        meta: dict[str, Any] = {k: ref[k] for k in ("media_type", "bytes", "name", *RENDITION_KEYS) if k in ref}
        texts: list[str] = []
        embed = False
        if kind != "video":  # video: metadata only; its bytes are never parsed here
            data = _get(blob_key(site, str(ref["sha256"])))
            if data is None or sha256_of(data) != ref["sha256"]:
                log.warning("attest: attachment %s of %s does not resolve; not extracted", ref["sha256"], aid)
                continue
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / (ref.get("name") or "blob")
                path.write_bytes(data)
                try:
                    found, sections = _extract(path, ref, site)
                except Exception as exc:  # noqa: BLE001 - extraction is never fatal (ADR-213 D6)
                    log.warning("attest: extracting %s failed: %s", ref["sha256"], exc)
                    found, sections = {}, []
                meta.update({k: v for k, v in found.items() if k not in meta})
                if kind == "document":
                    texts = [c for sec in sections for c in _chunks(sec, aid)]
                    embed = True
                elif kind == "image":
                    read_ = (_image_reader or _default_image_reader)(path, str(ref["media_type"]))
                    if read_:
                        texts = _chunks(read_[0], aid)
                        meta["read_by"] = read_[1]
        vectors: list[list[float]] | None = None
        model = None
        if embed and texts:
            got = None
            try:
                got = (_embedder or _default_embedder)(texts, tier)
            except Exception as exc:  # noqa: BLE001 - unembedded rows wait for an embedder
                log.warning("attest: embedding %s failed: %s", ref["sha256"], exc)
            if got and len(got[0]) == len(texts):
                vectors, model = got
        base = {"attestation_id": aid, "sha256": ref["sha256"], "site_id": site,
                "logbook": record["logbook"], "classification": labels["classification"],  # type: ignore[index]
                "export_controlled": bool(labels["export_controlled"]), "kind": kind}  # type: ignore[index]
        rows.append({**base, "part": "metadata", "chunk": 0, "meta": meta})
        for i, t in enumerate(texts):
            rows.append({**base, "part": "text", "chunk": i, "text": t,
                         "embedding": vectors[i] if vectors else None, "embed_model": model})  # fmt: skip
    write_rows(aid, rows)
    return len(rows)


def write_rows(attestation_id: str, rows: list[dict[str, Any]]) -> None:
    """Replace a record's extraction rows. Every row carries the labels, the record and the
    hash, or nothing is written."""
    for r in rows:
        if (r.get("attestation_id") != attestation_id or not SHA.match(str(r.get("sha256", "")))
                or not r.get("site_id") or not r.get("logbook") or not r.get("classification")
                or not isinstance(r.get("export_controlled"), bool)):  # fmt: skip
            raise AttestRefused("an extraction row carries the entry's labels, its record and the hash")
    now = datetime.now(UTC)
    with store.session_scope() as s:
        s.execute(delete(AttestAttachmentIndex).where(AttestAttachmentIndex.attestation_id == attestation_id))
        for r in rows:
            s.add(AttestAttachmentIndex(created_at=now, **r))
        s.commit()


def index_rows(attestation_id: str) -> list[dict[str, Any]]:
    """A record's extraction rows (for its door and for tests)."""
    cols = AttestAttachmentIndex.__table__.columns
    with store.session_scope() as s:
        got = s.execute(select(AttestAttachmentIndex).where(AttestAttachmentIndex.attestation_id == attestation_id)
                        .order_by(AttestAttachmentIndex.sha256, AttestAttachmentIndex.part,
                                  AttestAttachmentIndex.chunk)).scalars()  # fmt: skip
        return [{c.name: getattr(row, c.name) for c in cols if c.name not in ("id", "created_at")} for row in got]


def after_signed(record: Mapping[str, Any], draft_id: str | None = None) -> None:
    """After the commit: the draft's staged copies go and the record's attachments are indexed.
    Never raises; a failure leaves the index to be rebuilt."""
    if draft_id:
        forget_draft(str(record["site_id"]), draft_id, refs_of(record))
    if not refs_of(record):
        return
    try:
        index_record(record)
    except Exception as exc:  # noqa: BLE001 - the signature stands whatever the index does
        log.warning("attest: indexing the attachments of %s failed: %s", record.get("attestation_id"), exc)


def draft_attachments(d: AttestDraft) -> list[dict[str, Any]]:
    return [dict(r) for r in d.attachments or ()]


def _restore_check() -> list[str]:
    return [f.line() for f in restore_findings()]


def _register_restore_check() -> None:
    from axiom.infra.backup import register_restore_check

    register_restore_check("attest_attachments_resolve", _restore_check)


_register_restore_check()


__all__ = [
    "Altered",
    "Finding",
    "PURPOSE",
    "REF_KEYS",
    "after_signed",
    "attach",
    "blob_key",
    "blobs",
    "check_rows",
    "detach",
    "forget_draft",
    "index_record",
    "index_rows",
    "kind_of",
    "list_blobs",
    "make_ref",
    "named",
    "promote",
    "read",
    "refs_of",
    "remove",
    "restore_findings",
    "set_embedder",
    "set_image_reader",
    "set_store",
    "valid_ref",
    "write_rows",
]
