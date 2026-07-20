# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AWS SES backend — Amazon Simple Email Service via boto3 (SESv2).

Registered under the ``ses`` name in the email factory. Auth follows
boto3's default credential chain (IAM role / instance profile / env /
shared config) OR explicit access keys passed in config. ``boto3`` is a
**lazy import** and an *optional* extra (``axiom-os-lm[herald-aws]``) so
the base install stays lean — the module imports fine without boto3 and
only fails (with a clear message) when a send is actually attempted
without the dependency.

The boto3 client is injectable (``config["client"]``) so tests stay
offline + deterministic, mirroring the poster/smtp-factory seams in the
sibling backends.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailMessage,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.email.factory import (
    register_email_provider,
)


def _build_secret_stripper(*secrets: str | None):
    """Return a redactor that strips any provided secret from error text.

    AWS secret access keys (and, defensively, the access-key id) must
    never round-trip through a receipt or log; SES error text can echo
    request context, so we exact-match redact anything sensitive we hold.
    """
    real = [s for s in secrets if s]

    def _strip(text: str) -> str:
        if not text:
            return text
        for s in real:
            text = text.replace(s, "***")
        return text

    return _strip


def _from_field(message: EmailMessage) -> str:
    if message.from_name:
        return f"{message.from_name} <{message.from_address}>"
    return message.from_address


class SesEmailProvider:
    """Amazon SES (SESv2 ``send_email``) outbound backend."""

    name = "ses"

    def __init__(
        self,
        *,
        region: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._region = region
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
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
        # Explicit keys are optional; without them boto3 uses its default
        # credential chain (IAM role / env / shared config).
        if self._aws_access_key_id and self._aws_secret_access_key:
            kwargs["aws_access_key_id"] = self._aws_access_key_id
            kwargs["aws_secret_access_key"] = self._aws_secret_access_key
        self._client = boto3.client("sesv2", **kwargs)
        return self._client

    def send(self, message: EmailMessage) -> EmailSendResult:
        try:
            client = self._get_client()
        except Exception as exc:  # noqa: BLE001 — client build / missing boto3
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=self._strip(f"{type(exc).__name__}: {exc}"),
            )

        body: dict[str, Any] = {}
        if message.body_text:
            body["Text"] = {"Data": message.body_text, "Charset": "UTF-8"}
        if message.body_html:
            body["Html"] = {"Data": message.body_html, "Charset": "UTF-8"}

        destination: dict[str, Any] = {"ToAddresses": list(message.to)}
        if message.cc:
            destination["CcAddresses"] = list(message.cc)
        if message.bcc:
            destination["BccAddresses"] = list(message.bcc)

        request: dict[str, Any] = {
            "FromEmailAddress": _from_field(message),
            "Destination": destination,
            "Content": {
                "Simple": {
                    "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                    "Body": body,
                }
            },
        }
        if message.reply_to:
            request["ReplyToAddresses"] = [message.reply_to]

        try:
            resp = client.send_email(**request)
        except Exception as exc:  # noqa: BLE001 — botocore ClientError etc.
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=self._strip(f"{type(exc).__name__}: {exc}"),
            )

        message_id = (
            resp.get("MessageId") if isinstance(resp, dict) else None
        )
        return EmailSendResult(
            ok=True,
            provider=self.name,
            message_id=message_id,
        )


def _build_ses(config: dict[str, Any]) -> SesEmailProvider:
    """Builder registered with the email factory under ``ses``.

    Accepts both the canonical AWS key names (``region``,
    ``aws_access_key_id``, ``aws_secret_access_key``) and the
    ``ses_``-prefixed aliases used by the factory's config-discovery rule
    (``ses_access_key_id`` etc.).
    """
    region = (
        config.get("region")
        or config.get("ses_region")
        or config.get("aws_region")
    )
    access_key_id = (
        config.get("aws_access_key_id") or config.get("ses_access_key_id")
    )
    secret_access_key = (
        config.get("aws_secret_access_key")
        or config.get("ses_secret_access_key")
    )
    return SesEmailProvider(
        region=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        client=config.get("client"),
    )


register_email_provider("ses", _build_ses)


__all__ = ["SesEmailProvider"]
