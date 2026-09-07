# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signal Source and Subscriber Registry for the signal agent.

Provides auto-discovery of signal sources (extractors) and subscribers (sinks),
dynamic inbox directory management, and easy addition of new signal types.

Architecture:
    Sources (extractors) → Pipeline → Subscribers (sinks)

    Sources: GitHub, Teams Chat, Voice, Outlook, GCal, GitLab, etc.
    Subscribers: Briefings, PRD updates, Slack, Email digests, Teams posts, etc.

Usage:
    from axiom.extensions.builtins.signals.registry import get_registry

    registry = get_registry()

    # List all sources
    for source in registry.sources:
        print(f"{source.name}: {source.description}")

    # Get inbox path for a source
    inbox = registry.get_inbox_path("github")

    # List all subscribers
    for sub in registry.subscribers:
        print(f"{sub.name}: {sub.description}")

    # Publish to subscribers
    registry.publish(signals, ["briefing", "slack"])

Adding new sources:
    1. Create extractor in extractors/ (implement BaseExtractor)
    2. Add @register_source decorator with metadata
    3. Directory is auto-created in inbox/raw/{source_name}/

Adding new subscribers:
    1. Create subscriber in subscribers/ (implement BaseSubscriber)
    2. Add @register_subscriber decorator with metadata
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extractors.base import BaseExtractor
    from .models import Signal


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import atomic_write

_RUNTIME_DIR = _REPO_ROOT / "runtime"
INBOX_DIR = _RUNTIME_DIR / "inbox"
RAW_DIR = INBOX_DIR / "raw"
PROCESSED_DIR = INBOX_DIR / "processed"
STATE_DIR = INBOX_DIR / "state"
CACHE_DIR = INBOX_DIR / "cache"


class SourceType(str, Enum):
    """Type of signal source."""
    PUSH = "push"       # Data is pushed to us (voice uploads, webhooks)
    PULL = "pull"       # We fetch data (GitHub API, Teams API)
    HYBRID = "hybrid"   # Both (can receive webhooks AND poll)


class SourceStatus(str, Enum):
    """Current status of a source."""
    ACTIVE = "active"           # Configured and working
    UNCONFIGURED = "unconfigured"  # Needs setup (API keys, etc.)
    ERROR = "error"             # Last fetch failed
    DISABLED = "disabled"       # Manually disabled


class SubscriberType(str, Enum):
    """Type of subscriber."""
    DIGEST = "digest"       # Aggregates signals over time (briefings)
    REALTIME = "realtime"   # Immediate notification (Slack, email)
    STORAGE = "storage"     # Archives signals (database, files)
    SYNC = "sync"           # Syncs to external system (PRD, Notion)


@dataclass
class SourceMetadata:
    """Metadata about a signal source."""

    name: str                   # Unique identifier (e.g., "github")
    description: str            # Human-readable description
    source_type: SourceType     # push/pull/hybrid

    # Inbox configuration
    inbox_subdir: str = ""      # Subdirectory in inbox/raw/ (defaults to name)
    file_patterns: list[str] = field(default_factory=list)  # Glob patterns for files

    # Configuration requirements
    requires_auth: bool = False
    auth_env_vars: list[str] = field(default_factory=list)  # e.g., ["GITHUB_TOKEN"]
    config_file: str = ""       # Path to config file if needed

    # Fetch configuration (for PULL sources)
    default_poll_interval: int = 3600  # Seconds between fetches
    supports_webhook: bool = False

    # Display
    icon: str = "📥"            # Emoji for CLI/UI
    category: str = "general"   # For grouping in UI

    # Runtime state (not persisted)
    status: SourceStatus = SourceStatus.UNCONFIGURED
    last_fetch: str | None = None
    last_error: str | None = None
    signal_count: int = 0

    def __post_init__(self):
        if not self.inbox_subdir:
            self.inbox_subdir = self.name

    @property
    def inbox_path(self) -> Path:
        """Full path to inbox directory for this source."""
        return RAW_DIR / self.inbox_subdir

    def ensure_inbox(self) -> Path:
        """Create inbox directory if it doesn't exist."""
        self.inbox_path.mkdir(parents=True, exist_ok=True)
        return self.inbox_path

    def check_configured(self) -> tuple[bool, str]:
        """Check if source is properly configured."""
        import os

        missing = []
        for var in self.auth_env_vars:
            if not os.environ.get(var):
                missing.append(var)

        if missing:
            return False, f"Missing env vars: {', '.join(missing)}"

        if self.config_file:
            config_path = STATE_DIR / self.config_file
            if not config_path.exists():
                return False, f"Missing config: {self.config_file}"

        return True, "OK"


