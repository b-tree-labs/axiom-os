# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reconcile what we STORE against what the issuer actually says.

The credential that broke git access had been deleted or regenerated in the
GitLab UI months earlier. Our store still held its value and had no expiry
recorded, so no audit could flag it, no rotation pass could act on it, and the
first symptom was an HTTP 401 in somebody's face — followed by the wrong
conclusion that the whole host was unreachable.

Nothing had ever compared the two sides. Expiry auditing only ever read our own
metadata, which is a record of what we believed when we wrote it down, not of
what is true at the issuer.

A token can describe itself (`GET /personal_access_tokens/self`), so the
comparison is cheap and exact: is it revoked, when does it really expire, what
is it really called, what can it really do.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.vault.skills import reconcile as reconcile_skill
from axiom.infra.skills import SkillContext, default_registry

SECRET = b"SUPERSECRET-VALUE-9f3c1"


@pytest.fixture
def ctx(tmp_path):
    store = ForeignCredentialStore(tmp_path)
    store.set("gl-unknown-expiry", SECRET, provider="gitlab-pat",
              issuer_url="https://git.example.org")
    store.set("gl-agrees", SECRET, provider="gitlab-pat",
              issuer_url="https://git.example.org", expires_at="2027-01-01")
    store.set("gl-orphan", SECRET, provider="gitlab-pat",
              issuer_url="https://git.example.org", expires_at="2027-01-01")
    store.set("hand-managed", SECRET, provider="guided")
    return SkillContext(registry=default_registry(), state_dir=tmp_path,
                        logger=logging.getLogger("t"))


def _describer(responses):
    def describe(name, meta):
        return responses.get(name, ("unsupported", None))
    return describe


def _run(ctx, responses, **params):
    return reconcile_skill.run(
        {"_describer": _describer(responses), **params}, ctx
    )


def test_a_credential_the_issuer_no_longer_recognises_is_orphaned(ctx):
    """The incident, exactly. We hold a value; the issuer has never heard of it."""
    result = _run(ctx, {
        "gl-orphan": ("rejected", None),
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
    })
    findings = {f["name"]: f for f in result.value["findings"]}
    assert findings["gl-orphan"]["status"] == "orphaned"
    assert result.ok is False


def test_a_missing_expiry_is_backfilled_from_the_issuer(ctx):
    """The single change that would have let this be seen coming.

    An expiry we never recorded is not unknowable — the issuer knows it. Asking
    turns an unauditable credential into an auditable one.
    """
    result = _run(ctx, {
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
    })
    finding = next(f for f in result.value["findings"] if f["name"] == "gl-unknown-expiry")
    assert finding["status"] == "expiry_unknown_locally"
    assert finding["issuer"]["expires_at"] == "2027-06-01"
    assert finding["proposed_metadata"]["expires_at"] == "2027-06-01"


def test_disagreement_is_reported_with_the_issuer_winning(ctx):
    """Our metadata is what we believed when we wrote it down. The issuer is
    what is true."""
    result = _run(ctx, {
        "gl-agrees": ("ok", {"expires_at": "2026-10-01", "revoked": False}),
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
    })
    finding = next(f for f in result.value["findings"] if f["name"] == "gl-agrees")
    assert finding["status"] == "expiry_drift"
    assert finding["stored"]["expires_at"] == "2027-01-01"
    assert finding["issuer"]["expires_at"] == "2026-10-01"
    assert finding["proposed_metadata"]["expires_at"] == "2026-10-01"


def test_a_revoked_credential_is_a_finding_even_while_it_still_answers(ctx):
    result = _run(ctx, {
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": True}),
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
    })
    finding = next(f for f in result.value["findings"] if f["name"] == "gl-agrees")
    assert finding["status"] == "revoked"


def test_everything_agreeing_reports_clean(ctx):
    result = _run(ctx, {
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
        "gl-unknown-expiry": ("ok", {"expires_at": None, "revoked": False}),
        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
    })
    statuses = {f["status"] for f in result.value["findings"]}
    assert statuses <= {"ok", "unsupported", "expiry_unknown_everywhere"}
    assert result.ok is True


