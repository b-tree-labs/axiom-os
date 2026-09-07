# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for PublisherFactory — provider registration and instantiation."""

import pytest

from axiom.extensions.builtins.publishing.factory import PublisherFactory
from axiom.extensions.builtins.publishing.providers.base import StorageProvider


class TestFactoryRegistration:
    """Test the factory registration mechanism."""

    def test_register_and_create(self):
        """Providers can be registered and instantiated."""
        # The built-in providers are already registered via auto-import
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        provider = PublisherFactory.create("storage", "local", {"base_dir": "/tmp/test"})
        assert provider is not None
        assert isinstance(provider, StorageProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown storage provider"):
            PublisherFactory.create("storage", "nonexistent-provider")

    def test_unknown_category_raises(self):
        class FakeProvider:
            pass

        with pytest.raises(ValueError, match="Unknown provider category"):
            PublisherFactory.register("invalid_category", "fake", FakeProvider)

    def test_available_lists_registered(self):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        available = PublisherFactory.available("storage")
        assert "local" in available
        assert "onedrive" in available

    def test_available_all_categories(self):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        all_available = PublisherFactory.available()
        assert isinstance(all_available, dict)
        assert "generation" in all_available
        assert "storage" in all_available
        assert "feedback" in all_available
        assert "notification" in all_available
        assert "embedding" in all_available

    def test_generation_providers(self):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        available = PublisherFactory.available("generation")
        assert "pandoc-docx" in available

    def test_notification_providers(self):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        available = PublisherFactory.available("notification")
        assert "terminal" in available

    def test_feedback_providers(self):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        available = PublisherFactory.available("feedback")
        assert "docx-comments" in available


class TestFactoryInstantiation:
    """Test that factory creates providers with correct config."""

    def test_local_storage_with_config(self, tmp_path):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        provider = PublisherFactory.create(
            "storage", "local", {"base_dir": str(tmp_path / "output")}
        )
        assert provider.base_dir == tmp_path / "output"

    def test_local_storage_default_config(self):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        provider = PublisherFactory.create("storage", "local", {})
        assert provider.base_dir.name == "generated"

    def test_terminal_notification(self):
        import axiom.extensions.builtins.publishing.providers  # noqa: F401

        provider = PublisherFactory.create("notification", "terminal", {})
        assert provider is not None
