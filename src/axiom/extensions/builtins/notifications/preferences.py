# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Recipient-preferences primitive — friction-killer track #5.

A ``@handle`` resolves to an ordered list of ``(channel, address)`` pairs
filtered by priority floor + classification ceiling. One ``send()``
fans out across the operator's preferred channels per
``(classification, priority)``.

Per spec-axiom-notifications §4 (classification routing), §7 (send
façade), §8 (receipts + channel preferences).

ADR-052: storage layered behind a ``RecipientPreferenceStore`` Protocol;
the Postgres backend uses ``axiom.infra.db.session_for("notifications")``
exclusively — never a private engine, never a hand-crafted ``schema=``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapterRegistry,
)
from axiom.extensions.builtins.notifications.db_models import Base
from axiom.extensions.builtins.notifications.send import (
    ChannelPreferences,
    Priority,
)
from axiom.governance import Classification

# ---- Priority ordering ------------------------------------------------------

_PRIORITY_ORDER: dict[Priority, int] = {
    Priority.LOW: 0,
    Priority.NORMAL: 1,
    Priority.HIGH: 2,
    Priority.URGENT: 3,
}


def _priority_gte(actual: Priority, floor: Priority) -> bool:
    return _PRIORITY_ORDER[actual] >= _PRIORITY_ORDER[floor]


# ---- Value objects ----------------------------------------------------------


@dataclass(frozen=True)
class RecipientChannel:
    """One row in a recipient's ordered fan-out list.

    ``min_priority`` is the priority floor — the channel only fires when
    the send's priority is at or above this. ``address`` is the operator-
    visible identifier (``#alerts``, ``+15125550100``, ``ben@example.com``,
    ``@operator``); the actual secret/credential lookup happens at the
    KEEP layer at dispatch time and is not stored here.
    """

    channel: str
    address: str
    min_priority: Priority = Priority.LOW


@dataclass(frozen=True)
class RecipientProfile:
    """A ``@handle`` → ordered channels mapping.

    The order in ``channels`` IS the preference order — ``resolve_recipient``
    preserves it after filtering. Validation rules:

    - ``recipient`` MUST start with ``@`` (Matrix-style principal handle).
    - At least one channel.
    """

    recipient: str
    channels: tuple[RecipientChannel, ...]

    def __post_init__(self) -> None:
        if not self.recipient or not self.recipient.startswith("@"):
            raise ValueError(
                f"recipient must start with '@'; got {self.recipient!r}"
            )
        if not self.channels:
            raise ValueError("RecipientProfile requires at least one channel")


def resolve_recipient(
    profile: RecipientProfile,
    classification: Classification,
    priority: Priority,
    registry: ChannelAdapterRegistry,
) -> ChannelPreferences:
    """Return the ordered ``ChannelPreferences`` for this (class, priority).

    Rules:

    1. Drop channels whose ``min_priority`` exceeds ``priority``.
    2. Intersect with ``registry.admitted_for(classification)`` — channels
       whose ceiling can't admit the envelope are dropped (centralized
       per channels/base.py).
    3. Preserve the profile's declared order.
    4. Empty result → fall back to ``("inbox",)`` so something always
       delivers.
    """
    admitted_names = {p.name for p in registry.admitted_for(classification)}

    ordered: list[str] = []
    seen: set[str] = set()
    for ch in profile.channels:
        if not _priority_gte(priority, ch.min_priority):
            continue
        if ch.channel not in admitted_names:
            continue
        if ch.channel in seen:
            continue
        ordered.append(ch.channel)
        seen.add(ch.channel)

    if not ordered:
        return ChannelPreferences(ordered_channels=("inbox",))
    return ChannelPreferences(ordered_channels=tuple(ordered))


# ---- Store protocol + in-memory backend -------------------------------------


@runtime_checkable
class RecipientPreferenceStore(Protocol):
    """CRUD shape for recipient profiles. Backends: in-memory + Postgres."""

    def put(self, profile: RecipientProfile) -> None: ...
    def get(self, recipient: str) -> RecipientProfile | None: ...
    def delete(self, recipient: str) -> bool: ...
    def list(self) -> list[RecipientProfile]: ...


