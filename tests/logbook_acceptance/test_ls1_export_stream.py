# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LS-1 (#2182): the log export streams. First bytes under a second, memory bounded however many
entries the log holds, and the package the same bytes it always was.

- LB-LS1-EXP-A1: the evidence package of a selection is byte-identical to the package as it was
  built whole in memory (the reference below is that builder, kept here as the oracle), for every
  format, filter, a declared form, entries out of time order, nested supplements, and reads and
  sorts that span many batches;
- LB-LS1-EXP-A2: a streamed view of a 20,000-entry log yields its first bytes within the budget's
  second, and holds the same bytes as the view in the package;
- LB-LS1-EXP-A3: writing the package of a 20,000-entry log peaks at no more Python memory than the
  package of a 5,000-entry log, give or take a fixed allowance (bounded, not proportional);
- LB-LS1-EXP-A4: the stream is a door: one check, one read record, and a person who may not read
  the log is refused as at every other door (the LD-1 matrix drives the refusals).

The 100,000-entry numbers are the nightly scale job's (``test_w3_scale.py``).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import time
import tracemalloc
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from axiom.extensions.builtins.attest import (
    exports,
    registry,
    scale_fixture,
    service,
    signing,
    store,
)
from axiom.extensions.builtins.attest.logbooks import parse_logbook

from . import ls1_budgets
from .test_l6 import LOG, SITE, chain, export_log  # noqa: F401

# -- the oracle: the package as it was built whole in memory before #2182 ------------------------


def _reference_select(whole, f, site, logbook):
    if f.records.any():
        kept = {r["attestation_id"] for r in f.records.apply(logbook, site)}
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


def _reference_package(site, logbook, f, *, formats, templates=()):
    from axiom.attest.tools import verify as standalone

    whole = service.records(site, logbook)
    book = registry.get(logbook)
    picked = _reference_select(whole, f, site, logbook)
    files = {"chain.jsonl": exports.render_jsonl(whole)}
    for fmt in formats:
        if fmt == "jsonl":
            files["records.jsonl"] = exports.render_jsonl(picked)
        elif fmt == "text":
            files["records.txt"] = exports.render_text(site, book, picked, chain=whole, f=f)
        else:
            files["records.csv"] = exports.render_csv(picked)
    for export_id in templates:
        name = exports.template_filename(site, book, export_id)
        files[name] = exports.render_template(site, book, export_id, picked, chain=whole)
    keys = signing.public_keys()
    files["keys.json"] = json.dumps(
        {k: base64.b64encode(v).decode("ascii") for k, v in sorted(keys.items())}, indent=2
    ).encode("utf-8")
    files["verify.py"] = Path(standalone.__file__).read_bytes()
    anchors, anchor_result = exports._anchors(site)
    verification = {"chain": exports._verification(whole), "anchor": anchor_result,
                    "note": "the standalone verify.py checks the chain; the anchor result is the node's"}  # fmt: skip
    files["verification.json"] = json.dumps(verification, indent=2, sort_keys=True).encode("utf-8")
    files["anchors.json"] = json.dumps(anchors, indent=2, sort_keys=True, default=str).encode("utf-8")
    head = whole[-1]
    manifest = {
        "site_id": site, "logbook": logbook, "logbook_version": book.version,
        "filter": f.as_json(), "formats": list(formats), "forms": list(templates),
        "counts": {"chain": len(whole), "selected": len(picked)},
        "head": {"seq": head["seq"], "digest": head["digest"]},
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "generator": {"name": "axiom attest export", "version": exports._version()},
        "verify": "python verify.py chain.jsonl --keys keys.json",
    }  # fmt: skip
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in [("manifest.json", manifest_bytes), *files.items()]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return buf.getvalue()


# -- a log with everything the orderings have to get right ---------------------------------------

CRAFTED = "export_log"  # the L6 declaration: CHECK (a quantity and a checkbox) and NOTE


