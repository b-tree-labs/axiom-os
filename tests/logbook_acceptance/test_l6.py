# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit L6: export a log as JSON Lines, text and CSV, with filters, templates
and an evidence package that re-verifies without the platform.

Ticket: docs/working/logbook-units/L6.md in the product repo that holds the plan. The package
holds the whole chain from sequence 1 (so verification always runs from genesis) beside the
filtered views; two exports of one selection are byte-identical; a template is a
`string.Template` beside the logbook file and nothing more; nothing an export does changes a
record. The web half (LB-L6-A10) is tested in axiom-appkit.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from axiom.attest.canonical import digest
from axiom.extensions.builtins.attest import exports, logbooks, registry, service, store
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook
from axiom.extensions.builtins.attest.skills import SPECS
from axiom.extensions.builtins.attest.tests.test_signing import person
from axiom.extensions.builtins.attest.tests.test_supplement import _supplement_draft

SITE = "site-a"
LOG = "export_log"
ME = "@op1:site-a"
EXPORT_LOG = Path(__file__).parent / "logbooks" / "export_log.toml"


@pytest.fixture
def export_log(attest_db, tmp_path):
    registry.register_file(EXPORT_LOG)
    exports.set_storage(None, base_dir=tmp_path / "storage")
    yield registry.get(LOG)
    exports.set_storage(None)


def _sign(draft_id, signer, who=None):
    who = who or person(roles=("member",))
    p = service.present(draft_id)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


def check(signer, weight="31.20", ok=True):
    d = service.create_draft(
        site_id=SITE, logbook=LOG, entry_type="CHECK", meaning="performed",
        content={"title": "Check", "fields": {"weight": {"value": weight, "unit": "kg"}, "ok": ok}},
        origin="human", for_principal=ME,
    )  # fmt: skip
    return _sign(d, signer)


def note(signer, words="all quiet"):
    d = service.create_draft(
        site_id=SITE, logbook=LOG, entry_type="NOTE", meaning="authored",
        content={"title": "Note", "fields": {"text": words}}, origin="human", for_principal=ME,
    )  # fmt: skip
    return _sign(d, signer)


def supplement(signer, target):
    return _sign(_supplement_draft(target["attestation_id"], content={"fields": {"ok": False}}),
                 signer)  # fmt: skip


@pytest.fixture
def chain(export_log, bound_signer):
    a = check(bound_signer)
    b = note(bound_signer, "pump noted")
    s = supplement(bound_signer, a)
    c = check(bound_signer, weight="0.5", ok=False)
    return {"a": a, "b": b, "s": s, "c": c, "signer": bound_signer}


def select(**raw):
    return exports.select(SITE, LOG, exports.parse_filter(raw))


def seqs(records):
    return [r["seq"] for r in records]


# -- A4: the filters ------------------------------------------------------------------------


@pytest.mark.acceptance("LB-L6-A4")
def test_lb_l6_a4_each_filter_selects_and_they_combine(chain):
    a, b, s, c = chain["a"], chain["b"], chain["s"], chain["c"]
    assert seqs(select()) == [a["seq"], b["seq"], s["seq"], c["seq"]]
    # An entry type keeps its entries and, by default, the supplements under them.
    assert seqs(select(entry_type="CHECK")) == [a["seq"], s["seq"], c["seq"]]
    assert seqs(select(entry_type="CHECK", include_supplements=False)) == [a["seq"], c["seq"]]
    assert seqs(select(q="pump")) == [b["seq"]]
    assert seqs(select(signer=ME, entry_type="NOTE")) == [b["seq"]]
    assert seqs(select(signer="@nobody:site-a")) == []
    future = "2999-01-01T00:00:00Z"
    assert seqs(select(**{"from": future})) == []
    assert seqs(select(to=future, entry_type="NOTE")) == [b["seq"]]


@pytest.mark.acceptance("LB-L6-A4")
def test_lb_l6_a4_an_unknown_filter_key_is_refused(chain):
    with pytest.raises(ValueError, match="colour"):
        exports.parse_filter({"colour": "blue"})


# -- A1: JSON Lines -------------------------------------------------------------------------


@pytest.mark.acceptance("LB-L6-A1")
def test_lb_l6_a1_json_lines_hold_each_record_exactly_as_chained(chain):
    picked = select()
    lines = exports.render_jsonl(picked).decode("utf-8").splitlines()
    assert len(lines) == len(picked)
    for line, rec in zip(lines, picked, strict=True):
        loaded = json.loads(line)
        assert loaded == rec
        assert digest(loaded) == loaded["digest"]


# -- A2: text -------------------------------------------------------------------------------


