# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A person the gate turns away is given somewhere to go, not a dead end.

The self-service key page told a signed-in person with no role to "ask whoever
runs this site" — true, and useless: they have already decided to act by the
time they read it, and the sentence hands them nothing to act on. The fix is a
single site setting (``AXIOM_BRAND_SUPPORT``, an email or a URL) surfaced as an
actionable link wherever the gate dead-ends. When it is not set the page still
renders the honest text — the config is the recourse, not a new wall.
"""

from __future__ import annotations

import sys

import pytest

if "fastapi" not in sys.modules:
    pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate import role_bundles
from axiom.extensions.builtins.webgate.api import routers


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(role_bundles, "_default", None)
    yield
    role_bundles._default = None


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(tmp_path / "api-keys.json"))
    app = FastAPI()
    app.include_router(routers.build_webgate_router(oidc=None, secure_cookies=False))
    return TestClient(app, follow_redirects=False)


def _signed_in_no_role(monkeypatch):
    monkeypatch.setattr(
        routers,
        "session_from_cookies",
        lambda *_a, **_k: {"sub": "u-1", "name": "Someone", "roles": [], "site": "example"},
    )


def test_a_configured_email_becomes_a_mailto_link(client, monkeypatch):
    monkeypatch.setenv("AXIOM_BRAND_SUPPORT", "rc-admin@example.edu")
    _signed_in_no_role(monkeypatch)
    page = client.get("/gate/keys")
    assert page.status_code == 200
    assert "no role" in page.text.lower()
    assert 'href="mailto:rc-admin@example.edu"' in page.text


def test_a_configured_url_becomes_a_request_link(client, monkeypatch):
    monkeypatch.setenv("AXIOM_BRAND_SUPPORT", "https://site.example/request-access")
    _signed_in_no_role(monkeypatch)
    page = client.get("/gate/keys")
    assert page.status_code == 200
    assert 'href="https://site.example/request-access"' in page.text


def test_without_the_setting_the_page_is_honest_not_broken(client, monkeypatch):
    monkeypatch.delenv("AXIOM_BRAND_SUPPORT", raising=False)
    _signed_in_no_role(monkeypatch)
    page = client.get("/gate/keys")
    assert page.status_code == 200
    assert "no role" in page.text.lower()
    # No empty/placeholder link when nothing is configured.
    assert "href=\"mailto:\"" not in page.text
    assert "href=\"\"" not in page.text


def test_an_unsafe_configured_value_is_not_rendered_as_a_link(client, monkeypatch):
    monkeypatch.setenv("AXIOM_BRAND_SUPPORT", "javascript:alert(1)")
    _signed_in_no_role(monkeypatch)
    page = client.get("/gate/keys")
    assert page.status_code == 200
    assert "javascript:alert(1)" not in page.text


def test_the_json_path_also_points_a_stuck_caller_somewhere(client, monkeypatch):
    """The CLI/API door hits the same wall; it should carry the recourse too."""
    monkeypatch.setenv("AXIOM_BRAND_SUPPORT", "https://site.example/request-access")
    _signed_in_no_role(monkeypatch)
    reply = client.post("/gate/keys", json={})
    assert reply.status_code == 403
    detail = reply.json().get("detail", "")
    assert "https://site.example/request-access" in detail


def test_the_json_path_is_clean_when_nothing_is_configured(client, monkeypatch):
    monkeypatch.delenv("AXIOM_BRAND_SUPPORT", raising=False)
    _signed_in_no_role(monkeypatch)
    reply = client.post("/gate/keys", json={})
    assert reply.status_code == 403
    assert "Request access:" not in reply.json().get("detail", "")
