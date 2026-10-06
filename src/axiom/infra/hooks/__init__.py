# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Platform hooks v1 — the synchronous interceptor primitive.

Public API:

    from axiom.infra.hooks import (
        HookBus, HookContext, HookResult, HookSpec, FailMode,
        allow, allow_modified, deny, request_approval,
        HookDenied, ApprovalRequired,
        ManifestPriorityStrategy, TrustWeightedStrategy, PriorityStrategy,
        get_default_hookbus, set_default_hookbus,
    )

Discovery + routing live in `axiom.infra.hooks.registry`.
TypedDict payload schemas live in `axiom.infra.hooks.event_schemas`.

See ``docs/specs/spec-hooks.md`` for the full design.
"""

from __future__ import annotations

from axiom.infra.hooks.hookbus import (
    HookBus,
    get_default_hookbus,
    set_default_hookbus,
)
from axiom.infra.hooks.priority import (
    ManifestPriorityStrategy,
    PriorityStrategy,
    TrustWeightedStrategy,
)
from axiom.infra.hooks.types import (
    ApprovalRequired,
    FailMode,
    HookContext,
    HookDenied,
    HookEntry,
    HookResult,
    HookSpec,
    allow,
    allow_modified,
    deny,
    request_approval,
)

__all__ = [
    "ApprovalRequired",
    "FailMode",
    "HookBus",
    "HookContext",
    "HookDenied",
    "HookEntry",
    "HookResult",
    "HookSpec",
    "ManifestPriorityStrategy",
    "PriorityStrategy",
    "TrustWeightedStrategy",
    "allow",
    "allow_modified",
    "deny",
    "get_default_hookbus",
    "request_approval",
    "set_default_hookbus",
]
