# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axiom.governance` — shared substrate for the governance fabric (ADR-055).

This module is the load-bearing foundation for the four sibling primitives:

- ``axiom.extensions.builtins.authz`` (GUARD) — consumes ``ActionEnvelope``,
  returns ``Verdict``.
- ``axiom.extensions.builtins.vault`` (KEEP) — issues / verifies
  ``CapabilityToken``s, owns ``outbound_call``.
- ``axiom.extensions.builtins.notifications`` (HERALD) — sends + receives;
  every send carries an envelope.
- ``axiom.extensions.builtins.schedule`` (PULSE) — registers cadences; every
  fire constructs an envelope.

All four primitives consume the same shape. See spec-governance-fabric for
the build-ready substrate.
"""

from __future__ import annotations

from axiom.governance.capability import CapabilityToken
from axiom.governance.classification import (
    Classification,
    classification_lte,
)
from axiom.governance.controlled_code import (
    CODE_SCHEME,
    AccessRule,
    ConflictingDeclaration,
    ControlBasis,
    ControlFinding,
    ControlledCodeEntry,
    ControlledCodeError,
    ControlledCodeRegistry,
    ControlStanding,
    ControlStatus,
    Declarations,
    ManifestError,
    UndeclaredArtifact,
    code_ref,
    load_manifest,
)
from axiom.governance.envelope import ActionEnvelope
from axiom.governance.intent import (
    REGISTERED_INTENTS,
    ActionIntent,
    IntentPattern,
    register_intent,
)
from axiom.governance.provenance import SYNTHETIC, ProvenanceRef
from axiom.governance.resource import ResourcePattern, ResourceRef

# Easy onramp for extension authors — re-exported for convenience.
# See docs/working/extension-authn-quickstart.md for the 5-line pattern.
from axiom.governance.simple import (
    AuthnUnavailable,
    AuthorizationDenied,
    ExtensionAuthnContext,
    get_current_actor,
    set_current_actor,
    setup_extension,
)
from axiom.governance.actor import ActorContext, Assurance, resolve_actor
from axiom.governance.subject import ContextualTuple, SubjectContext
from axiom.governance.verdict import Challenge, Decision, NextAction, Verdict

__all__ = [
    "CODE_SCHEME",
    "AccessRule",
    "ActionEnvelope",
    "ActionIntent",
    "AuthnUnavailable",
    "AuthorizationDenied",
    "CapabilityToken",
    "Classification",
    "ConflictingDeclaration",
    "ContextualTuple",
    "ControlBasis",
    "ControlFinding",
    "ControlStanding",
    "ControlStatus",
    "ControlledCodeEntry",
    "ControlledCodeError",
    "ControlledCodeRegistry",
    "Declarations",
    "Decision",
    "ExtensionAuthnContext",
    "IntentPattern",
    "ManifestError",
    "NextAction",
    "ProvenanceRef",
    "REGISTERED_INTENTS",
    "ResourcePattern",
    "ResourceRef",
    "SYNTHETIC",
    "ActorContext",
    "Challenge",
    "Assurance",
    "SubjectContext",
    "UndeclaredArtifact",
    "resolve_actor",
    "Verdict",
    "classification_lte",
    "code_ref",
    "get_current_actor",
    "load_manifest",
    "register_intent",
    "set_current_actor",
    "setup_extension",
]
