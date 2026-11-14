# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S7 Export and offline verification (#1818): the package verifies with a bare interpreter, and
the three renderings of one filtered selection agree with the records route, record for record."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import zipfile

import pytest

from .conftest import ANN, LOG, SITE, as_, check, open_session, sign_cli, sign_web


def _run(world):
    from axiom.extensions.builtins.attest import service

    open_session(world)
    c = check(world)
    sign_cli(ANN, "NOTE", {"text": "a note"})

    d = service.create_supplement_draft(site_id=SITE, target=c["attestation_id"], reason="correction",
                                        content={"fields": {"readings": {"flow": "12.4"}}},
                                        origin="human", for_principal=ANN.handle)  # fmt: skip
    sign_web(ANN, d, device="desk-1")
    return c


@pytest.mark.acceptance("LB-E2E-A16")
def test_lb_e2e_a16_the_package_verifies_with_a_bare_interpreter_at_the_current_head(
    world, tmp_path
):
    from axiom.extensions.builtins.attest import exports, service

    _run(world)
    exports.set_storage(None, base_dir=tmp_path / "storage")
    try:
        pkg = exports.build_package(SITE, LOG, exports.parse_filter({}), formats=("jsonl",))
    finally:
        exports.set_storage(None)
    out = tmp_path / "pkg"
    zipfile.ZipFile(io.BytesIO(pkg.data)).extractall(out)
    run = subprocess.run([sys.executable, "-I", "verify.py", "chain.jsonl", "--keys", "keys.json"],
                         cwd=out, capture_output=True, text=True, timeout=120, env={})  # fmt: skip
    head = service.records(SITE, LOG)[-1]
    assert run.returncode == 0, run.stdout + run.stderr
    assert head["digest"] in run.stdout and pkg.manifest["head"]["seq"] == head["seq"]


@pytest.mark.acceptance("LB-E2E-A17")
def test_lb_e2e_a17_three_renderings_agree_with_the_records_route(world, web):
    from axiom.extensions.builtins.attest import exports, registry

    c = _run(world)
    f = exports.parse_filter(
        {"entry_type": "CHECK", "signer": ANN.handle, "include_supplements": False}
    )
    picked = exports.select(SITE, LOG, f)
    route = as_(web, ANN).get(f"/api/v1/attest/{LOG}/records", params={"entry_type": "CHECK",
                                                                       "signer": ANN.handle}).json()  # fmt: skip
    assert [r["seq"] for r in picked] == sorted(r["seq"] for r in route["records"]) == [c["seq"]]
    lines = [json.loads(x) for x in exports.render_jsonl(picked).decode().splitlines()]
    assert [x["seq"] for x in lines] == [c["seq"]]
    rows = list(csv.DictReader(io.StringIO(exports.render_csv(picked).decode())))
    assert [int(r["seq"]) for r in rows] == [c["seq"]] and rows[0]["readings.flow"] == "12.5"
    chain = exports._chain(SITE, LOG)
    book = registry.get(LOG)
    one = exports.render_text(SITE, book, picked, chain=chain, f=f)
    assert one == exports.render_text(SITE, book, picked, chain=chain, f=f)
    form = exports.render_template(SITE, book, "daily", picked, chain=chain).decode()
    assert form.startswith("Desk Log") and f"#{c['seq']}" in form and "Verification: ok" in form


@pytest.mark.acceptance("LB-E2E-A17")
def test_lb_e2e_a17_the_declared_form_renders_blank_with_page_numbers(world):
    # #1805: a blank form for a backup binder is not refused, and every page says "X of Y".
    from axiom.extensions.builtins.attest import exports, registry

    blank = exports.render_template(SITE, registry.get(LOG), "daily", [], chain=[]).decode()
    assert "Page 1 of 1" in blank and blank.startswith("Desk Log")
    assert "Verification: blank form" in blank and "#" not in blank


@pytest.mark.acceptance("LB-E2E-A17")
def test_lb_e2e_a17_the_declared_form_pages_its_rows_with_the_head_on_every_page(world):
    from axiom.extensions.builtins.attest import exports, registry, service

    open_session(world)
    for i in range(4):
        sign_cli(ANN, "NOTE", {"text": f"note {i}"})
    book = registry.get(LOG)
    assert book.export("daily").rows_per_page == 2
    chain = exports._chain(SITE, LOG)
    form = exports.render_template(SITE, book, "daily", service.records(SITE, LOG), chain=chain)
    pages = form.decode().split("\f")
    assert [p.count("Page ") for p in pages] == [1, 1, 1]
    assert [f"Page {n} of 3" in p for n, p in enumerate(pages, 1)] == [True, True, True]
    assert all("Verification: ok" in p for p in pages)


@pytest.mark.acceptance("LB-E2E-A17")
def test_lb_e2e_a17_the_blank_form_is_one_skill_on_every_surface(world, web):
    import logging

    from axiom.extensions.builtins.attest.skills import SPECS, bind_default
    from axiom.infra.skills import SkillContext

    spec = {s.name: s for s in SPECS}["attest.export_blank"]
    assert set(spec.surfaces) == {"cli", "mcp", "agent_tool"} and not spec.side_effects
    ctx = SkillContext(registry=bind_default(), state_dir=world.state_dir,
                       logger=logging.getLogger("e2e"))  # fmt: skip
    r = ctx.registry.invoke("attest.export_blank", {"site": SITE, "logbook": LOG, "form": "daily"}, ctx)
    assert r.ok and "Page 1 of 1" in r.value["text"], r.errors
    page = as_(web, ANN).get(f"/api/v1/attest/{LOG}/forms/daily/blank")
    assert page.status_code == 200 and "Page 1 of 1" in page.text
    assert as_(web, ANN).get(f"/api/v1/attest/{LOG}/forms/nope/blank").status_code == 404


