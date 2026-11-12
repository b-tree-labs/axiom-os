# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing decision audit log — JSONL recording of every LLM routing decision.

Every call to Gateway._select_provider() or ChatAgent.turn() logs:
- Timestamp, session ID, routing tier, classifier, provider, query hash
- No plaintext query or response content (privacy/EC compliance)

Log location: ``<platform state dir>/logs/routing_audit.jsonl`` (``~/.axi/logs/``
by default; ``AXI_STATE_DIR`` redirects it). One file per install, whatever the
working directory of the process that writes or reads it.

It used to live under the project root (``<root>/runtime/logs/``), which a
process resolves from its working directory or ``AXIOM_ROOT``. An MCP server
spawned by a harness ran with a working directory of ``/`` and wrote under
``/private/tmp``; a developer with sixty worktrees had sixty logs; and
``audit explain`` run from any other directory read a different file, so the
id a withhold handed the client resolved nowhere. The old location is still
read, so an id written by older code is still found.

Enabled by default (routing.audit_log = true in settings).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.hash_utils import LONG, fingerprint
from axiom.infra.state import locked_append_jsonl

logger = logging.getLogger(__name__)

_FILE_NAME = "routing_audit.jsonl"

#: An explicit location, set by tests and isolated runtimes. ``None`` means the
#: install's platform state directory.
_AUDIT_PATH: Path | None = None

#: Where older code wrote. Read, never written.
_LEGACY_PATHS: list[Path] = [_REPO_ROOT / "runtime" / "logs" / _FILE_NAME]


def audit_path() -> Path:
    """The one file this install's routing decisions are written to."""
    if _AUDIT_PATH is not None:
        return Path(_AUDIT_PATH)
    try:
        from axiom.infra.paths import get_platform_state_dir

        return get_platform_state_dir() / "logs" / _FILE_NAME
    except Exception:  # noqa: BLE001 — a state dir that cannot be resolved falls back
        return _LEGACY_PATHS[0]


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
    extra: dict[str, Any] | None = None,
) -> bool:
    """Append a routing decision to the audit log. True when it was written.

    Writes never raise and never block the chat loop. A write that fails is
    logged at warning level and reported as False, so a gate can say in its
    envelope that the decision it is handing back is not on record, instead of
    the failure being swallowed where nobody reads.

    ``routing_event_id`` is the id a routing decision hands back to a caller
    (e.g. the MCP sink gate returns it on a withhold). Storing it here is what
    makes ``axi audit explain <id>`` able to resolve the breadcrumb the caller
    was given; before it was stored, that id resolved nowhere.

    ``extra`` carries content-free fields a gate adds (the server revision,
    the labels that decided, the tool and client names).
    """
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        if not SettingsStore().get("routing.audit_log", True):
            return False
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
    if extra:
        for key, value in extra.items():
            entry.setdefault(key, value)

    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_append_jsonl(path, entry)
        return True
    except OSError as exc:
        logger.warning(
            "routing audit: could not write %s to %s (%s: %s)",
            routing_event_id or "a decision", path, type(exc).__name__, exc,
        )
        return False


def _scan(path: Path, rid: str) -> dict[str, Any] | None:
    match: dict[str, Any] | None = None
    try:
        with open(path, encoding="utf-8") as fh:
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


def find_routing_decision(routing_event_id: str) -> dict[str, Any] | None:
    """The most recent logged routing decision for ``routing_event_id``, or None.

    Reads the install's audit log, then the locations older code wrote to.
    Used by ``audit explain`` to resolve a routing-event breadcrumb that a gate
    handed a caller — a different store from the authz verdict ledger, which is
    why a routing id was never found there. Best-effort: a missing or
    unreadable log returns None rather than raising, so an audit lookup never
    becomes the thing that fails.
    """
    rid = (routing_event_id or "").strip()
    if not rid:
        return None
    seen: set[Path] = set()
    for path in [audit_path(), *_LEGACY_PATHS]:
        path = Path(path)
        if path in seen:
            continue
        seen.add(path)
        match = _scan(path, rid)
        if match is not None:
            return match
    return None


def hash_query(text: str) -> str:
    """SHA-256 hash of query text (for audit log — no plaintext stored)."""
    return fingerprint(text, length=LONG)
