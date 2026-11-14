# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Export a log (unit L6): one selection, three renderings, declared forms, an evidence package.

- :func:`parse_filter` reads W3's record filters (:mod:`.filters`) plus ``include_supplements``,
  and refuses a key it does not know.
- :func:`select` filters the chain in one place for every format: the records the filters keep,
  in chain order, with every action record (supplement, acknowledgement, co-signature, seal) under
  a kept record; supplements leave when ``include_supplements`` is false.
- :func:`render_jsonl` writes each record exactly as chained (it re-hashes to its digest);
  :func:`render_text` is deterministic, with each record's sequence number and digest and its
  action records indented under it; :func:`render_csv` is one row per record, a stable header, a
  quantity as value and unit columns, and no value ever turned into a float.
- :func:`render_template` fills a logbook's ``[[export]]`` template with ``string.Template``
  (``${header}``, ``${rows}``, ``${footer}``, ``${chain_head}``, ``${verification}``, ``${page}``)
  and nothing else: no code runs, an unknown placeholder is refused, and the file must sit inside
  the logbook file's directory.
- :func:`build_package` is a zip with a fixed entry order and fixed timestamps, so two exports of
  one selection are byte-identical: ``manifest.json`` first (with the SHA-256 of every other
  file), ``chain.jsonl`` (the whole chain from sequence 1, so verification always runs from
  genesis), the requested views, ``keys.json``, ``verify.py`` (the standalone verifier),
  ``verification.json`` and ``anchors.json``.
- :func:`create` stores a package through ``axiom.infra.storage`` and records it in
  ``attest_exports`` (working state, not evidence). Nothing here writes a record.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import string
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select as sa_select

from . import filters as record_filters
from . import registry, service, signing, site_settings, store
from .db_models import AttestExport
from .logbooks import Logbook

FORMATS = ("jsonl", "text", "csv")
#: ``pdf`` prints each named form as a PDF too (#1805); it renders forms, not a view of records.
FORM_FORMATS = ("pdf",)
VIEW_NAMES = {"jsonl": "records.jsonl", "text": "records.txt", "csv": "records.csv"}
#: The preview's bound, and the most it may be asked for.
PREVIEW_MAX = 200
PREVIEW_LIMIT = PREVIEW_MAX
#: A zip entry's time: fixed, so the bytes do not depend on when the export ran.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
PLACEHOLDERS = ("header", "rows", "footer", "chain_head", "verification", "page")

#: Every key a caller may filter by: W3's spellings and the export's own.
_FILTER_KEYS = {
    "entry_type", "entry_types", "type", "types", "meaning", "meanings", "from", "since", "to",
    "until", "interval", "q", "text", "signer", "include_supplements",
}  # fmt: skip


class ExportRefused(ValueError):
    """An export cannot be made as asked; the message says why."""


@dataclass(frozen=True)
class ExportFilter:
    records: record_filters.RecordFilters = field(default_factory=record_filters.RecordFilters)
    include_supplements: bool = True
    #: The filter as the caller spelled it, for the manifest.
    raw: tuple[tuple[str, Any], ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.raw}


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def parse_filter(raw: Mapping[str, Any], *, extra: Iterable[str] = ()) -> ExportFilter:
    """The export's filter from a caller's keys. ``extra`` names the caller's other parameters
    (site, logbook, formats, ...), which are not filters; any other unknown key is refused."""
    allowed = _FILTER_KEYS | set(extra)
    unknown = sorted(k for k, v in raw.items() if k not in allowed and v not in (None, "", []))
    if unknown:
        raise ExportRefused(f"unknown filter {unknown}; filters are {sorted(_FILTER_KEYS)}")
    wanted = {k: v for k, v in raw.items() if k in _FILTER_KEYS and v not in (None, "", [])}
    include = _bool(wanted.pop("include_supplements", True))
    try:
        parsed = record_filters.parse_filters(wanted)
    except ValueError as exc:
        raise ExportRefused(str(exc)) from None
    shown = {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in wanted.items()}
    shown["include_supplements"] = include
    return ExportFilter(records=parsed, include_supplements=include,
                        raw=tuple(sorted(shown.items())))  # fmt: skip


# -- selection ------------------------------------------------------------------------------


def _chain(site: str, logbook: str) -> list[dict[str, Any]]:
    try:
        registry.get(logbook)
    except Exception as exc:  # noqa: BLE001 - an unknown logbook is a refusal with its reason
        raise ExportRefused(str(exc)) from None
    records = service.records(site, logbook)
    if not records:
        raise ExportRefused(f"log {logbook!r} has no records at {site}")
    return records


