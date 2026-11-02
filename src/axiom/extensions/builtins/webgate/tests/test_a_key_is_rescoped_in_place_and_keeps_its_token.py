# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A key moves to a narrower rule without its holder doing anything.

A key that carries no site used to read every site on its node. It now reads no
site's data, and is told why in one plain sentence. So that no holder of such a
key meets that refusal, the serving app ties every no-site key to the node's home
site when it starts, in place: the record changes and the token does not. The
same move is one administrator verb, ``axi gate rescope api-key``.

What holds here:

- the same token authenticates before and after a rescope, and is then held to
  its new site; its id, principal and hash are kept byte for byte;
- the startup migration is idempotent and leaves revoked and sited keys alone;
- through the served path, a no-site key lists nothing and is told why, and after
  the migration the same token reads its node's site and nothing else;
- when each key last authenticated is recorded, at most once a minute per key,
  beside the keys file and never in it.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

from axiom.extensions.builtins.http.authz_hook import migrate_unsited_keys_to_home_site
from axiom.extensions.builtins.webgate import skills as gate_skills
from axiom.infra import site_authority as sa
from axiom.infra import site_scope
from axiom.infra.skills import SkillContext, SkillRegistry
from axiom.webauth.api_keys import (
    ApiKeysFileError,
    JsonFileApiKeyStore,
    LastUsedRecorder,
    append_api_key_record,
    assign_site_to_unsited_keys,
    last_used_path,
    load_api_key_records,
    load_last_used,
    mint_api_key,
    rescope_api_key_record,
    revoke_api_key_record,
)

HOME, PARTNER = "home-site", "partner-a"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_SITE", HOME)
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    monkeypatch.delenv(site_scope.SERVED_SITES_ENV, raising=False)
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))
    sa._reset_default_site_context()
    yield
    sa._reset_default_site_context()


def _key(path: Path, *, principal="@person", scopes=("*:read",), site=None):
    token, record = mint_api_key(principal=principal, scopes=scopes, site=site)
    append_api_key_record(path, record)
    return token, record["key_id"]


# --- rescope: the record changes, the token does not ------------------------------


def test_the_same_token_authenticates_before_and_after_and_is_then_held(tmp_path):
    keys = tmp_path / "keys.json"
    token, key_id = _key(keys)
    store = JsonFileApiKeyStore(keys)
    assert store.resolve(token).site is None
    original = load_api_key_records(keys)[0]

    before, after = rescope_api_key_record(keys, key_id, site=HOME)

    assert before["site"] is None and after["site"] == HOME
    stored = load_api_key_records(keys)[0]
    for field in ("key_id", "principal", "secret_hash", "created_at", "name"):
        assert stored[field] == original[field]
    identity = store.resolve(token)
    assert identity is not None and identity.site == HOME
    assert identity.key_id == key_id and identity.principal == original["principal"]


def test_scopes_change_in_place_and_the_last_one_cannot_go(tmp_path):
    keys = tmp_path / "keys.json"
    token, key_id = _key(keys, scopes=("*:read", "chat:invoke"))
    _, after = rescope_api_key_record(keys, key_id, add_scopes=["rag:read"],
                                      remove_scopes=["chat:invoke"])
    assert after["scopes"] == ["*:read", "rag:read"]
    assert JsonFileApiKeyStore(keys).resolve(token).scopes == ("*:read", "rag:read")
    with pytest.raises(ApiKeysFileError, match="non-empty"):
        rescope_api_key_record(keys, key_id, remove_scopes=["*:read", "rag:read"])


def test_a_sited_key_cannot_be_moved_to_another_site(tmp_path):
    keys = tmp_path / "keys.json"
    _, key_id = _key(keys, principal="@viewer", site=PARTNER)
    with pytest.raises(ApiKeysFileError, match="cannot be moved"):
        rescope_api_key_record(keys, key_id, site=HOME)
    _, same = rescope_api_key_record(keys, key_id, site=PARTNER)  # a no-op, allowed
    assert same["site"] == PARTNER


def test_a_revoked_or_unknown_key_is_refused(tmp_path):
    keys = tmp_path / "keys.json"
    _, key_id = _key(keys)
    revoke_api_key_record(keys, key_id)
    with pytest.raises(ApiKeysFileError, match="revoked"):
        rescope_api_key_record(keys, key_id, site=HOME)
    with pytest.raises(ApiKeysFileError, match="no such key"):
        rescope_api_key_record(keys, "nope", site=HOME)


