# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.rotation`` — the autorotation layer above SecretStore (ADR-003 D3).

Rotation splits from storage: a ``SecretStore`` holds credentials and (for
some backends) rotates them natively; this package adds the vendor-API
rotation the SaaS-key case needs — mint at the vendor, stage the new
version, keep the old valid through an overlap window, then revoke it.

Public surface::

    from axiom.extensions.builtins.secrets.rotation import (
        RotationEngine, RotationPolicy, RotationRegistry,
    )

    engine = RotationEngine(resolver=..., store_for=..., clock=...)
    engine.rotate(ref, policy=RotationPolicy(cadence_seconds=None,
                                             overlap_seconds=3600),
                  force=True)          # the leaked-key closer

Concrete per-vendor strategies (SendGrid, GitHub PAT, OpenAI, …) and the
PULSE-driven schedule land as follow-on bricks on this contract.
"""

from __future__ import annotations

from .engine import NotDue, RotationEngine
from .registry import RotationRegistry, default_registry
from .strategy import RotationOutcome, RotationPolicy, RotationStrategy

__all__ = [
    "NotDue",
    "RotationEngine",
    "RotationOutcome",
    "RotationPolicy",
    "RotationRegistry",
    "RotationStrategy",
    "default_registry",
]
