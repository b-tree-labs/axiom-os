# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SenderRegistry — canonical principal -> rendered channel surface (ADR-066).

Every notification HERALD sends must wear the right nameplate. The
principal grammar (ADR-020 ``@name:context``) is i18n-clean and carries
no possessive, version, or locale suffix. This module owns the *render
layer* that translates a principal into the locale-appropriate surface a
channel adapter consumes ("Ben's Rivet 0.6.0").

Three pieces, per ADR-066:

1. :class:`SenderIdentity` — the frozen contract (principal + display
   metadata). This is the join key ADR-067's inbound classifier resolves
   ``@mention`` against.
2. The ``[sender]`` manifest block, validated against
   ``schemas/sender.schema.json`` (ADR-065 config primitive).
3. :func:`render_for_channel` — the *only* place possessive forms exist;
   every adapter calls it and applies the resulting :class:`RenderedSender`.

PR-1 scope (this file): the registry, identity contract, ``en`` render
table, per-channel surface mapping, and schema validation. The
``ChannelAdapter.deliver_sync(sender=...)`` signature change that
consumes :class:`RenderedSender` lands in PR-2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "sender.schema.json"

# The possessive template table. The render layer is the only place
# possessive forms exist; locales are one-line additions plus tests.
# v1 ships ``en`` (ADR-066 §Render layer); ``ja``/``de`` land in PR-4.
_POSSESSIVE_TEMPLATES: dict[str, str] = {
    "en": "{owner}'s {display_name} {version}",
}
_DEFAULT_LOCALE = "en"


@dataclass(frozen=True)
class SenderIdentity:
    """The frozen sender contract (ADR-066).

    ``principal`` stays exactly the ADR-020 form (``@name:context``);
    ``version`` lives here in metadata, never embedded in the principal.
    ``owner_handle`` is resolved from the ``:context`` at registry-load
    time; its human display form is resolved at render time.
    """

    principal: str
    display_name: str
    version: str
    owner_handle: str
    avatar_uri: str | None = None
    from_address: str | None = None


@dataclass(frozen=True)
class RenderedSender:
    """A sender already shaped for one channel's vendor surface.

    Adapters consume the fields relevant to their surface and ignore the
    rest. The mapping table is owned here, not by each adapter, so two
    adapters never disagree on what "Ben's Rivet" looks like.

    | Channel    | Fields consumed                       |
    |------------|---------------------------------------|
    | Slack      | ``username`` + ``icon_url``           |
    | Mattermost | ``username`` + ``icon_url``           |
    | Teams      | ``display`` (Adaptive Card from.name) + ``icon_url`` |
    | Twilio SMS | ``body_prefix``                       |
    | Email      | ``display`` (From name) + ``from_address`` |
    | Inbox      | ``display`` + ``sender_principal``    |
    """

    channel: str
    display: str
    sender_principal: str
    username: str | None = None
    icon_url: str | None = None
    body_prefix: str | None = None
    from_address: str | None = None


def _owner_display_from_handle(owner_handle: str) -> str:
    """Deterministic fallback owner display form.

    Production passes the federation-peer-registry human name explicitly;
    when absent we derive a reasonable display from the handle:
    ``@ben.booth`` -> ``Ben``.
    """

    local = owner_handle.lstrip("@").split(":", 1)[0]
    first = local.split(".", 1)[0]
    return first[:1].upper() + first[1:] if first else owner_handle


