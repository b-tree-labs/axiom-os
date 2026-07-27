# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the uniform AuthzHook adapter (RATIONALIZE-3).

The adapter turns the composed app's ``MiddlewareConfig.authz`` seam into a
real call into the governance ``decide`` engine. It is pure: the principal
resolver, the decide function, and the envelope builder are injected, so these
tests need no DB, no key store, and no authz extension.

Contract recap (middleware.py): an ``AuthzHook`` is
``Callable[[Request], AuthzDecision]`` and must NEVER raise — every failure
path returns ``AuthzDecision(allow=False, reason=...)`` (fail-closed).
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.governance import Decision, Verdict
from axiom.vega.identity.principal import Principal


def _fake_request(*, path="/v1/chat/completions", method="POST",
                  mount="gateway", headers=None):
    state = SimpleNamespace(mount_extension=mount)
    url = SimpleNamespace(path=path)
    return SimpleNamespace(state=state, method=method, url=url,
                           headers=headers or {})


def _principal(handle="@svc:org"):
    import hashlib
    return Principal(handle=handle,
                     public_bytes=hashlib.sha256(handle.encode()).digest())


def _verdict(decision, reason="because"):
    return Verdict.from_decision(decision, reason, "rcpt-1")


# --- the pure hook ---------------------------------------------------------


def test_permit_allows_and_attaches_principal():
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook

    seen = {}

    def decide_fn(env):
        seen["intent"] = env.intent.value
        seen["resource"] = str(env.resource)
        seen["actor"] = env.actor.handle
        return _verdict(Decision.PERMIT)

    hook = build_authz_hook(
        resolve_principal=lambda r: _principal("@svc:org"),
        decide_fn=decide_fn,
    )
    req = _fake_request()
    decision = hook(req)
    assert decision.allow is True
    # the resolved principal is attached for downstream handlers
    assert req.state.principal.handle == "@svc:org"
    # envelope was built from the request: http.<verb> + mount in the resource
    assert seen["intent"] == "http.invoke"
    assert "gateway" in seen["resource"]
    assert seen["actor"] == "@svc:org"


def test_deny_blocks_and_surfaces_reason():
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook

    hook = build_authz_hook(
        resolve_principal=lambda r: _principal(),
        decide_fn=lambda env: _verdict(Decision.DENY, "no rule matched"),
    )
    decision = hook(_fake_request())
    assert decision.allow is False
    assert "no rule matched" in decision.reason


def test_propose_to_human_is_not_proceed():
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook

    hook = build_authz_hook(
        resolve_principal=lambda r: _principal(),
        decide_fn=lambda env: _verdict(Decision.PROPOSE_TO_HUMAN, "novel"),
    )
    assert hook(_fake_request()).allow is False


def test_no_principal_fails_closed():
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook

    called = {"decide": False}

    def decide_fn(env):
        called["decide"] = True
        return _verdict(Decision.PERMIT)

    hook = build_authz_hook(
        resolve_principal=lambda r: None,  # unauthenticated
        decide_fn=decide_fn,
    )
    decision = hook(_fake_request(headers={}))
    assert decision.allow is False
    # never even consults decide when there is no principal
    assert called["decide"] is False


def test_decide_exception_fails_closed():
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook

    def boom(env):
        raise RuntimeError("authz DB down")

    hook = build_authz_hook(
        resolve_principal=lambda r: _principal(),
        decide_fn=boom,
    )
    decision = hook(_fake_request())
    assert decision.allow is False
    assert "authz" in decision.reason.lower() or "error" in decision.reason.lower()


def test_get_method_maps_to_read_verb():
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook

    seen = {}

    def decide_fn(env):
        seen["intent"] = env.intent.value
        return _verdict(Decision.PERMIT)

    hook = build_authz_hook(
        resolve_principal=lambda r: _principal(),
        decide_fn=decide_fn,
    )
    hook(_fake_request(method="GET", path="/v1/models"))
    assert seen["intent"] == "http.read"


# --- the bearer-token resolver --------------------------------------------


def test_bearer_resolver_maps_token_to_principal():
    from axiom.extensions.builtins.http.authz_hook import build_bearer_resolver

    resolve = build_bearer_resolver({"sek-abc": "@svc:org"})
    p = resolve(_fake_request(headers={"authorization": "Bearer sek-abc"}))
    assert p is not None and p.handle == "@svc:org"


def test_bearer_resolver_unknown_token_is_none():
    from axiom.extensions.builtins.http.authz_hook import build_bearer_resolver

    resolve = build_bearer_resolver({"sek-abc": "@svc:org"})
    assert resolve(_fake_request(headers={"authorization": "Bearer nope"})) is None
    assert resolve(_fake_request(headers={})) is None


def test_bearer_resolver_anonymous_fallback_for_dev():
    from axiom.extensions.builtins.http.authz_hook import build_bearer_resolver

    resolve = build_bearer_resolver({}, anonymous_handle="@dev:box")
    # no credential → dev anonymous principal (dev convenience only)
    p = resolve(_fake_request(headers={}))
    assert p is not None and p.handle == "@dev:box"


# --- default wiring + auto-wire detection ---------------------------------


def test_maybe_default_authz_hook_dev_mode_permits(monkeypatch):
    """In dev mode the default hook resolves a dev principal and permits."""
    from axiom.extensions.builtins.http import authz_hook as ah

    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.delenv("AXIOM_API_KEY", raising=False)
    monkeypatch.delenv("AXIOM_HTTP_API_KEYS", raising=False)

    hook = ah.maybe_default_authz_hook()
    assert hook is not None
    decision = hook(_fake_request(headers={}))
    assert decision.allow is True
