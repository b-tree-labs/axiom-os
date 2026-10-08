# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``aws-sns-sms`` channel adapter — Amazon SNS ``publish`` for SMS.

Sibling of ``twilio_sms.py``: the same lock-screen safety-escalation
tier, delivered through AWS SNS instead of Twilio. Auth follows boto3's
default credential chain (IAM role / instance profile / env) OR explicit
access keys in config. ``boto3`` is a **lazy import** and an *optional*
extra (``axiom-os-lm[herald-aws]``) so the base install stays lean.

boto3 owns its own transport-level retry/backoff, so unlike the raw-HTTP
adapters this one does not re-implement ``Retry-After`` handling; it
surfaces the failure as a structured result. Auth-class failures
(``AuthorizationError`` / ``InvalidClientTokenId`` / ``SignatureDoesNotMatch``
/ ``UnrecognizedClientException``) set ``reconnect_required`` so the
connector can prompt a re-auth.

Per spec §9 the ceiling is ``INTERNAL`` — matches the SMS/chat tier.
Every EXTERNAL cloud channel is ``INTERNAL``-ceilinged so ``regulated`` /
``controlled`` (EC-controlled / ITAR) envelopes are never admitted here
and fall back to the inbox channel (see ``send.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.sender import SenderIdentity

from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelCapabilities,
    Direction,
)
from axiom.governance import Classification

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class AwsSnsSmsDispatchResult:
    ok: bool
    error: str | None = None
    message_id: str | None = None
    """SNS ``MessageId`` on success."""
    reconnect_required: bool = False
    retry_attempts: int = 0


# ---------------------------------------------------------------------------
# Body shaping — reuse the SMS urgency-prefix convention
# ---------------------------------------------------------------------------


_URGENCY_PREFIX = {
    "urgent": "🚨 URGENT — ",
    "high": "⚠️ HIGH — ",
    "normal": "",
    "low": "",
}

# SNS/AWS error codes that mean "your credentials are bad" → reconnect.
_RECONNECT_CODES = {
    "AuthorizationError",
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "UnrecognizedClientException",
    "AccessDenied",
    "AccessDeniedException",
    "ExpiredToken",
    "ExpiredTokenException",
}


def _body_for(summary: str, priority: str) -> str:
    prefix = _URGENCY_PREFIX.get(priority, "")
    body = f"{prefix}{summary}"
    if len(body) > 320:
        body = body[:317] + "…"
    return body


def _error_code(exc: Exception) -> str | None:
    """Best-effort extraction of a botocore ClientError code."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        err = resp.get("Error")
        if isinstance(err, dict):
            code = err.get("Code")
            if isinstance(code, str):
                return code
    return None


def _build_secret_stripper(*secrets: str | None):
    real = [s for s in secrets if s]

    def _strip(text: str) -> str:
        if not text:
            return text
        for s in real:
            text = text.replace(s, "***")
        return text

    return _strip


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AwsSnsSmsChannelAdapter:
    """Outbound HERALD adapter for Amazon SNS SMS (``sns.publish``)."""

    name = "aws-sns-sms"

    def __init__(
        self,
        *,
        region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        sender_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._region = region
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._sender_id = sender_id
        self._client = client
        self._strip = _build_secret_stripper(
            aws_secret_access_key, aws_access_key_id
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        import boto3  # lazy — optional extra [herald-aws]

        kwargs: dict[str, Any] = {}
        if self._region:
            kwargs["region_name"] = self._region
        if self._aws_access_key_id and self._aws_secret_access_key:
            kwargs["aws_access_key_id"] = self._aws_access_key_id
            kwargs["aws_secret_access_key"] = self._aws_secret_access_key
        self._client = boto3.client("sns", **kwargs)
        return self._client

    def deliver_sync(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
        sender: SenderIdentity | None = None,
    ) -> AwsSnsSmsDispatchResult:
        body = _body_for(summary, priority)
        if sender is not None:
            from axiom.extensions.builtins.notifications.sender import (
                render_for_channel,
            )

            _rs = render_for_channel(sender, "twilio_sms")
            if _rs.body_prefix:
                body = f"{_rs.body_prefix} {body}"

        try:
            client = self._get_client()
        except Exception as exc:  # noqa: BLE001 — client build / missing boto3
            return AwsSnsSmsDispatchResult(
                ok=False, error=self._strip(f"{type(exc).__name__}: {exc}")
            )

        message_attributes: dict[str, Any] = {
            # Transactional SMS = higher deliverability, no promotional throttling.
            "AWS.SNS.SMS.SMSType": {
                "DataType": "String",
                "StringValue": "Transactional",
            }
        }
        if self._sender_id:
            message_attributes["AWS.SNS.SMS.SenderID"] = {
                "DataType": "String",
                "StringValue": self._sender_id,
            }

        try:
            resp = client.publish(
                PhoneNumber=recipient,
                Message=body,
                MessageAttributes=message_attributes,
            )
        except Exception as exc:  # noqa: BLE001 — botocore ClientError etc.
            code = _error_code(exc)
            return AwsSnsSmsDispatchResult(
                ok=False,
                error=self._strip(f"{type(exc).__name__}: {exc}"),
                reconnect_required=code in _RECONNECT_CODES,
            )

        message_id = resp.get("MessageId") if isinstance(resp, dict) else None
        return AwsSnsSmsDispatchResult(ok=True, message_id=message_id)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AwsSnsSmsChannelAdapterProvider:
    """Factory. Credentials/region are passed at ``build()`` time from the
    caller's secret resolution; the provider is account-agnostic."""

    name = "aws-sns-sms"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="aws-sns-sms",
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            # Per spec §9 — external channel; INTERNAL ceiling (open+internal).
            classification_ceiling=Classification.INTERNAL,
            supports_threading=False,
            supports_acknowledge=False,
            delivery_sla_p95_ms=3000,
            connector_ref="aws-sns-account",
        )

    def build(
        self, config: dict[str, Any] | None = None
    ) -> AwsSnsSmsChannelAdapter:
        cfg = config or {}
        region = cfg.get("region") or cfg.get("aws_region")
        # region is required unless a pre-built client is injected (tests) or
        # AWS_DEFAULT_REGION is set in the boto3 default chain.
        client = cfg.get("client")
        if not region and client is None:
            import os

            if not (os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")):
                raise ValueError(
                    "aws-sns-sms channel requires `region` in config "
                    "(e.g. us-east-1) unless AWS_DEFAULT_REGION is set"
                )
        return AwsSnsSmsChannelAdapter(
            region=region,
            aws_access_key_id=cfg.get("aws_access_key_id"),
            aws_secret_access_key=cfg.get("aws_secret_access_key"),
            sender_id=cfg.get("sender_id"),
            client=client,
        )


__all__ = [
    "AwsSnsSmsChannelAdapter",
    "AwsSnsSmsChannelAdapterProvider",
    "AwsSnsSmsDispatchResult",
]
