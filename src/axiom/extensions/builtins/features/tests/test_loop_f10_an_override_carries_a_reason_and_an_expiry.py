# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F10: overrides can carry a reason and an expiry; a protected flag accepts only a
reasoned override there; an expired override is ignored and reported."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.flags import cli
from axiom.extensions.builtins.features.resolver import FlagError
from axiom.extensions.builtins.features.tests.conftest import mcp, svc, web


@pytest.mark.acceptance("LOOP-F10")
def test_an_override_stores_its_reason_and_expiry(loop):
    later = datetime.now(UTC) + timedelta(days=3)
    row = svc().set_override("user", "ana", "beta_editor", "on", by="@t", reason="demo", expires_at=later)
    assert row["reason"] == "demo" and row["expires_at"].startswith(later.date().isoformat())
    out = svc().explain("beta_editor", user="ana")
    assert out["decided_by"] == "user" and out["detail"]["reason"] == "demo"
    assert out["detail"]["expires_at"]


@pytest.mark.acceptance("LOOP-F10")
def test_an_expired_override_is_ignored_and_reported(loop):
    s = svc()
    s.overrides.put("account", "acme", "beta_editor", "on", by="@t", reason="trial",
                    expires_at=datetime.now(UTC) - timedelta(minutes=1))
    out = s.explain("beta_editor", account="acme")
    assert out["value"] == "off" and out["decided_by"] == "default"
    assert out["ignored"][0]["step"] == "account" and out["ignored"][0]["status"] == "expired"
    assert s.resolve(account="acme").sources["beta_editor"] == "default"


@pytest.mark.acceptance("LOOP-F10")
def test_an_expiry_in_the_past_is_refused(loop):
    with pytest.raises(FlagError, match="past"):
        svc().set_override("user", "ana", "beta_editor", "on", by="@t",
                           expires_at=datetime.now(UTC) - timedelta(days=1))


@pytest.mark.acceptance("LOOP-F10")
def test_a_protected_flag_accepts_only_a_reasoned_override_there(loop, monkeypatch, capsys):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    s = svc()
    with pytest.raises(FlagError, match="reason") as exc:
        s.set_override("user", "ana", "guarded", "on", by="@t")
    assert exc.value.code == "protected"
    s.set_override("user", "ana", "guarded", "on", by="@t", reason="incident follow-up")
    assert s.explain("guarded", user="ana")["decided_by"] == "user"
    # An unreasoned row (written before this rule, or by hand) is ignored there and reported.
    s.overrides.put("account", "acme", "guarded", "on", by="@t")
    out = s.explain("guarded", account="acme")
    assert out["value"] == "off"
    assert out["ignored"][0]["status"] == "unreasoned_on_protected"
    # Every surface takes the reason.
    assert cli.main(["set", "guarded", "on", "--scope", "user", "--id", "bob", "--reason", "support"]) == 0
    assert mcp("axiom_flags__set", {"key": "guarded", "value": "on", "scope": "user", "scope_id": "cy",
                                      "reason": "support"}, loop["state"])["ok"]
    client = web(monkeypatch, sub="dee")
    assert client.patch("/api/v1/features/guarded", json={"value": "on"}).status_code == 403
    r = client.patch("/api/v1/features/guarded", json={"value": "on", "reason": "beta tester"})
    assert r.status_code == 200 and r.json()["flags"]["guarded"] == "on"


@pytest.mark.acceptance("LOOP-F10")
def test_the_cli_takes_an_expiry(loop):
    assert cli.main(["set", "beta_editor", "on", "--scope", "user", "--id", "ana",
                     "--reason", "demo", "--expires", "2999-01-01T00:00:00Z"]) == 0
    row = [r for r in loop["store"].rows("beta_editor") if r["scope_id"] == "ana"][0]
    assert row["reason"] == "demo" and row["expires_at"].startswith("2999-01-01")
