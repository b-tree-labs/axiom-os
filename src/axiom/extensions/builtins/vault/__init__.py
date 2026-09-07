# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP — secret storage + capability token issuance.

Per ADR-055 + prd-axiom-vault: every authenticated outbound action goes
through ``outbound_call(capability, request, ctx)``. KEEP holds the
underlying credentials; agents only ever see scoped, time-limited,
revocable capability tokens.

Phase 1 backend: the existing ``axiom.infra.connections.get_credential``
chain (env → settings → 0600 file). Phase 2 adds hardware-attested
backends (TPM2 / Secure Enclave / Windows TBS). Phase 5 adds
HashiCorp Vault / AWS Secrets Manager / 1Password.

Consumer integration::

    from axiom.extensions.builtins.vault import (
        VaultContext, issue_capability, outbound_call, HttpRequest,
    )
    from axiom.governance import IntentPattern, ResourcePattern, Classification
    from axiom.infra.db import session_for

    ctx = VaultContext(session_factory=lambda: session_for("vault"))
    cap = issue_capability(
        ctx,
        subject=actor,
        intent_pattern=IntentPattern("notification.send"),
        resource_pattern=ResourcePattern("slack://*"),
        classification_ceiling=Classification.INTERNAL,
        secret_ref="slack",
    )

    response = outbound_call(
        cap,
        HttpRequest(method="POST", url="https://slack.com/...", headers={}),
        ctx,
    )
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.vault.capability_store import (
    VaultContext,
    get_capability_by_id,
    is_revoked,
    issue_capability,
    revoke_capability,
)
from axiom.extensions.builtins.vault.outbound import (
    HttpRequest,
    HttpResponse,
    outbound_call,
)

keep_persona_path = str(
    Path(__file__).parent / "agents" / "keep" / "persona.md"
)


__all__ = [
    "HttpRequest",
    "HttpResponse",
    "VaultContext",
    "get_capability_by_id",
    "is_revoked",
    "issue_capability",
    "keep_persona_path",
    "outbound_call",
    "revoke_capability",
]
