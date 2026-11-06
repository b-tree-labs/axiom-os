# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation — node identity, A2A agent cards, peer discovery, trust,
digest exchange, and signed-finding receive pipeline.

Slice 7 additions (digest + receive) implement ADR-021 P2/P3/P6/P7 on
top of axiom.vega.identity (Ed25519) and axiom.findings (content-addressed
signed findings).
"""

from axiom.vega.federation.digest import Digest, build_digest, verify_digest
from axiom.vega.federation.receive import (
    ReceiveOutcome,
    Rejected,
    receive_digest,
)

# NOTE: ``FederationGateway`` and friends are *not* eagerly imported here.
# They live in ``axiom.vega.federation.gateway`` which imports from
# ``axiom.memory.fragment``; the dependency direction
# (memory → federation.policy) means a top-level import would create a
# cycle. Callers explicitly import from
# ``axiom.vega.federation.gateway``. See spec §6 + §8.1.

__all__ = [
    "Digest",
    "ReceiveOutcome",
    "Rejected",
    "build_digest",
    "receive_digest",
    "verify_digest",
]