class InMemoryRecipientPreferenceStore:
    """Default store for tests + bootstrap (no Postgres dependency)."""

    def __init__(self) -> None:
        self._rows: dict[str, RecipientProfile] = {}

    def put(self, profile: RecipientProfile) -> None:
        self._rows[profile.recipient] = profile

    def get(self, recipient: str) -> RecipientProfile | None:
        return self._rows.get(recipient)

    def delete(self, recipient: str) -> bool:
        return self._rows.pop(recipient, None) is not None

    def list(self) -> list[RecipientProfile]:
        return sorted(self._rows.values(), key=lambda p: p.recipient)


# ---- Postgres backend (ADR-052) --------------------------------------------


class RecipientProfileRow(Base):
    """Per-recipient profile row.

    One row per ``@handle``; channel list serialized as JSON in declared
    order. Lives in the ``notifications`` schema per ADR-052; the schema
    is set by the connection's ``search_path``, never hardcoded here.
    """

    __tablename__ = "recipient_profiles"
    # No ``__table_args__ = {"schema": ...}`` — matches every other model
    # in db_models.py. The ``notifications`` schema is set per-connection
    # by ``axiom.infra.db.session_for("notifications")`` via search_path
    # (ADR-052 §D2). Hardcoding ``schema=`` on the model would break the
    # search_path contract and prevent the sqlite test seam from
    # rebinding the table into a schema-free MetaData.

    recipient: Mapped[str] = mapped_column(String, primary_key=True)
    channels_json: Mapped[list] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


def _profile_to_json(profile: RecipientProfile) -> list[dict]:
    return [
        {
            "channel": c.channel,
            "address": c.address,
            "min_priority": c.min_priority.value,
        }
        for c in profile.channels
    ]


def _profile_from_row(recipient: str, channels_json: list[dict]) -> RecipientProfile:
    chans = tuple(
        RecipientChannel(
            channel=row["channel"],
            address=row["address"],
            min_priority=Priority(row.get("min_priority", "low")),
        )
        for row in channels_json
    )
    return RecipientProfile(recipient=recipient, channels=chans)


@dataclass
class PostgresRecipientPreferenceStore:
    """Postgres-backed store. ADR-052 — uses ``session_for("notifications")``.

    The ``session_cm`` field is the only injection seam: production binds
    it to ``axiom.infra.db.session_for``; tests bind it to a sqlite
    session-factory context-manager (see ``tests/test_preferences.py``).
    """

    session_cm: object = field(default=None)
    """A context-manager callable returning a SQLAlchemy ``Session``."""

    def _session(self):
        if self.session_cm is None:
            from axiom.infra.db import session_for
            return session_for("notifications")
        return self.session_cm()

    def put(self, profile: RecipientProfile) -> None:
        payload = _profile_to_json(profile)
        with self._session() as s:
            row = s.get(RecipientProfileRow, profile.recipient)
            if row is None:
                row = RecipientProfileRow(
                    recipient=profile.recipient,
                    channels_json=payload,
                    updated_at=datetime.now(UTC),
                )
                s.add(row)
            else:
                row.channels_json = payload
                row.updated_at = datetime.now(UTC)
            s.commit()

    def get(self, recipient: str) -> RecipientProfile | None:
        with self._session() as s:
            row = s.get(RecipientProfileRow, recipient)
            if row is None:
                return None
            return _profile_from_row(row.recipient, list(row.channels_json))

    def delete(self, recipient: str) -> bool:
        with self._session() as s:
            row = s.get(RecipientProfileRow, recipient)
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def list(self) -> list[RecipientProfile]:
        with self._session() as s:
            rows = s.query(RecipientProfileRow).order_by(
                RecipientProfileRow.recipient
            ).all()
            return [
                _profile_from_row(r.recipient, list(r.channels_json))
                for r in rows
            ]


# ---- Profile-spec parsing (for CLI) ----------------------------------------


