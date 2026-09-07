# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signal Router — matches signals to interested endpoints and tracks delivery.

The router is the central dispatch for all synthesized signals:
1. Load endpoint definitions from endpoints.yaml
2. Match signals to endpoints based on interests/capabilities
3. Deliver via appropriate backend (file, webhook, slack, etc.)
4. Track all transit in transit_log.json

Usage:
    from axiom.extensions.builtins.signals.router import Router

    router = Router()
    router.route(signals)  # Match and queue
    router.deliver()       # Push to endpoints
    router.status()        # Get delivery status
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.config_loader import load_yaml
from axiom.infra.state import atomic_write

from .models import Signal

_RUNTIME_DIR = _REPO_ROOT / "runtime"
CONFIG_DIR = _RUNTIME_DIR / "config"
TRANSIT_LOG = _RUNTIME_DIR / "inbox" / "processed" / "transit_log.json"


@dataclass
class Endpoint:
    """An endpoint that can receive signals."""

    id: str
    name: str
    description: str = ""
    enabled: bool = True

    # Interest filters
    signal_types: list[str] | str = "all"
    initiatives: list[str] | str = "all"
    people: list[str] | str = "all"
    min_confidence: float = 0.0
    max_confidence: float = 1.0

    # Capabilities
    formats: list[str] = field(default_factory=lambda: ["markdown"])
    max_signals_per_batch: int = 1000

    # Delivery
    delivery_method: str = "file"
    delivery_config: dict = field(default_factory=dict)
    frequency: str = "manual"  # realtime | daily | weekly | manual

    def matches(self, signal: Signal) -> bool:
        """Check if this endpoint is interested in a signal."""
        if not self.enabled:
            return False

        # Check confidence bounds
        if signal.confidence < self.min_confidence:
            return False
        if signal.confidence > self.max_confidence:
            return False

        # Check signal type
        if self.signal_types != "all":
            if signal.signal_type not in self.signal_types:
                return False

        # Check initiatives
        if self.initiatives != "all":
            if not signal.initiatives:
                return False
            if not any(init in self.initiatives for init in signal.initiatives):
                return False

        # Check people
        if self.people != "all":
            if not signal.people:
                return False
            if not any(person in self.people for person in signal.people):
                return False

        return True


@dataclass
class TransitRecord:
    """Record of a signal routed to an endpoint."""

    signal_id: str
    endpoint_id: str
    queued_at: str
    delivered_at: str | None = None
    status: str = "queued"  # queued | delivered | failed | skipped
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "endpoint_id": self.endpoint_id,
            "queued_at": self.queued_at,
            "delivered_at": self.delivered_at,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TransitRecord:
        return cls(**data)


