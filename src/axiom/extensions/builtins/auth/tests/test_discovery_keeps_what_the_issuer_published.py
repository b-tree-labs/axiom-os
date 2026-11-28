# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Discovery must keep every field it has somewhere to put.

`from_discovery` read an issuer's `.well-known/openid-configuration` and built
an `IdpConfig` from five of its keys, dropping `device_authorization_endpoint`
— a field `IdpConfig` has, and the only one the device flow needs.

The consequence is worse than a missing field. `start_device_flow` raises
"<name> has no device authorization endpoint", which is false: the issuer
published one and the loader threw it away. So the message accuses the identity
provider, and whoever reads it goes to look at the wrong system. Every IdP
reached by discovery rather than by one of the two hand-written presets was
silently without a device flow, which is to say every issuer except Entra and
Google — and the device flow is how somebody with no browser session gets a
credential.

The second half is the registry. `register_idp` exists so a new provider is
added "without editing a single caller", and the registry shipped exactly two
entries, neither of them the generic discovery path it already had code for.
So the open registry was closed in practice: any other issuer needed a caller
to import `from_discovery` directly, which is the dispatch the registry
replaced.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.auth import providers
from axiom.extensions.builtins.auth.device_flow import DeviceFlowError, start_device_flow

ISSUER = "https://id.example.edu"


class _Http:
    """An issuer that publishes a full discovery document."""

    def __init__(self, doc: dict | None = None):
        self.doc = (
            doc
            if doc is not None
            else {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
                "device_authorization_endpoint": f"{ISSUER}/device",
            }
        )
        self.asked: list[str] = []

    def get(self, url: str) -> dict:
        self.asked.append(url)
        return self.doc

    def post(self, url: str, _body: dict) -> dict:
        self.asked.append(url)
        return {"device_code": "d", "user_code": "ABCD-EFGH", "interval": 5}


def test_discovery_keeps_the_device_endpoint_the_issuer_published():
    cfg = providers.from_discovery(_Http(), ISSUER)
    assert cfg.device_authorization_endpoint == f"{ISSUER}/device"


def test_an_issuer_reached_by_discovery_can_run_a_device_flow():
    """The point of the field, exercised through the caller that needs it.

    Asserting the field alone would pass against a config nothing can use.
    """
    http = _Http()
    cfg = providers.from_discovery(http, ISSUER)
    started = start_device_flow(http, cfg, client_id="c", scopes=["openid"])
    assert started["user_code"] == "ABCD-EFGH"
    assert f"{ISSUER}/device" in http.asked


def test_an_issuer_with_no_device_flow_is_refused_truthfully():
    """Absence has kinds. An issuer that publishes no device endpoint genuinely
    has none, and that refusal is correct — it is the same message for a
    dropped field that was the defect."""
    http = _Http(
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
        }
    )
    cfg = providers.from_discovery(http, ISSUER)
    assert cfg.device_authorization_endpoint is None
    with pytest.raises(DeviceFlowError):
        start_device_flow(http, cfg, client_id="c", scopes=["openid"])


def test_discovery_keeps_nothing_it_has_nowhere_to_put():
    """A guard against the opposite overcorrection: the loader copies the
    fields `IdpConfig` declares and does not invent attributes from the
    document, so an issuer cannot shape our config."""
    http = _Http(
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "some_vendor_extension": "surprise",
        }
    )
    cfg = providers.from_discovery(http, ISSUER)
    assert not hasattr(cfg, "some_vendor_extension")


def test_every_field_idpconfig_declares_is_read_from_discovery():
    """The guard that outlives this fix.

    The defect was not a typo, it was a hand-written copy of five keys out of
    a document that had six. Adding a field to `IdpConfig` and forgetting the
    loader would reproduce it exactly, so this fails on the next one rather
    than on this one.
    """
    from dataclasses import fields

    doc = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "device_authorization_endpoint": f"{ISSUER}/device",
    }
    cfg = providers.from_discovery(_Http(doc), ISSUER)
    # `name` and `default_scopes` are ours, not the issuer's.
    ours = {"name", "default_scopes"}
    for f in fields(providers.IdpConfig):
        if f.name in ours:
            continue
        assert f.name in doc, (
            f"IdpConfig declares {f.name!r} and this test's document does not "
            f"publish it — add it to the document and to from_discovery, or the "
            f"loader will silently drop it the way it dropped the device endpoint"
        )
        assert getattr(cfg, f.name) == doc[f.name], f"from_discovery dropped {f.name!r}"


