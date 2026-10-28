# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``AwsSnsSmsChannelAdapter`` — Amazon SNS ``publish`` for SMS.

Pins:
- Capabilities (OUTBOUND + INTERNAL ceiling)
- ``sns.publish`` wire shape (PhoneNumber / Message / MessageAttributes)
- boto3 client injectable (offline, no network)
- urgency prefix in the body
- auth-class ClientError → ``reconnect_required``
- secret redaction (AWS secret key never in error text)
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.aws_sns_sms import (
    AwsSnsSmsChannelAdapter,
    AwsSnsSmsChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.channels.base import Direction
from axiom.governance import Classification

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class _FakeSnsClient:
    def __init__(self, *, message_id="sns-1", raise_exc=None):
        self.calls: list[dict] = []
        self._message_id = message_id
        self._raise = raise_exc

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return {"MessageId": self._message_id}


class _FakeClientError(Exception):
    """Stand-in for botocore ClientError (carries ``.response``)."""

    def __init__(self, code, message="denied"):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def _adapter(client, *, sender_id=None):
    return AwsSnsSmsChannelAdapter(
        region="us-east-1", sender_id=sender_id, client=client
    )


class TestProvider:
    def test_capabilities(self):
        caps = AwsSnsSmsChannelAdapterProvider().capabilities()
        assert caps.direction == Direction.OUTBOUND
        assert caps.classification_ceiling == Classification.INTERNAL
        assert caps.connector_ref == "aws-sns-account"

    def test_build_requires_region_without_client(self, monkeypatch):
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        with pytest.raises(ValueError, match="region"):
            AwsSnsSmsChannelAdapterProvider().build({})

    def test_build_with_injected_client_ok(self):
        adapter = AwsSnsSmsChannelAdapterProvider().build(
            {"client": _FakeSnsClient()}
        )
        assert adapter.name == "aws-sns-sms"


class TestWireShape:
    def test_publish_shape(self):
        client = _FakeSnsClient(message_id="sns-42")
        result = _adapter(client, sender_id="HERALD").deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="dose-rate excursion",
        )
        assert result.ok is True
        assert result.message_id == "sns-42"
        call = client.calls[0]
        assert call["PhoneNumber"] == "+15125550199"
        assert "dose-rate excursion" in call["Message"]
        attrs = call["MessageAttributes"]
        assert attrs["AWS.SNS.SMS.SMSType"]["StringValue"] == "Transactional"
        assert attrs["AWS.SNS.SMS.SenderID"]["StringValue"] == "HERALD"

    def test_urgent_prefix(self):
        client = _FakeSnsClient()
        _adapter(client).deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="scram",
        )
        body = client.calls[0]["Message"]
        assert any(m in body for m in ("URGENT", "🚨"))


class TestFailures:
    @pytest.mark.parametrize(
        "code", ["AuthorizationError", "InvalidClientTokenId", "ExpiredToken"]
    )
    def test_auth_class_sets_reconnect(self, code):
        client = _FakeSnsClient(raise_exc=_FakeClientError(code))
        result = _adapter(client).deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is False
        assert result.reconnect_required is True

    def test_generic_error_not_reconnect(self):
        client = _FakeSnsClient(raise_exc=_FakeClientError("Throttling"))
        result = _adapter(client).deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is False
        assert result.reconnect_required is False

    def test_secret_redacted(self):
        client = _FakeSnsClient(raise_exc=RuntimeError(f"key {SECRET} rejected"))
        adapter = AwsSnsSmsChannelAdapter(
            region="us-east-1",
            aws_secret_access_key=SECRET,
            client=client,
        )
        result = adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is False
        assert SECRET not in (result.error or "")