@pytest.mark.acceptance("LB-E2E-A16")
def test_lb_e2e_a16_a_seal_anchor_and_a_package_are_receipts_on_the_action_ledger(world, tmp_path):
    # #1814, decided by the owner: each is a row on the action ledger, listed by one read in the
    # receipts console ("sealed and packaged"): what, when, by whom and a link; the digest one
    # step away.
    import logging

    from axiom.extensions.builtins.attest import exports, grants, service, signing
    from axiom.extensions.builtins.receipts.skills import bind_default
    from axiom.infra.skills import SkillContext

    from .conftest import BO, auth, keys, signatory

    _run(world)
    d = service.create_seal_draft(site_id=SITE, logbook=LOG, summary="Closed out.", origin="human",
                                  for_principal=BO.handle, boundary="session")  # fmt: skip
    pres = service.present(d, modality="screen")
    service.respond(pres.presentation_id, principal=signatory(BO).principal, answer="sign",
                    via="screen")  # fmt: skip
    token = grants.mint(pres.presentation_id, auth=auth(BO), device=grants.Device("desk-1", "personal"),
                        signer=signing.signer())  # fmt: skip
    anchor = service.sign(pres.presentation_id, signatory=signatory(BO), signer=signing.signer(),
                          source="screen", grant=token, grant_keys=keys()).anchor
    exports.set_storage(None, base_dir=tmp_path / "storage")
    try:
        row, _pkg = exports.create(SITE, LOG, exports.parse_filter({}), formats=("text",),
                                   requested_by=ANN.handle)  # fmt: skip
    finally:
        exports.set_storage(None)
    reg = bind_default()
    ctx = SkillContext(registry=reg, state_dir=world.state_dir, logger=logging.getLogger("e2e"))
    got = reg.invoke("receipts.sealed", {}, ctx)
    assert got.ok, got.errors
    rows = {r["what"]: r for r in got.value["rows"]}
    assert set(rows) == {"sealed", "packaged"}
    assert rows["sealed"]["by"] == BO.handle and rows["sealed"]["ref"] == anchor["anchor_id"]
    assert rows["packaged"]["by"] == ANN.handle and rows["packaged"]["ref"] == row["export_id"]
    assert all(r["link"].startswith(f"/log/{LOG}") and r["at"] for r in rows.values())
    assert rows["packaged"]["detail"]["digest"] == row["manifest_sha256"]
    assert "sign" not in rows["packaged"]["summary"].lower()


@pytest.mark.acceptance("LB-E2E-A17")
def test_lb_e2e_a17_the_declared_form_prints_as_a_pdf_on_the_server(world, tmp_path):
    # #1805, decided by the owner: a server-side PDF, so the command line, an automation and the
    # web print the same document, with page numbers and the verification footer on every page.
    import pypdf

    from axiom.extensions.builtins.attest import exports, registry, service

    open_session(world)
    for i in range(4):
        sign_cli(ANN, "NOTE", {"text": f"note {i}"})
    book = registry.get(LOG)
    chain = exports._chain(SITE, LOG)
    pdf = exports.render_template_pdf(SITE, book, "daily", service.records(SITE, LOG), chain=chain)
    assert pdf.startswith(b"%PDF-")
    assert pdf == exports.render_template_pdf(SITE, book, "daily", service.records(SITE, LOG),
                                              chain=chain), "the same selection, the same bytes"  # fmt: skip
    pages = [p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages]
    assert len(pages) == 3
    assert all(f"Page {n} of 3" in p and "Verification: ok" in p for n, p in enumerate(pages, 1))
    blank = pypdf.PdfReader(io.BytesIO(exports.render_blank_pdf(SITE, book, "daily")))
    assert len(blank.pages) == 1 and "Page 1 of 1" in blank.pages[0].extract_text()
    exports.set_storage(None, base_dir=tmp_path / "storage")
    try:
        pkg = exports.build_package(SITE, LOG, exports.parse_filter({}), formats=("pdf",),
                                    templates=("daily",))  # fmt: skip
    finally:
        exports.set_storage(None)
    names = zipfile.ZipFile(io.BytesIO(pkg.data)).namelist()
    assert "daily.pdf" in names and "daily.pdf" in pkg.manifest["files"]
    with pytest.raises(exports.ExportRefused, match="form"):
        exports.build_package(SITE, LOG, exports.parse_filter({}), formats=("pdf",))


@pytest.mark.acceptance("LB-E2E-A17")
def test_lb_e2e_a17_the_blank_pdf_is_the_same_from_the_web_and_the_command_line(world, web, tmp_path):
    import logging

    from axiom.extensions.builtins.attest import exports, registry
    from axiom.extensions.builtins.attest.skills import bind_default
    from axiom.infra.skills import SkillContext

    r = as_(web, ANN).get(f"/api/v1/attest/{LOG}/forms/daily/blank?format=pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    ctx = SkillContext(registry=bind_default(), state_dir=world.state_dir,
                       logger=logging.getLogger("e2e"))  # fmt: skip
    out = tmp_path / "blank.pdf"
    got = ctx.registry.invoke("attest.export_blank", {"site": SITE, "logbook": LOG, "form": "daily",
                                                      "out": str(out)}, ctx)  # fmt: skip
    assert got.ok, got.errors
    assert out.read_bytes() == r.content == exports.render_blank_pdf(SITE, registry.get(LOG), "daily")
