# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LogSink factory/provider pattern for the domain consumer.

Follows the same self-registration pattern as PublisherFactory:

    from axiom.infra.log_sinks import LogSinkFactory, LogSinkBase

    class MySink(LogSinkBase):
        accepts_ec = False

        def write(self, record: dict) -> None:
            ...  # deliver the record

    LogSinkFactory.register("my_sink", MySink)

Sinks are configured in runtime/config/logging.toml:

    [[log.sinks]]
    type = "file"
    enabled = true
    level = "INFO"
    path = "runtime/logs/system/neut.log"

    [[log.sinks]]
    type = "gcp"
    enabled = false
    level = "WARNING"
    project_id = "my-project"

LogSinkFactory.load_from_toml(path) reads the file and returns a list of
instantiated, enabled sinks. LogSinkDispatcher fan-outs a record to all of them.

Built-in sinks (self-registering on import):
    file    — locked JSONL append (uses locked_append_jsonl from infra.state)
    null    — no-op (used in standard mode)
    signal  — promotes signal_event-marked records to the signal bus

Cloud sinks (gcp, cloudwatch, syslog) are registered when their optional
dependencies are available. See docs/specs/spec-logging.md §13.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from pathlib import Path
from typing import Any

from axiom.infra.provider_base import ProviderBase, ensure_provider_uids
from axiom.infra.state import locked_append_jsonl

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LogSinkBase — contract every sink must implement
# ---------------------------------------------------------------------------

class LogSinkBase(ProviderBase):
    """Base class for all log sinks.

    Extends ProviderBase with log-sink-specific concerns: level filtering,
    EC record gating, and write-error isolation.

    Subclasses implement write(). The base class handles everything else
    so individual sinks stay simple.

    Config keys common to all sinks:
        name       Stable identifier for this sink instance (default: type name)
        level      Minimum log level (default: "INFO")
        enabled    Whether to activate (handled by factory before instantiation)
    """

    _log_prefix: str = "log_sink"
    accepts_ec: bool = False  # Override to True only for sinks that may hold EC data

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        level_name = config.get("level", "INFO").upper()
        self._min_level: int = getattr(logging, level_name, logging.INFO)

    def handle(self, record: dict) -> None:
        """Entry point called by LogSinkDispatcher. Applies guards then calls write()."""
        # Level filter
        if record.get("levelno", logging.DEBUG) < self._min_level:
            return
        # EC guard — cloud and signal sinks must not receive EC records
        if not self.accepts_ec and record.get("is_ec_record", False):
            return
        try:
            self.write(record)
        except Exception as exc:
            # A sink failure must never propagate — log and continue
            _log.error("LogSink %s.write() raised: %s", type(self).__name__, exc)

    @abstractmethod
    def write(self, record: dict) -> None:
        """Deliver the record. Called only after guards pass."""


# ---------------------------------------------------------------------------
# LogSinkFactory
# ---------------------------------------------------------------------------

class LogSinkFactory:
    """Central registry: maps sink type names → LogSinkBase subclasses.

    Self-registering pattern — sinks call LogSinkFactory.register() at module
    level so they become available as soon as they are imported.
    """

    _registry: dict[str, type[LogSinkBase]] = {}

    @classmethod
    def register(cls, type_name: str, sink_cls: type[LogSinkBase]) -> None:
        """Register a sink class under a config type name (e.g. "file", "gcp")."""
        cls._registry[type_name] = sink_cls

    @classmethod
    def create(cls, type_name: str, config: dict[str, Any]) -> LogSinkBase:
        """Instantiate a sink by type name.

        Raises ValueError for unknown types — caller decides whether to skip or fail.
        """
        if type_name not in cls._registry:
            raise ValueError(
                f"Unknown log sink type: '{type_name}'. "
                f"Available: {sorted(cls._registry)}"
            )
        return cls._registry[type_name](config)

    @classmethod
    def available(cls) -> list[str]:
        """List all registered sink type names."""
        return list(cls._registry.keys())

    @classmethod
    def load_from_toml(cls, path: Path) -> list[LogSinkBase]:
        """Load and instantiate enabled sinks from a logging.toml file.

        Returns an empty list if the file is missing or has no sinks.
        Unknown sink types are skipped with a WARNING — they do not abort startup.
        """
        if not Path(path).exists():
            return []
        try:
            from axiom.infra.toml_compat import load_toml
        except ImportError:
            _log.warning("No TOML parser — logging.toml not loaded. pip install tomli")
            return []

        try:
            data = load_toml(path)
        except Exception as exc:
            _log.warning("Failed to parse %s: %s", path, exc)
            return []

        # Back-fill any missing uids before instantiating — writes to config file
        ensure_provider_uids(path, table_key="log.sinks")

        sinks: list[LogSinkBase] = []
        seen_uids: dict[str, str] = {}  # uid → display name of first occurrence
        for entry in data.get("log", {}).get("sinks", []):
            if not entry.get("enabled", True):
                continue
            uid = entry.get("uid", "")
            name = entry.get("name", entry.get("type", "<unnamed>"))
            if uid and uid in seen_uids:
                _log.error(
                    "Skipping log sink '%s': uid '%s' already used by '%s' in %s. "
                    "Assign a unique uid to resolve the conflict.",
                    name, uid, seen_uids[uid], path,
                )
                continue
            if uid:
                seen_uids[uid] = name
            type_name = entry.get("type", "")
            try:
                sinks.append(cls.create(type_name, entry))
            except ValueError:
                _log.warning(
                    "Skipping unknown log sink type '%s' in %s. "
                    "Is the sink's module imported?",
                    type_name, path,
                )
        return sinks

    @classmethod
    def reset(cls) -> None:
        """Clear registry and re-register built-in sinks — for test isolation."""
        cls._registry = {}
        cls._register_builtins()

    @classmethod
    def _register_builtins(cls) -> None:
        """Re-register the sinks that ship with the platform."""
        cls._registry.update(_BUILTIN_SINKS)


