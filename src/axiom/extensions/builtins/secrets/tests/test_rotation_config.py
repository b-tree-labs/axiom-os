# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``RotationConfig`` + ``build_vendor_strategy`` — the admin-client-from-cred-ref
wiring (#16 / ADR-095).

This is the plumbing ``secrets.rotate`` was missing: a vendor strategy's admin
credential lives in the SecretStore, not inlined. The factory resolves it
through the store seam, builds the HTTP client, and constructs the strategy.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets.providers.protocol import Secret
from axiom.extensions.builtins.secrets.rotation import (
    RotationConfig,
    build_vendor_strategy,
)
from axiom.extensions.builtins.secrets.rotation.config import _default_base_url
from axiom.extensions.builtins.secrets.rotation.strategies import (
    RotationError,
    SendGridRotation,
)


class FakeStore:
    """Resolves admin creds by ref path; records staged writes."""

    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self._values = values or {}
        self.puts: list[tuple[str, bytes]] = []

    def get(self, ref) -> Secret:
        return Secret(value=self._values[ref.path])

    def put(self, ref, value) -> None:
        self.puts.append((ref.path, value))


def _store_for(store):
    return lambda scheme: store


class TestBuildVendorStrategy:
    def test_builds_sendgrid_from_admin_ref(self):
        store = FakeStore({"kv/sendgrid-admin": b"SG.ADMIN"})
        cfg = RotationConfig(
            kind="sendgrid",
            admin_ref="openbao://kv/sendgrid-admin",
            managed_name="axiom-managed:sendgrid",
        )
        strat = build_vendor_strategy(cfg, store_for=_store_for(store))
        assert isinstance(strat, SendGridRotation)
        assert strat.kind == "sendgrid"

    def test_client_carries_the_resolved_admin_token(self):
        store = FakeStore({"kv/sg": b"SG.ADMIN"})
        cfg = RotationConfig(kind="sendgrid", admin_ref="openbao://kv/sg")
        strat = build_vendor_strategy(cfg, store_for=_store_for(store))
        # the admin token was resolved from the store and armed on the client
        assert strat._http._base == "https://api.sendgrid.com"
        assert strat._http._auth == "Bearer SG.ADMIN"

    def test_explicit_base_url_overrides_default(self):
        store = FakeStore({"kv/sg": b"t"})
        cfg = RotationConfig(
            kind="sendgrid", admin_ref="openbao://kv/sg", base_url="https://eu.sg.test"
        )
        strat = build_vendor_strategy(cfg, store_for=_store_for(store))
        assert strat._http._base == "https://eu.sg.test"

    def test_empty_admin_credential_raises(self):
        store = FakeStore({"kv/sg": b""})
        cfg = RotationConfig(kind="sendgrid", admin_ref="openbao://kv/sg")
        with pytest.raises(RotationError):
            build_vendor_strategy(cfg, store_for=_store_for(store))

    def test_unknown_kind_raises_keyerror(self):
        store = FakeStore({"kv/x": b"t"})
        cfg = RotationConfig(kind="bogus", admin_ref="openbao://kv/x")
        with pytest.raises(KeyError):
            build_vendor_strategy(cfg, store_for=_store_for(store))


class TestDefaults:
    def test_sendgrid_default_base_url(self):
        assert _default_base_url("sendgrid") == "https://api.sendgrid.com"

    def test_unknown_kind_has_no_default(self):
        with pytest.raises(KeyError):
            _default_base_url("bogus")


class TestBuiltStrategyRotatesEndToEnd:
    """The built strategy actually mints + stages through a fake opener —
    proving the wiring is live, not just constructed."""

    def test_perform_mints_via_client_and_stages_to_store(self):
        store = FakeStore({"kv/sg-admin": b"SG.ADMIN"})
        cfg = RotationConfig(
            kind="sendgrid",
            admin_ref="openbao://kv/sg-admin",
            managed_name="axiom-managed:sendgrid",
        )
        strat = build_vendor_strategy(cfg, store_for=_store_for(store))

        # swap the client's opener for a canned SendGrid mint response
        minted: list = []

        class Resp:
            def __init__(self, b):
                self._b = b

            def read(self):
                return self._b

        def opener(req, timeout=None):
            minted.append(req)
            return Resp(b'{"api_key_id":"KID9","api_key":"SG.new-secret"}')

        strat._http._opener = opener

        from axiom.extensions.builtins.secrets.providers.protocol import SecretRef
        from axiom.extensions.builtins.secrets.rotation import RotationPolicy

        ref = SecretRef.parse("openbao://kv/sendgrid-api-key")
        out = strat.perform(ref, store, now=1000.0, policy=RotationPolicy(overlap_seconds=3600))

        assert out.new_handle == "KID9"
        assert store.puts == [("kv/sendgrid-api-key", b"SG.new-secret")]
        assert minted[0].get_method() == "POST"
