# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Dynamically-generated auth UI 'surfaces' (M6).

Given an AEOS ``[[extension.consumes]]`` credential declaration, produce a
**framework-agnostic surface descriptor** — what kind of auth UI this endpoint
needs (an OIDC sign-in button, a device-code card, an API-key form, …) and which
**mature OSS** should render it. Axiom does NOT reimplement login widgets; it
generates the descriptor and binds it to packaged OSS components per framework.

The descriptor is the contract the (shipped) framework renderers consume.
"""

from __future__ import annotations

from typing import Any

# Auth UI kinds, derived from the credential's mode + idp.
_MODE_TO_KIND = {
    "delegated": "oidc_signin",     # user signs in via the IdP (redirect or popup)
    "device_code": "device_code",   # show a code + verification URL
    "app_only": "service_account",  # no end-user UI; server-side creds
    "api_key": "api_key",           # a key/secret entry form
}

# Recommended OSS to render/drive each kind, by framework *or language* — we
# package mature OSS rather than rebuild. Polyglot: web frameworks + backend
# languages + native mobile.
_OSS = {
    "oidc_signin": {
        # JS / web
        "react": "MSAL React (Entra) / oidc-client-ts + react-oidc-context",
        "vue": "vue3-oidc / oidc-client-ts",
        "svelte": "oidc-client-ts",
        "angular": "angular-oauth2-oidc",
        "web": "oauth4webapi (vanilla JS)",
        "node": "openid-client (panva)",
        "nextjs": "Auth.js (NextAuth)",
        # backend languages
        "ruby": "omniauth + omniauth_openid_connect (Rails: + Devise)",
        "python": "Authlib (or requests-oauthlib)",
        "go": "coreos/go-oidc + golang.org/x/oauth2 (or markbates/goth)",
        "java": "Spring Security OAuth2/OIDC / Nimbus oauth2-oidc-sdk / pac4j",
        "dotnet": "AspNetCore OpenIdConnect / Duende IdentityModel / MSAL.NET",
        "php": "league/oauth2-client / jumbojett openid-connect-php (Laravel: Socialite)",
        "rust": "openidconnect crate (+ oauth2 crate)",
        "elixir": "ueberauth + openid_connect",
        # native mobile
        "ios": "AppAuth-iOS",
        "android": "AppAuth-Android",
        "flutter": "flutter_appauth",
        "react-native": "react-native-app-auth",
        "_default": "oidc-client-ts (JS) / AppAuth (native) — pick by stack",
    },
    "device_code": {"_default": "Axiom built-in device flow + a code-display card (any stack)"},
    "service_account": {"_default": "(none — provisioned server-side; operator setup, not a user UI)"},
    "api_key": {"_default": "a plain key-entry form (framework-native); store via KEEP"},
}


def _kind(decl: dict) -> str:
    if decl.get("kind") == "secret":
        return "api_key"
    return _MODE_TO_KIND.get(decl.get("mode", "delegated"), "oidc_signin")


def _fields(kind: str) -> list:
    return {
        "oidc_signin": [{"type": "button", "label": "Sign in"}],
        "device_code": [
            {"type": "text", "name": "user_code", "readonly": True},
            {"type": "link", "name": "verification_uri"},
        ],
        "api_key": [{"type": "password", "name": "api_key", "label": "API key", "required": True}],
        "service_account": [],
    }[kind]


def auth_surface(decl: dict, *, framework: str = "react") -> dict[str, Any]:
    """The surface descriptor for a credential/secret declaration."""
    kind = _kind(decl)
    oss_map = _OSS[kind]
    return {
        "kind": kind,
        "framework": framework,
        "title": f"Connect {decl.get('idp', decl.get('ref', 'account'))}",
        "idp": decl.get("idp"),
        "scopes": list(decl.get("scopes", [])),
        "min_posture": decl.get("min_posture", "open"),
        "fields": _fields(kind),
        "render_with": oss_map.get(framework, oss_map["_default"]),
    }


__all__ = ["auth_surface"]
