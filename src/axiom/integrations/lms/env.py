# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Env-var driven LMS configuration — Prague deployment (T1b).

Mirror of the tracing env module — a node operator enables the LMS
integration by setting env vars; the classroom doesn't edit code or
config files.

Precedence:

1. ``AXIOM_LMS_PROVIDER`` — explicit provider name. ``none`` is the
   explicit opt-out (same semantics as ``prep lms-setup none``).
2. Auto-detect: if both ``AXIOM_CANVAS_API_URL`` and
   ``AXIOM_CANVAS_API_TOKEN`` are set, ``canvas`` is selected. This
   is the common case for a single-Canvas site that never touches
   the provider name.
3. Otherwise → ``None`` (no LMS configured; manual-roster flow).

Partial Canvas env vars (one set, one missing) → ``None`` silently.
Invalid provider names → ``None`` silently. Same defensive posture
as the tracing loader: a half-configured node never crashes the
chat path.
"""

from __future__ import annotations

import os
from typing import Any

from .base import LMSProvider
from .factory import create_lms_provider

LMS_ENV_VARS: tuple[str, ...] = (
    "AXIOM_LMS_PROVIDER",
    "AXIOM_LMS_NAME",
    "AXIOM_CANVAS_API_URL",
    "AXIOM_CANVAS_API_TOKEN",
)


_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"canvas", "none"})


def load_lms_config_from_env(
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Derive an LMS config dict from ``env`` (defaults to ``os.environ``).

    Returns a dict suitable for ``create_lms_provider``, or ``None``
    if no LMS should be configured. Never raises.
    """
    e = env if env is not None else os.environ

    explicit = e.get("AXIOM_LMS_PROVIDER", "").strip().lower()

    if explicit == "none":
        return None

    if explicit and explicit not in _SUPPORTED_PROVIDERS:
        # Unknown provider → treat as unconfigured (manual-roster path).
        return None

    # Either explicit=="canvas" or unset — try Canvas auto-detect.
    api_url = e.get("AXIOM_CANVAS_API_URL", "").strip()
    api_token = e.get("AXIOM_CANVAS_API_TOKEN", "").strip()

    if not api_url or not api_token:
        # Not fully configured for Canvas; if explicit=="canvas" was set
        # this is a misconfiguration, but we fall through to "not
        # configured" rather than raising, matching the tracing loader.
        return None

    return {
        "provider": "canvas",
        "name": e.get("AXIOM_LMS_NAME", "").strip() or "canvas-env",
        "api_url": api_url,
        "api_token": api_token,
    }


def build_lms_provider_from_env(
    env: dict[str, str] | None = None,
) -> LMSProvider | None:
    """Build an LMSProvider from the current env, or ``None`` if unset."""
    cfg = load_lms_config_from_env(env)
    if cfg is None:
        return None
    try:
        return create_lms_provider(cfg)
    except Exception:
        # Factory refused (missing adapter, bad config). Keep the chat
        # path running by falling through to no-LMS.
        return None
