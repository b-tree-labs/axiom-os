# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Test orchestration.

Coordinates multiple test types:
- Unit tests
- Integration tests
- Database migration tests
- Linting (ruff)
- Type checking (optional)

Use via CLI:
    axi test              # Default: quick local tests
    axi test --full       # Comprehensive tests
    axi test --pr         # Tests required for PR approval
    axi test --release    # Full release candidate validation
"""
