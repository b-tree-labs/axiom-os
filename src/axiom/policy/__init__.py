# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom policy — NL policy broadcasting engine.

Design from `docs/working/natural-language-policy-broadcasting.md`:
authorized humans broadcast scoped, revocable directives to agent swarms
via natural language. AXI interprets; the engine scopes, applies,
expires.

Atomic unit: Directive. Lifecycle: broadcast → active → (expire | revoke).
Scope kinds: period, classroom, course, session (add more as needed).

Slice: Classroom Phase 2 — advanced feature, composes over
axiom.chat (addressing), axiom.classroom (Period), axiom.vega.identity
(principal handles).
"""

from __future__ import annotations

from axiom.policy.directive import Directive
from axiom.policy.engine import PolicyEngine, expand_targets

__all__ = ["Directive", "PolicyEngine", "expand_targets"]
