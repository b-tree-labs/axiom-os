# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Supplements and the entry-action mechanism (unit L2): the migration, the unchanged statement
digest, the presenter, the thread and the skills. The acceptance rows live in
``tests/logbook_acceptance/test_l2.py``."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from axiom.attest.canonical import digest
from axiom.extensions.builtins.attest import proposals, service
from axiom.extensions.builtins.attest.skills import SPECS, bind_default
from axiom.infra.db import engine_for
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext

from .test_signing import SITE, _round_check, _sign, person

ME = "@op1:site-a"


def test_migration_adds_the_entry_action_columns_and_rewrites_nothing(attest_db):
    engine, _ = engine_for("attest")
    with engine.connect() as conn:
        cols = {
            (r.table_name, r.column_name): (
                r.is_nullable,
                r.column_default,
                r.character_maximum_length,
            )
            for r in conn.execute(
                text(
                    "SELECT table_name, column_name, is_nullable, column_default, "
                    "character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = :s AND column_name IN ('kind', 'target', 'reason')"
                ),
                {"s": attest_db},
            )
        }
        indexes = set(
            conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = :s"), {"s": attest_db}
            ).scalars()
        )
    assert cols[("attest_records", "target")][:1] == ("YES",)
    assert cols[("attest_records", "target")][2] == 36
    assert cols[("attest_drafts", "kind")][0] == "NO"
    assert "statement" in cols[("attest_drafts", "kind")][1]
    assert cols[("attest_drafts", "target")][0] == "YES"
    assert cols[("attest_drafts", "reason")][2] == 16
    assert "ix_attest_records_target" in indexes


def test_a_statement_digest_is_unchanged():
    """Pinned before L2: a statement's presented form gains no key."""
    d = SimpleNamespace(
        site_id="site-a", logbook="demo_log", entry_type="ROUND_CHECK", meaning="performed",
        content={"title": "Round check", "fields": {"reading": "ok", "walkdown": True}},
        origin="human", for_principal=ME, provenance={"reading": ME, "walkdown": ME},
        kind="statement", target=None, reason=None,
    )  # fmt: skip
    assert digest(service._statement(d, "1")) == (
        "9defa7de287ff1de3ef281aac2449aa88d14655795498bff8992fc70efb48d5c"
    )


def test_an_existing_statement_record_keeps_its_shape(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer).record
    assert rec["kind"] == "statement" and rec["entry_type"] == "ROUND_CHECK"
    assert not {"target", "target_digest", "reason"} & set(rec)
    assert service.brief(rec)["kind"] == "statement" and service.brief(rec)["target"] is None


def _supplement_draft(target, **over):
    kw = {"site_id": SITE, "target": target, "reason": "correction",
          "content": {"fields": {"reading": "fixed"}}, "origin": "human", "for_principal": ME}  # fmt: skip
    return service.create_supplement_draft(**{**kw, **over})


def test_the_card_names_the_original_and_the_reason(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer).record
    d = _supplement_draft(
        rec["attestation_id"], content={"body": "wrong line", "fields": {"reading": "fixed"}}
    )
    draft = service.draft(d)
    assert (draft["kind"], draft["target"], draft["reason"]) == (
        "supplement", rec["attestation_id"], "correction"
    )  # fmt: skip
    assert draft["entry_type"] == "ROUND_CHECK" and draft["meaning"] == "corrected"
    p = service.present(d)
    first, second, *rest = p.rendered.splitlines()
    assert first == f"Supplement to #{rec['seq']} ({rec['digest'][:8]}), Correction"
    assert second == "  Original: ROUND_CHECK · Round check"
    assert rest == ["  reading: fixed", "  Note: wrong line"]
    two = service.present(
        _supplement_draft(rec["attestation_id"], content={"title": "Fix", "body": "one\ntwo"})
    )
    assert two.rendered.splitlines()[2:] == ["  Title: Fix", "  Note: one", "    two"]
    assert p.forms["line"] == first
    assert p.statement["target_digest"] == rec["digest"] and p.statement["entry_type"] is None
    assert p.presenter.endswith(".supplement@1")


def test_a_supplement_needs_none_of_the_types_required_fields(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer).record
    sup = _sign(_supplement_draft(rec["attestation_id"], reason="addition",
                                  content={"fields": {"note": "added"}}), node_signer).record  # fmt: skip
    assert sup["content"] == {"fields": {"note": "added"}}


