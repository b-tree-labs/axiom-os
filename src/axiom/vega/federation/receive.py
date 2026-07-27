# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Receive pipeline: verify digest → eval gate → route.

ADR-021 implementation:
- P2: signed content-addressed findings must verify before anything else
- P3: local eval gate runs on every promotion
- P6: quarantined peers can deliver; nothing promotes into local corpus
- P7: per-peer pass rate is returned for caller-side aggregation
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from axiom.findings import Finding
from axiom.vega.federation.digest import Digest, verify_digest

EvalFn = Callable[[Finding], float]


@dataclass(frozen=True)
class Rejected:
    finding: Finding | None
    reason: str


@dataclass(frozen=True)
class ReceiveOutcome:
    accepted: list[Finding] = field(default_factory=list)
    quarantined: list[Finding] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    peer_pass_rate: float = 0.0


def receive_digest(
    digest: Digest,
    *,
    pubkeys: dict[str, bytes],
    peer_status: str,  # quarantine | cluster | partner | federated
    eval_fn: EvalFn,
    eval_threshold: float = 0.5,
) -> ReceiveOutcome:
    # Signature/chain verification is the first gate.
    if not verify_digest(digest, pubkeys):
        return ReceiveOutcome(rejected=[Rejected(finding=None, reason="invalid_signature")])

    accepted: list[Finding] = []
    quarantined: list[Finding] = []
    rejected: list[Rejected] = []

    for f in digest.findings:
        score = eval_fn(f)
        if score < eval_threshold:
            rejected.append(Rejected(finding=f, reason="eval_gate_failed"))
            continue

        if peer_status == "quarantine":
            quarantined.append(f)
        else:
            accepted.append(f)

    # Pass rate = findings that met eval gate / total findings in digest.
    total = len(digest.findings)
    passed = len(accepted) + len(quarantined)
    pass_rate = passed / total if total else 0.0

    return ReceiveOutcome(
        accepted=accepted,
        quarantined=quarantined,
        rejected=rejected,
        peer_pass_rate=pass_rate,
    )
