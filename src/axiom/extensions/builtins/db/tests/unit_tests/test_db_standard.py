# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Standard AEOS conformance tests for the db extension."""

from __future__ import annotations

from pathlib import Path

import pytest
from axiom_tests.unit_tests import ExtensionStandardTests


class TestUdbStandard(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self) -> Path:
        return Path(__file__).parent.parent.parent / "axiom-extension.toml"
