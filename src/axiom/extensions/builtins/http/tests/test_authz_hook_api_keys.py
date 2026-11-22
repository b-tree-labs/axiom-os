# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AuthzHook × issued API keys — resolution + fail-closed scope enforcement.

Issued keys (``axk_…``, minted by ``gate.issue``) resolve to their service
principal and carry scopes. The hook enforces the scopes deterministically
before consulting ``decide`` (deny when no scope covers the request) and
narrows the envelope's capability to the granted scope so GUARD's capability
floor sees least privilege. The legacy env-registry path (``AXIOM_API_KEY`` /
``AXIOM_HTTP_API_KEYS``) is untouched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.governance import Decision, Verdict
from axiom.webauth.api_keys import (
    JsonFileApiKeyStore,
    append_api_key_record,
    mint_api_key,
)


def _fake_request(*, path="/v1/chat/completions", method="POST",
                  mount="gateway", headers=None):
    state = SimpleNamespace(mount_extension=mount)
    url = SimpleNamespace(path=path)
    return SimpleNamespace(state=state, method=method, url=url,
                           headers=headers or {})


def _verdict(decision, reason="because"):
    return Verdict.from_decision(decision, reason, "rcpt-1")


def _issue(tmp_path: Path, *, principal="@svc:org", scopes=("gateway",)):
    f = tmp_path / "api-keys.json"
    token, record = mint_api_key(principal=principal, scopes=scopes)
    append_api_key_record(f, record)
    return token, JsonFileApiKeyStore(f)


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


# ---------- scope grammar ---------------------------------------------------


def test_parse_scope_shapes():
    from axiom.extensions.builtins.http.authz_hook import parse_scope

    ip, rp = parse_scope("gateway")
    assert ip.value == "http.*"
    assert rp.value == "extension://gateway/*"

    ip, rp = parse_scope("rag:read")
    assert ip.value == "http.read"
    assert rp.value == "extension://rag/*"

    ip, rp = parse_scope("*")
    assert ip.value == "http.*"
    assert rp.value == "extension://*"


@pytest.mark.parametrize("bad", ["", "  ", "mount:destroy", "mou nt", "a:b:c",
                                 "extension://x"])
def test_parse_scope_rejects_malformed(bad):
    from axiom.extensions.builtins.http.authz_hook import parse_scope

    with pytest.raises(ValueError):
        parse_scope(bad)


# ---------- resolver: issued keys → principal + scopes ----------------------


def test_api_key_resolves_to_service_principal(tmp_path: Path):
    from axiom.extensions.builtins.http.authz_hook import (
        ResolvedCredential,
        build_bearer_resolver,
    )

    token, store = _issue(tmp_path, principal="@svc:org", scopes=("gateway",))
    resolve = build_bearer_resolver({}, api_keys=store)
    cred = resolve(_fake_request(headers=_bearer(token)))
    assert isinstance(cred, ResolvedCredential)
    assert cred.principal.handle == "@svc:org"
    assert cred.scopes == ("gateway",)


def test_api_key_prefix_never_falls_back_to_env_registry(tmp_path: Path):
    from axiom.extensions.builtins.http.authz_hook import build_bearer_resolver

    token, store = _issue(tmp_path)
    # even if an identical token somehow lands in the env registry, the
    # axk_ prefix routes exclusively through the issued-key store
    resolve = build_bearer_resolver({token: "@spoof:env"}, api_keys=None)
    assert resolve(_fake_request(headers=_bearer(token))) is None


def test_legacy_env_registry_path_untouched(tmp_path: Path):
    from axiom.extensions.builtins.http.authz_hook import build_bearer_resolver

    _token, store = _issue(tmp_path)
    resolve = build_bearer_resolver({"legacy-secret": "@api:local"},
                                    api_keys=store)
    p = resolve(_fake_request(headers=_bearer("legacy-secret")))
    # plain Principal, no scopes — exactly the pre-existing behavior
    assert p is not None and p.handle == "@api:local"
    assert not hasattr(p, "scopes")


def test_revoked_api_key_denies(tmp_path: Path):
    from axiom.extensions.builtins.http.authz_hook import build_bearer_resolver
    from axiom.webauth.api_keys import revoke_api_key_record

    f = tmp_path / "api-keys.json"
    token, record = mint_api_key(principal="@svc:org", scopes=("gateway",))
    append_api_key_record(f, record)
    store = JsonFileApiKeyStore(f)
    resolve = build_bearer_resolver({}, api_keys=store)
    assert resolve(_fake_request(headers=_bearer(token))) is not None
    revoke_api_key_record(f, record["key_id"])
    store.reload()
    assert resolve(_fake_request(headers=_bearer(token))) is None


# ---------- hook: scope enforcement (fail-closed) ---------------------------


def _hook_with(store, decide_fn):
    from axiom.extensions.builtins.http.authz_hook import (
        build_authz_hook,
        build_bearer_resolver,
    )

    return build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=store),
        decide_fn=decide_fn,
    )


