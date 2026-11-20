# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A share by email reaches the person when they sign in (ADR-263 D9), on the real catalog.

The share dialog turns an email into the person's Team handle (#2042), which is
the gate's handle rule for an account: ``@<subject>:<site>``. A browser session
reaches the decision point as the authz edge names it, ``session_handle(sub)``:
``@<subject>:gate`` on every node that does not set a session context. So on a
real site the grant was keyed to one handle and the person signed in as another,
and their Shared with me stayed empty. Failing closed, but sharing by email did
not work.

The decision point now links the signed-in handle to the person's Team handle
through the gate's own account record (the session's verified subject IS the
account's id), and only when exactly one account at the caller's site answers to
both. Nothing is matched by likeness: a handle two accounts share links nobody.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library.tests.test_library_postgres import pg_library  # noqa: F401

DOC = b"# Review\n\nNotes kept for the team, about the quarterly review and what it found.\n"

#: An SSO account on a real site: keyed on the identity provider's immutable subject (ADR-103 D11).
DANA_SUBJECT = "4f1c2a90-7d3e-4b8a-9c11-0a2b3c4d5e6f"
EVE_SUBJECT = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    from axiom.webauth import live, upsert_user_record

    path = tmp_path / "gate-users.json"
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(path))
    monkeypatch.setenv("AXIOM_SERVED_SITES", "alpha,beta")
    monkeypatch.delenv("AXIOM_GATE_PRINCIPAL_CONTEXT", raising=False)
    sso = {"idp": "sso"}
    upsert_user_record(path, email="dana.ross@example.org", password_hash=None, name="Dana Ross",
                       user_id=DANA_SUBJECT, attributes={**sso, "idp_subject": DANA_SUBJECT}, site="alpha")
    upsert_user_record(path, email="eve.park@example.org", password_hash=None, name="Eve Park",
                       user_id=EVE_SUBJECT, attributes={**sso, "idp_subject": EVE_SUBJECT}, site="alpha")
    live._reset_for_tests()
    yield path
    live._reset_for_tests()


def _signed_in(subject: str, site: str = "alpha"):
    """The caller a browser session becomes: the authz edge's handle for the session's ``sub``."""
    from axiom.extensions.builtins.http.authz_hook import session_handle
    from axiom.extensions.builtins.library.scope import make_caller

    return make_caller(session_handle(subject), site)


def _private_item(alice):
    from axiom.extensions.builtins.library import service

    item = service.upload(alice, "review.md", DOC, visibility="private").item
    return item["id"]


def test_a_share_by_email_reaches_the_person_signed_in_through_sso(pg_library, accounts):  # noqa: F811
    from axiom.extensions.builtins.library import service, sharing, views
    from axiom.extensions.builtins.library.scope import make_caller

    alice = make_caller("@alice:alpha", "alpha")
    ref = _private_item(alice)
    sharing.share(alice, ref, "dana.ross@example.org", "viewer", notify=False)

    dana = _signed_in(DANA_SUBJECT)
    assert [i["id"] for i in views.shared_with_me(dana)["items"]] == [ref]
    assert service.get_item(dana, ref)["id"] == ref

    # Someone else at the site, signed in the same way, gains nothing.
    eve = _signed_in(EVE_SUBJECT)
    assert views.shared_with_me(eve)["items"] == []
    with pytest.raises(service.NotFound):
        service.get_item(eve, ref)


def test_unsharing_by_email_ends_the_signed_in_person_s_access(pg_library, accounts):  # noqa: F811
    from axiom.extensions.builtins.library import service, sharing, views
    from axiom.extensions.builtins.library.scope import make_caller

    alice = make_caller("@alice:alpha", "alpha")
    ref = _private_item(alice)
    sharing.share(alice, ref, "dana.ross@example.org", "viewer", notify=False)
    sharing.unshare(alice, ref, "dana.ross@example.org")
    dana = _signed_in(DANA_SUBJECT)
    assert views.shared_with_me(dana)["items"] == []
    with pytest.raises(service.NotFound):
        service.get_item(dana, ref)


def test_a_handle_two_accounts_answer_to_links_nobody(pg_library, accounts):  # noqa: F811
    """Two accounts whose Team handles collide are never resolved to by an email, nor linked at sign-in."""
    from axiom.extensions.builtins.actors.directory import person_handle
    from axiom.extensions.builtins.library import people, sharing, views
    from axiom.extensions.builtins.library.scope import make_caller
    from axiom.webauth import live, upsert_user_record

    # Two password accounts whose email local parts agree: the gate's rule gives both @dana:alpha.
    upsert_user_record(accounts, email="dana@example.org", password_hash="x", user_id="dana@example.org", site="alpha")
    upsert_user_record(accounts, email="dana@example.net", password_hash="x", user_id="dana@example.net", site="alpha")
    live._reset_for_tests()
    shared = person_handle("dana@example.org", "dana@example.org", "alpha")
    assert shared == person_handle("dana@example.net", "dana@example.net", "alpha")

    alice = make_caller("@alice:alpha", "alpha")
    with pytest.raises(ValueError, match="more than one"):
        people.resolve(alice, "dana@example.org")

    ref = _private_item(alice)
    sharing.share(alice, ref, shared, "viewer", notify=False)  # typed as a handle, by somebody
    for subject in ("dana@example.org", "dana@example.net"):
        assert views.shared_with_me(_signed_in(subject))["items"] == []


def test_an_account_at_another_site_is_never_linked(pg_library, accounts):  # noqa: F811
    from axiom.extensions.builtins.library import sharing, views
    from axiom.extensions.builtins.library.scope import make_caller
    from axiom.webauth import live, upsert_user_record

    upsert_user_record(accounts, email="dana.ross@example.net", password_hash=None, user_id=DANA_SUBJECT + "b",
                       attributes={"idp": "sso"}, site="beta")
    live._reset_for_tests()
    alice = make_caller("@alice:alpha", "alpha")
    ref = _private_item(alice)
    sharing.share(alice, ref, "dana.ross@example.org", "viewer", notify=False)
    # The same session handle presented at the wrong site reaches nothing.
    assert views.shared_with_me(_signed_in(DANA_SUBJECT, site="beta"))["items"] == []
