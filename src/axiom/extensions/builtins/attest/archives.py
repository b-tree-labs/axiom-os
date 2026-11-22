# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A sealed period archived as a verifiable package, verified offline, then retired (unit LD-5).

ADR-251 D7: the record is never compacted; a sealed period is packaged, the package goes to cold
storage through the storage-provider abstraction, and the hot copy is retired only after the
package verifies offline, as a person's explicit act through the switch runner (ADR-197).

- **The package** (:func:`build`) is a zip written entry by entry to a temporary file, never held
  whole in memory: ``records.jsonl`` (the period's signed records from its first to the seal, as
  chained), ``rows.jsonl`` (the chain rows' own columns, from which gold is rebuilt),
  ``attachments/sha256/<hex>`` (every blob a record names, refused when one is missing),
  ``keys.json``, ``anchors.jsonl`` (each anchor that fixes a head in the period, with the audit
  path from that head to the root, so no other log is named), ``summary.json`` (the seal's
  summary), ``verify.py`` and ``verify_archive.py`` (standard library only), and ``manifest.json``
  last: logbook, site, period, sequence range, head digest, anchors covered, labels, retention
  rule, every file's SHA-256 and the package digest over all of it.
- **The target** is the provider configured for :data:`PURPOSE` (``AXIOM_STORAGE_ATTEST_ARCHIVE``,
  or ``..._ARCHIVE_<NAME>`` for a named target); its ``storage_class`` says whether it is
  controlled. A log carrying the export-control flag archives only to a controlled class.
- **Verify** (:func:`verify`) downloads the stored package and runs the standalone verifier in a
  separate interpreter with no site packages, no environment and no database; the report is
  stored against exactly the package digest it checked.
- **The switch** (:func:`plan`): ``prepare`` builds and verifies, the gate is the stored report,
  ``commit`` turns the period's reads to the package, ``rollback`` turns them back, and
  ``retire`` (``reclaim``: a named person, a change record, the report checked again) drops the
  hot derived copies: the fine-grained vectors of the period's records (the seal's stays).
  Moving the chain's own rows is a partition move (LD-7), after this.
- **Reads** of an archived period come from the package (:func:`overlay`, called by the log's
  readers), through the same doors and the same check; the read record names the package as
  its source (``source = "archive:sha256:<package digest>"``). The package itself is reached only
  by an admitted door (:func:`read_package`).
- **The schedule** (:func:`seal_cadences`, :func:`schedule_seals`): a log whose seal boundary is
  a clock period gets a PULSE entry at each period end plus its grace; any other log is sealed by
  hand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from . import registry, service, signing, store
from .db_models import AttestArchive, AttestRecord
from .service import AttestRefused

log = logging.getLogger("axiom.attest.archives")

PURPOSE = "attest-archive"
FORMAT = "axiom-attest-archive/1"
CONTROLLED = "controlled"
STANDARD = "standard"
#: Statuses whose period is read from the package. A ``tombstoned`` period's package is gone with
#: its content (unit LD-6): its reads come from the chain, content-less.
SERVING = ("archived", "retired")
KIND = "attest.archive"
#: How many records one read of the chain table pages through while packaging.
PAGE = 500
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_BLOCK = 1 << 20


class ArchiveRefused(AttestRefused):
    """An archive step that cannot be taken; the message says why."""


# -- the target ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    name: str
    provider: Any
    storage_class: str


_override: dict[str, Target] = {}


def set_target(provider: Any = None, *, storage_class: str = STANDARD, name: str = "default") -> None:
    """Use ``provider`` for the named target (a test seam); None goes back to configuration."""
    if provider is None:
        _override.pop(name, None)
    else:
        _override[name] = Target(name, provider, storage_class)


def _purpose(name: str) -> str:
    return PURPOSE if name == "default" else f"{PURPOSE}-{name}"


def target(name: str = "default") -> Target:
    """The configured archive target: its provider and its storage class (``standard`` unless the
    configuration says ``storage_class``). Nothing here names a provider."""
    if name in _override:
        return _override[name]
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.storage import configured, env_name

    purpose = _purpose(name)
    raw = os.environ.get(env_name(purpose), "").strip()
    try:
        cls = str((json.loads(raw) if raw else {}).get("storage_class") or STANDARD)
    except (ValueError, AttributeError):
        raise ArchiveRefused(f"{env_name(purpose)} is not a JSON object") from None
    provider = configured(purpose, default_dir=Path(get_user_state_dir()) / "attest" / "archive")
    return Target(name, provider, cls)


