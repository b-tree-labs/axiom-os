# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom structured logging infrastructure.

Provides:
    get_logger(name)          — standard Python logger pre-wired to all Axiom handlers
    axiom_signal(event, **kw)  — build an extra= dict to promote a log record to the signal bus
    StructuredJsonFormatter   — serialises log records to JSON with trace_id, session_id, exc_info
    ForensicRingBuffer        — in-memory circular buffer of recent DEBUG+ records; no I/O until flushed
    IncidentSnapshotHandler   — auto-flushes ring buffer to disk on ERROR/CRITICAL
    SignalSink                — promotes explicitly-marked records to the signal bus
    load_signal_registry      — loads registered signal event types from TOML

Extension developer quick-start:

    from axiom.infra.axiom_logging import get_logger, axiom_signal

    logger = get_logger(__name__)

    logger.info("Signal batch ingested", extra={"count": 42})
    logger.warning("VPN degraded", extra=axiom_signal(
        "connections.vpn_degraded",
        provider="my-provider",
        attempts=3,
    ))

See docs/specs/spec-logging.md §19 for the full extension developer guide.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import traceback as _tb
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.trace import current_session, current_trace

# ---------------------------------------------------------------------------
# Internal logger (used only within this module — avoids recursion)
# ---------------------------------------------------------------------------
_internal = logging.getLogger("axiom.infra.axiom_logging")

# ---------------------------------------------------------------------------
# Fields on LogRecord that belong to the logging internals, not user payload.
# We exclude these when serialising extra= fields to avoid noise.
# ---------------------------------------------------------------------------
_STDLIB_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
})


# ---------------------------------------------------------------------------
# StructuredJsonFormatter
# ---------------------------------------------------------------------------

class StructuredJsonFormatter(logging.Formatter):
    """Serialises a LogRecord to a single-line JSON string.

    Guaranteed fields: ts, level, logger, trace_id, session_id, msg.
    Extra fields set via extra={} appear as top-level keys.
    Exception info is serialised to exc_type / exc_value / exc_traceback.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Ensure record.message is set
        record.message = record.getMessage()

        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
                  .isoformat(timespec="milliseconds")
                  .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": current_trace(),
            "session_id": current_session(),
            "msg": record.message,
        }

        # Serialise exception info to structured fields
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_value, exc_tb = record.exc_info
            data["exc_type"] = exc_type.__name__
            data["exc_value"] = str(exc_value)
            data["exc_traceback"] = [
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "name": frame.name,
                    "text": (frame.line or "").strip(),
                }
                for frame in _tb.extract_tb(exc_tb)
            ]

        # Merge extra= fields (anything not in stdlib attrs)
        for key, value in record.__dict__.items():
            if key not in _STDLIB_RECORD_ATTRS and not key.startswith("_"):
                data[key] = value

        return json.dumps(data, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# ForensicRingBuffer
# ---------------------------------------------------------------------------

class ForensicRingBuffer(logging.Handler):
    """In-memory circular buffer of recent log records. No I/O until flushed.

    Holds up to `capacity` records at DEBUG level. When an incident occurs,
    call flush_snapshot() to write the buffer to a timestamped JSONL file.
    The buffer captures the "what happened before the failure" context without
    the cost of writing DEBUG to disk continuously.
    """

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__(level=logging.DEBUG)
        self._buf: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "trace_id": getattr(record, "trace_id", current_trace()),
                "msg": record.getMessage(),
            }
            with self._lock:
                self._buf.append(entry)
        except Exception:
            self.handleError(record)

    def flush_snapshot(self, path: Path, *, reason: str) -> Path:
        """Flush buffer to a timestamped JSONL file. Returns the path written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = path.with_suffix(f".{ts}.jsonl")
        with self._lock:
            records = list(self._buf)
        with open(snapshot_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"reason": reason, "record_count": len(records)}) + "\n")
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
        return snapshot_path


# ---------------------------------------------------------------------------
# IncidentSnapshotHandler
# ---------------------------------------------------------------------------

class IncidentSnapshotHandler(logging.Handler):
    """On ERROR or CRITICAL: flush the ring buffer to a dated incident file.

    A cooldown prevents snapshot storms during cascading failures. Forensic
    snapshots are never auto-deleted — TIDY archives them after review.
    """

    def __init__(
        self,
        ring: ForensicRingBuffer,
        snapshot_dir: Path,
        cooldown_s: float = 30.0,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self._ring = ring
        self._snapshot_dir = Path(snapshot_dir)
        self._cooldown_s = cooldown_s
        self._last_snapshot: float = 0.0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        # Only act on ERROR and above (explicit guard — emit() may be called
        # directly in tests, bypassing the handler-level filter in handle())
        if record.levelno < logging.ERROR:
            return
        # Guard: do not re-enter if this is the internal snapshot-written log
        if record.name == _internal.name:
            return

        now = record.created
        with self._lock:
            if now - self._last_snapshot < self._cooldown_s:
                return
            self._last_snapshot = now

        trace_id = getattr(record, "trace_id", current_trace()) or "no-trace"
        reason = f"{record.levelname} in {record.name}: {record.getMessage()[:120]}"
        path = self._snapshot_dir / f"incident-{trace_id}"
        try:
            snapshot_path = self._ring.flush_snapshot(path, reason=reason)
            _internal.warning("Incident snapshot written: %s", snapshot_path)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# SignalSink
# ---------------------------------------------------------------------------

class SignalSink:
    """Promotes explicitly-marked log records to the signal bus.

    Only records with a ``signal_event`` key (set via axiom_signal()) are
    forwarded. Records without it are ignored with zero overhead.
    Unregistered event types are logged as errors and dropped.
    EC records (is_ec_record=True) are never promoted (hardcoded, not config).
    """

    accepts_ec: bool = False

    def __init__(self, registry: set[str], accepts_ec: bool = False) -> None:
        self._registry = registry
        self.accepts_ec = accepts_ec

    def write(self, record_dict: dict) -> None:
        signal_event = record_dict.get("signal_event")
        if not signal_event:
            return  # fast path: most records

        # EC records never go to the signal bus
        if not self.accepts_ec and record_dict.get("is_ec_record"):
            return

        if signal_event not in self._registry:
            _internal.error(
                "Unregistered signal_event '%s' blocked at promotion gate. "
                "Add it to signal_event_registry.toml before use.",
                signal_event,
            )
            return

        payload: dict = dict(record_dict.get("signal_payload") or {})
        payload["_log_level"] = record_dict.get("levelname", "")
        payload["_log_ts"] = record_dict.get("created", "")
        self._emit(signal_event, payload)

    def _emit(self, event_type: str, payload: dict) -> None:
        """Dispatch to the signal bus. Replaced in tests; overrideable in prod."""
        try:
            import asyncio

            from axiom.infra.events import emit as _bus_emit
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_bus_emit(event_type, payload))
        except Exception:
            pass  # signal bus unavailable; log record unaffected