def _insert(s, seq, *, kind="statement", target=None, occurred=None, recorded="2025-03-01T00:00:00+00:00",
            fields=None, entry_type="NOTE", title=None):  # fmt: skip
    rid = str(uuid.UUID(int=seq + 7_000_000))
    digest = hashlib.sha256(f"crafted:{seq}".encode()).hexdigest()
    prev = "0" * 64 if seq == 1 else hashlib.sha256(f"crafted:{seq - 1}".encode()).hexdigest()
    rec = {"attestation_id": rid, "seq": seq, "logbook": CRAFTED, "site_id": SITE, "kind": kind,
           "meaning": "authored" if kind == "statement" else kind, "digest": digest,
           "prev_digest": prev, "signer": {"principal": f"@desk{seq % 3}:{SITE}", "display": "Désk"},
           "recorded_at": recorded,
           "content": {"title": title or f"entry {seq} ünïcode", "body": "line, with \"quotes\"",
                       "fields": fields or {}}}  # fmt: skip
    if kind == "statement":
        rec["entry_type"] = entry_type
    if target:
        rec["target"] = target
    if occurred:
        rec["occurred_at"] = occurred
    s.execute(text(
        "INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest,"
        " kind, meaning, entry_type, signer, occurred_at, recorded_at, record, target) VALUES"
        " (:id, :site, :log, :seq, :d, :p, :k, :m, :e, :sg, :oa, :ra, CAST(:rec AS JSONB), :t)"),
        {"id": rid, "site": SITE, "log": CRAFTED, "seq": seq, "d": digest, "p": prev, "k": kind,
         "m": rec["meaning"], "e": rec.get("entry_type"), "sg": rec["signer"]["principal"],
         "oa": occurred or recorded, "ra": recorded, "rec": json.dumps(rec), "t": target})  # fmt: skip
    return rid


@pytest.fixture
def crafted(export_log):  # noqa: F811
    """60 entries: times out of chain order, ties broken by sequence, entries with no
    ``occurred_at`` (the text view falls back to ``recorded_at``, CSV does not), supplements of
    supplements, acknowledgements, quantities, nested field maps and new columns late in the log."""
    with store.session_scope() as s:
        ids: dict[int, str] = {}
        for seq in range(1, 61):
            if seq % 7 == 3 and seq > 3:
                ids[seq] = _insert(s, seq, kind="supplement", target=ids[seq - 2],
                                   fields={"ok": False})  # fmt: skip
            elif seq % 11 == 5 and seq > 11:
                ids[seq] = _insert(s, seq, kind="supplement", target=ids[seq - 1])
            elif seq % 13 == 0:
                ids[seq] = _insert(s, seq, kind="acknowledgement", target=ids[seq - 6])
            elif seq % 9 == 0:
                ids[seq] = _insert(s, seq, occurred=None, recorded=f"2025-0{1 + seq % 5}-02T00:00:00+00:00")
            else:
                minute = (seq * 37) % 50  # out of chain order, with ties
                fields = {"weight": {"value": f"{seq}.5", "unit": "kg"}, "ok": seq % 2 == 0}
                if seq > 40:
                    fields["late_column"] = {"inner": {"a": seq}, "outer": {"b": "x"}}
                ids[seq] = _insert(s, seq, entry_type="CHECK" if seq % 2 else "NOTE",
                                   occurred=f"2025-02-01T00:{minute:02d}:00+00:00", fields=fields)  # fmt: skip
        s.commit()
    return ids


SELECTIONS = [{}, {"entry_type": "CHECK"}, {"entry_type": "CHECK", "include_supplements": False},
              {"include_supplements": False}, {"q": "entry"}]  # fmt: skip


