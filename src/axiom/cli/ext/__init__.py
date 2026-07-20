# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext`` — AEOS extension-lifecycle CLI.

This package implements the Factory/Provider pattern described in
:ref:`spec-aeos-0.1 §11`. Every ``axi ext <verb>`` is an :class:`ExtCliProvider`
instance; built-in providers ship with Axiom, third-party overrides register
through the ``axiom.ext.cli.providers`` entry-point group.

Only the four Tier 1b verbs are implemented in this phase:

- ``init``     — scaffold a new AEOS-conformant compound extension
- ``lint``     — Bronze-level conformance report
- ``validate`` — deeper checks (entry-point resolution, standard tests, API)
- ``test``     — thin wrapper around pytest scoped to the extension

The remaining Tier 1+2 verbs (install, publish, sign, etc.) are stubbed in the
dispatcher for later phases.
"""

from axiom.cli.ext.provider import CliContext, ExtCliProvider
from axiom.cli.ext.registry import discover_providers

__all__ = ["CliContext", "ExtCliProvider", "discover_providers"]
