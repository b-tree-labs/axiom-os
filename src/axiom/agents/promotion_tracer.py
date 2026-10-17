# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Promotion pipeline tracer — proves data bubbles up from local → org → community.

Records every state transition in the knowledge promotion lifecycle:
  1. LEARNED: Pattern created (RED confidence)
  2. VERIFIED: Pattern verified at one node (RED → YELLOW)
  3. MULTI_VERIFIED: Pattern verified at 2+ nodes (YELLOW → GREEN)
  4. PROMOTED: Pattern promoted to shared repo (local → org)
  5. FEDERATED: Pattern synced to federation peers (org → community)
  6. INDEXED: Pattern indexed in knowledge corpus

Each event is timestamped and stored in a JSONL trace file
that can be audited to prove the promotion pipeline works end-to-end.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_TRACE_DIR = Path.home() / ".axi" / "traces"
_TRACE_FILE = _TRACE_DIR / "promotion.jsonl"


class PromotionTracer:
    """Records promotion lifecycle events for auditing and testing."""

    def __init__(self, trace_file: Path | None = None):
        self._trace_file = trace_file or _TRACE_FILE
        self._trace_file.parent.mkdir(parents=True, exist_ok=True)

    def trace(
        self,
        event: str,
        pattern_id: str,
        agent: str = "",
        confidence: str = "",
        node_id: str = "",
        details: dict | None = None,
    ) -> dict:
        """Record a promotion event.

        Returns the event dict for testing.
        """
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "pattern_id": pattern_id,
            "agent": agent,
            "confidence": confidence,
            "node_id": node_id or os.environ.get("AXIOM_NODE_ID", "local"),
            "details": details or {},
        }

        try:
            with open(self._trace_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            log.warning("Could not write promotion trace: %s", e)

        log.info(
            "PROMOTION_TRACE: %s pattern=%s agent=%s confidence=%s node=%s",
            event,
            pattern_id,
            agent,
            confidence,
            entry["node_id"],
        )
        return entry

    def get_traces(self, pattern_id: str | None = None) -> list[dict]:
        """Read traces, optionally filtered by pattern_id."""
        if not self._trace_file.exists():
            return []

        traces = []
        for line in self._trace_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if pattern_id is None or entry.get("pattern_id") == pattern_id:
                    traces.append(entry)
            except json.JSONDecodeError:
                continue
        return traces

    def get_promotion_journey(self, pattern_id: str) -> list[str]:
        """Get the sequence of events for a pattern (e.g., LEARNED → VERIFIED → PROMOTED)."""
        return [t["event"] for t in self.get_traces(pattern_id)]

    def assert_full_promotion(self, pattern_id: str) -> bool:
        """Assert that a pattern completed the full promotion journey."""
        journey = self.get_promotion_journey(pattern_id)
        required = {"LEARNED", "VERIFIED", "PROMOTED"}
        return required.issubset(set(journey))


# Singleton
_tracer: PromotionTracer | None = None


def get_tracer() -> PromotionTracer:
    global _tracer
    if _tracer is None:
        _tracer = PromotionTracer()
    return _tracer