# ---------------------------------------------------------------------------
# LogSinkDispatcher — fan-out to all active sinks
# ---------------------------------------------------------------------------

class LogSinkDispatcher:
    """Delivers a record to every active sink, isolating failures between them."""

    def __init__(self, sinks: list[LogSinkBase]) -> None:
        self._sinks = sinks

    def dispatch(self, record: dict) -> None:
        for sink in self._sinks:
            sink.handle(record)

    def add(self, sink: LogSinkBase) -> None:
        self._sinks.append(sink)


# ---------------------------------------------------------------------------
# Built-in sink: NullSink
# ---------------------------------------------------------------------------

class NullSink(LogSinkBase):
    """No-op sink. Used in standard mode where EC audit is inactive."""

    accepts_ec = False

    def write(self, record: dict) -> None:
        pass


# Populated as each built-in class is defined; used by reset() to restore them.
_BUILTIN_SINKS: dict[str, type[LogSinkBase]] = {}

LogSinkFactory.register("null", NullSink)
_BUILTIN_SINKS["null"] = NullSink


# ---------------------------------------------------------------------------
# Built-in sink: FileSink
# ---------------------------------------------------------------------------

class FileSink(LogSinkBase):
    """Appends structured JSON records to a JSONL file.

    Uses locked_append_jsonl (ADR-011) for multi-process safety.

    Config keys:
        path    (required) Path to the JSONL output file
        level   Minimum level (default: "INFO")
    """

    accepts_ec = False  # File sinks are not cleared on a schedule; don't put EC there

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        if "path" not in config:
            raise ValueError("FileSink requires 'path' in config")
        self._path = Path(config["path"])

    def write(self, record: dict) -> None:
        locked_append_jsonl(self._path, record)


LogSinkFactory.register("file", FileSink)
_BUILTIN_SINKS["file"] = FileSink


# ---------------------------------------------------------------------------
# Built-in sink: SignalSink (wraps infra.axiom_logging.SignalSink)
# ---------------------------------------------------------------------------

class _SignalSinkAdapter(LogSinkBase):
    """Promotes signal_event-marked records to the signal bus.

    Wraps the SignalSink implementation from axiom_logging to fit the
    LogSinkBase handle() / write() contract.

    Config keys:
        registry_path   Path to signal_event_registry.toml (optional;
                        defaults to runtime/config/signal_event_registry.toml)
    """

    accepts_ec = False  # EC records must never reach the signal bus

    def __init__(self, config: dict[str, Any]) -> None:
        # Signal sink ignores level — it filters on event_type identity only.
        # Force min_level to DEBUG so handle() never rejects on level.
        config = {**config, "level": "DEBUG"}
        super().__init__(config)

        from axiom.infra.axiom_logging import SignalSink, load_signal_registry

        if "registry_path" in config:
            registry_path = Path(config["registry_path"])
        else:
            from axiom import REPO_ROOT
            registry_path = REPO_ROOT / "runtime" / "config" / "signal_event_registry.toml"

        registry = load_signal_registry(registry_path)
        self._sink = SignalSink(registry=registry)

    def write(self, record: dict) -> None:
        self._sink.write(record)


LogSinkFactory.register("signal", _SignalSinkAdapter)
_BUILTIN_SINKS["signal"] = _SignalSinkAdapter
