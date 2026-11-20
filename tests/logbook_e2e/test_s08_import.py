# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S8 Import (#1818, #1804): history from another system placed on a log's chain as unsigned
``import`` records.

The design note is ``docs/working/design-note-import-records.md`` (approved by the owner
2026-11-13) and the meaning ``imported`` is ADR-262. One row per decision (``LB-I1-D1`` to
``D5``) and per note section (``LB-I1-S2`` to ``S6``); ``LB-E2E-A18`` is the stage's own row.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from .conftest import ANN, BO, CY, LOG, SITE, as_, open_session, sign_cli, signatory

LATER = (date.today() + timedelta(days=30)).isoformat()


def _write(tmp_path: Path, rows: list[dict], name: str = "history.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _old(n: int = 2, **extra) -> list[dict]:
    return [
        {"occurred_at": f"2025-01-0{i + 1}T08:00:00Z", "title": f"Old entry {i + 1}",
         "source_ref": f"paper-{i + 1}", "by": "J.D.", **extra}
        for i in range(n)
    ]  # fmt: skip


def _importer(world, p=BO, until=LATER):
    from axiom.extensions.builtins.attest import roles

    roles.grant(p.handle, SITE, "importer", by="@admin:example-site", state_dir=world.state_dir,
                logbook=LOG, until=until)  # fmt: skip


def _commit(world, path: Path, p=BO, **kw):
    from axiom.extensions.builtins.attest import imports

    content = path.read_bytes()
    dry = imports.dry_run(SITE, LOG, content)
    return imports.commit(SITE, LOG, content, digest=dry["digest"], by=p.handle,
                          state_dir=world.state_dir, **kw)  # fmt: skip


def _audit(world, skill: str) -> list[dict]:
    from axiom.infra import audit_trail

    return [r for r in audit_trail.read_chain(audit_trail.audit_path(world.state_dir))
            if r.get("skill") == skill]  # fmt: skip


@pytest.mark.acceptance("LB-E2E-A18")
def test_lb_e2e_a18_a_history_imports_as_unsigned_records(world, tmp_path):
    from axiom.extensions.builtins.attest import service

    _importer(world)
    history = tmp_path / "history.jsonl"
    history.write_text(
        '{"occurred_at": "2025-01-01T08:00:00Z", "title": "An old entry"}\n', encoding="utf-8"
    )
    dry = service.import_history(SITE, LOG, history, dry_run=True)
    assert dry["written"] == 0
    done = service.import_history(SITE, LOG, history, digest=dry["digest"], by=BO.handle)
    assert done["records"][0]["kind"] == "import"
    assert service.import_history(SITE, LOG, history, digest=dry["digest"], by=BO.handle)["written"] == 0


# -- decision 1: who may import ---------------------------------------------------------------


@pytest.mark.acceptance("LB-I1-D1")
def test_lb_i1_d1_the_importer_role_is_log_scoped_needs_an_expiry_and_is_implied_by_nothing(
    world, tmp_path, monkeypatch
):
    from axiom.extensions.builtins.attest import imports, roles, service

    for bad, why in (
        (dict(logbook=LOG), "expiry"),  # no --until
        (dict(until=LATER), "--log"),  # not for one log
        (dict(logbook=LOG, until="2020-01-01"), "past"),
    ):
        with pytest.raises(ValueError, match=why):
            roles.grant(BO.handle, SITE, "importer", by="@admin:x", state_dir=world.state_dir, **bad)
    content = _write(tmp_path, _old(1)).read_bytes()
    dry = imports.dry_run(SITE, LOG, content)
    # A signing role (BO is a lead, ANN a member), an administration role, a gate pair to the
    # importer role: none of them is the importer role.
    roles.grant("@root:example-site", SITE, "member", by="@admin:x", state_dir=world.state_dir)
    monkeypatch.setattr(roles, "gate_pairs", lambda site: {"admin": "importer", "ops": "importer"})
    for who in (BO, ANN):
        with pytest.raises(service.AttestRefused, match="importer"):
            imports.commit(SITE, LOG, content, digest=dry["digest"], by=who.handle,
                           state_dir=world.state_dir)  # fmt: skip
    # An expired grant is no grant.
    _importer(world, ANN, until=LATER)
    with monkeypatch.context() as m:
        m.setattr(imports, "_now", lambda: datetime.now(UTC) + timedelta(days=60))
        with pytest.raises(service.AttestRefused, match="expired"):
            imports.commit(SITE, LOG, content, digest=dry["digest"], by=ANN.handle,
                           state_dir=world.state_dir)  # fmt: skip
    _importer(world)
    done = imports.commit(SITE, LOG, content, digest=dry["digest"], by=BO.handle,
                          state_dir=world.state_dir)  # fmt: skip
    assert done["written"] == 1
    assert all(r["imported_by"] == BO.handle for r in done["records"])
    batches = _audit(world, "attest.import_commit")
    assert len(batches) == 1 and batches[0]["principal"] == BO.handle
    assert batches[0]["change"]["batch"] == done["batch"]


# -- decision 2: the meaning ------------------------------------------------------------------


@pytest.mark.acceptance("LB-I1-D2")
def test_lb_i1_d2_imported_is_a_meaning_only_an_import_carries(world, tmp_path):
    from axiom.extensions.builtins.attest import logbooks, service

    assert "imported" in logbooks.MEANINGS
    decl = Path(__file__).parent / "logbooks" / "e2e_log.toml"
    bad = decl.read_text(encoding="utf-8").replace(
        'id = "NOTE"\nmeanings = ["authored"]', 'id = "NOTE"\nmeanings = ["authored", "imported"]'
    )
    import tomllib

    with pytest.raises(logbooks.LogbookError, match="imported"):
        logbooks.parse_logbook(tomllib.loads(bad))
    with pytest.raises(service.AttestRefused, match="imported"):
        service.create_draft(site_id=SITE, logbook=LOG, entry_type="NOTE", meaning="imported",
                             content={"title": "x", "fields": {"text": "x"}}, origin="human",
                             for_principal=ANN.handle)  # fmt: skip
    _importer(world)
    rec = _commit(world, _write(tmp_path, _old(1, original_meaning="checked")))["records"][0]
    assert rec["meaning"] == "imported"
    assert rec["content"]["original_meaning"] == "checked"
    adr = sorted((Path(__file__).parents[2] / "docs" / "adrs").glob("adr-262-*.md"))
    assert adr and "`imported`" in adr[0].read_text(encoding="utf-8")


# -- decision 3: a person commits, at the prompt or through the gate ------------------------------


@pytest.mark.acceptance("LB-I1-D3")
def test_lb_i1_d3_software_prepares_a_dry_run_and_only_a_person_commits(world, tmp_path):
    from axiom.extensions.builtins.attest import skills

    reg = skills.bind_default()
    for name in ("attest.import", "attest.import_withdraw"):
        assert reg.spec(name).surfaces == ("cli",)
    preview = reg.spec("attest.import_preview")
    assert "mcp" in preview.surfaces and preview.side_effects is False
    for spec in reg.specs().values():
        if {"mcp", "agent_tool"} & set(spec.surfaces or ()) and spec.name.startswith("attest.import"):
            assert spec.name in ("attest.import_preview", "attest.imports")
    # The agent's dry run writes nothing.
    from axiom.extensions.builtins.attest.skills import imports as import_skills

    out = import_skills.preview({"logbook": LOG, "site": SITE,
                                 "content": _write(tmp_path, _old(2)).read_text()}, None)  # fmt: skip
    assert out.ok and out.value["counts"]["new"] == 2 and out.value["written"] == 0
    from axiom.extensions.builtins.attest import service

    assert service.records(SITE, LOG) == []
    # The command line commits only with a person at the prompt.
    from types import SimpleNamespace

    from axiom.infra.principal import PrincipalContext

    _importer(world)
    path = _write(tmp_path, _old(2))
    ctx = SimpleNamespace(principal=PrincipalContext(handle=BO.handle, posture="attested", assured=True),
                          state_dir=world.state_dir, user_prompt=None)  # fmt: skip
    dry = import_skills.run({"logbook": LOG, "site": SITE, "path": str(path)}, ctx)
    assert dry.ok and dry.value["written"] == 0
    digest = dry.value["digest"]
    nobody = import_skills.run({"logbook": LOG, "site": SITE, "path": str(path), "commit": digest}, ctx)
    assert not nobody.ok and "person at the prompt" in nobody.errors[0]
    asked: list[str] = []
    ctx.user_prompt = lambda text: asked.append(text) or "yes"
    done = import_skills.run({"logbook": LOG, "site": SITE, "path": str(path), "commit": digest}, ctx)
    assert done.ok and done.value["written"] == 2 and asked and "2" in asked[0]


@pytest.mark.acceptance("LB-I1-D3W")
def test_lb_i1_d3w_the_web_commits_only_through_the_gate_hand_off(world, web, tmp_path):
    from axiom.extensions.builtins.attest import service

    content = _write(tmp_path, _old(2)).read_text()
    # Not an importer: the dry run is shown, with no way to commit.
    r = as_(web, ANN).post(f"/api/v1/attest/{LOG}/imports/dry-run", json={"content": content})
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["new"] == 2 and r.json()["commit_url"] is None
    assert as_(web, ANN).get(f"/api/v1/attest/logbooks/{LOG}").json()["may_import"] is False
    _importer(world)
    assert as_(web, BO).get(f"/api/v1/attest/logbooks/{LOG}").json()["may_import"] is True
    r = as_(web, BO).post(f"/api/v1/attest/{LOG}/imports/dry-run",
                          json={"content": content, "return_to": "/log/desk"})  # fmt: skip
    body = r.json()
    url = body["commit_url"]
    assert url.startswith("/gate/import/commit?") and service.records(SITE, LOG) == []
    # No raw HTTP commit: no route takes one, by session or by key.
    for path in (f"/api/v1/attest/{LOG}/imports/commit", f"/api/v1/attest/{LOG}/imports"):
        assert as_(web, BO).post(path, json={"content": content, "digest": body["digest"]}).status_code in (404, 405)
    # Signed out, the gate sends the person to sign in first.
    assert as_(web, None).get(url).status_code == 303
    page = as_(web, BO).get(url)
    assert page.status_code == 200 and "Import 2 entries" in page.text and body["digest"][:12] in page.text
    # Another person holding no importer role is refused at the gate, and nothing is written.
    form = {"prepared_id": body["prepared_id"]}
    refused = as_(web, ANN).post("/gate/import/commit", data=form)
    assert refused.status_code == 403 and service.records(SITE, LOG) == []
    done = as_(web, BO).post("/gate/import/commit", data=form)
    assert done.status_code == 303 and done.headers["location"].startswith("/log/desk?import_batch=")
    recs = service.records(SITE, LOG)
    assert [r["kind"] for r in recs] == ["import", "import"]
    assert {r["imported_by"] for r in recs} == {BO.handle}
    assert _audit(world, "attest.import_commit")[0]["change"]["via"] == "web"
    # A prepared dry run commits once.
    assert as_(web, BO).post("/gate/import/commit", data=form).status_code in (400, 404)


# -- decision 4: imports after live entries ---------------------------------------------------------


@pytest.mark.acceptance("LB-I1-D4")
def test_lb_i1_d4_an_import_after_live_entries_is_appended_flagged_and_sorted_by_its_time(
    world, web, tmp_path
):
    from axiom.extensions.builtins.attest import exports, imports, service

    live = sign_cli(ANN, "NOTE", {"text": "a live note"})
    _importer(world)
    first_live = datetime.fromisoformat(live["occurred_at"].replace("Z", "+00:00"))
    after = (first_live + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    world.clock["t"] = first_live + timedelta(minutes=5)
    rows = _old(1) + [{"occurred_at": after, "title": "Late paper entry", "source_ref": "paper-9"}]
    content = _write(tmp_path, rows).read_bytes()
    dry = imports.dry_run(SITE, LOG, content)
    assert dry["counts"]["warnings"] == 1 and dry["counts"]["errors"] == 0
    assert dry["warnings"][0]["source_ref"] == "paper-9" and "first live entry" in dry["warnings"][0]["warning"]
    done = imports.commit(SITE, LOG, content, digest=dry["digest"], by=BO.handle, state_dir=world.state_dir)
    assert [r["seq"] for r in done["records"]] == [2, 3]  # appended in the order committed
    # Views sort by when it happened: the 2025 import first, then the live note, then the late one.
    page = as_(web, ANN).get(f"/api/v1/attest/{LOG}/records", params={"order": "occurred"}).json()
    assert [r["seq"] for r in page["records"]] == [2, 1, 3]
    imp = page["records"][0]["import"]
    assert imp["batch"] == done["batch"] and imp["original_time"] == "2025-01-01T08:00:00.000000Z"
    shown = service.briefs(service.records_where(SITE, LOG, order="occurred", limit=50))
    assert [b["seq"] for b in shown] == [2, 1, 3]
    f = exports.parse_filter({})
    csv_rows = exports.render_csv(exports.select(SITE, LOG, f)).decode().splitlines()[1:]
    assert [row.split(",")[0] for row in csv_rows] == ["2", "1", "3"]


# -- decision 5: corrections and withdrawal ---------------------------------------------------------


@pytest.mark.acceptance("LB-I1-D5A")
def test_lb_i1_d5a_one_imported_entry_is_corrected_by_the_existing_path(world, tmp_path):
    from axiom.extensions.builtins.attest import grants, service

    from .conftest import _signer, auth, keys, sign_web

    _importer(world)
    rec = _commit(world, _write(tmp_path, _old(1)))["records"][0]
    d = service.create_supplement_draft(site_id=SITE, target=rec["attestation_id"], reason="correction",
                                        content={"body": "It was 9 o'clock, not 8."}, origin="human",
                                        for_principal=ANN.handle)  # fmt: skip
    fix = sign_web(ANN, d)
    assert fix["kind"] == "supplement" and fix["target"] == rec["attestation_id"]
    assert service.record(rec["attestation_id"]) == rec  # never changed
    b = {x["attestation_id"]: x for x in service.briefs(service.records(SITE, LOG))}
    assert b[rec["attestation_id"]]["corrected"] is True
    # A person holding no role in the log cannot sign one.
    d2 = service.create_supplement_draft(site_id=SITE, target=rec["attestation_id"], reason="addition",
                                         content={"body": "more"}, origin="human",
                                         for_principal=CY.handle)  # fmt: skip
    p2 = service.present(d2, modality="screen")
    service.respond(p2.presentation_id, principal=signatory(CY).principal, answer="sign", via="screen")
    token = grants.mint(p2.presentation_id, auth=auth(CY), signer=_signer(),
                        device=grants.Device(device_id="desk-1", device_class="personal"))  # fmt: skip
    with pytest.raises(service.AttestRefused, match="holds no role"):
        service.sign(p2.presentation_id, signer=_signer(), source="screen", grant=token, grant_keys=keys(),
                     signatory=service.Signatory(principal=signatory(CY).principal, roles=(),
                                                 display=CY.name))  # fmt: skip


@pytest.mark.acceptance("LB-I1-D5C")
def test_lb_i1_d5c_a_batch_is_withdrawn_with_a_reason_then_imported_again(world, tmp_path):
    from axiom.extensions.builtins.attest import imports, service

    _importer(world)
    path = _write(tmp_path, _old(2))
    first = _commit(world, path)
    with pytest.raises(service.AttestRefused, match="reason"):
        imports.withdraw(SITE, LOG, first["batch"], reason="  ", by=BO.handle, state_dir=world.state_dir)
    with pytest.raises(service.AttestRefused, match="importer"):
        imports.withdraw(SITE, LOG, first["batch"], reason="wrong file", by=ANN.handle,
                         state_dir=world.state_dir)  # fmt: skip
    before = service.records(SITE, LOG)
    out = imports.withdraw(SITE, LOG, first["batch"], reason="the wrong year's binder",
                           by=BO.handle, state_dir=world.state_dir)  # fmt: skip
    w = out["record"]
    assert w["kind"] == "import_withdrawal" and w["meaning"] == "retracted"
    assert w["withdrawn_by"] == BO.handle and w["reason"] == "the wrong year's binder"
    assert sorted(w["targets"]) == sorted(r["attestation_id"] for r in first["records"])
    assert service.records(SITE, LOG)[: len(before)] == before  # nothing changed or deleted
    briefs = service.briefs(service.records(SITE, LOG))
    assert all(b["import"]["withdrawn"] for b in briefs if b["kind"] == "import")
    with pytest.raises(service.AttestRefused, match="already withdrawn"):
        imports.withdraw(SITE, LOG, first["batch"], reason="again", by=BO.handle, state_dir=world.state_dir)
    again = _commit(world, path)
    assert again["written"] == 2 and again["batch"] != first["batch"]
    assert len(_audit(world, "attest.import_withdraw")) == 1


@pytest.mark.acceptance("LB-I1-D5R")
def test_lb_i1_d5r_a_batch_with_a_correction_on_it_cannot_be_withdrawn(world, tmp_path):
    from axiom.extensions.builtins.attest import imports, service

    _importer(world)
    done = _commit(world, _write(tmp_path, _old(2)))
    d = service.create_supplement_draft(site_id=SITE, target=done["records"][1]["attestation_id"],
                                        reason="correction", content={"body": "fixed"},
                                        origin="human", for_principal=ANN.handle)  # fmt: skip
    from .conftest import sign_web

    sign_web(ANN, d)
    with pytest.raises(service.AttestRefused, match="correction"):
        imports.withdraw(SITE, LOG, done["batch"], reason="wrong", by=BO.handle, state_dir=world.state_dir)


@pytest.mark.acceptance("LB-I1-D5U")
def test_lb_i1_d5u_two_concurrent_imports_write_each_entry_once(world, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from axiom.extensions.builtins.attest import imports, service, store

    _importer(world)
    content = _write(tmp_path, _old(3)).read_bytes()
    dry = imports.dry_run(SITE, LOG, content)
    results: list = []

    def go():
        try:
            results.append(imports.commit(SITE, LOG, content, digest=dry["digest"], by=BO.handle,
                                          state_dir=world.state_dir)["written"])  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [0, 3]
    assert len(service.records(SITE, LOG)) == 3
    # The database holds the rule by itself: a second live reference is refused.
    with pytest.raises(IntegrityError):
        with store.session_scope() as s:
            s.execute(text("INSERT INTO attest_import_refs (attestation_id, site_id, logbook, "
                           "source_ref, batch_id) VALUES ('x', :site, :log, 'paper-1', 'b')"),
                      {"site": SITE, "log": LOG})  # fmt: skip
            s.commit()


# -- the note's sections ------------------------------------------------------------------------


@pytest.mark.acceptance("LB-I1-S2")
def test_lb_i1_s2_an_import_record_has_the_body_the_note_names(world, tmp_path):
    import hashlib

    _importer(world)
    line = {"occurred_at": "2025-03-01T07:30:00Z", "title": "Checked", "entry_type": "CHECK",
            "fields": {"readings": {"flow": "12.5"}}, "by": "A.B.", "interval": {"kind": "session", "number": 41}}  # fmt: skip
    plain = {"occurred_at": "2025-03-02T07:30:00Z", "title": "No id"}
    path = _write(tmp_path, [line, plain])
    done = _commit(world, path, source="paperlog")
    rec, rec2 = done["records"]
    assert rec["kind"] == "import" and rec["source"] == "import" and rec["origin"] == "integration:paperlog"
    assert rec["signer"] == {"kind": "unverified", "display": "A.B.", "principal": None}
    assert rec["entry_type"] == "CHECK" and rec["content"]["fields"]["readings"] == {"flow": "12.5"}
    assert rec["occurred_at"] == "2025-03-01T07:30:00.000000Z" and rec["recorded_at"] != rec["occurred_at"]
    assert rec["interval"] == {"kind": "session", "number": 41}  # no event: nothing opened
    assert rec["import_batch"] == done["batch"] == rec2["import_batch"]
    assert rec["source_digest"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert rec2["source_ref"].startswith("sha256:") and rec2["entry_type"] is None
    # An unknown field is refused in the dry run, and the dry run names it.
    from axiom.extensions.builtins.attest import imports

    bad = imports.dry_run(SITE, LOG, _write(tmp_path, [{**line, "fields": {"nope": "1"}, "source_ref": "z"}], "b.jsonl").read_bytes())
    assert bad["counts"]["errors"] == 1 and "nope" in bad["decisions"][0]["reason"]


@pytest.mark.acceptance("LB-I1-S4")
def test_lb_i1_s4_a_dry_run_by_default_and_a_commit_gated_by_its_digest(world, tmp_path):
    from axiom.extensions.builtins.attest import imports, service

    _importer(world)
    path = _write(tmp_path, _old(2))
    dry = imports.dry_run(SITE, LOG, path.read_bytes())
    assert dry["written"] == 0 and dry["counts"] == {"lines": 2, "new": 2, "skipped": 0, "errors": 0, "warnings": 0}
    assert [d["action"] for d in dry["decisions"]] == ["new", "new"] and service.records(SITE, LOG) == []
    changed = path.read_bytes() + b'{"occurred_at": "2025-02-01T00:00:00Z", "title": "x"}\n'
    with pytest.raises(service.AttestRefused, match="digest"):
        imports.commit(SITE, LOG, changed, digest=dry["digest"], by=BO.handle, state_dir=world.state_dir)
    broken = b'{"title": "no time"}\n'
    bad = imports.dry_run(SITE, LOG, broken)
    with pytest.raises(service.AttestRefused, match="error"):
        imports.commit(SITE, LOG, broken, digest=bad["digest"], by=BO.handle, state_dir=world.state_dir)
    imports.commit(SITE, LOG, path.read_bytes(), digest=dry["digest"], by=BO.handle, state_dir=world.state_dir)
    again = imports.dry_run(SITE, LOG, path.read_bytes())
    assert again["counts"]["skipped"] == 2 and again["counts"]["new"] == 0


@pytest.mark.acceptance("LB-I1-S4M")
def test_lb_i1_s4m_one_expand_only_migration_and_one_head(attest_db):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from axiom.infra.db import extensions_with_migrations

    config = Config()
    config.set_main_option("script_location", str(dict(extensions_with_migrations())["attest"]))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0014"]
    source = Path(script.get_revision("0014").path).read_text(encoding="utf-8")
    assert "drop_" not in source.split("def downgrade")[0] and "WHERE withdrawn_at IS NULL" in source


@pytest.mark.acceptance("LB-I1-S5")
def test_lb_i1_s5_an_import_never_opens_or_closes_and_live_numbering_continues_above(world, tmp_path):
    from axiom.extensions.builtins.attest import intervals

    _importer(world)
    _commit(world, _write(tmp_path, [
        {"occurred_at": "2025-01-01T08:00:00Z", "title": "Opened", "entry_type": "OPEN",
         "fields": {"text": "on"}, "interval": {"kind": "session", "number": 9100}, "source_ref": "o"},
    ]))  # fmt: skip
    assert intervals.open_interval(SITE, LOG, "session") is None
    assert intervals.listing(SITE, LOG)["intervals"] == []
    opened = open_session(world)
    assert opened["interval"] == {"kind": "session", "number": 9101, "event": "opened"}


@pytest.mark.acceptance("LB-I1-S6")
def test_lb_i1_s6_the_chain_verifies_the_source_is_checked_once_and_nothing_is_signed(world, tmp_path):
    import io
    import zipfile

    from axiom.extensions.builtins.attest import exports, imports, obligations, service, signing

    _importer(world)
    open_session(world)
    chained = [
        {"occurred_at": "2025-01-01T08:00:00Z", "title": "a", "source_ref": "a", "source_hash": "h1"},
        {"occurred_at": "2025-01-02T08:00:00Z", "title": "b", "source_ref": "b", "source_hash": "h2", "source_prev": "h1"},
    ]  # fmt: skip
    assert imports.dry_run(SITE, LOG, _write(tmp_path, chained).read_bytes())["source_verified"] == "ok"
    broken = [chained[0], {**chained[1], "source_prev": "hX"}]
    assert imports.dry_run(SITE, LOG, _write(tmp_path, broken, "x.jsonl").read_bytes())["source_verified"] == "broken at b"
    assert imports.dry_run(SITE, LOG, _write(tmp_path, _old(1), "y.jsonl").read_bytes())["source_verified"] == "none"
    # A CHECK imported into the open session never meets the cadence obligation.
    before = obligations.evaluate(SITE, LOG, now=datetime.now(UTC))[0]["last_met_at"]
    rows = [{"occurred_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "c", "source_ref": "c",
             "source_hash": "h3", "source_prev": "h2", "entry_type": "CHECK", "interval": {"kind": "session", "number": 5000}}]  # fmt: skip
    world.clock["t"] = datetime.now(UTC) + timedelta(minutes=1)
    done = _commit(world, _write(tmp_path, chained + rows, "z.jsonl"))
    assert done["records"][0]["source_verified"] == "ok"
    assert obligations.evaluate(SITE, LOG, now=datetime.now(UTC))[0]["last_met_at"] == before
    assert service.verify_logbook(SITE, LOG, signing.public_keys()).ok
    b = [x for x in service.briefs(service.records(SITE, LOG)) if x["kind"] == "import"]
    assert {x["label"]["text"] for x in b} == {"Imported, not signed"} and {x["signer"] for x in b} == {None}
    exports.set_storage(None, base_dir=tmp_path / "storage")
    try:
        pkg = exports.build_package(SITE, LOG, exports.parse_filter({}), formats=("jsonl", "csv"))
    finally:
        exports.set_storage(None)
    names = zipfile.ZipFile(io.BytesIO(pkg.data)).namelist()
    csv_text = zipfile.ZipFile(io.BytesIO(pkg.data)).read([n for n in names if n.endswith(".csv")][0]).decode()
    assert ",import," in csv_text
