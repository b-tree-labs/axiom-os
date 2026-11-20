# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Concrete rotation strategies — the three shapes of ADR-095.

Which strategy a secret resolves to answers one question: *who mints the new
credential?*

- **The backend itself** → ``ProviderNativeRotation`` (AWS Secrets Manager
  Lambda, Vault dynamic engines). We trigger ``store.rotate()`` and the backend
  produces + ages out the overlap window; ``revoke_previous`` is a no-op.
- **An external vendor's API** → a vendor-API strategy such as
  ``SendGridRotation``: mint at the vendor, stage the new value through the
  store, then delete the superseded managed credentials once the window closes.
- **A human** → ``HitlRotation``, for vendors whose API cannot mint (GitHub
  classic PATs, OpenAI, Anthropic, Qwen). A single confirm supplies the new
  value; we stage it and notify to revoke the old one. ADR-080's overlap window
  means even a delayed human step widens the window rather than outaging.

Vendor strategies never inline their own admin credential — the caller wires
the vendor client from a credential resolved through the ``SecretStore`` seam
(ADR-095 §Consequences).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..providers.protocol import SecretRef
from .strategy import RotationOutcome, RotationPolicy

_log = logging.getLogger(__name__)


class RotationError(RuntimeError):
    """A strategy could not complete a rotation (bad input, vendor refusal)."""


def _window(now: float, policy: RotationPolicy) -> tuple[float | None, float]:
    """(old_valid_until, revoke_at) for a rotation at ``now`` under ``policy``."""
    overlap = policy.overlap_seconds
    if overlap:
        return now + overlap, now + overlap
    return None, now


# --- provider-native --------------------------------------------------------


class ProviderNativeRotation:
    """The backend rotates itself; we only trigger it (ADR-080's model)."""

    kind = "provider-native"

    def perform(
        self, ref: SecretRef, store: Any, *, now: float, policy: RotationPolicy
    ) -> RotationOutcome:
        store.rotate(ref)  # backend mints + stages + owns the overlap
        old_valid_until, revoke_at = _window(now, policy)
        return RotationOutcome(
            ref=ref,
            strategy=self.kind,
            rotated_at=now,
            new_version=None,
            old_valid_until=old_valid_until,
            revoke_at=revoke_at,
            forced=False,
        )

    def revoke_previous(
        self, ref: SecretRef, store: Any, outcome: RotationOutcome
    ) -> None:
        # The backend ages out the prior version (AWSPREVIOUS, etc.). Nothing
        # for us to revoke.
        return None


# --- HITL --------------------------------------------------------------------


class HitlRotation:
    """A human supplies the new value; we stage it and flag the old for revoke.

    ``value_provider(ref) -> bytes`` yields the human-supplied new credential
    (from the CLI's ``--value``/prompt, or a confirm callback). ``notifier``
    receives a message telling the operator to revoke the superseded credential
    at the vendor console.
    """

    kind = "hitl"

    def __init__(
        self,
        *,
        value_provider: Callable[[SecretRef], bytes],
        notifier: Callable[[str], None] | None = None,
    ) -> None:
        self._value_provider = value_provider
        self._notifier = notifier or (lambda _msg: None)

    def perform(
        self, ref: SecretRef, store: Any, *, now: float, policy: RotationPolicy
    ) -> RotationOutcome:
        new = self._value_provider(ref)
        if not new:
            raise RotationError(
                f"HITL rotation of {ref} needs a human-supplied new value; "
                "none was provided"
            )
        store.put(ref, new)
        old_valid_until, revoke_at = _window(now, policy)
        return RotationOutcome(
            ref=ref,
            strategy=self.kind,
            rotated_at=now,
            new_version=None,
            old_valid_until=old_valid_until,
            revoke_at=revoke_at,
            forced=False,
        )

    def revoke_previous(
        self, ref: SecretRef, store: Any, outcome: RotationOutcome
    ) -> None:
        self._notifier(
            f"Revoke the previous credential for {ref} at the vendor console — "
            "it has been superseded and its overlap window has closed."
        )


# --- vendor-API: SendGrid ---------------------------------------------------


class SendGridRotation:
    """Mint/revoke SendGrid API keys via its v3 API.

    ``http`` is a minimal client exposing ``post(path, body)->dict``,
    ``get(path)->dict``, ``delete(path)->None`` against the SendGrid base URL,
    already carrying the vendor admin credential (itself resolved through the
    ``SecretStore`` seam — never inlined here). ``key_name`` is the managed
    name every key this strategy mints is tagged with, so ``revoke_previous``
    can find *our* superseded keys without touching anyone else's.
    """

    kind = "sendgrid"

    def __init__(
        self, *, http: Any, key_name: str, scopes: list[str] | None = None
    ) -> None:
        self._http = http
        self._key_name = key_name
        self._scopes = scopes or ["mail.send"]

    def perform(
        self, ref: SecretRef, store: Any, *, now: float, policy: RotationPolicy
    ) -> RotationOutcome:
        resp = self._http.post(
            "/v3/api_keys", {"name": self._key_name, "scopes": self._scopes}
        )
        new_id = resp.get("api_key_id")
        new_secret = resp.get("api_key")
        if not new_id or not new_secret:
            raise RotationError(
                f"SendGrid did not return a usable api_key for {ref}: {resp!r}"
            )
        store.put(ref, new_secret.encode("utf-8"))
        old_valid_until, revoke_at = _window(now, policy)
        return RotationOutcome(
            ref=ref,
            strategy=self.kind,
            rotated_at=now,
            new_version=None,
            old_valid_until=old_valid_until,
            revoke_at=revoke_at,
            forced=False,
            new_handle=new_id,
        )

    def revoke_previous(
        self, ref: SecretRef, store: Any, outcome: RotationOutcome
    ) -> None:
        # Delete every key under OUR managed name except the one we just minted.
        # Keys under any other name (someone else's) are never touched.
        listed = self._http.get("/v3/api_keys").get("result", [])
        for key in listed:
            kid = key.get("api_key_id")
            if key.get("name") == self._key_name and kid != outcome.new_handle:
                self._http.delete(f"/v3/api_keys/{kid}")


__all__ = [
    "RotationError",
    "ProviderNativeRotation",
    "HitlRotation",
    "SendGridRotation",
]
