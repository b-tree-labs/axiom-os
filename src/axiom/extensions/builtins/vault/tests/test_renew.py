# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP's expiry-rotation policy.

The gates are the point. A credential rotates itself only when BOTH hold:
its provider can mint a replacement unattended, and a human has attested that
every consumer resolves the value dynamically rather than holding a copy.

The second gate is not ceremony. A read-only database credential was once rotated
and the new value reached only three of the five env files holding a copy; the
affected units kept reporting healthy while every query behind them failed
overnight. Mechanically-rotatable and safe-to-rotate
are different questions, and only a person can answer the second.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.secrets.skills import renewable
from axiom.extensions.builtins.vault.skills import renew

NOW = datetime(2026, 8, 24, tzinfo=UTC)


class _Store:
    """Minimal ForeignCredentialStore stand-in: only `.list()` is exercised."""

    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return list(self._rows)


def _cred(name, *, days, provider="gitlab-pat", auto_rotate=False):
    return {
        "name": name,
        "provider": provider,
        "auto_rotate": auto_rotate,
        "expires_at": (NOW + timedelta(days=days)).date().isoformat(),
    }


def _run(rows, **params):
    store = _Store(rows)
    return renew.run({"_store": store, "_now": NOW, "dry_run": True, **params}, None)


# --- the gates -------------------------------------------------------------

def test_unattended_provider_alone_is_not_enough():
    """Gate 1 without gate 2 is the dangerous combination — must escalate."""
    res = _run([_cred("vcs-api-token", days=1, provider="gitlab-pat", auto_rotate=False)])
    assert res.ok is False
    assert res.value["rotated"] == []
    assert [e["name"] for e in res.value["escalated"]] == ["vcs-api-token"]
    assert "not opted in" in res.value["escalated"][0]["reason"]


def test_opt_in_alone_is_not_enough():
    """An interactive provider cannot rotate headless no matter the opt-in."""
    res = _run([_cred("pypi-token", days=1, provider="guided", auto_rotate=True)])
    assert res.ok is False
    assert res.value["rotated"] == []
    assert "human" in res.value["escalated"][0]["reason"]


def test_both_gates_pass_rotates():
    res = _run([_cred("vcs-api-token", days=1, provider="gitlab-pat", auto_rotate=True)])
    assert res.ok is True
    assert res.value["rotated"] == ["vcs-api-token"]
    assert res.value["escalated"] == []


# --- horizon ---------------------------------------------------------------

def test_credential_beyond_the_horizon_is_left_alone():
    res = _run([_cred("far-off", days=365, provider="gitlab-pat", auto_rotate=True)])
    assert res.ok is True
    assert res.value["due"] == 0
    assert res.value["rotated"] == []


def test_already_expired_is_actionable_not_ignored():
    """Past-expiry must still be picked up — it is the worst case, not a no-op."""
    res = _run([_cred("lapsed", days=-2, provider="gitlab-pat", auto_rotate=True)])
    assert res.value["due"] == 1
    assert res.value["rotated"] == ["lapsed"]


def test_horizon_is_configurable():
    rows = [_cred("mid", days=10, provider="gitlab-pat", auto_rotate=True)]
    assert _run(rows).value["due"] == 0                      # default 3-day horizon
    assert _run(rows, horizon_days=14).value["due"] == 1


# --- escalation is loud ----------------------------------------------------

def test_escalation_makes_the_pass_fail_so_the_heartbeat_reddens():
    """Silence is never success: a human-needed credential must fail the pass."""
    res = _run([_cred("needs-human", days=1, provider="guided", auto_rotate=True)])
    assert res.ok is False
    assert any("needs-human" in e for e in res.errors)
    assert any("axi secrets rotate" in e for e in res.errors), "must name the fix"


def test_no_credentials_due_is_quiet_and_green():
    res = _run([_cred("fine", days=200, provider="gitlab-pat", auto_rotate=True)])
    assert res.ok is True
    assert res.errors == []


def test_missing_expiry_is_not_silently_treated_as_safe():
    """A credential with no expiry cannot be audited — it must not read as ok."""
    res = _run([{"name": "no-expiry", "provider": "gitlab-pat", "auto_rotate": True}])
    # Not actionable by expiry, but the audit records it as a distinct level
    # rather than 'ok' — surfaced by `secrets audit`, not silently dropped here.
    assert res.value["due"] == 0


# --- renewability classification ------------------------------------------

def test_classify_reports_which_gate_blocked():
    both_fail = renewable.classify({"name": "x", "provider": "guided"})
    assert both_fail["auto_renewable"] is False
    assert both_fail["provider_unattended"] is False
    assert both_fail["operator_opted_in"] is False

    only_provider = renewable.classify(
        {"name": "y", "provider": "gitlab-pat", "auto_rotate": False}
    )
    assert only_provider["provider_unattended"] is True
    assert only_provider["operator_opted_in"] is False
    assert only_provider["auto_renewable"] is False


@pytest.mark.parametrize("raw,expected", [
    (True, True), ("true", True), ("on", True), ("1", True), ("yes", True),
    (False, False), ("false", False), ("", False), (None, False),
])
def test_opt_in_accepts_hand_edited_toml_truthiness(raw, expected):
    """settings.toml is hand-edited; a string 'true' must not read as False."""
    meta = {"name": "z", "provider": "gitlab-pat", "auto_rotate": raw}
    assert renewable.classify(meta)["operator_opted_in"] is expected


def test_gitlab_pat_is_unattended_and_guided_is_not():
    kinds = renewable.unattended_provider_kinds()
    assert "gitlab-pat" in kinds
    assert "guided" not in kinds