def test_a_provider_we_cannot_query_is_unsupported_not_broken(ctx):
    """`guided` credentials have no issuer API. Reporting them as drifted would
    make the report noise, and a noisy report is one nobody reads."""
    result = _run(ctx, {"gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
                        "gl-unknown-expiry": ("ok", {"expires_at": None, "revoked": False}),
                        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False})})
    finding = next(f for f in result.value["findings"] if f["name"] == "hand-managed")
    assert finding["status"] == "unsupported"
    assert result.ok is True


def test_nothing_is_written_without_apply(ctx):
    """Reporting drift and silently rewriting metadata are different acts."""
    _run(ctx, {"gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
               "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
               "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False})})
    store = ForeignCredentialStore(ctx.state_dir)
    assert store.metadata("gl-unknown-expiry").get("expires_at") in (None, "")


def test_apply_backfills_only_metadata_never_a_credential(ctx):
    """Applying records a fact we just learned. It must not rotate, mint or
    touch a value — recording what the issuer says is not the same authority as
    changing what the credential is."""
    result = _run(ctx, {
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
    }, apply=True)

    store = ForeignCredentialStore(ctx.state_dir)
    assert store.metadata("gl-unknown-expiry")["expires_at"] == "2027-06-01"
    assert store.get("gl-unknown-expiry").value == SECRET, "the value was touched"
    assert "gl-unknown-expiry" in result.value["applied"]


def test_apply_does_not_try_to_fix_an_orphan(ctx):
    """An orphaned credential needs a human to mint a new one. Writing an
    expiry onto a dead value would make it look healthy — the precise failure
    mode this whole feature exists to end."""
    result = _run(ctx, {
        "gl-orphan": ("rejected", None),
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
    }, apply=True)
    assert "gl-orphan" not in result.value["applied"]
    assert any("gl-orphan" in e for e in result.errors)


def test_no_credential_value_appears_in_the_report(ctx):
    import json

    result = _run(ctx, {"gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
                        "gl-unknown-expiry": ("ok", {"expires_at": None, "revoked": False}),
                        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False})})
    assert SECRET.decode() not in json.dumps(result.value)


# --- proof of life: the substitute for an expiry we cannot know ---------------


def test_a_successful_check_records_proof_of_life(ctx):
    """For a credential we cannot date, "it worked a moment ago" is the only
    honest health signal available. Recording it turns "we have no idea" into
    "verified N days ago"."""
    result = _run(ctx, {
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
    }, apply=True)

    store = ForeignCredentialStore(ctx.state_dir)
    stamped = store.metadata("gl-agrees").get("last_verified_at")
    assert stamped, "a credential that answered was not stamped as verified"
    assert "gl-agrees" in result.value["verified"]


def test_a_failed_check_does_not_stamp_proof_of_life(ctx):
    """Stamping a dead credential as verified would be the worst possible
    outcome: it would look healthier after the check than before it."""
    _run(ctx, {
        "gl-orphan": ("rejected", None),
        "gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
    }, apply=True)
    store = ForeignCredentialStore(ctx.state_dir)
    assert not store.metadata("gl-orphan").get("last_verified_at")


def test_an_unreachable_issuer_does_not_stamp_either(ctx):
    """"Could not ask" is not "asked and it worked"."""
    _run(ctx, {
        "gl-agrees": ("unreachable", None),
        "gl-unknown-expiry": ("ok", {"expires_at": "2027-06-01", "revoked": False}),
        "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
    }, apply=True)
    store = ForeignCredentialStore(ctx.state_dir)
    assert not store.metadata("gl-agrees").get("last_verified_at")


def test_without_apply_no_proof_of_life_is_written(ctx):
    _run(ctx, {"gl-agrees": ("ok", {"expires_at": "2027-01-01", "revoked": False}),
               "gl-unknown-expiry": ("ok", {"expires_at": None, "revoked": False}),
               "gl-orphan": ("ok", {"expires_at": "2027-01-01", "revoked": False})})
    store = ForeignCredentialStore(ctx.state_dir)
    assert not store.metadata("gl-agrees").get("last_verified_at")
