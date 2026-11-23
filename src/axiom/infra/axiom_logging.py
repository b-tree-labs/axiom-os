# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom structured logging infrastructure.

Provides:
    get_logger(name)          — standard Python logger pre-wired to all Axiom handlers
    axiom_bus_event(event, **kw) — build an extra= dict to forward a log record to the event bus
                                   (axiom_signal is the same, under its older keys, for one release)
    StructuredJsonFormatter   — serialises log records to JSON with trace_id, session_id, exc_info
    ForensicRingBuffer        — in-memory circular buffer of recent DEBUG+ records; no I/O until flushed
    IncidentSnapshotHandler   — auto-flushes ring buffer to disk on ERROR/CRITICAL
    BusForwardSink            — forwards explicitly-marked records to the event bus (was SignalSink)
    load_bus_event_registry   — loads registered bus event types: shipped plus a site's own (was load_signal_registry)

Extension developer quick-start:

    from axiom.infra.axiom_logging import get_logger, axiom_bus_event

    logger = get_logger(__name__)

    logger.info("Signal batch ingested", extra={"count": 42})
    logger.warning("VPN degraded", extra=axiom_bus_event(
        "connections.vpn_degraded",
        provider="my-provider",
        attempts=3,
    ))

See docs/specs/spec-logging.md §19 for the full extension developer guide.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback as _tb
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.brand_migration import getenv
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
# BusForwardSink (formerly SignalSink)
# ---------------------------------------------------------------------------

#: Keys on a log record that mark it for the bus. The first of each pair is the current name;
#: the second is read for one release (ADR-265: expand first).
_EVENT_KEYS = ("bus_event", "signal_event")
_PAYLOAD_KEYS = ("bus_payload", "signal_payload")

#: Set while a forwarder is publishing on this thread: a subscriber that logs a marked record
#: from inside its handler must not publish it again, or the bus loops.
_forwarding = threading.local()
_warned_failures: set[str] = set()


class BusForwardSink:
    """Forwards explicitly-marked log records to the event bus.

    Only records marked with ``bus_event`` (set via :func:`axiom_bus_event`; the older
    ``signal_event`` key is still read) are forwarded. Records without one are ignored with zero
    overhead. An event type not in the registry is logged as an error and dropped. EC records
    (``is_ec_record=True``) are never forwarded (hardcoded, not config).

    Published on the process default bus (``axiom.infra.bus.get_default_eventbus``) with the
    source ``log``; the payload carries ``_log_level`` and ``_log_ts``. A bus that fails is
    reported once per event type, and the log call is never affected.
    """

    accepts_ec: bool = False

    def __init__(self, registry: set[str], accepts_ec: bool = False) -> None:
        self._registry = registry
        self.accepts_ec = accepts_ec

    def write(self, record_dict: dict) -> None:
        event = next((record_dict[k] for k in _EVENT_KEYS if record_dict.get(k)), None)
        if not event:
            return  # fast path: most records

        # EC records never go to the bus
        if not self.accepts_ec and record_dict.get("is_ec_record"):
            return

        if event not in self._registry:
            _internal.error(
                "Unregistered bus event '%s' blocked at the forwarder's gate. "
                "Add it to bus_event_registry.toml before use.",
                event,
            )
            return

        raw = next((record_dict[k] for k in _PAYLOAD_KEYS if record_dict.get(k)), None)
        payload: dict = dict(raw or {})
        payload["_log_level"] = record_dict.get("levelname", "")
        payload["_log_ts"] = record_dict.get("created", "")
        self._emit(event, payload)

    def _emit(self, event_type: str, payload: dict) -> None:
        """Publish on the default bus. Replaced in tests; overrideable in prod."""
        if getattr(_forwarding, "active", False):
            return  # a subscriber's own marked record: not published again
        _forwarding.active = True
        try:
            from axiom.infra.bus import get_default_eventbus

            get_default_eventbus().publish(event_type, payload, source="log")
        except Exception as exc:  # noqa: BLE001 - the log call is never affected
            if event_type not in _warned_failures:
                _warned_failures.add(event_type)
                _internal.warning("Bus event '%s' could not be published: %s", event_type, exc)
        finally:
            _forwarding.active = False


