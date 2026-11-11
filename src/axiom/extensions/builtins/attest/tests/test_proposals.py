# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Proposals from the edges the acceptance rows do not walk: the library's origin check, the
tray's held mark, a discarded draft going nowhere, and the command line's two verbs."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.attest import cli, proposals, service
from axiom.extensions.builtins.attest.service import AttestRefused, Signatory
from axiom.infra.principal import PrincipalContext

from .test_skills import roles  # noqa: F401

SITE = "site-a"
ME = PrincipalContext(handle="@op1:site-a", posture="sso", assured=True)


def _proposed(roles, **over):  # noqa: F811
    args = {"site_id": SITE, "logbook": "demo_log", "entry_type": "ROUND_CHECK",
            "meaning": "performed", "content": {"title": "Round", "fields": {"walkdown": True}},
            "origin": "agent:helper", "for_principal": ME.handle, "state_dir": roles}  # fmt: skip
    return proposals.propose(**{**args, **over})


@pytest.fixture(autouse=True)
def no_roster(monkeypatch):
    monkeypatch.setattr(proposals, "_agent_names", lambda: {"helper"})
    monkeypatch.setattr(proposals, "_installed_extensions", lambda: {"example_ext"})


def test_the_library_refuses_a_software_origin_outside_the_grammar(attest_db, monkeypatch):
    from axiom.attest import drafts

    monkeypatch.setenv("AXIOM_SERVED_SITES", SITE)
    content = {"title": "Round", "fields": {"walkdown": True}}
    with pytest.raises(AttestRefused, match="not a proposer"):
        drafts.create("demo_log", "ROUND_CHECK", content, "robot", ME.handle)
    d = drafts.create("demo_log", "ROUND_CHECK", content, "rule:due-soon", ME.handle)
    assert d.origin == "rule:due-soon"


def test_a_held_draft_stays_in_the_tray_marked_held(attest_db, roles):  # noqa: F811
    d = _proposed(roles)
    service.fill(d["draft_id"], {"reading": "ok"}, by=Signatory(principal=ME, roles=()))
    p = service.present(d["draft_id"], modality="cli")
    service.respond(p.presentation_id, principal=ME, answer="hold", via="cli")
    (row,) = service.list_drafts(for_principal=ME.handle)
    assert row["held"] is True and row["status"] == "open"


def test_a_discarded_draft_cannot_be_filled_presented_or_discarded_again(attest_db, roles):  # noqa: F811
    d = _proposed(roles)
    service.discard(d["draft_id"], by=ME.handle)
    with pytest.raises(AttestRefused, match="discarded"):
        service.fill(d["draft_id"], {"reading": "ok"}, by=Signatory(principal=ME, roles=()))
    with pytest.raises(AttestRefused, match="discarded"):
        service.present(d["draft_id"])
    with pytest.raises(AttestRefused, match="discarded"):
        service.discard(d["draft_id"], by=ME.handle)
    assert service.list_drafts(for_principal=ME.handle, status="discarded")[0]["held"] is False


def test_the_tray_filters_by_origin_prefix_and_log(attest_db, roles):  # noqa: F811
    _proposed(roles)
    _proposed(roles, origin="integration:example_ext")
    agents = service.list_drafts(for_principal=ME.handle, origin_prefix="agent")
    assert {d["origin"] for d in agents} == {"agent:helper"}
    assert service.list_drafts(for_principal=ME.handle, logbook="other_log") == []
    assert len(service.list_drafts(for_principal=ME.handle)) == 2


def test_the_command_line_has_propose_and_drafts():
    parser = cli.get_parser()
    a = parser.parse_args(["propose", "--logbook", "demo_log", "--type", "ROUND_CHECK",
                           "--meaning", "performed", "--title", "Round", "--for", ME.handle,
                           "--integration", "example_ext", "--field", "walkdown=true"])  # fmt: skip
    assert a.for_principal == ME.handle and a.integration == "example_ext"
    assert parser.parse_args(["drafts", "--origin", "agent"]).origin == "agent"
