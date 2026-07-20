# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``manifest_validator`` fixture — reusable AEOS JSON Schema validator.

Tests that need to validate a parsed manifest can request this fixture and
call ``validator.iter_errors(manifest)`` directly, or pass it to
``axiom_tests.validate_manifest``.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from axiom_tests._manifest import build_validator


@pytest.fixture(scope="session")
def manifest_validator() -> Draft202012Validator:
    """Return the AEOS manifest validator, built once per test session."""
    return build_validator()


__all__ = ["manifest_validator"]
