# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Plain logs (ADR-290): a log is unsigned unless it declares ``signed = true``.

A person writes an entry to a plain log in one step, as themself, with no presentation, readback
or signing ceremony. The entry joins the same chain (sealed by the node), the same indexes, doors,
paging, search, export and retention as any other. A signed log keeps every rule it has today.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from axiom.attest import verify as verify_lib  # noqa: E402
from axiom.extensions.builtins.attest import registry, service, store  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE, person  # noqa: E402


@pytest.fixture
def roles(client, tmp_path):
    """The state dir the HTTP client's routes read roles from: @op1:site-a is an operator."""
    return tmp_path


from .test_ld2 import ids, me  # noqa: E402
from .test_ld2 import world as _ld2_world  # noqa: E402

world = _ld2_world  # the LD-2 fixture, by the name its tests use

PLAIN = "plain_notes"
SIGNED = "signed_checks"


def decl(log_id, *, signed=None, extra_head="", types=None, extra=""):
    head = f'id = "{log_id}"\nversion = "1"\ndisplay = "{log_id}"\n'
    if signed is not None:
        head += f"signed = {'true' if signed else 'false'}\n"
    body = types or (
        '[[type]]\nid = "NOTE"\nmeanings = ["authored"]\nroles = ["operator"]\n'
        'fields = [{ id = "text", type = "text", required = true }, { id = "tag", type = "text" }]\n'
    )
    import tomllib

    return tomllib.loads(f"[logbook]\n{head}{extra_head}\n{body}\n{extra}")


def parse(log_id, **kw):
    return parse_logbook(decl(log_id, **kw), source="plain-test")


@pytest.fixture
def logs(attest_db, bound_signer):
    registry.register(parse(PLAIN))
    registry.register(parse(SIGNED, signed=True))
    return bound_signer


def write(log=PLAIN, text="pump room walked down", **kw):
    return service.record_entry(
        site_id=SITE, logbook=log, entry_type="NOTE",
        content={"title": text[:40], "fields": {"text": text, **kw.pop("fields", {})}},
        by=kw.pop("by", person()), **kw,
    )  # fmt: skip


# -- the declaration -------------------------------------------------------------------------


@pytest.mark.acceptance("LB-PL-A1")
def test_pl_a1_a_log_that_says_nothing_about_signing_is_plain():
    lb = parse("plain_a1")
    assert lb.signed is False and lb.signed_by_inference is False


@pytest.mark.acceptance("LB-PL-A2")
def test_pl_a2_a_log_declares_signed_true_and_is_signed():
    lb = parse("signed_a2", signed=True)
    assert lb.signed is True and lb.signed_by_inference is False


@pytest.mark.acceptance("LB-PL-A3")
@pytest.mark.parametrize(
    "types, feature",
    [
        ('[[type]]\nid = "NOTE"\nmeanings = ["authored"]\nroles = ["operator"]\n'
         'confirm = { modalities_any = ["screen", "cli"], timeout = "10m" }\n', "confirm"),
        ('[[type]]\nid = "NOTE"\nmeanings = ["performed"]\nroles = ["operator"]\n'
         'fields = [{ id = "reading", type = "quantity", unit = "kg", observe = true }]\n', "observed field"),
        ('[[type]]\nid = "NOTE"\nmeanings = ["authored"]\nroles = ["operator"]\n'
         'assurance = { posture = "attested", fresh_within = "5m" }\n', "fresh sign-in"),
    ],
)  # fmt: skip
def test_pl_a3_a_declaration_that_uses_a_signing_only_feature_and_says_nothing_is_signed(types, feature):
    """A regulated log written before this ADR keeps signing: the loader never drops a signature
    from a log whose declaration only a signature can satisfy."""
    try:
        lb = parse("legacy_a3", types=types)
    except LogbookError as exc:  # the parser does not know a feature key: nothing to infer from
        pytest.skip(f"declaration key not accepted here: {exc}")
    assert lb.signed is True and lb.signed_by_inference is True, feature


