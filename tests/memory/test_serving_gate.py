# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Serving gate — one door out, fail-closed (ADR-087 D7; security doc §2/§3).

The gate sits AFTER retrieval, BEFORE text serialization. It is the single
choke point every transport funnels through. Doubt → deny, enumerated:
vault-never (unconditional), secret-routed-to-vault (OQ6), unlabeled-deny,
policy-error-deny, cross-account-deny, deployment-tier-deny, and
unresolved-consumer-deny. No-push is a structural property (query-time only).
"""

from __future__ import annotations

import pytest

from axiom.memory.fragment import create_fragment
from axiom.memory.serving import (
    ConsumerCoordinate,
    DenyReason,
    NoPushError,
    PolicyUnavailable,
    ServableItem,
    ServingGate,
    looks_like_secret,
    refuse_push,
)
from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)

LOCAL = "local"
REMOTE = "remote"


def _consumer(**kw) -> ConsumerCoordinate:
    base = dict(
        principal="@alice:work",
        harness="claude-code",
        account="work",
        deployment_tier=LOCAL,
        model_endpoint="local://ollama",
    )
    base.update(kw)
    return ConsumerCoordinate(**base)


def _item(**kw) -> ServableItem:
    base = dict(
        fragment_id="f1",
        cognitive_type="semantic",
        visibility=VisibilityHorizon.PUBLIC.value,
        classification=ClassificationStamp.unclassified().to_dict(),
        account="work",
        text="alice prefers test-driven development",
    )
    base.update(kw)
    return ServableItem(**base)


class TestVaultNever:
    def test_vault_denied_unconditionally(self):
        gate = ServingGate()
        d = gate.evaluate(_item(cognitive_type="vault"), _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.VAULT

    def test_vault_denied_even_with_permissive_policy(self):
        gate = ServingGate(policy=lambda item, consumer: True)
        d = gate.evaluate(_item(cognitive_type="vault"), _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.VAULT


class TestSecretRouting:
    def test_secret_content_routed_to_vault_denied(self):
        gate = ServingGate()
        secret = _item(text="my openai key is sk-abcdef0123456789abcdef0123456789")
        d = gate.evaluate(secret, _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.SECRET_ROUTED_TO_VAULT

    def test_looks_like_secret_matches_common_shapes(self):
        assert looks_like_secret("token=ghp_0123456789abcdefABCDEF0123456789abcd")
        assert looks_like_secret("AKIA0123456789ABCDEF is the access key")
        assert looks_like_secret("-----BEGIN RSA PRIVATE KEY-----")
        assert looks_like_secret('password: "hunter2-super-secret-value!"')

    def test_ordinary_text_is_not_a_secret(self):
        assert looks_like_secret("alice prefers dark roast coffee") is None
        assert looks_like_secret("the deploy checklist has six steps") is None


class TestUnlabeled:
    def test_missing_visibility_denied(self):
        gate = ServingGate()
        d = gate.evaluate(_item(visibility=None), _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.UNLABELED

    def test_unknown_visibility_denied(self):
        gate = ServingGate()
        d = gate.evaluate(_item(visibility="totally-made-up"), _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.UNLABELED

    def test_unknown_classification_level_denied(self):
        gate = ServingGate()
        d = gate.evaluate(
            _item(classification={"level": "ultra-cosmic"}), _consumer()
        )
        assert not d.allowed
        assert d.reason is DenyReason.UNLABELED


class TestPolicyFailClosed:
    def test_policy_raises_denies(self):
        def boom(item, consumer):
            raise RuntimeError("policy backend exploded")

        gate = ServingGate(policy=boom)
        d = gate.evaluate(_item(), _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.POLICY_ERROR

    def test_policy_unavailable_denies(self):
        def unreachable(item, consumer):
            raise PolicyUnavailable("policy engine unreachable")

        gate = ServingGate(policy=unreachable)
        d = gate.evaluate(_item(), _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.POLICY_UNAVAILABLE

    def test_policy_clean_false_denies(self):
        gate = ServingGate(policy=lambda item, consumer: False)
        d = gate.evaluate(_item(), _consumer())
        assert not d.allowed
        assert d.reason is DenyReason.POLICY_DENIED


class TestConsumerResolution:
    def test_unresolved_consumer_denies(self):
        gate = ServingGate()
        d = gate.evaluate(_item(), _consumer(account=""))
        assert not d.allowed
        assert d.reason is DenyReason.UNRESOLVED_CONSUMER

    def test_empty_principal_denies(self):
        gate = ServingGate()
        d = gate.evaluate(_item(), _consumer(principal=""))
        assert not d.allowed
        assert d.reason is DenyReason.UNRESOLVED_CONSUMER


class TestCrossAccount:
    def test_foreign_account_denied(self):
        gate = ServingGate()
        d = gate.evaluate(_item(account="personal"), _consumer(account="work"))
        assert not d.allowed
        assert d.reason is DenyReason.CROSS_ACCOUNT

    def test_matching_account_allowed(self):
        gate = ServingGate()
        d = gate.evaluate(_item(account="work"), _consumer(account="work"))
        assert d.allowed

    def test_declared_compatible_account_allowed(self):
        gate = ServingGate()
        consumer = _consumer(
            account="work", compatible_accounts=frozenset({"work", "shared"})
        )
        d = gate.evaluate(_item(account="shared"), consumer)
        assert d.allowed


class TestDeploymentTier:
    def test_controlled_content_denied_to_remote(self):
        gate = ServingGate()
        controlled = _item(visibility=VisibilityHorizon.SCOPE_INTERNAL.value)
        d = gate.evaluate(controlled, _consumer(deployment_tier=REMOTE))
        assert not d.allowed
        assert d.reason is DenyReason.TIER_MISMATCH

    def test_controlled_content_allowed_to_local(self):
        gate = ServingGate()
        controlled = _item(visibility=VisibilityHorizon.SCOPE_INTERNAL.value)
        d = gate.evaluate(controlled, _consumer(deployment_tier=LOCAL))
        assert d.allowed

    def test_public_content_allowed_to_remote(self):
        gate = ServingGate()
        d = gate.evaluate(
            _item(visibility=VisibilityHorizon.PUBLIC.value),
            _consumer(deployment_tier=REMOTE),
        )
        assert d.allowed

    def test_secret_classification_denied_to_remote(self):
        gate = ServingGate()
        secret = _item(
            visibility=VisibilityHorizon.PUBLIC.value,
            classification={"level": "secret"},
        )
        d = gate.evaluate(secret, _consumer(deployment_tier=REMOTE))
        assert not d.allowed
        assert d.reason is DenyReason.TIER_MISMATCH


class TestFilterAndFragmentView:
    def test_filter_partitions_allowed_and_denied(self):
        gate = ServingGate()
        items = [
            _item(fragment_id="ok"),
            _item(fragment_id="vault", cognitive_type="vault"),
            _item(fragment_id="foreign", account="personal"),
        ]
        allowed, denials = gate.filter(items, _consumer(account="work"))
        assert [i.fragment_id for i in allowed] == ["ok"]
        assert {d.fragment_id: d.reason for d in denials} == {
            "vault": DenyReason.VAULT,
            "foreign": DenyReason.CROSS_ACCOUNT,
        }

    def test_from_fragment_maps_labels_and_account(self):
        frag = create_fragment(
            content={"fact": "alice likes almond croissants"},
            cognitive_type="semantic",
            principal_id="@alice:work",
            agents={"axi"},
            resources=set(),
        )
        item = ServableItem.from_fragment(frag)
        assert item.fragment_id == frag.id
        assert item.cognitive_type == "semantic"
        assert item.account == "@alice:work"  # native → owning principal
        assert "almond croissants" in item.text
        assert item.visibility == VisibilityHorizon.SCOPE_INTERNAL.value

    def test_from_absorbed_fragment_uses_origin_account(self):
        import dataclasses

        from axiom.memory.fragment import SourceOrigin

        frag = create_fragment(
            content={"fact": "imported note"},
            cognitive_type="semantic",
            principal_id="@alice:work",
            agents={"axi"},
            resources=set(),
        )
        origin = SourceOrigin(
            harness="chatgpt",
            account="personal-openai",
            source_ref="row-42",
            imported_at="2026-07-01T00:00:00+00:00",
        )
        frag = dataclasses.replace(
            frag,
            provenance=dataclasses.replace(frag.provenance, origin=origin),
        )
        item = ServableItem.from_fragment(frag)
        assert item.account == "personal-openai"


class TestNoPush:
    def test_refuse_push_always_raises(self):
        with pytest.raises(NoPushError):
            refuse_push("any-foreign-store")