def render_for_channel(
    identity: SenderIdentity,
    channel: str,
    locale: str = _DEFAULT_LOCALE,
    owner_display: str | None = None,
) -> RenderedSender:
    """Produce the channel-specific surface for ``identity``.

    ``owner_display`` is the federation-resolved human name; when omitted
    it derives deterministically from ``owner_handle``. Unknown locales
    fall back to ``en``.
    """

    template = _POSSESSIVE_TEMPLATES.get(locale, _POSSESSIVE_TEMPLATES[_DEFAULT_LOCALE])
    if owner_display:
        owner = owner_display
    elif identity.owner_handle and not identity.owner_handle.startswith("@"):
        # A resolved owner display ("Ben", "ben-mbp", "Alice") is used
        # verbatim; only a raw "@handle" is munged for a fallback display.
        owner = identity.owner_handle
    else:
        owner = _owner_display_from_handle(identity.owner_handle)
    display = template.format(
        owner=owner,
        display_name=identity.display_name,
        version=identity.version,
    )

    if channel in ("slack", "mattermost"):
        return RenderedSender(
            channel=channel,
            display=display,
            sender_principal=identity.principal,
            username=display,
            icon_url=identity.avatar_uri,
        )
    if channel == "teams":
        return RenderedSender(
            channel=channel,
            display=display,
            sender_principal=identity.principal,
            icon_url=identity.avatar_uri,
        )
    if channel == "twilio_sms":
        # SMS has no rich From; the possessive (sans version) prefixes the body.
        prefix = f"{owner}'s {identity.display_name}"
        return RenderedSender(
            channel=channel,
            display=display,
            sender_principal=identity.principal,
            body_prefix=f"[{prefix}]",
        )
    if channel == "email":
        return RenderedSender(
            channel=channel,
            display=display,
            sender_principal=identity.principal,
            from_address=identity.from_address,
        )
    # inbox + any future surface: carry the principal + display on the receipt.
    return RenderedSender(
        channel=channel,
        display=display,
        sender_principal=identity.principal,
    )


class SenderRegistry:
    """Process-level registry of :class:`SenderIdentity` by principal."""

    _schema_cache: dict[str, Any] | None = None

    def __init__(self) -> None:
        self._identities: dict[str, SenderIdentity] = {}

    # -- registration --------------------------------------------------- #
    def register(self, identity: SenderIdentity, *, replace: bool = False) -> None:
        if identity.principal in self._identities and not replace:
            raise ValueError(
                f"sender identity {identity.principal!r} already registered; "
                "pass replace=True to override"
            )
        self._identities[identity.principal] = identity

    def get(self, principal: str) -> SenderIdentity:
        return self._identities[principal]

    def render(
        self,
        principal: str,
        channel: str,
        locale: str = _DEFAULT_LOCALE,
        owner_display: str | None = None,
    ) -> RenderedSender:
        return render_for_channel(
            self.get(principal), channel, locale=locale, owner_display=owner_display
        )

    # -- manifest -> identity ------------------------------------------- #
    @classmethod
    def _schema(cls) -> dict[str, Any]:
        if cls._schema_cache is None:
            cls._schema_cache = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        return cls._schema_cache

    @classmethod
    def identity_from_manifest(
        cls,
        *,
        principal: str,
        version: str,
        owner_handle: str,
        sender_block: dict[str, Any],
    ) -> SenderIdentity:
        """Validate a ``[sender]`` manifest block and build an identity.

        Raises :class:`ValueError` on any schema violation (missing
        ``display_name``, unknown key, bad type) so a malformed manifest
        fails closed rather than registering a half-formed nameplate.
        """

        try:
            import jsonschema

            jsonschema.validate(instance=sender_block, schema=cls._schema())
        except ImportError:  # pragma: no cover - jsonschema is a hard dep
            # Minimal fallback validation if jsonschema is unavailable.
            if "display_name" not in sender_block:
                raise ValueError("[sender] block missing required 'display_name'")
            allowed = {"display_name", "avatar_uri", "from_address"}
            unknown = set(sender_block) - allowed
            if unknown:
                raise ValueError(f"[sender] block has unknown keys: {sorted(unknown)}")
        except Exception as exc:  # jsonschema.ValidationError, etc.
            raise ValueError(f"invalid [sender] block for {principal}: {exc}") from exc

        return SenderIdentity(
            principal=principal,
            display_name=sender_block["display_name"],
            version=version,
            owner_handle=owner_handle,
            avatar_uri=sender_block.get("avatar_uri"),
            from_address=sender_block.get("from_address"),
        )


__all__ = [
    "SenderIdentity",
    "RenderedSender",
    "SenderRegistry",
    "render_for_channel",
]
