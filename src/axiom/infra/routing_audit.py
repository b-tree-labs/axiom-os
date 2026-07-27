# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing decision audit log — JSONL recording of every LLM routing decision.

Every call to Gateway._select_provider() or ChatAgent.turn() logs:
- Timestamp, session ID, routing tier, classifier, provider, query hash
- No plaintext query or response content (privacy/EC compliance)

Log location: runtime/logs/routing_audit.jsonl

Enabled by default (routing.audit_log = true in settings).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.hash_utils import LONG, fingerprint
from axiom.infra.state import locked_append_jsonl

logger = logging.getLogger(__name__)

_AUDIT_PATH = _REPO_ROOT / "runtime" / "logs" / "routing_audit.jsonl"


def log_routing_decision(
    *,
    session_id: str = "",
    query_hash: str = "",
    tier: str,
    classifier: str,
    provider: str = "",
    matched_terms: list[str] | None = None,
    reason: str = "",
    sensitivity: str = "",
) -> None:
    """Append a routing decision to the audit log.

    Writes are best-effort — never raises, never blocks the chat loop.
    """
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore
        if not SettingsStore().get("routing.audit_log", True):
            return
    except Exception:
        pass

    entry: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "query_hash": query_hash,
        "tier": tier,
        "classifier": classifier,
        "provider": provider,
        "reason": reason,
    }
    if matched_terms:
        entry["matched_terms"] = matched_terms
    if sensitivity:
        entry["sensitivity"] = sensitivity

    try:
        locked_append_jsonl(_AUDIT_PATH, entry)
    except OSError:
        logger.debug("Failed to write routing audit log", exc_info=True)


def hash_query(text: str) -> str:
    """SHA-256 hash of query text (for audit log — no plaintext stored)."""
    return fingerprint(text, length=LONG)
