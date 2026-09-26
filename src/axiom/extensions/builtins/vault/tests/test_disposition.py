# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Not every credential can be rotated, and a schedule that assumes otherwise rots.

Six credentials here had no expiry, so the expiry audit reported them red every
run with the same advice: "record one". For most of them that advice is wrong.

- A PyPI token has no expiry to record. A date written there would be a lie that
  fires a false alarm on an arbitrary day.
- A service account on somebody else's network is not ours to rotate at all.
- An Entra client secret genuinely does expire, and only a human with console
  access can learn the date.

An unclearable finding is worse than no finding: it repeats until people filter
the report, and then the one real warning arrives in a channel nobody reads.
That is how this failure survived six months.

So a credential declares a **disposition** — what can actually be done about it
— and the audit asks the question that disposition makes answerable. What
replaces expiry for the ones we cannot date is **proof of life**: we cannot
predict death for a credential we do not control, but we can notice it within
one heartbeat instead of six months.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.vault.disposition import (
    DISPOSITIONS,
    ROTATE_AT_FRACTION,
    classify,
    rotation_due_at,
)

NOW = datetime(2026, 9, 21, tzinfo=UTC)


def _meta(**kw):
    base = {"name": "cred", "provider": "guided"}
    base.update(kw)
    return base


def test_an_expiry_is_always_enough():
    """A dated credential is auditable whatever its disposition. Demanding a
    disposition as well would make every healthy credential a finding."""
    assert classify(_meta(expires_at="2027-01-01"), now=NOW)["status"] == "ok"


def test_no_expiry_and_no_disposition_asks_for_the_declaration():
    """The actual blind spot. Not 'record an expiry' — that advice is wrong for
    most of these — but 'say what this is, so the right question can be asked'."""
    finding = classify(_meta(), now=NOW)
    assert finding["status"] == "undeclared"
    assert "disposition" in finding["remedy"]
    for name in DISPOSITIONS:
        assert name in finding["remedy"], f"the remedy must list {name}"


def test_a_non_expiring_credential_needs_a_review_date_not_an_expiry():
    finding = classify(_meta(disposition="non_expiring"), now=NOW)
    assert finding["status"] == "needs_review_by"
    assert "expires_at" not in finding["remedy"]
    assert "review_by" in finding["remedy"]


def test_a_non_expiring_credential_with_a_future_review_is_clean():
    meta = _meta(disposition="non_expiring", review_by="2027-01-01")
    assert classify(meta, now=NOW)["status"] == "ok"


def test_a_review_date_that_has_passed_is_due():
    meta = _meta(disposition="non_expiring", review_by="2026-09-01")
    assert classify(meta, now=NOW)["status"] == "review_overdue"


def test_an_externally_owned_credential_must_name_its_owner():
    """"Someone else controls this" is only useful if it says who. Without a
    name the finding is unactionable, which is the state we are leaving."""
    finding = classify(_meta(disposition="externally_owned"), now=NOW)
    assert finding["status"] == "needs_owner"
    assert "owner" in finding["remedy"]


def test_an_externally_owned_credential_is_never_proposed_for_rotation():
    """Rotating somebody else's service account is not ours to do, and
    suggesting it invites breaking a system we do not run."""
    meta = _meta(disposition="externally_owned", owner="NETL ops",
                 review_by="2027-01-01")
    finding = classify(meta, now=NOW)
    assert finding["status"] == "ok"
    assert finding["rotatable"] is False


def test_a_self_rotatable_credential_is_due_before_it_expires():
    """The wall this whole episode hit: a self-rotating token authenticates as
    itself, so once it expires it can no longer rotate. Expiry must never be the
    trigger — the trigger is a fraction of the lifetime."""
    issued = NOW - timedelta(days=200)
    expires = NOW + timedelta(days=100)
    due = rotation_due_at(issued_at=issued, expires_at=expires)
    assert due < expires, "rotation must fall due BEFORE expiry"
    expected = issued + (expires - issued) * ROTATE_AT_FRACTION
    assert abs((due - expected).total_seconds()) < 60


def test_a_self_rotatable_credential_past_its_rotation_point_is_due(  ):
    meta = _meta(
        disposition="self_rotatable", provider="gitlab-pat",
        last_rotated_at="2026-01-01T00:00:00+00:00", expires_at="2026-10-01",
    )
    finding = classify(meta, now=NOW)
    assert finding["status"] == "rotation_due"
    assert finding["rotatable"] is True