def test_the_thread_follows_supplements_of_supplements_in_chain_order(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer).record
    s1 = _sign(_supplement_draft(rec["attestation_id"]), node_signer).record
    other = _sign(_round_check(reading="other"), node_signer).record
    s2 = _sign(_supplement_draft(s1["attestation_id"], reason="clarification",
                                 content={"body": "why"}), node_signer).record  # fmt: skip
    ids = [b["attestation_id"] for b in service.thread(rec["attestation_id"])]
    assert ids == [rec["attestation_id"], s1["attestation_id"], s2["attestation_id"]]
    assert [b["attestation_id"] for b in service.thread(other["attestation_id"])] == [
        other["attestation_id"]
    ]
    assert service.thread("00000000-0000-0000-0000-000000000000") == []


# -- skills -------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, node_signer):
    from axiom.extensions.builtins.attest import roles, signing

    roles.grant(ME, SITE, "operator", by="@admin:site-a", state_dir=tmp_path)
    signing.set_provider(lambda: node_signer)
    yield tmp_path
    signing.reset_provider()


def _ctx(state_dir, answers=None, *, actor=None, surface="cli"):
    replies = list(answers or [])
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.l2"),
        principal=PrincipalContext(handle=ME, posture="sso", assured=True),
        user_prompt=(lambda _t: replies.pop(0)) if answers is not None else None,
        actor=actor, surface=surface,
    )  # fmt: skip


def _invoke(name, params, c):
    return c.registry.invoke(name, params, c)


def test_attest_supplement_signs_at_the_prompt_and_only_there(attest_db, state, node_signer):
    rec = _sign(_round_check(), node_signer).record
    params = {"attestation_id": rec["attestation_id"], "reason": "correction",
              "fields": ["reading=fixed"]}  # fmt: skip
    nobody = _invoke("attest.supplement", params, _ctx(state))
    assert not nobody.ok and "nothing was signed" in nobody.errors[0]
    held = _invoke("attest.supplement", params, _ctx(state, ["hold"]))
    assert held.ok and held.value["status"] == "hold"
    signed = _invoke("attest.supplement", params, _ctx(state, ["sign"]))
    assert signed.ok, signed.errors
    sup = service.record(signed.value["attestation_id"])
    assert sup["target"] == rec["attestation_id"] and sup["origin"] == "human"
    bad = _invoke("attest.supplement", {**params, "reason": "maybe"}, _ctx(state, ["sign"]))
    assert not bad.ok and "reason" in bad.errors[0]


def test_supplement_propose_drafts_for_the_original_signer(
    attest_db, state, node_signer, monkeypatch
):
    monkeypatch.setattr(proposals, "_agent_names", lambda: {"clerk"})
    rec = _sign(_round_check(), node_signer).record
    c = _ctx(state, actor="@clerk:site-a", surface="mcp")
    r = _invoke("attest.supplement_propose",
                {"attestation_id": rec["attestation_id"], "reason": "addition",
                 "fields": {"note": "seen later"}}, c)  # fmt: skip
    assert r.ok, r.errors
    assert (r.value["origin"], r.value["for_principal"], r.value["status"]) == (
        "agent:clerk", ME, "open"
    )  # fmt: skip
    assert len(service.records(SITE, "demo_log")) == 1
    observed = _invoke("attest.supplement_propose",
                       {"attestation_id": rec["attestation_id"], "reason": "correction",
                        "fields": {"reading": "9"}}, c)  # fmt: skip
    assert not observed.ok and "observe" in observed.errors[0]
    spoofed = _invoke("attest.supplement_propose",
                      {"attestation_id": rec["attestation_id"], "reason": "addition",
                       "body": "x", "origin": "human"}, c)  # fmt: skip
    assert not spoofed.ok


def test_entry_action_skills_declare_risk_and_requires():
    by_name = {s.name: s for s in SPECS}
    for name in ("attest.supplement", "attest.supplement_propose"):
        assert by_name[name].risk == "reversible-write"
        assert by_name[name].requires is not None
        assert by_name[name].side_effects is True
    assert by_name["attest.supplement"].surfaces == ("cli",)


def test_a_person_supplementing_is_not_a_proposal(attest_db, node_signer):
    rec = _sign(_round_check(), node_signer).record
    d = service.draft(_supplement_draft(rec["attestation_id"]))
    assert d["origin"] == "human"
    assert person().principal.handle == d["for_principal"]
