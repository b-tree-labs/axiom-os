# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/renew only ever offers, matches or resumes sessions the asking principal may see."""

from __future__ import annotations

from axiom.extensions.builtins.chat.commands_ext import renew
from axiom.infra.orchestrator.session import Session

from ._renew_support import ME, SOMEONE_ELSE, Agent, make_session, new_store


def _setup(tmp_path):
    store = new_store(tmp_path)
    make_session(store, "mine00000001", "reactor startup notes", hours_ago=3)
    make_session(store, "theirs000001", "reactor startup secrets", principal=SOMEONE_ELSE, hours_ago=1)
    make_session(store, "legacy000001", "from before ownership existed", principal="", hours_ago=5)
    return store, Agent(Session(principal_id=ME))


def test_listing_never_includes_another_principals_session(tmp_path):
    store, agent = _setup(tmp_path)
    ids = [r["id"] for r in renew.session_rows(store, agent.session)]
    assert "theirs000001" not in ids
    assert ids == ["mine00000001", "legacy000001"]  # most recent first, unowned ones included


def test_a_title_match_never_reaches_another_principals_session(tmp_path):
    store, agent = _setup(tmp_path)
    out = renew.renew(agent, store, "secrets")
    assert "no session matches" in out
    assert agent.session.session_id != "theirs000001"


def test_renew_by_a_strangers_exact_id_changes_nothing(tmp_path):
    store, agent = _setup(tmp_path)
    mine = agent.session
    out = renew.renew(agent, store, "theirs000001")
    assert agent.session is mine
    assert "renewed" not in out
    assert "reactor startup secrets" not in out


def test_resume_session_by_id_is_refused_for_a_stranger(tmp_path):
    store, agent = _setup(tmp_path)
    mine = agent.session
    out = renew.resume_session(store, agent, "theirs000001")
    assert "not found" in out.lower()
    assert agent.session is mine


def test_site_and_tenant_scope_the_listing(tmp_path):
    store = new_store(tmp_path)
    make_session(store, "siteA0000001", "alpha", site_id="site-a", tenant_id="t1")
    make_session(store, "siteB0000001", "beta", site_id="site-b", tenant_id="t1")
    make_session(store, "tenant200001", "gamma", site_id="site-a", tenant_id="t2")
    agent = Agent(Session(principal_id=ME, site_id="site-a", tenant_id="t1"))
    assert [r["id"] for r in renew.session_rows(store, agent.session)] == ["siteA0000001"]


def test_an_unbound_asker_sees_only_unowned_sessions(tmp_path):
    store, _ = _setup(tmp_path)
    anon = Session(principal_id="")
    assert [r["id"] for r in renew.session_rows(store, anon)] == ["legacy000001"]


def test_archived_sessions_are_hidden_unless_all(tmp_path):
    store, agent = _setup(tmp_path)
    store.archive("mine00000001")
    assert "mine00000001" not in [r["id"] for r in renew.session_rows(store, agent.session)]
    with_all = renew.session_rows(store, agent.session, include_archived=True)
    assert "mine00000001" in [r["id"] for r in with_all]
    # a title query does not reach an archived session either, until --all
    assert "no session matches" in renew.renew(agent, store, "startup notes")
    assert "renewed reactor startup notes" in renew.renew(agent, store, "startup notes", include_archived=True)


def test_listing_does_not_unarchive(tmp_path):
    store, agent = _setup(tmp_path)
    store.archive("mine00000001")
    renew.session_rows(store, agent.session, include_archived=True)
    assert (store._dir / "archive" / "mine00000001.json").exists()
    assert not (store._dir / "mine00000001.json").exists()


def test_sessions_listing_and_numbers_use_the_same_scope(tmp_path):
    """/sessions numbers and /resume numbers must name the same session, and never a stranger's."""
    from axiom.extensions.builtins.chat.commands import cmd_resume, cmd_sessions

    store, agent = _setup(tmp_path)
    listing = cmd_sessions(store, agent=agent)
    assert "theirs000001" not in listing and "mine00000001" in listing
    assert "renewed reactor startup notes" in cmd_resume("1", store, agent)