def select(site: str, logbook: str, f: ExportFilter, *, chain: list[dict[str, Any]] | None = None
           ) -> list[dict[str, Any]]:  # fmt: skip
    """The records ``f`` selects, in chain order, each action record under a kept target."""
    whole = chain if chain is not None else _chain(site, logbook)
    if f.records.any():
        kept = {r["attestation_id"] for r in f.records.apply(logbook, site)}
        # Action records follow their target, at any depth (a supplement of a supplement).
        for r in whole:
            if r.get("target") in kept:
                kept.add(r["attestation_id"])
    else:
        kept = {r["attestation_id"] for r in whole}
    if not f.include_supplements:
        dropped = {r["attestation_id"] for r in whole if r.get("kind") == "supplement"}
        for r in whole:
            if r.get("target") in dropped:
                dropped.add(r["attestation_id"])
        kept -= dropped
    return [r for r in whole if r["attestation_id"] in kept]


# -- renderings -----------------------------------------------------------------------------


def _line(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_jsonl(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Each record exactly as chained, one per line: re-hashing a line gives its digest."""
    return "".join(_line(r) + "\n" for r in records).encode("utf-8")


def _scalar(value: Any) -> str:
    """A value as text, exactly as signed: strings as they are, never through a float."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, Mapping) and "value" in value and "unit" in value:
        return f"{_scalar(value['value'])} {_scalar(value['unit'])}".strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _signer(r: Mapping[str, Any]) -> str:
    s = r.get("signer") or {}
    display, principal = s.get("display") or "", s.get("principal") or ""
    return f"{display} ({principal})" if display and principal else display or principal


def _verification(chain: list[dict[str, Any]]) -> dict[str, Any]:
    from axiom.attest.tools.verify import verify_records

    ok, checked, last_seq, last_digest, bad_seq, reason = verify_records(
        chain, signing.public_keys()
    )
    return {"ok": ok, "checked": checked, "from_seq": 1, "head_seq": last_seq,
            "head_digest": last_digest, "first_bad_seq": bad_seq, "reason": reason}  # fmt: skip


def _verification_text(v: Mapping[str, Any]) -> str:
    if v["ok"]:
        return f"ok ({v['checked']} records from #1)"
    return f"BROKEN at #{v['first_bad_seq']}: {v['reason']} ({v['checked']} verified before it)"


def _head_text(chain: list[dict[str, Any]]) -> str:
    if not chain:
        return "none (blank form)"
    head = chain[-1]
    return f"#{head['seq']} {head['digest']}"


def _time_order(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records a person reads, by when each entry happened (#1804): entries sorted by
    ``occurred_at`` then ``seq``, each followed by the records acting on it in chain order. An
    import committed after live entries is placed by its own time. The JSON Lines views stay in
    chain order: that is the verifiable form."""
    ids = {r["attestation_id"] for r in records}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for r in records:
        target = r.get("target")
        if target in ids:
            children.setdefault(target, []).append(r)
        else:
            roots.append(r)
    roots.sort(key=lambda r: (str(r.get("occurred_at") or r.get("recorded_at") or ""), int(r["seq"])))
    out: list[dict[str, Any]] = []

    def walk(r: dict[str, Any]) -> None:
        out.append(r)
        for child in children.get(r["attestation_id"], []):
            walk(child)

    for r in roots:
        walk(r)
    return out


def _text_rows(records: list[dict[str, Any]]) -> str:
    records = _time_order(records)
    ids = {r["attestation_id"] for r in records}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for r in records:
        target = r.get("target")
        if target in ids:
            children.setdefault(target, []).append(r)
        else:
            roots.append(r)
    out: list[str] = []

    def emit(r: dict[str, Any], depth: int) -> None:
        pad = "    " * depth
        kind = r.get("kind") or "statement"
        what = r.get("entry_type") if kind == "statement" else kind
        head = f"{pad}#{r['seq']} {r.get('occurred_at') or r.get('recorded_at') or ''}  {what}  {r.get('meaning') or ''}"
        out.append(f"{head}  {_signer(r)}".rstrip())
        out.append(f"{pad}    digest {r['digest']}")
        content = r.get("content") or {}
        if r.get("reason"):
            out.append(f"{pad}    reason: {r['reason']}")
        if content.get("title"):
            out.append(f"{pad}    {content['title']}")
        if content.get("body"):
            out.append(f"{pad}    {content['body']}")
        for key in sorted(content.get("fields") or {}):
            out.append(f"{pad}    {key}: {_scalar(content['fields'][key])}")
        for child in children.get(r["attestation_id"], []):
            emit(child, depth + 1)

    for r in roots:
        emit(r, 0)
    return "\n".join(out)


def _parts(site: str, book: Logbook, records: list[dict[str, Any]], chain: list[dict[str, Any]],
           header: str | None = None) -> dict[str, str]:  # fmt: skip
    blank = not chain and not records
    return {
        "header": header or book.display,
        "rows": _text_rows(records),
        "footer": f"{len(records)} of {len(chain)} records of {book.display} ({book.id}) at {site}",
        "chain_head": _head_text(chain),
        "verification": "blank form, no records to verify"
        if blank
        else _verification_text(_verification(chain)),
        "page": "1",
    }


def _pages(records: list[dict[str, Any]], per_page: int | None) -> list[list[dict[str, Any]]]:
    """The records in printed pages of ``per_page`` entries; an action stays on its target's page,
    under it. No records is one blank page."""
    if not records:
        return [[]]
    records = _time_order(records)
    if per_page is None:
        return [records]
    ids = {r["attestation_id"] for r in records}
    pages: list[list[dict[str, Any]]] = []
    page_of: dict[str, int] = {}
    roots = 0
    for r in records:
        target = r.get("target")
        if target in ids and target in page_of:
            pages[page_of[target]].append(r)
            page_of[r["attestation_id"]] = page_of[target]
            continue
        if roots % per_page == 0:
            pages.append([])
        roots += 1
        pages[-1].append(r)
        page_of[r["attestation_id"]] = len(pages) - 1
    return pages


def render_text(site: str, book: Logbook, records: list[dict[str, Any]], *,
                chain: list[dict[str, Any]], f: ExportFilter | None = None) -> bytes:  # fmt: skip
    """Deterministic plain text: a fixed field order and date format, each record's sequence
    number and digest, action records under their target, a footer with the chain head and the
    verification result."""
    p = _parts(site, book, records, chain)
    lines = [f"{book.display} ({book.id}) at {site}"]
    if f is not None:
        lines.append("Filter: " + json.dumps(f.as_json(), sort_keys=True, ensure_ascii=False))
    lines += ["", p["rows"], "", p["footer"], f"Chain head: {p['chain_head']}",
              f"Verification: {p['verification']}"]  # fmt: skip
    return ("\n".join(lines) + "\n").encode("utf-8")


_CSV_FIXED = ("seq", "attestation_id", "kind", "target", "entry_type", "meaning", "occurred_at",
              "signer", "digest")  # fmt: skip


def _flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    if isinstance(value, Mapping) and "value" in value and "unit" in value:
        out[prefix] = _scalar(value["value"])
        out[f"{prefix}.unit"] = _scalar(value["unit"])
    elif (
        isinstance(value, Mapping) and value and all(isinstance(v, Mapping) for v in value.values())
    ):
        for k in sorted(value):
            _flatten(f"{prefix}.{k}", value[k], out)
    else:
        out[prefix] = _scalar(value)


def render_csv(records: list[dict[str, Any]]) -> bytes:
    """One row per record; the header is the fixed columns then the union of field columns,
    sorted; a quantity is a value column and a ``.unit`` column; every value as signed."""
    rows: list[dict[str, str]] = []
    columns: set[str] = set()
    # A flat table: every row by when it happened, then by sequence (#1804).
    for r in sorted(records, key=lambda r: (str(r.get("occurred_at") or ""), int(r["seq"]))):
        row = {
            "seq": str(r["seq"]), "attestation_id": r["attestation_id"],
            "kind": r.get("kind") or "statement", "target": r.get("target") or "",
            "entry_type": r.get("entry_type") or "", "meaning": r.get("meaning") or "",
            "occurred_at": r.get("occurred_at") or "", "signer": _signer(r), "digest": r["digest"],
        }  # fmt: skip
        fields: dict[str, str] = {}
        for key, value in sorted(((r.get("content") or {}).get("fields") or {}).items()):
            _flatten(key, value, fields)
        columns |= set(fields)
        rows.append({**row, **fields})
    header = list(_CSV_FIXED) + sorted(columns - set(_CSV_FIXED))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow({k: row.get(k, "") for k in header})
    return buf.getvalue().encode("utf-8")


def _template_path(book: Logbook, template: str) -> Path:
    if not book.source:
        raise ExportRefused(f"log {book.id!r} was not loaded from a file; its template has no home")
    home = Path(book.source).resolve().parent
    path = (home / template).resolve()
    if home not in path.parents:
        raise ExportRefused(f"template {template!r} is outside the log's directory")
    if not path.is_file():
        raise ExportRefused(f"template {template!r} of log {book.id!r} does not exist")
    return path


def render_template(site: str, book: Logbook, export_id: str, records: list[dict[str, Any]], *,
                    chain: list[dict[str, Any]]) -> bytes:  # fmt: skip
    """A declared form, filled by ``string.Template`` with the six placeholders and nothing else."""
    decl = book.export(export_id)
    tmpl = string.Template(_template_path(book, decl.template).read_text(encoding="utf-8"))
    named = {m.group("named") or m.group("braced") for m in tmpl.pattern.finditer(tmpl.template)}
    unknown = sorted(n for n in named if n and n not in PLACEHOLDERS)
    if unknown:
        raise ExportRefused(
            f"template {decl.template!r} names {unknown}; a template may use only "
            + ", ".join(f"${{{p}}}" for p in PLACEHOLDERS)
        )
    header = None
    if "header" in decl.site_overrides:
        header = site_settings.value(site, f"export.{decl.id}.header", None)
    pages = _pages(records, decl.rows_per_page)
    # Verified once; every page carries the same head, verification and whole-selection footer.
    whole = _parts(site, book, records, chain, header=header)
    out: list[str] = []
    try:
        for n, page in enumerate(pages, 1):
            parts = {**whole, "rows": _text_rows(page), "page": f"{n} of {len(pages)}"}
            out.append(tmpl.substitute(parts))
    except (KeyError, ValueError) as exc:
        raise ExportRefused(f"template {decl.template!r} cannot be filled: {exc}") from None
    # A form feed between pages: what a printer and a print preview read as a new page.
    return "\f".join(out).encode("utf-8")


def render_template_pdf(site: str, book: Logbook, export_id: str, records: list[dict[str, Any]], *,
                        chain: list[dict[str, Any]]) -> bytes:  # fmt: skip
    """The declared form as a PDF, one page per form page, made on the server (#1805), so every
    surface prints the same document. Same selection, same bytes."""
    from axiom.infra.text_pdf import text_to_pdf

    text = render_template(site, book, export_id, records, chain=chain).decode("utf-8")
    return text_to_pdf(text.split("\f"), title=f"{book.display} {export_id}")


def render_blank_pdf(site: str, book: Logbook, export_id: str) -> bytes:
    """The declared form blank, as a PDF, for a backup binder (#1805)."""
    return render_template_pdf(site, book, export_id, [], chain=[])


def render_blank(site: str, book: Logbook, export_id: str) -> bytes:
    """The declared form with no records, for a backup binder (#1805): headers, a blank row area,
    and a footer that says it is blank. Never refused for being empty."""
    return render_template(site, book, export_id, [], chain=[])


def template_filename(site: str, book: Logbook, export_id: str) -> str:
    decl = book.export(export_id)
    name = None
    if "filename" in decl.site_overrides:
        name = site_settings.value(site, f"export.{decl.id}.filename", None)
    name = str(name or f"{decl.id}{Path(decl.template).suffix or '.txt'}")
    if "/" in name or "\\" in name or name.startswith("."):
        raise ExportRefused(f"export filename {name!r} must be a plain file name")
    return name


# -- the package ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    data: bytes
    manifest: dict[str, Any]
    manifest_sha256: str
    selected: int


def _anchors(site: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    rows = service.anchors(site)
    listed = [{**a, "created_at": a["created_at"].isoformat() if isinstance(a["created_at"], datetime)
               else a["created_at"]} for a in rows]  # fmt: skip
    if not rows:
        return listed, None
    latest = rows[-1]
    ok, reason = service.verify_site_anchor(latest["anchor_id"], signing.public_keys())
    return listed, {"anchor_id": latest["anchor_id"], "ok": ok, "reason": reason}


def _check_formats(formats: Iterable[str]) -> tuple[str, ...]:
    out = tuple(dict.fromkeys(formats))
    bad = [x for x in out if x not in FORMATS]
    if bad:
        raise ExportRefused(f"unknown format {bad}; formats are {list(FORMATS)}")
    return out


def _version() -> str:
    from importlib import metadata

    try:
        return metadata.version("axiom-os-lm")
    except metadata.PackageNotFoundError:
        return "unknown"


def build_package(site: str, logbook: str, f: ExportFilter, *, formats: Iterable[str],
                  templates: Iterable[str] = ()) -> Package:  # fmt: skip
    """The evidence package as bytes; the same selection always gives the same bytes."""
    import base64

    from axiom.attest.tools import verify as standalone

    asked = tuple(dict.fromkeys(formats))
    pdf = "pdf" in asked
    templates = tuple(templates)
    if pdf and not templates:
        raise ExportRefused("pdf prints a declared form: name the form to print")
    fmts = _check_formats(x for x in asked if x not in FORM_FORMATS)
    if not fmts and not templates:
        raise ExportRefused("name at least one format or form")
    whole = _chain(site, logbook)
    book = registry.get(logbook)
    picked = select(site, logbook, f, chain=whole)
    if not picked:
        raise ExportRefused("nothing matches this selection; no package was written")
    files: dict[str, bytes] = {"chain.jsonl": render_jsonl(whole)}
    for fmt in fmts:
        if fmt == "jsonl":
            files[VIEW_NAMES[fmt]] = render_jsonl(picked)
        elif fmt == "text":
            files[VIEW_NAMES[fmt]] = render_text(site, book, picked, chain=whole, f=f)
        else:
            files[VIEW_NAMES[fmt]] = render_csv(picked)
    for export_id in templates:
        name = template_filename(site, book, export_id)
        files[name] = render_template(site, book, export_id, picked, chain=whole)
        if pdf:
            files[Path(name).stem + ".pdf"] = render_template_pdf(site, book, export_id, picked,
                                                                   chain=whole)  # fmt: skip
    keys = signing.public_keys()
    files["keys.json"] = json.dumps(
        {k: base64.b64encode(v).decode("ascii") for k, v in sorted(keys.items())}, indent=2
    ).encode("utf-8")
    files["verify.py"] = Path(standalone.__file__).read_bytes()
    anchors, anchor_result = _anchors(site)
    verification = {"chain": _verification(whole), "anchor": anchor_result,
                    "note": "the standalone verify.py checks the chain; the anchor result is the node's"}  # fmt: skip
    files["verification.json"] = json.dumps(verification, indent=2, sort_keys=True).encode("utf-8")
    files["anchors.json"] = json.dumps(anchors, indent=2, sort_keys=True, default=str).encode(
        "utf-8"
    )
    head = whole[-1]
    manifest = {
        "site_id": site,
        "logbook": logbook,
        "logbook_version": book.version,
        "filter": f.as_json(),
        "formats": [*fmts, *(["pdf"] if pdf else [])],
        "forms": list(templates),
        "counts": {"chain": len(whole), "selected": len(picked)},
        "head": {"seq": head["seq"], "digest": head["digest"]},
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "generator": {"name": "axiom attest export", "version": _version()},
        "verify": "python verify.py chain.jsonl --keys keys.json",
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in [("manifest.json", manifest_bytes), *files.items()]:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return Package(data=buf.getvalue(), manifest=manifest,
                   manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), selected=len(picked))  # fmt: skip


def preview(site: str, logbook: str, f: ExportFilter, *, fmt: str = "text") -> dict[str, Any]:
    """At most :data:`PREVIEW_LIMIT` selected records, rendered in memory. Writes nothing."""
    (fmt,) = _check_formats([fmt])
    whole = _chain(site, logbook)
    picked = select(site, logbook, f, chain=whole)
    limit = max(1, min(PREVIEW_LIMIT, PREVIEW_MAX))
    shown = picked[-limit:]
    if fmt == "jsonl":
        body = render_jsonl(shown)
    elif fmt == "csv":
        body = render_csv(shown)
    else:
        body = render_text(site, registry.get(logbook), shown, chain=whole, f=f)
    return {"site_id": site, "logbook": logbook, "format": fmt, "records": len(shown),
            "selected": len(picked), "truncated": len(picked) > len(shown),
            "text": body.decode("utf-8")}  # fmt: skip


def counted(site: str, logbook: str, f: ExportFilter) -> dict[str, Any]:
    """What a package of this selection would hold, counted and not rendered (Axiom #1970). Writes
    nothing. ``selected`` and ``supplements`` add up to the package's ``counts.selected``;
    ``total`` is the log's entries at the site, supplements not counted."""
    whole = _chain(site, logbook)
    picked = select(site, logbook, f, chain=whole)
    supplements = sum(1 for r in picked if r.get("kind") == "supplement")
    return {"site_id": site, "logbook": logbook, "selected": len(picked) - supplements,
            "supplements": supplements,
            "total": sum(1 for r in whole if r.get("kind") != "supplement")}  # fmt: skip


# -- storage and the exports table ----------------------------------------------------------

_storage: Any = None
_storage_dir: Path | None = None


def set_storage(provider: Any = None, *, base_dir: Path | None = None) -> None:
    """The seam: a ``StorageProvider``, or None for the local provider (under ``base_dir``, or
    the node's state directory)."""
    global _storage, _storage_dir
    _storage, _storage_dir = provider, base_dir


def storage() -> Any:
    if _storage is not None:
        return _storage
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.storage.local import LocalStorageProvider

    base = _storage_dir or (Path(get_user_state_dir()) / "attest" / "exports")
    return LocalStorageProvider({"base_dir": str(base)})


def _row(e: AttestExport) -> dict[str, Any]:
    return {
        "export_id": e.export_id, "site_id": e.site_id, "logbook": e.logbook,
        "requested_by": e.requested_by, "filter": e.filter, "formats": e.formats,
        "counts": e.counts,
        "status": e.status, "manifest_sha256": e.manifest_sha256,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }  # fmt: skip


def create(site: str, logbook: str, f: ExportFilter, *, formats: Iterable[str],
           requested_by: str, templates: Iterable[str] = ()) -> tuple[dict[str, Any], Package]:  # fmt: skip
    """Build a package, store it, and record it. Changes no signed record."""
    import tempfile

    pkg = build_package(site, logbook, f, formats=formats, templates=templates)
    export_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{export_id}.zip"
        path.write_bytes(pkg.data)
        up = storage().upload(path, destination=f"{site}/{logbook}/{export_id}.zip",
                              metadata={"content_type": "application/zip"})  # fmt: skip
    status = "ready" if getattr(up, "success", True) else "failed"
    row = AttestExport(
        export_id=export_id, site_id=site, logbook=logbook, requested_by=requested_by,
        filter=f.as_json(), formats=[*pkg.manifest["formats"], *pkg.manifest["forms"]],
        counts=pkg.manifest["counts"],
        status=status, storage_id=up.storage_id, manifest_sha256=pkg.manifest_sha256,
        created_at=datetime.now(UTC),
    )  # fmt: skip
    with store.session_scope() as s:
        s.add(row)
        s.commit()
        out = _row(row)
    if status != "ready":
        raise ExportRefused(f"the package could not be stored: {getattr(up, 'error', '')}")
    from . import receipts

    # A receipt on the action ledger (#1814). The package stands either way; when the receipt
    # could not be written the result says so (#1992).
    out["receipt_error"] = receipts.packaged(out)
    return out, pkg


def get(export_id: str, *, site: str) -> dict[str, Any] | None:
    """One export's metadata, or None when it does not exist at ``site``."""
    with store.session_scope() as s:
        e = s.get(AttestExport, export_id)
        return _row(e) if e is not None and e.site_id == site else None


def listed(site: str, logbook: str, *, limit: int = 50) -> list[dict[str, Any]]:
    with store.session_scope() as s:
        rows = s.execute(
            sa_select(AttestExport)
            .where(AttestExport.site_id == site, AttestExport.logbook == logbook)
            .order_by(AttestExport.created_at.desc())
            .limit(max(1, min(int(limit), 200)))
        ).scalars()
        return [_row(e) for e in rows]


def download(export_id: str, *, site: str) -> bytes | None:
    import tempfile

    with store.session_scope() as s:
        e = s.get(AttestExport, export_id)
        if e is None or e.site_id != site or e.status != "ready":
            return None
        storage_id = e.storage_id
    with tempfile.TemporaryDirectory() as tmp:
        return storage().download(storage_id, Path(tmp) / "package.zip").read_bytes()


__all__ = [
    "FORMATS",
    "PLACEHOLDERS",
    "PREVIEW_MAX",
    "ExportFilter",
    "ExportRefused",
    "Package",
    "build_package",
    "create",
    "download",
    "get",
    "listed",
    "parse_filter",
    "preview",
    "render_csv",
    "render_jsonl",
    "render_template",
    "render_text",
    "select",
    "set_storage",
    "storage",
    "template_filename",
]
