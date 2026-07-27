# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""EC Compliance Audit Log — Layer 2 of the two-layer logging system.

Layer 1 (System Log) is always active in all modes and uses standard Python
logging. This module implements Layer 2: structured, HMAC-chained audit records
that are only written in EC mode.

In standard mode all write_* calls are no-ops. No database connection is
opened. No files are created. Zero overhead for non-EC deployments.

In EC mode, records are written synchronously (blocking) to preserve ordering
and guarantee delivery before the LLM HTTP call completes.

Backend selection (AXIOM_AUDIT_BACKEND env var):
    "auto"    Try PostgreSQL; fall back to JSONL flat-file (default)
    "jsonl"   Always use JSONL flat-file
    "null"    No-op (forced by set_mode("standard"))

HMAC chain: every routing_events record includes an HMAC-SHA256 field that
chains from the previous record's HMAC. The first record uses the sentinel
prev_hmac = "GENESIS". verify_chain() detects any tampered or deleted record.

Usage:
    from axiom.infra.audit_log import AuditLog

    audit = AuditLog.get()   # module-level singleton
    audit.set_mode("ec")     # called by Gateway._load_config

    audit.write_routing(session_id=..., ...)
    ok, broken_at = audit.verify_chain(table="routing_events")
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.state import locked_append_jsonl

_log = logging.getLogger(__name__)

_GENESIS = "GENESIS"


class ECViolationError(Exception):
    """Raised by write_routing when an EC routing violation is detected.

    Callers handling export_controlled requests must catch this and block
    the request — the audit record is already written before the raise.
    """


# ---------------------------------------------------------------------------
# BackendInfo — returned by AuditLog.backend_info()
# ---------------------------------------------------------------------------

@dataclass
class BackendInfo:
    backend: str          # "null" | "jsonl" | "postgres"
    pending_promotion: int = 0
    log_size_bytes: int = 0


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------

class _NullBackend:
    """No-op backend used in standard mode."""

    def write(self, table: str, record: dict) -> None:
        pass

    def read_ordered(self, table: str) -> list[dict]:
        return []

    def info(self) -> BackendInfo:
        return BackendInfo(backend="null")


