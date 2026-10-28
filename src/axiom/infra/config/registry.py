# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Schema registry + value store + locks for `axiom.infra.config`.

Per AEOS §2.13 (the state-externalization rule) + ADR-058: extensions
declare configurable fields with types + classification + lock policy;
the registry validates writes; the value store is the single source of
truth that ``get_value`` reads from.

Locks are a thin compose-point. The full lock authority story (who may
unlock, signed capabilities, federation handoff) lives in the parallel
keystore session's work. This module exposes the *predicate* — "is this
key locked, and what capability is required to override?" — and leaves
the cryptographic enforcement to that layer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaError(ValueError):
    """A schema or value rejected by validation."""


class LockedConfigError(PermissionError):
    """Raised when a write hits a locked key without override authority."""

    def __init__(self, key: str, lock: "Lock") -> None:
        super().__init__(
            f"config key {key!r} is locked by {lock.locked_by} "
            f"(since {lock.locked_at.isoformat()}, reason={lock.reason!r}); "
            "override requires the capability declared in the lock"
        )
        self.key = key
        self.lock = lock


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One configurable field's schema."""

    name: str
    """Fully-qualified key, e.g. ``"expman.sla_active_hours"``."""

    type: type
    """The Python type the value must satisfy at write time."""

    default: Any = None

    classification: str = "internal"
    """One of public / internal / regulated / controlled
    (matches axiom.governance.Classification). Per AEOS §2.13, the
    classification of a config value gates which channels can carry
    its change events + which actors may write."""

    lockable: bool = True
    """If False, ``lock(key)`` rejects regardless of caller authority."""

    description: str = ""

    def validate(self, value: Any) -> None:
        if not isinstance(value, self.type):
            raise SchemaError(
                f"config {self.name!r}: expected {self.type.__name__}, "
                f"got {type(value).__name__}"
            )


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lock:
    """An active lock on a config key.

    Per the AEOS §2.14 normative requirement: a locked key is the
    *system's* commitment that this value stays put until an explicit
    authority unlocks it. Reactor environments lean on this.
    """

    key: str
    locked_by: str
    """A Principal handle or capability id — opaque to this module;
    interpreted by the authority that wired the lock."""

    locked_at: datetime
    reason: str
    """Human-readable rationale; ends up in receipts + lock-status outputs."""

    override_capability_pattern: str | None = None
    """An IntentPattern-style string describing which capabilities may
    override (e.g. ``"config.unlock"``, ``"compliance.override_lock"``).
    ``None`` means "no override path beyond the locked_by". The keystore
    session enforces the actual cryptographic check; this module just
    surfaces the requirement."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ChangeRecord:
    """One value change. Hands off to the receipt writer."""

    key: str
    old_value: Any
    new_value: Any
    actor: str
    """Principal handle or "(unknown)" — populated by the caller."""
    source: str
    """Where the change came from: ``"file:<path>"``, ``"api"``, etc."""
    changed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class ConfigRegistry:
    """Thread-safe schema + value store + lock state.

    Singleton in production (via ``axiom.infra.config.get_registry``);
    tests construct instances directly.
    """

    def __init__(self) -> None:
        self._fields: dict[str, Field] = {}
        self._values: dict[str, Any] = {}
        self._locks: dict[str, Lock] = {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[ChangeRecord], None]] = []

    # ------- schema -------

    def register(self, *fields: Field) -> None:
        """Register one or more fields. Re-registration is allowed if the
        type + classification + lockable flag match; otherwise raises."""
        with self._lock:
            for f in fields:
                existing = self._fields.get(f.name)
                if existing is not None:
                    if (
                        existing.type is f.type
                        and existing.classification == f.classification
                        and existing.lockable == f.lockable
                    ):
                        continue
                    raise SchemaError(
                        f"config {f.name!r}: re-registered with "
                        "incompatible schema"
                    )
                self._fields[f.name] = f
                if f.name not in self._values:
                    self._values[f.name] = f.default

    def fields(self) -> Iterable[Field]:
        with self._lock:
            return list(self._fields.values())

    # ------- values -------

    def get(self, key: str, *, default: Any = None) -> Any:
        with self._lock:
            if key not in self._fields:
                return default
            v = self._values.get(key)
            if v is None:
                return self._fields[key].default if default is None else default
            return v

    def write(
        self,
        key: str,
        value: Any,
        *,
        actor: str = "(unknown)",
        source: str = "api",
        override_capability: object | None = None,
    ) -> ChangeRecord:
        """Validate + lock-check + commit the new value.

        Raises ``SchemaError`` for unknown / wrongly-typed values, and
        ``LockedConfigError`` for locked keys absent an override.
        """
        with self._lock:
            field_obj = self._fields.get(key)
            if field_obj is None:
                raise SchemaError(f"config {key!r}: not registered")
            field_obj.validate(value)

            lock = self._locks.get(key)
            if lock is not None and override_capability is None:
                raise LockedConfigError(key, lock)

            old = self._values.get(key)
            if old == value:
                return ChangeRecord(
                    key=key,
                    old_value=old,
                    new_value=value,
                    actor=actor,
                    source=source,
                )
            self._values[key] = value
            record = ChangeRecord(
                key=key,
                old_value=old,
                new_value=value,
                actor=actor,
                source=source,
            )
            # Snapshot listeners under the lock; invoke outside.
            listeners = list(self._listeners)

        for fn in listeners:
            try:
                fn(record)
            except Exception:
                # A failing listener does not block other listeners
                # or the change itself; hygiene watchers see the noise.
                pass

        return record

    # ------- locks -------

    def lock_key(self, lock: Lock) -> None:
        with self._lock:
            field_obj = self._fields.get(lock.key)
            if field_obj is None:
                raise SchemaError(
                    f"config {lock.key!r}: not registered; cannot lock"
                )
            if not field_obj.lockable:
                raise SchemaError(
                    f"config {lock.key!r}: field declared lockable=False"
                )
            self._locks[lock.key] = lock

    def unlock_key(self, key: str, *, override_capability: object) -> None:
        """Remove a lock. Caller's responsibility to verify capability
        BEFORE calling — this module trusts the caller's authorization
        check. The keystore session is the right caller."""
        with self._lock:
            self._locks.pop(key, None)

    def lock_status(self, key: str) -> Lock | None:
        with self._lock:
            return self._locks.get(key)

    def all_locks(self) -> dict[str, Lock]:
        with self._lock:
            return dict(self._locks)

    # ------- change listeners (registry-wide) -------

    def add_listener(self, fn: Callable[[ChangeRecord], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[ChangeRecord], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    # ------- bulk load (used by the file watcher) -------

    def load_dict(
        self,
        values: dict[str, Any],
        *,
        actor: str,
        source: str,
        override_capability: object | None = None,
    ) -> list[ChangeRecord]:
        """Apply a batch of values; returns the records for changes that
        actually happened (unchanged keys produce no record)."""
        records: list[ChangeRecord] = []
        for key, value in values.items():
            try:
                rec = self.write(
                    key,
                    value,
                    actor=actor,
                    source=source,
                    override_capability=override_capability,
                )
                if rec.old_value != rec.new_value:
                    records.append(rec)
            except (SchemaError, LockedConfigError):
                # Bulk-load is best-effort per key; the caller (the
                # watcher) decides whether to surface a hygiene finding.
                pass
        return records


# ---------------------------------------------------------------------------
# Module-singleton plumbing
# ---------------------------------------------------------------------------


_singleton_lock = threading.Lock()
_singleton: ConfigRegistry | None = None


def get_registry() -> ConfigRegistry:
    """Process-wide singleton."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ConfigRegistry()
    return _singleton


def reset_for_testing() -> None:
    """Tests use this between cases to get a clean registry."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "ChangeRecord",
    "ConfigRegistry",
    "Field",
    "Lock",
    "LockedConfigError",
    "SchemaError",
    "get_registry",
    "reset_for_testing",
]
