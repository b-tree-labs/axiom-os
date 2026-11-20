# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Prebuilt groups: the node's groups, the same for every account, never copied per account."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.charts import kinds_group as kg
from axiom.extensions.builtins.charts import prebuilt
from axiom.extensions.builtins.charts.tests.test_mvp_03_kinds_group import (  # noqa: F401
    _ctx,
    site,
    views,
)
from axiom.extensions.builtins.scidisplay import unified_spec as us


def test_a_registered_config_is_a_prebuilt_group_with_nothing_saved(site) -> None:  # noqa: F811
    _, state = site
    groups = prebuilt.listing(state)
    assert [g["name"] for g in groups] == ["Chart kinds · Site A"]
    g = groups[0]
    assert g["prebuilt"] is True and g["id"] == "kinds-site-a"
    assert len(g["graphs"]) == len(us.registered_kinds())
    assert "featured" not in g


def test_every_account_gets_the_same_group_from_the_skill(site, views) -> None:  # noqa: F811
    from axiom.extensions.builtins.charts.skills import prebuilt_groups
    from axiom.extensions.builtins.webapp.views import read_view

    _, state = site
    a = prebuilt_groups({}, _ctx(state, "probe@local"))
    b = prebuilt_groups({}, _ctx(state, "ac5f42d3-0000-4000-8000-000000000000"))
    assert a.ok and b.ok and a.value == b.value and a.value["prebuilt"]
    # Neither account holds a copy.
    assert read_view("probe@local", "groups") is None and read_view("ac5f42d3-0000-4000-8000-000000000000", "groups") is None


def test_the_skill_needs_somebody_signed_in(site) -> None:  # noqa: F811
    import dataclasses

    from axiom.extensions.builtins.charts.skills import prebuilt_groups
    from axiom.infra.principal import PrincipalContext

    _, state = site
    nobody = dataclasses.replace(_ctx(state), principal=PrincipalContext(handle=""))
    assert not prebuilt_groups({}, nobody).ok


def test_a_group_whose_day_is_too_young_is_not_offered_and_does_not_hide_the_rest(site, monkeypatch) -> None:  # noqa: F811
    _, state = site
    monkeypatch.setattr(kg, "check_delay", lambda *a, **k: (_ for _ in ()).throw(kg.KindsError("too young")))
    assert prebuilt.listing(state) == []


def test_a_second_provider_joins_without_special_cases(site, monkeypatch) -> None:  # noqa: F811
    _, state = site
    other = {"id": "ops-1", "name": "Operations", "graphs": [], "layout": "2"}
    monkeypatch.setattr(prebuilt, "PROVIDERS", [*prebuilt.PROVIDERS, lambda _s: [other]])
    assert [g["id"] for g in prebuilt.listing(state)] == ["kinds-site-a", "ops-1"]


def test_the_http_listing_is_access_checked_and_identical_across_accounts(site, monkeypatch) -> None:  # noqa: F811
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.charts.api import register_routes

    _, state = site
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: state)
    app = fastapi.FastAPI()
    who = {"email": ""}

    @app.middleware("http")
    async def _signed_in(request, call_next):  # noqa: ANN001
        if who["email"]:
            request.state.user = SimpleNamespace(email=who["email"])
        return await call_next(request)

    router = fastapi.APIRouter()
    register_routes(router)
    app.include_router(router, prefix="/api/v1")
    c = TestClient(app)
    assert c.get("/api/v1/charts/groups").status_code == 401
    who["email"] = "probe@local"
    a = c.get("/api/v1/charts/groups")
    who["email"] = "chart-kinds-demo@local"
    b = c.get("/api/v1/charts/groups")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() and a.json()["prebuilt"][0]["id"] == "kinds-site-a"
