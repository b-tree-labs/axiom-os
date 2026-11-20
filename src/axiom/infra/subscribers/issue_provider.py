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

from axiom.infra.connector_registry import ConnectorRegistry


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
    """Central factory for issue providers with self-registration.

    Backed by the shared ``ConnectorRegistry`` mechanism (ADR-110 §Decision-1) —
    a factory-registry, like directory and the document editors."""

    _reg: ConnectorRegistry = ConnectorRegistry("issue provider")

    @classmethod
    def register(cls, name: str, provider_cls: type[IssueProvider]) -> None:
        """Register a provider class by name (last registration wins)."""
        cls._reg.register(name, provider_cls, replace=True)

    @classmethod
    def create(cls, name: str, config: dict[str, Any] | None = None) -> IssueProvider:
        """Instantiate a provider by name.

        Raises ValueError if the provider is not registered.
        """
        try:
            provider_cls = cls._reg.get(name)
        except KeyError:
            raise ValueError(
                f"Unknown issue provider: '{name}'. Available: {cls.available()}"
            ) from None
        return provider_cls(config or {})  # type: ignore[call-arg]

    @classmethod
    def available(cls) -> list[str]:
        """List registered provider names."""
        return list(cls._reg.available())

    @classmethod
    def reset(cls) -> None:
        """Reset registry (for testing)."""
        cls._reg = ConnectorRegistry("issue provider")
