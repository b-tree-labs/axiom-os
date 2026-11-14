# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flag declarations moved to ``features.flag_registry`` (ADR-254); this path stays importable
through the 0.70 line and is removed at 0.71.0.

Everything here is the same object as there: ``set_registry`` pins the one
registry the switchboard reads.
"""

from __future__ import annotations

from axiom.extensions.builtins.features.flag_registry import (
    ENTRY_POINT_GROUP,
    ENV_PREFIX,
    KEY_RE,
    OFF,
    ON,
    SOON,
    VALUES,
    FlagDef,
    get_environment,
    load_registry,
    normalize_value,
    parse_manifest_flags,
    registry,
    set_registry,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "ENV_PREFIX",
    "KEY_RE",
    "OFF",
    "ON",
    "SOON",
    "VALUES",
    "FlagDef",
    "get_environment",
    "load_registry",
    "normalize_value",
    "parse_manifest_flags",
    "registry",
    "set_registry",
]
