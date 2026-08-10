# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Security Event Log — always-on, fires in both standard and EC mode.

Distinct from AuditLog (EC-mode-only compliance records). SecurityLog captures
active threat signals that require SCAN attention regardless of routing tier:

  - chunk_injection_detected   RAG chunk contained an injection pattern
  - response_scan_hit          LLM response matched a classified keyword
  - ec_leakage_suspected       EC-classified content appeared in a public response

Every event is written to runtime/logs/security/security_events.jsonl via the
same locked-append primitive as the audit log, and simultaneously promoted to
the signal bus via axiom_signal() so SCAN can react.

Signal events emitted (must be registered in signal_event_registry.toml):
  security.chunk_injection_detected
  security.response_scan_hit
  security.ec_leakage_suspected

Usage:
    from axiom.infra.security_log import SecurityLog

    SecurityLog.get().chunk_injection(
        chunk_source="rag-org/procedures.md",
        patterns_matched=["ignore previous instructions"],
        session_id="sess-abc",
        corpus="rag-org",
    )
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.axiom_logging import axiom_signal, get_logger
from axiom.infra.state import locked_append_jsonl

_log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SecurityLog:
    """Always-on security event log singleton.

    All write methods fire in both standard and EC mode — unlike AuditLog,
    which is EC-mode-only.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        from axiom import REPO_ROOT
        self._log_dir = Path(log_dir) if log_dir else REPO_ROOT / "runtime" / "logs" / "security"

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> SecurityLog:
        global _instance
        if _instance is None:
            _instance = cls()
        return _instance

    # ------------------------------------------------------------------
    # Internal write
    # ------------------------------------------------------------------

    def _write(self, table: str, record: dict) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / f"{table}.jsonl"
        locked_append_jsonl(path, record)

    # ------------------------------------------------------------------
    # Event writers
    # ------------------------------------------------------------------

    def chunk_injection(
        self,
        *,
        chunk_source: str,
        patterns_matched: list[str],
        session_id: str = "",
        corpus: str = "",
        sanitized: bool = True,
    ) -> None:
        """Log a prompt injection pattern detected in a retrieved RAG chunk."""
        event_id = str(uuid.uuid4())
        record = {
            "event_id": event_id,
            "event_type": "chunk_injection_detected",
            "ts": _now(),
            "chunk_source": chunk_source,
            "patterns_matched": patterns_matched,
            "session_id": session_id,
            "corpus": corpus,
            "sanitized": sanitized,
        }
        self._write("security_events", record)
        _log.warning(
            "Prompt injection pattern detected in RAG chunk from '%s' (%d pattern(s) matched)",
            chunk_source,
            len(patterns_matched),
            extra=axiom_signal(
                "security.chunk_injection_detected",
                event_id=event_id,
                chunk_source=chunk_source,
                patterns_matched=patterns_matched,
                session_id=session_id,
                corpus=corpus,
                sanitized=sanitized,
            ),
        )

    def response_scan_hit(
        self,
        *,
        session_id: str,
        provider_name: str,
        routing_tier: str,
        matched_terms: list[str],
        prompt_hash: str,
        response_hash: str,
        warning_prepended: bool = True,
    ) -> None:
        """Log a classified keyword hit in an LLM response (possible EC leakage)."""
        event_id = str(uuid.uuid4())
        is_ec_leakage = routing_tier == "public" and bool(matched_terms)
        event_type = "ec_leakage_suspected" if is_ec_leakage else "response_scan_hit"
        record = {
            "event_id": event_id,
            "event_type": event_type,
            "ts": _now(),
            "session_id": session_id,
            "provider_name": provider_name,
            "routing_tier": routing_tier,
            "matched_terms": matched_terms,
            "prompt_hash": prompt_hash,
            "response_hash": response_hash,
            "warning_prepended": warning_prepended,
        }
        self._write("security_events", record)

        signal_event = (
            "security.ec_leakage_suspected"
            if is_ec_leakage
            else "security.response_scan_hit"
        )
        level = logging.ERROR if is_ec_leakage else logging.WARNING
        _log.log(
            level,
            "%s: %d classified term(s) in LLM response from '%s' (tier=%s)",
            event_type,
            len(matched_terms),
            provider_name,
            routing_tier,
            extra=axiom_signal(
                signal_event,
                event_id=event_id,
                session_id=session_id,
                provider_name=provider_name,
                routing_tier=routing_tier,
                matched_terms=matched_terms,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
            ),
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: SecurityLog | None = None
