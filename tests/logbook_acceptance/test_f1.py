# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit F1: the public `axiom.attest` library.

Ticket: docs/working/logbook-units/F1.md in the product repo that holds the plan, built against
`docs/working/f1-attest-library-note.md`. F1a built drafts, query and verify (rows A1, A3, A6, A7
and A8); F1b builds confirm, sign and the two stubs (rows A2, A4 and A5, and A1's two new names).

`_sign` signs through `service`, so the read tests do not depend on the library's signing path
and A2 can compare the two paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.extensions.builtins.attest import service
from axiom.extensions.builtins.attest.service import AttestRefused, Signatory
from axiom.extensions.builtins.attest.skills import SPECS
from axiom.infra.principal import PrincipalContext

SRC = Path(__file__).resolve().parents[2] / "src"
CLI = ("cli",)
READ = ("cli", "mcp", "agent_tool")

#: The attest skills and their surfaces on main when F1a was built (2026-11-09). F1 adds none and
#: moves none; only the four read skills are on an agent surface.
SKILLS_BEFORE_F1 = {
    "attest.logbook_list": READ, "attest.logbook_validate": CLI, "attest.show": READ, "attest.verify": READ,
    "attest.export": CLI, "attest.anchor": CLI, "attest.obligations": READ, "attest.obligations_tick": CLI,
    "attest.new": CLI, "attest.sign": CLI,
    "attest.device_enroll": CLI, "attest.device_list": CLI, "attest.device_reclaim": CLI,
    "attest.device_retire": CLI, "attest.location_code": CLI, "attest.location_init": CLI,
    "attest.role_grant": CLI, "attest.role_list": CLI, "attest.role_revoke": CLI,
}  # fmt: skip

#: Skills later units add. F1's rule is that it moved none of the ones above; each later unit
#: lists its own here (L1: the propose skill and the tray read, both on agent surfaces).
ADDED_AFTER_F1 = {
    "attest.intervals": ("cli", "mcp", "agent_tool"),  # W3: a read
    "attest.propose": READ, "attest.drafts": READ,
    # L2: a person supplements on the CLI; software only proposes one.
    "attest.supplement": CLI, "attest.supplement_propose": ("mcp", "agent_tool"),
    # L3: a person acknowledges on the CLI; the unacknowledged list is a read everywhere.
    "attest.ack": CLI, "attest.unacknowledged": READ,
    # L4: a second person co-signs on the CLI; the pending list is a read everywhere.
    "attest.cosign": CLI, "attest.cosign_pending": READ,
    # X2: reads recorded, chain checked on the heartbeat
    "attest.access": READ, "attest.verify_all": CLI,
    # L5: a person seals on the CLI; software proposes a summary; the preview is a read everywhere.
    "attest.seal": CLI, "attest.seal_propose": ("mcp", "agent_tool"), "attest.seal_preview": READ,
    # L6: a person exports on the CLI; the preview is a read everywhere.
    "attest.export_preview": READ,
    "attest.seal_due": READ,
    "attest.export_blank": READ,
    "attest.sealed_and_packaged": READ,
    # #1804: a person imports and withdraws on the CLI; the dry run and the batches are reads.
    "attest.import": CLI, "attest.import_withdraw": CLI,
    "attest.import_preview": READ, "attest.imports": READ,
}  # fmt: skip

SITE = "site-a"
LOGBOOK = "demo_log"
ROUND = {"title": "Round check", "fields": {"reading": "ok", "walkdown": True}}
OP1 = PrincipalContext(handle="@op1:site-a", posture="sso", assured=True)
OP2 = PrincipalContext(handle="@op2:site-a", posture="sso", assured=True)


@pytest.fixture
def served(monkeypatch):
    """This node serves one site, so `site` may be left out."""
    monkeypatch.setenv("AXIOM_SERVED_SITES", SITE)


@pytest.fixture
def library_env(attest_db, bound_signer, roles, served, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(roles))
    return bound_signer


def _draft(**over):
    from axiom.attest import drafts

    args = {"logbook": LOGBOOK, "entry_type": "ROUND_CHECK", "content": ROUND, "origin": "human",
            "for_principal": OP1.handle, "entered_by": OP1}  # fmt: skip
    return drafts.create(**{**args, **over})


def _sign(signer, content=ROUND):
    """Sign one entry through the service, the path the library must agree with."""
    d = _draft(content=content)
    p = service.present(d.id, modality="cli")
    service.respond(p.presentation_id, principal=OP1, answer="sign", via="cli")
    who = Signatory(principal=OP1, roles=("operator",), display=OP1.handle)
    return service.sign(p.presentation_id, signatory=who, signer=signer).record


# -- acceptance rows ----------------------------------------------------------------------


@pytest.mark.acceptance("LB-F1-A1")
def test_f1_a1_the_library_imports_and_the_package_stays_light():
    """`from axiom.attest import drafts, confirm, sign, query, verify` imports, and `import
    axiom.attest` alone does not import `sqlalchemy` (checked in a subprocess)."""
    from axiom.attest import confirm, drafts, query, sign, verify

    assert all((drafts, confirm, sign, query, verify))
    code = "import sys, axiom.attest; sys.exit('sqlalchemy' in sys.modules)"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr


def _present_and_answer(answer="sign", *, principal=OP1, **draft):
    """Draft, present on the CLI and answer through the library; returns the presentation view."""
    from axiom.attest import confirm

    d = _draft(**draft)
    p = confirm.present(d.id, modalities=["cli"])
    confirm.respond(p.id, principal=principal, answer=answer, via="cli")
    return p


@pytest.mark.acceptance("LB-F1-A2")
def test_f1_a2_the_library_signs_what_the_service_signs(library_env):
    """The full path through the library on `demo_log` (draft, present, respond sign, sign, query,
    verify) produces a record byte-identical in `content`, `meaning` and `signer` to the same steps
    through `service`, and `verify.chain` reports ok with the right `head_seq`."""
    from axiom.attest import query, sign, verify

    by_service = _sign(library_env)
    result = sign.sign(_present_and_answer().id, principal=OP1)
    by_library = result.record
    assert result.status == "signed" and by_library["seq"] == 2
    for key in ("content", "meaning", "signer"):
        assert json.dumps(by_library[key], sort_keys=True) == json.dumps(
            by_service[key], sort_keys=True
        )
    assert by_library["source"] == "library" and by_service["source"] == "cli"
    assert [r["attestation_id"] for r in query.records(LOGBOOK)] == [
        by_service["attestation_id"], by_library["attestation_id"],
    ]  # fmt: skip
    report = verify.chain(SITE, LOGBOOK)
    assert report.ok and report.head_seq == 2


@pytest.mark.acceptance("LB-F1-A3")
def test_f1_a3_software_may_not_supply_an_observed_value(library_env):
    """`drafts.create` with `origin="agent:x"` and a value for an `observe` field raises `AttestRefused`
    (ADR-144), and with `origin="integration:ext"` the same."""
    for origin in ("agent:x", "integration:ext"):
        with pytest.raises(AttestRefused, match="observe"):
            _draft(origin=origin, entered_by=None)


@pytest.mark.acceptance("LB-F1-A4")
def test_f1_a4_sign_refuses_what_is_not_this_persons_act(library_env, tmp_path, monkeypatch):
    """`sign.sign` refuses when the principal is not the person the draft is for, when the answer
    was `hold` or `ask`, when the draft changed after presentation, and when the principal holds no
    listed role."""
    from axiom.attest import query, sign

    with pytest.raises(AttestRefused, match="is for"):
        sign.sign(_present_and_answer().id, principal=OP2)
    for answer in ("hold", "ask"):
        with pytest.raises(AttestRefused, match="only 'sign' signs"):
            sign.sign(_present_and_answer(answer).id, principal=OP1)
    p = _present_and_answer()
    d = service.presentation_context(p.id)["draft_id"]
    service.fill(d, {"note": "added after"}, by=Signatory(principal=OP1, roles=()))
    with pytest.raises(AttestRefused, match="changed"):
        sign.sign(p.id, principal=OP1)
    p = _present_and_answer(principal=OP2, for_principal=OP2.handle, entered_by=OP2)
    with pytest.raises(AttestRefused, match="holds no role"):
        sign.sign(p.id, principal=OP2)
    assert query.records(LOGBOOK) == []


@pytest.mark.acceptance("LB-F1-A5")
def test_f1_a5_the_stubs_refuse_and_name_their_unit():
    """F1 stubbed `sign.supplement` (`L2`) and `sign.cosign` (`L4`). Both are built now
    (`tests/logbook_acceptance/test_l2.py`, `test_l4.py`), so an unusable grant is refused
    rather than unimplemented, and nothing is signed."""
    from axiom.attest import sign

    with pytest.raises(AttestRefused):
        sign.supplement("rec-1", "correction", {"fields": {}}, "grant")
    with pytest.raises(AttestRefused):
        sign.cosign("rec-1", "grant")


@pytest.mark.acceptance("LB-F1-A6")
def test_f1_a6_query_filters_and_keeps_chain_order(library_env):
    """`query.records` filters by interval, meaning and text and returns records in `seq` order; a
    logbook with no records returns an empty list (the service does not check the registry on reads,
    so the wrapper does not add a rule)."""
    from axiom.attest import query

    _sign(library_env)
    _sign(
        library_env,
        content={"title": "Second look", "fields": {"reading": "fine", "walkdown": True}},
    )
    assert [r["seq"] for r in query.records(LOGBOOK)] == [1, 2]
    assert [r["seq"] for r in query.records(LOGBOOK, site=SITE, from_seq=2)] == [2]
    assert len(query.records(LOGBOOK, meanings=["performed"])) == 2
    assert query.records(LOGBOOK, meanings=["authored"]) == []
    assert [r["seq"] for r in query.records(LOGBOOK, text="SECOND LOOK")] == [2]
    assert query.records(LOGBOOK, text="no such words") == []
    assert query.records(LOGBOOK, interval=("run", "1")) == []
    assert query.records("no_such_logbook") == []
    assert query.records(LOGBOOK) == service.records(SITE, LOGBOOK)


@pytest.mark.acceptance("LB-F1-A7")
def test_f1_a7_verify_names_the_first_bad_record_and_verifies_a_slice(library_env):
    """`verify.chain` on a chain whose stored record was altered names `first_bad_seq`; a `from_seq`
    slice verifies."""
    from axiom.attest import verify
    from axiom.attest.chain import verify_chain

    for _ in range(3):
        _sign(library_env)
    whole = verify.chain(SITE, LOGBOOK)
    assert whole == service.verify_logbook(
        SITE, LOGBOOK, {library_env.key_id: library_env.public_bytes}
    )
    assert whole.ok and whole.head_seq == 3
    tail = verify.chain(SITE, LOGBOOK, from_seq=2)
    assert tail.ok and tail.checked == 2 and tail.head_seq == 3
    head = verify.chain(SITE, LOGBOOK, from_seq=1, to_seq=2)
    assert head.ok and head.checked == 2 and head.head_seq == 2
    keys = {library_env.key_id: library_env.public_bytes}
    altered = json.loads(json.dumps(service.records(SITE, LOGBOOK), default=str))
    altered[1]["content"]["title"] = "changed"
    assert verify_chain(altered, keys).first_bad_seq == 2


@pytest.mark.acceptance("LB-F1-A8")
def test_f1_a8_the_skills_and_their_surfaces_do_not_move():
    """The set of names in `attest.skills.SPECS` and their `surfaces` tuples is unchanged by this unit,
    and `attest.sign` and `attest.new` are still `("cli",)` only."""
    by_name = {s.name: tuple(s.surfaces) for s in SPECS}
    assert by_name == {**SKILLS_BEFORE_F1, **ADDED_AFTER_F1}


# -- drafts: argument handling (note questions 1, 9, 10) ----------------------------------


def test_a_draft_defaults_site_and_meaning_and_returns_a_view(library_env):
    from axiom.attest import drafts

    d = _draft()
    assert (d.site_id, d.logbook, d.entry_type, d.meaning) == (
        SITE,
        LOGBOOK,
        "ROUND_CHECK",
        "performed",
    )
    assert (d.origin, d.for_principal, d.status) == ("human", OP1.handle, "open")
    assert d.content["fields"]["reading"] == "ok"
    assert drafts.get(d.id) == d
    with pytest.raises(AttributeError):
        d.status = "signed"


def test_a_meaning_the_type_does_not_carry_is_refused(library_env):
    with pytest.raises(AttestRefused, match="meaning"):
        _draft(meaning="authored")


def test_a_site_the_node_does_not_serve_is_refused(library_env):
    with pytest.raises(AttestRefused, match="does not serve"):
        _draft(site="site-b")


def test_a_node_serving_several_sites_needs_the_site_named(library_env, monkeypatch):
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a,site-b")
    with pytest.raises(AttestRefused, match="site"):
        _draft()
    assert _draft(site=SITE).site_id == SITE


def test_a_human_origin_names_the_person_who_entered_it(library_env):
    """Note question 9: software may not reach the human path by declaring it."""
    with pytest.raises(AttestRefused, match="entered_by"):
        _draft(entered_by=None)
    with pytest.raises(AttestRefused, match="entered_by"):
        _draft(entered_by=OP2)


def test_no_observed_value_enters_without_the_person_who_entered_it(library_env):
    """Note Q9, as the owner answered it: whatever the origin, an observed value without
    `entered_by` is refused, and nothing is drafted."""
    from axiom.attest import query

    for origin in ("human", "agent:x", "integration:ext", "rule"):
        with pytest.raises(AttestRefused):
            _draft(origin=origin, entered_by=None)
    assert query.records(LOGBOOK) == []


def test_software_does_not_claim_a_person_entered_it(library_env):
    walk = {"title": "Proposed", "fields": {"walkdown": True}}
    with pytest.raises(AttestRefused, match="entered_by"):
        _draft(origin="integration:ext", content=walk, entered_by=OP1)
    d = _draft(origin="integration:ext", content=walk, entered_by=None)
    assert d.origin == "integration:ext" and d.provenance == {"walkdown": "integration:ext"}


# -- query and verify: argument handling (note questions 5, 6, 10) -------------------------


def test_reads_refuse_a_site_the_node_does_not_serve(library_env):
    from axiom.attest import query, verify

    with pytest.raises(AttestRefused, match="does not serve"):
        query.records(LOGBOOK, site="site-b")
    with pytest.raises(AttestRefused, match="does not serve"):
        verify.chain("site-b", LOGBOOK)


def test_an_interval_matches_kind_and_number_as_text():
    from axiom.attest.query import _in_interval

    rec = {"interval": {"kind": "run", "number": 1207, "event": "opened"}}
    assert _in_interval(rec, ("run", "1207")) and _in_interval(rec, ("run", 1207))
    assert not _in_interval(rec, ("run", "1208")) and not _in_interval(rec, ("shift", "1207"))
    assert not _in_interval({}, ("run", "1207"))


def test_a_slice_whose_predecessor_is_missing_fails_at_the_gap(library_env):
    from axiom.attest import verify

    _sign(library_env)
    report = verify.chain(SITE, LOGBOOK, from_seq=3)
    assert not report.ok and report.first_bad_seq == 2 and report.reason == "seq_gap"
    assert verify.chain(SITE, LOGBOOK, from_seq=2).ok  # the slice after the head is empty


def test_verify_refuses_bounds_that_make_no_slice():
    from axiom.attest import verify

    with pytest.raises(ValueError):
        verify.chain(SITE, LOGBOOK, from_seq=0)
    with pytest.raises(ValueError):
        verify.chain(SITE, LOGBOOK, from_seq=3, to_seq=2)


# -- confirm and sign: argument handling (note questions 2, 3, 4) --------------------------


def test_present_shows_each_modality_in_order(library_env):
    """Note Q2: the first is the view, the rest are in `.also`; all share one digest."""
    from axiom.attest import confirm

    p = confirm.present(_draft().id, modalities=["screen", "cli", "voice"])
    assert p.modality == "screen" and [a.modality for a in p.also] == ["cli", "voice"]
    assert {p.digest} == {a.digest for a in p.also}
    assert len({p.id, *(a.id for a in p.also)}) == 3
    assert p.rendered and p.forms and p.speakable and p.expires_at is not None
    assert all(a.also == () for a in p.also)
    with pytest.raises(AttributeError):
        p.digest = "x"


def test_present_refuses_an_empty_list_and_a_modality_the_type_does_not_allow(library_env):
    from axiom.attest import confirm

    d = _draft()
    with pytest.raises(AttestRefused, match="modalit"):
        confirm.present(d.id, modalities=[])
    with pytest.raises(AttestRefused, match="kiosk"):
        confirm.present(d.id, modalities=["kiosk"])


def test_sign_signs_the_one_presentation_the_person_answered(library_env):
    """Note Q2: nothing is signed as a set. The answer is to one presentation; only it signs."""
    from axiom.attest import confirm, sign

    p = confirm.present(_draft().id, modalities=["screen", "cli"])
    cli = p.also[0]
    confirm.respond(cli.id, principal=OP1, answer="sign", via="cli")
    with pytest.raises(AttestRefused, match="no answer"):
        sign.sign(p.id, principal=OP1)
    rec = sign.sign(cli.id, principal=OP1).record
    assert rec["evidence"]["presentation_digest"] == p.digest


def test_respond_passes_through_and_an_ask_with_a_correction_presents_again(library_env):
    """Note Q3: the service's `Response`, with `.next_presentation` after a correction."""
    from axiom.attest import confirm, sign

    p = confirm.present(_draft().id, modalities=["cli"])
    r = confirm.respond(p.id, principal=OP1, answer="ask", via="cli",
                        correction={"note": "one more thing"}, transcript="ask", latency_ms=5)  # fmt: skip
    assert isinstance(r, service.Response) and r.answer == "ask"
    nxt = r.next_presentation
    assert nxt is not None and nxt.digest != p.digest
    with pytest.raises(AttestRefused, match="changed"):
        confirm.respond(p.id, principal=OP1, answer="sign", via="cli")
    plain = confirm.respond(nxt.presentation_id, principal=OP1, answer="sign", via="cli")
    assert plain.next_presentation is None
    rec = sign.sign(nxt.presentation_id, principal=OP1).record
    assert rec["content"]["fields"]["note"] == "one more thing"


def test_respond_refuses_another_person_and_an_unknown_answer(library_env):
    from axiom.attest import confirm

    p = confirm.present(_draft().id, modalities=["cli"])
    with pytest.raises(AttestRefused, match="is for"):
        confirm.respond(p.id, principal=OP2, answer="sign", via="cli")
    with pytest.raises(AttestRefused, match="must be one of"):
        confirm.respond(p.id, principal=OP1, answer="yes", via="cli")


def test_software_proposes_and_only_a_persons_answer_signs(library_env):
    """Must refuse: an integration's draft ends in a record only after the person entered the
    observed value and answered `sign`; the record keeps the software origin."""
    from axiom.attest import confirm, drafts, sign

    d = drafts.create(LOGBOOK, "ROUND_CHECK", {"title": "Proposed", "fields": {"walkdown": True}},
                      "integration:ext", OP1.handle)  # fmt: skip
    service.fill(d.id, {"reading": "ok"}, by=Signatory(principal=OP1, roles=()))
    p = confirm.present(d.id, modalities=["cli"])
    with pytest.raises(AttestRefused, match="no answer"):
        sign.sign(p.id, principal=OP1)
    confirm.respond(p.id, principal=OP1, answer="sign", via="cli")
    rec = sign.sign(p.id, principal=OP1).record
    assert rec["origin"] == "integration:ext" and rec["signer"]["kind"] == "human"


def test_roles_come_from_the_node_and_cannot_be_passed(library_env):
    """Note Q4: `sign.sign` takes the person, never their roles or a signer."""
    import inspect

    from axiom.attest import sign

    params = set(inspect.signature(sign.sign).parameters)
    assert params == {"presentation_id", "principal", "grant", "occurred_at"}
    rec = sign.sign(_present_and_answer().id, principal=OP1).record
    assert rec["signer"]["authority"]["roles"] == ["operator"]
    assert rec["signer"]["display"] == OP1.handle


def test_a_signed_draft_cannot_be_signed_again(library_env):
    from axiom.attest import sign

    p = _present_and_answer()
    sign.sign(p.id, principal=OP1)
    with pytest.raises(AttestRefused, match="already signed"):
        sign.sign(p.id, principal=OP1)