@pytest.mark.acceptance("LB-L6-A2")
def test_lb_l6_a2_text_is_deterministic_and_places_a_supplement_under_its_target(chain):
    picked = select()
    one = exports.render_text(SITE, registry.get(LOG), picked, chain=service.records(SITE, LOG))
    two = exports.render_text(SITE, registry.get(LOG), picked, chain=service.records(SITE, LOG))
    assert one == two
    body = one.decode("utf-8")
    for rec in picked:
        assert f"#{rec['seq']}" in body and rec["digest"] in body
    a, s, b = chain["a"], chain["s"], chain["b"]
    a_at, s_at, b_at = (body.index(f"#{r['seq']} ") for r in (a, s, b))
    # The supplement sits under its target, before the next entry, and is indented.
    assert a_at < s_at < b_at
    s_line = next(line for line in body.splitlines() if line.lstrip().startswith(f"#{s['seq']} "))
    assert s_line.startswith("    ") and "supplement" in s_line
    assert "Chain head: #4" in body and "Verification: ok" in body


# -- A3: CSV --------------------------------------------------------------------------------


@pytest.mark.acceptance("LB-L6-A3")
def test_lb_l6_a3_csv_one_row_per_record_stable_header_value_and_unit_no_floats(chain):
    picked = select()
    raw = exports.render_csv(picked)
    assert raw == exports.render_csv(picked)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    assert len(rows) == len(picked)
    header = next(csv.reader(io.StringIO(raw.decode("utf-8"))))
    assert header[:8] == ["seq", "attestation_id", "kind", "target", "entry_type", "meaning",
                          "occurred_at", "signer"]  # fmt: skip
    assert {"weight", "weight.unit", "ok", "text"} <= set(header)
    first = rows[0]
    assert first["weight"] == "31.20" and first["weight.unit"] == "kg", "the digits as signed"
    assert rows[-1]["weight"] == "0.5"
    assert rows[2]["kind"] == "supplement" and rows[2]["target"] == chain["a"]["attestation_id"]


# -- A7: templates --------------------------------------------------------------------------


@pytest.mark.acceptance("LB-L6-A7")
def test_lb_l6_a7_a_template_renders_through_string_template(chain):
    book = registry.get(LOG)
    assert "export" not in book.not_yet_enforced
    assert [e.id for e in book.exports] == ["sheet"]
    out = exports.render_template(SITE, book, "sheet", select(), chain=service.records(SITE, LOG))
    body = out.decode("utf-8")
    assert body.startswith("Export Log")
    assert "Page 1" in body and f"#{chain['a']['seq']}" in body
    assert "Chain head: #4" in body and "Verification: ok" in body
    assert "${" not in body


@pytest.mark.acceptance("LB-L6-A7")
@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "templates/../../x.txt"])
def test_lb_l6_a7_a_template_outside_the_logbooks_directory_is_refused(path):
    data = logbooks.tomllib.loads(EXPORT_LOG.read_text(encoding="utf-8"))
    data["export"][0]["template"] = path
    with pytest.raises(LogbookError, match="template"):
        parse_logbook(data, source=str(EXPORT_LOG))


@pytest.mark.acceptance("LB-L6-A7")
def test_lb_l6_a7_a_template_construct_that_is_not_a_placeholder_is_refused(chain, tmp_path):
    bad = tmp_path / "book.toml"
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "sheet.txt").write_text("${header} ${__import__}", encoding="utf-8")
    bad.write_text(EXPORT_LOG.read_text(encoding="utf-8"), encoding="utf-8")
    book = logbooks.load_logbook(bad)
    with pytest.raises(exports.ExportRefused, match="__import__"):
        exports.render_template(SITE, book, "sheet", select(), chain=service.records(SITE, LOG))


# -- A5, A6: the package --------------------------------------------------------------------


