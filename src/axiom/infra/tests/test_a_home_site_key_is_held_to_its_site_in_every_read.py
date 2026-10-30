# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A key minted for the node's home site reads the home site, and nothing else.

Found on a live multi-site node: a viewer key (``*:read``) bound to the node's
home site listed the channels of every partner site hosted beside it, through
the catalogue reads of the data door, and counted their readings. The home site
was an exemption in :func:`axiom.infra.site_authority.tenant_scope`: a credential
bound to it was treated as an operator, whatever its role. ADR-203 says the
opposite ("any credential bound to a site, the node's home site included, sees
that site's objects only"), and partner isolation depends on it.

What holds now, in every read verb and every listing:

- a home-site viewer key is held to the home site, and is refused another site
  by name rather than answered with an empty set;
- a partner key is held to its own site, as before;
- a key holding the owner's grant (scope ``*``) reads every site;
- a caller with no site binding (an operator at the terminal) is unchanged.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from axiom.infra import site_authority as sa
from axiom.infra import site_scope

HOME, PARTNERS = "home-site", ("partner-a", "partner-b", "partner-c")
ALL = (HOME, *PARTNERS)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_SITE", HOME)
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    monkeypatch.delenv(site_scope.SERVED_SITES_ENV, raising=False)
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))
    sa._reset_default_site_context()
    yield
    sa._reset_default_site_context()


class bound:
    """The credential's site and grant, as the HTTP authz middleware binds them."""

    def __init__(self, site, *, every_site=False):
        self.site, self.every_site = site, every_site

    def __enter__(self):
        self._tok = sa.bind_caller_site(self.site, every_site=self.every_site)

    def __exit__(self, *exc):
        sa.unbind_caller_site(self._tok)


# --- the scope itself ---------------------------------------------------------


def test_a_home_site_key_is_a_tenant_of_the_home_site():
    with bound(HOME):
        assert sa.tenant_scope() == HOME


def test_a_partner_key_is_still_a_tenant_of_its_own_site():
    with bound("partner-a"):
        assert sa.tenant_scope() == "partner-a"


@pytest.mark.parametrize("site", [HOME, "partner-a"])
def test_the_owners_grant_reads_every_site(site):
    with bound(site, every_site=True):
        assert sa.tenant_scope() is None
        assert sa.authorize_site("partner-b", principal="@owner") == "partner-b"


def test_no_site_binding_is_unchanged():
    with bound(None):
        assert sa.tenant_scope() is None
        assert sa.authorize_site("partner-b", principal="@op", bound=HOME) == "partner-b"


@pytest.mark.parametrize("other", PARTNERS)
def test_a_home_site_key_naming_a_partner_is_refused_by_name(other):
    with bound(HOME), pytest.raises(sa.SiteNotAuthorized, match=other):
        sa.authorize_site(other, principal="@viewer:home-site", bound=HOME)


# --- every listing --------------------------------------------------------------


@pytest.mark.parametrize(
    ("site", "every", "expected"),
    [
        (HOME, False, [HOME]),
        ("partner-a", False, ["partner-a"]),
        (HOME, True, list(ALL)),
        (None, False, list(ALL)),
    ],
)
def test_a_listing_shows_only_the_sites_the_credential_holds(site, every, expected):
    with bound(site, every_site=every):
        assert site_scope.resolve().filter(list(ALL)) == expected


@pytest.mark.parametrize("other", PARTNERS)
def test_a_listing_refuses_to_open_a_partner_for_a_home_site_key(other):
    with bound(HOME):
        site_scope.resolve().require(HOME)
        with pytest.raises(site_scope.SiteOutOfScope):
            site_scope.resolve().require(other)


# --- every gold read verb -------------------------------------------------------


class _Scope(Exception):
    """Carries the ``site_scope`` a verb handed the query builder."""

    def __init__(self, scope):
        super().__init__(scope)
        self.scope = scope


def _capture(*_a, site_scope=None, **_k):
    raise _Scope(site_scope)


GOLD_VERBS = {
    "aggregate": ("aggregate", {"table": "signals", "column": "value", "fn": "count"}),
    "series": ("series", {"table": "signals", "column": "value", "bucket": "1 hour",
                          "time_column": "ts"}),
    "roles": ("roles", {}),
    "compare": ("compare", {"role": "temperature", "bucket": "1 hour"}),
    "uncertainty_coverage": ("uncertainty_coverage", {"table": "signals"}),
}


