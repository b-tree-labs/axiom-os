# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""IssueProvider ABC + Factory — files issues from self-healing signals.

Follows the same Factory/Provider pattern as PublisherFactory
(tools/publisher/factory.py). Providers self-register on import.

Usage:
    provider = IssueProviderFactory.create("gitlab", {"project": "..."})
    url = provider.create_issue("title", "body", labels=["self-heal"])
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class IssueProvider(ABC):
    """Files issues for self-healing signals."""

    @abstractmethod
    def find_existing(self, fingerprint: str) -> str | None:
        """Return issue URL if a matching open issue exists, else None."""

    @abstractmethod
    def create_issue(self, title: str, body: str, labels: list[str]) -> str:
        """Create an issue, return its URL."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if this provider is configured and usable."""


class IssueProviderFactory:
    """Central factory for issue providers with self-registration."""

    _providers: dict[str, type[IssueProvider]] = {}

    @classmethod
    def register(cls, name: str, provider_cls: type[IssueProvider]) -> None:
        """Register a provider class by name."""
        cls._providers[name] = provider_cls

    @classmethod
    def create(cls, name: str, config: dict[str, Any] | None = None) -> IssueProvider:
        """Instantiate a provider by name.

        Raises ValueError if the provider is not registered.
        """
        if name not in cls._providers:
            available = list(cls._providers.keys())
            raise ValueError(
                f"Unknown issue provider: '{name}'. Available: {available}"
            )
        return cls._providers[name](config or {})  # type: ignore[call-arg]

    @classmethod
    def available(cls) -> list[str]:
        """List registered provider names."""
        return list(cls._providers.keys())

    @classmethod
    def reset(cls) -> None:
        """Reset registry (for testing)."""
        cls._providers = {}