@pytest.mark.acceptance("LB-LS1-EXP-A1")
@pytest.mark.parametrize("raw", SELECTIONS, ids=lambda r: json.dumps(r, sort_keys=True))
def test_lb_ls1_exp_a1_the_streamed_package_is_byte_identical_to_the_whole_one(crafted, raw, monkeypatch):
    # Small reads and small sorted runs, so every boundary of the streamed path is crossed.
    monkeypatch.setattr(exports, "_BATCH", 4)
    monkeypatch.setattr(exports, "_RUN", 5)
    f = exports.parse_filter(raw)
    want = _reference_package(SITE, CRAFTED, f, formats=("jsonl", "text", "csv"), templates=("sheet",))
    got = exports.build_package(SITE, CRAFTED, f, formats=("jsonl", "text", "csv"), templates=("sheet",))
    assert got.data == want
    with tempfile.TemporaryFile() as fh:
        pkg = exports.write_package(SITE, CRAFTED, f, formats=("jsonl", "text", "csv"),
                                    templates=("sheet",), out=fh)  # fmt: skip
        fh.seek(0)
        assert fh.read() == want
    assert pkg.manifest_sha256 == got.manifest_sha256 and pkg.selected == got.selected
    for fmt, name in exports.VIEW_NAMES.items():
        streamed = b"".join(exports.stream_view(SITE, CRAFTED, f, fmt=fmt))
        assert streamed == zipfile.ZipFile(io.BytesIO(want)).read(name), fmt


@pytest.mark.acceptance("LB-LS1-EXP-A1")
def test_lb_ls1_exp_a1_a_signed_chain_packages_as_before(chain):  # noqa: F811
    for raw in SELECTIONS:
        f = exports.parse_filter(raw)
        if not _reference_select(service.records(SITE, LOG), f, SITE, LOG):
            continue
        want = _reference_package(SITE, LOG, f, formats=("csv", "jsonl", "text"), templates=("sheet",))
        assert exports.build_package(SITE, LOG, f, formats=("csv", "jsonl", "text"),
                                     templates=("sheet",)).data == want, raw  # fmt: skip


@pytest.mark.acceptance("LB-LS1-EXP-A1")
def test_lb_ls1_exp_a1_preview_and_counts_read_as_before(crafted, monkeypatch):
    monkeypatch.setattr(exports, "_BATCH", 4)
    whole = service.records(SITE, CRAFTED)
    for raw in SELECTIONS:
        f = exports.parse_filter(raw)
        picked = _reference_select(whole, f, SITE, CRAFTED)
        assert [r["seq"] for r in exports.select(SITE, CRAFTED, f)] == [r["seq"] for r in picked]
        counted = exports.counted(SITE, CRAFTED, f)
        sups = sum(1 for r in picked if r.get("kind") == "supplement")
        assert counted["selected"] + counted["supplements"] == len(picked) and counted["supplements"] == sups
        for fmt in ("text", "jsonl", "csv"):
            got = exports.preview(SITE, CRAFTED, f, fmt=fmt)
            shown = picked[-exports.PREVIEW_LIMIT:]
            book = registry.get(CRAFTED)
            body = {"jsonl": lambda: exports.render_jsonl(shown), "csv": lambda: exports.render_csv(shown),
                    "text": lambda: exports.render_text(SITE, book, shown, chain=whole, f=f)}[fmt]()  # fmt: skip
            assert got["text"] == body.decode("utf-8") and got["selected"] == len(picked), (raw, fmt)


# -- first bytes and memory at a size a development machine can bear ----------------------------

SMALL, LARGE = 5_000, 20_000


def _scale_log(log_id: str, n: int) -> str:
    registry.register(parse_logbook(scale_fixture.declaration(log_id), source="ls1-export"))
    with store.session_scope() as s:
        scale_fixture.seed_records(s, site=SITE, n=n, logbooks=[log_id])
    return log_id


@pytest.fixture
def sized(attest_db, tmp_path):
    exports.set_storage(None, base_dir=tmp_path / "storage")
    yield {SMALL: _scale_log("scale_log_90", SMALL), LARGE: _scale_log("scale_log_91", LARGE)}
    exports.set_storage(None)


