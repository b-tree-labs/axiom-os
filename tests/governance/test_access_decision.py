# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The sharing record and the one decision point (ADR-263 D9, D10; prd-sharing-and-access).

Each test names the acceptance row it proves. The record runs on SQLite here
for speed; ``TestOnPostgres`` proves the same acts on the migrated ``authz``
schema of a real Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.governance.access import decision, sharing, store
from axiom.governance.access.decision import AccessObject, NoAccess, Refused, require, role_of, who
from axiom.governance.access.roles import DOCUMENT, FOLDER, RESTRICTED, SITE, Role

OWNER = "@ana:alpha"
COLLEAGUE = "@ben:alpha"
THIRD = "@cy:alpha"
ADMIN = "@sam:alpha"
STRANGER = "@eve:beta"


@pytest.fixture
def record(tmp_path):
    """The authz tables on SQLite, the decision point's receipts written to the same database."""
    from axiom.extensions.builtins.authz.db_models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'authz.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    store.set_session_provider(provider)
    decision.set_decide_context(decision.build_access_context(session_factory=provider))
    groups = {COLLEAGUE: ["readers"], THIRD: []}
    roles = {ADMIN: ["site-admin"], "@bea:alpha": ["book-manager"]}
    decision.set_resolvers(groups=lambda p, s: groups.get(p, []), roles=lambda p, s: roles.get(p, []))
    yield factory
    decision.set_resolvers()
    decision.set_decide_context(None)
    store.set_session_provider(None)


def doc(general: str = SITE, *, parents=(), held=False, owner=OWNER, id="d1") -> AccessObject:
    return AccessObject(kind=DOCUMENT, id=id, site="alpha", owner=owner, legacy_general=general,
                        parents=tuple(parents), held=held)


def folder(id="f1", owner=OWNER) -> AccessObject:
    return AccessObject(kind=FOLDER, id=id, site="alpha", owner=owner, legacy_general=RESTRICTED)


def me(p: str):
    return who(p, "beta" if p == STRANGER else "alpha")


def _verdicts(factory):
    from axiom.extensions.builtins.authz.db_models import Verdict

    with factory() as s:
        return [(v.actor, v.intent, v.resource, v.decision) for v in s.query(Verdict).order_by(Verdict.decided_at)]


def test_roles_are_a_ladder():
    assert Role.OWNER.at_least(Role.EDITOR) and Role.EDITOR.at_least(Role.INVOKER)
    assert Role.INVOKER.at_least(Role.COMMENTER) and Role.COMMENTER.at_least(Role.VIEWER)
    assert not Role.VIEWER.at_least(Role.COMMENTER)


def test_every_verb_names_its_least_role():
    from axiom.governance.access.verbs import VERBS

    assert VERBS["library.move"].least is Role.EDITOR
    assert VERBS["library.purge"].least is Role.OWNER
    assert VERBS["library.show"].least is Role.VIEWER
    assert all(v.least is not None or v.rule for v in VERBS.values())


def test_shr_a11_today_s_access_is_kept_by_the_projection(record):
    """SHR-A11: a ``site`` file reads as Everyone at the site · Viewer; a ``private`` one as Restricted."""
    assert role_of(me(COLLEAGUE), doc(SITE)) is Role.VIEWER
    assert role_of(me(COLLEAGUE), doc(RESTRICTED)) is None
    assert role_of(me(OWNER), doc(RESTRICTED)) is Role.OWNER
    assert role_of(me(STRANGER), doc(SITE)) is None  # another site never reads it


def test_shr_a1_no_role_is_not_found_and_a_viewer_is_refused_naming_the_role(record):
    with pytest.raises(NoAccess):
        require(me(COLLEAGUE), "library.show", doc(RESTRICTED))
    require(me(COLLEAGUE), "library.show", doc(SITE))
    with pytest.raises(Refused) as refused:
        require(me(COLLEAGUE), "library.update", doc(SITE))
    assert refused.value.needed == "Editor" and "Editor" in str(refused.value)


def test_a_direct_share_and_a_group_share_reach_only_the_people_named(record):
    d = doc(RESTRICTED)
    sharing.share(me(OWNER), d, THIRD, "editor")
    sharing.share(me(OWNER), d, "group:readers", "viewer")
    assert role_of(me(THIRD), d) is Role.EDITOR
    assert role_of(me(COLLEAGUE), d) is Role.VIEWER  # through the group
    assert role_of(who("@dee:alpha", "alpha"), d) is None


