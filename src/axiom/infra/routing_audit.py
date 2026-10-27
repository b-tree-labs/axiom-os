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

import json
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
    routing_event_id: str = "",
) -> None:
    """Append a routing decision to the audit log.

    Writes are best-effort — never raises, never blocks the chat loop.

    ``routing_event_id`` is the id a routing decision hands back to a caller
    (e.g. the MCP sink gate returns it on a withhold). Storing it here is what
    makes ``axi audit explain <id>`` able to resolve the breadcrumb the caller
    was given; before it was stored, that id resolved nowhere.
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
    if routing_event_id:
        entry["routing_event_id"] = routing_event_id
    if matched_terms:
        entry["matched_terms"] = matched_terms
    if sensitivity:
        entry["sensitivity"] = sensitivity

    try:
        locked_append_jsonl(_AUDIT_PATH, entry)
    except OSError:
        logger.debug("Failed to write routing audit log", exc_info=True)


def find_routing_decision(routing_event_id: str) -> dict[str, Any] | None:
    """The most recent logged routing decision for ``routing_event_id``, or None.

    Reads the JSONL audit log back. Used by ``audit explain`` to resolve a
    routing-event breadcrumb that a gate handed a caller — a different store
    from the authz verdict ledger, which is why a routing id was never found
    there. Best-effort: a missing or unreadable log returns None rather than
    raising, so an audit lookup never becomes the thing that fails.
    """
    rid = (routing_event_id or "").strip()
    if not rid:
        return None
    match: dict[str, Any] | None = None
    try:
        with open(_AUDIT_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or rid not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("routing_event_id") == rid:
                    match = row  # last writer wins
    except OSError:
        return None
    return match


def hash_query(text: str) -> str:
    """SHA-256 hash of query text (for audit log — no plaintext stored)."""
    return fingerprint(text, length=LONG)
