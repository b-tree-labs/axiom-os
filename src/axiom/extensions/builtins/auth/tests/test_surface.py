# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""M6: auth-surface descriptor — generated from a consumes declaration, bound to
the right OSS renderer per framework."""

from __future__ import annotations

from axiom.extensions.builtins.auth.surface import auth_surface


def test_delegated_oidc_surface_for_react_recommends_msal_or_oidc_client():
    s = auth_surface({"kind": "credential", "idp": "entra", "mode": "delegated",
                      "scopes": ["openid"], "min_posture": "sso"}, framework="react")
    assert s["kind"] == "oidc_signin"
    assert s["fields"][0]["type"] == "button"
    assert "MSAL" in s["render_with"] or "oidc-client" in s["render_with"]
    assert s["min_posture"] == "sso"


def test_device_code_surface_shows_code_and_uri():
    s = auth_surface({"kind": "credential", "idp": "entra", "mode": "device_code"})
    assert s["kind"] == "device_code"
    names = {f.get("name") for f in s["fields"]}
    assert {"user_code", "verification_uri"} <= names


def test_secret_becomes_an_api_key_form():
    s = auth_surface({"kind": "secret", "ref": "openbao://kv/x"})
    assert s["kind"] == "api_key"
    assert s["fields"][0]["type"] == "password"


def test_app_only_has_no_end_user_surface():
    s = auth_surface({"kind": "credential", "idp": "entra", "mode": "app_only"})
    assert s["kind"] == "service_account" and s["fields"] == []


def test_framework_falls_back_to_default_renderer():
    s = auth_surface({"kind": "credential", "idp": "google", "mode": "delegated"}, framework="qt")
    assert s["render_with"]   # unknown framework -> the _default OSS renderer


def test_polyglot_oss_recommendations():
    decl = {"kind": "credential", "idp": "entra", "mode": "delegated"}
    assert "omniauth" in auth_surface(decl, framework="ruby")["render_with"]
    assert "Authlib" in auth_surface(decl, framework="python")["render_with"]
    assert "go-oidc" in auth_surface(decl, framework="go")["render_with"]
    assert "AppAuth" in auth_surface(decl, framework="ios")["render_with"]
    assert "league/oauth2-client" in auth_surface(decl, framework="php")["render_with"]
