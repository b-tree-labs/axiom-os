# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-213 D11-D12: every item is filed and explained; user folders and moves teach it; previews are safe."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.book.place import Correction, Signals, UserFolder, place
from axiom.extensions.builtins.library.book.scheme import SchemeError, scheme_from_dict
from axiom.extensions.builtins.library.book.service import active_scheme
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.preview.conformance import assert_conformant, check
from axiom.extensions.builtins.library.preview.contract import (
    PreviewContext,
    RepresentationError,
    validate,
)
from axiom.extensions.builtins.library.preview.sanitize import clean_html
from axiom.extensions.builtins.library.preview.service import (
    CORE_PREVIEWERS,
    clear_previewers,
    register_previewer,
)
from axiom.extensions.builtins.library.tests.test_library_upload import ALICE, ALICE_COLLEAGUE, _up

API = "/api/v1/library"
PROSE = "\n\nThis paragraph is long enough that the ingestion guard treats it as prose and indexes it for retrieval.\n"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    contrib.reset(rediscover=False)
    registry.clear_local()
    clear_previewers()
    yield
    clear_previewers()
    registry.clear_local()
    contrib.reset()


# ------------------------------------------------------------------ the scheme


WIDGET_BOOK = {
    "scheme": {"name": "widgets", "display_name": "Widget Book", "version": "1.0.0"},
    "categories": [
        {"path": "logs", "label": "Logs"},
        {"path": "logs/by-line", "label": "By line"},
        {"path": "manuals", "label": "Manuals"},
    ],
    "rules": [
        {"category": "logs/by-line/{metadata.line}", "confidence": 0.85, "because": "it is a widget log from line {line}",
         "when": {"ext": [".wlog"]}},
        {"category": "manuals", "confidence": 0.5, "because": "it reads like a manual", "when": {"text_any": ["(?i)operating manual"]}},
    ],
}


def test_rules_template_paths_and_explain_themselves():
    scheme = scheme_from_dict(WIDGET_BOOK)
    p = place(Signals(name="a.wlog", metadata={"line": "B7"}), scheme)
    assert p.path == "logs/by-line/b7" and p.source == "rule" and not p.needs_look
    assert p.explanation == "Filed under Logs / By line / B7 because it is a widget log from line B7."
    # A template value missing: the static parent, less sure.
    p = place(Signals(name="b.wlog"), scheme)
    assert p.path == "logs/by-line" and p.confidence < 0.85
    # Weak evidence: filed, but marked for a look. No evidence: Unsorted.
    p = place(Signals(name="m.pdf", text="Operating Manual v2"), scheme)
    assert p.path == "manuals" and p.needs_look
    p = place(Signals(name="x.bin"), scheme)
    assert p.path == "unsorted" and p.needs_look and p.labels == ["Unsorted, needs a look"]


def test_schemes_refuse_rules_that_file_nowhere():
    bad = json.loads(json.dumps(WIDGET_BOOK))
    bad["rules"][0]["category"] = "nowhere/{metadata.line}"
    with pytest.raises(SchemeError, match="no category declares"):
        scheme_from_dict(bad)
    bad = json.loads(json.dumps(WIDGET_BOOK))
    bad["rules"][0]["because"] = ""
    with pytest.raises(SchemeError, match="says why"):
        scheme_from_dict(bad)


def test_a_user_folder_outranks_the_domain_category_when_it_fits_and_corrections_win():
    scheme = scheme_from_dict(WIDGET_BOOK)
    log = Signals(name="pump-calibration.wlog", metadata={"line": "B7"}, extractors=["pack.extract.wlog"])
    folder = UserFolder(path="my-folders/calibration", label="Calibration",
                        member_signatures=[{"x:pack.extract.wlog", "ext:.wlog"}])
    p = place(log, scheme, user_folders=[folder])
    assert p.path == "my-folders/calibration" and p.source == "user-folder" and "calibration" in p.because
    moved = Correction(signature=log.signature(), to_path="manuals")
    p = place(log, scheme, user_folders=[folder], corrections=[moved])
    assert p.path == "manuals" and p.source == "correction" and "you moved 1 similar item" in p.because