@dataclass
class SubscriberMetadata:
    """Metadata about a signal subscriber."""

    name: str                   # Unique identifier (e.g., "briefing")
    description: str            # Human-readable description
    subscriber_type: SubscriberType

    # Configuration
    requires_auth: bool = False
    auth_env_vars: list[str] = field(default_factory=list)
    config_file: str = ""

    # Filtering
    signal_types: list[str] = field(default_factory=list)  # Empty = all types
    source_filter: list[str] = field(default_factory=list)  # Empty = all sources
    min_confidence: float = 0.0

    # Scheduling (for DIGEST type)
    schedule: str = ""          # Cron expression or "realtime"

    # Display
    icon: str = "📤"
    category: str = "general"

    # Runtime
    status: SourceStatus = SourceStatus.UNCONFIGURED
    last_publish: str | None = None
    last_error: str | None = None
    publish_count: int = 0


# ---------------------------------------------------------------------------
# Registry implementation
# ---------------------------------------------------------------------------

class SignalRegistry:
    """Central registry for signal sources and subscribers."""

    _instance: SignalRegistry | None = None

    def __init__(self):
        self._sources: dict[str, tuple[SourceMetadata, type[BaseExtractor]]] = {}
        self._subscribers: dict[str, tuple[SubscriberMetadata, Callable]] = {}
        self._state_file = STATE_DIR / "registry_state.json"
        self._discovered = False

    @classmethod
    def get_instance(cls) -> SignalRegistry:
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = SignalRegistry()
        return cls._instance

    # ---------------------------------------------------------------------------
    # Source management
    # ---------------------------------------------------------------------------

    def register_source(
        self,
        metadata: SourceMetadata,
        extractor_class: type[BaseExtractor],
    ) -> None:
        """Register a signal source."""
        self._sources[metadata.name] = (metadata, extractor_class)
        metadata.ensure_inbox()

    def get_source(self, name: str) -> tuple[SourceMetadata, type[BaseExtractor]] | None:
        """Get a source by name."""
        self._ensure_discovered()
        return self._sources.get(name)

    @property
    def sources(self) -> list[SourceMetadata]:
        """Get all registered sources."""
        self._ensure_discovered()
        return [meta for meta, _ in self._sources.values()]

    def get_extractor(self, name: str) -> BaseExtractor | None:
        """Get an instantiated extractor for a source."""
        source = self.get_source(name)
        if source:
            _, extractor_class = source
            return extractor_class()
        return None

    def get_inbox_path(self, source_name: str) -> Path | None:
        """Get the inbox path for a source."""
        source = self.get_source(source_name)
        if source:
            return source[0].inbox_path
        return None

    def list_source_files(self, source_name: str) -> list[Path]:
        """List files in a source's inbox."""
        source = self.get_source(source_name)
        if not source:
            return []

        meta, _ = source
        inbox = meta.inbox_path
        if not inbox.exists():
            return []

        files = []
        patterns = meta.file_patterns or ["*"]
        for pattern in patterns:
            files.extend(inbox.glob(pattern))

        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    # ---------------------------------------------------------------------------
    # Subscriber management
    # ---------------------------------------------------------------------------

    def register_subscriber(
        self,
        metadata: SubscriberMetadata,
        handler: Callable,
    ) -> None:
        """Register a signal subscriber."""
        self._subscribers[metadata.name] = (metadata, handler)

    def get_subscriber(self, name: str) -> tuple[SubscriberMetadata, Callable] | None:
        """Get a subscriber by name."""
        self._ensure_discovered()
        return self._subscribers.get(name)

    @property
    def subscribers(self) -> list[SubscriberMetadata]:
        """Get all registered subscribers."""
        self._ensure_discovered()
        return [meta for meta, _ in self._subscribers.values()]

    def publish(
        self,
        signals: list[Signal],
        subscriber_names: list[str] | None = None,
    ) -> dict[str, bool]:
        """Publish signals to subscribers.

        Args:
            signals: Signals to publish
            subscriber_names: Specific subscribers (None = all matching)

        Returns:
            Dict of subscriber_name -> success
        """
        self._ensure_discovered()
        results = {}

        targets = subscriber_names or list(self._subscribers.keys())

        for name in targets:
            sub = self._subscribers.get(name)
            if not sub:
                results[name] = False
                continue

            meta, handler = sub

            # Filter signals for this subscriber
            filtered = self._filter_signals(signals, meta)

            if not filtered:
                results[name] = True  # Nothing to publish
                continue

            try:
                handler(filtered)
                meta.last_publish = datetime.now(UTC).isoformat()
                meta.publish_count += len(filtered)
                results[name] = True
            except Exception as e:
                meta.last_error = str(e)
                results[name] = False

        return results

    def _filter_signals(
        self,
        signals: list[Signal],
        meta: SubscriberMetadata,
    ) -> list[Signal]:
        """Filter signals for a subscriber based on its metadata."""
        filtered = []

        for sig in signals:
            # Check signal type
            if meta.signal_types and sig.signal_type not in meta.signal_types:
                continue

            # Check source
            if meta.source_filter and sig.source not in meta.source_filter:
                continue

            # Check confidence
            if sig.confidence < meta.min_confidence:
                continue

            filtered.append(sig)

        return filtered

    # ---------------------------------------------------------------------------
    # Auto-discovery
    # ---------------------------------------------------------------------------

    def _ensure_discovered(self) -> None:
        """Ensure extractors and subscribers have been discovered."""
        if self._discovered:
            return

        self._discover_extractors()
        self._discover_subscribers()
        self._load_state()
        self._discovered = True

    def _discover_extractors(self) -> None:
        """Auto-discover extractors in the extractors package."""
        try:
            from axiom.extensions.builtins.signals import extractors

            for _importer, modname, _ispkg in pkgutil.iter_modules(extractors.__path__):
                if modname.startswith("_") or modname == "base":
                    continue

                try:
                    module = importlib.import_module(
                        f"axiom.extensions.builtins.signals.extractors.{modname}"
                    )

                    # Look for registered sources in module
                    if hasattr(module, "_REGISTERED_SOURCES"):
                        for meta, cls in module._REGISTERED_SOURCES:
                            self._sources[meta.name] = (meta, cls)
                            meta.ensure_inbox()

                except Exception as e:
                    print(f"Warning: Failed to load extractor {modname}: {e}")

        except ImportError:
            pass

    def _discover_subscribers(self) -> None:
        """Auto-discover subscribers in the subscribers package."""
        try:
            # Import known subscribers
            self._register_builtin_subscribers()
        except ImportError:
            pass

    def _register_builtin_subscribers(self) -> None:
        """Register built-in subscribers."""
        # Briefing subscriber
        try:
            from .briefing import BriefingService  # noqa: F401

            meta = SubscriberMetadata(
                name="briefing",
                description="Daily/weekly briefing digest",
                subscriber_type=SubscriberType.DIGEST,
                schedule="0 7 * * *",  # 7 AM daily
                icon="📋",
                category="digest",
            )

            def briefing_handler(signals: list[Signal]) -> None:
                # BriefingService loads signals from processed dir on demand
                # This handler is for future real-time briefing updates
                pass

            self._subscribers["briefing"] = (meta, briefing_handler)
        except ImportError:
            pass

        # PRD sync subscriber
        try:
            from .smart_router import SmartRouter

            meta = SubscriberMetadata(
                name="prd_sync",
                description="Sync signals to PRD suggestions",
                subscriber_type=SubscriberType.SYNC,
                signal_types=["progress", "decision", "blocker"],
                min_confidence=0.6,
                icon="📝",
                category="sync",
            )

            def prd_handler(signals: list[Signal]) -> None:
                router = SmartRouter()
                # Match signals to PRDs and queue suggestions
                matches = router.match_to_prds(signals)
                if matches:
                    router.suggest(matches)

            self._subscribers["prd_sync"] = (meta, prd_handler)
        except ImportError:
            pass

    # ---------------------------------------------------------------------------
    # State persistence
    # ---------------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load persisted state (last fetch times, errors, etc.)."""
        if not self._state_file.exists():
            return

        try:
            state = json.loads(self._state_file.read_text())

            for name, source_state in state.get("sources", {}).items():
                if name in self._sources:
                    meta, _ = self._sources[name]
                    meta.last_fetch = source_state.get("last_fetch")
                    meta.last_error = source_state.get("last_error")
                    meta.signal_count = source_state.get("signal_count", 0)
                    meta.status = SourceStatus(source_state.get("status", "unconfigured"))

            for name, sub_state in state.get("subscribers", {}).items():
                if name in self._subscribers:
                    meta, _ = self._subscribers[name]
                    meta.last_publish = sub_state.get("last_publish")
                    meta.last_error = sub_state.get("last_error")
                    meta.publish_count = sub_state.get("publish_count", 0)

        except Exception:
            pass

    def save_state(self) -> None:
        """Persist registry state."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        state = {
            "updated_at": datetime.now(UTC).isoformat(),
            "sources": {},
            "subscribers": {},
        }

        for name, (meta, _) in self._sources.items():
            state["sources"][name] = {
                "last_fetch": meta.last_fetch,
                "last_error": meta.last_error,
                "signal_count": meta.signal_count,
                "status": meta.status.value,
            }

        for name, (meta, _) in self._subscribers.items():
            state["subscribers"][name] = {
                "last_publish": meta.last_publish,
                "last_error": meta.last_error,
                "publish_count": meta.publish_count,
            }

        atomic_write(self._state_file, state)

    # ---------------------------------------------------------------------------
    # Status and diagnostics
    # ---------------------------------------------------------------------------

    def get_status(self) -> dict:
        """Get overall registry status."""
        self._ensure_discovered()

        sources_by_status = {}
        for meta in self.sources:
            configured, _ = meta.check_configured()
            status = "active" if configured else "unconfigured"
            sources_by_status.setdefault(status, []).append(meta.name)

        return {
            "total_sources": len(self._sources),
            "total_subscribers": len(self._subscribers),
            "sources_by_status": sources_by_status,
            "inbox_directories": [
                str(meta.inbox_path) for meta in self.sources
            ],
        }

    def ensure_all_inboxes(self) -> list[Path]:
        """Create all inbox directories."""
        self._ensure_discovered()
        paths = []
        for meta in self.sources:
            path = meta.ensure_inbox()
            paths.append(path)
        return paths


# ---------------------------------------------------------------------------
# Decorators for easy registration
# ---------------------------------------------------------------------------

def register_source(
    name: str,
    description: str,
    source_type: SourceType = SourceType.PULL,
    **kwargs,
) -> Callable:
    """Decorator to register an extractor as a signal source.

    Usage:
        @register_source(
            name="github",
            description="GitHub repository activity",
            source_type=SourceType.PULL,
            requires_auth=True,
            auth_env_vars=["GITHUB_TOKEN"],
        )
        class GitHubExtractor(BaseExtractor):
            ...
    """
    def decorator(cls: type[BaseExtractor]) -> type[BaseExtractor]:
        meta = SourceMetadata(
            name=name,
            description=description,
            source_type=source_type,
            **kwargs,
        )

        # Store for auto-discovery
        mod = importlib.import_module(cls.__module__)
        if not hasattr(mod, "_REGISTERED_SOURCES"):
            mod._REGISTERED_SOURCES = []  # type: ignore[attr-defined]
        mod._REGISTERED_SOURCES.append((meta, cls))  # type: ignore[attr-defined]

        return cls

    return decorator


def register_subscriber(
    name: str,
    description: str,
    subscriber_type: SubscriberType = SubscriberType.DIGEST,
    **kwargs,
) -> Callable:
    """Decorator to register a function as a signal subscriber.

    Usage:
        @register_subscriber(
            name="slack",
            description="Post signals to Slack",
            subscriber_type=SubscriberType.REALTIME,
        )
        def slack_handler(signals: list[Signal]) -> None:
            ...
    """
    def decorator(func: Callable) -> Callable:
        meta = SubscriberMetadata(
            name=name,
            description=description,
            subscriber_type=subscriber_type,
            **kwargs,
        )

        registry = SignalRegistry.get_instance()
        registry.register_subscriber(meta, func)

        return func

    return decorator


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def get_registry() -> SignalRegistry:
    """Get the singleton registry instance."""
    return SignalRegistry.get_instance()


def list_sources() -> list[SourceMetadata]:
    """List all registered sources."""
    return get_registry().sources


def list_subscribers() -> list[SubscriberMetadata]:
    """List all registered subscribers."""
    return get_registry().subscribers


def ensure_inboxes() -> list[Path]:
    """Ensure all inbox directories exist."""
    return get_registry().ensure_all_inboxes()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def print_sources_table() -> None:
    """Print a table of all sources."""
    registry = get_registry()
    sources = registry.sources

    if not sources:
        print("No sources registered.")
        return

    print("\n=== Signal Sources ===\n")
    print(f"{'Icon':<4} {'Name':<15} {'Type':<8} {'Status':<12} {'Description'}")
    print("-" * 70)

    for src in sorted(sources, key=lambda s: s.name):
        configured, msg = src.check_configured()
        status = "✓ active" if configured else f"✗ {msg[:20]}"
        print(f"{src.icon:<4} {src.name:<15} {src.source_type.value:<8} {status:<12} {src.description}")

    print()


def print_subscribers_table() -> None:
    """Print a table of all subscribers."""
    registry = get_registry()
    subs = registry.subscribers

    if not subs:
        print("No subscribers registered.")
        return

    print("\n=== Signal Subscribers ===\n")
    print(f"{'Icon':<4} {'Name':<15} {'Type':<10} {'Schedule':<15} {'Description'}")
    print("-" * 75)

    for sub in sorted(subs, key=lambda s: s.name):
        schedule = sub.schedule or "on-demand"
        print(f"{sub.icon:<4} {sub.name:<15} {sub.subscriber_type.value:<10} {schedule:<15} {sub.description}")

    print()


def print_inbox_status() -> None:
    """Print status of all inbox directories."""
    registry = get_registry()
    sources = registry.sources

    print("\n=== Inbox Status ===\n")

    for src in sorted(sources, key=lambda s: s.name):
        inbox = src.inbox_path
        exists = inbox.exists()

        if exists:
            files = list(inbox.iterdir())
            file_count = len([f for f in files if f.is_file()])
            print(f"{src.icon} {src.name}: {file_count} files in {inbox}")
        else:
            print(f"{src.icon} {src.name}: (no inbox)")

    print()
