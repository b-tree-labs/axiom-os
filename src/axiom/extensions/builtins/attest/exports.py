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
- An export streams (#2182): :func:`write_package` reads the chain a batch at a time in one pass,
  spools every file to disk and hashes it on the way, sorts the text and CSV views by external
  merge sort, and writes the zip to a file, so memory does not grow with the log;
  :func:`stream_view` sends one view as it is read; :func:`download_chunks` streams a stored one.
- :func:`create` stores a package through ``axiom.infra.storage`` and records it in
  ``attest_exports`` (working state, not evidence). Nothing here writes a record.
"""

from __future__ import annotations

import csv
import hashlib
import heapq
import io
import json
import string
import tempfile
import uuid
import zipfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path
from typing import Any, BinaryIO

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
#: An export streams (#2182): the chain is read this many records at a time, a view that is sorted
#: holds this many records before it spills a sorted run to disk, and bytes move in chunks of this
#: size. None of them grows with the log.
_BATCH = 1000
_RUN = 2000
_CHUNK = 1 << 16
#: What a streamed view is served as.
MEDIA_TYPES = {"jsonl": "application/x-ndjson", "text": "text/plain; charset=utf-8",
               "csv": "text/csv; charset=utf-8"}  # fmt: skip

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


def _is_filter(key: str) -> bool:
    return key in _FILTER_KEYS or key == "where" or key.startswith("field.")


def parse_filter(raw: Mapping[str, Any], *, extra: Iterable[str] = (), logbook: Any = None) -> ExportFilter:
    """The export's filter from a caller's keys. ``extra`` names the caller's other parameters
    (site, logbook, formats, ...), which are not filters; any other unknown key is refused. The
    log's declared field filters (``field.<id>``, ``where``; ADR-268) need ``logbook``."""
    allowed = _FILTER_KEYS | set(extra)
    unknown = sorted(k for k, v in raw.items()
                     if k not in allowed and not _is_filter(k) and v not in (None, "", []))  # fmt: skip
    if unknown:
        raise ExportRefused(f"unknown filter {unknown}; filters are {sorted(_FILTER_KEYS)}")
    wanted = {k: v for k, v in raw.items() if _is_filter(k) and v not in (None, "", [])}
    include = _bool(wanted.pop("include_supplements", True))
    try:
        parsed = record_filters.parse_filters(wanted, logbook)
    except ValueError as exc:
        refused = ExportRefused(str(exc))
        refused.code = "filter"  # a malformed or undeclared filter: the CLI exits 2 (ADR-268 D3)
        raise refused from None
    shown = {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in wanted.items()}
    shown["include_supplements"] = include
    return ExportFilter(records=parsed, include_supplements=include,
                        raw=tuple(sorted(shown.items())))  # fmt: skip


# -- selection ------------------------------------------------------------------------------


def _head_seq(site: str, logbook: str) -> int:
    """The chain's last sequence number, where every streamed read of it stops; refused for an
    unknown log or one with no records here."""
    try:
        registry.get(logbook)
    except Exception as exc:  # noqa: BLE001 - an unknown logbook is a refusal with its reason
        raise ExportRefused(str(exc)) from None
    head = service.last_seq(site, logbook)
    if not head:
        raise ExportRefused(f"log {logbook!r} has no records at {site}")
    return head


def _chain(site: str, logbook: str) -> list[dict[str, Any]]:
    """The whole chain as a list, for a caller that renders from lists (tests, a printed form).
    The exports themselves never hold it: they read it a batch at a time (#2182)."""
    head = _head_seq(site, logbook)
    return service.records(site, logbook, to_seq=head)


def _matched_seqs(site: str, logbook: str, f: ExportFilter) -> Iterator[int]:
    """The sequence numbers ``f``'s record filters keep, ascending, read a keyset page at a time."""
    after = 0
    while True:
        page = f.records.apply(logbook, site, after_seq=after, limit=_BATCH)
        for r in page:
            yield int(r["seq"])
        if len(page) < _BATCH:
            return
        after = int(page[-1]["seq"])


class _Selection:
    """Which records of a chain ``f`` keeps, decided one record at a time in chain order, so the
    chain is read once and never held (#2182). An action record follows its target, at any depth
    (a supplement of a supplement); with ``include_supplements`` false a supplement and what acts
    on it leave. Only the records something acts on (``targeted``) are remembered as it passes."""

    def __init__(self, f: ExportFilter, matched: Iterator[int] | None, targeted: set[str]) -> None:
        self.f = f
        self.targeted = targeted
        self._matched = matched
        self._next: int | None = None
        self._kept: set[str] = set()
        self._dropped: set[str] = set()

    @classmethod
    def of(cls, site: str, logbook: str, f: ExportFilter, *, head: int) -> _Selection:
        matched = _matched_seqs(site, logbook, f) if f.records.any() else None
        return cls(f, matched, service.targeted(site, logbook, to_seq=head))

    def _is_matched(self, seq: int) -> bool:
        while self._next is None or self._next < seq:
            nxt = next(self._matched, None)  # type: ignore[arg-type]
            if nxt is None:
                self._next = None
                return False
            self._next = nxt
        return self._next == seq

    def keeps(self, r: Mapping[str, Any]) -> bool:
        rid, target = r["attestation_id"], r.get("target")
        kept = self._matched is None or self._is_matched(int(r["seq"])) or target in self._kept
        dropped = not self.f.include_supplements and (
            r.get("kind") == "supplement" or target in self._dropped
        )
        if rid in self.targeted:
            if kept:
                self._kept.add(rid)
            if dropped:
                self._dropped.add(rid)
        return kept and not dropped


def select(site: str, logbook: str, f: ExportFilter, *, chain: list[dict[str, Any]] | None = None
           ) -> list[dict[str, Any]]:  # fmt: skip
    """The records ``f`` selects, in chain order, each action record under a kept target."""
    if chain is None:
        head = _head_seq(site, logbook)
        sel = _Selection.of(site, logbook, f, head=head)
        return [r for r in service.iter_records(site, logbook, to_seq=head, batch=_BATCH) if sel.keeps(r)]
    targets = {r["target"] for r in chain if r.get("target")}
    sel = _Selection(f, _matched_seqs(site, logbook, f) if f.records.any() else None, targets)
    return [r for r in chain if sel.keeps(r)]


@dataclass
class _Walked:
    """What one pass over the chain saw: its length, its head, how many records were selected
    and, when asked, the verification from genesis."""

    chain: int = 0
    selected: int = 0
    head: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


def _verification_of(result: tuple) -> dict[str, Any]:
    ok, checked, last_seq, last_digest, bad_seq, reason = result
    return {"ok": ok, "checked": checked, "from_seq": 1, "head_seq": last_seq,
            "head_digest": last_digest, "first_bad_seq": bad_seq, "reason": reason}  # fmt: skip


def _walk(site: str, logbook: str, f: ExportFilter, *, head: int, sel: _Selection | None = None,
          on_chain: Callable[[dict[str, Any], str | None], None] | None = None,
          on_picked: Callable[[dict[str, Any], str | None], None] | None = None,
          verify: bool = False, lines: bool = False) -> _Walked:  # fmt: skip
    """One pass over the chain, a batch at a time: each record goes to ``on_chain``, each selected
    one to ``on_picked`` (with its JSON line when ``lines``), and the verifier reads the same pass.
    Nothing is kept here but counts and the head."""
    from axiom.attest.tools.verify import verify_records

    sel = sel or _Selection.of(site, logbook, f, head=head)
    out = _Walked()

    def fed() -> Iterator[dict[str, Any]]:
        for r in service.iter_records(site, logbook, to_seq=head, batch=_BATCH):
            line = _line(r) if lines else None
            out.chain += 1
            out.head = r
            if on_chain is not None:
                on_chain(r, line)
            if sel.keeps(r):
                out.selected += 1
                if on_picked is not None:
                    on_picked(r, line)
            yield r

    records = fed()
    if verify:
        out.verification = _verification_of(verify_records(records, signing.public_keys()))
    for _ in records:  # the verifier stops at a broken record; the views still read to the head
        pass
    return out


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

    return _verification_of(verify_records(chain, signing.public_keys()))


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


def _record_lines(r: Mapping[str, Any], depth: int) -> list[str]:
    """One record in the text view, indented ``depth`` levels under what it acts on."""
    pad = "    " * depth
    kind = r.get("kind") or "statement"
    what = r.get("entry_type") if kind == "statement" else kind
    head = f"{pad}#{r['seq']} {r.get('occurred_at') or r.get('recorded_at') or ''}  {what}  {r.get('meaning') or ''}"
    out = [f"{head}  {_signer(r)}".rstrip(), f"{pad}    digest {r['digest']}"]
    content = r.get("content") or {}
    if r.get("reason"):
        out.append(f"{pad}    reason: {r['reason']}")
    if content.get("title"):
        out.append(f"{pad}    {content['title']}")
    if content.get("body"):
        out.append(f"{pad}    {content['body']}")
    for key in sorted(content.get("fields") or {}):
        out.append(f"{pad}    {key}: {_scalar(content['fields'][key])}")
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
        out.extend(_record_lines(r, depth))
        for child in children.get(r["attestation_id"], []):
            emit(child, depth + 1)

    for r in roots:
        emit(r, 0)
    return "\n".join(out)


@dataclass(frozen=True)
class _Whole:
    """What every view says about the whole chain: its length, its head and its verification."""

    count: int
    head_text: str
    verification_text: str

    @classmethod
    def of(cls, chain: list[dict[str, Any]]) -> _Whole:
        return cls(len(chain), _head_text(chain), _verification_text(_verification(chain)))

    @classmethod
    def walked(cls, w: _Walked) -> _Whole:
        assert w.head is not None and w.verification is not None
        return cls(w.chain, f"#{w.head['seq']} {w.head['digest']}", _verification_text(w.verification))


def _parts(site: str, book: Logbook, records: list[dict[str, Any]], whole: _Whole,
           header: str | None = None) -> dict[str, str]:  # fmt: skip
    blank = not whole.count and not records
    return {
        "header": header or book.display,
        "rows": _text_rows(records),
        "footer": _footer(site, book, len(records), whole),
        "chain_head": whole.head_text,
        "verification": "blank form, no records to verify" if blank else whole.verification_text,
        "page": "1",
    }


def _footer(site: str, book: Logbook, selected: int, whole: _Whole) -> str:
    return f"{selected} of {whole.count} records of {book.display} ({book.id}) at {site}"


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
    return _text_view(site, book, records, _Whole.of(chain), f)


def _text_title(site: str, book: Logbook, f: ExportFilter | None) -> list[str]:
    lines = [f"{book.display} ({book.id}) at {site}"]
    if f is not None:
        lines.append("Filter: " + json.dumps(f.as_json(), sort_keys=True, ensure_ascii=False))
    return lines


def _text_view(site: str, book: Logbook, records: list[dict[str, Any]], whole: _Whole,
               f: ExportFilter | None) -> bytes:  # fmt: skip
    p = _parts(site, book, records, whole)
    lines = _text_title(site, book, f)
    lines += ["", p["rows"], "", p["footer"], f"Chain head: {p['chain_head']}",
              f"Verification: {p['verification']}"]  # fmt: skip
    return ("\n".join(lines) + "\n").encode("utf-8")


def _text_head(site: str, book: Logbook, f: ExportFilter | None) -> str:
    """The text view up to its first row: what a streamed text view sends before it has read."""
    return "".join(line + "\n" for line in _text_title(site, book, f)) + "\n"


def _text_tail(site: str, book: Logbook, selected: int, whole: _Whole) -> str:
    return (f"\n\n{_footer(site, book, selected, whole)}\nChain head: {whole.head_text}\n"
            f"Verification: {whole.verification_text}\n")  # fmt: skip


def _text_body(rows: Iterable[tuple[list[Any], dict[str, Any]]]) -> Iterator[str]:
    """The rows of the text view from records sorted by their tree key (see :func:`_text_key`):
    the same lines :func:`_text_rows` writes, one record at a time."""
    first = True
    for key, r in rows:
        for line in _record_lines(r, len(key) - 2):
            yield line if first else "\n" + line
            first = False


def _text_key(r: Mapping[str, Any], tree: dict[str, list[Any]], targeted: set[str]) -> list[Any]:
    """A record's place in the text view as one sortable key: an entry not under another selected
    record sorts by when it happened, then sequence (#1804); a record acting on a selected one
    sorts right after it, its siblings by sequence. ``[time, seq]`` for an entry, its target's key
    plus its own ``seq`` below it, so the key's length is its depth plus two. Only the keys of
    records something acts on are remembered (``tree``)."""
    target = r.get("target")
    if target in tree:
        key = [*tree[target], int(r["seq"])]
    else:
        key = [str(r.get("occurred_at") or r.get("recorded_at") or ""), int(r["seq"])]
    if r["attestation_id"] in targeted:
        tree[r["attestation_id"]] = key
    return key


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
    for r in sorted(records, key=_csv_key):
        fields = _csv_fields(r)
        columns |= set(fields)
        rows.append({**_csv_fixed(r), **fields})
    return "".join(_csv_pieces(columns, rows)).encode("utf-8")


def _csv_key(r: Mapping[str, Any]) -> list[Any]:
    return [str(r.get("occurred_at") or ""), int(r["seq"])]


def _csv_fixed(r: Mapping[str, Any]) -> dict[str, str]:
    return {
        "seq": str(r["seq"]), "attestation_id": r["attestation_id"],
        "kind": r.get("kind") or "statement", "target": r.get("target") or "",
        "entry_type": r.get("entry_type") or "", "meaning": r.get("meaning") or "",
        "occurred_at": r.get("occurred_at") or "", "signer": _signer(r), "digest": r["digest"],
    }  # fmt: skip


def _csv_fields(r: Mapping[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value in sorted(((r.get("content") or {}).get("fields") or {}).items()):
        _flatten(key, value, fields)
    return fields


def _csv_pieces(columns: set[str], rows: Iterable[Mapping[str, str]]) -> Iterator[str]:
    """The CSV view, a row at a time: the header (the fixed columns, then every field column the
    selection holds, sorted), then each row."""
    header = list(_CSV_FIXED) + sorted(columns - set(_CSV_FIXED))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow({k: row.get(k, "") for k in header})
        if buf.tell() >= _CHUNK:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


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


def _whole(chain: list[dict[str, Any]] | _Whole) -> _Whole:
    return chain if isinstance(chain, _Whole) else _Whole.of(chain)


def render_template(site: str, book: Logbook, export_id: str, records: list[dict[str, Any]], *,
                    chain: list[dict[str, Any]] | _Whole) -> bytes:  # fmt: skip
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
    whole = _parts(site, book, records, _whole(chain), header=header)
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
                        chain: list[dict[str, Any]] | _Whole) -> bytes:  # fmt: skip
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
    """A package's manifest and counts; ``data`` holds its bytes only when it was built in memory
    (:func:`build_package`). :func:`write_package` and :func:`create` never hold them (#2182)."""

    data: bytes | None
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


class _Bytes:
    """A small file of the package, held as bytes."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.size = len(data)
        self.sha256 = hashlib.sha256(data).hexdigest()

    def chunks(self) -> Iterator[bytes]:
        yield self.data


class _Spooled:
    """A file of the package written to disk as it is made, hashed on the way: never in memory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: BinaryIO | None = path.open("wb")
        self._hash = hashlib.sha256()
        self.size = 0
        self.sha256 = ""

    def write(self, text_: str) -> None:
        data = text_.encode("utf-8")
        self._hash.update(data)
        self.size += len(data)
        assert self._fh is not None
        self._fh.write(data)

    def close(self) -> _Spooled:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self.sha256 = self._hash.hexdigest()
        return self

    def chunks(self) -> Iterator[bytes]:
        with self.path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                yield chunk


class _Sorted:
    """Records sorted by a key without holding them all: :data:`_RUN` at a time are sorted and
    spilled to a run file, and reading merges the runs (an external merge sort)."""

    def __init__(self, tmp: Path, name: str) -> None:
        self.tmp, self.name = tmp, name
        self._buf: list[tuple[list[Any], dict[str, Any]]] = []
        self._runs: list[Path] = []

    def add(self, key: list[Any], r: dict[str, Any]) -> None:
        self._buf.append((key, r))
        if len(self._buf) >= _RUN:
            self._spill()

    def _spill(self) -> None:
        self._buf.sort(key=itemgetter(0))
        path = self.tmp / f"{self.name}-{len(self._runs)}.run"
        with path.open("w", encoding="utf-8") as fh:
            for item in self._buf:
                fh.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._runs.append(path)
        self._buf = []

    @staticmethod
    def _read(path: Path) -> Iterator[tuple[list[Any], dict[str, Any]]]:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                key, r = json.loads(line)
                yield key, r

    def __iter__(self) -> Iterator[tuple[list[Any], dict[str, Any]]]:
        self._buf.sort(key=itemgetter(0))
        return heapq.merge(*(self._read(p) for p in self._runs), iter(self._buf), key=itemgetter(0))


class _Views:
    """The views of one selection, fed one record at a time by :func:`_walk`: the JSON Lines view
    as it comes, and the text and CSV views into sorted runs, rendered once the pass is over."""

    def __init__(self, tmp: Path, fmts: Iterable[str], targeted: set[str]) -> None:
        fmts = set(fmts)
        self.tmp, self.targeted = tmp, targeted
        self.jsonl = _Spooled(tmp / "view.jsonl") if "jsonl" in fmts else None
        self.text = _Sorted(tmp, "text") if "text" in fmts else None
        self.csv = _Sorted(tmp, "csv") if "csv" in fmts else None
        self.columns: set[str] = set()
        self._tree: dict[str, list[Any]] = {}

    def __call__(self, r: dict[str, Any], line: str | None) -> None:
        if self.jsonl is not None:
            self.jsonl.write((line if line is not None else _line(r)) + "\n")
        if self.text is not None:
            self.text.add(_text_key(r, self._tree, self.targeted), r)
        if self.csv is not None:
            self.columns |= set(_csv_fields(r))
            self.csv.add(_csv_key(r), r)

    def text_pieces(self, site: str, book: Logbook, selected: int, whole: _Whole,
                    f: ExportFilter | None) -> Iterator[str]:  # fmt: skip
        assert self.text is not None
        yield _text_head(site, book, f)
        yield from _text_body(self.text)
        yield _text_tail(site, book, selected, whole)

    def csv_pieces(self) -> Iterator[str]:
        assert self.csv is not None
        return _csv_pieces(self.columns, ({**_csv_fixed(r), **_csv_fields(r)} for _, r in self.csv))


def _spool(path: Path, pieces: Iterable[str]) -> _Spooled:
    out = _Spooled(path)
    for piece in pieces:
        out.write(piece)
    return out.close()


def write_package(site: str, logbook: str, f: ExportFilter, *, formats: Iterable[str],
                  templates: Iterable[str] = (), out: BinaryIO) -> Package:  # fmt: skip
    """Write the evidence package to ``out`` (a seekable binary file), streaming (#2182): the chain
    is read a batch at a time and every file of the package is spooled to a temporary directory
    and hashed as it is written, so memory does not grow with the log. The bytes are the bytes a
    whole build in memory gives; the same selection always gives the same bytes. A declared form
    is a printed document and is still filled in memory."""
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
    head_seq = _head_seq(site, logbook)
    book = registry.get(logbook)
    sel = _Selection.of(site, logbook, f, head=head_seq)
    with tempfile.TemporaryDirectory(prefix="axiom-export-") as tmp_name:
        tmp = Path(tmp_name)
        chain_file = _Spooled(tmp / "chain.jsonl")
        views = _Views(tmp, fmts, sel.targeted)
        picked: list[dict[str, Any]] = []  # only for a declared form, which prints whole

        def on_picked(r: dict[str, Any], line: str | None) -> None:
            views(r, line)
            if templates:
                picked.append(r)

        walked = _walk(site, logbook, f, head=head_seq, sel=sel,
                       on_chain=lambda _r, line: chain_file.write(f"{line}\n"),
                       on_picked=on_picked, verify=True, lines=True)  # fmt: skip
        chain_file.close()
        if not walked.selected:
            raise ExportRefused("nothing matches this selection; no package was written")
        whole = _Whole.walked(walked)
        files: dict[str, _Bytes | _Spooled] = {"chain.jsonl": chain_file}
        for fmt in fmts:
            if fmt == "jsonl":
                assert views.jsonl is not None
                files[VIEW_NAMES[fmt]] = views.jsonl.close()
            elif fmt == "text":
                files[VIEW_NAMES[fmt]] = _spool(
                    tmp / "view.txt", views.text_pieces(site, book, walked.selected, whole, f)
                )
            else:
                files[VIEW_NAMES[fmt]] = _spool(tmp / "view.csv", views.csv_pieces())
        for export_id in templates:
            name = template_filename(site, book, export_id)
            files[name] = _Bytes(render_template(site, book, export_id, picked, chain=whole))
            if pdf:
                files[Path(name).stem + ".pdf"] = _Bytes(
                    render_template_pdf(site, book, export_id, picked, chain=whole)
                )
        keys = signing.public_keys()
        files["keys.json"] = _Bytes(json.dumps(
            {k: base64.b64encode(v).decode("ascii") for k, v in sorted(keys.items())}, indent=2
        ).encode("utf-8"))  # fmt: skip
        files["verify.py"] = _Bytes(Path(standalone.__file__).read_bytes())
        anchors, anchor_result = _anchors(site)
        verification = {"chain": walked.verification, "anchor": anchor_result,
                        "note": "the standalone verify.py checks the chain; the anchor result is the node's"}  # fmt: skip
        files["verification.json"] = _Bytes(
            json.dumps(verification, indent=2, sort_keys=True).encode("utf-8")
        )
        files["anchors.json"] = _Bytes(
            json.dumps(anchors, indent=2, sort_keys=True, default=str).encode("utf-8")
        )
        head = walked.head
        assert head is not None
        manifest = {
            "site_id": site,
            "logbook": logbook,
            "logbook_version": book.version,
            "filter": f.as_json(),
            "formats": [*fmts, *(["pdf"] if pdf else [])],
            "forms": list(templates),
            "counts": {"chain": walked.chain, "selected": walked.selected},
            "head": {"seq": head["seq"], "digest": head["digest"]},
            "files": {name: blob.sha256 for name, blob in files.items()},
            "generator": {"name": "axiom attest export", "version": _version()},
            "verify": "python verify.py chain.jsonl --keys keys.json",
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        # The manifest goes first and names every other file's hash, so the package is assembled
        # once they are all spooled; each entry is copied in a chunk at a time.
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name, blob in [("manifest.json", _Bytes(manifest_bytes)), *files.items()]:
                info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                info.file_size = blob.size
                with z.open(info, "w") as dest:
                    for chunk in blob.chunks():
                        dest.write(chunk)
    return Package(data=None, manifest=manifest,
                   manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), selected=walked.selected)  # fmt: skip


def build_package(site: str, logbook: str, f: ExportFilter, *, formats: Iterable[str],
                  templates: Iterable[str] = ()) -> Package:  # fmt: skip
    """The evidence package as bytes in memory; the same selection always gives the same bytes.
    A stored export never comes this way: :func:`create` writes the package to a file."""
    buf = io.BytesIO()
    pkg = write_package(site, logbook, f, formats=formats, templates=templates, out=buf)
    return Package(data=buf.getvalue(), manifest=pkg.manifest, manifest_sha256=pkg.manifest_sha256,
                   selected=pkg.selected)  # fmt: skip


def stream_view(site: str, logbook: str, f: ExportFilter, *, fmt: str = "jsonl") -> Iterator[bytes]:
    """One view of a selection as a stream of byte chunks (#2182), the same bytes as that view in
    the package. JSON Lines is in chain order, so its first bytes leave with the first batch read;
    text sends its title at once and its rows once they are sorted; CSV sends its header, which
    names every field column, after one pass. Refusals (format, log) are raised here, before the
    first byte; a selection that matches nothing is an empty view."""
    (fmt,) = _check_formats([fmt])
    head_seq = _head_seq(site, logbook)
    book = registry.get(logbook)
    if fmt == "jsonl":
        return _stream_jsonl(site, logbook, f, head_seq)
    return _stream_sorted(site, logbook, book, f, head_seq, fmt)


def _chunked(pieces: Iterable[str]) -> Iterator[bytes]:
    buf: list[str] = []
    size = 0
    for piece in pieces:
        buf.append(piece)
        size += len(piece)
        if size >= _CHUNK:
            yield "".join(buf).encode("utf-8")
            buf, size = [], 0
    if buf:
        yield "".join(buf).encode("utf-8")


def _stream_jsonl(site: str, logbook: str, f: ExportFilter, head: int) -> Iterator[bytes]:
    sel = _Selection.of(site, logbook, f, head=head)
    records = service.iter_records(site, logbook, to_seq=head, batch=_BATCH)
    yield from _chunked(_line(r) + "\n" for r in records if sel.keeps(r))


def _stream_sorted(site: str, logbook: str, book: Logbook, f: ExportFilter, head: int,
                   fmt: str) -> Iterator[bytes]:  # fmt: skip
    if fmt == "text":
        yield _text_head(site, book, f).encode("utf-8")
    sel = _Selection.of(site, logbook, f, head=head)
    with tempfile.TemporaryDirectory(prefix="axiom-export-") as tmp_name:
        views = _Views(Path(tmp_name), (fmt,), sel.targeted)
        walked = _walk(site, logbook, f, head=head, sel=sel, on_picked=views, verify=fmt == "text")
        if fmt == "text":
            pieces = views.text_pieces(site, book, walked.selected, _Whole.walked(walked), f)
            next(pieces)  # the title, sent already
            yield from _chunked(pieces)
        else:
            yield from _chunked(views.csv_pieces())


def preview(site: str, logbook: str, f: ExportFilter, *, fmt: str = "text") -> dict[str, Any]:
    """At most :data:`PREVIEW_LIMIT` selected records, rendered in memory; the chain is read a
    batch at a time and only the last of the selection kept. Writes nothing."""
    (fmt,) = _check_formats([fmt])
    head = _head_seq(site, logbook)
    limit = max(1, min(PREVIEW_LIMIT, PREVIEW_MAX))
    kept: deque[dict[str, Any]] = deque(maxlen=limit)
    walked = _walk(site, logbook, f, head=head, on_picked=lambda r, _line: kept.append(r),
                   verify=fmt == "text")  # fmt: skip
    shown = list(kept)
    if fmt == "jsonl":
        body = render_jsonl(shown)
    elif fmt == "csv":
        body = render_csv(shown)
    else:
        body = _text_view(site, registry.get(logbook), shown, _Whole.walked(walked), f)
    return {"site_id": site, "logbook": logbook, "format": fmt, "records": len(shown),
            "selected": walked.selected, "truncated": walked.selected > len(shown),
            "text": body.decode("utf-8")}  # fmt: skip


def counted(site: str, logbook: str, f: ExportFilter) -> dict[str, Any]:
    """What a package of this selection would hold, counted and not rendered (Axiom #1970). Writes
    nothing. ``selected`` and ``supplements`` add up to the package's ``counts.selected``;
    ``total`` is the log's entries at the site, supplements not counted."""
    n = {"supplements": 0, "total": 0}

    def on_chain(r: dict[str, Any], _line: str | None) -> None:
        if r.get("kind") != "supplement":
            n["total"] += 1

    def on_picked(r: dict[str, Any], _line: str | None) -> None:
        if r.get("kind") == "supplement":
            n["supplements"] += 1

    walked = _walk(site, logbook, f, head=_head_seq(site, logbook), on_chain=on_chain,
                   on_picked=on_picked)  # fmt: skip
    return {"site_id": site, "logbook": logbook, "selected": walked.selected - n["supplements"],
            "supplements": n["supplements"], "total": n["total"]}  # fmt: skip


# -- storage and the exports table ----------------------------------------------------------

_storage: Any = None
_storage_dir: Path | None = None


def set_storage(provider: Any = None, *, base_dir: Path | None = None) -> None:
    """The seam: a ``StorageProvider``, or None for the configured one (``attest-exports``;
    with none configured, the local filesystem under ``base_dir`` or the node's state dir)."""
    global _storage, _storage_dir
    _storage, _storage_dir = provider, base_dir


def storage() -> Any:
    if _storage is not None:
        return _storage
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.storage import configured

    base = _storage_dir or (Path(get_user_state_dir()) / "attest" / "exports")
    if _storage_dir is not None:
        # A directory named by the caller is the local filesystem, whatever is configured.
        from axiom.infra.storage import provider_from_config

        return provider_from_config({"base_dir": str(base)})
    return configured("attest-exports", default_dir=base)


def _row(e: AttestExport) -> dict[str, Any]:
    return {
        "export_id": e.export_id, "site_id": e.site_id, "logbook": e.logbook,
        "requested_by": e.requested_by, "filter": e.filter, "formats": e.formats,
        "counts": e.counts,
        "status": e.status, "manifest_sha256": e.manifest_sha256,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }  # fmt: skip


def create(site: str, logbook: str, f: ExportFilter, *, formats: Iterable[str],
           requested_by: str, templates: Iterable[str] = (),
           unpack_to: Path | None = None) -> tuple[dict[str, Any], Package]:  # fmt: skip
    """Build a package, store it, and record it; with ``unpack_to``, also write its files there.
    The package goes to a file as it is made, never whole into memory (#2182). Changes no signed
    record."""
    export_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{export_id}.zip"
        with path.open("wb") as fh:
            pkg = write_package(site, logbook, f, formats=formats, templates=templates, out=fh)
        up = storage().upload(path, destination=f"{site}/{logbook}/{export_id}.zip",
                              metadata={"content_type": "application/zip"})  # fmt: skip
        if unpack_to is not None and getattr(up, "success", True):
            unpack_to.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as z:
                z.extractall(unpack_to)
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
    chunks = download_chunks(export_id, site=site)
    return None if chunks is None else b"".join(chunks)


def download_chunks(export_id: str, *, site: str) -> Iterator[bytes] | None:
    """A stored package as a stream of chunks (#2182), or None when there is none at ``site``:
    the first bytes leave at once and memory does not grow with the package."""
    with store.session_scope() as s:
        e = s.get(AttestExport, export_id)
        if e is None or e.site_id != site or e.status != "ready":
            return None
        storage_id = e.storage_id
    tmp = tempfile.TemporaryDirectory(prefix="axiom-export-")
    try:
        path = storage().download(storage_id, Path(tmp.name) / "package.zip")
    except BaseException:
        tmp.cleanup()
        raise

    def chunks() -> Iterator[bytes]:
        try:
            with Path(path).open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    yield chunk
        finally:
            tmp.cleanup()

    return chunks()


__all__ = [
    "FORMATS",
    "MEDIA_TYPES",
    "PLACEHOLDERS",
    "PREVIEW_MAX",
    "ExportFilter",
    "ExportRefused",
    "Package",
    "build_package",
    "create",
    "download",
    "download_chunks",
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
    "stream_view",
    "template_filename",
    "write_package",
]