def test_shr_a4_a_folder_share_reaches_every_item_and_an_item_never_has_less(record):
    f = folder()
    sharing.share(me(OWNER), f, THIRD, "editor")
    inside = doc(RESTRICTED, parents=[f], owner=COLLEAGUE)
    assert role_of(me(THIRD), inside) is Role.EDITOR
    sharing.share(me(COLLEAGUE), inside, THIRD, "viewer")  # cannot lower what the folder gives
    assert role_of(me(THIRD), inside) is Role.EDITOR
    # The folder's Owner does not own another person's file in it: Editor at most.
    assert role_of(me(OWNER), inside) is Role.EDITOR


def test_an_editor_of_the_folder_may_delete_what_it_holds(record):
    f = folder()
    sharing.share(me(OWNER), f, THIRD, "editor")
    inside = doc(SITE, parents=[f], owner=COLLEAGUE)
    require(me(THIRD), "library.delete", inside)
    with pytest.raises(Refused):
        require(who("@dee:alpha", "alpha"), "library.delete", inside)


def test_shr_a5_the_two_settings(record):
    d = doc(RESTRICTED)
    sharing.share(me(OWNER), d, THIRD, "editor")
    sharing.share(me(OWNER), d, COLLEAGUE, "viewer")
    sharing.share(me(THIRD), d, "@dee:alpha", "viewer")  # editors share by default
    sharing.set_settings(me(OWNER), d, editors_can_share=False, viewers_can_download=False)
    with pytest.raises(Refused, match="turned off sharing by editors"):
        sharing.share(me(THIRD), d, "@fay:alpha", "viewer")
    with pytest.raises(Refused, match="turned off download"):
        require(me(COLLEAGUE), "library.download", d)
    with pytest.raises(Refused):
        require(me(COLLEAGUE), "library.copy", d)
    require(me(THIRD), "library.download", d)  # an Editor still may


def test_settings_are_the_owner_s(record):
    d = doc(RESTRICTED)
    sharing.share(me(OWNER), d, THIRD, "editor")
    with pytest.raises(Refused):
        sharing.set_settings(me(THIRD), d, editors_can_share=False)


def test_general_access_changes_who_can_open_it(record):
    d = doc(RESTRICTED)
    sharing.set_general(me(OWNER), d, SITE, "editor")
    assert role_of(me(COLLEAGUE), d) is Role.EDITOR
    sharing.set_general(me(OWNER), d, RESTRICTED)
    assert role_of(me(COLLEAGUE), d) is None


def test_shr_a6_an_entry_ends_on_its_day(record):
    d = doc(RESTRICTED)
    tomorrow = (store.now() + timedelta(days=1)).isoformat()
    sharing.share(me(OWNER), d, THIRD, "viewer", expires=tomorrow)
    assert role_of(me(THIRD), d) is Role.VIEWER
    with record() as s:
        ended = store.expire_due(s, at=store.now() + timedelta(days=2))
        s.commit()
    assert [g.subject for g in ended] == [THIRD]
    assert role_of(me(THIRD), d) is None
    with pytest.raises(ValueError, match="a year"):
        sharing.share(me(OWNER), d, THIRD, "viewer", expires=(store.now() + timedelta(days=400)).isoformat())


def test_shr_a8_a_held_item_s_shares_wait_and_return(record):
    d = doc(RESTRICTED)
    sharing.share(me(OWNER), d, THIRD, "viewer")
    held = doc(RESTRICTED, held=True)
    assert role_of(me(THIRD), held) is None
    with pytest.raises(Refused, match="waits for the review"):
        sharing.share(me(OWNER), held, COLLEAGUE, "viewer")
    assert role_of(me(THIRD), d) is Role.VIEWER  # released: the share is used again


def test_shr_a10_a_site_admin_opens_no_restricted_item_and_gives_themselves_nothing(record):
    d = doc(RESTRICTED)
    with pytest.raises(NoAccess):
        require(me(ADMIN), "library.show", d)
    with pytest.raises(NoAccess):
        sharing.share(me(ADMIN), d, ADMIN, "editor")
    assert role_of(me(ADMIN), d) is None


