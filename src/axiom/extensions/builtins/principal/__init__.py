# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axiom.principal`` — DESK: the human a harness works for.

P0 of ``docs/prd-axiom-principal-comms.md``: a principal record, a setup
interview, a proven round trip, and deterministic topic → channel routing.

The load-bearing invariant across all of it: **a channel that has not delivered a
real message is not a channel.** Configuration is a claim; a receipt is evidence.
Routing consults ``verified_at``, so an unproven endpoint cannot be selected no
matter how correct it looks.

Later phases add mail transport, threading, and assisted drafting. Nothing here
sends on a person's behalf.
"""

from __future__ import annotations

from .interview import QUESTIONS, Question, apply_answers, next_question
from .models import (
    ChannelPreference,
    ContactEndpoint,
    EndpointHealth,
    PrincipalProfile,
    PrincipalStatus,
)
from .routing import Decision, route
from .verify import VerificationOutcome, verify_endpoints

__all__ = [
    "QUESTIONS",
    "ChannelPreference",
    "ContactEndpoint",
    "Decision",
    "EndpointHealth",
    "PrincipalProfile",
    "PrincipalStatus",
    "Question",
    "VerificationOutcome",
    "apply_answers",
    "next_question",
    "route",
    "verify_endpoints",
]