# ---------------------------------------------------------------------------
# Signal event registry loader
# ---------------------------------------------------------------------------

def load_signal_registry(path: Path) -> set[str]:
    """Load registered signal event types from a TOML file.

    Returns a set of event_type strings. Returns empty set if file is missing
    or unparseable (logs a warning in that case).
    """
    if not path.exists():
        return set()
    try:
        from axiom.infra.toml_compat import load_toml
        data = load_toml(path)
        return {e["event_type"] for e in data.get("events", []) if "event_type" in e}
    except Exception as exc:
        _internal.warning("Failed to load signal registry from %s: %s", path, exc)
        return set()


# ---------------------------------------------------------------------------
# axiom_signal helper
# ---------------------------------------------------------------------------

def axiom_signal(event_type: str, **payload: Any) -> dict:
    """Build an extra= dict that marks a log record as signal-eligible.

    Usage:
        logger.warning(
            "VPN degraded after %d attempts", n,
            extra=axiom_signal("connections.vpn_degraded", provider="my-provider", attempts=n),
        )

    The SignalSink reads signal_event and signal_payload from the record dict
    and dispatches to the signal bus if the event_type is registered.
    """
    return {
        "signal_event": event_type,
        "signal_payload": payload,
    }


# ---------------------------------------------------------------------------
# get_logger — primary entry point for extension developers
# ---------------------------------------------------------------------------

# Module-level shared instances (initialised lazily on first get_logger call)
_ring_buffer: ForensicRingBuffer | None = None
_signal_sink: SignalSink | None = None
_handlers_installed: bool = False
_setup_lock = threading.Lock()


def _get_or_create_ring() -> ForensicRingBuffer:
    global _ring_buffer
    if _ring_buffer is None:
        _ring_buffer = ForensicRingBuffer(
            capacity=int(os.environ.get("NEUT_LOG_RING_CAPACITY", "2000"))
        )
    return _ring_buffer


def _get_or_create_signal_sink() -> SignalSink:
    global _signal_sink
    if _signal_sink is None:
        from axiom import REPO_ROOT
        registry_path = REPO_ROOT / "runtime" / "config" / "signal_event_registry.toml"
        registry = load_signal_registry(registry_path)
        _signal_sink = SignalSink(registry=registry)
    return _signal_sink


def _install_root_handlers() -> None:
    """Install Axiom handlers on the root axiom logger once."""
    global _handlers_installed
    with _setup_lock:
        if _handlers_installed:
            return

        root = logging.getLogger("axiom")
        if not root.handlers:
            root.setLevel(logging.DEBUG)  # handlers filter; root accepts all

        ring = _get_or_create_ring()
        ring.setFormatter(StructuredJsonFormatter())
        root.addHandler(ring)

        snapshot_dir = Path(
            os.environ.get("NEUT_LOG_FORENSIC_DIR", "runtime/logs/forensic")
        )
        incident_handler = IncidentSnapshotHandler(
            ring=ring,
            snapshot_dir=snapshot_dir,
            cooldown_s=float(os.environ.get("NEUT_LOG_SNAPSHOT_COOLDOWN_S", "30")),
        )
        root.addHandler(incident_handler)

        _handlers_installed = True


class _SignalLoggingHandler(logging.Handler):
    """Bridge between Python logging and SignalSink."""

    def emit(self, record: logging.LogRecord) -> None:
        if "signal_event" not in record.__dict__:
            return
        sink = _get_or_create_signal_sink()
        sink.write(record.__dict__)


def get_logger(name: str) -> logging.Logger:
    """Return a logger pre-wired to Axiom handlers.

    Equivalent to logging.getLogger(name) but ensures the ring buffer,
    incident snapshot handler, and signal sink are attached to the
    axiom logger hierarchy on first call.

    Always call as: logger = get_logger(__name__)
    """
    _install_root_handlers()

    logger = logging.getLogger(name)

    # Attach the signal bridge to this logger if not already present
    if not any(isinstance(h, _SignalLoggingHandler) for h in logger.handlers):
        bridge = _SignalLoggingHandler()
        bridge.setLevel(logging.DEBUG)
        logger.addHandler(bridge)

    return logger
