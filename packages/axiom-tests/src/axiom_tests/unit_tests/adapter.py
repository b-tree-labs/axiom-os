# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``AdapterTests`` — base conformance for adapter capabilities (AEOS §4.5)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

VALID_AUTH_METHODS = {
    "none",
    "api_token",
    "oauth2",
    "oidc",
    "saml",
    "mutual_tls",
    "basic_auth",
    "service_account",
    "piv",
    "incommon",
}


class AdapterTests:
    """Conformance for an AEOS ``adapter`` capability."""

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def adapter_class(self) -> type:
        """Return the adapter class under test (override)."""
        raise NotImplementedError("subclasses of AdapterTests must override adapter_class")

    @pytest.fixture
    def adapter_manifest_block(self) -> dict[str, Any] | None:
        return None

    # ---- Capability properties -----------------------------------------

    @property
    def auth_methods(self) -> tuple[str, ...]:
        return ()

    @property
    def capabilities(self) -> tuple[str, ...]:
        return ()

    # ---- Standard tests -------------------------------------------------

    def test_adapter_class_exists(self, adapter_class: type) -> None:
        assert inspect.isclass(adapter_class), "adapter_class fixture must return a class"

    def test_has_connection_interface(self, adapter_class: type) -> None:
        """Adapter exposes a Connection-like interface per AEOS §4.5.

        AEOS leaves the exact interface to the Axiom Connection framework;
        at minimum a ``connect`` or ``get_connection`` method is expected.
        """
        has_connect = any(
            hasattr(adapter_class, name)
            for name in ("connect", "get_connection", "open", "__enter__")
        )
        assert has_connect, (
            f"{adapter_class.__name__} must expose a Connection-like interface "
            "(connect / get_connection / open / __enter__) per AEOS §4.5"
        )

    def test_declared_auth_methods_are_valid(self) -> None:
        invalid = [m for m in self.auth_methods if m not in VALID_AUTH_METHODS]
        assert not invalid, f"unknown auth methods: {invalid} (valid: {sorted(VALID_AUTH_METHODS)})"

    def test_manifest_auth_methods_match(
        self, adapter_manifest_block: dict[str, Any] | None
    ) -> None:
        if adapter_manifest_block is None:
            pytest.skip("adapter_manifest_block not provided")
        declared = adapter_manifest_block.get("auth_methods")
        if declared is None:
            pytest.skip("manifest does not declare auth_methods")
        if not self.auth_methods:
            pytest.skip("subclass has not declared auth_methods")
        assert set(declared) == set(self.auth_methods), (
            f"manifest auth_methods {sorted(declared)!r} disagree with subclass "
            f"{sorted(self.auth_methods)!r}"
        )

    def test_manifest_capabilities_match(
        self, adapter_manifest_block: dict[str, Any] | None
    ) -> None:
        if adapter_manifest_block is None:
            pytest.skip("adapter_manifest_block not provided")
        declared = adapter_manifest_block.get("capabilities")
        if declared is None:
            pytest.skip("manifest does not declare capabilities")
        if not self.capabilities:
            pytest.skip("subclass has not declared capabilities")
        assert set(declared) == set(self.capabilities), (
            f"manifest capabilities {sorted(declared)!r} disagree with subclass "
            f"{sorted(self.capabilities)!r}"
        )


__all__ = ["AdapterTests", "VALID_AUTH_METHODS"]
