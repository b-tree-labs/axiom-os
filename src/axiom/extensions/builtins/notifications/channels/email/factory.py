# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""EmailProvider registry + factory.

Mirrors the outer ``ChannelAdapterRegistry`` shape: a registry maps the
provider name to its build function. ``detect_email_provider`` resolves
a config dict to a built provider, dispatching on the most-specific
config key present.

Resolution order (most-specific first):

1. ``provider`` explicit override — caller asserts which backend.
2. Vendor-API keys (``resend_api_key``, ``sendgrid_api_key``,
   ``postmark_server_token``, ``mailgun_api_key``, ``ses_access_key_id``,
   ``microsoft365_client_id``, ``gmail_client_id``).
3. ``smtp_host`` — generic SMTP fallback.

Returning ``None`` rather than raising lets the caller surface a clear
configuration error at the HERALD send site rather than at adapter
build time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailProvider,
)

# Module-level registry. Each provider module registers itself at import
# time via ``register_email_provider`` so adding a vendor = a new file +
# import, with no platform-code change.
_BUILDERS: dict[str, Callable[[dict[str, Any]], EmailProvider]] = {}


def register_email_provider(
    name: str,
    builder: Callable[[dict[str, Any]], EmailProvider],
    *,
    replace: bool = False,
) -> None:
    """Register a vendor's build function under its name."""
    if name in _BUILDERS and not replace:
        raise ValueError(
            f"email provider {name!r} already registered; "
            "pass replace=True to override"
        )
    _BUILDERS[name] = builder


def email_provider_names() -> list[str]:
    """Sorted list of currently-registered vendor names."""
    return sorted(_BUILDERS)


def get_email_provider(name: str) -> Callable[[dict[str, Any]], EmailProvider]:
    """Look up a builder by exact name; raises ``KeyError`` if unknown."""
    if name not in _BUILDERS:
        raise KeyError(
            f"no email provider registered for {name!r}; "
            f"known: {email_provider_names()}"
        )
    return _BUILDERS[name]


# Config-key → provider-name discovery rules. The first match wins.
# Adding a new provider = registering it in ``_BUILDERS`` plus appending
# its discovery key here.
_CONFIG_DISCOVERY_RULES: tuple[tuple[str, str], ...] = (
    # vendor-API providers, most specific
    ("resend_api_key", "resend"),
    ("sendgrid_api_key", "sendgrid"),
    ("postmark_server_token", "postmark"),
    ("mailgun_api_key", "mailgun"),
    ("ses_access_key_id", "ses"),
    ("microsoft365_client_id", "microsoft365"),
    ("gmail_client_id", "gmail"),
    # generic SMTP fallback — last
    ("smtp_host", "smtp"),
)


def detect_email_provider(config: dict[str, Any]) -> EmailProvider | None:
    """Resolve config → a built ``EmailProvider``, or None when unmatched.

    The detection rule is intentionally cheap + explicit. Operators see
    "I set ``resend_api_key`` and I got the Resend provider" — no magic.
    """
    # Explicit override.
    explicit = config.get("provider")
    if explicit:
        builder = get_email_provider(explicit)
        return builder(config)

    # Config-key discovery.
    for cfg_key, provider_name in _CONFIG_DISCOVERY_RULES:
        if config.get(cfg_key):
            if provider_name in _BUILDERS:
                return _BUILDERS[provider_name](config)
            # Provider keyed in config but not registered — explicit gap.
            raise KeyError(
                f"config implies email provider {provider_name!r} but it "
                "is not registered; import its module to register it"
            )

    return None


__all__ = [
    "detect_email_provider",
    "email_provider_names",
    "get_email_provider",
    "register_email_provider",
]