def test_only_a_book_manager_changes_a_category(record):
    category = AccessObject(kind=FOLDER, id="scheme:reports", site="alpha", owner="")
    require(who("@bea:alpha", "alpha"), "library.book.category", category)
    require(me(ADMIN), "library.book.category", category)  # site-admins hold it by default
    with pytest.raises(Refused, match="Book manager"):
        require(me(COLLEAGUE), "library.book.category", category)


def test_a_share_never_leaves_the_site(record):
    with pytest.raises(ValueError, match="outside its site"):
        sharing.share(me(OWNER), doc(), "@eve:beta", "viewer")
    with pytest.raises(ValueError, match="with their site"):
        sharing.share(me(OWNER), doc(), "@eve", "viewer")
    with pytest.raises(ValueError, match="transferred"):
        sharing.share(me(OWNER), doc(), THIRD, "owner")


def test_shr_a16_every_share_change_and_refusal_is_a_decide_receipt(record):
    d = doc(RESTRICTED)
    sharing.share(me(OWNER), d, THIRD, "viewer")
    with pytest.raises(Refused):
        sharing.share(me(THIRD), d, COLLEAGUE, "viewer")
    rows = _verdicts(record)
    assert (OWNER, "library.share", "document://d1", "permit") in rows
    assert (THIRD, "library.share", "document://d1", "deny") in rows


def test_shr_a7_request_share_lower_decline_and_request_again(record):
    d = doc(RESTRICTED)
    asked = sharing.request_access(me(THIRD), d, "editor", note="for the review")
    assert asked["state"] == "open" and "title" not in asked
    assert sharing.request_access(me(THIRD), d, "editor")["already_open"]  # not doubled
    assert role_of(me(THIRD), d) is None  # a request grants nothing until answered
    answered, change = sharing.answer_request(me(OWNER), d, asked["id"], approve=True, role="viewer")
    assert answered["granted_role"] == "viewer" and change is not None
    assert role_of(me(THIRD), d) is Role.VIEWER

    again = sharing.request_access(me(THIRD), d, "editor")
    with pytest.raises(ValueError, match="say why"):
        sharing.answer_request(me(OWNER), d, again["id"], approve=False)
    sharing.answer_request(me(OWNER), d, again["id"], approve=False, reason="Not until the audit closes")
    mine = sharing.my_requests(me(THIRD), d)
    assert mine["previously_refused"] == {"by": OWNER, "at": mine["previously_refused"]["at"],
                                          "reason": "Not until the audit closes"}
    assert sharing.request_access(me(THIRD), d, "editor")["state"] == "open"  # Request again works at once


def test_a_viewer_cannot_answer_a_request(record):
    d = doc(SITE)
    asked = sharing.request_access(me(THIRD), d, "editor")
    with pytest.raises(Refused):
        sharing.answer_request(me(COLLEAGUE), d, asked["id"], approve=True)


def test_transfer_goes_to_someone_on_the_list_and_the_old_owner_becomes_editor(record):
    d = doc(RESTRICTED)
    with pytest.raises(ValueError, match="already on the access list"):
        sharing.transfer(me(OWNER), d, THIRD)
    sharing.share(me(OWNER), d, THIRD, "viewer")
    change = sharing.transfer(me(OWNER), d, THIRD)
    assert change.detail == {"owner": THIRD, "was": OWNER}
    after = doc(RESTRICTED, owner=THIRD)  # the home moves its owner column
    assert role_of(me(THIRD), after) is Role.OWNER
    assert role_of(me(OWNER), after) is Role.EDITOR


def test_a_viewer_not_on_the_list_sees_only_the_owner_and_general_access(record):
    d = doc(SITE)
    sharing.share(me(OWNER), d, THIRD, "editor")
    seen = sharing.access_list(me(COLLEAGUE), d)
    assert seen["owner"] == OWNER and seen["people"] is None and seen["general"]["general"] == SITE
    assert [p["subject"] for p in sharing.access_list(me(THIRD), d)["people"]] == [THIRD]