def _peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.acceptance("LB-LS1-EXP-A2")
def test_lb_ls1_exp_a2_a_streamed_view_yields_its_first_bytes_within_the_budget(sized):
    limit = ls1_budgets.BUDGETS["budget"]["export_100k_first_bytes"]["limit"]
    f = exports.parse_filter({})
    t0 = time.perf_counter()
    stream = exports.stream_view(SITE, sized[LARGE], f, fmt="jsonl")
    first = next(stream)
    ms = (time.perf_counter() - t0) * 1000
    assert first and json.loads(first.split(b"\n", 1)[0])["seq"] == 1
    assert ms < limit, f"first bytes of a {LARGE}-entry export after {ms:.0f} ms"
    lines = first.count(b"\n") + sum(chunk.count(b"\n") for chunk in stream)
    assert lines == LARGE


@pytest.mark.acceptance("LB-LS1-EXP-A3")
def test_lb_ls1_exp_a3_package_memory_is_bounded_not_proportional(sized):
    f = exports.parse_filter({})

    def write(log_id):
        with tempfile.TemporaryFile() as fh:
            pkg = exports.write_package(SITE, log_id, f, formats=("jsonl", "text", "csv"), out=fh)
        assert pkg.manifest["counts"]["chain"] in (SMALL, LARGE)

    write(sized[SMALL])  # warm imports and caches, so neither measurement pays for them
    small, large = _peak(lambda: write(sized[SMALL])), _peak(lambda: write(sized[LARGE]))
    # Four times the entries: a bounded export peaks at about the same; a whole one at four times.
    assert large < small * 1.5 + 2 * 2**20, f"{SMALL}: {small / 2**20:.1f} MiB, {LARGE}: {large / 2**20:.1f} MiB"
    assert large < 48 * 2**20, f"{large / 2**20:.1f} MiB"


@pytest.mark.acceptance("LB-LS1-EXP-A3")
def test_lb_ls1_exp_a3_a_stored_export_never_holds_the_package_in_memory(sized):
    f = exports.parse_filter({})
    exports.create(SITE, sized[SMALL], f, formats=("jsonl",), requested_by="@op1:site-a")  # warm
    small = _peak(lambda: exports.create(SITE, sized[SMALL], f, formats=("jsonl",), requested_by="@op1:site-a"))
    large = _peak(lambda: exports.create(SITE, sized[LARGE], f, formats=("jsonl",), requested_by="@op1:site-a"))
    assert large < small * 1.5 + 2 * 2**20, f"{small / 2**20:.1f} MiB vs {large / 2**20:.1f} MiB"


# -- the stream is a door -----------------------------------------------------------------------


@pytest.mark.acceptance("LB-LS1-EXP-A4")
def test_lb_ls1_exp_a4_the_stream_door_serves_the_view_and_records_one_read(chain, client, tmp_path,  # noqa: F811
                                                                             monkeypatch):  # fmt: skip
    from axiom.extensions.builtins.attest import reads
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_

    seen = []
    real = reads.record_access
    monkeypatch.setattr(reads, "record_access", lambda **kw: (seen.append(kw), real(**kw))[1])
    as_(client, OP1)
    r = client.get(f"/api/v1/attest/{LOG}/exports/stream?format=jsonl&entry_type=CHECK")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson")
    f = exports.parse_filter({"entry_type": "CHECK"})
    assert r.content == exports.render_jsonl(_reference_select(service.records(SITE, LOG), f, SITE, LOG))
    assert [s["what"] for s in seen] == ["export"] and seen[0]["params"]["stream"] is True
    text_view = client.get(f"/api/v1/attest/{LOG}/exports/stream?format=text")
    assert text_view.status_code == 200 and text_view.text.startswith("Export Log (export_log)")
    bad = client.get(f"/api/v1/attest/{LOG}/exports/stream?format=docx")
    assert bad.status_code == 400 and bad.json()["code"] == "export"
