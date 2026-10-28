# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A credential bound to a tenant site reads that site and no other.

A rehearsal on a live node signed in a throwaway account belonging to one
tenant, minted its own key, and read another tenant's series by naming it in
the query, and the node's home site by naming nothing. Site reads were decided
in the open posture: any identified caller could read any site, and a rule
could only name a literal handle, so no operator rule could say "a tenant
reads its own site".

The credential already says which site it belongs to (an issued key's site is
its handle's context, a session carries its account's site). This makes that
binding the boundary:

- A caller whose credential is bound to a site that is not one of this node's
  home sites may read only that site, and a read that names no site means it.
- A caller bound to a home site, or to no site, is unchanged.
"""

from __future__ import annotations

import pytest

from axiom.infra import site_authority as sa
from axiom.infra.site_authority import SiteNotAuthorized, authorize_site


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", "home-site")
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    sa._reset_default_site_context()
    yield
    sa._reset_default_site_context()


def _read(requested, *, caller, bound="home-site", principal="@p:tenant-a"):
    return authorize_site(requested, principal=principal, bound=bound, caller_site=caller)


def test_a_tenant_reads_its_own_site():
    assert _read("tenant-a", caller="tenant-a") == "tenant-a"


def test_naming_nothing_means_the_tenants_own_site_not_the_nodes():
    assert _read("", caller="tenant-a") == "tenant-a"


@pytest.mark.parametrize("other", ["tenant-b", "home-site"])
def test_a_tenant_cannot_read_another_site_by_naming_it(other):
    with pytest.raises(SiteNotAuthorized, match="tenant-a"):
        _read(other, caller="tenant-a")


def test_a_home_site_credential_is_unchanged():
    assert _read("tenant-b", caller="home-site", principal="@p:home-site") == "tenant-b"
    assert _read("", caller="home-site", principal="@p:home-site") == "home-site"


def test_the_bound_site_counts_as_home_under_another_id():
    # A node can be bound under one id while its accounts carry another.
    assert _read("tenant-b", caller="bound-id", bound="bound-id") == "tenant-b"


def test_an_operator_can_name_further_home_sites(monkeypatch):
    monkeypatch.setenv("AXIOM_HOME_SITES", "partner-lab, other")
    assert _read("tenant-b", caller="partner-lab") == "tenant-b"


def test_a_caller_with_no_site_binding_is_unchanged():
    assert _read("tenant-b", caller=None, principal="@p") == "tenant-b"


# --- through the served path: the binding comes from the credential --------


def _served(tmp_path, *, principal):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
    from axiom.extensions.builtins.http.middleware import MiddlewareConfig, install_middleware
    from axiom.governance import Decision, Verdict
    from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

    keys = tmp_path / "keys.json"
    token, record = mint_api_key(principal=principal, scopes=("*:read",))
    append_api_key_record(keys, record)
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys)),
        decide_fn=lambda env: Verdict.from_decision(Decision.PERMIT, "ok", "r"),
    )
    app = fastapi.FastAPI()

    @app.get("/read")
    def read(site: str = ""):  # sync, like the data routes: runs in a worker thread
        try:
            return {"site": authorize_site(site, principal="@x", bound="home-site")}
        except SiteNotAuthorized:
            raise fastapi.HTTPException(status_code=403)

    install_middleware(app, MiddlewareConfig(authz=hook))
    client = TestClient(app)
    return lambda q="": client.get("/read", params={"site": q} if q else None,
                                   headers={"authorization": f"Bearer {token}"})


def test_a_tenant_key_is_held_to_its_site_on_a_served_read(tmp_path):
    get = _served(tmp_path, principal="@reader:tenant-a")
    assert get().json() == {"site": "tenant-a"}
    assert get("tenant-a").json() == {"site": "tenant-a"}
    assert get("tenant-b").status_code == 403
    assert get("home-site").status_code == 403


def test_a_home_key_still_reads_any_site_on_a_served_read(tmp_path):
    get = _served(tmp_path, principal="@reader:home-site")
    assert get("tenant-b").json() == {"site": "tenant-b"}


def test_the_binding_does_not_outlive_its_request(tmp_path):
    get = _served(tmp_path, principal="@reader:tenant-a")
    assert get().json() == {"site": "tenant-a"}
    assert sa.caller_site() is None