#: The name before ADR-265, kept for one release.
SignalSink = BusForwardSink


# ---------------------------------------------------------------------------
# Bus event registry loader
# ---------------------------------------------------------------------------

_SHIPPED_REGISTRY = Path(__file__).with_name("bus_event_registry.toml")
_LEGACY_REGISTRY_NAME = "signal_event_registry.toml"


def _read_event_types(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        from axiom.infra.toml_compat import load_toml
        data = load_toml(path)
        return {e["event_type"] for e in data.get("events", []) if "event_type" in e}
    except Exception as exc:
        _internal.warning("Failed to load bus event registry from %s: %s", path, exc)
        return set()


def shipped_bus_event_registry() -> set[str]:
    """The event types the platform's own code marks, from the file that ships with the package."""
    return _read_event_types(_SHIPPED_REGISTRY)


def load_bus_event_registry(path: Path) -> set[str]:
    """The registered bus event types: the shipped ones plus a site's own.

    ``path`` is the site's file (``bus_event_registry.toml``); a file of the older name
    ``signal_event_registry.toml`` beside it is read too, for one release. A missing or
    unparseable site file adds nothing (a warning for the unparseable one); the shipped events
    are always there.
    """
    path = Path(path)
    return (
        shipped_bus_event_registry()
        | _read_event_types(path)
        | _read_event_types(path.with_name(_LEGACY_REGISTRY_NAME))
    )


def load_signal_registry(path: Path) -> set[str]:
    """The loader before ADR-265, kept for one release as it was: only the file it is given.

    New code calls :func:`load_bus_event_registry`, which adds the events the platform ships.
    """
    return _read_event_types(Path(path))


# ---------------------------------------------------------------------------
# axiom_signal helper
# ---------------------------------------------------------------------------

def axiom_bus_event(event_type: str, **payload: Any) -> dict:
    """Build an extra= dict that marks a log record for the event bus.

    Usage:
        logger.warning(
            "VPN degraded after %d attempts", n,
            extra=axiom_bus_event("connections.vpn_degraded", provider="my-provider", attempts=n),
        )

    The BusForwardSink reads ``bus_event`` and ``bus_payload`` from the record and publishes
    them on the default bus if the event type is registered (``bus_event_registry.toml``).
    """
    return {
        "bus_event": event_type,
        "bus_payload": payload,
    }


def axiom_signal(event_type: str, **payload: Any) -> dict:
    """The name before ADR-265, kept for one release: marks a record with the older keys,
    which the sink still reads. New code calls :func:`axiom_bus_event`."""
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
            capacity=int(getenv("AXIOM_LOG_RING_CAPACITY", "2000"))
        )
    return _ring_buffer


def _get_or_create_bus_sink() -> BusForwardSink:
    global _signal_sink
    if _signal_sink is None:
        from axiom import REPO_ROOT
        registry_path = REPO_ROOT / "runtime" / "config" / "bus_event_registry.toml"
        registry = load_bus_event_registry(registry_path)
        _signal_sink = BusForwardSink(registry=registry)
    return _signal_sink


#: The name before ADR-265, kept for one release.
_get_or_create_signal_sink = _get_or_create_bus_sink


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
            getenv("AXIOM_LOG_FORENSIC_DIR", "runtime/logs/forensic")
        )
        incident_handler = IncidentSnapshotHandler(
            ring=ring,
            snapshot_dir=snapshot_dir,
            cooldown_s=float(getenv("AXIOM_LOG_SNAPSHOT_COOLDOWN_S", "30")),
        )
        root.addHandler(incident_handler)

        _handlers_installed = True


class _SignalLoggingHandler(logging.Handler):
    """Bridge between Python logging and the BusForwardSink."""

    def emit(self, record: logging.LogRecord) -> None:
        if not any(key in record.__dict__ for key in _EVENT_KEYS):
            return
        sink = _get_or_create_bus_sink()
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
