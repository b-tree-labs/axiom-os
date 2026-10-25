# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Memory-domain exceptions.

Centralized so call sites raise + catch the same types regardless of
whether the failure originates in CompositionService, the EventStore,
the projection layer, or the migration helpers.
"""

from __future__ import annotations


class MemoryError(ValueError):
    """Base class for memory-domain errors.

    Subclasses ValueError so existing call sites that catch ValueError
    on memory writes continue to work; new code SHOULD catch the more
    specific subclasses.
    """


class AccountabilityError(MemoryError):
    """Raised when a memory write would persist a fragment without a
    valid ``accountable_human_id``.

    Per ADR-035 §D1: every memorable action MUST be bound to a named
    human. CompositionService rejects writes whose
    ``provenance.accountable_human_id`` is unset, empty, or a legacy
    sentinel before any persistence happens.
    """


class UnsupportedSchemaError(MemoryError):
    """Raised when a fragment dict has a schema_version newer than this
    Axiom can decode. Per ``working/memory-persistence-plan.md`` §3.

    Decoders are pinned per-version; future-version fragments fail
    closed with this error rather than silently mangling.
    """


__all__ = [
    "AccountabilityError",
    "MemoryError",
    "UnsupportedSchemaError",
]