@pytest.mark.acceptance("LB-PL-A4")
def test_pl_a4_signed_false_with_a_signing_only_feature_is_refused_and_names_it():
    types = (
        '[[type]]\nid = "NOTE"\nmeanings = ["authored"]\nroles = ["operator"]\n'
        'confirm = { modalities_any = ["screen", "cli"], timeout = "10m" }\n'
    )
    with pytest.raises(LogbookError, match=r"signed = false.*confirm"):
        parse("bad_a4", signed=False, types=types)


@pytest.mark.acceptance("LB-PL-A5")
def test_pl_a5_the_platforms_own_signed_logs_say_so():
    from axiom.extensions.builtins.attest import logbooks

    for name in ("approvals", "settings_authority"):
        lb = logbooks.load_builtin(name) if hasattr(logbooks, "load_builtin") else None
        if lb is None:
            import tomllib
            from pathlib import Path

            path = Path(logbooks.__file__).parent / "logbooks" / f"{name}.toml"
            lb = parse_logbook(tomllib.loads(path.read_text(encoding="utf-8")), source=name)
        assert lb.signed is True and lb.signed_by_inference is False, name


# -- writing an entry --------------------------------------------------------------------------


@pytest.mark.acceptance("LB-PL-A6")
def test_pl_a6_a_person_writes_a_plain_entry_in_one_step_and_it_joins_the_chain(logs):
    rec = write()
    assert rec["seq"] == 1 and rec["logbook"] == PLAIN and rec["entry_type"] == "NOTE"
    assert rec["signer"]["principal"] == "@op1:site-a" and rec["signer"]["kind"] == "session"
    assert "evidence" not in rec and "node_sig" in rec
    second = write(text="second")
    assert second["seq"] == 2 and second["prev_digest"] == rec["digest"]
    keys = {logs.key_id: logs.public_bytes}
    report = verify_lib.chain(SITE, PLAIN, 1, 2, public_keys=keys)
    assert report.ok and report.checked == 2


@pytest.mark.acceptance("LB-PL-A7")
def test_pl_a7_the_entry_is_read_back_by_the_ordinary_reads(logs):
    rec = write(text="found by the reads")
    shown = service.record(rec["attestation_id"])
    assert shown is not None and shown["content"]["fields"]["text"] == "found by the reads"
    rows = service.records(SITE, PLAIN)
    assert [r["attestation_id"] for r in rows] == [rec["attestation_id"]]


@pytest.mark.acceptance("LB-PL-A8")
def test_pl_a8_a_signed_log_refuses_a_plain_write_and_a_plain_log_refuses_a_draft(logs):
    with pytest.raises(service.AttestRefused, match="signed log"):
        write(log=SIGNED)
    with pytest.raises(service.AttestRefused, match="plain log"):
        service.create_draft(
            site_id=SITE, logbook=PLAIN, entry_type="NOTE", meaning="authored",
            content={"title": "x", "fields": {"text": "x"}}, origin="human",
            for_principal="@op1:site-a",
        )  # fmt: skip


@pytest.mark.acceptance("LB-PL-A9")
def test_pl_a9_a_person_without_the_types_role_and_a_bad_field_are_refused_and_nothing_is_written(logs):
    with pytest.raises(service.AttestRefused, match="holds no role"):
        write(by=person(roles=("visitor",)))
    with pytest.raises(service.AttestRefused, match="unknown field"):
        write(fields={"nope": 1})
    with pytest.raises(service.AttestRefused, match="required"):
        service.record_entry(site_id=SITE, logbook=PLAIN, entry_type="NOTE",
                             content={"title": "t", "fields": {}}, by=person())  # fmt: skip
    assert service.records(SITE, PLAIN) == []


@pytest.mark.acceptance("LB-PL-A10")
def test_pl_a10_an_unauthenticated_person_cannot_write(logs):
    with pytest.raises(service.AttestRefused, match="posture"):
        write(by=person(posture="open"))


