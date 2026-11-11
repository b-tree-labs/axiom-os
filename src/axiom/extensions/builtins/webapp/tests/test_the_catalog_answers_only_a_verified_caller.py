# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The catalog answers a verified caller, and only for the sites that caller holds.

Found on a site node after a deploy (2026-10-30): ``GET /api/v1/sites`` and
``/api/v1/sites/<site>/channels`` returned every hosted site's channel names, row
counts and date ranges to a request carrying no credential at all. The ``/api/v1``
mount had its gate off and left authentication to each route, and these routes
never checked. With no credential the site scope had nothing to narrow by, so it
fell back to the deployment's whole bound.

These run through the composed app and the real authz middleware, because the
defect was in the composition, not in a handler: a bare ``FastAPI()`` with the
router included passes every one of them with the gate off.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.registry import RouterRegistry
from axiom.extensions.builtins.webapp.catalog import models, store, sync
from axiom.extensions.builtins.webapp.mount import mount_spec
from axiom.governance import Decision, Verdict

HOME = "example-home"
PARTNER = "example-loop"
OTHER = "example-other"


class _Keys:
    """The issued-keys store shape: token -> (principal, scopes, site)."""

    def __init__(self, table: dict) -> None:
        self._table = table

    def resolve(self, token):
        found = self._table.get(token)
        if found is None:
            return None
        principal, scopes, site = found
        return SimpleNamespace(
            principal=principal, scopes=scopes, key_id=token[-4:], name="k", site=site
        )


KEYS = {
    "axk_home_viewer": ("@viewer:example-home", ("*:read",), HOME),
    "axk_partner_viewer": ("@viewer:example-loop", ("*:read",), PARTNER),
    "axk_owner": ("@owner:example-home", ("*",), HOME),
}


def _permit(_envelope):
    return Verdict.from_decision(Decision.PERMIT, "test permits", f"t-{uuid.uuid4().hex}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", HOME)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    models.Base.metadata.create_all(engine)
    s = Session(engine)
    s.execute(
        text(
            "CREATE TABLE signals (site TEXT, feed TEXT, channel TEXT, ts TEXT, "
            "value REAL, unit TEXT)"
        )
    )
    ts = datetime(2026, 9, 15, tzinfo=UTC).isoformat()
    for site, channel in ((HOME, "fuel_temp_1"), (PARTNER, "loop_flow"), (OTHER, "loop_dp")):
        s.execute(
            text("INSERT INTO signals VALUES (:a, 'main', :c, :t, 1.0, '')"),
            {"a": site, "c": channel, "t": ts},
        )
    s.commit()
    sync.project_catalog(s, source="signals")

    @contextlib.contextmanager
    def _provider():
        yield s

    monkeypatch.setattr(store, "_provider", _provider)

    registry = RouterRegistry()
    registry.register(mount_spec())
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=_Keys(KEYS)), decide_fn=_permit
    )
    app = compose_app(
        registry=registry,
        include_builtins=False,
        middleware=MiddlewareConfig(authz=hook),
        auto_authz=False,
    )
    yield TestClient(app)
    s.close()


def _get(client, path, key=None):
    return client.get(path, headers={"Authorization": f"Bearer {key}"} if key else {})


def _sites(response):
    assert response.status_code == 200, response.text
    return {row["site"] for row in response.json()["sites"]}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/sites",
        f"/api/v1/sites/{HOME}/channels",
        f"/api/v1/sites/{PARTNER}/channels",
        "/api/v1/health/sites",
        "/api/v1/binding",
    ],
)
def test_no_credential_is_refused_before_the_handler(client, path):
    response = _get(client, path)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "invalid_credential"
    assert PARTNER not in response.text and HOME not in response.text


def test_an_unknown_key_is_refused(client):
    assert _get(client, "/api/v1/sites", "axk_not_issued").status_code == 403


def test_the_home_sites_key_lists_only_the_home_site(client):
    assert _sites(_get(client, "/api/v1/sites", "axk_home_viewer")) == {HOME}


def test_a_partners_key_lists_only_its_own_site(client):
    assert _sites(_get(client, "/api/v1/sites", "axk_partner_viewer")) == {PARTNER}


def test_the_owners_key_lists_every_site(client):
    assert _sites(_get(client, "/api/v1/sites", "axk_owner")) == {HOME, PARTNER, OTHER}


def test_channels_answer_for_the_keys_own_site_and_404_for_another(client):
    own = _get(client, f"/api/v1/sites/{PARTNER}/channels", "axk_partner_viewer")
    assert own.status_code == 200, own.text
    assert [c["channel"] for c in own.json()["channels"]] == ["loop_flow"]

    for site in (HOME, OTHER):
        other = _get(client, f"/api/v1/sites/{site}/channels", "axk_partner_viewer")
        # 404, not 403: a refusal would confirm the other site is hosted here.
        assert other.status_code == 404, other.text
        assert "loop_dp" not in other.text and "fuel_temp_1" not in other.text


def test_the_owners_key_reads_any_sites_channels(client):
    for site in (HOME, PARTNER, OTHER):
        assert _get(client, f"/api/v1/sites/{site}/channels", "axk_owner").status_code == 200


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/version"])
def test_liveness_and_build_identity_stay_public(client, path):
    assert _get(client, path).status_code == 200