class Router:
    """Routes signals to interested endpoints."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or (CONFIG_DIR / "endpoints.yaml")
        self.endpoints: dict[str, Endpoint] = {}
        self.transit: list[TransitRecord] = []
        self._deliverers: dict[str, Callable] = {}

        self._load_config()
        self._load_transit()
        self._register_deliverers()

    def _load_config(self) -> None:
        """Load endpoint definitions from YAML."""
        config = load_yaml(self.config_path)
        if not config:
            return

        for endpoint_id, cfg in config.items():
            if not isinstance(cfg, dict):
                continue

            interests = cfg.get("interests", {})
            capabilities = cfg.get("capabilities", {})
            delivery = cfg.get("delivery", {})

            self.endpoints[endpoint_id] = Endpoint(
                id=endpoint_id,
                name=cfg.get("name", endpoint_id),
                description=cfg.get("description", ""),
                enabled=cfg.get("enabled", True),
                signal_types=interests.get("signal_types", "all"),
                initiatives=interests.get("initiatives", "all"),
                people=interests.get("people", "all"),
                min_confidence=interests.get("min_confidence", 0.0),
                max_confidence=interests.get("max_confidence", 1.0),
                formats=capabilities.get("formats", ["markdown"]),
                max_signals_per_batch=capabilities.get("max_signals_per_batch", 1000),
                delivery_method=delivery.get("method", "file"),
                delivery_config=delivery,
                frequency=delivery.get("frequency", "manual"),
            )

    def _load_transit(self) -> None:
        """Load transit log from disk."""
        if TRANSIT_LOG.exists():
            try:
                data = json.loads(TRANSIT_LOG.read_text())
                self.transit = [TransitRecord.from_dict(r) for r in data.get("records", [])]
            except (json.JSONDecodeError, KeyError):
                self.transit = []

    def _save_transit(self) -> None:
        """Persist transit log to disk."""
        TRANSIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_updated": datetime.now(UTC).isoformat(),
            "records": [r.to_dict() for r in self.transit[-1000:]],  # Keep last 1000
        }
        atomic_write(TRANSIT_LOG, data)

    def _register_deliverers(self) -> None:
        """Register delivery backends."""
        self._deliverers = {
            "file": self._deliver_file,
            "webhook": self._deliver_webhook,
            "internal": self._deliver_internal,
            # Add more: slack, email, gitlab_issue, etc.
        }

    def route(self, signals: list[Signal]) -> dict[str, list[Signal]]:
        """Match signals to endpoints and queue for delivery.

        Returns:
            Dict mapping endpoint_id to list of matched signals.
        """
        now = datetime.now(UTC).isoformat()
        routed: dict[str, list[Signal]] = {}

        for signal in signals:
            for endpoint_id, endpoint in self.endpoints.items():
                if endpoint.matches(signal):
                    if endpoint_id not in routed:
                        routed[endpoint_id] = []
                    routed[endpoint_id].append(signal)

                    # Create transit record
                    self.transit.append(TransitRecord(
                        signal_id=signal.signal_id,
                        endpoint_id=endpoint_id,
                        queued_at=now,
                    ))

        self._save_transit()
        return routed

    def deliver(
        self,
        endpoint_ids: list[str] | None = None,
        frequency: str | None = None,
    ) -> dict[str, int]:
        """Deliver queued signals to endpoints.

        Args:
            endpoint_ids: Specific endpoints to deliver to (default: all).
            frequency: Only deliver to endpoints with this frequency.

        Returns:
            Dict mapping endpoint_id to count of delivered signals.
        """
        delivered: dict[str, int] = {}
        now = datetime.now(UTC).isoformat()

        # Group queued records by endpoint
        queued_by_endpoint: dict[str, list[TransitRecord]] = {}
        for record in self.transit:
            if record.status == "queued":
                if endpoint_ids and record.endpoint_id not in endpoint_ids:
                    continue
                if record.endpoint_id not in queued_by_endpoint:
                    queued_by_endpoint[record.endpoint_id] = []
                queued_by_endpoint[record.endpoint_id].append(record)

        for endpoint_id, records in queued_by_endpoint.items():
            endpoint = self.endpoints.get(endpoint_id)
            if not endpoint or not endpoint.enabled:
                continue

            if frequency and endpoint.frequency != frequency:
                continue

            # Get the deliverer for this method
            deliverer = self._deliverers.get(endpoint.delivery_method)
            if not deliverer:
                for record in records:
                    record.status = "failed"
                    record.error = f"Unknown delivery method: {endpoint.delivery_method}"
                continue

            # Deliver
            try:
                # We need the actual signals, not just records
                # For now, mark as delivered (actual delivery happens in specific methods)
                count = deliverer(endpoint, records)
                delivered[endpoint_id] = count

                for record in records:
                    record.status = "delivered"
                    record.delivered_at = now

            except Exception as e:
                for record in records:
                    record.status = "failed"
                    record.error = str(e)

        self._save_transit()
        return delivered

    def _deliver_file(self, endpoint: Endpoint, records: list[TransitRecord]) -> int:
        """Deliver signals by writing to a file."""
        path = endpoint.delivery_config.get("path", f"exports/{endpoint.id}.md")
        full_path = _RUNTIME_DIR / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # For now, just note that signals were routed
        # Actual signal content would need to be looked up
        lines = [
            f"# {endpoint.name}",
            "",
            f"*Updated: {datetime.now(UTC).isoformat()}*",
            "",
            f"{len(records)} signal(s) routed to this endpoint.",
            "",
        ]

        for record in records:
            lines.append(f"- `{record.signal_id}` queued at {record.queued_at}")

        full_path.write_text("\n".join(lines))
        return len(records)

    def _deliver_webhook(self, endpoint: Endpoint, records: list[TransitRecord]) -> int:
        """Deliver signals via HTTP webhook."""
        url = endpoint.delivery_config.get("url", "")

        # Expand env vars
        if url.startswith("${") and url.endswith("}"):
            env_var = url[2:-1]
            url = os.environ.get(env_var, "")

        if not url:
            raise ValueError(f"No webhook URL configured for {endpoint.id}")

        # Would implement actual HTTP POST here
        # For now, just return count
        return len(records)

    def _deliver_internal(self, endpoint: Endpoint, records: list[TransitRecord]) -> int:
        """Internal delivery (for dashboard, status commands, etc.)."""
        # No-op — internal endpoints are always "delivered"
        return len(records)

    def status(self) -> dict:
        """Get current routing status."""
        # Count by status
        status_counts = {"queued": 0, "delivered": 0, "failed": 0}
        for record in self.transit:
            if record.status in status_counts:
                status_counts[record.status] += 1

        # Count by endpoint
        by_endpoint: dict[str, dict] = {}
        for endpoint_id, endpoint in self.endpoints.items():
            ep_records = [r for r in self.transit if r.endpoint_id == endpoint_id]
            by_endpoint[endpoint_id] = {
                "name": endpoint.name,
                "enabled": endpoint.enabled,
                "frequency": endpoint.frequency,
                "method": endpoint.delivery_method,
                "queued": sum(1 for r in ep_records if r.status == "queued"),
                "delivered": sum(1 for r in ep_records if r.status == "delivered"),
                "failed": sum(1 for r in ep_records if r.status == "failed"),
            }

        return {
            "total_endpoints": len(self.endpoints),
            "enabled_endpoints": sum(1 for e in self.endpoints.values() if e.enabled),
            "status_counts": status_counts,
            "by_endpoint": by_endpoint,
        }

    def get_pending(self, endpoint_id: str | None = None) -> list[TransitRecord]:
        """Get all queued (pending) records."""
        records = [r for r in self.transit if r.status == "queued"]
        if endpoint_id:
            records = [r for r in records if r.endpoint_id == endpoint_id]
        return records

    def clear_delivered(self, older_than_days: int = 7) -> int:
        """Clear old delivered records from transit log."""
        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        cutoff_str = cutoff.isoformat()

        original = len(self.transit)
        self.transit = [
            r for r in self.transit
            if r.status != "delivered" or (r.delivered_at and r.delivered_at > cutoff_str)
        ]
        removed = original - len(self.transit)
        self._save_transit()
        return removed


def get_router() -> Router:
    """Get the singleton router instance."""
    return Router()
