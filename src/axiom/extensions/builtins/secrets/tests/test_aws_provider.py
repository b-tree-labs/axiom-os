# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the aws_secrets_manager SecretStoreProvider.

Two layers of fakes:

* ``moto.mock_aws`` for the round-trip + overlap-window behavior, so the
  AWSCURRENT/AWSPREVIOUS staging-label semantics are exercised against a
  faithful Secrets Manager emulation (no AWS account needed).
* A tiny hand-rolled client for error-translation paths moto won't
  easily produce on demand (403, malformed refs).
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from axiom.extensions.builtins.secrets import (
    AWSSecretsManagerProvider,
    SecretRef,
    SecretStoreRegistry,
)
from axiom.extensions.builtins.secrets.providers.aws_secrets_manager import (
    _AWSSecretsManagerStore,
    _looks_like_region,
    _payload_bytes,
    _sdk_status,
)

_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# moto-backed fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def aws():
    with mock_aws():
        yield boto3.client("secretsmanager", region_name=_REGION)


@pytest.fixture
def provider(aws):
    return AWSSecretsManagerProvider(
        {"name": "primary", "region": _REGION, "_client": aws}
    )


# ---------------------------------------------------------------------------
# SecretRef -> SecretId translation
# ---------------------------------------------------------------------------


class TestSecretIdTranslation:
    def test_plain_name(self, provider):
        store = provider.open()
        assert store._secret_id(SecretRef.parse("aws://db-password")) == (
            "db-password"
        )

    def test_region_prefix_stripped(self, provider):
        store = provider.open()
        ref = SecretRef.parse("aws://us-west-2/db-password")
        assert store._secret_id(ref) == "db-password"

    def test_non_region_slash_kept(self, provider):
        # A name that happens to contain a slash but no region prefix.
        store = provider.open()
        ref = SecretRef.parse("aws://team/db-password")
        assert store._secret_id(ref) == "team/db-password"

    def test_arn_passthrough(self, provider):
        store = provider.open()
        arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbCdEf"
        ref = SecretRef.parse(f"aws://{arn}")
        assert store._secret_id(ref) == arn

    def test_empty_path_raises(self, provider):
        store = provider.open()
        with pytest.raises(ValueError, match="missing secret id"):
            store._secret_id(SecretRef(scheme="aws", path=""))


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_put_creates_then_get(self, provider):
        ref = SecretRef.parse("aws://db-password")
        provider.open().put(ref, b"hunter2")
        s = provider.open().get(ref)
        assert s.value == b"hunter2"
        assert s.metadata["backend"] == "aws_secrets_manager"
        assert "AWSCURRENT" in s.metadata["stages"]

    def test_put_existing_stages_new_current(self, provider):
        ref = SecretRef.parse("aws://rotating")
        provider.open().put(ref, b"v1")
        provider.open().put(ref, b"v2")
        assert provider.open().get(ref).value == b"v2"

    def test_get_missing_is_keyerror(self, provider):
        with pytest.raises(KeyError):
            provider.open().get(SecretRef.parse("aws://never-existed"))

    def test_string_payload_decodes(self, provider, aws):
        # A secret written as SecretString (e.g. by another tool / console).
        aws.create_secret(Name="str-secret", SecretString="plain-text")
        s = provider.open().get(SecretRef.parse("aws://str-secret"))
        assert s.value == b"plain-text"


# ---------------------------------------------------------------------------
# Overlap window — the load-bearing rotation-without-outage logic
# ---------------------------------------------------------------------------


class TestOverlapWindow:
    def test_single_value_before_first_rotation(self, provider):
        ref = SecretRef.parse("aws://fresh")
        provider.open().put(ref, b"only")
        accepted = provider.open().resolve_overlap(ref)
        assert [s.value for s in accepted] == [b"only"]

    def test_both_values_accepted_during_overlap(self, provider):
        ref = SecretRef.parse("aws://rotated")
        provider.open().put(ref, b"old")
        provider.open().put(ref, b"new")  # demotes "old" to AWSPREVIOUS
        accepted = provider.open().resolve_overlap(ref)
        values = [s.value for s in accepted]
        # New value first (acceptance order), old still honored.
        assert values == [b"new", b"old"]

    def test_old_secret_still_works_if_flip_missed(self, provider):
        """The whole point: a consumer that never updated to the new
        secret keeps authenticating against the overlap set."""
        ref = SecretRef.parse("aws://creds")
        provider.open().put(ref, b"OLD-KEY")
        provider.open().put(ref, b"NEW-KEY")
        accepted = {s.value for s in provider.open().resolve_overlap(ref)}
        # An app still presenting the old key is inside the accepted set.
        assert b"OLD-KEY" in accepted
        assert b"NEW-KEY" in accepted

    def test_explicit_stage_bypasses_overlap(self, provider):
        ref = SecretRef.parse("aws://rolled")
        provider.open().put(ref, b"old")
        provider.open().put(ref, b"new")
        prev = SecretRef.parse("aws://rolled?stage=AWSPREVIOUS")
        accepted = provider.open().resolve_overlap(prev)
        assert [s.value for s in accepted] == [b"old"]

    def test_get_previous_stage_directly(self, provider):
        ref = SecretRef.parse("aws://rolled2")
        provider.open().put(ref, b"old")
        provider.open().put(ref, b"new")
        prev = provider.open().get(SecretRef.parse("aws://rolled2?stage=AWSPREVIOUS"))
        assert prev.value == b"old"