def test_a_folder_of_items_just_like_it_outranks_a_rule_without_a_name_match():
    scheme = scheme_from_dict(WIDGET_BOOK)
    log = Signals(name="x.wlog", metadata={"line": "B7"}, extractors=["pack.extract.wlog"])
    folder = UserFolder(path="my-folders/mine", label="Mine", member_signatures=[log.signature()])
    p = place(log, scheme, user_folders=[folder])
    assert p.path == "my-folders/mine" and p.source == "user-folder"


def test_the_active_scheme_is_the_product_one_when_installed(tmp_path):
    assert active_scheme().name == "data" and active_scheme().display_name == "Data Book"
    (tmp_path / "book.toml").write_text(
        '[scheme]\nname = "widgets"\ndisplay_name = "Widget Book"\n\n[[categories]]\npath = "logs"\nlabel = "Logs"\n'
    )
    (tmp_path / "axiom-extension.toml").write_text('[extension]\nname = "widgets"\n\n[[book_schemes]]\nfile = "book.toml"\n')
    contrib.add_manifest(tmp_path / "axiom-extension.toml")
    assert active_scheme().display_name == "Widget Book" and active_scheme().source == "widgets"


# ------------------------------------------------------------- the Book, live


def test_every_upload_is_filed_and_the_tree_counts_them(client, library):
    _up(client, "plan.md", ("# Plan" + PROSE).encode())
    _up(client, "runs.csv", b"t,v\n1,2\n2,3\n3,5\n")
    _up(client, "tool.py", b"def f():\n    return 1\n")
    _up(client, "cfg.json", b'{"a": 1}')
    book = client.get(f"{API}/book", headers=ALICE).json()
    assert book["scheme"]["display_name"] == "Data Book"
    counts = {f["path"]: f["count"] for f in book["folders"]}
    assert counts["documents"] == 1 and counts["code"] == 1 and counts["structured"] == 1
    assert counts["datasets/series"] == 1 and {f["path"]: f["total"] for f in book["folders"]}["datasets"] == 1
    items = client.get(f"{API}/items?folder=datasets&below=1", headers=ALICE).json()["items"]
    assert [i["file_name"] for i in items] == ["runs.csv"]


def test_a_user_folder_catches_later_similar_uploads_and_a_move_teaches_it(client, library):
    first = _up(client, "pump-calibration-2026.md", ("# Pump calibration" + PROSE).encode()).json()["items"][0]
    assert first["folder"] == "documents"
    folder = client.post(f"{API}/book/folders", json={"label": "Calibration"}, headers=ALICE).json()
    assert folder["path"] == "my-folders/calibration"
    moved = client.post(f"{API}/items/{first['id']}/move", json={"folder": folder["path"]}, headers=ALICE).json()
    assert moved["folder"] == "my-folders/calibration" and moved["placement"]["source"] == "manual"
    later = _up(client, "valve-calibration-2026.md", ("# Valve calibration" + PROSE).encode(), headers=ALICE_COLLEAGUE).json()["items"][0]
    assert later["folder"] == "my-folders/calibration", later["placement"]
    assert later["placement"]["source"] in ("correction", "user-folder")
    assert "Calibration" in later["placement"]["explanation"]
    # Renaming the folder relabels it; nothing is re-filed.
    client.patch(f"{API}/book/folders/my-folders/calibration", json={"label": "Calibrations"}, headers=ALICE)
    tree = client.get(f"{API}/book", headers=ALICE).json()
    f = next(x for x in tree["folders"] if x["path"] == "my-folders/calibration")
    assert f["label"] == "Calibrations" and f["count"] == 2
    # A folder holding items is not deleted out from under them.
    assert client.delete(f"{API}/book/folders/my-folders/calibration", headers=ALICE).status_code == 422
    # A scheme folder can be renamed and shown elsewhere, never deleted.
    r = client.patch(f"{API}/book/folders/code", json={"label": "Scripts", "parent": "documents"}, headers=ALICE)
    assert r.status_code == 200
    code = next(x for x in client.get(f"{API}/book", headers=ALICE).json()["folders"] if x["path"] == "code")
    assert code["label"] == "Scripts" and code["parent"] == "documents"
    assert client.delete(f"{API}/book/folders/code", headers=ALICE).status_code == 404


