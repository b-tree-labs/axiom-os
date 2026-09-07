# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs.
# Apache-2.0 licensed.
"""Registry protocol checks: url_for is optional (SupportsUrlFor capability)."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.sources.contracts import (
    SupportsUrlFor,
)
from axiom.extensions.builtins.data_platform.sources.registry import (
    SourceKindRegistry,
)


class _UrlLessProvider:
    """Minimal provider WITHOUT url_for — registration must still succeed."""

    kind = "urlless"
    description = "kind whose documents have no shareable URLs"

    def add_register_args(self, subparser):  # pragma: no cover - shape only
        pass

    def params_from_args(self, args):  # pragma: no cover - shape only
        return {}

    def validate(self, config):
        return []

    def construct(self, config):  # pragma: no cover - shape only
        raise NotImplementedError

    def preflight(self, config):  # pragma: no cover - shape only
        raise NotImplementedError


def test_register_accepts_provider_without_url_for():
    """url_for is an optional capability, never required at registration."""
    reg = SourceKindRegistry()
    reg.register(_UrlLessProvider())
    provider = reg.get("urlless")
    assert getattr(provider, "url_for", None) is None


def test_supports_url_for_capability_check():
    class _WithUrl(_UrlLessProvider):
        kind = "urlful"

        def url_for(self, config, ref_id):
            return f"https://example.invalid/{ref_id}"

    assert isinstance(_WithUrl(), SupportsUrlFor)
    assert not isinstance(_UrlLessProvider(), SupportsUrlFor)