def test_a_self_rotatable_credential_before_its_rotation_point_is_clean():
    meta = _meta(
        disposition="self_rotatable", provider="gitlab-pat",
        last_rotated_at="2026-09-01T00:00:00+00:00", expires_at="2028-09-01",
    )
    assert classify(meta, now=NOW)["status"] == "ok"


def test_human_rotatable_without_a_date_says_where_to_get_it():
    """A console-only credential cannot be queried, so the remedy has to point
    at the place a person can actually look."""
    finding = classify(_meta(disposition="human_rotatable"), now=NOW)
    assert finding["status"] == "needs_expiry_from_console"
    assert finding["rotatable"] is False
    assert "console" in finding["remedy"].lower()


def test_an_unknown_disposition_is_refused_rather_than_ignored():
    """A typo that silently reverts to "undeclared" would let somebody believe
    they had classified a credential when they had not."""
    with pytest.raises(ValueError, match="nonsense"):
        classify(_meta(disposition="nonsense"), now=NOW)


def test_proof_of_life_clears_the_undated_case():
    """The substitute for an expiry we cannot know. We cannot predict death for
    a credential we do not control; we can notice it within a heartbeat."""
    meta = _meta(
        disposition="non_expiring",
        review_by="2027-01-01",
        last_verified_at=(NOW - timedelta(days=1)).isoformat(),
    )
    finding = classify(meta, now=NOW)
    assert finding["status"] == "ok"
    assert finding["verified_days_ago"] == 1


def test_stale_proof_of_life_is_a_finding_even_with_a_future_review():
    """A credential nothing has exercised in months may already be dead. The
    review date says when to look again; verification says it worked recently."""
    meta = _meta(
        disposition="non_expiring",
        review_by="2027-01-01",
        last_verified_at=(NOW - timedelta(days=120)).isoformat(),
    )
    assert classify(meta, now=NOW)["status"] == "verification_stale"


# --- declaring one, so the finding is clearable -------------------------------


def _ctx(tmp_path):
    import logging

    from axiom.infra.skills import SkillContext, default_registry

    return SkillContext(registry=default_registry(), state_dir=tmp_path,
                        logger=logging.getLogger("t"))


def _seed(tmp_path, name="cred", **meta):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    store = ForeignCredentialStore(tmp_path)
    store.set(name, b"v", provider="guided", **meta)
    return store


def test_declare_records_the_disposition(tmp_path):
    from axiom.extensions.builtins.vault.skills import declare

    store = _seed(tmp_path)
    result = declare.run(
        {"name": "cred", "disposition": "non_expiring", "review_by": "2027-06-01"},
        _ctx(tmp_path),
    )
    assert result.ok, result.errors
    meta = store.metadata("cred")
    assert meta["disposition"] == "non_expiring"
    assert meta["review_by"] == "2027-06-01"


def test_declare_refuses_an_unknown_disposition(tmp_path):
    from axiom.extensions.builtins.vault.skills import declare

    _seed(tmp_path)
    result = declare.run({"name": "cred", "disposition": "whatever"}, _ctx(tmp_path))
    assert result.ok is False
    assert "whatever" in " ".join(result.errors)
    for known in DISPOSITIONS:
        assert known in " ".join(result.errors)


def test_declaring_externally_owned_without_an_owner_is_refused(tmp_path):
    """Recording "somebody else's" without saying whose leaves a finding nobody
    can act on — the state this whole change exists to end."""
    from axiom.extensions.builtins.vault.skills import declare

    _seed(tmp_path)
    result = declare.run(
        {"name": "cred", "disposition": "externally_owned"}, _ctx(tmp_path))
    assert result.ok is False
    assert "owner" in " ".join(result.errors)


def test_declare_reports_what_is_still_missing(tmp_path):
    """Declaring is a step, not always the finish. Saying so beats a success
    message followed by the same red finding on the next heartbeat."""
    from axiom.extensions.builtins.vault.skills import declare

    _seed(tmp_path)
    result = declare.run({"name": "cred", "disposition": "non_expiring"}, _ctx(tmp_path))
    assert result.ok is True
    assert "review_by" in " ".join(result.actions_taken + result.errors)


def test_declare_refuses_an_unknown_credential(tmp_path):
    from axiom.extensions.builtins.vault.skills import declare

    _seed(tmp_path)
    result = declare.run(
        {"name": "not-there", "disposition": "non_expiring"}, _ctx(tmp_path))
    assert result.ok is False
    assert "not-there" in " ".join(result.errors)