def parse_channel_spec(spec: str) -> tuple[RecipientChannel, ...]:
    """Parse ``"slack=#alerts,twilio-sms=+15125550100,email=ben@x,inbox"``.

    Bare tokens (e.g. ``inbox``) become channel entries with the operator's
    handle implied (address ``""``); the channel adapter decides how to
    fan that out at dispatch.

    Per-channel ``min_priority`` is encoded with ``@`` after the address:
    ``twilio-sms=+15125550100@urgent``.
    """
    if not spec or not spec.strip():
        raise ValueError("empty channel spec")
    out: list[RecipientChannel] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        channel, sep, address = token.partition("=")
        channel = channel.strip()
        address = address.strip()
        if not channel:
            raise ValueError(f"missing channel name in token {token!r}")
        min_priority = Priority.LOW
        # Priority floor is encoded with a trailing ``@<priority>`` —
        # use ``rpartition`` and only treat the suffix as a priority
        # when it parses; otherwise the ``@`` is part of an email
        # address (``ben@example.com``) and we leave it alone.
        if "@" in address:
            head, _, tail = address.rpartition("@")
            tail = tail.strip()
            valid_priorities = {p.value for p in Priority}
            if tail in valid_priorities:
                address = head.strip()
                min_priority = Priority(tail)
            elif tail and tail.isalpha() and head:
                # Trailing alpha-only suffix that isn't a priority →
                # operator typo (e.g. ``email=x@bogus``); fail loudly so
                # the misconfiguration surfaces.
                raise ValueError(
                    f"unknown priority {tail!r} in token {token!r}"
                )
        if not sep:
            address = ""
        out.append(
            RecipientChannel(
                channel=channel, address=address, min_priority=min_priority
            )
        )
    if not out:
        raise ValueError("channel spec parsed to zero entries")
    return tuple(out)


# ---- Process-level default store -------------------------------------------

_DEFAULT_STORE: RecipientPreferenceStore | None = None


def _try_build_postgres_default() -> RecipientPreferenceStore | None:
    """Try to build a Postgres-backed default; ``None`` if DB unavailable.

    HERALD-2a regression: prior versions returned an in-memory store
    unconditionally, which meant ``axi notifications recipient set``
    writes from one process were invisible to the daemon dispatch
    process — silently demoting routing to the inbox-fallback. The
    Postgres store rides the shared engine and survives process exit.
    """
    try:
        from axiom.infra.db import engine_for

        engine, _schema = engine_for("notifications")
        # Bootstrap: ``CREATE TABLE IF NOT EXISTS`` for the prefs row
        # so the store works on a fresh install even before
        # ``axi db migrate`` has run the extension's alembic history.
        # Per ADR-052 §D2 this is the search_path-scoped engine, so
        # the table lands in the ``notifications`` schema.
        RecipientProfileRow.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        return None
    return PostgresRecipientPreferenceStore()


def default_store() -> RecipientPreferenceStore:
    """Process-wide store shared by the CLI verbs + ``send()``.

    Resolution order:

    1. Whatever the test seam pinned via ``set_default_store``.
    2. A Postgres-backed store (``session_for("notifications")``) when
       the shared engine builds — this is what makes prefs survive
       process exit and what makes HERALD-2a route to Slack/Teams/email
       instead of falling back to the inbox channel.
    3. In-memory fallback when no DB is reachable (unit-test envs, dev
       boxes without ``axi db up``).
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = _try_build_postgres_default() or (
            InMemoryRecipientPreferenceStore()
        )
    return _DEFAULT_STORE


def set_default_store(store: RecipientPreferenceStore | None) -> None:
    """Test seam — swap or reset the process-level default store."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def iter_channels(profile: RecipientProfile) -> Iterable[RecipientChannel]:
    """Iterate channels in declared order — explicit helper for callers."""
    return iter(profile.channels)


__all__ = [
    "InMemoryRecipientPreferenceStore",
    "PostgresRecipientPreferenceStore",
    "RecipientChannel",
    "RecipientPreferenceStore",
    "RecipientProfile",
    "RecipientProfileRow",
    "default_store",
    "iter_channels",
    "parse_channel_spec",
    "resolve_recipient",
    "set_default_store",
]