def _checked_target(name: str, labels: Mapping[str, Any]) -> Target:
    """The target, refused before anything is read when it may not hold this log (LD-5-A5)."""
    from axiom.infra.storage import StorageProvider

    t = target(name)
    if not isinstance(t.provider, StorageProvider):
        raise ArchiveRefused("a package is written only through the storage-provider abstraction "
                             "(axiom.infra.storage), never a vendor client")  # fmt: skip
    if labels.get("export_controlled") and t.storage_class != CONTROLLED:
        raise ArchiveRefused(
            f"this log carries the export-control flag, so it archives only to controlled storage; "
            f"the target {name!r} is {t.storage_class!r}. Nothing was written."
        )
    return t


# -- the package --------------------------------------------------------------------------------


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def package_digest(manifest: Mapping[str, Any]) -> str:
    """The digest over the manifest without its own ``package_digest`` (the verifier's rule)."""
    return hashlib.sha256(_canonical({k: v for k, v in manifest.items() if k != "package_digest"})).hexdigest()


def _tools() -> dict[str, Path]:
    from axiom.attest.tools import verify as core
    from axiom.attest.tools import verify_archive

    return {"verify.py": Path(core.__file__), "verify_archive.py": Path(verify_archive.__file__)}


class _Writer:
    """Zip entries written one at a time, each hashed as it streams; fixed times and order."""

    def __init__(self, path: Path) -> None:
        self.zip = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        self.files: dict[str, str] = {}

    def _info(self, name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        return info

    def lines(self, name: str, items: Iterable[Any]) -> None:
        h = hashlib.sha256()
        with self.zip.open(self._info(name), "w", force_zip64=True) as out:
            for item in items:
                line = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                h.update(line)
                out.write(line)
        self.files[name] = h.hexdigest()

    def data(self, name: str, data: bytes) -> None:
        self.zip.writestr(self._info(name), data)
        self.files[name] = hashlib.sha256(data).hexdigest()

    def file(self, name: str, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as src, self.zip.open(self._info(name), "w", force_zip64=True) as out:
            for block in iter(lambda: src.read(_BLOCK), b""):
                h.update(block)
                out.write(block)
        self.files[name] = h.hexdigest()
        return self.files[name]

    def close(self) -> None:
        self.zip.close()


def _iso(value: Any) -> Any:
    from .gold import iso

    return iso(value) if isinstance(value, datetime) else value


def _row_columns(r: AttestRecord) -> dict[str, Any]:
    """A chain row's own columns (everything but the record and the response link)."""
    return {"attestation_id": r.attestation_id, "site_id": r.site_id, "logbook": r.logbook,
            "seq": int(r.seq), "digest": r.digest, "prev_digest": r.prev_digest, "kind": r.kind,
            "meaning": r.meaning, "entry_type": r.entry_type, "signer": r.signer,
            "occurred_at": _iso(r.occurred_at), "recorded_at": _iso(r.recorded_at), "target": r.target}  # fmt: skip


def _segment(site: str, logbook: str, lo: int, hi: int) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """``(record, row)`` from ``lo`` to ``hi``, a page at a time: the chain's own rows, read by
    the log that owns them (building the package is building evidence, not serving)."""
    after = lo - 1
    while after < hi:
        with store.session_scope() as s:
            rows = list(s.execute(
                select(AttestRecord).where(AttestRecord.site_id == site, AttestRecord.logbook == logbook,
                                           AttestRecord.seq > after, AttestRecord.seq <= hi)
                .order_by(AttestRecord.seq).limit(PAGE)).scalars())  # fmt: skip
            got = [(dict(r.record), _row_columns(r)) for r in rows]
        if not got:
            return
        yield from got
        after = got[-1][1]["seq"]


def _seal(site: str, logbook: str, seal_id: str | None) -> dict[str, Any]:
    with store.session_scope() as s:
        seals = service._seal_records(s, site, logbook)  # noqa: SLF001 - the log reading its own chain
    if seal_id:
        seals = [r for r in seals if r["attestation_id"] == seal_id]
        if not seals:
            raise ArchiveRefused(f"no seal {seal_id} in {logbook} at {site}; only a sealed period is archived")
    if not seals:
        raise ArchiveRefused(f"{logbook} at {site} has no sealed period to archive")
    return seals[-1]


def _anchors_for(site: str, logbook: str, lo: int, hi: int) -> list[dict[str, Any]]:
    """Each anchor that fixed this log's head inside the period, as the package carries it: the
    head, the audit path to the root, the root and the node's signature, and no other head."""
    from axiom.attest.anchor import inclusion_proof

    out = []
    for a in service.anchors(site):
        head = next((h for h in a["heads"] if h["logbook"] == logbook), None)
        if head is None or not lo <= int(head["seq"]) <= hi:
            continue
        out.append({"anchor_id": a["anchor_id"], "created_at": _iso(a["created_at"]), "root": a["root"],
                    "node_sig": a["node_sig"], "head": dict(head),
                    "proof": inclusion_proof(a["heads"], logbook)})  # fmt: skip
    return out


def _blob_to(site: str, sha: str, dest: Path) -> bool:
    """Download one attachment blob through the store's provider; False when it is absent."""
    from . import attachments

    store_ = attachments.blobs()
    key = attachments.blob_key(site, sha)
    if not store_.exists(key):
        return False
    try:
        store_.download(key, dest)
    except FileNotFoundError:
        return False
    return True


def _write_package(path: Path, site: str, book: Any, seal: dict[str, Any], t: Target) -> dict[str, Any]:
    covers = (seal.get("content") or {}).get("seal", {}).get("covers") or {}
    lo, hi = int(covers["from_seq"]), int(seal["seq"])
    w = _Writer(path)
    try:
        first: dict[str, Any] = {}
        key_ids: set[str] = set()
        blobs: dict[str, int] = {}
        count = 0

        from . import attachments

        def records() -> Iterator[dict[str, Any]]:
            nonlocal count
            for rec, _row in _segment(site, book.id, lo, hi):
                first.setdefault("rec", rec)
                key_ids.add(str((rec.get("node_sig") or {}).get("key_id", "")))
                if rec.get("personal_sig"):
                    key_ids.add(str(rec["personal_sig"].get("key_id", "")))
                for ref in attachments.refs_of(rec):
                    blobs.setdefault(str(ref.get("sha256")), int(rec["seq"]))
                count += 1
                yield rec

        w.lines("records.jsonl", records())
        if count != hi - lo + 1 or not first:
            raise ArchiveRefused(f"#{lo} to #{hi} of {book.id} is not whole in the chain; nothing was written")
        w.lines("rows.jsonl", (row for _rec, row in _segment(site, book.id, lo, hi)))
        with tempfile.TemporaryDirectory() as tmp:
            for sha, seq in sorted(blobs.items()):
                blob = Path(tmp) / sha
                if not _blob_to(site, sha, blob):
                    raise ArchiveRefused(f"#{seq} names attachment sha256:{sha}, which is not stored; a package "
                                         "never omits an attachment a record names. Nothing was written.")  # fmt: skip
                if w.file(f"attachments/sha256/{sha}", blob) != sha:
                    raise ArchiveRefused(f"#{seq} names attachment sha256:{sha}, whose stored bytes are altered; "
                                         "nothing was written")  # fmt: skip
                blob.unlink()
        anchors = _anchors_for(site, book.id, lo, hi)
        if not any(int(a["head"]["seq"]) == hi for a in anchors):
            raise ArchiveRefused(f"no anchor fixes the seal (#{hi}); anchor the site, then archive")
        key_ids |= {str(a["node_sig"].get("key_id", "")) for a in anchors}
        keys = signing.public_keys()
        missing = sorted(k for k in key_ids if k not in keys)
        if missing:
            raise ArchiveRefused(f"the public key {missing[0]} that signed this period is not known here")
        w.data("keys.json", json.dumps({k: base64.b64encode(keys[k]).decode("ascii") for k in sorted(key_ids)},
                                       indent=2).encode())  # fmt: skip
        w.lines("anchors.jsonl", anchors)
        content = seal.get("content") or {}
        w.data("summary.json", json.dumps({"title": content.get("title"), "summary": content.get("summary")},
                                          indent=2, sort_keys=True, ensure_ascii=False).encode())  # fmt: skip
        for name, src in _tools().items():
            w.file(name, src)
        manifest = {
            "format": FORMAT,
            "site_id": site,
            "logbook": book.id,
            "logbook_version": book.version,
            "period": covers.get("period") or {"boundary": content.get("seal", {}).get("boundary")},
            "range": {"from_seq": lo, "to_seq": int(covers["to_seq"]), "last_seq": hi,
                      "start_prev": first["rec"]["prev_digest"]},
            "covers": covers,
            "head": {"seq": hi, "digest": seal["digest"]},
            "seal": {"attestation_id": seal["attestation_id"], "seq": hi, "digest": seal["digest"]},
            "anchors": sorted(a["anchor_id"] for a in anchors),
            "labels": dict(book.labels),
            "retention": book.retention,
            "storage_class": t.storage_class,
            "target": t.name,
            "counts": {"records": count, "attachments": len(blobs)},
            "files": dict(sorted(w.files.items())),
            "verify": "python verify_archive.py <this package, unpacked or zipped>",
        }  # fmt: skip
        manifest["package_digest"] = package_digest(manifest)
        w.zip.writestr(w._info("manifest.json"), json.dumps(manifest, indent=2, sort_keys=True).encode())  # noqa: SLF001
    finally:
        w.close()
    return manifest


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def _row(a: AttestArchive) -> dict[str, Any]:
    return {
        "archive_id": a.archive_id, "site_id": a.site_id, "logbook": a.logbook, "seal_id": a.seal_id,
        "from_seq": int(a.from_seq), "to_seq": int(a.to_seq), "last_seq": int(a.last_seq),
        "package_digest": a.package_digest, "package_sha256": a.package_sha256,
        "storage_class": a.storage_class, "status": a.status,
        "verified": bool((a.verify_report or {}).get("ok"))
        and (a.verify_report or {}).get("package_digest") == a.package_digest,
        "verified_at": _iso(a.verified_at), "retired_at": _iso(a.retired_at), "retired_by": a.retired_by,
        "created_by": a.created_by, "created_at": _iso(a.created_at), "switch_id": a.switch_id,
    }  # fmt: skip


def build(site: str, logbook: str, *, by: str, seal_id: str | None = None, target_name: str = "default"
          ) -> dict[str, Any]:  # fmt: skip
    """Package a sealed period and store it (status ``packaged``). The same seal is packaged once."""
    from axiom.governance import doors as g

    book = registry.get(logbook)
    labels = book.labels
    if not g.labels_valid(labels):
        raise ArchiveRefused(f"{logbook} declares no labels; an unlabeled log is not archived")
    t = _checked_target(target_name, labels)
    seal = _seal(site, logbook, seal_id)
    with store.session_scope() as s:
        have = s.execute(select(AttestArchive).where(
            AttestArchive.site_id == site, AttestArchive.logbook == logbook,
            AttestArchive.seal_id == seal["attestation_id"])).scalar_one_or_none()  # fmt: skip
        if have is not None:
            return _row(have)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "package.zip"
        manifest = _write_package(path, site, book, seal, t)
        sha = _sha256_file(path)
        dest = f"{site}/{logbook}/{seal['attestation_id']}/{manifest['package_digest']}.zip"
        up = t.provider.upload(path, destination=dest, metadata={"content_type": "application/zip"})
    if not getattr(up, "success", True):
        raise ArchiveRefused(f"the package could not be stored: {getattr(up, 'error', '')}")
    covers = manifest["covers"]
    row = AttestArchive(
        archive_id=str(uuid.uuid4()), site_id=site, logbook=logbook, seal_id=seal["attestation_id"],
        from_seq=manifest["range"]["from_seq"], to_seq=int(covers["to_seq"]), last_seq=manifest["range"]["last_seq"],
        package_digest=manifest["package_digest"], package_sha256=sha, storage_id=up.storage_id,
        storage_class=t.storage_class, manifest=manifest, status="packaged", created_by=by,
        created_at=datetime.now(UTC),
    )  # fmt: skip
    with store.session_scope() as s:
        s.add(row)
        s.commit()
        return _row(row)


def _get(archive_id: str) -> AttestArchive:
    with store.session_scope() as s:
        a = s.get(AttestArchive, archive_id)
        if a is None:
            raise ArchiveRefused(f"no archive {archive_id}")
        s.expunge(a)
        return a


def get(archive_id: str) -> dict[str, Any]:
    return _row(_get(archive_id))


def manifest(archive_id: str) -> dict[str, Any]:
    return dict(_get(archive_id).manifest)


# -- verify, offline ----------------------------------------------------------------------------


def _download(a: AttestArchive, dest: Path, provider: Any = None) -> Path:
    """The stored package, checked against the digest of what was stored."""
    p = provider if provider is not None else _provider_for(a)
    p.download(a.storage_id, dest)
    if _sha256_file(dest) != a.package_sha256:
        raise ArchiveRefused(f"the stored package of archive {a.archive_id} is not the package that was written")
    return dest


def _provider_for(a: AttestArchive) -> Any:
    name = (a.manifest or {}).get("target") or "default"
    return target(name).provider


def run_verifier(package: Path) -> dict[str, Any]:
    """The standalone verifier in its own interpreter: isolated (``-I``: no environment, no user
    site, not the working directory), no site packages (``-S``), so nothing of this node is
    importable, and run from a directory that holds only the two scripts."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, src in _tools().items():
            shutil.copyfile(src, Path(tmp) / name)
        done = subprocess.run(  # noqa: S603 - our own script, a fixed argument list
            [sys.executable, "-I", "-S", str(Path(tmp) / "verify_archive.py"), str(package)],
            cwd=tmp, env={}, capture_output=True, text=True, timeout=600, check=False,
        )
    try:
        report = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report = {"ok": False, "reason": f"the verifier said nothing readable (exit {done.returncode})"}
    report["exit"] = done.returncode
    report["ok"] = bool(report.get("ok")) and done.returncode == 0
    return report


def verify(archive_id: str, *, by: str) -> dict[str, Any]:
    """Download the stored package, verify it offline, and store the report against exactly the
    package digest it checked (status ``verified`` when it holds)."""
    a = _get(archive_id)
    with tempfile.TemporaryDirectory() as tmp:
        report = run_verifier(_download(a, Path(tmp) / "package.zip"))
    report.update(package_sha256=a.package_sha256, by=by, at=datetime.now(UTC).isoformat(),
                  verifier=_sha256_file(_tools()["verify_archive.py"]))  # fmt: skip
    with store.session_scope() as s:
        row = s.get(AttestArchive, archive_id)
        row.verify_report = report
        row.verified_at = datetime.now(UTC)
        if report["ok"] and report.get("package_digest") == row.package_digest and row.status == "packaged":
            row.status = "verified"
        s.commit()
        return {**_row(row), "report": report}


def verified(a: AttestArchive | dict[str, Any]) -> bool:
    """A stored verify report for exactly this package digest, and it holds (LD-5-A2)."""
    report = a.verify_report if isinstance(a, AttestArchive) else a.get("verify_report")
    digest = a.package_digest if isinstance(a, AttestArchive) else a.get("package_digest")
    return bool(report) and bool(report.get("ok")) and report.get("package_digest") == digest


# -- the switch (ADR-197) -----------------------------------------------------------------------


def _set_status(archive_id: str, status: str, **extra: Any) -> None:
    with store.session_scope() as s:
        row = s.get(AttestArchive, archive_id)
        row.status = status
        for k, v in extra.items():
            setattr(row, k, v)
        s.commit()
    _forget_ranges()


class ArchiveHooks:
    """The four hooks, and ``retire``, for one archive."""

    def __init__(self, archive_id: str, *, by: str) -> None:
        self.archive_id, self.by = archive_id, by

    def prepare(self, ctx: Any) -> None:
        a = _get(self.archive_id)
        if not verified(a):
            verify(self.archive_id, by=self.by)
        ctx.state["package_digest"] = a.package_digest

    def _report_holds(self, ctx: Any) -> tuple[bool, str]:
        a = _get(self.archive_id)
        if not verified(a):
            return False, f"no stored verify report for package {a.package_digest}"
        return True, f"verified {a.package_digest}"

    def _stored_still_matches(self, ctx: Any) -> tuple[bool, str]:
        a = _get(self.archive_id)
        if not verified(a):
            return False, f"no stored verify report for package {a.package_digest}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                _download(a, Path(tmp) / "package.zip")
        except (ArchiveRefused, OSError) as exc:
            return False, str(exc)
        return True, "the stored package is the one verified"

    def probe(self, ctx: Any) -> list[Any]:
        from axiom.infra.switchplan import Probe

        return [Probe(name="package-verified", kind="callable", when="pre", fn=self._report_holds),
                Probe(name="stored-package-verified", kind="callable", when="post", fn=self._stored_still_matches)]  # fmt: skip

    def commit(self, ctx: Any, scope: Any) -> str:
        with scope.holding():
            _set_status(self.archive_id, "archived")
        return "reads of the period come from the package"

    def rollback(self, ctx: Any) -> str:
        a = _get(self.archive_id)
        if a.status == "archived":
            _set_status(self.archive_id, "verified" if verified(a) else "packaged")
        return "reads of the period come from the chain"

    def retire(self, ctx: Any) -> str:
        a = _get(self.archive_id)
        from . import index

        _set_status(self.archive_id, "retired", retired_at=datetime.now(UTC), retired_by=ctx.params.get("approver"))
        # The period's fine rows go and its seal's summary stays, folded (unit LD-8).
        index.refresh([(a.site_id, a.logbook)])
        return "the period's fine chunks dropped; the seal's summary stays indexed"


def _switch_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir()) / "attest" / "archive-switches"


def plan(archive_id: str, *, owner: str, change_path: Path | None = None, soak: timedelta = timedelta(0),
         approver: str | None = None) -> Any:  # fmt: skip
    """The archive's switch on the one runner: ``reclaim`` retire, owned by ``owner``."""
    from axiom.infra.switchplan import FileLedger, SwitchPlan

    a = _get(archive_id)
    p = SwitchPlan(kind=KIND, subject=f"{a.site_id}/{a.logbook}#{a.from_seq}-{a.last_seq}",
                   hooks=ArchiveHooks(archive_id, by=owner), ledger=FileLedger(_switch_dir()),
                   change_kind="migration", retire="reclaim", owner=owner, soak=soak,
                   change_path=change_path, record_id=a.switch_id, params={"approver": approver},
                   rollback_command=f"log archive rollback {archive_id}")  # fmt: skip
    if a.switch_id != p.record.id:
        with store.session_scope() as s:
            s.get(AttestArchive, archive_id).switch_id = p.record.id
            s.commit()
    return p


def commit(archive_id: str, *, owner: str, change_path: Path | None = None) -> dict[str, Any]:
    """Prepare (verify when not yet verified), prove (the stored report) and commit: reads of the
    period come from the package. Refused, with nothing switched, when the report does not hold."""
    p = plan(archive_id, owner=owner, change_path=change_path)
    p.run()
    return get(archive_id)


def soak_check(archive_id: str, *, owner: str, change_path: Path | None = None) -> str:
    return plan(archive_id, owner=owner, change_path=change_path).soak_check()


def retire(archive_id: str, *, approver: str, change_path: Path | None = None,
           override_soak: str | None = None) -> dict[str, Any]:  # fmt: skip
    """A person's explicit retire through the runner (ADR-197 D6). Refused without a stored verify
    report for exactly this package digest, without the switch's change record, or before the
    switch committed; the runner adds a named approver, the owner and due time, and the post
    probe run again now."""
    a = _get(archive_id)
    if not verified(a):
        raise ArchiveRefused(f"retire refused: no stored verify report for package {a.package_digest}; "
                             "verify it first")  # fmt: skip
    if not a.switch_id:
        raise ArchiveRefused("retire refused: this archive's switch has no change record; commit it first")
    from axiom.infra import change_intent

    p = plan(archive_id, owner=a.created_by, change_path=change_path, approver=approver)
    if not change_intent.has_intent(p.record.change_id, path=change_path):
        raise ArchiveRefused("retire refused: the switch's change record was not written (ADR-182 D5a)")
    p.retire(approver=approver, override_soak=override_soak)
    return get(archive_id)


# -- reads: the period from the package ----------------------------------------------------------

_packages: OrderedDict[str, dict[int, dict[str, Any]]] = OrderedDict()
_PACKAGES_KEPT = 4
_lock = threading.Lock()
_source: ContextVar[tuple[str, ...]] = ContextVar("axiom_attest_archive_source", default=())


def _forget_ranges() -> None:
    """Nothing is cached about which periods serve from a package: each read asks."""


def serving_ranges(site: str, logbook: str) -> list[dict[str, Any]]:
    """The archived periods of one log whose reads come from their packages."""
    with store.session_scope() as s:
        if not _table_exists(s):
            return []
        rows = s.execute(
            select(AttestArchive).where(AttestArchive.site_id == site, AttestArchive.logbook == logbook,
                                        AttestArchive.status.in_(SERVING))
            .order_by(AttestArchive.from_seq)).scalars()  # fmt: skip
        return [{"archive_id": a.archive_id, "from_seq": int(a.from_seq), "last_seq": int(a.last_seq),
                 "package_digest": a.package_digest, "package_sha256": a.package_sha256} for a in rows]  # fmt: skip


def _table_exists(s: Any) -> bool:
    """Whether this schema has the archives table (a node before migration 0018 has none)."""
    try:
        return bool(s.execute(text("SELECT to_regclass('attest_archives') IS NOT NULL")).scalar())
    except Exception:  # noqa: BLE001 - no table, no archive
        return False


def _package_records(archive_id: str, sha: str) -> dict[int, dict[str, Any]]:
    with _lock:
        if sha in _packages:
            _packages.move_to_end(sha)
            return _packages[sha]
    a = _get(archive_id)
    out: dict[int, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        path = _download(a, Path(tmp) / "package.zip")
        with zipfile.ZipFile(path) as z, z.open("records.jsonl") as fh:
            for raw in fh:
                if raw.strip():
                    rec = json.loads(raw)
                    out[int(rec["seq"])] = rec
    with _lock:
        _packages[sha] = out
        while len(_packages) > _PACKAGES_KEPT:
            _packages.popitem(last=False)
    return out


def note_source(package_digest: str) -> None:
    got = _source.get()
    if package_digest not in got:
        _source.set((*got, package_digest))


def forget_source() -> None:
    """A new read begins: no package has been read for it yet."""
    _source.set(())


def source_fields() -> dict[str, Any]:
    """What a read record adds when the read came from a package: its source."""
    got = _source.get()
    if not got:
        return {}
    return {"source": ",".join(f"archive:sha256:{d}" for d in got)}


def overlay(site: str, logbook: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``records`` with every one inside an archived period read from its package instead."""
    if not records:
        return records
    hidden = _hidden(site, logbook)
    ranges = serving_ranges(site, logbook)
    if not ranges and not hidden:
        return records
    out = []
    for rec in records:
        seq = int(rec["seq"])
        tid = next((t for lo, hi, t in hidden if lo <= seq <= hi), None)
        if tid is not None:
            # A tombstone hides or removed this record's content (unit LD-6): never the package.
            from . import retention

            out.append(rec if "content" not in rec else retention.without_content(rec, tid))
            continue
        a = next((r for r in ranges if r["from_seq"] <= seq <= r["last_seq"]), None)
        if a is None:
            out.append(rec)
            continue
        got = _package_records(a["archive_id"], a["package_sha256"]).get(seq)
        if got is None or got.get("attestation_id") != rec.get("attestation_id"):
            raise ArchiveRefused(f"#{seq} is not in its archive's package as chained; it is not served")
        note_source(a["package_digest"])
        out.append(got)
    return out


def overlay_gold(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """A gold row whose record is in an archived period, with its content from the package (the
    gold doors: a reference resolved, a search result shown)."""
    if not row or row.get("seq") is None:
        return row
    if "content" in row and row["content"] is None:
        return row  # removed (unit LD-6): answered as removed, never filled from the package
    seq = int(row["seq"])
    if any(lo <= seq <= hi for lo, hi, _t in _hidden(str(row["site_id"]), str(row["logbook"]))):
        return {**row, "content": None}  # a tombstone hides or removed it (unit LD-6)
    ranges = serving_ranges(str(row["site_id"]), str(row["logbook"]))
    a = next((r for r in ranges if r["from_seq"] <= seq <= r["last_seq"]), None)
    if a is None:
        return row
    rec = _package_records(a["archive_id"], a["package_sha256"]).get(seq)
    if rec is None or (row.get("digest") and rec.get("digest") != row["digest"]) or (
            rec.get("attestation_id") != row.get("attestation_id")):  # fmt: skip
        raise ArchiveRefused(f"#{seq} is not in its archive's package as chained; it is not served")
    note_source(a["package_digest"])
    return {**row, "content": rec.get("content")}


def _hidden(site: str, logbook: str) -> list[tuple[int, int, str]]:
    from . import retention

    return retention.hidden_ranges(site, logbook)


def read_package(archive_id: str) -> list[dict[str, Any]]:
    """The records of an archived period, for a door that admitted the read; refused outside one
    (ADR-251 D7: archive reads go through the same door)."""
    from . import doors

    admitted = doors.current()
    a = _get(archive_id)
    if admitted is None or admitted.target.site != a.site_id or admitted.target.scope != a.logbook:
        raise ArchiveRefused("an archive is read through a door that admitted its log; nothing was read")
    recs = _package_records(a.archive_id, a.package_sha256)
    note_source(a.package_digest)
    return [recs[k] for k in sorted(recs)]


def listed(site: str, logbook: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """A log's archives, newest first: metadata only, for a door that admitted the log."""
    with store.session_scope() as s:
        if not _table_exists(s):
            return []
        rows = s.execute(select(AttestArchive).where(AttestArchive.site_id == site, AttestArchive.logbook == logbook)
                         .order_by(AttestArchive.from_seq.desc()).limit(max(1, min(int(limit), 200)))).scalars()  # fmt: skip
        return [_row(a) for a in rows]


def retired_ranges(s: Any, site: str, logbook: str) -> list[tuple[int, int, str]]:
    """``(from_seq, last_seq, seal_id)`` of each retired period of a log (for the index)."""
    if not _table_exists(s):
        return []
    return [(int(a.from_seq), int(a.last_seq), a.seal_id) for a in s.execute(
        select(AttestArchive).where(AttestArchive.site_id == site, AttestArchive.logbook == logbook,
                                    AttestArchive.status == "retired")).scalars()]  # fmt: skip


# -- gold, rebuilt from the package (LD-5-A7) ----------------------------------------------------


def gold_rows_from_package(archive_id: str) -> list[dict[str, Any]]:
    """The gold rows of the period, built from the package exactly as the view builds them from
    the chain: the row's columns, and ``content`` and ``interval`` from the signed record."""
    a = _get(archive_id)
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        path = _download(a, Path(tmp) / "package.zip")
        with zipfile.ZipFile(path) as z:
            recs = [json.loads(x) for x in z.read("records.jsonl").splitlines() if x.strip()]
            rows = [json.loads(x) for x in z.read("rows.jsonl").splitlines() if x.strip()]
    for rec, row in zip(recs, rows, strict=True):
        out.append({**{k: row[k] for k in ("attestation_id", "site_id", "logbook", "seq", "kind", "meaning",
                                           "entry_type", "signer", "occurred_at", "recorded_at", "target",
                                           "digest")},
                    "content": rec.get("content"), "interval": rec.get("interval")})  # fmt: skip
    return out


# -- the sealing schedule (LD-5-A6) -------------------------------------------------------------

#: The PULSE action a sealing entry fires: the list of periods due, for a person to close.
SEAL_ACTION = "attest.seal_due"


def seal_cadences(site: str, book: Any) -> list[Any]:
    """The PULSE cadences that seal ``book`` at ``site``: one daily cron per start minute, at
    each clock period's end plus its grace, in the site's zone. Empty when the log declares no
    cadence (a seal boundary that is an interval or a range): it is sealed by hand."""
    from axiom.extensions.builtins.schedule.api import Cadence

    from . import periods

    if book.seal is None or book.seal.boundary not in book.periods:
        return []
    decl = book.periods[book.seal.boundary]
    zone = periods.site_zone(site)
    grace = periods.grace(site, decl)
    by_minute: dict[int, list[int]] = {}
    for hh, mm in periods._starts(site, decl):  # noqa: SLF001 - the period's own start times
        due = datetime(2000, 1, 1, hh, mm) + grace
        by_minute.setdefault(due.minute, []).append(due.hour)
    return [Cadence(kind="cron", cron=f"{m} {','.join(str(h) for h in sorted(set(hs)))} * * *",
                    tz=getattr(zone, "key", str(zone))) for m, hs in sorted(by_minute.items())]  # fmt: skip


def _schedule_name(site: str, logbook: str, cron: str) -> str:
    return f"Seal due: {logbook} at {site} @ {cron}"


def schedule_seals(site: str, logbooks: Iterable[str] | None = None) -> dict[str, Any]:
    """Reconcile PULSE with the declared cadences: register what is missing, cancel what no log
    declares any more. Idempotent."""
    from axiom.extensions.builtins.schedule import api as pulse
    from axiom.extensions.builtins.schedule import store as pulse_store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    books = [registry.get(x) for x in logbooks] if logbooks is not None else list(registry.all_logbooks())
    wanted: dict[str, tuple[str, Any]] = {}
    for book in books:
        for c in seal_cadences(site, book):
            wanted[_schedule_name(site, book.id, c.cron)] = (book.id, c)
    prefix = "Seal due: "
    with pulse_store.session_scope() as s:
        have = {r.name: r.id for r in s.query(ScheduleDefinition)
                .filter(ScheduleDefinition.action == SEAL_ACTION, ScheduleDefinition.state != "cancelled").all()
                if (r.name or "").startswith(prefix) and f" at {site} @ " in (r.name or "")}  # fmt: skip
    added, cancelled = [], []
    for name, (logbook, cadence) in sorted(wanted.items()):
        if name in have:
            continue
        pulse.register(envelope={"action": SEAL_ACTION, "params": {"site": site, "logbook": logbook}},
                       cadence=cadence, action=SEAL_ACTION, description=name, extension="attest",
                       retry_policy={"max_attempts": 1}, misfire_policy="fire_once")  # fmt: skip
        added.append(name)
    for name, sid in sorted(have.items()):
        if name not in wanted and (logbooks is None or any(f": {b} at " in name for b in logbooks)):
            pulse.cancel(pulse.ScheduleId(sid))
            cancelled.append(name)
    return {"site": site, "added": added, "cancelled": cancelled, "kept": sorted(set(have) & set(wanted))}


__all__ = [
    "CONTROLLED", "FORMAT", "KIND", "PURPOSE", "SEAL_ACTION", "SERVING", "ArchiveHooks", "ArchiveRefused",
    "Target", "build", "commit", "forget_source", "get", "gold_rows_from_package", "listed", "manifest",
    "note_source", "overlay", "overlay_gold", "package_digest", "plan", "read_package", "retire", "retired_ranges",
    "run_verifier", "schedule_seals", "seal_cadences", "serving_ranges", "set_target", "soak_check",
    "source_fields", "target", "verified", "verify",
]