def test_uploading_into_a_folder_files_it_there(client, library):
    folder = client.post(f"{API}/book/folders", json={"label": "Inbox"}, headers=ALICE).json()
    item = _up(client, "x.md", ("# X" + PROSE).encode(), folder=folder["path"]).json()["items"][0]
    assert item["folder"] == folder["path"] and "uploaded into this folder" in item["placement"]["explanation"]


def test_a_reextraction_never_undoes_a_move(client, library):
    item = _up(client, "a.md", ("# A" + PROSE).encode()).json()["items"][0]
    client.post(f"{API}/items/{item['id']}/move", json={"folder": "code"}, headers=ALICE)
    client.post(f"{API}/items/{item['id']}/reextract", json={}, headers=ALICE)
    assert client.get(f"{API}/items/{item['id']}", headers=ALICE).json()["folder"] == "code"


# --------------------------------------------------------------------- previews


def _preview(client, item):
    r = client.get(f"{API}/items/{item['id']}/preview", headers=ALICE)
    assert r.status_code == 200, r.text
    return r.json()


def test_core_previews_for_common_types(client, library):
    import importlib.util

    from PIL import Image

    md = _up(client, "a.md", ("# A" + PROSE).encode()).json()["items"][0]
    assert _preview(client, md)["type"] == "text" and _preview(client, md)["language"] == "markdown"
    csv = _up(client, "t.csv", b"t,v\n1,2\n2,3\n").json()["items"][0]
    rep = _preview(client, csv)
    assert rep["type"] == "composite" and [p["title"] for p in rep["parts"]] == ["Chart", "Table"]
    assert rep["parts"][1]["preview"]["columns"][0] == {"name": "t", "type": "int"}
    people = _up(client, "p.csv", b"name,role\nana,lead\n").json()["items"][0]
    assert _preview(client, people)["type"] == "table"
    js = _up(client, "c.json", b'{"pump": {"rpm": 1200}, "tags": [1, 2]}').json()["items"][0]
    rep = _preview(client, js)
    assert rep["type"] == "tree" and rep["nodes"][0]["children"][0]["name"] == "pump"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    img = _up(client, "i.png", buf.getvalue()).json()["items"][0]
    rep = _preview(client, img)
    assert rep == {**rep, "type": "media", "media": "image", "src": "content", "width": 8}
    if importlib.util.find_spec("h5py") is not None:  # optional extractor dependency
        import h5py
        import numpy as np

        h5 = library["root"].parent / "s.h5"
        with h5py.File(h5, "w") as f:
            f.create_dataset("g/x", data=np.arange(5))
        item = _up(client, "s.h5", h5.read_bytes()).json()["items"][0]
        rep = _preview(client, item)
        assert rep["type"] == "tree" and rep["previewer"] == "core.preview.extracted"
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("a.txt", "x")
    item = _up(client, "z.zip", zbuf.getvalue()).json()["items"][0]
    assert _preview(client, item)["type"] == "listing"


def test_nothing_can_show_it_says_so_with_identity_and_a_way_to_contribute(client, library):
    item = _up(client, "blob.gz", b"\x1f\x8b\x08\x00binary").json()["items"][0]
    rep = _preview(client, item)
    assert rep["type"] == "none" and rep["identity"]["sha256"] == item["sha256"]
    assert rep["contribute"]["guide"].endswith("build-a-previewer.md")


