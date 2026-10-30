# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-213: every core extractor on a tiny file, and the registry's fan-out, order and narrowing."""

from __future__ import annotations

import io
import json
import tarfile
import time
import zipfile

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.extract.contract import (
    ExtractionContext,
    ExtractionResult,
    ManifestError,
    NotYet,
    TextSection,
    manifest_from_dict,
)
from axiom.extensions.builtins.library.extract.runner import run


@pytest.fixture(autouse=True)
def _isolated_registry():
    contrib.reset(rediscover=False)
    registry.clear_local()
    yield
    registry.clear_local()
    contrib.reset()


def _ctx(tmp_path, name, data: bytes, mime="application/octet-stream"):
    p = tmp_path / name
    p.write_bytes(data)
    return ExtractionContext.for_path(p, mime=mime)


def _run(name: str, ctx):
    entry = registry.get(name)
    out = run(entry, ctx)
    assert out.status == "done", (out.status, out.error)
    return out.result


def _planned(ctx):
    return [p.name for p in registry.plan(ctx) if not p.skip]


# ----------------------------------------------------------------- each extractor


def test_text_markdown_and_code(tmp_path):
    md = _ctx(tmp_path, "notes.md", b"# Seal inspection\n\nQuarterly checks.\n\n## Bearings\n")
    r = _run("core.extract.text", md)
    assert r.metadata["language"] == "markdown" and r.metadata["title"] == "Seal inspection"
    assert r.metadata["headings"] == ["Seal inspection", "Bearings"] and r.text[0].text.startswith("# Seal")
    py = _ctx(tmp_path, "tool.py", b"def solve(x):\n    return x\n\nclass Grid:\n    pass\n")
    r = _run("core.extract.text", py)
    assert r.features["code_like"] is True and r.metadata["definitions"] == ["solve", "Grid"]


