# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The model a product names for its people, used unless they choose otherwise.

A product often knows where its people's questions should go: the deployment
an install belongs to serves chat over its own data, and a small model on the
person's own machine has none of it. ``BrandingConfig.chat_provider_fn`` says
so; this module offers that provider ahead of the configured ones.

It is a default and nothing more. ``--local`` keeps the machine's own models,
``--provider`` and the ``routing.prefer_provider`` setting win as they always
did, and a provider whose key is not in the vault is not offered at all,
because every question would then fail with 401.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import fields
from typing import Any

log = logging.getLogger(__name__)


def apply_product_default(gateway: Any, *, local: bool, brand: Any | None = None) -> str | None:
    """Offer the product's chat provider first. Returns its name, or None. Never raises."""
    if local or getattr(gateway, "_provider_override", None):
        return None
    if brand is None:
        from axiom.infra.branding import get_branding

        brand = get_branding()
    hook = getattr(brand, "chat_provider_fn", None)
    if not callable(hook):
        return None
    try:
        spec = hook()
    except Exception as exc:  # noqa: BLE001 - a product hook costs only its default
        log.warning("chat: the product's default model could not be read: %s", exc)
        return None
    if not spec:
        return None

    from axiom.llm.gateway import LLMProvider

    known = {f.name for f in fields(LLMProvider) if f.init}
    given = {k: v for k, v in dict(spec).items() if k in known}
    # Stable across sessions without a config entry to persist it in, so the
    # audit record names the same provider every time (and nothing asks the
    # person to add a uid to a file this provider never came from).
    given.setdefault(
        "uid", str(uuid.uuid5(uuid.NAMESPACE_URL, f"{given.get('endpoint', '')}#{given.get('model', '')}"))
    )
    try:
        provider = LLMProvider(**{"use_for": ["chat", "fallback"], **given})
    except TypeError as exc:
        log.warning("chat: the product's default model is incomplete: %s", exc)
        return None
    if not provider.is_usable:
        log.info("chat: %s has no key in the vault; using this machine's models", provider.name)
        return None
    gateway.add_provider(provider, first=True)
    return provider.name


__all__ = ["apply_product_default"]
