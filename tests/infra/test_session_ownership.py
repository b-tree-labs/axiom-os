# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Sessions belong to a principal, and the store enforces it.

`SessionStore` was written for a single-user CLI: one flat directory of
`<session_id>.json`, and `load(session_id)` hands back whatever it finds. On
a laptop that is right — the only principal is you.

The served surface is the problem. `http/chat_server.py` memoises ONE
`ChatAgent` with ONE `Session()` for the whole process, so every caller shares
one conversation and one `context["user_identity"]`, which is written a line
before `turn()` and can be overwritten by a concurrent request in between. To
give each caller their own session safely, the store has to know who owns one
— otherwise per-principal sessions are one guessed id away from being another
principal's transcript.
"""
from __future__ import annotations

import pytest

from axiom.infra.orchestrator.session import Session, SessionStore


def _spoken(store, principal, text="hello"):
    """A session with a message; `save` skips empty ones by design."""
    session = Session(principal_id=principal)
    session.add_message("user", text)
    store.save(session)
    return session


class TestOwnershipIsRecorded:
    def test_a_saved_session_remembers_its_principal(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path)
        saved = _spoken(store, "@alice:site")
        assert store.load(saved.session_id).principal_id == "@alice:site"

    def test_a_session_without_a_principal_still_saves(self, tmp_path):
        """The CLI does not set one today, and must keep working."""
        store = SessionStore(sessions_dir=tmp_path)
        saved = _spoken(store, "")
        assert store.load(saved.session_id) is not None


class TestOwnershipIsEnforcedWhenAsked:
    def test_another_principal_cannot_load_it(self, tmp_path):
        """The whole reason this exists: on a served surface a session id is
        guessable, and without this check per-principal sessions would be one
        guess away from another person's transcript."""
        store = SessionStore(sessions_dir=tmp_path)
        saved = _spoken(store, "@alice:site", "my private question")
        assert store.load(saved.session_id, principal="@bob:site") is None

    def test_the_owner_can_load_it(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path)
        saved = _spoken(store, "@alice:site")
        assert store.load(saved.session_id, principal="@alice:site") is not None

    def test_no_principal_argument_keeps_the_old_behaviour(self, tmp_path):
        """The CLI calls `load(id)` with no principal and must not break."""
        store = SessionStore(sessions_dir=tmp_path)
        saved = _spoken(store, "@alice:site")
        assert store.load(saved.session_id) is not None

    def test_an_unowned_session_is_claimable(self, tmp_path):
        """Sessions written before ownership existed have no principal. They
        must stay readable, or upgrading strands every existing transcript."""
        store = SessionStore(sessions_dir=tmp_path)
        saved = _spoken(store, "")
        assert store.load(saved.session_id, principal="@alice:site") is not None


class TestFindingYourOwnSessions:
    def test_it_lists_only_the_caller_s_sessions(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path)
        mine = _spoken(store, "@alice:site")
        _spoken(store, "@bob:site")
        assert store.list_sessions(principal="@alice:site") == [mine.session_id]

    def test_it_returns_the_most_recent_first(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path)
        first = _spoken(store, "@alice:site", "one")
        second = _spoken(store, "@alice:site", "two")
        listed = store.list_sessions(principal="@alice:site")
        assert set(listed) == {first.session_id, second.session_id}

    def test_latest_for_returns_a_resumable_session(self, tmp_path):
        """What a served surface needs: continue where this principal left
        off, without the caller having to carry a session id."""
        store = SessionStore(sessions_dir=tmp_path)
        mine = _spoken(store, "@alice:site")
        found = store.latest_for("@alice:site")
        assert found is not None and found.session_id == mine.session_id

    def test_an_unowned_session_is_findable_not_just_loadable(self, tmp_path):
        """`load` accepts a pre-ownership session, but a served surface finds
        one through `latest_for`. Filtering them out of the listing would
        strand every transcript written before ownership existed — readable
        in principle, unreachable in practice.
        """
        store = SessionStore(sessions_dir=tmp_path)
        legacy = _spoken(store, "")
        found = store.latest_for("@alice:site")
        assert found is not None and found.session_id == legacy.session_id

    def test_latest_for_a_stranger_is_none(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path)
        _spoken(store, "@alice:site")
        assert store.latest_for("@bob:site") is None