def test_json_yaml_and_xml(tmp_path):
    r = _run("core.extract.structured", _ctx(tmp_path, "cfg.json", json.dumps({"a": 1, "b": [1, 2, 3]}).encode()))
    assert r.metadata["keys"] == ["a", "b"] and r.features["structure"]["b"][1] == "… 3 items"
    r = _run("core.extract.structured", _ctx(tmp_path, "cfg.yaml", b"name: pump\nlimits:\n  max: 4\n"))
    assert r.metadata["format"] == "yaml" and r.metadata["keys"] == ["name", "limits"]
    r = _run("core.extract.xml", _ctx(tmp_path, "m.xml", b"<root><item/><item/><x>hi</x></root>"))
    assert r.metadata["root"] == "root" and r.metadata["elements"]["item"] == 2
    bomb = _ctx(tmp_path, "bomb.xml", b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa">]><l>&a;</l>')
    out = run(registry.get("core.extract.xml"), bomb)
    assert out.status == "failed" and "entit" in out.error


def test_pdf(tmp_path):
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": "Blank report"})
    buf = io.BytesIO()
    w.write(buf)
    r = _run("core.extract.pdf", _ctx(tmp_path, "r.pdf", buf.getvalue(), "application/pdf"))
    assert r.metadata["pages"] == 1 and r.metadata["title"] == "Blank report"
    assert r.warnings and "OCR" in r.warnings[0]


def test_office_documents(tmp_path):
    import docx
    import openpyxl
    import pptx

    d = docx.Document()
    d.add_heading("Procedure", level=1)
    d.add_paragraph("Close valve three before the test.")
    d.save(tmp_path / "p.docx")
    r = _run("core.extract.office", ExtractionContext.for_path(tmp_path / "p.docx"))
    assert r.metadata["headings"] == ["Procedure"] and "valve three" in r.text[0].text

    deck = pptx.Presentation()
    deck.slides.add_slide(deck.slide_layouts[1]).shapes.title.text = "Results"
    deck.save(tmp_path / "d.pptx")
    r = _run("core.extract.office", ExtractionContext.for_path(tmp_path / "d.pptx"))
    assert r.metadata["slides"] == 1

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "runs"
    ws.append(["run", "temp"])
    ws.append([1, 20.5])
    ws.append([2, 21.0])
    wb.create_sheet("notes").append(["note"])
    wb.save(tmp_path / "w.xlsx")
    r = _run("core.extract.workbook", ExtractionContext.for_path(tmp_path / "w.xlsx"))
    assert [t.name for t in r.tables] == ["runs", "notes"]
    assert r.tables[0].row_count == 2 and r.tables[0].types == {"run": "int", "temp": "float"}


def test_csv_series_becomes_a_chart(tmp_path):
    rows = ["time,temp,flow"] + [f"2026-01-01T00:{m:02d}:00Z,{20 + m * 0.1:.1f},{m}" for m in range(60)]
    r = _run("core.extract.delimited", _ctx(tmp_path, "run.csv", "\n".join(rows).encode(), "text/csv"))
    t = r.tables[0]
    assert t.row_count == 60 and t.types["temp"] == "float" and t.types["time"] == "datetime"
    assert r.features["series"] is True and r.preview["type"] == "chart"
    assert [s["name"] for s in r.preview["series"]] == ["temp", "flow"] and r.preview["x"]["scale"] == "time"
    r = _run("core.extract.delimited", _ctx(tmp_path, "people.tsv", b"name\trole\nana\tlead\n", "text/tab-separated-values"))
    assert r.metadata["delimiter"] == "\t" and r.preview is None


def test_parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    pq.write_table(pa.table({"ts": [1, 2, 3], "v": [0.1, 0.2, 0.3]}), tmp_path / "x.parquet")
    r = _run("core.extract.parquet", ExtractionContext.for_path(tmp_path / "x.parquet"))
    assert r.tables[0].row_count == 3 and r.tables[0].rows[0] == [1, 0.1] and r.tables[0].types["v"] == "double"


def test_hdf5_and_netcdf(tmp_path):
    h5py = pytest.importorskip("h5py")
    netCDF4 = pytest.importorskip("netCDF4")
    import numpy as np

    with h5py.File(tmp_path / "s.h5", "w") as f:
        f.attrs["title"] = "a run"
        g = f.create_group("results")
        ds = g.create_dataset("k", data=np.arange(100.0))
        ds.attrs["units"] = "1"
    r = _run("core.extract.hdf5", ExtractionContext.for_path(tmp_path / "s.h5"))
    assert r.metadata["datasets"] == 1 and r.features["dataset_paths"] == ["/results/k"]
    k = r.features["structure"]["children"][0]["children"][0]
    assert k["shape"] == [100] and k["peek"] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    assert r.preview["type"] == "tree"

    with netCDF4.Dataset(tmp_path / "c.nc", "w", format="NETCDF3_CLASSIC") as ds:
        ds.createDimension("t", 4)
        v = ds.createVariable("temp", "f4", ("t",))
        v[:] = [1, 2, 3, 4]
        ds.title = "classic"
    ctx = ExtractionContext.for_path(tmp_path / "c.nc")
    assert "core.extract.netcdf3" in _planned(ctx)
    r = _run("core.extract.netcdf3", ctx)
    assert r.metadata["dimensions"] == {"t": 4} and r.metadata["attributes"]["title"] == "classic"


def test_image_exif_keeps_camera_and_never_copies_location(tmp_path):
    from PIL import Image

    im = Image.new("RGB", (32, 16), "white")
    exif = im.getexif()
    exif[0x010F] = "ACME"  # Make
    exif[0x0110] = "Model 1"
    exif[0x8825] = {1: "N", 2: (30.0, 17.0, 0.0)}  # GPS IFD
    im.save(tmp_path / "i.jpg", exif=exif)
    r = _run("core.extract.image", ExtractionContext.for_path(tmp_path / "i.jpg"))
    assert r.metadata["width"] == 32 and r.metadata["exif"]["Make"] == "ACME"
    assert r.metadata["exif"].get("GPS", "").startswith("present")


def test_archive_lists_unpacks_and_refuses_unsafe_members(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data/a.csv", "x,y\n1,2\n")
        z.writestr("../evil.txt", "nope")
        z.writestr("readme.md", "# hi")
    r = _run("core.extract.archive", _ctx(tmp_path, "bundle.zip", buf.getvalue()))
    assert {e["path"] for e in r.preview["entries"]} == {"data/a.csv", "../evil.txt", "readme.md"}
    assert {c.member for c in r.children} == {"data/a.csv", "readme.md"}
    assert any("unsafe path" in w for w in r.warnings)

    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as t:
        data = b"hello"
        info = tarfile.TarInfo("docs/n.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    r = _run("core.extract.archive", _ctx(tmp_path, "b.tar.gz", tbuf.getvalue()))
    assert [c.member for c in r.children] == ["docs/n.txt"]


def test_archive_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_ARCHIVE_MEMBERS", "2")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for i in range(5):
            z.writestr(f"f{i}.txt", "x")
        z.writestr("zeros.txt", b"\0" * 2_000_000)
    r = _run("core.extract.archive", _ctx(tmp_path, "many.zip", buf.getvalue()))
    assert len(r.children) == 2 and any("only the first 2" in w for w in r.warnings)
    monkeypatch.setenv("AXIOM_LIBRARY_ARCHIVE_MEMBERS", "100")
    r = _run("core.extract.archive", _ctx(tmp_path, "bomb.zip", buf.getvalue()))
    assert any("zeros.txt" in w and "suspiciously" in w for w in r.warnings)
    deep = ExtractionContext.for_path(tmp_path / "bomb.zip", depth=3)
    r = _run("core.extract.archive", deep)
    assert r.children == [] and any("nested" in w for w in r.warnings)


# --------------------------------------------------------------------- registry


def _m(name, **kw):
    base = {"name": name, "version": "1.0.0", "summary": name, "outputs": ["metadata"], "match": {"extensions": [".wid"]}}
    base.update(kw)
    return manifest_from_dict(base, scope=base.pop("scope", "pack"))


def test_fan_out_order_supersedes_and_exclusive(tmp_path):
    ctx = _ctx(tmp_path, "a.wid", b"WIDGET v1\n")
    hit = lambda ctx: ExtractionResult(summary="x")  # noqa: E731
    registry.register(_m("pack.extract.low", priority=1), hit)
    registry.register(_m("pack.extract.high", priority=9), hit)
    registry.register(manifest_from_dict({"name": "kit.extract.mine", "version": "1.0.0", "summary": "k", "outputs": ["metadata"],
                                          "priority": 9, "match": {"sniff": ["^WIDGET v\\d"]}}, scope="kit"), hit)
    order = _planned(ctx)
    assert order.index("kit.extract.mine") < order.index("pack.extract.high") < order.index("pack.extract.low")
    assert order[-1] == "core.extract.file_facts"  # always, last

    registry.register(_m("pack.extract.boss", priority=20, supersedes=["pack.extract.low"]), hit)
    plans = {p.name: p.skip for p in registry.plan(ctx)}
    assert plans["pack.extract.low"] == "superseded by pack.extract.boss"

    registry.register(_m("pack.extract.only", priority=30, exclusive=True), hit)
    plans = {p.name: p.skip for p in registry.plan(ctx)}
    assert "exclusive" in plans["pack.extract.high"] and plans["core.extract.file_facts"] == ""


def test_size_limits_and_missing_dependencies_are_skips_not_failures(tmp_path):
    ctx = _ctx(tmp_path, "a.wid", b"0123456789")
    registry.register(_m("pack.extract.small", limits={"max_bytes": 5}), lambda c: ExtractionResult())
    registry.register(_m("pack.extract.needs", requires=["no_such_module_xyz"]), lambda c: ExtractionResult())
    plans = {p.name: p.skip for p in registry.plan(ctx)}
    assert plans["pack.extract.small"].startswith("file is larger")
    assert plans["pack.extract.needs"] == "unavailable: needs no_such_module_xyz"


def test_manifests_refuse_what_would_surprise():
    with pytest.raises(ManifestError, match="<scope>.extract"):
        _m("bad-name")
    with pytest.raises(ManifestError, match="tags"):
        _m("pack.extract.x", tags=["everywhere"])
    with pytest.raises(ManifestError, match="export-control"):
        _m("pack.extract.x", outputs=["text"], tags=["rag"], ec="none")
    with pytest.raises(ManifestError, match="signal"):
        _m("pack.extract.x", match={})


def test_a_manifest_contributes_extractors_and_types(tmp_path):
    (tmp_path / "axiom-extension.toml").write_text(
        """
[extension]
name = "widgets"

[[extractors]]
name = "widgets.extract.log"
version = "2.1.0"
summary = "Widget logs"
entry = "axiom.extensions.builtins.library.extract.core.text:plain_text"
outputs = ["metadata", "text"]
tags = ["rag"]
priority = 40
supersedes = ["core.extract.text"]
[extractors.match]
extensions = [".wlog"]
[[extractors.accepts]]
extension = ".wlog"
sniff = "text"
"""
    )
    contrib.add_manifest(tmp_path / "axiom-extension.toml")
    from axiom.extensions.builtins.library import limits

    assert limits.file_type_for("a.wlog").sniff == "text"
    ctx = _ctx(tmp_path, "a.wlog", b"line\n")
    names = _planned(ctx)
    assert names[0] == "widgets.extract.log"
    entry = registry.get("widgets.extract.log")
    assert entry.manifest.contributed_by == "widgets" and entry.manifest.scope == "pack"
    assert run(entry, ctx).status == "done"


# ----------------------------------------------------------------------- runner


def test_failures_timeouts_and_not_yet_are_outcomes(tmp_path):
    ctx = _ctx(tmp_path, "a.wid", b"x")

    def boom(c):
        raise ValueError("bad header")

    def slow(c):
        time.sleep(2)
        return ExtractionResult()

    def later(c):
        raise NotYet("not yet", tracking="docs/tracking.md")

    registry.register(_m("pack.extract.boom"), boom)
    registry.register(_m("pack.extract.slow", limits={"timeout_s": 0.2}), slow)
    registry.register(_m("pack.extract.later"), later)
    assert run(registry.get("pack.extract.boom"), ctx).status == "failed"
    out = run(registry.get("pack.extract.slow"), ctx)
    assert out.status == "timeout" and "0.2s" in out.error
    out = run(registry.get("pack.extract.later"), ctx)
    assert out.status == "not-yet" and "docs/tracking.md" in out.remedy


def test_a_stub_says_not_yet(tmp_path):
    registry.register(_m("pack.extract.future", status="stub"), lambda c: ExtractionResult())
    out = run(registry.get("pack.extract.future"), _ctx(tmp_path, "a.wid", b"x"))
    assert out.status == "not-yet"


def test_process_isolation_kills_at_the_timeout_and_returns_results(tmp_path):
    ctx = _ctx(tmp_path, "a.wid", b"hello")
    registry.register(_m("kit.extract.ok", isolation="process",
                         entry="axiom.extensions.builtins.library.extract.core.text:facts"), None)
    out = run(registry.get("kit.extract.ok"), ctx)
    assert out.status == "done" and out.result.metadata["size"] == 5
    registry.register(_m("kit.extract.hang", isolation="process", limits={"timeout_s": 1.0},
                         entry="axiom.extensions.builtins.library.tests.test_extractors:_hang"), None)
    t0 = time.monotonic()
    out = run(registry.get("kit.extract.hang"), ctx)
    assert out.status == "timeout" and time.monotonic() - t0 < 10


def _hang(ctx):  # used by the process-isolation test, in a child process
    time.sleep(60)
    return ExtractionResult(text=[TextSection("never")])
