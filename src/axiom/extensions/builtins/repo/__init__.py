# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Multi-source repo sensing — provider-based repository activity export.

Providers are auto-imported so they register themselves when the package
is imported.
"""

# Import providers for self-registration
import axiom.extensions.builtins.repo.providers  # noqa: F401