# ---------------------------------------------------------------------------
# The registry half: open in name, closed in practice.
# ---------------------------------------------------------------------------


def test_the_registry_offers_the_generic_issuer_it_already_had_code_for():
    assert "oidc" in providers.available_idps()


def test_a_generic_issuer_is_selected_by_name_like_every_other():
    http = _Http()
    cfg = providers.get_idp("oidc", issuer=ISSUER, http=http)
    assert cfg.issuer == ISSUER
    assert cfg.device_authorization_endpoint == f"{ISSUER}/device"


def test_the_generic_builder_says_what_it_needs():
    """A builder that needs two keys and gets one must say which, the way the
    entra builder does. An unknown-shaped TypeError from inside a factory is
    how somebody concludes the registry does not support their issuer."""
    with pytest.raises(ValueError) as refused:
        providers.get_idp("oidc", http=_Http())
    assert "issuer" in str(refused.value)

    with pytest.raises(ValueError) as refused:
        providers.get_idp("oidc", issuer=ISSUER)
    assert "http" in str(refused.value)


def test_an_unknown_idp_still_names_the_ones_that_exist():
    with pytest.raises(ValueError) as refused:
        providers.get_idp("okta")
    said = str(refused.value)
    assert "okta" in said
    assert "oidc" in said and "entra" in said


def test_registering_an_idp_still_works_and_is_still_how_okta_arrives():
    """The extension seam, unchanged. Guarded here because this change adds a
    built-in entry and a built-in entry is the easy way to start treating the
    map as closed again."""
    providers.register_idp("fake-okta", lambda **_c: providers.google())
    try:
        assert providers.get_idp("fake-okta").name == "google"
    finally:
        providers._IDP_REGISTRY.pop("fake-okta", None)


# ---------------------------------------------------------------------------
# The CLI. Adding a registry entry adds a `--provider` choice, so the command
# now advertises a provider it must actually be able to serve.
# ---------------------------------------------------------------------------


def test_the_cli_documents_the_flag_the_generic_provider_needs(capsys):
    """A flag nobody can discover is a flag nobody passes."""
    from axiom.extensions.builtins.auth.cli import main

    with pytest.raises(SystemExit):
        main(["login", "--help"])
    helped = capsys.readouterr().out
    assert "--issuer" in helped
    assert "oidc" in helped


def test_choosing_the_generic_provider_without_an_issuer_says_which_flag(capsys):
    """A declared surface must exist. `--provider oidc` appears in the choices
    the moment the registry has the entry, so selecting it has to either work
    or say exactly what is missing."""
    from axiom.extensions.builtins.auth.cli import main

    with pytest.raises(SystemExit) as stopped:
        main(["login", "--provider", "oidc", "--client-id", "c"])
    said = str(stopped.value)
    assert "issuer" in said
    assert "--issuer" in said, "the refusal has to name the flag, not the concept"


def test_the_listing_and_the_factory_agree_about_what_each_provider_needs(capsys):
    """Two answers to one question is how `(needs --tenant)` ends up beside a
    factory that wants something else."""
    from axiom.extensions.builtins.auth.cli import main

    main(["providers"])
    out = capsys.readouterr().out
    assert "entra (needs --tenant)" in out
    assert "oidc (needs --issuer)" in out
    assert "google" in out and "google (needs" not in out


def test_the_cli_http_client_can_fetch_a_discovery_document():
    """The generic provider reads a document over HTTP, so the client the CLI
    hands the factory needs a `get`. It had only `post`."""
    from axiom.extensions.builtins.auth.cli import _RequestsHttp

    assert hasattr(_RequestsHttp(), "get")
    assert hasattr(_RequestsHttp(), "post")