class _JsonlBackend:
    """Locked JSONL flat-file backend — always available, no external deps."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, table: str) -> Path:
        return self._log_dir / f"{table}.jsonl"

    def write(self, table: str, record: dict) -> None:
        locked_append_jsonl(self._path(table), record)

    def read_ordered(self, table: str) -> list[dict]:
        path = self._path(table)
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    def info(self) -> BackendInfo:
        total = sum(
            p.stat().st_size for p in self._log_dir.glob("*.jsonl") if p.exists()
        )
        return BackendInfo(backend="jsonl", log_size_bytes=total)


# ---------------------------------------------------------------------------
# HMAC chain helpers
# ---------------------------------------------------------------------------

def _canonical(record: dict) -> str:
    """Deterministic JSON serialisation (sorted keys, no whitespace)."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _compute_hmac(key: str, record_without_hmac: dict, prev_hmac: str) -> str:
    message = (_canonical(record_without_hmac) + prev_hmac).encode()
    return hmac_lib.new(key.encode(), message, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# AuditLog — public interface
# ---------------------------------------------------------------------------

class AuditLog:
    """EC Compliance Audit Log singleton.

    All write_* methods are no-ops in standard mode. In EC mode they write
    synchronously to the active backend with HMAC chaining (routing_events only).
    """

    def __init__(
        self,
        log_dir: Path | None = None,
        hmac_key: str | None = None,
    ) -> None:
        from axiom import REPO_ROOT
        self._log_dir = Path(log_dir) if log_dir else REPO_ROOT / "runtime" / "logs" / "audit"
        self._hmac_key: str | None = hmac_key
        self._mode: str = "standard"
        self._backend: _NullBackend | _JsonlBackend = _NullBackend()
        self._last_routing_hmac: str = _GENESIS

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> AuditLog:
        global _instance
        if _instance is None:
            hmac_key = os.environ.get("AXIOM_AUDIT_HMAC_KEY")
            _instance = cls(hmac_key=hmac_key)
        return _instance

    # ------------------------------------------------------------------
    # Mode / backend management
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in ("standard", "ec"):
            raise ValueError(f"Unknown audit mode '{mode}'. Expected 'standard' or 'ec'.")
        if mode == "ec" and not self._hmac_key:
            raise ValueError(
                "EC audit log requires AXIOM_AUDIT_HMAC_KEY. "
                "Set this environment variable or run 'axi setup audit-key'."
            )
        self._mode = mode
        if mode == "standard":
            self._backend = _NullBackend()
        else:
            self._backend = self._select_backend()

    def _select_backend(self) -> _NullBackend | _JsonlBackend:
        env = os.environ.get("AXIOM_AUDIT_BACKEND", "auto")
        if env == "jsonl":
            return _JsonlBackend(self._log_dir)
        if env == "auto":
            try:
                from axiom.infra.db import get_db_connection  # type: ignore[import]
                get_db_connection(timeout=2)
                # PostgreSQL available — would return PostgresBackend here (Phase 2)
                _log.info("audit_log: PostgreSQL available (Phase 2 backend pending — using jsonl)")
            except Exception:
                pass
        return _JsonlBackend(self._log_dir)

    def _force_jsonl_backend(self) -> None:
        """Test hook: force JSONL backend regardless of mode or env."""
        self._backend = _JsonlBackend(self._log_dir)

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def write_classification(
        self,
        *,
        routing_event_id: str,
        prompt_hash: str,
        classifier: str,
        keyword_matched: bool,
        keyword_term: str | None,
        ollama_result: str | None,
        sensitivity: str,
        final_tier: str,
        is_ec: bool,
    ) -> None:
        if self._mode == "standard":
            return
        record = {
            "event_id": str(uuid.uuid4()),
            "ts": _now(),
            "routing_event_id": routing_event_id,
            "prompt_hash": prompt_hash,
            "classifier": classifier,
            "keyword_matched": keyword_matched,
            "keyword_term": keyword_term,
            "ollama_result": ollama_result,
            "sensitivity": sensitivity,
            "final_tier": final_tier,
            "is_ec": is_ec,
        }
        self._backend.write("classification_events", record)

    def write_classifier_failure(
        self,
        *,
        reason: str,
        detail: str,
        endpoint: str,
        model: str | None,
    ) -> None:
        """Record a typed SLM classifier failure (Improvement Item 1).

        Distinct from write_classification: this captures the *failure mode*
        (ollama_unreachable, model_not_loaded, timeout, unexpected_response,
        transport_error) so an operator can diagnose why the SLM stage
        produced no signal — instead of mis-reading silent fallbacks as
        benign uncertain results.
        """
        if self._mode == "standard":
            return
        record = {
            "event_id": str(uuid.uuid4()),
            "ts": _now(),
            "reason": reason,
            "detail": detail,
            "endpoint": endpoint,
            "model": model,
        }
        self._backend.write("classifier_failure_events", record)

    def write_routing(
        self,
        *,
        session_id: str,
        tier_requested: str,
        tier_assigned: str,
        provider_name: str,
        provider_tier: str,
        blocked: bool,
        block_reason: str | None,
        prompt_hash: str,
        response_hash: str | None,
        ec_violation: bool,
        is_ec: bool,
    ) -> None:
        if self._mode == "standard":
            return
        record: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "ts": _now(),
            "session_id": session_id,
            "tier_requested": tier_requested,
            "tier_assigned": tier_assigned,
            "provider_name": provider_name,
            "provider_tier": provider_tier,
            "blocked": blocked,
            "block_reason": block_reason,
            "prompt_hash": prompt_hash,
            "response_hash": response_hash,
            "ec_violation": ec_violation,
            "is_ec": is_ec,
        }
        # HMAC chain — stamp before writing
        if self._hmac_key:
            record["hmac"] = _compute_hmac(self._hmac_key, record, self._last_routing_hmac)
            self._last_routing_hmac = record["hmac"]
        self._backend.write("routing_events", record)

        # EC violation: write first (audit record preserved), then raise
        if ec_violation:
            raise ECViolationError(
                f"EC routing violation: session={session_id} provider={provider_name}"
            )

    def write_vpn(
        self,
        *,
        routing_event_id: str,
        provider_name: str,
        vpn_reachable: bool,
        check_duration_ms: int,
    ) -> None:
        if self._mode == "standard":
            return
        record = {
            "event_id": str(uuid.uuid4()),
            "ts": _now(),
            "routing_event_id": routing_event_id,
            "provider_name": provider_name,
            "vpn_reachable": vpn_reachable,
            "check_duration_ms": check_duration_ms,
        }
        self._backend.write("vpn_events", record)

    def write_config_load(
        self,
        *,
        config_file: str,
        providers: list[dict],
        ec_providers_count: int,
    ) -> None:
        if self._mode == "standard":
            return
        record = {
            "event_id": str(uuid.uuid4()),
            "ts": _now(),
            "config_file": config_file,
            "providers_json": providers,
            "ec_providers_count": ec_providers_count,
        }
        self._backend.write("config_load_events", record)

    # ------------------------------------------------------------------
    # Chain verification
    # ------------------------------------------------------------------

    def verify_chain(
        self, *, table: str = "routing_events", since: datetime | None = None
    ) -> tuple[bool, int | None]:
        """Verify the HMAC chain for routing_events.

        Returns (True, None) if intact, or (False, index) of first broken link.
        index is 0-based position in the file.
        """
        if not self._hmac_key:
            return True, None

        records = self._backend.read_ordered(table)
        if not records:
            return True, None

        prev_hmac = _GENESIS
        for i, record in enumerate(records):
            stored_hmac = record.get("hmac", "")
            body = {k: v for k, v in record.items() if k != "hmac"}
            expected = _compute_hmac(self._hmac_key, body, prev_hmac)
            if stored_hmac != expected:
                _log.error(
                    "HMAC chain broken at record %d in %s — possible tampering detected.",
                    i, table,
                )
                return False, i
            prev_hmac = stored_hmac

        return True, None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def backend_info(self) -> BackendInfo:
        return self._backend.info()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: AuditLog | None = None
