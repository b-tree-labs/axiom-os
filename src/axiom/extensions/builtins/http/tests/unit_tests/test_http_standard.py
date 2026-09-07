# Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed.
# Copyright (c) 2026 B-Tree Labs
"""Standard AEOS conformance tests for the http extension."""

from __future__ import annotations

from pathlib import Path

import pytest
from axiom_tests.unit_tests import ExtensionStandardTests


class TestHttpStandard(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self) -> Path:
        return Path(__file__).parent.parent.parent / "axiom-extension.toml"
