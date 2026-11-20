# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Standard AEOS conformance tests for the video extension."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from axiom_tests.unit_tests import ExtensionStandardTests
from axiom_tests.unit_tests.service import ServiceTests

MANIFEST = Path(__file__).parent.parent.parent / "axiom-extension.toml"


class TestVideoStandard(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self) -> Path:
        return MANIFEST


class TestCaptureServiceStandard(ServiceTests):
    """AEOS §4.4: the capture service answers start, stop, status and health_check."""

    @property
    def deployment_profile(self) -> str:
        return "edge"

    @pytest.fixture
    def service_class(self) -> type:
        from axiom.extensions.builtins.video.services.capture import CaptureService

        return CaptureService

    @pytest.fixture
    def service_manifest_block(self) -> dict:
        provides = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["extension"]["provides"]
        return next(p for p in provides if p["kind"] == "service")
