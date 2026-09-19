# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the easy-onramp authn pattern."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from axiom.governance.simple import (
    AuthnUnavailable,
    AuthorizationDenied,
    ExtensionAuthnContext,
    get_current_actor,
    set_current_actor,
    setup_extension,
)
from axiom.vega.identity.principal import Principal


@pytest.fixture(autouse=True)
def _clean_thread_local():
    """Reset thread-local actor between tests."""
    from axiom.governance.simple import _actor_local

    if hasattr(_actor_local, "principal"):
        del _actor_local.principal
    yield
    if hasattr(_actor_local, "principal"):
        del _actor_local.principal


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


class TestActorResolution:
    def test_set_current_actor_wins(self):
        alice = Principal(handle="@alice:test", public_bytes=b"\x00" * 32)
        set_current_actor(alice)
        assert get_current_actor() is alice

    def test_env_var_resolution(self):
        with mock.patch.dict(os.environ, {"AXIOM_ACTOR": "@austin:example-org"}):
            p = get_current_actor()
            assert p.handle == "@austin:example-org"
            # Public bytes are derived, never empty.
            assert len(p.public_bytes) > 0

    def test_env_var_normalizes_missing_at(self):
        with mock.patch.dict(os.environ, {"AXIOM_ACTOR": "austin:example-org"}):
            p = get_current_actor()
            assert p.handle == "@austin:example-org"

    def test_dev_mode_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            p = get_current_actor(dev_mode=True)
            assert p.handle.startswith("@dev:")

    def test_no_actor_no_dev_mode_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(AuthnUnavailable):
                get_current_actor()


# ---------------------------------------------------------------------------
# setup_extension + action()
# ---------------------------------------------------------------------------


class TestSetupExtension:
    def test_dev_mode_works_without_postgres(self):
        # The whole point: setup_extension in dev_mode must succeed
        # even if neither GUARD nor KEEP are wired up.
        with mock.patch.dict(os.environ, {"AXIOM_ACTOR": "@dev:test"}):
            ext = setup_extension("test_ext", verbs=["frobnicate"], dev_mode=True)
        assert ext.extension_name == "test_ext"
        # Capability assigned even without KEEP.
        assert ext.default_capability is not None

    def test_verbs_get_registered(self):
        from axiom.governance.intent import REGISTERED_INTENTS

        with mock.patch.dict(os.environ, {"AXIOM_ACTOR": "@dev:test"}):
            setup_extension("test_ext2", verbs=["custom_verb"], dev_mode=True)
        assert "test_ext2.custom_verb" in REGISTERED_INTENTS

    def test_action_yields_handle_with_receipt(self):
        with mock.patch.dict(os.environ, {"AXIOM_ACTOR": "@austin:example-org"}):
            ext = setup_extension("test_ext3", verbs=["transition_state"], dev_mode=True)
            with ext.action(
                verb="transition_state",
                resource="extension://test_ext3/sample/SR-007",
            ) as act:
                assert act.receipt_id
                assert act.envelope.intent.value == "test_ext3.transition_state"
                assert act.envelope.actor.handle == "@austin:example-org"

    def test_action_with_explicit_actor_string(self):
        ext = setup_extension("test_ext4", verbs=["x"], dev_mode=True)
        with ext.action(
            verb="x",
            actor="@nick:example-org",
            resource="extension://test_ext4/foo",
        ) as act:
            assert act.envelope.actor.handle == "@nick:example-org"

    def test_action_with_explicit_principal(self):
        nick = Principal(handle="@nick:example-org", public_bytes=b"\x01" * 32)
        ext = setup_extension("test_ext5", verbs=["x"], dev_mode=True)
        with ext.action(
            verb="x",
            actor=nick,
            resource="extension://test_ext5/foo",
        ) as act:
            assert act.envelope.actor is nick

    def test_dev_mode_permits_when_no_decide_ctx(self):
        # The dev_mode permits without GUARD wired up — this is the
        # property that makes Austin's first iteration "just work".
        ext = ExtensionAuthnContext(
            extension_name="ad_hoc",
            dev_mode=True,
        )
        # Manually attach a stub capability (skipping setup_extension).
        from axiom.governance.capability import CapabilityToken

        alice = Principal(handle="@alice:test", public_bytes=b"\x00" * 32)
        ext.default_capability = CapabilityToken.unscoped_test_token(subject=alice)
        from axiom.governance.intent import register_intent

        register_intent("ad_hoc.x")
        with ext.action(
            verb="ad_hoc.x",
            actor=alice,
            resource="extension://ad_hoc/foo",
        ) as act:
            assert act.receipt_id

    def test_strict_mode_no_authz_raises(self):
        from axiom.governance.capability import CapabilityToken

        alice = Principal(handle="@alice:test", public_bytes=b"\x00" * 32)
        ext = ExtensionAuthnContext(
            extension_name="strict",
            dev_mode=False,  # not dev_mode
        )
        ext.default_capability = CapabilityToken.unscoped_test_token(
            subject=alice
        )
        from axiom.governance.intent import register_intent

        register_intent("strict.x")
        with pytest.raises(RuntimeError, match="has no authz_ctx"):
            with ext.action(
                verb="strict.x",
                actor=alice,
                resource="extension://strict/foo",
            ):
                pass


# ---------------------------------------------------------------------------
# AuthorizationDenied propagation
# ---------------------------------------------------------------------------


class TestAuthorizationDeniedPropagation:
    """When GUARD denies, the `with` block must not execute the body."""

    def test_denied_short_circuits_block(self):
        from axiom.extensions.builtins.authz import DecideContext, Rule
        from axiom.governance import IntentPattern, ResourcePattern

        ctx = DecideContext()
        ctx.add_rule(
            Rule(
                name="deny_all",
                intent_pattern=IntentPattern("*"),
                actor_pattern="*",
                resource_pattern=ResourcePattern("*"),
                disposition="deny",
            )
        )
        ext = setup_extension(
            "denied_ext", verbs=["x"], dev_mode=True, wire_authz=False
        )
        ext.authz_ctx = ctx  # inject the deny-all DecideContext

        body_ran = []

        with pytest.raises(AuthorizationDenied) as exc_info:
            with ext.action(
                verb="x",
                actor="@austin:example-org",
                resource="extension://denied_ext/foo",
            ):
                body_ran.append("nope")

        assert not body_ran
        assert exc_info.value.verdict.decision.value == "deny"
