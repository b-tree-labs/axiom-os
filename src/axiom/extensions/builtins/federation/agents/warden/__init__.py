# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""WARDEN — Vega's federation-governance agent.

Verifier · Enforcer · Gatekeeper · Arbiter. WARDEN owns the federation
trust-boundary contracts: peer-state transitions, signature
verification on inbound artifacts, classification-policy evaluation,
and trust-graph queries. See ``persona.md`` for the role definition;
runtime entrypoints live in ``warden.py``.

Today WARDEN is deterministic — every decision is a code path, not an
LLM judgment. The persona-loaded LLM seam is reserved for future
"explain this verdict to the operator" surfaces.
"""

from .warden import Warden, WardenVerdict  # noqa: F401
