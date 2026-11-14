# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Flag resolution moved to ``features.resolver`` (ADR-254); this path stays importable
through the 0.70 line and is removed at 0.71.0."""

from __future__ import annotations

from axiom.extensions.builtins.features.resolver import (
    EXTENSION_OFF,
    FlagError,
    FlagService,
    OverrideReader,
    Resolution,
    env_override,
    set_switched_off_owners,
    switched_off_owners,
)

__all__ = [
    "EXTENSION_OFF",
    "FlagError",
    "FlagService",
    "OverrideReader",
    "Resolution",
    "env_override",
    "set_switched_off_owners",
    "switched_off_owners",
]