@pytest.fixture
def gold(monkeypatch):
    from axiom.extensions.builtins.data_platform import gold_query as gq
    from axiom.extensions.builtins.data_platform.skills import gold as gold_skills

    monkeypatch.setattr(gold_skills, "_GUARD", None)
    # No database: the verb's query builder is handed the scope and reports it.
    monkeypatch.setattr(gold_skills, "_run", lambda params, ctx, fn, **kw: fn(None))
    for name, _ in GOLD_VERBS.values():
        monkeypatch.setattr(gq, name, _capture)
    return gold_skills


def _scope_of(gold_skills, verb, params, tmp_path):
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext, SkillRegistry

    ctx = SkillContext(
        registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"),
        principal=PrincipalContext(handle="@viewer:home-site"), surface="mcp",
    )
    with pytest.raises(_Scope) as got:
        getattr(gold_skills, verb)(dict(params), ctx)
    return got.value.scope


@pytest.mark.parametrize("verb", sorted(GOLD_VERBS))
@pytest.mark.parametrize(
    ("site", "every", "expected"),
    [(HOME, False, HOME), ("partner-a", False, "partner-a"), (HOME, True, None), (None, False, None)],
)
def test_every_gold_verb_is_held_to_the_credentials_site(gold, tmp_path, verb, site, every, expected):
    name, params = GOLD_VERBS[verb]
    with bound(site, every_site=every):
        assert _scope_of(gold, name, params, tmp_path) == expected


@pytest.mark.parametrize("other", PARTNERS)
def test_the_held_scope_refuses_a_filter_naming_a_partner(other):
    from axiom.extensions.builtins.data_platform import gold_query as gq

    with bound(HOME):
        scope = sa.tenant_scope()
    clauses = list(gq.parse_filter(f"site = '{other}'"))
    with pytest.raises(gq.TenantScopeError, match=other):
        gq.scope_clause({"site", "ts"}, scope, table="signal_catalogue", clauses=clauses)
    held = gq.scope_clause({"site", "ts"}, scope, table="signal_catalogue")
    assert held is not None and held.values == (HOME,)


# --- through the served path: the grant comes from the credential ---------------


def _served(tmp_path, monkeypatch, *, principal, scopes=("*:read",)):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
    from axiom.extensions.builtins.http.middleware import MiddlewareConfig, install_middleware
    from axiom.extensions.builtins.webapp import bound_web
    from axiom.extensions.builtins.webapp.api.catalog import register_catalog_routes
    from axiom.extensions.builtins.webapp.catalog import store
    from axiom.governance import Decision, Verdict
    from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

    @contextmanager
    def _session():
        yield None

    monkeypatch.setattr(bound_web, "forwarding", lambda: False)
    monkeypatch.setattr(store, "session_scope", _session)
    monkeypatch.setattr(store, "read_site_summaries",
                        lambda s: [{"site": n, "rows": 1} for n in ALL])
    monkeypatch.setattr(store, "read_channels", lambda s, site: [{"channel": f"{site}:x"}])

    keys = tmp_path / "keys.json"
    token, record = mint_api_key(principal=principal, scopes=scopes)
    append_api_key_record(keys, record)
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys)),
        decide_fn=lambda env: Verdict.from_decision(Decision.PERMIT, "ok", "r"),
    )
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    register_catalog_routes(router)
    app.include_router(router)
    install_middleware(app, MiddlewareConfig(authz=hook))
    client = TestClient(app)
    return lambda path: client.get(path, headers={"authorization": f"Bearer {token}"})


def _listed(get):
    return [row["site"] for row in get("/api/v1/sites").json()["sites"]]


def test_a_home_site_viewer_key_lists_and_opens_only_the_home_site(tmp_path, monkeypatch):
    get = _served(tmp_path, monkeypatch, principal="@viewer:home-site")
    assert _listed(get) == [HOME]
    assert get(f"/api/v1/sites/{HOME}/channels").status_code == 200
    for other in PARTNERS:
        assert get(f"/api/v1/sites/{other}/channels").status_code == 404


def test_a_partner_key_lists_and_opens_only_its_own_site(tmp_path, monkeypatch):
    get = _served(tmp_path, monkeypatch, principal="@viewer:partner-a")
    assert _listed(get) == ["partner-a"]
    for other in (HOME, "partner-b"):
        assert get(f"/api/v1/sites/{other}/channels").status_code == 404


def test_an_owner_key_lists_every_site(tmp_path, monkeypatch):
    get = _served(tmp_path, monkeypatch, principal="@owner:home-site", scopes=("*",))
    assert _listed(get) == list(ALL)
    assert get("/api/v1/sites/partner-b/channels").status_code == 200