def test_an_unchanged_rescope_writes_nothing(tmp_path):
    keys = tmp_path / "keys.json"
    _, key_id = _key(keys, site=HOME)
    stamp = keys.stat().st_mtime_ns
    _, after = rescope_api_key_record(keys, key_id, site=HOME)
    assert after["site"] == HOME and keys.stat().st_mtime_ns == stamp


# --- the verb ---------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    reg = SkillRegistry()
    gate_skills.bind(reg)
    return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t.gate"))


def test_the_rescope_verb_reports_before_and_after(ctx, tmp_path):
    keys = tmp_path / "keys.json"
    token, key_id = _key(keys)
    r = ctx.registry.invoke("gate.rescope", {"resource": "api-key", "key_id": key_id,
                                             "site": HOME, "keys_file": str(keys)}, ctx)
    assert r.ok, r.errors
    assert r.value["changed"] is True
    assert r.value["before"]["site"] is None and r.value["after"]["site"] == HOME
    assert "same token" in r.actions_taken[0]
    assert JsonFileApiKeyStore(keys).resolve(token).site == HOME
    again = ctx.registry.invoke("gate.rescope", {"resource": "api-key", "key_id": key_id,
                                                 "site": HOME, "keys_file": str(keys)}, ctx)
    assert again.ok and again.value["changed"] is False


def test_the_rescope_verb_needs_something_to_change(ctx, tmp_path):
    keys = tmp_path / "keys.json"
    _, key_id = _key(keys)
    r = ctx.registry.invoke("gate.rescope", {"key_id": key_id, "keys_file": str(keys)}, ctx)
    assert not r.ok and "nothing to change" in r.errors[0]


# --- the startup migration --------------------------------------------------------


def test_the_migration_ties_only_active_unsited_keys_and_is_idempotent(tmp_path):
    keys = tmp_path / "keys.json"
    t1, unsited = _key(keys)
    _, revoked = _key(keys)
    revoke_api_key_record(keys, revoked)
    _, partner = _key(keys, principal="@viewer", site=PARTNER)

    changed = assign_site_to_unsited_keys(keys, HOME)

    assert [c["key_id"] for c in changed] == [unsited]
    by_id = {r["key_id"]: r for r in load_api_key_records(keys)}
    assert by_id[unsited]["site"] == HOME
    assert by_id[revoked]["site"] is None
    assert by_id[partner]["site"] == PARTNER
    assert assign_site_to_unsited_keys(keys, HOME) == []
    assert JsonFileApiKeyStore(keys).resolve(t1).site == HOME


def test_the_serving_startup_uses_the_nodes_home_site(tmp_path, monkeypatch):
    keys = tmp_path / "keys.json"
    _, key_id = _key(keys)
    monkeypatch.delenv("AXIOM_SITE")
    assert migrate_unsited_keys_to_home_site(keys) == []  # no home site: nothing to do
    monkeypatch.setenv("AXIOM_SITE", HOME)
    assert [c["key_id"] for c in migrate_unsited_keys_to_home_site(keys)] == [key_id]
    assert migrate_unsited_keys_to_home_site(keys) == []


def test_the_startup_migration_never_raises(tmp_path):
    bad = tmp_path / "keys.json"
    bad.write_text("not json")
    assert migrate_unsited_keys_to_home_site(bad) == []


# --- what a no-site key is told ---------------------------------------------------


def test_a_no_site_key_reads_nothing_and_is_told_why():
    tok = sa.bind_caller_site(None, unsited_key=True)
    try:
        with pytest.raises(sa.SiteNotAuthorized) as got:
            sa.tenant_scope()
        assert "not tied to a site" in str(got.value)
        assert "same key keeps working" in str(got.value)
        with pytest.raises(sa.SiteNotAuthorized):
            sa.authorize_site(HOME, principal="@person", bound=HOME)
        assert site_scope.resolve().filter([HOME, PARTNER]) == []
    finally:
        sa.unbind_caller_site(tok)


def test_no_credential_at_all_is_unchanged():
    tok = sa.bind_caller_site(None)
    try:
        assert sa.tenant_scope() is None
        assert sa.authorize_site(PARTNER, principal="@op", bound=HOME) == PARTNER
    finally:
        sa.unbind_caller_site(tok)


def test_a_partner_refusal_says_what_holds_and_why():
    tok = sa.bind_caller_site(HOME)
    try:
        with pytest.raises(sa.SiteNotAuthorized) as got:
            sa.authorize_site(PARTNER, principal="@viewer:home-site")
        assert "reads only the site it was issued for" in str(got.value)
    finally:
        sa.unbind_caller_site(tok)


