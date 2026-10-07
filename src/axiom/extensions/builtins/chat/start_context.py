# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What a chat session is using, said once at the start.

The banner used to read ``<provider> (<model>)`` and nothing more. A person on
a laptop could not tell that the model was a small one running on that laptop,
what else the session was connected to, or how to change either — and a
default nobody was told about is a default nobody corrects.

The platform states the model, where it runs, and the switch. A product adds
what its users need to know at the same moment through
``BrandingConfig.chat_context_fn``; the platform prints those lines without
knowing what they are about.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _where(endpoint: str) -> str:
    host = (urlparse(endpoint or "").hostname or "").lower()
    if not host:
        return ""
    if host in _LOCAL_HOSTS:
        return "running on this machine"
    return f"served from {host}"


def start_context_lines(
    provider: Any | None,
    *,
    model_override: str | None = None,
    brand: Any | None = None,
) -> list[str]:
    """Plain-text lines for the top of a chat session. Never raises."""
    if brand is None:
        from axiom.infra.branding import get_branding

        brand = get_branding()
    cli = getattr(brand, "cli_name", "") or "axi"

    if provider is None:
        lines = [f"Model: No LLM configured. Run `{cli} config` to set one up."]
    else:
        model = model_override or getattr(provider, "model", "") or "?"
        name = getattr(provider, "name", "") or ""
        where = _where(getattr(provider, "endpoint", "") or "")
        parts = [f"Model: {model}" + (f" via {name}" if name else "")]
        if where:
            parts.append(where)
        parts.append("/model to switch")
        lines = [" · ".join(parts)]

    extra = getattr(brand, "chat_context_fn", None)
    if callable(extra):
        try:
            lines.extend(str(line) for line in (extra() or []) if str(line).strip())
        except Exception:  # noqa: BLE001 - a product's hook costs only its lines
            pass
    return lines


__all__ = ["start_context_lines"]
