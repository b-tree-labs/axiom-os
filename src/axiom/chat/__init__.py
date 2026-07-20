# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom Chat primitives.

Phase 2 Classroom: @agent addressing is the core chat primitive. Humans
mention agents by handle; resolution expands wildcards like @all-curios
against a period roster; unresolved mentions are dropped silently (never
errored — chat must remain forgiving).

This module is the foundation NL policy broadcasting, /invite, and
multi-agent research sessions all compose over.
"""

from __future__ import annotations

from axiom.chat.addressing import (
    AddressBook,
    MentionTarget,
    parse_mentions,
    resolve,
)

__all__ = ["AddressBook", "MentionTarget", "parse_mentions", "resolve"]