# ---------------------------------------------------------------------------
# Delete + list
# ---------------------------------------------------------------------------


class TestDeleteList:
    def test_delete_then_missing(self, provider):
        ref = SecretRef.parse("aws://doomed")
        provider.open().put(ref, b"x")
        provider.open().delete(ref)
        with pytest.raises(KeyError):
            provider.open().get(ref)

    def test_list_with_prefix(self, provider):
        provider.open().put(SecretRef.parse("aws://dp1-a"), b"x")
        provider.open().put(SecretRef.parse("aws://dp1-b"), b"x")
        provider.open().put(SecretRef.parse("aws://other"), b"x")
        assert provider.open().list_paths("dp1-") == ["dp1-a", "dp1-b"]

    def test_list_region_prefix_sugar(self, provider):
        provider.open().put(SecretRef.parse("aws://k-1"), b"x")
        assert provider.open().list_paths("us-east-1/k-") == ["k-1"]


# ---------------------------------------------------------------------------
# Rotation trigger
# ---------------------------------------------------------------------------


class TestRotate:
    def test_rotate_triggers_backend_call(self, provider):
        # rotate() delegates to SM's own rotation (it does not rotate
        # in-process). Against moto the call succeeds; we assert it does
        # not raise for a valid secret.
        ref = SecretRef.parse("aws://to-rotate")
        provider.open().put(ref, b"x")
        provider.open().rotate(ref)

    def test_rotate_missing_secret_surfaces_error(self, provider):
        with pytest.raises((RuntimeError, KeyError, PermissionError)):
            provider.open().rotate(SecretRef.parse("aws://never-existed"))


# ---------------------------------------------------------------------------
# Error translation (hand fake — deterministic 403)
# ---------------------------------------------------------------------------


class _DenyClient:
    def get_secret_value(self, **_kw):
        raise type("AccessDeniedException", (Exception,), {})("nope")


class TestErrorTranslation:
    def test_403_to_permissionerror(self):
        store = _AWSSecretsManagerStore(default_region=_REGION, client=_DenyClient())
        with pytest.raises(PermissionError):
            store.get(SecretRef.parse("aws://anything"))

    def test_sdk_status_from_client_error_shape(self):
        exc = Exception("boom")
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "ResourceNotFoundException"},
        }
        assert _sdk_status(exc) == 404

    def test_sdk_status_from_http_metadata(self):
        exc = Exception("boom")
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "Throttling"},
            "ResponseMetadata": {"HTTPStatusCode": 503},
        }
        assert _sdk_status(exc) == 503

    def test_sdk_status_from_named_exception(self):
        assert _sdk_status(type("NotFound", (Exception,), {})("x")) == 404


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("us-east-1", True),
            ("eu-west-2", True),
            ("ap-southeast-1", True),  # 3 dash-segments: alpha/alpha/digit
            ("team", False),
            ("db-password", False),
            ("a/b", False),
        ],
    )
    def test_looks_like_region(self, token, expected):
        assert _looks_like_region(token) is expected

    def test_payload_bytes_prefers_binary(self):
        assert _payload_bytes({"SecretBinary": b"bin"}) == b"bin"

    def test_payload_bytes_falls_back_to_string(self):
        assert _payload_bytes({"SecretString": "abc"}) == b"abc"

    def test_payload_bytes_empty(self):
        assert _payload_bytes({}) == b""


# ---------------------------------------------------------------------------
# Provider factory + registry wiring
# ---------------------------------------------------------------------------


class TestProvider:
    def test_capabilities_advertise_aws_shape(self):
        caps = AWSSecretsManagerProvider.capabilities
        assert caps.read and caps.write and caps.delete and caps.list_paths
        assert caps.rotation is True
        assert caps.audit_stream and caps.encryption_at_rest
        assert caps.dynamic_credentials is False

    def test_available_is_bool(self, aws):
        p = AWSSecretsManagerProvider(
            {"name": "p", "region": _REGION, "_client": aws}
        )
        assert isinstance(p.available(), bool)

    def test_registered_under_aws_kind(self):
        assert "aws" in SecretStoreRegistry.available_kinds()
        assert SecretStoreRegistry.get("aws") is AWSSecretsManagerProvider

    def test_factory_creates_store(self, aws):
        p = AWSSecretsManagerProvider(
            {"name": "p", "region": "eu-west-1", "_client": aws}
        )
        store = p.open()
        assert isinstance(store, _AWSSecretsManagerStore)
        assert store._default_region == "eu-west-1"


class TestUnsupportedOps:
    def test_lease_refused(self, provider):
        with pytest.raises(PermissionError, match="leased"):
            provider.open().lease(SecretRef.parse("aws://x"), 60)
