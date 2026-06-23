# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""axiom-tests — AEOS-conformance test infrastructure.

This package provides pytest base classes and fixtures used by every
AEOS-conformant extension to verify that it meets the AEOS 0.1 standard.

Public surface exposes:

- Unit-test base classes at :mod:`axiom_tests.unit_tests`
- Integration-test base classes at :mod:`axiom_tests.integration_tests`
- Reusable fixtures via the :mod:`axiom_tests.plugin` ``pytest11`` plugin
- The AEOS JSON Schema under :mod:`axiom_tests.schemas`

See ``docs/specs/spec-aeos-0.1.md §8`` for the specification.
"""

from axiom_tests._manifest import (
    AEOS_SCHEMA_VERSION,
    load_manifest,
    load_schema,
    validate_manifest,
)
from axiom_tests._version import __version__

__all__ = [
    "AEOS_SCHEMA_VERSION",
    "__version__",
    "load_manifest",
    "load_schema",
    "validate_manifest",
]