class TestOnPostgres:
    """The same record on the migrated ``authz`` schema of a real Postgres."""

    @pytest.fixture
    def pg(self, fresh_postgres, monkeypatch):
        from pathlib import Path

        from alembic import command
        from alembic.config import Config

        from axiom.extensions.builtins import authz as authz_extension
        from axiom.infra.db import session_for

        monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
        config = Config()
        config.set_main_option("script_location", str(Path(authz_extension.__file__).parent / "migrations"))
        command.upgrade(config, "head")
        store.set_session_provider(None)
        decision.set_decide_context(decision.build_access_context(session_factory=lambda: session_for("authz")))
        decision.set_resolvers(groups=lambda p, s: [], roles=lambda p, s: [])
        yield
        decision.set_resolvers()
        decision.set_decide_context(None)

    def test_the_migration_builds_the_three_tables_and_only_adds(self, pg):
        from sqlalchemy import inspect

        from axiom.infra.db import get_engine

        tables = set(inspect(get_engine()).get_table_names(schema="authz"))
        assert {"access_grants", "access_settings", "access_requests", "verdicts"} <= tables

    def test_a_share_a_request_and_its_receipts_on_postgres(self, pg):
        from sqlalchemy import text

        from axiom.infra.db import session_for

        d = doc(RESTRICTED, id="pg-1")
        assert role_of(me(THIRD), d) is None
        sharing.share(me(OWNER), d, THIRD, "editor")
        assert role_of(me(THIRD), d) is Role.EDITOR
        asked = sharing.request_access(me(COLLEAGUE), d, "viewer")
        sharing.answer_request(me(THIRD), d, asked["id"], approve=True)
        assert role_of(me(COLLEAGUE), d) is Role.VIEWER
        with session_for("authz") as s:
            n = s.execute(text("select count(*) from verdicts where intent = 'library.share' and decision = 'permit'"))
            assert n.scalar() >= 2


# --- a person's clearance outranks every share, checked when they open it (ADR-263 D8) ---------


@pytest.fixture
def clearances():
    from axiom.governance import classification as gc

    levels = {"@cy:alpha": "internal", "@dee:alpha": "controlled", OWNER: "public"}
    gc.set_clearance_resolver(lambda p: levels.get(p, "internal"))
    yield levels
    gc.set_clearance_resolver(None)


def labelled(general: str = RESTRICTED, *, label: str = "controlled") -> AccessObject:
    return AccessObject(kind=DOCUMENT, id="lab", site="alpha", owner=OWNER, legacy_general=general, label=label)


def test_a_group_share_reaches_only_the_members_cleared_for_the_label(record, clearances):
    d = labelled()
    sharing.share(me(OWNER), d, "group:team", "viewer")
    decision.set_resolvers(groups=lambda p, s: ["team"] if p in ("@cy:alpha", "@dee:alpha") else [],
                           roles=lambda p, s: [])
    assert role_of(who("@dee:alpha", "alpha"), d) is Role.VIEWER
    assert role_of(who("@cy:alpha", "alpha"), d) is None
    with pytest.raises(NoAccess):
        require(who("@cy:alpha", "alpha"), "library.show", d)


def test_a_clearance_lowered_after_a_direct_share_ends_that_share_s_use(record, clearances):
    d = labelled()
    sharing.share(me(OWNER), d, "@dee:alpha", "editor")
    assert role_of(who("@dee:alpha", "alpha"), d) is Role.EDITOR
    clearances["@dee:alpha"] = "internal"
    assert role_of(who("@dee:alpha", "alpha"), d) is None


def test_general_access_and_a_folder_share_also_yield_to_clearance(record, clearances):
    assert role_of(me(COLLEAGUE), labelled(SITE)) is None
    f = folder()
    sharing.share(me(OWNER), f, "@cy:alpha", "editor")
    inside = AccessObject(kind=DOCUMENT, id="in", site="alpha", owner=OWNER, legacy_general=RESTRICTED,
                          parents=(f,), label="controlled")
    assert role_of(who("@cy:alpha", "alpha"), inside) is None


def test_the_owner_keeps_their_own_file_and_an_unlabelled_file_is_unaffected(record, clearances):
    assert role_of(me(OWNER), labelled()) is Role.OWNER  # the owner's own upload is theirs to keep
    sharing.share(me(OWNER), doc(RESTRICTED), "@cy:alpha", "viewer")
    assert role_of(who("@cy:alpha", "alpha"), doc(RESTRICTED)) is Role.VIEWER