def _unzip(blob: bytes, where: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(where)
        return {n: z.read(n) for n in z.namelist()}


@pytest.mark.acceptance("LB-L6-A5")
def test_lb_l6_a5_the_package_verifies_from_genesis_with_a_bare_interpreter(chain, tmp_path):
    f = exports.parse_filter({"entry_type": "NOTE"})
    pkg = exports.build_package(
        SITE, LOG, f, formats=("jsonl", "text", "csv"), templates=("sheet",)
    )
    files = _unzip(pkg.data, tmp_path)
    assert list(files)[0] == "manifest.json"
    assert {"chain.jsonl", "records.jsonl", "records.txt", "records.csv", "sheet.txt", "keys.json",
            "verify.py", "verification.json", "anchors.json"} <= set(files)  # fmt: skip
    manifest = json.loads(files["manifest.json"])
    import hashlib

    for name, sha in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == sha, name
    assert manifest["counts"] == {"chain": 4, "selected": 1}
    assert manifest["head"]["seq"] == 4
    chain_lines = files["chain.jsonl"].decode().splitlines()
    assert [json.loads(x)["seq"] for x in chain_lines] == [1, 2, 3, 4]
    assert json.loads(files["verification.json"])["chain"]["ok"] is True
    run = subprocess.run([sys.executable, "-I", "verify.py", "chain.jsonl", "--keys", "keys.json"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=120)  # fmt: skip
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.acceptance("LB-L6-A6")
def test_lb_l6_a6_two_exports_of_one_selection_are_byte_identical(chain):
    f = exports.parse_filter({"entry_type": "CHECK"})
    one = exports.build_package(SITE, LOG, f, formats=("jsonl", "text", "csv"))
    two = exports.build_package(SITE, LOG, f, formats=("jsonl", "text", "csv"))
    assert one.data == two.data and one.manifest_sha256 == two.manifest_sha256


# -- A8: refusals, and nothing changes ------------------------------------------------------


def _chain_rows():
    with store.session_scope() as s:
        return s.execute(
            text("SELECT seq, record FROM attest_records ORDER BY site_id, logbook, seq")
        ).all()


@pytest.mark.acceptance("LB-L6-A8")
def test_lb_l6_a8_refusals_name_their_reason_and_export_changes_no_record(chain):
    before = _chain_rows()
    with pytest.raises(exports.ExportRefused, match="nothing"):
        exports.build_package(
            SITE, LOG, exports.parse_filter({"q": "absent words"}), formats=("text",)
        )
    with pytest.raises(exports.ExportRefused, match="format"):
        exports.build_package(SITE, LOG, exports.parse_filter({}), formats=("docx",))
    # PDF prints a declared form (#1805), so it is refused with no form named.
    with pytest.raises(exports.ExportRefused, match="form"):
        exports.build_package(SITE, LOG, exports.parse_filter({}), formats=("pdf",))
    with pytest.raises(exports.ExportRefused, match="site-b"):
        exports.build_package("site-b", LOG, exports.parse_filter({}), formats=("text",))
    exports.create(SITE, LOG, exports.parse_filter({}), formats=("csv",), requested_by=ME)
    assert _chain_rows() == before


@pytest.mark.acceptance("LB-L6-A8")
def test_lb_l6_a8_a_caller_outside_the_site_gets_404_and_the_routes_work(chain, client, monkeypatch):
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_
    from axiom.webauth.users import User

    other = User(user_id="op3", email="op3@example.org", name="Op Three", roles=("user",),
                 site="site-b", attributes={"idp": "entra"})  # fmt: skip
    as_(client, OP1)
    r = client.post(
        f"/api/v1/attest/{LOG}/exports", json={"formats": ["text", "csv"], "entry_type": "CHECK"}
    )
    assert r.status_code == 200, r.text
    export_id = r.json()["export_id"]
    meta = client.get(f"/api/v1/attest/exports/{export_id}")
    assert meta.status_code == 200 and meta.json()["status"] == "ready"
    assert meta.json()["counts"] == {"chain": 4, "selected": 3}  # two checks and the supplement
    listed = client.get(f"/api/v1/attest/{LOG}/exports")
    assert [e["export_id"] for e in listed.json()["exports"]] == [export_id]
    dl = client.get(f"/api/v1/attest/exports/{export_id}/download")
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(dl.content)).namelist()[0] == "manifest.json"
    book = client.get(f"/api/v1/attest/logbooks/{LOG}")
    assert book.json()["exports"] == [{"id": "sheet", "template": "templates/sheet.txt"}]
    # A person at the site who may not read the log (W1-A9's rule): its exports do not exist.
    from axiom.extensions.builtins.attest import access_rules

    with monkeypatch.context() as m:
        m.setattr(access_rules, "can_read", lambda book, roles: False)
        assert client.get(f"/api/v1/attest/exports/{export_id}").status_code == 404
        assert client.get(f"/api/v1/attest/exports/{export_id}/download").status_code == 404
        assert client.get(f"/api/v1/attest/{LOG}/exports").status_code == 404
        assert client.post(f"/api/v1/attest/{LOG}/exports", json={"formats": ["text"]}).status_code == 404
    as_(client, other)  # another site's person
    assert client.get(f"/api/v1/attest/exports/{export_id}").status_code == 404
    assert client.get(f"/api/v1/attest/exports/{export_id}/download").status_code == 404
    assert (
        client.post(f"/api/v1/attest/{LOG}/exports", json={"formats": ["text"]}).status_code == 404
    )


# -- A9: the preview, and the surfaces ------------------------------------------------------


@pytest.mark.acceptance("LB-L6-A9")
def test_lb_l6_a9_the_preview_route_counts_what_the_export_then_selects(chain, client, monkeypatch):
    """Axiom #1970: the Log page's Export panel says the selection's count before a package is made
    ("2 of 3 entries selected, plus 1 supplement"). The route writes nothing, counts exactly what
    ``POST /exports`` then selects, and refuses as that route does."""
    from axiom.extensions.builtins.attest import access_rules
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_
    from axiom.webauth.users import User

    as_(client, OP1)
    url = f"/api/v1/attest/{LOG}/exports/preview"
    # The body the web sends: the export's own, formats and forms included (appkit exportBody).
    body = {"formats": ["text"], "forms": [], "entry_type": ["CHECK"]}
    r = client.post(url, json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"site_id": SITE, "logbook": LOG, "selected": 2, "supplements": 1, "total": 3}
    assert client.post(url, json={**body, "include_supplements": False}).json()["supplements"] == 0
    assert client.post(url, json={"formats": ["text"]}).json() == {
        "site_id": SITE, "logbook": LOG, "selected": 3, "supplements": 1, "total": 3}  # fmt: skip
    # Nothing written: no export row, no stored package.
    assert exports.listed(SITE, LOG) == []
    storage_dir = exports._storage_dir
    assert storage_dir is None or not any(Path(storage_dir).rglob("*.zip"))
    # The count is the package's: entries plus supplements is what the export then selects.
    made = client.post(f"/api/v1/attest/{LOG}/exports", json=body)
    assert made.status_code == 200, made.text
    assert made.json()["counts"]["selected"] == 2 + 1
    # A filter the export refuses, the preview refuses.
    assert client.post(url, json={"colour": "blue"}).status_code == 400
    # A person who may not read the log, or another site's: the log does not exist for them.
    with monkeypatch.context() as m:
        m.setattr(access_rules, "can_read", lambda book, roles: False)
        assert client.post(url, json=body).status_code == 404
    other = User(user_id="op3", email="op3@example.org", name="Op Three", roles=("user",),
                 site="site-b", attributes={"idp": "entra"})  # fmt: skip
    as_(client, other)
    assert client.post(url, json=body).status_code == 404



@pytest.mark.acceptance("LB-L6-A9")
def test_lb_l6_a9_the_preview_writes_nothing_and_returns_at_most_200(chain, monkeypatch):
    from axiom.extensions.builtins.attest.skills import read as read_skills

    monkeypatch.setattr(exports, "PREVIEW_LIMIT", 2)
    wrote = []
    monkeypatch.setattr(exports, "create", lambda *a, **k: wrote.append(a))
    r = read_skills.export_preview({"site": SITE, "logbook": LOG, "format": "text"}, None)
    assert r.ok, r.errors
    assert r.value["records"] == 2 and r.value["truncated"] is True
    assert "#" in r.value["text"] and not wrote
    by = {s.name: s for s in SPECS}
    assert exports.PREVIEW_MAX == 200
    assert by["attest.export_preview"].side_effects is False
    assert set(by["attest.export_preview"].surfaces) == {"cli", "mcp", "agent_tool"}
    assert by["attest.export"].surfaces == ("cli",), "no agent creates a package"


@pytest.mark.acceptance("LB-L6-A5")
def test_lb_l6_the_export_skill_with_a_format_writes_the_package_files(chain, tmp_path, roles):
    from axiom.extensions.builtins.attest.tests.test_skills import _run, ctx

    out = tmp_path / "out"
    r = _run("attest.export", {"site": SITE, "logbook": LOG, "out": str(out), "format": ["csv"],
                               "entry_type": "CHECK"}, ctx(roles))  # fmt: skip
    assert r.ok, r.errors
    assert (out / "records.csv").exists() and (out / "chain.jsonl").exists()
    assert r.value["export_id"]


@pytest.mark.acceptance("LB-L6-A4")
def test_lb_l6_the_cli_export_takes_formats_and_filters(
    chain, tmp_path, roles, monkeypatch, capsys
):
    from axiom.extensions.builtins.attest import log_cli  # the `log` noun (ADR-243, unit F5)
    from axiom.extensions.builtins.attest.tests.test_skills import ctx

    made = ctx(roles)
    monkeypatch.setattr(log_cli, "_build_ctx", lambda: made[0] if isinstance(made, tuple) else made)
    out = tmp_path / "cli-out"
    code = log_cli.main([log_cli._handle_of(LOG), "export", "--site", SITE, "--format", "csv",
                         "--type", "CHECK", "--no-supplements", "--out", str(out)])  # fmt: skip
    assert code == 0, capsys.readouterr()
    rows = list(csv.DictReader(io.StringIO((out / "records.csv").read_text(encoding="utf-8"))))
    assert [r["kind"] for r in rows] == ["statement", "statement"]
    assert "evidence package" in capsys.readouterr().out