# --- through the served path ------------------------------------------------------


def _served(tmp_path, monkeypatch, keys: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
    from axiom.extensions.builtins.http.middleware import MiddlewareConfig, install_middleware
    from axiom.extensions.builtins.webapp import bound_web
    from axiom.extensions.builtins.webapp.api.catalog import register_catalog_routes
    from axiom.extensions.builtins.webapp.catalog import store
    from axiom.governance import Decision, Verdict

    @contextmanager
    def _session():
        yield None

    monkeypatch.setattr(bound_web, "forwarding", lambda: False)
    monkeypatch.setattr(store, "session_scope", _session)
    monkeypatch.setattr(store, "read_site_summaries",
                        lambda s: [{"site": n, "rows": 1} for n in (HOME, PARTNER)])
    monkeypatch.setattr(store, "read_channels", lambda s, site: [{"channel": f"{site}:x"}])
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys)),
        decide_fn=lambda env: Verdict.from_decision(Decision.PERMIT, "ok", "r"),
    )
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    register_catalog_routes(router)
    app.include_router(router)
    install_middleware(app, MiddlewareConfig(authz=hook))
    return TestClient(app)


def _message(resp) -> str:
    return json.dumps(resp.json())


def test_a_no_site_key_is_moved_to_its_nodes_site_with_the_same_token(tmp_path, monkeypatch):
    keys = tmp_path / "keys.json"
    token, _ = _key(keys)
    client = _served(tmp_path, monkeypatch, keys)
    auth = {"authorization": f"Bearer {token}"}

    before = client.get(f"/api/v1/sites/{HOME}/channels", headers=auth)
    assert before.status_code == 404 and "not tied to a site" in _message(before)
    assert client.get("/api/v1/sites", headers=auth).json()["sites"] == []

    migrate_unsited_keys_to_home_site(keys)  # what the serving app does at startup

    assert [r["site"] for r in client.get("/api/v1/sites", headers=auth).json()["sites"]] == [HOME]
    assert client.get(f"/api/v1/sites/{HOME}/channels", headers=auth).status_code == 200
    partner = client.get(f"/api/v1/sites/{PARTNER}/channels", headers=auth)
    assert partner.status_code == 404
    assert "reads only the site it was issued for" in _message(partner)


# --- when each key was last used --------------------------------------------------


def test_a_use_is_recorded_beside_the_keys_file_once_a_minute(tmp_path):
    keys = tmp_path / "keys.json"
    token, key_id = _key(keys, site=HOME)
    keys_bytes = keys.read_bytes()
    now = [1000.0]
    store = JsonFileApiKeyStore(keys, clock=lambda: now[0])

    assert store.resolve(token) is not None
    first = load_last_used(keys)[key_id]
    assert last_used_path(keys).is_file() and last_used_path(keys) != keys
    assert keys.read_bytes() == keys_bytes  # never written to the keys file

    stamp = last_used_path(keys).stat().st_mtime_ns
    now[0] += 30
    assert store.resolve(token) is not None
    assert last_used_path(keys).stat().st_mtime_ns == stamp  # throttled
    now[0] += 31
    assert store.resolve(token) is not None
    assert load_last_used(keys)[key_id] >= first


def test_a_wrong_secret_is_not_a_use(tmp_path):
    keys = tmp_path / "keys.json"
    token, key_id = _key(keys, site=HOME)
    assert JsonFileApiKeyStore(keys).resolve(token[:-2] + "xx") is None
    assert key_id not in load_last_used(keys)


def test_a_recorder_that_cannot_write_never_refuses(tmp_path):
    recorder = LastUsedRecorder(tmp_path / "missing-dir" / "keys.json")
    assert recorder.record("abc") is False


def test_the_listing_shows_when_each_key_was_last_used(ctx, tmp_path):
    keys = tmp_path / "keys.json"
    token, used = _key(keys, site=HOME)
    _, idle = _key(keys, site=HOME)
    JsonFileApiKeyStore(keys).resolve(token)
    r = ctx.registry.invoke("gate.list", {"resource": "api-keys", "keys_file": str(keys)}, ctx)
    assert r.ok, r.errors
    by_id = {i["key_id"]: i for i in r.value["items"]}
    assert by_id[used]["last_used_at"] and by_id[idle]["last_used_at"] is None