def test_a_contributed_previewer_wins_and_an_invalid_one_falls_through(client, library):
    calls = []

    def bad(ctx):
        calls.append("bad")
        return {"type": "media", "media": "image", "src": "https://example.invalid/x.png", "alt": "x"}

    def good(ctx):
        calls.append("good")
        return {"type": "summary", "fields": [{"label": "Lines", "value": ctx.read_text().count("\n")}]}

    register_previewer({"name": "pack.preview.bad", "version": "1.0.0", "summary": "bad", "produces": ["media"],
                        "priority": 99, "match": {"extensions": [".md"]}}, bad)
    register_previewer({"name": "pack.preview.lines", "version": "1.0.0", "summary": "lines", "produces": ["summary"],
                        "match": {"extensions": [".md"]}}, good)
    item = _up(client, "a.md", ("# A" + PROSE).encode()).json()["items"][0]
    rep = _preview(client, item)
    assert rep["previewer"] == "pack.preview.lines" and calls == ["bad", "good"]


def test_representations_are_validated_for_safety_access_and_theme():
    validate({"type": "table", "columns": [{"name": "a"}], "rows": [[1]]})
    for rep, needle in [
        ({"type": "table", "columns": [{"name": ""}], "rows": []}, "column needs a name"),
        ({"type": "chart", "title": "t", "x": {"label": "x"}, "y": {}, "series": [{"name": "s"}]}, "y axis needs a label"),
        ({"type": "media", "media": "image", "alt": ""}, "alt text"),
        ({"type": "html", "html": "<p onclick=x>hi</p>", "title": "t"}, "script"),
        ({"type": "summary", "fields": [{"label": "a", "value": 1, "color": "#ff0000"}]}, "raw colour"),
    ]:
        with pytest.raises(RepresentationError, match=needle):
            validate(rep)
    validate({"type": "summary", "fields": [{"label": "a", "value": 1, "color": "var(--accent)"}]})


def test_html_is_sanitized():
    dirty = '<p onclick="x()">Hi <script>alert(1)</script><a href="javascript:alert(1)">l</a><img src="https://x/y.png"><b>ok</b></p>'
    clean = clean_html(dirty)
    assert "script" not in clean and "onclick" not in clean and "javascript" not in clean and "https://x" not in clean
    assert "<b>ok</b>" in clean and "alert(1)" not in clean


def test_core_previewers_pass_the_conformance_suite(tmp_path):
    (tmp_path / "a.md").write_text("# A\n\nsome text\n")
    (tmp_path / "c.json").write_text('{"a": {"b": [1, 2]}}')
    md = PreviewContext(path=tmp_path / "a.md", file_name="a.md", size=12, sha256="0" * 64, mime="text/markdown")
    js = PreviewContext(path=tmp_path / "c.json", file_name="c.json", size=20, sha256="0" * 64, mime="application/json")
    by = {m.name: (m, fn) for m, fn in CORE_PREVIEWERS}
    assert_conformant(by["core.preview.text"][1], by["core.preview.text"][0], [md], tmp_dir=tmp_path)
    assert_conformant(by["core.preview.tree"][1], by["core.preview.tree"][0], [js], tmp_dir=tmp_path)


def test_the_conformance_suite_catches_network_slowness_and_unsafe_output(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    ctx = PreviewContext(path=tmp_path / "a.txt", file_name="a.txt", size=1, sha256="0" * 64, mime="text/plain")
    m = {"name": "pack.preview.x", "version": "1.0.0", "summary": "x", "produces": ["html"], "match": {"extensions": [".txt"]},
         "limits": {"timeout_s": 0.3}}

    def phones_home(c):
        import socket

        socket.create_connection(("example.invalid", 80))

    def slow(c):
        import time

        time.sleep(1)

    def unsafe(c):
        return {"type": "html", "title": "t", "html": "<script>x</script>"}

    assert "used the network" in check(phones_home, m, [ctx]).problems[0]
    assert "did not finish" in check(slow, m, [ctx]).problems[0]
    assert "script" in check(unsafe, m, [ctx]).problems[0]
