# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the AWS SES email backend (``ses`` provider).

Pins:
- registered under ``ses`` at import time
- SESv2 ``send_email`` wire shape (From / Destination / Content)
- MessageId surfaced on success
- boto3 client is injectable (offline, no network)
- botocore failures become structured EmailSendResult(ok=False)
- the AWS secret access key never round-trips through error text
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channels.email import (
    EmailChannelAdapterProvider,
    EmailMessage,
    SesEmailProvider,
    detect_email_provider,
    email_provider_names,
)
from axiom.governance import Classification

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"


class _FakeSesClient:
    def __init__(self, *, message_id="ses-msg-1", raise_exc=None):
        self.calls: list[dict] = []
        self._message_id = message_id
        self._raise = raise_exc

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return {"MessageId": self._message_id}


def _msg(**overrides):
    base = dict(
        to=("alice@example.com",),
        subject="DP1 ingest complete",
        from_address="herald@b-tree.labs",
        body_text="ingest finished",
    )
    base.update(overrides)
    return EmailMessage(**base)


def test_registered_at_import():
    assert "ses" in email_provider_names()


def test_detect_ses_via_explicit_provider():
    p = detect_email_provider({"provider": "ses", "region": "us-east-1"})
    assert isinstance(p, SesEmailProvider)
    assert p.name == "ses"


def test_detect_ses_via_access_key():
    p = detect_email_provider({"ses_access_key_id": ACCESS_KEY_ID})
    assert isinstance(p, SesEmailProvider)


def test_happy_path_returns_message_id():
    client = _FakeSesClient(message_id="ses-987")
    p = SesEmailProvider(region="us-east-1", client=client)
    result = p.send(_msg(from_name="HERALD"))
    assert result.ok is True
    assert result.provider == "ses"
    assert result.message_id == "ses-987"
    req = client.calls[0]
    assert req["FromEmailAddress"] == "HERALD <herald@b-tree.labs>"
    assert req["Destination"]["ToAddresses"] == ["alice@example.com"]
    simple = req["Content"]["Simple"]
    assert simple["Subject"]["Data"] == "DP1 ingest complete"
    assert simple["Body"]["Text"]["Data"] == "ingest finished"


def test_html_and_cc_bcc():
    client = _FakeSesClient()
    p = SesEmailProvider(region="us-east-1", client=client)
    p.send(
        _msg(
            body_html="<b>done</b>",
            cc=("watch@example.com",),
            bcc=("audit@example.com",),
        )
    )
    req = client.calls[0]
    assert req["Content"]["Simple"]["Body"]["Html"]["Data"] == "<b>done</b>"
    assert req["Destination"]["CcAddresses"] == ["watch@example.com"]
    assert req["Destination"]["BccAddresses"] == ["audit@example.com"]


def test_client_error_is_structured_failure():
    client = _FakeSesClient(raise_exc=RuntimeError("SES throttled"))
    p = SesEmailProvider(region="us-east-1", client=client)
    result = p.send(_msg())
    assert result.ok is False
    assert "throttled" in (result.error or "")


def test_secret_key_redacted_in_error():
    client = _FakeSesClient(raise_exc=RuntimeError(f"bad creds {SECRET}"))
    p = SesEmailProvider(
        region="us-east-1",
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=SECRET,
        client=client,
    )
    result = p.send(_msg())
    assert result.ok is False
    assert SECRET not in (result.error or "")


def test_via_email_channel_adapter():
    client = _FakeSesClient(message_id="ses-chan")
    adapter = EmailChannelAdapterProvider().build(
        {
            "from_address": "herald@b-tree.labs",
            "provider": "ses",
            "region": "us-east-1",
            "client": client,
        }
    )
    result = adapter.deliver_sync(
        recipient="alice@example.com",
        receipt_id="r-1",
        classification=Classification.INTERNAL,
        priority="normal",
        summary="hello",
    )
    assert result.ok is True
    assert result.provider == "ses"
    assert result.message_id == "ses-chan"