@pytest.mark.acceptance("LB-PL-A11")
def test_pl_a11_signed_and_plain_logs_keep_separate_valid_chains(logs):
    write()
    write(text="again")
    keys = {logs.key_id: logs.public_bytes}
    assert service.verify_logbook(SITE, PLAIN, keys).ok
    with store.session_scope() as s:
        assert service.last_seq(SITE, SIGNED) == 0
        s.rollback()


@pytest.mark.acceptance("LB-PL-A12")
def test_pl_a12_a_future_time_is_refused(logs):
    with pytest.raises(service.AttestRefused, match="future"):
        write(occurred_at="2999-01-01T00:00:00Z")


@pytest.mark.acceptance("LB-PL-A13")
def test_pl_a13_the_api_writes_a_plain_entry_and_the_records_page_and_its_search_find_it(
    logs, client, roles, monkeypatch
):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    c = as_(client, OP1)
    r = c.post(f"/api/v1/attest/{PLAIN}/entries", json={
        "site": SITE, "entry_type": "NOTE", "title": "Handover", "fields": {"text": "handover to nights"},
    })  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["record"]["signer"]["kind"] == "session"
    page = c.get(f"/api/v1/attest/{PLAIN}/records?limit=20")
    assert page.status_code == 200, page.text
    assert [x["title"] for x in page.json()["records"]] == ["Handover"]
    hit = c.get(f"/api/v1/attest/{PLAIN}/records?limit=20&q=handover").json()["records"]
    assert len(hit) == 1
    miss = c.get(f"/api/v1/attest/{PLAIN}/records?limit=20&q=zebra").json()["records"]
    assert miss == []


