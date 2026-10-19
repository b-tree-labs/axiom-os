# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``ServiceTests`` — base conformance for service capabilities (AEOS §4.4)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

REQUIRED_SERVICE_METHODS = ("start", "stop", "status", "health_check")

VALID_DEPLOYMENT_PROFILES = {"edge", "workstation", "server", "platform"}


class ServiceTests:
    """Conformance for an AEOS ``service`` capability."""

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def service_class(self) -> type:
        """Return the service class under test (override)."""
        raise NotImplementedError("subclasses of ServiceTests must override service_class")

    @pytest.fixture
    def service_manifest_block(self) -> dict[str, Any] | None:
        return None

    # ---- Capability properties -----------------------------------------

    @property
    def deployment_profile(self) -> str:
        return "workstation"

    # ---- Standard tests -------------------------------------------------

    def test_service_class_exists(self, service_class: type) -> None:
        assert inspect.isclass(service_class), "service_class fixture must return a class"

    def test_service_has_required_interface(self, service_class: type) -> None:
        missing = [m for m in REQUIRED_SERVICE_METHODS if not hasattr(service_class, m)]
        assert not missing, (
            f"{service_class.__name__} is missing required service methods: "
            f"{missing} (AEOS §4.4 requires start/stop/status/health_check)"
        )

    def test_service_methods_are_callable(self, service_class: type) -> None:
        non_callable = [
            m
            for m in REQUIRED_SERVICE_METHODS
            if hasattr(service_class, m) and not callable(getattr(service_class, m))
        ]
        assert not non_callable, f"these service methods are not callable: {non_callable}"

    def test_deployment_profile_is_valid(self) -> None:
        assert self.deployment_profile in VALID_DEPLOYMENT_PROFILES, (
            f"deployment_profile {self.deployment_profile!r} is not one of "
            f"{sorted(VALID_DEPLOYMENT_PROFILES)}"
        )

    def test_manifest_deployment_profile_matches(
        self, service_manifest_block: dict[str, Any] | None
    ) -> None:
        if service_manifest_block is None:
            pytest.skip("service_manifest_block not provided")
        declared = service_manifest_block.get("deployment_profile")
        if declared is None:
            pytest.skip("manifest does not declare deployment_profile")
        assert declared == self.deployment_profile, (
            f"manifest says deployment_profile={declared!r} but subclass "
            f"declares {self.deployment_profile!r}"
        )


__all__ = ["REQUIRED_SERVICE_METHODS", "ServiceTests", "VALID_DEPLOYMENT_PROFILES"]
