# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""graduation — shadow-mode outcome logging for internal classifiers.

Phase 1 wraps ONE internal classification call site (the LLM tier/routing
classifier) in shadow mode: every routing decision's *shape* is appended to
a local outcome log, so evidence accumulates for a later, evidence-gated
graduation of the rule. Nothing about live behavior changes, nothing leaves
the machine, and no classified content is ever recorded — decisions, never
data. See README.md for the explicit out-of-scope list.
"""

from .shadow import (
    load_outcome_records,
    observe_routing_decision,
    outcome_log_path,
    shadow_enabled,
)

__all__ = [
    "load_outcome_records",
    "observe_routing_decision",
    "outcome_log_path",
    "shadow_enabled",
]