class TestAdoptingWhatThisNodeAlreadyOwned:
    """Switching to the canonical IdP principal must not strand transcripts.

    Everything written before it exists is owned by a node-derived handle —
    `@laptop:person`. The canonical `@a1b2c3:example` is the same human, so it
    inherits them on read, the same way an unowned session is claimable.

    The adoptable set is derived from THIS NODE's identity, never passed in.
    An earlier version took it as a `load(..., adopts=[...])` argument, and an
    end-to-end probe showed the hole immediately: naming the owning handle
    read somebody else's transcript. The unit test had missed it because the
    stranger case named a handle that did not own the session — the bypass
    only appears when the attacker names the RIGHT one.
    """

    @pytest.fixture(autouse=True)
    def _this_node_minted(self, monkeypatch):
        """Both halves of the boundary, because both are node-derived: WHICH
        handles this node minted, and WHO on this node may inherit them."""
        monkeypatch.setattr(
            "axiom.infra.orchestrator.session.node_legacy_handles",
            lambda: ["@laptop:person", "@person:laptop"],
        )
        monkeypatch.setattr(
            "axiom.infra.orchestrator.session.may_adopt",
            lambda principal: principal == "@a1b2c3:example",
        )

    def test_a_legacy_owned_session_is_adopted(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path)
        old = _spoken(store, "@laptop:person", "what I asked yesterday")
        assert store.load(old.session_id, principal="@a1b2c3:example") is not None

    def test_a_stranger_naming_the_owning_handle_is_still_refused(self, tmp_path):
        """The exact bypass. A caller cannot widen adoption, because adoption
        is not something a caller supplies."""
        store = SessionStore(sessions_dir=tmp_path)
        old = _spoken(store, "@laptop:person", "private")
        assert store.load(old.session_id, principal="@mallory:evil") is None
        assert store.latest_for("@mallory:evil") is None

    def test_adopted_sessions_are_listed(self, tmp_path):
        """Loadable but unfindable is how a migration strands history."""
        store = SessionStore(sessions_dir=tmp_path)
        old = _spoken(store, "@laptop:person")
        assert old.session_id in store.list_sessions(principal="@a1b2c3:example")

    def test_latest_for_reaches_an_adopted_session(self, tmp_path):
        store = SessionStore(sessions_dir=tmp_path)
        old = _spoken(store, "@laptop:person")
        found = store.latest_for("@a1b2c3:example")
        assert found is not None and found.session_id == old.session_id


class TestAdoptionOnANodeThatMintedNothing:
    def test_a_legacy_session_from_elsewhere_is_refused(self, tmp_path, monkeypatch):
        """A different installation's handle is not adoptable here."""
        monkeypatch.setattr(
            "axiom.infra.orchestrator.session.node_legacy_handles", lambda: []
        )
        store = SessionStore(sessions_dir=tmp_path)
        old = _spoken(store, "@workstation:person")
        assert store.load(old.session_id, principal="@a1b2c3:example") is None


class TestTheRealAdoptionGuard:
    """Unpatched. The class above stubs `may_adopt` to isolate the store, so
    mutating the guard itself changed nothing there — two mutants survived on
    that alone. This exercises the real function.
    """

    def test_a_stranger_may_not_adopt(self, monkeypatch):
        from axiom.infra.orchestrator import session as S

        monkeypatch.delenv("AXIOM_IDP_SUBJECT", raising=False)
        assert S.may_adopt("@mallory:evil") is False

    def test_an_empty_principal_may_not_adopt(self):
        from axiom.infra.orchestrator import session as S

        assert S.may_adopt("") is False

    def test_this_node_s_own_canonical_principal_may(self, monkeypatch):
        from axiom.infra.orchestrator import session as S

        monkeypatch.setenv("AXIOM_IDP_SUBJECT", "a1b2c3@idp.example.edu")
        monkeypatch.setenv("AXIOM_IDENTITY_CONTEXT", "example")
        assert S.may_adopt("@a1b2c3:example") is True
        assert S.may_adopt("@someone.else:example") is False


class TestTheListingRespectsTheGuardToo:
    def test_a_stranger_does_not_see_legacy_sessions_listed(self, tmp_path, monkeypatch):
        """`load` refusing is not enough. If the listing leaks ids, a caller
        learns what exists and can probe each one."""
        from axiom.infra.orchestrator import session as S

        monkeypatch.setattr(S, "node_legacy_handles", lambda: ["@laptop:person"])
        monkeypatch.setattr(S, "may_adopt", lambda p: p == "@a1b2c3:example")
        store = SessionStore(sessions_dir=tmp_path)
        old = _spoken(store, "@laptop:person")
        assert store.list_sessions(principal="@mallory:evil") == []
        assert old.session_id in store.list_sessions(principal="@a1b2c3:example")
