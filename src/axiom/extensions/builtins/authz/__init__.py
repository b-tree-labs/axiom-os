# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GUARD — unified authorization decision site.

Per ADR-055 + prd-axiom-authz: every action that crosses an authorization
boundary on the platform calls ``decide(envelope) → Verdict`` exactly
once. The verdict's ``next_action_for_caller`` is the caller-side
contract; callers never inspect the raw decision.

Quick consumer integration::

    from axiom.extensions.builtins.authz import decide, DecideContext
    from axiom.governance import ActionEnvelope, NextAction
    from axiom.infra.db import session_for

    ctx = DecideContext(session_factory=lambda: session_for("authz"))

    def my_action(envelope: ActionEnvelope) -> None:
        verdict = decide(envelope, ctx)
        if verdict.next_action_for_caller is not NextAction.PROCEED:
            return  # caller aborts or enqueues a proposal
        # do the action
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.authz.decide import (
    DecideContext,
    decide,
    decide_with_default_context,
)
from axiom.extensions.builtins.authz.rules import (
    CombinedDisposition,
    Disposition,
    Rule,
    RuleEngine,
)

# GUARD persona path — consumed by the AEOS extension manifest's
# [[extension.provides]] agent block.
guard_persona_path = str(
    Path(__file__).parent / "agents" / "guard" / "persona.md"
)


__all__ = [
    "CombinedDisposition",
    "DecideContext",
    "Disposition",
    "Rule",
    "RuleEngine",
    "decide",
    "decide_with_default_context",
    "guard_persona_path",
]
