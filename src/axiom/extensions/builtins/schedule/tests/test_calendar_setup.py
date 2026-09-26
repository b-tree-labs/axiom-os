# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Calendar setup utility: key validation, gcloud command generation, and the
access doctor that turns a raw 403/404 into a precise next action."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.schedule.calendar import setup


def _write_sa(tmp_path, **over):
    data = {
        "type": "service_account",
        "client_email": "axiom-pulse-calendar@axiom-support.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\n...",
        "project_id": "axiom-support",
    }
    data.update(over)
    f = tmp_path / "sa.json"
    f.write_text(json.dumps(data))
    return str(f)


def test_load_service_account_valid(tmp_path):
    sa = setup.load_service_account(_write_sa(tmp_path))
    assert sa["client_email"].endswith("gserviceaccount.com")


def test_load_service_account_errors(tmp_path):
    with pytest.raises(setup.SetupError):
        setup.load_service_account(str(tmp_path / "nope.json"))      # missing file
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(setup.SetupError):
        setup.load_service_account(str(bad))                          # not JSON
    missing = tmp_path / "m.json"
    missing.write_text(json.dumps({"type": "service_account", "client_email": "x"}))
    with pytest.raises(setup.SetupError):
        setup.load_service_account(str(missing))                      # missing fields
    notsa = tmp_path / "u.json"
    notsa.write_text(json.dumps({"type": "authorized_user"}))
    with pytest.raises(setup.SetupError):
        setup.load_service_account(str(notsa))                        # wrong type


def test_gcloud_bootstrap_commands():
    cmds = setup.gcloud_bootstrap_commands(project_id="axiom-support", key_path="/tmp/k.json")
    assert cmds[0] == "gcloud config set project axiom-support"
    assert any("services enable calendar-json.googleapis.com" in c for c in cmds)
    assert any("axiom-pulse-calendar@axiom-support.iam.gserviceaccount.com" in c for c in cmds)
    assert any("keys create /tmp/k.json" in c for c in cmds)


def test_preflight_configured():
    class _Ok:
        def list_events(self, **kw):
            return []

    r = setup.preflight(calendar_id="c", sa_email="sa@x", provider=_Ok())
    assert r["state"] == "configured" and r["remediation"] is None


def test_preflight_404_tells_you_to_share():
    class _P:
        def list_events(self, **kw):
            raise RuntimeError("<HttpError 404 ... Not Found>")

    r = setup.preflight(calendar_id="cal123", sa_email="sa@x.iam", provider=_P())
    assert r["state"] == "broken"
    assert "Share calendar cal123 with sa@x.iam" in r["remediation"]


def test_preflight_api_not_enabled_tells_you_to_enable():
    class _P:
        def list_events(self, **kw):
            raise RuntimeError("Google Calendar API has not been enabled for project")

    r = setup.preflight(calendar_id="c", sa_email="sa", provider=_P())
    assert "Enable the Google Calendar API" in r["remediation"]


def test_doctor_validates_key_then_probes(tmp_path):
    class _Ok:
        def list_events(self, **kw):
            return []

    r = setup.doctor(credentials_file=_write_sa(tmp_path), calendar_id="c", provider=_Ok())
    assert r["state"] == "configured"
    assert r["sa_email"].endswith("gserviceaccount.com")


def test_provision_creates_shares_and_verifies(tmp_path):
    from axiom.extensions.builtins.schedule.calendar.vendors.fake import (
        FakeCalendarProvider,
    )

    fake = FakeCalendarProvider()
    r = setup.provision(
        credentials_file=_write_sa(tmp_path), share_with="ben@x.com", provider=fake,
    )
    assert r["verified"] is True
    assert r["calendar_id"] in fake.calendars                       # calendar created
    assert ("ben@x.com", "owner") in fake.acl[r["calendar_id"]]     # shared back to user
    assert len(r["next_fires"]) == 3                                # Mon/Wed/Fri bound + computed
    assert fake._events == {}                                       # round-trip event cleaned up


def test_provision_fails_clearly_when_api_unreachable(tmp_path):
    from axiom.extensions.builtins.schedule.calendar.vendors.fake import (
        FakeCalendarProvider,
    )

    fake = FakeCalendarProvider({"healthy": False})
    with pytest.raises(setup.SetupError):
        setup.provision(credentials_file=_write_sa(tmp_path), share_with="x@y", provider=fake)


# --- M365: the cryptic-error translator (the admin dread-killer) ---

def test_m365_doctor_configured():
    class _Ok:
        def list_events(self, **kw):
            return []

    assert setup.m365_doctor(config={"user_id": "u@x"}, provider=_Ok())["state"] == "configured"


def test_m365_doctor_translates_consent_error():
    class _P:
        def list_events(self, **kw):
            raise RuntimeError("ErrorAccessDenied: Access is denied. (403)")

    r = setup.m365_doctor(config={"user_id": "u@x"}, provider=_P())
    assert r["kind"] == "consent" and "Grant admin consent" in r["remediation"]


def test_m365_doctor_translates_secret_and_tenant_errors():
    class _Secret:
        def list_events(self, **kw):
            raise RuntimeError("AADSTS7000215: Invalid client secret provided.")

    class _Tenant:
        def list_events(self, **kw):
            raise RuntimeError("AADSTS90002: Tenant 'foo' not found.")

    assert setup.m365_doctor(config={"user_id": "u@x"}, provider=_Secret())["kind"] == "secret"
    assert setup.m365_doctor(config={"user_id": "u@x"}, provider=_Tenant())["kind"] == "tenant"


def test_m365_provision_creates_calendar_in_mailbox_and_verifies():
    from axiom.extensions.builtins.schedule.calendar.vendors.fake import (
        FakeCalendarProvider,
    )

    fake = FakeCalendarProvider()
    r = setup.m365_provision(config={"user_id": "u@x"}, provider=fake)
    assert r["verified"] is True
    assert r["calendar_id"] in fake.calendars      # created in the mailbox (no share step)
    assert len(r["next_fires"]) == 3
