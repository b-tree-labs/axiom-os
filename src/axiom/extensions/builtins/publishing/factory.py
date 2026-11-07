# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PublisherFactory — central provider registry with self-registration.

Providers register themselves on import:

    from axiom.extensions.builtins.publishing.factory import PublisherFactory
    PublisherFactory.register("storage", "local", LocalStorageProvider)

The factory instantiates providers from YAML config:

    provider = PublisherFactory.create("storage", "local", config_dict)
"""

from __future__ import annotations

from typing import Any


class PublisherFactory:
    """Central factory that instantiates providers from configuration."""

    _registries: dict[str, dict[str, type]] = {
        "generation": {},
        "storage": {},
        "feedback": {},
        "notification": {},
        "embedding": {},
    }

    @classmethod
    def register(cls, category: str, name: str, provider_cls: type) -> None:
        """Register a provider class under a category and name.

        Args:
            category: One of generation, storage, feedback, notification, embedding
            name: Config-friendly name (e.g., "pandoc-docx", "onedrive", "local")
            provider_cls: The provider class to register
        """
        if category not in cls._registries:
            raise ValueError(f"Unknown provider category: {category}")
        cls._registries[category][name] = provider_cls

    @classmethod
    def create(cls, category: str, name: str, config: dict[str, Any] | None = None) -> Any:
        """Instantiate a provider by category and name.

        Args:
            category: Provider category
            name: Provider name (as registered)
            config: Provider-specific configuration dict
        Returns:
            Instantiated provider
        Raises:
            ValueError: If provider not found
        """
        registry = cls._registries.get(category, {})
        if name not in registry:
            available = list(registry.keys())
            raise ValueError(
                f"Unknown {category} provider: '{name}'. "
                f"Available: {available}"
            )
        return registry[name](config or {})

    @classmethod
    def available(cls, category: str | None = None) -> dict[str, list[str]] | list[str]:
        """List available providers.

        Args:
            category: If specified, return list of names for that category.
                      If None, return dict of all categories → names.
        """
        if category:
            return list(cls._registries.get(category, {}).keys())
        return {cat: list(names.keys()) for cat, names in cls._registries.items()}

    @classmethod
    def reset(cls) -> None:
        """Reset all registries (for testing)."""
        for cat in cls._registries:
            cls._registries[cat] = {}
