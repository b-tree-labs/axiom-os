# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Instruments: the ``instrument`` noun and the registry of instrument kinds (ADR-256, spec-instrument).

A sensor is the physical device; an instrument is the part on the node that turns
its output into quantified data (a frame, a reading with a unit); an instrument kind
is defined by what it quantifies into. This extension owns the noun and the kind
registry and nothing else: capture, frames, triggers, journals and connector kinds
belong to a kind. A kind registers by ``[[instrument_kinds]]`` or the
``axiom.instrument_kinds`` entry point, behind the portfolio gate.

Public surface::

    from axiom.extensions.builtins.instrument import (
        InstrumentKind, InstrumentRow, KindStatus, HEALTH_STATES,   # what a kind implements
        KindCatalog, InstrumentKindError, UnknownKind, catalog,     # what is registered
        register_kind, ENTRY_POINT_GROUP, MANIFEST_BLOCK, RESERVED_NAMES,
    )
"""

from __future__ import annotations

from .kinds import HEALTH_STATES, InstrumentKind, InstrumentRow, KindStatus
from .registry import (
    ENTRY_POINT_GROUP,
    MANIFEST_BLOCK,
    RESERVED_NAMES,
    InstrumentKindError,
    KindCatalog,
    UnknownKind,
    catalog,
    register_kind,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "HEALTH_STATES",
    "InstrumentKind",
    "InstrumentKindError",
    "InstrumentRow",
    "KindCatalog",
    "KindStatus",
    "MANIFEST_BLOCK",
    "RESERVED_NAMES",
    "UnknownKind",
    "catalog",
    "register_kind",
]
