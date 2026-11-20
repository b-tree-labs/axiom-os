# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LMS provider factory — instantiate the right LMS adapter from config.

Reads `lms.provider` from config and returns the appropriate LMSProvider
subclass. Follows the ADR-012 provider/factory pattern used throughout
Axiom (LLM providers, storage providers, trace providers, etc.).

Usage:
    from axiom.integrations.lms.factory import create_lms_provider

    provider = create_lms_provider({
        "provider": "canvas",
        "api_url": "https://canvas.university.edu",
        "api_token": "...",
    })
    roster = provider.get_roster("12345")
"""

from __future__ import annotations

from typing import Any

from .base import LMSProvider

# Registry of known LMS provider implementations.
# Extensions can register additional providers at runtime.
_PROVIDER_REGISTRY: dict[str, type] = {}


def register_lms_provider(name: str, cls: type) -> None:
    """Register an LMS provider class by name."""
    _PROVIDER_REGISTRY[name] = cls


def create_lms_provider(config: dict[str, Any]) -> LMSProvider:
    """Create an LMS provider from config.

    Config must include "provider" key (e.g. "canvas", "moodle").
    All other keys are passed to the provider constructor.
    """
    provider_name = config.get("provider", "").lower()

    # Lazy-register built-in providers on first call
    if not _PROVIDER_REGISTRY:
        _register_builtins()

    cls = _PROVIDER_REGISTRY.get(provider_name)
    if cls is None:
        available = ", ".join(sorted(_PROVIDER_REGISTRY.keys())) or "none"
        raise ValueError(
            f"Unknown LMS provider: '{provider_name}'. "
            f"Available: {available}. "
            "Check your config or install the provider extension."
        )

    return cls(config)


def _register_builtins() -> None:
    """Lazy-register built-in LMS providers."""
    try:
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        register_lms_provider("canvas", CanvasLMSProvider)
    except ImportError:
        pass  # Canvas adapter not installed; that's OK