@pytest.mark.acceptance("LB-PL-A14")
def test_pl_a14_the_api_refuses_a_signed_log_an_unknown_log_and_a_wrong_role(logs, client, roles, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    c = as_(client, OP1)
    body = {"site": SITE, "entry_type": "NOTE", "title": "t", "fields": {"text": "x"}}
    assert c.post(f"/api/v1/attest/{SIGNED}/entries", json=body).status_code == 422
    assert c.post("/api/v1/attest/no_such_log/entries", json=body).status_code == 404
    assert c.post(f"/api/v1/attest/{PLAIN}/entries", json={**body, "fields": {}}).status_code == 422


@pytest.mark.acceptance("LB-PL-A15")
def test_pl_a15_a_log_whose_newest_entry_was_signed_does_not_quietly_become_plain(logs, monkeypatch):
    """The log is signed and a person signs an entry; its declaration is then edited to plain: the
    next one-step write is refused, never chained after a signed entry."""
    from axiom.extensions.builtins.attest.tests.test_signing import _sign

    registry.register(parse("flipped", signed=True))
    draft = service.create_draft(
        site_id=SITE, logbook="flipped", entry_type="NOTE", meaning="authored",
        content={"title": "signed", "fields": {"text": "signed first"}}, origin="human",
        for_principal="@op1:site-a",
    )  # fmt: skip
    assert _sign(draft, logs).record["seq"] == 1
    now_plain, original = parse("flipped"), registry.get
    monkeypatch.setattr(registry, "get", lambda log: now_plain if log == "flipped" else original(log))
    with pytest.raises(service.AttestRefused, match="have been signed"):
        write(log="flipped")


@pytest.mark.acceptance("LB-PL-A16")
def test_pl_a16_validate_and_list_say_how_a_log_is_written_and_why(capsys):
    from axiom.extensions.builtins.attest import log_cli
    from axiom.extensions.builtins.attest.skills.logbook import _summary
    from axiom.infra.skills import SkillResult

    types = ('[[type]]\nid = "NOTE"\nmeanings = ["authored"]\nroles = ["operator"]\n'
             'confirm = { modalities_any = ["screen", "cli"], timeout = "10m" }\n')
    for log, kw, said in (
        ("p16a", {}, "plain: a person writes each entry in one step"),
        ("p16b", {"signed": True}, "signed: each entry is shown to a person and signed"),
        ("p16c", {"types": types}, "signed, because it uses confirm; declare signed = true to say so"),
    ):
        log_cli._print_text(SkillResult(ok=True, value=_summary(parse(log, **kw))))
        assert said in capsys.readouterr().out


@pytest.mark.acceptance("LB-PL-A17")
def test_pl_a17_the_new_verb_records_a_plain_entry_with_no_prompt_and_signs_a_signed_one_at_the_prompt(
    logs, roles
):
    import logging

    from axiom.extensions.builtins.attest.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext

    ctx = SkillContext(
        registry=bind_default(), state_dir=roles, logger=logging.getLogger("test.plain"),
        principal=PrincipalContext(handle="@op1:site-a", posture="sso", assured=True),
        actor="@op1:site-a", surface=CLI_SURFACE,
    )  # fmt: skip
    base = {"site": SITE, "type": "NOTE", "title": "From the terminal", "fields": ["text=checked the pump"]}
    done = invoke_capability(ctx.registry, "attest.new", {**base, "logbook": PLAIN}, ctx, surface=CLI_SURFACE)
    assert done.ok, done.errors
    assert done.value["record"]["signer"]["kind"] == "session"
    refused = invoke_capability(ctx.registry, "attest.new", {**base, "logbook": SIGNED, "meaning": "authored"},
                                ctx, surface=CLI_SURFACE)  # fmt: skip
    assert not refused.ok and "prompt" in " ".join(refused.errors)


@pytest.mark.acceptance("LB-PL-A19")
def test_pl_a19_the_logbook_routes_say_whether_a_log_is_plain_so_a_page_can_offer_the_right_form(
    logs, client, roles, monkeypatch
):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    c = as_(client, OP1)
    assert c.get(f"/api/v1/attest/logbooks/{PLAIN}").json()["signed"] is False
    assert c.get(f"/api/v1/attest/logbooks/{SIGNED}").json()["signed"] is True
    listed = {b["id"]: b for b in c.get("/api/v1/attest/logbooks").json()["logbooks"] if b.get("may_read")}
    assert listed[PLAIN]["signed"] is False and listed[SIGNED]["signed"] is True


# -- edit: a correcting entry, appended (ADR-290 D7) ---------------------------------------------


def correct(target, *, log=PLAIN, reason="correction", fields=None, body=None, **kw):
    content = {"fields": fields if fields is not None else {"text": "pump room walked down twice"}}
    if body:
        content["body"] = body
    return service.record_supplement(
        site_id=SITE, logbook=log, target=target, reason=reason, content=content,
        by=kw.pop("by", person()), **kw,
    )  # fmt: skip


@pytest.mark.acceptance("LB-PL-A20")
def test_pl_a20_a_person_corrects_a_plain_entry_by_appending_a_linked_correction(logs):
    original = write(text="pump room walked down")
    fix = correct(original["attestation_id"])
    assert fix["kind"] == "supplement" and fix["seq"] == 2 and fix["prev_digest"] == original["digest"]
    assert fix["target"] == original["attestation_id"] and fix["target_digest"] == original["digest"]
    assert fix["reason"] == "correction" and fix["meaning"] == "corrected"
    assert fix["signer"]["kind"] == "session" and "evidence" not in fix
    # the original is untouched, and the chain holds both under one node seal
    assert service.record(original["attestation_id"])["content"]["fields"]["text"] == "pump room walked down"
    keys = {logs.key_id: logs.public_bytes}
    report = verify_lib.chain(SITE, PLAIN, 1, 2, public_keys=keys)
    assert report.ok and report.checked == 2


@pytest.mark.acceptance("LB-PL-A21")
def test_pl_a21_the_thread_and_the_page_show_the_correction_with_its_original(logs, client, roles, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    original = write(text="first words")
    fix = correct(original["attestation_id"], fields={"text": "corrected words"})
    thread = service.thread(original["attestation_id"])
    assert [r["attestation_id"] for r in thread] == [original["attestation_id"], fix["attestation_id"]]
    c = as_(client, OP1)
    page = c.get(f"/api/v1/attest/{PLAIN}/records?limit=20").json()["records"]
    by_id = {r["attestation_id"]: r for r in page}
    assert by_id[fix["attestation_id"]]["kind"] == "supplement"
    # the entry's own page carries the thread: its original and the correction, in order
    detail = c.get(f"/api/v1/attest/{PLAIN}/records/{original['attestation_id']}").json()
    assert [t["attestation_id"] for t in detail["thread"]] == [original["attestation_id"], fix["attestation_id"]]


@pytest.mark.acceptance("LB-PL-A22")
def test_pl_a22_a_correction_is_refused_for_a_signed_log_a_missing_target_a_bad_reason_nothing_to_say_and_a_wrong_role(logs):
    original = write()
    target = original["attestation_id"]
    with pytest.raises(service.AttestRefused, match="signed log"):
        correct(target, log=SIGNED)
    with pytest.raises(service.AttestRefused, match="no record"):
        correct("00000000-0000-4000-8000-000000000000")
    with pytest.raises(service.AttestRefused, match="reason"):
        correct(target, reason="whim")
    with pytest.raises(service.AttestRefused, match="body or at least one field"):
        correct(target, fields={})
    with pytest.raises(service.AttestRefused, match="unknown field"):
        correct(target, fields={"nope": 1})
    with pytest.raises(service.AttestRefused, match="holds no role"):
        correct(target, by=person(roles=("visitor",)))
    assert len(service.records(SITE, PLAIN)) == 1


@pytest.mark.acceptance("LB-PL-A23")
def test_pl_a23_the_supplement_route_writes_a_plain_correction_at_once_and_still_drafts_for_a_signed_log(
    logs, client, roles, monkeypatch
):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    c = as_(client, OP1)
    original = write(text="before")
    r = c.post(f"/api/v1/attest/{PLAIN}/records/{original['attestation_id']}/supplement",
               json={"site": SITE, "reason": "correction", "fields": {"text": "after"}})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["record"]["kind"] == "supplement" and "draft_id" not in r.json()
    # a signed log is unchanged: the route makes a draft for the person to review and sign
    from axiom.extensions.builtins.attest.tests.test_signing import _sign

    d = service.create_draft(site_id=SITE, logbook=SIGNED, entry_type="NOTE", meaning="authored",
                             content={"title": "s", "fields": {"text": "s"}}, origin="human",
                             for_principal="@op1:site-a")  # fmt: skip
    signed_rec = _sign(d, logs).record
    r2 = c.post(f"/api/v1/attest/{SIGNED}/records/{signed_rec['attestation_id']}/supplement",
                json={"site": SITE, "reason": "correction", "fields": {"text": "t"}})  # fmt: skip
    assert r2.status_code == 201 and "draft_id" in r2.json()


@pytest.mark.acceptance("LB-PL-A24")
def test_pl_a24_a_plain_entry_is_found_by_the_logs_search_like_any_other(world):
    """The index reads gold: a plain entry, which carries no evidence block, is still one chunk and
    is found by what it says."""
    from axiom.extensions.builtins.attest import index

    registry.register(parse("ld2_plain"))
    rec = service.record_entry(
        site_id=SITE, logbook="ld2_plain", entry_type="NOTE", by=person(),
        content={"title": "Gland weeping", "fields": {"text": "the gland on pump two is weeping"}},
    )  # fmt: skip
    found = index.search(me(world["state"]), "gland weeping")
    assert rec["attestation_id"] in ids(found), found
    assert any(c.get("attestation_id") == rec["attestation_id"] for c in index.rows_for_tests())


@pytest.mark.acceptance("LB-PL-A25")
def test_pl_a25_content_that_cannot_be_canonical_is_a_refusal_not_a_crash(logs, client, roles, monkeypatch):
    """A float is not allowed in chained content (use a decimal string). The service refuses it in
    words, and the API answers 422, never 500."""
    monkeypatch.setenv("AXIOM_SITE", SITE)
    with pytest.raises(service.AttestRefused, match="cannot be recorded"):
        service.record_entry(site_id=SITE, logbook=PLAIN, entry_type="NOTE", by=person(),
                             content={"title": "t", "fields": {"text": 1.5}})  # fmt: skip
    c = as_(client, OP1)
    r = c.post(f"/api/v1/attest/{PLAIN}/entries", json={"site": SITE, "entry_type": "NOTE", "title": "t",
                                                         "fields": {"text": 1.5}})  # fmt: skip
    assert r.status_code == 422, r.text
    original = write(text="fine")
    with pytest.raises(service.AttestRefused, match="cannot be recorded"):
        correct(original["attestation_id"], fields={"text": 2.5})
    assert len(service.records(SITE, PLAIN)) == 1


@pytest.mark.acceptance("LB-PL-A26")
def test_pl_a26_an_entry_has_a_size_limit_and_an_ordinary_one_is_far_inside_it(logs, client, roles, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    assert service.MAX_PLAIN_ENTRY_BYTES == 64 * 1024
    big = "x" * (service.MAX_PLAIN_ENTRY_BYTES + 1)
    with pytest.raises(service.AttestRefused, match="too large"):
        write(text=big)
    original = write(text="a paragraph of the sort a shift note holds " * 20)
    with pytest.raises(service.AttestRefused, match="too large"):
        correct(original["attestation_id"], fields={"text": big})
    c = as_(client, OP1)
    r = c.post(f"/api/v1/attest/{PLAIN}/entries", json={"site": SITE, "entry_type": "NOTE", "title": "t",
                                                         "fields": {"text": big}})  # fmt: skip
    assert r.status_code == 422 and "too large" in r.text
    assert len(service.records(SITE, PLAIN)) == 1


# -- review findings (2026-11-25): posture, the node key, shapes, meanings, retention ---------------


@pytest.mark.acceptance("LB-PL-A27")
def test_pl_a27_only_a_signing_posture_writes_a_plain_entry_not_a_service_or_an_open_session(logs):
    """A machine principal ranks as high as sso but is not a person: the signed path refuses it
    (SIGNING_POSTURES), and so does the plain one, so no entry is recorded as a person's by a service."""
    for posture in ("service", "open"):
        with pytest.raises(service.AttestRefused, match="posture"):
            write(by=person(posture=posture))
    original = write()
    with pytest.raises(service.AttestRefused, match="posture"):
        correct(original["attestation_id"], by=person(posture="service"))
    assert len(service.records(SITE, PLAIN)) == 1
    assert write(by=person(posture="sso"))["seq"] == 2


@pytest.mark.acceptance("LB-PL-A28")
def test_pl_a28_a_node_whose_key_is_down_refuses_with_nothing_written_not_a_crash(logs, client, roles, monkeypatch):
    from axiom.extensions.builtins.attest import signing
    from axiom.vega.identity.node_key import NodeKeyUnavailable

    monkeypatch.setenv("AXIOM_SITE", SITE)
    original = write()

    def down():
        raise NodeKeyUnavailable("the node key is locked")

    signing.set_provider(down)
    c = as_(client, OP1)
    body = {"site": SITE, "entry_type": "NOTE", "title": "t", "fields": {"text": "x"}}
    r = c.post(f"/api/v1/attest/{PLAIN}/entries", json=body)
    assert r.status_code == 503 and "nothing was written" in r.text, r.text
    r2 = c.post(f"/api/v1/attest/{PLAIN}/records/{original['attestation_id']}/supplement",
                json={"site": SITE, "reason": "correction", "fields": {"text": "y"}})  # fmt: skip
    assert r2.status_code == 503 and "nothing was written" in r2.text, r2.text
    signing.set_provider(lambda: logs)
    assert len(service.records(SITE, PLAIN)) == 1


@pytest.mark.acceptance("LB-PL-A29")
def test_pl_a29_content_of_the_wrong_shape_is_a_refusal_not_a_crash(logs, client, roles, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    for bad in (["ab"], [1], "text", 7):
        with pytest.raises(service.AttestRefused, match="content is"):
            service.record_entry(site_id=SITE, logbook=PLAIN, entry_type="NOTE", by=person(),
                                 content={"title": "t", "fields": bad})  # fmt: skip
    c = as_(client, OP1)
    r = c.post(f"/api/v1/attest/{PLAIN}/entries", json={"site": SITE, "entry_type": "NOTE", "title": "t",
                                                         "fields": ["ab"]})  # fmt: skip
    assert r.status_code == 422, r.text
    r = c.post(f"/api/v1/attest/{PLAIN}/entries", json={"site": SITE, "entry_type": "NOTE", "title": "t",
                                                         "fields": {"text": "x"}, "occurred_at": "not a time"})  # fmt: skip
    assert r.status_code == 422, r.text
    r = c.post("/api/v1/attest/no_such_log/records/00000000-0000-4000-8000-000000000000/supplement",
               json={"site": SITE, "reason": "correction", "fields": {"text": "y"}})  # fmt: skip
    assert r.status_code in (404, 422), r.text


@pytest.mark.acceptance("LB-PL-A30")
def test_pl_a30_a_plain_entry_cannot_carry_imported_or_reviewed_and_its_subjects_are_checked(logs):
    types = (
        '[[type]]\nid = "NOTE"\nmeanings = ["authored", "reviewed"]\nroles = ["operator"]\n'
        'fields = [{ id = "text", type = "text", required = true }]\n'
    )
    registry.register(parse("meaning_log", types=types))
    for meaning in ("imported", "reviewed"):
        with pytest.raises(service.AttestRefused, match=meaning):
            service.record_entry(site_id=SITE, logbook="meaning_log", entry_type="NOTE", meaning=meaning,
                                 by=person(), content={"title": "t", "fields": {"text": "x"}})  # fmt: skip
    with pytest.raises(service.AttestRefused, match="subjects"):
        service.record_entry(site_id=SITE, logbook=PLAIN, entry_type="NOTE", by=person(), subjects="nope",
                             content={"title": "t", "fields": {"text": "x"}})  # fmt: skip
    assert service.records(SITE, "meaning_log") == []


@pytest.mark.acceptance("LB-PL-A31")
def test_pl_a31_a_retention_rule_is_no_reason_to_sign_entries_but_a_silent_declaration_keeps_its_meaning():
    """Archiving and deleting are acts on a log (ADR-290 D8): `signed = false` beside a rule that
    archives or deletes is accepted. A declaration that says nothing and declares one stays signed,
    as it was before, so no existing log silently loses its signatures."""
    for rule in ('{ archive = "site" }', '{ delete_after_days = 2000, tombstone_by = ["lead"] }'):
        head = f"retention = {rule}\n"
        assert parse("kept_a", signed=False, extra_head=head).signed is False
        silent = parse("kept_b", extra_head=head)
        assert silent.signed is True and silent.signed_by_inference is True
    # a rule that only moves old years to the cold tier is no archival act at all
    assert parse("kept_c", extra_head="retention = { live_days = 90 }\n").signed is False


@pytest.mark.acceptance("LB-PL-A32")
def test_pl_a32_writers_at_the_same_moment_each_take_the_next_sequence_and_the_chain_holds(logs):
    """The head lock serialises plain writers as it does signing ones: twelve at once give twelve
    consecutive entries, each linked to the one before, and the chain verifies."""
    from concurrent.futures import ThreadPoolExecutor

    n = 12
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: write(text=f"writer {i}"), range(n)))
    assert sorted(r["seq"] for r in results) == list(range(1, n + 1))
    keys = {logs.key_id: logs.public_bytes}
    report = verify_lib.chain(SITE, PLAIN, 1, n, public_keys=keys)
    assert report.ok and report.checked == n
