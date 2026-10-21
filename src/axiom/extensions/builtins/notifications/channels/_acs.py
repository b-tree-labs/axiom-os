# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Shared Azure Communication Services (ACS) request signing.

Both the ACS SMS channel (``acs_sms.py``) and the ACS email backend
(``email/acs.py``) authenticate the same way: either an HMAC-SHA256
signature derived from the connection-string access key, or an Entra
(Azure AD) bearer token. This module holds the HMAC scheme so the two
adapters share one audited implementation.

The HMAC scheme is Azure's standard ``HMAC-SHA256`` request signing
(the same one ``azure-core``'s ``HttpLoggingPolicy`` / shared-key policy
emits): the string-to-sign is ``VERB\\npath-and-query\\ndate;host;content-hash``
and the signed headers are ``x-ms-date;host;x-ms-content-sha256``.

No Azure SDK dependency — this is raw ``hmac``/``hashlib`` so the base
install stays lean (per the connector-quality bar; lazy/no heavy deps).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import urlparse


def parse_connection_string(connection_string: str) -> tuple[str, str]:
    """Return ``(endpoint, access_key_b64)`` from an ACS connection string.

    Format: ``endpoint=https://<res>.communication.azure.com/;accesskey=<b64>``.
    Raises ``ValueError`` when either component is missing so a
    misconfiguration surfaces at build time, not at first send.
    """
    parts: dict[str, str] = {}
    for token in connection_string.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        parts[key.strip().lower()] = value.strip()
    endpoint = parts.get("endpoint", "")
    access_key = parts.get("accesskey", "")
    if not endpoint:
        raise ValueError("ACS connection string missing `endpoint=`")
    if not access_key:
        raise ValueError("ACS connection string missing `accesskey=`")
    return endpoint.rstrip("/"), access_key


def rfc1123_now(clock: object | None = None) -> str:
    """RFC-1123 GMT timestamp for the ``x-ms-date`` header.

    ``clock`` is an optional zero-arg callable returning a ``datetime``
    (test seam); defaults to ``datetime.now(UTC)``.
    """
    now = clock() if callable(clock) else datetime.now(UTC)
    return now.strftime("%a, %d %b %Y %H:%M:%S GMT")


def content_sha256(body: bytes) -> str:
    """Base64(SHA-256(body)) — the ``x-ms-content-sha256`` header value."""
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def sign_request(
    *,
    access_key_b64: str,
    method: str,
    url: str,
    body: bytes,
    date_str: str,
) -> dict[str, str]:
    """Return the ACS HMAC auth headers for one request.

    The returned dict carries ``x-ms-date``, ``x-ms-content-sha256`` and
    the ``Authorization: HMAC-SHA256 ...`` header. The caller merges these
    into the outbound request headers.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    content_hash = content_sha256(body)
    string_to_sign = f"{method}\n{path_and_query}\n{date_str};{host};{content_hash}"
    decoded_key = base64.b64decode(access_key_b64)
    signature = base64.b64encode(
        hmac.new(decoded_key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return {
        "x-ms-date": date_str,
        "x-ms-content-sha256": content_hash,
        "Authorization": (
            "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256"
            f"&Signature={signature}"
        ),
    }


__all__ = [
    "content_sha256",
    "parse_connection_string",
    "rfc1123_now",
    "sign_request",
]