def test_in_scope_request_reaches_decide_with_scoped_capability(tmp_path):
    token, store = _issue(tmp_path, scopes=("gateway:invoke",))
    seen = {}

    def decide_fn(env):
        seen["actor"] = env.actor.handle
        seen["cap_intent"] = env.capability.intent_pattern.value
        seen["cap_resource"] = env.capability.resource_pattern.value
        # the capability floor must actually cover the request
        assert env.capability.permits_intent(env.intent)
        assert env.capability.permits_resource(env.resource)
        return _verdict(Decision.PERMIT)

    hook = _hook_with(store, decide_fn)
    req = _fake_request(mount="gateway", method="POST",
                        headers=_bearer(token))
    decision = hook(req)
    assert decision.allow is True
    assert seen["actor"] == "@svc:org"
    assert seen["cap_intent"] == "http.invoke"
    assert seen["cap_resource"] == "extension://gateway/*"
    assert req.state.principal.handle == "@svc:org"


def test_out_of_scope_mount_denies_without_decide(tmp_path):
    token, store = _issue(tmp_path, scopes=("gateway",))
    called = {"decide": False}

    def decide_fn(env):
        called["decide"] = True
        return _verdict(Decision.PERMIT)

    hook = _hook_with(store, decide_fn)
    decision = hook(_fake_request(mount="rag", headers=_bearer(token)))
    assert decision.allow is False
    assert "scope" in decision.reason.lower()
    assert called["decide"] is False  # fail-closed before the engine


def test_out_of_scope_verb_denies(tmp_path):
    token, store = _issue(tmp_path, scopes=("gateway:read",))
    hook = _hook_with(store, lambda env: _verdict(Decision.PERMIT))
    # POST maps to http.invoke — a read-only key must not invoke
    assert hook(_fake_request(mount="gateway", method="POST",
                              headers=_bearer(token))).allow is False
    # …but GET (http.read) passes the scope gate and reaches decide
    assert hook(_fake_request(mount="gateway", method="GET",
                              headers=_bearer(token))).allow is True


def test_malformed_stored_scope_grants_nothing(tmp_path):
    # a record with an unparseable scope must fail closed, not open
    token, store = _issue(tmp_path, scopes=("not a scope",))
    hook = _hook_with(store, lambda env: _verdict(Decision.PERMIT))
    assert hook(_fake_request(mount="gateway", headers=_bearer(token))).allow is False


def test_wildcard_scope_covers_all_mounts(tmp_path):
    token, store = _issue(tmp_path, scopes=("*",))
    hook = _hook_with(store, lambda env: _verdict(Decision.PERMIT))
    assert hook(_fake_request(mount="gateway", headers=_bearer(token))).allow is True
    assert hook(_fake_request(mount="rag", headers=_bearer(token))).allow is True


def test_engine_deny_still_denies_in_scope_request(tmp_path):
    # scopes gate access; they never override a GUARD deny
    token, store = _issue(tmp_path, scopes=("gateway",))
    hook = _hook_with(store, lambda env: _verdict(Decision.DENY, "rule says no"))
    decision = hook(_fake_request(mount="gateway", headers=_bearer(token)))
    assert decision.allow is False
    assert "rule says no" in decision.reason


# ---------- default wiring ---------------------------------------------------


def test_maybe_default_hook_wires_issued_keys_in_dev(tmp_path, monkeypatch):
    from axiom.extensions.builtins.http import authz_hook as ah

    f = tmp_path / "api-keys.json"
    token, record = mint_api_key(principal="@svc:org", scopes=("gateway",))
    append_api_key_record(f, record)

    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.delenv("AXIOM_API_KEY", raising=False)
    monkeypatch.delenv("AXIOM_HTTP_API_KEYS", raising=False)
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(f))

    hook = ah.maybe_default_authz_hook()
    assert hook is not None
    req = _fake_request(mount="gateway", headers=_bearer(token))
    assert hook(req).allow is True
    assert req.state.principal.handle == "@svc:org"
    # scope enforcement holds even in dev mode
    assert hook(_fake_request(mount="rag", headers=_bearer(token))).allow is False


def test_maybe_default_hook_keys_file_alone_suffices(monkeypatch, tmp_path):
    """Post-migration: legacy key unset, only the issued-keys file remains.

    The hook must still wire (not fall back to refusing all auth-required
    mounts) — otherwise retiring the legacy key would take the node down.
    Non-dev without an authz DB still returns None further down; dev mode
    exercises the registry-presence condition deterministically.
    """
    from axiom.extensions.builtins.http import authz_hook as ah

    f = tmp_path / "api-keys.json"
    token, record = mint_api_key(principal="@svc:org", scopes=("gateway",))
    append_api_key_record(f, record)

    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.delenv("AXIOM_API_KEY", raising=False)
    monkeypatch.delenv("AXIOM_HTTP_API_KEYS", raising=False)
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(f))

    hook = ah.maybe_default_authz_hook()
    assert hook is not None
    assert hook(_fake_request(mount="gateway", headers=_bearer(token))).allow
