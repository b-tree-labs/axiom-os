# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Timeline names a person by the rule attest uses (#1960), and a row's summary is plain (#1962)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.fleet import api as fleet_api
from axiom.extensions.builtins.journal import api, rows


@pytest.fixture(autouse=True)
def no_site_env(monkeypatch):
    monkeypatch.delenv("AXIOM_SITE", raising=False)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)


def _session(monkeypatch, claims):
    monkeypatch.setattr(fleet_api, "_session_resolver_factory",
                        lambda: (lambda request: SimpleNamespace(claims=claims)))


@pytest.mark.parametrize("sub,handle", [
    ("first+tag@example.org", "@first-tag"),
    ("12345+octo-cat@users.noreply.github.com", "@octo-cat"),
    ("op1@example.org", "@op1"),
])
def test_an_email_with_a_plus_is_named_the_way_attest_names_it(monkeypatch, sub, handle):
    _session(monkeypatch, {"sub": sub, "site": "site-a"})
    assert api._person(object()) == {"handle": f"{handle}:site-a", "site": "site-a"}


def test_the_journal_and_attest_name_the_same_person(monkeypatch):
    from axiom.extensions.builtins.attest.naming import signer_handle

    claims = {"sub": "first+tag@example.org", "site": "site-a"}
    _session(monkeypatch, claims)
    assert api._person(object())["handle"] == signer_handle(claims)


def test_an_unnamable_email_is_still_no_person(monkeypatch):
    _session(monkeypatch, {"sub": "a@b@example.org"})
    assert api._person(object()) is None


BRIEF = {
    "attestation_id": "a1", "seq": 3, "logbook": "demo_log", "site_id": "site-a",
    "entry_type": "CHECK", "meaning": "performed", "title": "Morning check",
    "signer": "@op1:site-a", "occurred_at": "2026-11-10T12:00:00+00:00", "digest": "d" * 64,
}


def test_a_summary_is_the_plain_title_and_the_type_is_its_own_field():
    row = rows.from_brief(BRIEF)
    assert row.summary == "Morning check"
    assert row.ref["entry_type"] == "CHECK"


def test_without_a_title_the_summary_is_the_types_human_label():
    row = rows.from_brief({**BRIEF, "title": None, "entry_type": "ROUND_CHECK"})
    assert row.summary == "Round check"


def test_a_proposed_draft_names_the_type_by_its_label():
    row = rows.from_draft_created({"logbook": "demo_log", "draft_id": "d1", "entry_type": "ROUND_CHECK",
                                   "origin": "@agent", "for_principal": "@op1:site-a"})
    assert "ROUND_CHECK" not in row.summary and row.summary.startswith("Round check proposed by")
